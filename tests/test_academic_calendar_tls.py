import asyncio
import ipaddress
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.services.academic_calendar import (
    KU_INTERMEDIATE_PATH,
    _fetch_html,
    _validated_ku_ssl_context,
)


def _certificate(
    subject: x509.Name,
    issuer: x509.Name,
    public_key,
    issuer_key,
    *,
    is_ca: bool,
    san: x509.SubjectAlternativeName | None = None,
) -> x509.Certificate:
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=1 if is_ca else None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=not is_ca,
                content_commitment=False,
                key_encipherment=not is_ca,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=is_ca,
                crl_sign=is_ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    if san is not None:
        builder = builder.add_extension(san, critical=False).add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _write_test_chain(tmp_path: Path):
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Root")])
    root = _certificate(root_name, root_name, root_key.public_key(), root_key, is_ca=True)

    intermediate_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    intermediate_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Test Intermediate")]
    )
    intermediate = _certificate(
        intermediate_name,
        root_name,
        intermediate_key.public_key(),
        root_key,
        is_ca=True,
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    leaf = _certificate(
        leaf_name,
        intermediate_name,
        leaf_key.public_key(),
        intermediate_key,
        is_ca=False,
        san=x509.SubjectAlternativeName(
            [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
        ),
    )

    root_path = tmp_path / "root.pem"
    intermediate_path = tmp_path / "intermediate.pem"
    leaf_path = tmp_path / "leaf.pem"
    key_path = tmp_path / "leaf-key.pem"
    root_path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    intermediate_path.write_bytes(intermediate.public_bytes(serialization.Encoding.PEM))
    leaf_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return root_path, intermediate_path, leaf_path, key_path


def _handshake_once(
    server_context: ssl.SSLContext,
    client_context: ssl.SSLContext,
    *,
    server_hostname: str = "localhost",
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    server_errors = []

    def serve():
        try:
            connection, _address = listener.accept()
            with connection:
                with server_context.wrap_socket(connection, server_side=True) as tls:
                    tls.recv(1)
        except (ssl.SSLError, OSError) as exc:
            server_errors.append(exc)
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            with client_context.wrap_socket(connection, server_hostname=server_hostname) as tls:
                tls.sendall(b"x")
    finally:
        thread.join(timeout=3)


def test_packaged_ku_context_preserves_chain_and_hostname_verification():
    context = _validated_ku_ssl_context()

    assert KU_INTERMEDIATE_PATH.is_file()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_leaf_only_server_fails_without_intermediate_and_succeeds_with_it(tmp_path):
    root_path, intermediate_path, leaf_path, key_path = _write_test_chain(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(leaf_path, key_path)

    roots_only = ssl.create_default_context(cafile=str(root_path))
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake_once(server_context, roots_only)

    roots_plus_intermediate = ssl.create_default_context(cafile=str(root_path))
    roots_plus_intermediate.load_verify_locations(cafile=str(intermediate_path))
    _handshake_once(server_context, roots_plus_intermediate)

    with pytest.raises(ssl.SSLCertVerificationError, match="Hostname mismatch"):
        _handshake_once(
            server_context,
            roots_plus_intermediate,
            server_hostname="wrong.example",
        )


def test_packaged_intermediate_expiry_and_replacement_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="validity window"):
        _validated_ku_ssl_context(now=datetime(2037, 1, 1, tzinfo=timezone.utc))

    replacement = tmp_path / "replacement.pem"
    mutated = KU_INTERMEDIATE_PATH.read_text(encoding="ascii").replace("MIIGTD", "MIIGTE", 1)
    replacement.write_text(mutated, encoding="ascii")
    with pytest.raises((ValueError, x509.InvalidVersion)):
        _validated_ku_ssl_context(intermediate_path=replacement)


def test_fetch_retries_only_certificate_failure_with_scoped_verified_context():
    request = httpx.Request("GET", "https://do.ku.edu.kw/ar/academic-calendar-0")
    verification_error = httpx.ConnectError("CERTIFICATE_VERIFY_FAILED", request=request)
    scoped_context = ssl.create_default_context()

    with (
        patch(
            "app.services.academic_calendar._request_official_html",
            new=AsyncMock(side_effect=[verification_error, "<html>official</html>"]),
        ) as request_html,
        patch(
            "app.services.academic_calendar._validated_ku_ssl_context",
            return_value=scoped_context,
        ) as build_context,
    ):
        result = asyncio.run(_fetch_html(str(request.url)))

    assert result == "<html>official</html>"
    assert request_html.await_args_list[0].args[1] is True
    assert request_html.await_args_list[1].args[1] is scoped_context
    build_context.assert_called_once_with()


def test_fetch_never_retries_non_certificate_errors_or_invalid_context():
    request = httpx.Request("GET", "https://do.ku.edu.kw/ar/academic-calendar-0")
    network_error = httpx.ConnectError("connection refused", request=request)
    with (
        patch(
            "app.services.academic_calendar._request_official_html",
            new=AsyncMock(side_effect=network_error),
        ) as request_html,
        patch("app.services.academic_calendar._validated_ku_ssl_context") as build_context,
        pytest.raises(httpx.ConnectError),
    ):
        asyncio.run(_fetch_html(str(request.url)))
    assert request_html.await_count == 1
    build_context.assert_not_called()

    verification_error = httpx.ConnectError("CERTIFICATE_VERIFY_FAILED", request=request)
    with (
        patch(
            "app.services.academic_calendar._request_official_html",
            new=AsyncMock(side_effect=verification_error),
        ) as request_html,
        patch(
            "app.services.academic_calendar._validated_ku_ssl_context",
            side_effect=ValueError("invalid intermediate"),
        ),
        pytest.raises(ValueError, match="invalid intermediate"),
    ):
        asyncio.run(_fetch_html(str(request.url)))
    assert request_html.await_count == 1


def test_correctly_chained_future_certificate_uses_normal_tls_path_only():
    with (
        patch(
            "app.services.academic_calendar._request_official_html",
            new=AsyncMock(return_value="<html>rotated chain works</html>"),
        ) as request_html,
        patch("app.services.academic_calendar._validated_ku_ssl_context") as build_context,
    ):
        result = asyncio.run(
            _fetch_html("https://do.ku.edu.kw/ar/academic-calendar-0")
        )

    assert result == "<html>rotated chain works</html>"
    request_html.assert_awaited_once()
    assert request_html.await_args.args[1] is True
    build_context.assert_not_called()
