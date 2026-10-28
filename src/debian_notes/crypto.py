"""Password-based encryption for locked notes, using only the standard library.

Key derivation: PBKDF2-HMAC-SHA256 (600k iterations) over a per-database salt
yields 64 bytes, split into an encryption key and a MAC key. Each payload is
encrypted with an HMAC-SHA256 keystream in counter mode under a fresh random
nonce, then authenticated with HMAC-SHA256 (encrypt-then-MAC). Note bodies are
stored as JSON; attachment files as MAGIC + nonce + tag + ciphertext.
"""

import base64
import hashlib
import hmac
import json
import os

ITERATIONS = 600_000
VERSION = 1
MAGIC = b"DNLOCK1\0"


class Keys:
    __slots__ = ("enc", "mac")

    def __init__(self, enc, mac):
        self.enc = enc
        self.mac = mac


def new_salt():
    return base64.b64encode(os.urandom(16)).decode()


def derive(password, salt_b64, iterations=ITERATIONS):
    raw = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt_b64), iterations, dklen=64)
    return Keys(raw[:32], raw[32:])


def verifier(keys):
    return hmac.new(keys.mac, b"debian-notes-password-check", hashlib.sha256).hexdigest()


def check(keys, expected):
    return hmac.compare_digest(verifier(keys), expected)


def _keystream(key, nonce, length):
    blocks = (length + 31) // 32
    return b"".join(hmac.digest(key, nonce + i.to_bytes(8, "big"), "sha256") for i in range(blocks))[:length]


def _xor(data, key, nonce):
    if not data:
        return b""
    stream = _keystream(key, nonce, len(data))
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(len(data), "big")


def _seal(keys, data):
    nonce = os.urandom(16)
    ct = _xor(data, keys.enc, nonce)
    return nonce, ct, hmac.digest(keys.mac, nonce + ct, "sha256")


def _open(keys, nonce, ct, tag):
    if not hmac.compare_digest(tag, hmac.digest(keys.mac, nonce + ct, "sha256")):
        raise ValueError("Wrong password or damaged note")
    return _xor(ct, keys.enc, nonce)


def encrypt(keys, plaintext):
    nonce, ct, tag = _seal(keys, plaintext.encode("utf-8"))
    return json.dumps({
        "v": VERSION,
        "n": base64.b64encode(nonce).decode(),
        "c": base64.b64encode(ct).decode(),
        "t": base64.b64encode(tag).decode(),
    })


def decrypt(keys, payload):
    obj = json.loads(payload)
    nonce = base64.b64decode(obj["n"])
    ct = base64.b64decode(obj["c"])
    tag = base64.b64decode(obj["t"])
    return _open(keys, nonce, ct, tag).decode("utf-8")


def encrypt_bytes(keys, data):
    """Binary payload for attachment files."""
    nonce, ct, tag = _seal(keys, data)
    return MAGIC + nonce + tag + ct


def decrypt_bytes(keys, blob):
    if not blob.startswith(MAGIC):
        raise ValueError("Not an encrypted attachment")
    blob = blob[len(MAGIC):]
    return _open(keys, blob[:16], blob[48:], blob[16:48])
