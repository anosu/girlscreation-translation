"""Girls Creation CDN signatures and master-data decryption."""

import base64
import hashlib
import json
import zlib
from datetime import datetime, timedelta
from typing import Any

from Crypto.Cipher import DES3
from Crypto.Util.Padding import unpad

KEY = "amvBZLfOUWwAXoVu8xxGwibrwqGsneLR"
MASTER_KEY = "DMM.OLG.Unity.Engine.MasterLoader"


def decrypt_3des(ciphertext: bytes, key: bytes) -> bytes:
    key_bytes = hashlib.md5(key).digest()
    cipher = DES3.new(key_bytes, DES3.MODE_ECB)
    return unpad(cipher.decrypt(ciphertext), DES3.block_size)


def get_url_params(path, hash) -> dict[str, Any]:
    expire_time = datetime.now() + timedelta(hours=2)
    expire_time = expire_time.replace(minute=0, second=0, microsecond=0)
    t = int(expire_time.timestamp())
    key_str = f"{KEY}{path}{t}".encode()
    md5 = hashlib.md5(key_str).digest()
    s = base64.urlsafe_b64encode(md5).decode()[:-2]
    return {"s": s, "t": t, "h": hash}


def decrypt_master_text(b64_text: str | bytes | memoryview | bytearray):
    ciphertext = base64.b64decode(b64_text)
    compressed_text = decrypt_3des(ciphertext, MASTER_KEY.encode())
    compressed_data = base64.b64decode(compressed_text)
    decompressed_data = zlib.decompress(compressed_data, -zlib.MAX_WBITS)
    return json.loads(decompressed_data)
