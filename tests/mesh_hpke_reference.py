"""Independent test-only X-Wing-11 decapsulation and RFC 9180 base schedule.

No production mesh code is imported. KEM uses pyca's component primitives,
not its HPKE wrapper; the key schedule uses only hashlib and hmac.
"""

import hashlib
import hmac


def kem_keys(seed):
    from cryptography.hazmat.primitives import hpke
    from cryptography.hazmat.primitives.asymmetric import mlkem, x25519

    expanded = hashlib.shake_256(seed).digest(96)
    ml = mlkem.MLKEM768PrivateKey.from_seed_bytes(expanded[:64])
    x = x25519.X25519PrivateKey.from_private_bytes(expanded[64:])
    public = ml.public_key().public_bytes_raw() + x.public_key().public_bytes_raw()
    return hpke.MLKEM768X25519PrivateKey(ml, x), public


def decapsulate(seed, enc):
    from cryptography.hazmat.primitives.asymmetric import mlkem, x25519

    assert len(seed) == 32 and len(enc) == 1120
    expanded = hashlib.shake_256(seed).digest(96)
    ml = mlkem.MLKEM768PrivateKey.from_seed_bytes(expanded[:64])
    x = x25519.X25519PrivateKey.from_private_bytes(expanded[64:])
    ss_m = ml.decapsulate(enc[:1088])
    ss_x = x.exchange(x25519.X25519PublicKey.from_public_bytes(enc[1088:]))
    return hashlib.sha3_256(
        ss_m + ss_x + enc[1088:] + x.public_key().public_bytes_raw() + bytes.fromhex("5c2e2f2f5e5c")
    ).digest()


def schedule(shared_secret, info, aead_id=2):
    suite_id = b"HPKE" + bytes.fromhex("647a0001") + aead_id.to_bytes(2, "big")

    def extract(salt, label, ikm):
        return hmac.digest(salt, b"HPKE-v1" + suite_id + label + ikm, "sha256")

    def expand(prk, label, context, length):
        labeled = length.to_bytes(2, "big") + b"HPKE-v1" + suite_id + label + context
        # Both key and nonce fit in a single SHA-256 block.
        assert length <= 32
        return hmac.digest(prk, labeled + b"\x01", "sha256")[:length]

    context = b"\x00" + extract(b"", b"psk_id_hash", b"") + extract(b"", b"info_hash", info)
    secret = extract(shared_secret, b"secret", b"")
    return expand(secret, b"key", context, 32), expand(secret, b"base_nonce", context, 12)


def aead(key, aead_id):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305

    return AESGCM(key) if aead_id == 2 else ChaCha20Poly1305(key)


def open_wrap(seed, wrapped, info, aead_id=2, aad=b""):
    key, nonce = schedule(decapsulate(seed, wrapped[:1120]), info, aead_id)
    return aead(key, aead_id).decrypt(nonce, wrapped[1120:], aad)
