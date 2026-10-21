"""
app/firebase.py

Initialises the Firebase Admin SDK once.
Called at application startup before any request is handled.

On Railway: set FIREBASE_SERVICE_ACCOUNT_JSON to the contents of your
            Firebase service account JSON file (single-line string).
            Download it from: Firebase Console → Project Settings → Service Accounts.

Locally:    Either set FIREBASE_SERVICE_ACCOUNT_JSON,
            or set GOOGLE_APPLICATION_CREDENTIALS to the file path.
"""

import json
import os
import logging

import firebase_admin
from firebase_admin import credentials
from google.auth.credentials import AnonymousCredentials

from .local_notification_test import enabled, validate_environment

logger = logging.getLogger(__name__)

_initialized = False


class _EmulatorCredential(credentials.Base):
    def get_credential(self):
        return AnonymousCredentials()


def init_firebase() -> None:
    """Initialise the Firebase Admin SDK. Safe to call multiple times."""
    global _initialized
    if _initialized:
        return

    if enabled():
        settings = validate_environment()
        firebase_admin.initialize_app(
            _EmulatorCredential(), {"projectId": settings.project_id}
        )
        logger.info("Firebase Admin SDK initialised for loopback emulators only.")
        _initialized = True
        return

    sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        try:
            cred_dict = json.loads(sa_json)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin SDK initialised from FIREBASE_SERVICE_ACCOUNT_JSON.")
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(
                "FIREBASE_SERVICE_ACCOUNT_JSON is set but could not be parsed as JSON. "
                f"Ensure it is a valid service account JSON string. Error: {exc}"
            ) from exc
    else:
        # Fallback: GOOGLE_APPLICATION_CREDENTIALS file path, or ADC (GCP)
        firebase_admin.initialize_app()
        logger.info(
            "Firebase Admin SDK initialised via GOOGLE_APPLICATION_CREDENTIALS / ADC."
        )

    _initialized = True
