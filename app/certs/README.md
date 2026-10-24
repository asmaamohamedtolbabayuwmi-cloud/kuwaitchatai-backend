# KU calendar TLS intermediate

`sectigo-public-server-authentication-ca-dv-r36.pem` is the Sectigo Public
Server Authentication CA DV R36 intermediate currently referenced by the
Authority Information Access `CA Issuers` extension of the certificate served
by `do.ku.edu.kw`.

- Source: `http://crt.sectigo.com/SectigoPublicServerAuthenticationCADVR36.crt`
- Source encoding: DER; stored here as PEM
- Subject: `CN=Sectigo Public Server Authentication CA DV R36,O=Sectigo Limited,C=GB`
- Issuer: `CN=Sectigo Public Server Authentication Root R46,O=Sectigo Limited,C=GB`
- Validity: 2021-03-22 00:00:00 UTC through 2036-03-21 23:59:59 UTC
- SHA-256 DER fingerprint: `8c54c334b66ba4e426772af4a3f9136c19a1aec729fdb28c535c07a5a4ef22e0`

The certificate was validated as an intermediate against the normal operating
system trust roots with `openssl verify -untrusted`, including hostname
verification of the KU leaf certificate. Runtime code also checks the exact
fingerprint, CA constraint, subject, issuer, and validity window before adding
it to a KU-calendar-only SSL context. The context starts with normal system
trust roots; this file never replaces the root store and is never used by
OpenAI, Firebase, notification, or other HTTP clients.

The normal system TLS path is attempted first. This workaround is therefore
not required after KU begins serving a complete chain or rotates to a correctly
chained certificate. Replace this file and the expected metadata in
`app/services/academic_calendar.py` only after independently validating the new
intermediate against normal trusted roots.
