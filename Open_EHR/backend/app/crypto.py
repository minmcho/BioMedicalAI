"""Field-level envelope encryption — Phase 10, Track K; key
management Phase 17, BD-51 (app.keymgmt).

Envelope scheme: every protected value gets its own random data key
(DEK); the value is AES-256-GCM encrypted under the DEK, and the DEK
is wrapped (AES-256-GCM) under the master key (KEK). What is stored
is a self-describing envelope: {v, kid, wk (wrapped DEK), n (nonce),
ct} — the key id travels WITH the ciphertext, so key rotation never
orphans old values: a reader resolves the KEK by kid.

Phase 17 (BD-51/CMP-30): KEK resolution goes through the key-provider
interface in app.keymgmt. The default provider is `env` — the Phase
10 behaviour, byte-for-byte (OPENEHR_MASTER_KEY /
OPENEHR_MASTER_KEY_FILE / the local dev key at
~/.opencare/dev-master-key, outside the repo, mode 0600). Setting
OPENEHR_KMS_PROVIDER=software-kms selects the local SOFTWARE-KMS
provider (a software key store — NOT AN HSM) whose keyring supports
rotation, DEK re-wrap and retirement: decrypt resolves the envelope's
kid through the provider, so pre-rotation values keep reading while
their key is `retiring`, and stop reading once it is retired.
`encrypt`/`decrypt` remain the only two functions callers use.
"""
import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import keymgmt


class CryptoError(Exception):
    """Encryption/decryption failure (tamper, wrong/retired key,
    bad envelope)."""


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _load_master_key() -> bytes:
    """The ACTIVE KEK bytes under the env/key-file provider — kept
    for backward compatibility with Phase 10 callers/tests. New code
    resolves keys through app.keymgmt providers."""
    try:
        return keymgmt.EnvKeyProvider().active()[1]
    except keymgmt.KeyMgmtError as exc:
        raise CryptoError(str(exc)) from exc


def _provider():
    try:
        return keymgmt.get_provider()
    except keymgmt.KeyMgmtError as exc:  # pragma: no cover - defensive
        raise CryptoError(str(exc)) from exc


def key_id() -> str:
    """The kid of the provider's ACTIVE KEK — stored inside every new
    envelope so readers can tell which KEK a value was wrapped under."""
    try:
        return _provider().active()[0]
    except keymgmt.KeyMgmtError as exc:
        raise CryptoError(str(exc)) from exc


def encrypt(plaintext: str) -> dict:
    """Encrypt one string value under the active KEK; returns the
    storable envelope dict."""
    try:
        kid, kek = _provider().active()
    except keymgmt.KeyMgmtError as exc:
        raise CryptoError(str(exc)) from exc
    dek = AESGCM.generate_key(bit_length=256)
    wrap_nonce = os.urandom(12)
    wrapped = AESGCM(kek).encrypt(wrap_nonce, dek, None)
    nonce = os.urandom(12)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), None)
    return {"v": 1, "kid": kid,
            "wk": _b64e(wrap_nonce + wrapped),
            "n": _b64e(nonce), "ct": _b64e(ciphertext)}


def is_envelope(value) -> bool:
    return (isinstance(value, dict) and value.get("v") == 1
            and isinstance(value.get("ct"), str) and isinstance(value.get("wk"), str)
            and isinstance(value.get("n"), str))


def decrypt(envelope: dict) -> str:
    """Decrypt an envelope produced by `encrypt`. The KEK is resolved
    by the envelope's kid through the active provider: an unknown or
    RETIRED kid fails, as does any tampering (the GCM tag check)."""
    if not is_envelope(envelope):
        raise CryptoError("Not an encryption envelope")
    kid = envelope.get("kid")
    try:
        kek = _provider().resolve(kid) if isinstance(kid, str) else None
    except keymgmt.KeyMgmtError as exc:
        raise CryptoError(str(exc)) from exc
    if kek is None:
        raise CryptoError(
            f"Envelope was wrapped under key '{kid}', which is unknown "
            "or retired and can no longer be resolved")
    try:
        wrapped = _b64d(envelope["wk"])
        dek = AESGCM(kek).decrypt(wrapped[:12], wrapped[12:], None)
        plaintext = AESGCM(dek).decrypt(_b64d(envelope["n"]),
                                        _b64d(envelope["ct"]), None)
    except (InvalidTag, ValueError) as exc:
        raise CryptoError("Ciphertext failed integrity verification") from exc
    return plaintext.decode("utf-8")
