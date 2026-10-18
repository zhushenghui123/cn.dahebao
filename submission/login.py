#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
login.py —— 顶端新闻(cn.dahebao) 账号+验证码登录（授权测试用）

链路（端点来自未掏空 dex 的 Retrofit 注解，host 来自 DomainServiceHelperKt.pro_common_server）：
    发码  POST https://common.dingxinwen.cn/user/risk/sms/sendCode
          form: u_cellphone, captchaType, devToken
    登录  POST https://common.dingxinwen.cn/user/risk/captchaLogin
          form: u_cellphone, captchaType, captcha, devToken, captchaVerifyParam
    两者都带 query 签名：timestamp + app_id + sign

⚠️ 为什么不是别的端点（已实测证伪）：
    - /user/sms/sendCode            → 恒 429「操作频繁」，非业务路由
    - /login/user/captchaLogin      → 对非法手机号也无条件 code:0 success，是桩接口（假成功）
    - user.topnews.cn 上的 /user/risk/* → 「请登录!」，risk 系列只在 common 域
    真路由的判据：common 域 /user/risk/sms/sendCode 对非法号返回「手机号格式有误」
                  user 域 /user/sms/verifyCaptcha  同样返回「手机号码格式错误」→ 真实业务校验

签名（关键：POST 分支会把【表单体也纳入签名】）：
    明文 = (query 基础参数 + 表单字段) 合并后，按 String.compareTo 自然序，
           逐对 "key" + "=" + value 直接拼接（对之间无分隔符）
    sign = md5( base64( AES-192-CBC-PKCS7(明文, Ck.getAESsc(), Ck.getAESIv()) ) + app_secret )

⚠️ 凭据红线：
    - 手机号 / 验证码 / token 一律通过交互输入或文件读取，**本脚本从不打印它们的值**
    - token 落盘到 .token（0600），只显示长度与前后各 4 位
"""
import argparse
import base64
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from campus_sign import APP_ID, AES_IV, AES_KEY, APP_SECRET  # noqa: E402

from cryptography.hazmat.primitives import padding  # noqa: E402
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

UA = "Mozilla/5.0 (Linux; Android 16; 24129PN74C) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36"
DEVICE_ID = "00000000000000000000000000000000"   # ← 换成你自己的设备 id；此处为占位
CAPTCHA_TYPE = os.environ.get("CAPTCHA_TYPE", "1")   # 场景码，字节码里取值被掏空，可覆盖
DEV_TOKEN = os.environ.get("DEV_TOKEN", "")          # 个推 clientId，可空
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")


def sign_post(params: dict) -> str:
    """POST 版签名：明文含全部参数（query + 表单体）"""
    plain = "".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    pad = padding.PKCS7(128).padder()
    data = pad.update(plain.encode("utf-8")) + pad.finalize()
    enc = Cipher(algorithms.AES(AES_KEY.encode()), modes.CBC(AES_IV.encode())).encryptor()
    ct = enc.update(data) + enc.finalize()
    return hashlib.md5((base64.b64encode(ct).decode() + APP_SECRET).encode()).hexdigest()


def enc_val(v: str) -> str:
    """字段值的 AES-192-CBC + 标准 Base64（与网关签名用的是同一把 key/iv 同一编码）"""
    pad = padding.PKCS7(128).padder()
    data = pad.update(v.encode("utf-8")) + pad.finalize()
    enc = Cipher(algorithms.AES(AES_KEY.encode()), modes.CBC(AES_IV.encode())).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def base_params() -> dict:
    return {
        "app_id": APP_ID,
        "appCurrentVersion": "8.5.1",
        "platformPublic": "Android",
        "phoneModel": "24129PN74C",
        "systemOS": "16",
        "channelPackage": "pro",
        "deviceId": DEVICE_ID,
        "screenW": "1200",
        "screenH": "2618",
        "timestamp": str(int(time.time())),
    }


def call(host: str, path: str, form: dict, timeout: int = 15):
    p = base_params()
    merged = dict(p)
    merged.update(form)
    merged["sign"] = sign_post(merged)
    url = f"https://{host}{path}?{urlencode(merged)}"
    req = urllib.request.Request(
        url, data=urlencode(form).encode(),
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"ERR {type(e).__name__}: {e}"


# ---- H5 生产请求的完全复刻（web 凭据 + 明文 md5 签名，与原生那套是两套）----
WEB_ID = "210710201732916889"        # 静态.topnews.cn 的 index.js 明文给的
WEB_SECRET = "594578ce51b456787f2e97ec76e2fff4"


def call_web(host: str, path: str, form: dict, in_query: bool = False, timeout: int = 15):
    """H5 形状：只带 timestamp/app_id/表单字段 + sign（sign = md5(排序k=v拼接 + web_secret)）。
    in_query=False 时参数只进 body（H5 的 POST 就是这么发的）。"""
    p = {"timestamp": str(int(time.time())), "app_id": WEB_ID}
    p.update(form)
    plain = "".join(f"{k}={p[k]}" for k in sorted(p))
    p["sign"] = hashlib.md5((plain + WEB_SECRET).encode()).hexdigest()
    q = p if in_query else {"timestamp": p["timestamp"], "app_id": p["app_id"], "sign": p["sign"]}
    url = f"https://{host}{path}?{urlencode(q)}"
    body = urlencode(p if not in_query else form).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"ERR {type(e).__name__}: {e}"


def send_code(phone: str):
    st, txt = call("common.dingxinwen.cn", "/user/risk/sms/sendCode",
                   {"u_cellphone": phone, "captchaType": CAPTCHA_TYPE, "devToken": DEV_TOKEN})
    return st, txt


def do_login(phone: str, code: str, captcha_verify_param: str = ""):
    st, txt = call("common.dingxinwen.cn", "/user/risk/captchaLogin",
                   {"u_cellphone": phone, "captchaType": CAPTCHA_TYPE, "captcha": code,
                    "devToken": DEV_TOKEN, "captchaVerifyParam": captcha_verify_param})
    return st, txt


def norm_phone(raw: str) -> str:
    """严格校验手机号：只认 ASCII 数字，按大陆号段规则判。
    为什么不用 str.isdigit()：isdigit 对全角『１３８…』、阿拉伯-印度数字、上标
    『²』都返回 True —— 中文输入法粘贴时极易混进来，服务端随后报「手机号格式有误」。
    """
    s = raw.strip()
    # 去掉常见的前缀写法
    for pre in ("+86", "0086", "86"):
        if s.startswith(pre) and len(s) - len(pre) == 11:
            s = s[len(pre):]
            break
    if not s.isascii() or not s.isdigit():
        return ""                      # 含全角/非 ASCII 数字
    if len(s) == 11 and s[0] == "1" and s[1] in "3456789":
        return s
    if len(s) == 11 and s.startswith("165"):
        return s
    return ""                          # 号段规则不过


def diag():
    """诊断「手机号格式有误」到底卡在哪。
    第 1 段用 verifyCaptcha（只校验、不发短信）判断号码本身过不过格式规则；
    第 2 段对 sendCode 的 captchaType / devToken 做小范围穷举 —— 只会给你自己的号发码，
    出现第一个非「格式有误」的响应就停。
    """
    from getpass import getpass
    raw = getpass("  请输入手机号（回显已关闭、不落盘）: ").strip()
    phone = norm_phone(raw)
    if not phone:
        print(f"  ❌ 本地校验就不通过：长度={len(raw.strip())}，ascii数字="
              f"{raw.strip().isascii() and raw.strip().isdigit()}")
        if any(ord(c) > 0x3000 and ord(c) < 0xFF20 for c in raw):
            print("     → 检测到**全角数字**（中文输入法粘贴常见），这就是服务端说「手机号格式有误」的原因。")
        print("     规则：11 位 ASCII 数字，第 1 位=1，第 2 位∈3-9。")
        return 2
    print(f"  → 本地校验通过（尾号 {phone[-4:]}）")

    print("\n【1/2】格式探针（不发短信）：user.topnews.cn/user/sms/verifyCaptcha")
    st, txt = call("user.topnews.cn", "/user/sms/verifyCaptcha",
                   {"u_cellphone": phone, "captcha": "000000"})
    print(f"  [{st}] {txt}")
    if "格式" in txt:
        print("  → 号码本身就被判为格式错误（可能是 10x/12x 开头、或带了前缀），")
        print("    那 sendCode 的报错是合理的，先核对号码。")
        return 0
    print("  → 号码格式通过（否则这里会说格式错误）。sendCode 报格式有误 = 参数组合问题。")

    print("\n【2/2】sendCode 参数穷举（最多 7 发，都是给你自己发；成功即停）")
    encrypted = enc_val(phone)          # u_cellphone 是否要求 AES+base64？
    variants = [
        ("captchaType=1", {"u_cellphone": phone, "captchaType": "1", "devToken": ""}),
        ("captchaType=0", {"u_cellphone": phone, "captchaType": "0", "devToken": ""}),
        ("captchaType=2", {"u_cellphone": phone, "captchaType": "2", "devToken": ""}),
        ("captchaType=1 + devToken=deviceId", {"u_cellphone": phone, "captchaType": "1", "devToken": DEVICE_ID}),
        ("只有 u_cellphone", {"u_cellphone": phone}),
        ("u_cellphone=AES+base64", {"u_cellphone": encrypted, "captchaType": "1", "devToken": ""}),
        ("明文号 + captchaType=login", {"u_cellphone": phone, "captchaType": "login", "devToken": ""}),
    ]
    for label, form in variants:
        st, txt = call("common.dingxinwen.cn", "/user/risk/sms/sendCode", form)
        print(f"  {label:<34} -> [{st}] {txt[:160]}")
        if "格式" not in txt and '"code":429' not in txt and "操作频繁" not in txt:
            print("  ★ 换出非「格式有误」的响应了 —— 用上面这组参数。")
            break
        time.sleep(2)
    else:
        print("  ✗ 7 组都还是同样报错 → 不是参数组合问题，往「号码本身格式」或风控滑块上查。")
    return 0


def save_token(tok: str):
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    os.chmod(TOKEN_FILE, 0o600)


def mask(s: str) -> str:
    if not s or len(s) <= 12:
        return f"***（长度 {len(s) if s else 0}）"
    return f"{s[:4]}…{s[-4:]}（长度 {len(s)}）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true",
                    help="只验证已有 .token 是否可用（不重新登录）")
    ap.add_argument("--skip-send", action="store_true",
                    help="跳过发码，直接进第 2 步（验证码仍从终端交互输入，不走命令行参数）")
    ap.add_argument("--diag", action="store_true",
                    help="诊断发码为什么报「手机号格式有误」（先不发短信的格式探针，再穷举参数）")
    args = ap.parse_args()

    if args.diag:
        print("=" * 68)
        print("发码诊断（授权测试）")
        print("=" * 68)
        return diag()

    print("=" * 68)
    print("顶端新闻 账号+验证码 登录（授权测试）")
    print("=" * 68)

    if args.verify:
        if not os.path.exists(TOKEN_FILE):
            print(f"没有 {TOKEN_FILE}")
            return 1
        with open(TOKEN_FILE) as f:
            tok = f.read().strip()
        print(f"已有 token：{mask(tok)}")
        st, txt = call("api.topnews.cn", "/api/semester/currSemester",
                       {"token": tok}, timeout=15)
        print(f"验证结果 [{st}]：{txt[:200]}")
        return 0

    print("\n第 1 步：手机号")
    from getpass import getpass
    raw = getpass("  请输入手机号（回显已关闭、不落盘）: ").strip()
    phone = norm_phone(raw)
    if not phone:
        print(f"  ❌ 本地校验不通过：长度={len(raw)}，纯ASCII数字={raw.isascii() and raw.isdigit()}")
        if any(0xFF10 <= ord(c) <= 0xFF19 for c in raw):
            print("     → 检测到**全角数字**（中文输入法粘贴常见），这正是服务端报「手机号格式有误」的典型原因。")
        print("     要求：11 位 ASCII 数字，第 1 位=1，第 2 位∈3-9。")
        return 2
    print(f"  → 手机号已收到（尾号 {phone[-4:]}）")

    if args.skip_send:
        print("  （--skip-send：跳过发码，请用 App 登录页收到的验证码）")
    else:
        print("  正在请求发码…")
        st, txt = send_code(phone)
        print(f"  服务端返回 [{st}]：{txt[:300]}")
        if '"code":429' in txt or "操作频繁" in txt:
            print("  ⏳ 被限流了（同 IP 短时多次触发）。等 3~5 分钟，")
            print("     或直接在 App 登录页点「获取验证码」，然后：python3 login.py --skip-send")
        elif '"code":1' in txt or '"code": 1' in txt:
            print("  ⚠️ 业务拒绝（手机号格式 / 未注册 / 需滑块 captchaVerifyParam），看上面 msg。")
        elif '"code":0' not in txt:
            print("  ⚠️ 未成功。若持续失败，用 App 收到的码：python3 login.py --skip-send")

    print("\n第 2 步：输入收到的验证码并登录")
    code = getpass("  请输入短信验证码（回显已关闭、不落盘）: ").strip()
    if not code:
        print("  未输入验证码，已中止。")
        return 2
    st, txt = do_login(phone, code)
    print(f"  服务端返回 [{st}]：{txt[:400]}")

    # 解析 token（不预设字段名，先把结构打出来）
    tok = None
    try:
        import json
        j = json.loads(txt)
        d = j.get("data")
        print(f"  data 结构：{type(d).__name__}，键 = {list(d.keys()) if isinstance(d, dict) else 'n/a'}")
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, str) and ("token" in k.lower() or "Token" in k):
                    tok = v
        if not tok and isinstance(d, list) and d and isinstance(d[0], dict):
            print(f"  data[0] 键 = {list(d[0].keys())}")
            for k, v in d[0].items():
                if isinstance(v, str) and "token" in k.lower():
                    tok = v
    except Exception as e:
        print(f"  解析失败：{e}")

    if tok:
        save_token(tok)
        print(f"\n✅ 登录成功，token 已保存到 {TOKEN_FILE}")
        print(f"   token = {mask(tok)}")
        print(f"   文件权限 = {oct(os.stat(TOKEN_FILE).st_mode)[-3:]}")
        print("\n   下一步：python3 campus_client.py --path /api/semester/currSemester --token-file .token")
    else:
        print("\n⚠️ 返回里没找到 token 字段。把上面的返回原文发我（如含敏感值请自行打码）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
