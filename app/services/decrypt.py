import json
import base64
import hashlib
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int):
    """
    OpenSSL EVP_BytesToKey (MD5) derivation used by CryptoJS when encrypting with a passphrase.
    """
    d = b""
    prev = b""
    while len(d) < key_len + iv_len:
        prev = hashlib.md5(prev + password + salt).digest()
        d += prev
    return d[:key_len], d[key_len:key_len+iv_len]

def decrypt_cryptojs_openssl(b64_ciphertext: str, passphrase: str) -> dict | None:
    try:
        raw = base64.b64decode(b64_ciphertext)
        if not raw.startswith(b"Salted__"):
            raise ValueError("Not OpenSSL salted format (missing 'Salted__' header)")
        salt = raw[8:16]
        ciphertext = raw[16:]

        # Try common AES key sizes used by CryptoJS (256, 192, 128)
        for key_len in (32, 24, 16):
            key, iv = _evp_bytes_to_key(passphrase.encode("utf-8"), salt, key_len, 16)
            cipher = AES.new(key, AES.MODE_CBC, iv)
            plaintext = cipher.decrypt(ciphertext)
            try:
                plaintext = unpad(plaintext, AES.block_size)
                return json.loads(plaintext.decode("utf-8"))
            except Exception:
                # Wrong key size guess; try the next one
                continue

        raise ValueError("Failed to decrypt with AES-256/192/128. Wrong passphrase or corrupted data.")
    except Exception as e:
        print(f"❌ Decryption failed: {e}")
        return None
