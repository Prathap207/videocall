import json
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Util.Padding import unpad
import base64

def decrypt_aes_cbc(encrypted_data: str, password: str) -> dict:
    """
    Decrypt CryptoJS AES encrypted data (CBC + PBKDF2)
    """
    try:
        # CryptoJS format: Salted__<salt><data>
        decoded = base64.b64decode(encrypted_data)
        
        if not decoded.startswith(b'Salted__'):
            raise ValueError("Invalid header")

        salt = decoded[8:16]
        ciphertext = decoded[16:]

        # Derive key using PBKDF2 (same as CryptoJS)
        key_iv = PBKDF2(password, salt, dkLen=32 + 16, count=1000)
        key = key_iv[:32]
        iv = key_iv[32:]

        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = unpad(cipher.decrypt(ciphertext), AES.block_size)
        return json.loads(decrypted.decode('utf-8'))
    except Exception as e:
        print(f"❌ Decryption failed: {e}")
        return None