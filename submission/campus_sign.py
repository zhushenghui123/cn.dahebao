#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
campus_sign.py —— 顶端新闻(cn.dahebao) 网关签名复现（授权测试）

算法（从「未掏空的真实现 dex」字节码 + 模拟器动态探针双重确认）：
    plaintext = 参数按 String.compareTo 自然字典序，逐对 "key" + "=" + value 直接拼接（对之间无分隔符）
    cipher    = AES/CBC/PKCS7Padding(plaintext, key=Ck.getAESsc(), iv=Ck.getAESIv())
    sign      = md5( base64(cipher) + getCurrentAppSecret() )

app 内自证：md5(aes + secret) == oracle sign → MATCH = true
"""
import base64
import hashlib
from urllib.parse import parse_qsl, urlparse

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ---- 凭据（从模拟器动态探针 Ck.* 读取；仅用于本次授权测试）----
APP_ID = "253092578416035927"          # Ck.bip()
APP_SECRET = "77a85fcd3f8ab388189815cbaa642969"   # DomainServiceHelperKt.getCurrentAppSecret()
AES_KEY = "Uag4j7WJqE2s8eFr7PfM3Hb9"   # Ck.getAESsc()  24 chars -> AES-192
AES_IV = "tW6rA3bHaUja3Pkg"            # Ck.getAESIv()  16 chars


def _plaintext(params: dict) -> str:
    """自然字典序（String.compareTo），k=v 无分隔符拼接"""
    return "".join(f"{k}={params[k]}" for k in sorted(params.keys()))


def sign(params: dict, key: str = AES_KEY, iv: str = AES_IV,
         secret: str = APP_SECRET) -> str:
    plain = _plaintext(params)
    p = padding.PKCS7(128).padder()
    data = p.update(plain.encode("utf-8")) + p.finalize()
    e = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
    ct = e.update(data) + e.finalize()
    return hashlib.md5((base64.b64encode(ct).decode() + secret).encode()).hexdigest()


def sign_url(url: str) -> str:
    """给一个 URL，补上 sign，返回新 URL"""
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q.pop("sign", None)
    q["sign"] = sign(q)
    from urllib.parse import urlencode
    return f"{u.scheme}://{u.netloc}{u.path}?{urlencode(q)}"


# ---- 自测：对 3 条真实 oracle 样本验证 ----
# ⚠️ 例外声明：这 3 条样本含 **2 个不同 deviceId**（同型号模拟器不同实例），未做替换 ——
#    它们是"App 自己签出来的真实 URL"，是算法自证的证据；替换等于把证据换成自己的输出。
#    详见仓库 README〈三、凭据与个人数据〉。
ORACLE = [
    ("https://api.topnews.cn/api/common/config?"
     "app_id=253092578416035927&appCurrentVersion=8.5.1&platformPublic=Android"
     "&phoneModel=google%20sdk_gphone64_x86_64&systemOS=14&timestamp=1791988783"
     "&channelPackage=pro&deviceId=5f879e113c8706c68122fb79d5341c0"
     "&screenW=1080&screenH=2400&sign=5981f173d4d8443a1344e5ee2c99ceb4"),
    ("https://api.topnews.cn/api/common/config?"
     "app_id=253092578416035927&appCurrentVersion=8.5.1&platformPublic=Android"
     "&phoneModel=google%20sdk_gphone64_x86_64&systemOS=14&timestamp=1791989152"
     "&channelPackage=pro&deviceId=fd87e78dd5a1db4b9ff28b42feac124"
     "&screenW=1080&screenH=2400&sign=36eb6dcaa2a5570d088ea7d41e4089ab"),
    ("https://project-api.dingxinwen.cn/noweb/DDCampusRun/api/school/currSchoolName?"
     "app_id=253092578416035927&appCurrentVersion=8.5.1&platformPublic=Android"
     "&phoneModel=google%20sdk_gphone64_x86_64&systemOS=14&timestamp=1791988783"
     "&channelPackage=pro&deviceId=5f879e113c8706c68122fb79d5341c0"
     "&screenW=1080&screenH=2400&sign=5981f173d4d8443a1344e5ee2c99ceb4"),
]


def selftest():
    ok = 0
    for url in ORACLE:
        u = urlparse(url)
        q = dict(parse_qsl(u.query, keep_blank_values=True))
        want = q.pop("sign")
        got = sign(q)
        flag = "✓" if got == want else "✗"
        if got == want:
            ok += 1
        print(f"  {flag} {u.netloc}{u.path}")
        print(f"      期望 {want}")
        print(f"      实算 {got}")
    print(f"\n{ok}/{len(ORACLE)} 条一致")
    return ok == len(ORACLE)


if __name__ == "__main__":
    import sys
    print("=== 自测：复现真实 oracle 样本的 sign ===")
    sys.exit(0 if selftest() else 1)
