"""Passphrase encryption: AES-256-GCM with a scrypt KDF (needs `cryptography`).

File layout (version 1), all header bytes are authenticated as AAD:

    magic  "SBCE"  4 bytes
    version        1 byte  (1)
    log2(n)        1 byte  scrypt cost
    r              1 byte
    p              1 byte
    salt          16 bytes random
    nonce         12 bytes random
    ciphertext + 16 byte GCM tag
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

MAGIC = b"SBCE"
VERSION = 1
_HEADER = struct.Struct("!4sBBBB16s12s")
HEADER_SIZE = _HEADER.size
_LOG2_N = 15
_R = 8
_P = 1
_MAX_LOG2_N = 20  # refuse absurd costs from a tampered header


class CryptoUnavailable(RuntimeError):
    pass


class DecryptError(Exception):
    pass


def available() -> bool:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


def _modules():
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:
        raise CryptoUnavailable(
            "encryption needs the 'cryptography' package (pip install cryptography)"
        ) from exc
    return InvalidTag, AESGCM, Scrypt


def _derive(Scrypt, passphrase: str, salt: bytes, log2n: int, r: int, p: int) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=1 << log2n, r=r, p=p)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    if not passphrase:
        raise ValueError("empty passphrase")
    _, AESGCM, Scrypt = _modules()
    salt = os.urandom(16)
    nonce = os.urandom(12)
    header = _HEADER.pack(MAGIC, VERSION, _LOG2_N, _R, _P, salt, nonce)
    key = _derive(Scrypt, passphrase, salt, _LOG2_N, _R, _P)
    data = Path(src).read_bytes()
    ct = AESGCM(key).encrypt(nonce, data, header)
    Path(dst).write_bytes(header + ct)


def decrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    InvalidTag, AESGCM, Scrypt = _modules()
    blob = Path(src).read_bytes()
    if len(blob) < HEADER_SIZE + 16:
        raise DecryptError("file too short to be an encrypted backup")
    header = blob[:HEADER_SIZE]
    magic, version, log2n, r, p, salt, nonce = _HEADER.unpack(header)
    if magic != MAGIC:
        raise DecryptError("not an encrypted backup (bad magic)")
    if version != VERSION:
        raise DecryptError(f"unsupported encryption format version {version}")
    if not (1 <= log2n <= _MAX_LOG2_N) or not (1 <= r <= 32) or not (1 <= p <= 16):
        raise DecryptError("implausible KDF parameters in header")
    key = _derive(Scrypt, passphrase, salt, log2n, r, p)
    try:
        data = AESGCM(key).decrypt(nonce, blob[HEADER_SIZE:], header)
    except InvalidTag as exc:
        raise DecryptError("wrong passphrase or corrupted data") from exc
    Path(dst).write_bytes(data)


def load_passphrase(env: str | None = None, file: str | os.PathLike | None = None) -> str | None:
    """Read a passphrase from an environment variable or a file (never argv)."""
    if env and file:
        raise ValueError("use either an env var or a file for the passphrase, not both")
    if env:
        value = os.environ.get(env)
        if not value:
            raise ValueError(f"environment variable {env} is not set or empty")
        return value
    if file:
        text = Path(file).read_text(encoding="utf-8")
        value = text.rstrip("\r\n")
        if not value:
            raise ValueError(f"passphrase file {file} is empty")
        return value
    return None
