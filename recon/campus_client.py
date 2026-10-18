#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
campus_client.py —— 顶端新闻(cn.dahebao) 校园跑 授权测试客户端

作用：不依赖 App / 模拟器，纯 Python 复现客户端请求（含网关签名）。

签名算法（真实现 dex 字节码 + 模拟器动态探针 + 服务端实测 三重确认）：
    plaintext = 参数按 String.compareTo 自然字典序，逐对 "key" + "=" + value 直接拼接（对间无分隔符）
    cipher    = AES/CBC/PKCS7Padding(plaintext, key=Ck.getAESsc(), iv=Ck.getAESIv())
    sign      = md5( base64(cipher) + getCurrentAppSecret() )

实测：本脚本算出的 sign 被 api.topnews.cn 接受（服务端返回业务错误而非签名错误）。

用法：
    python3 campus_client.py --demo                 # 自检 + 演示
    python3 campus_client.py --path /api/semester/currSemester
    python3 campus_client.py --path /api/runRecord/getList --token-file .token
    python3 campus_client.py --list                 # 列出已知端点

⚠️ 凭据红线：本脚本从不打印 token 内容；token 只从 --token-file 读（文件权限自管）。
⚠️ 只读优先：默认只调 GET 读接口。写接口（save/bind/appeal 等）需显式 --allow-write。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from campus_sign import APP_ID, sign  # noqa: E402

UA = "Mozilla/5.0 (Linux; Android 16; 24129PN74C) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36"

# 接受我们签名的网关（H5 校园跑模块走这个）
GATEWAY = "https://api.topnews.cn"

# 另一套网关（原生 DDCampusRun，签名算法不同 —— 实测拒绝本签名）
NATIVE_GATEWAY = "https://project-api.dingxinwen.cn/noweb/DDCampusRun"

BASE = {
    "app_id": APP_ID,
    "appCurrentVersion": "8.5.1",
    "platformPublic": "Android",
    "phoneModel": "24129PN74C",
    "systemOS": "16",
    "channelPackage": "pro",
    "deviceId": "00000000000000000000000000000000",
    "screenW": "1200",
    "screenH": "2618",
}

# H5 逆向笔记里提取的端点全表（23 个）
ENDPOINTS = {
    "跑步记录": ["/api/runRecord/getList", "/api/runRecord/detail",
                 "/api/runRecord/getAppealList"],
    "免跑申请": ["/api/applyFor/getList", "/api/applyFor/detail"],
    "教师审核": ["/api/teacherReport/appealList", "/api/teacherReport/applyForList",
                 "/api/teacherReport/studentList", "/api/teacherReport/report"],
    "师生绑定": ["/api/studentTeacher/getList"],
    "报表排行": ["/api/runReport/myCurr", "/api/runReport/schoolReport", "/api/runReport/topList"],
    "积分":     ["/api/points/myPoints"],
    "学期":     ["/api/semester/currSemester", "/api/semester/getList"],
    "登录态":   ["/api/login/personalInfo", "/api/user/baseInfo"],
}

WRITE_PATHS = ("/save", "/bind", "/appeal", "/unbind", "/Audit")


def build_params(extra=None):
    p = dict(BASE)
    p["timestamp"] = str(int(time.time()))
    if extra:
        p.update(extra)
    p["sign"] = sign(p)
    return p


def request(path, extra=None, token=None, method="GET", body=None,
            gateway=GATEWAY, timeout=15):
    """发一个已签名请求。返回 (status, text)"""
    p = build_params(extra)
    url = f"{gateway}{path}?{urlencode(p)}"
    h = {"User-Agent": UA, "Accept": "application/json"}
    if token:
        # 头名未确认；这里只做占位，实际测试时按需替换
        h["token"] = token
    data = None
    if body is not None:
        h["Content-Type"] = "application/json; charset=utf-8"
        data = body.encode()
    req = urllib.request.Request(url, headers=h, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"ERR {type(e).__name__}: {e}"


def demo():
    print("=== 1) 签名自检（对真实 oracle 样本）===")
    from campus_sign import selftest
    if not selftest():
        print("签名自检失败！")
        return 1
    print("\n=== 2) 用自算签名打真实网关（证明签名被服务端接受）===")
    for path in ("/api/semester/currSemester", "/api/runRecord/getList",
                 "/api/teacherReport/studentList"):
        st, txt = request(path)
        verdict = ("签名通过（服务端进入业务逻辑）" if "请登录" in txt or '"code":0' in txt
                   else "签名被拒")
        print(f"  {path:36s} [{st}] {txt[:80]}")
        print(f"      判定: {verdict}")
    print("\n=== 3) 对照：同一签名打原生网关（另一套算法）===")
    st, txt = request("/api/semester/currSemester", gateway=NATIVE_GATEWAY)
    print(f"  {NATIVE_GATEWAY}/api/semester/currSemester")
    print(f"      [{st}] {txt[:100]}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--path")
    ap.add_argument("--token-file", help="含 session token 的文件（内容不会被打印）")
    ap.add_argument("--extra", help='额外参数 JSON，如 \'{"id":"123"}\'')
    ap.add_argument("--gateway", default=GATEWAY)
    ap.add_argument("--allow-write", action="store_true", help="允许调用写接口（默认禁止）")
    args = ap.parse_args()

    if args.list:
        for grp, paths in ENDPOINTS.items():
            print(f"[{grp}]")
            for p in paths:
                print(f"   {p}")
        return 0

    if args.demo or not args.path:
        return demo()

    if any(w in args.path for w in WRITE_PATHS) and not args.allow_write:
        print(f"拒绝：{args.path} 看起来是写接口。加 --allow-write 才允许（会真实改动数据）。")
        return 2

    token = None
    if args.token_file:
        with open(args.token_file) as f:
            token = f.read().strip()
        print(f"（已从 {args.token_file} 读取 token，长度 {len(token)}，内容不回显）")

    extra = json.loads(args.extra) if args.extra else None
    st, txt = request(args.path, extra=extra, token=token, gateway=args.gateway)
    print(f"[{st}] {txt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
