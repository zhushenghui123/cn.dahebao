#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
campus_reverse.py —— 校园跑模块【接口层】系统性逆向（只读）

做法：23 个端点逐个探真实方法（GET 先试，遇 403「不支持的请求类型」改 POST），
     记录 code/msg/data 结构，并递归推导返回模型；写类端点一律不调用（只登记）。

钥匙（已取证）：
  host   : school-api.dingxinwen.cn（积分 points.dingxinwen.cn）
  sign   : md5( 排序 "k=v" 直连(空值计入) + web_secret )
  params : app_id=210710201732916889 & timestamp=<秒> & token=<u_token:...> & sign
输出：
  campus_reverse_out.json（原始，PII 已打码）
  docs/08-接口契约与数据模型.md（模块地图，由本脚本生成）
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")
OUT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "campus_reverse_out.json")
OUT_MD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs/08-接口契约与数据模型.md")

WEB_ID = "210710201732916889"
WEB_SECRET = "594578ce51b456787f2e97ec76e2fff4"
SCHOOL = "school-api.dingxinwen.cn"
POINTS = "points.dingxinwen.cn"
UA = ("Mozilla/5.0 (Linux; Android 16; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")

PII = {"name", "ddNickName", "avatar", "u_cellphone", "cellphone", "mobile",
       "idCard", "phone", "realName", "u_name", "studentTeacherName"}

# 端点清单（来自 campusRunning.deob.js 全量提取，23 个）
ENDPOINTS = [
    ("/api/login/personalInfo",              SCHOOL, "read",  {}),
    ("/api/login/unbind",                    SCHOOL, "write", None),
    ("/api/semester/currSemester",           SCHOOL, "read",  {}),
    ("/api/semester/getList",                SCHOOL, "read",  {}),
    ("/api/runRecord/getList",               SCHOOL, "read",  {"page": 1, "size": 5}),
    ("/api/runRecord/detail",                SCHOOL, "read",  {"id": 1}),
    ("/api/runRecord/getAppealList",         SCHOOL, "read",  {"page": 1, "size": 5}),
    ("/api/runRecord/appeal",                SCHOOL, "write", None),
    ("/api/applyFor/getList",                SCHOOL, "read",  {"page": 1, "size": 5}),
    ("/api/applyFor/detail",                 SCHOOL, "read",  {"id": 1}),
    ("/api/applyFor/save",                   SCHOOL, "write", None),
    ("/api/studentTeacher/getList",          SCHOOL, "read",  {}),
    ("/api/studentTeacher/bind",             SCHOOL, "write", None),
    ("/api/runReport/myCurr",                SCHOOL, "read",  {}),
    ("/api/runReport/schoolReport",          SCHOOL, "read",  {}),
    ("/api/runReport/topList",               SCHOOL, "read",  {"page": 1, "size": 5}),
    ("/api/points/myPoints",                 POINTS, "read",  {}),
    ("/api/teacherReport/studentList",       SCHOOL, "read",  {"page": 1, "size": 5}),
    ("/api/teacherReport/report",            SCHOOL, "read",  {}),
    ("/api/teacherReport/appealList",        SCHOOL, "read",  {"page": 1, "size": 5}),
    ("/api/teacherReport/applyForList",      SCHOOL, "read",  {"page": 1, "size": 5}),
    ("/api/teacherReport/appealAudit",       SCHOOL, "write", None),
    ("/api/teacherReport/applyForAudit",     SCHOOL, "write", None),
]


def sign_params(token, extra=None):
    p = {"app_id": WEB_ID, "timestamp": str(int(time.time())), "token": token}
    if extra:
        p.update({k: str(v) for k, v in extra.items()})
    plain = "".join(f"{k}={p[k]}" for k in sorted(p))
    p["sign"] = hashlib.md5((plain + WEB_SECRET).encode()).hexdigest()
    return p


def call(host, path, token, extra=None, method="GET", body=None, timeout=15):
    p = sign_params(token, extra)
    url = f"https://{host}{path}?" + urllib.parse.urlencode(p)
    hdr = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    data = None
    if method == "POST":
        hdr["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(body if body is not None else {}).encode()
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:
        return -1, f"ERR {type(e).__name__}: {e}"


def shape(o, depth=0):
    """递归描述返回结构（不含具体值，PII 键名保留但值隐去）"""
    if depth > 3:
        return "…"
    if isinstance(o, dict):
        return {k: ("***" if k in PII else shape(v, depth + 1)) for k, v in list(o.items())[:24]}
    if isinstance(o, list):
        return [shape(o[0], depth + 1)] + (["…×%d" % len(o)] if len(o) > 1 else []) if o else []
    if isinstance(o, str):
        return f"str({len(o)})"
    return type(o).__name__


def main():
    tok = open(TOKEN_FILE).read().strip()
    results = []
    print("=" * 84)
    print("校园跑模块 · 接口层系统性逆向（只读；写类端点不调用）")
    print(f"token = {tok[:7]}…{tok[-4:]}")
    print("=" * 84)

    for path, host, kind, extra in ENDPOINTS:
        if kind == "write":
            print(f"\n⏸ {path}  —— 写类，跳过（需授权批准）")
            results.append({"path": path, "host": host, "kind": kind,
                            "method": "-", "code": None, "msg": "未调用（写类）", "shape": None})
            continue

        # GET 先试；遇「不支持的请求类型」改 POST
        st, body = call(host, path, tok, extra, "GET")
        method = "GET"
        if "不支持的请求类型" in body:
            st, body = call(host, path, tok, extra, "POST", {})
            method = "POST"
        try:
            j = json.loads(body)
        except Exception:
            j = None
        code = j.get("code") if j else None
        msg = j.get("msg") if j else body[:70]
        shp = shape(j.get("data")) if j else None
        results.append({"path": path, "host": host, "kind": kind, "method": method,
                        "code": code, "msg": msg, "shape": shp})
        print(f"\n▶ {method:<4} {path}   [code={code} msg={msg}]")
        if shp is not None:
            print("    data 结构: " + json.dumps(shp, ensure_ascii=False)[:300])
        time.sleep(0.35)

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(f"\n原始结果 -> {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
