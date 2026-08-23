"""Encrypted local credential storage for the Nugs downloader."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Dict, Optional

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Random import get_random_bytes


SALT_SIZE = 16
NONCE_SIZE = 12
KEY_SIZE = 32
PBKDF2_ROUNDS = 250_000


def _credentials_path(config_path: Path) -> Path:
    override = os.environ.get("NUGS_CREDENTIALS_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return config_path.with_name("nugs_credentials.enc")


def _key_path(config_path: Path) -> Path:
    override = os.environ.get("NUGS_CREDENTIALS_KEY_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return _credentials_path(config_path).with_suffix(".key")


def credential_paths(config_path: Path) -> Dict[str, Path]:
    return {
        "credentials": _credentials_path(config_path),
        "key": _key_path(config_path),
    }


def _read_or_create_secret(path: Path) -> bytes:
    if path.exists():
        return base64.urlsafe_b64decode(path.read_text(encoding="utf-8").strip().encode("ascii"))
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = get_random_bytes(KEY_SIZE)
    path.write_text(base64.urlsafe_b64encode(secret).decode("ascii") + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret


def _derive_key(secret: bytes, salt: bytes) -> bytes:
    return PBKDF2(secret, salt, dkLen=KEY_SIZE, count=PBKDF2_ROUNDS)


def read_credentials(config_path: Path) -> Dict[str, str]:
    creds_path = _credentials_path(config_path)
    key_path = _key_path(config_path)
    if not creds_path.exists() or not key_path.exists():
        return {}

    try:
        envelope = json.loads(creds_path.read_text(encoding="utf-8"))
        salt = base64.b64decode(envelope["salt"])
        nonce = base64.b64decode(envelope["nonce"])
        tag = base64.b64decode(envelope["tag"])
        ciphertext = base64.b64decode(envelope["ciphertext"])
        secret = _read_or_create_secret(key_path)
        cipher = AES.new(_derive_key(secret, salt), AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        data = json.loads(plaintext.decode("utf-8"))
    except Exception:
        return {}

    return {key: str(value) for key, value in data.items() if value is not None}


def write_credentials(config_path: Path, credentials: Dict[str, Optional[str]]) -> None:
    creds_path = _credentials_path(config_path)
    key_path = _key_path(config_path)
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    secret = _read_or_create_secret(key_path)
    salt = get_random_bytes(SALT_SIZE)
    nonce = get_random_bytes(NONCE_SIZE)
    payload = {
        key: value
        for key, value in credentials.items()
        if value is not None and str(value) != ""
    }
    cipher = AES.new(_derive_key(secret, salt), AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(json.dumps(payload, sort_keys=True).encode("utf-8"))
    envelope = {
        "version": 1,
        "kdf": "PBKDF2-HMAC-SHA1",
        "rounds": PBKDF2_ROUNDS,
        "cipher": "AES-256-GCM",
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "tag": base64.b64encode(tag).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    creds_path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    try:
        creds_path.chmod(0o600)
    except OSError:
        pass
