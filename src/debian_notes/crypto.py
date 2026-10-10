"""Password-based encryption for locked notes, using only the standard library.

Key derivation: PBKDF2-HMAC-SHA256 (600k iterations) over a per-database salt
yields 64 bytes, split into an encryption key and a MAC key. Each payload is
encrypted with an HMAC-SHA256 keystream in counter mode under a fresh random
nonce, then authenticated with HMAC-SHA256 (encrypt-then-MAC).
"""

import base64
import hashlib
import hmac
import json
import os

ITERATIONS = 600_000
VERSION = 1


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
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def encrypt(keys, plaintext):
    data = plaintext.encode("utf-8")
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(data, _keystream(keys.enc, nonce, len(data))))
    tag = hmac.new(keys.mac, nonce + ct, hashlib.sha256).digest()
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
    if not hmac.compare_digest(tag, hmac.new(keys.mac, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("Wrong password or damaged note")
    return bytes(a ^ b for a, b in zip(ct, _keystream(keys.enc, nonce, len(ct)))).decode("utf-8")
