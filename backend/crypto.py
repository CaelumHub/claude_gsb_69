"""Cryptographic primitives: hashing, ECDSA key generation, signing, addresses.

Uses the SECP256K1 curve (the same curve Bitcoin uses) via the ``cryptography``
package.  Addresses are derived from the SHA-256 digest of the uncompressed
public key, which gives a compact, collision-resistant identifier without a
separate RIPEMD pass.
"""

import hashlib

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

CURVE = ec.SECP256K1()


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #
def sha256(data) -> bytes:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).digest()


def sha256_hex(data) -> str:
    return sha256(data).hex()


def double_sha256(data) -> bytes:
    return sha256(sha256(data))


# --------------------------------------------------------------------------- #
# Key management
# --------------------------------------------------------------------------- #
def generate_private_key():
    return ec.generate_private_key(CURVE)


def private_key_to_hex(priv) -> str:
    """Serialize a private key object to a 64-char hex scalar."""
    value = priv.private_numbers().private_value
    return format(value, "064x")


def private_key_from_hex(hexstr: str):
    """Reconstruct a private key object from a 64-char hex scalar."""
    value = int(hexstr, 16)
    return ec.derive_private_key(value, CURVE)


def public_key_bytes(pub) -> bytes:
    return pub.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def public_key_from_bytes(raw: bytes):
    return ec.EllipticCurvePublicKey.from_encoded_point(CURVE, raw)


def public_key_hex(pub) -> str:
    return public_key_bytes(pub).hex()


def public_key_from_hex(hexstr: str):
    return public_key_from_bytes(bytes.fromhex(hexstr))


def address_from_public_key(pub) -> str:
    """Derive a 0x-prefixed address from a public key."""
    digest = hashlib.sha256(public_key_bytes(pub)).hexdigest()
    return "0x" + digest[:40]


def address_from_private_key(priv) -> str:
    return address_from_public_key(priv.public_key())


# --------------------------------------------------------------------------- #
# Signing / verification
# --------------------------------------------------------------------------- #
def sign(priv, data: bytes) -> str:
    """Produce a hex-encoded ECDSA/SHA-256 signature over ``data``."""
    sig = priv.sign(data, ec.ECDSA(hashes.SHA256()))
    return sig.hex()


def verify(pub, data: bytes, sig_hex: str) -> bool:
    """Verify a hex-encoded signature; never raises on malformed input."""
    try:
        pub.verify(bytes.fromhex(sig_hex), data, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def is_valid_address(addr: str) -> bool:
    if not isinstance(addr, str) or not addr.startswith("0x"):
        return False
    hexpart = addr[2:]
    return len(hexpart) == 40 and all(c in "0123456789abcdefABCDEF" for c in hexpart)


def is_valid_public_key(hexstr: str) -> bool:
    try:
        public_key_from_hex(hexstr)
        return True
    except Exception:
        return False
