from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models


_PREFIX = "enc:v1:"


@lru_cache(maxsize=4)
def _fernet(key_material: str) -> Fernet:
    digest = hashlib.sha256(key_material.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _key_material() -> str:
    return (
        getattr(settings, "TOTP_ENCRYPTION_KEY", "")
        or settings.SECRET_KEY
    )


def encrypt_secret(value: str) -> str:
    text = str(value or "")
    if not text or text.startswith(_PREFIX):
        return text
    token = _fernet(_key_material()).encrypt(text.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt_secret(value: str) -> str:
    text = str(value or "")
    if not text or not text.startswith(_PREFIX):
        return text
    try:
        return _fernet(_key_material()).decrypt(
            text[len(_PREFIX):].encode("ascii")
        ).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError):
        return ""


class EncryptedSecretField(models.CharField):
    """CharField that exposes plaintext to Python and stores ciphertext."""

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        return decrypt_secret(value)

    def to_python(self, value):
        value = super().to_python(value)
        if value is None:
            return value
        return decrypt_secret(value)

    def get_prep_value(self, value):
        if value is None:
            return value
        return encrypt_secret(str(value))
