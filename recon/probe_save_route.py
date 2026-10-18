#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_save_route.py —— 判定「提交接口是否也在 H5 网关开放」

只做 GET（对写接口而言 GET 无副作用：若路由存在会返回 403 不支持的请求类型，
与已实测的 runRecord/getList 等 POST-only 接口同款响应；若路由不存在则走另一条分支）。

对照设计（关键）：
  A. /api/__no_such_path__        → 不存在路由的基准响应
  B. /api/runRecord/save          → 目标：是否存在
  C. /api/runRecord/getList       → 已知 POST-only，作为「存在但方法不对」的基准响应
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from login import UA, WEB_ID, WEB_SECRET, TOKEN_FILE  # noqa: E402

HOST = "school-api.dingxinwen.cn"


def call_get(path, form=None, timeout=15):
    form = form or {}
    p = {"timestamp": str(int(time.time())), "app_id": WEB_ID}
    p.update(form)
    plain = "".join(f"{k}={p[k]}" for k in sorted(p))
    p["sign"] = __import__("hashlib").md5((plain + WEB_SECRET).encode()).hexdigest()
    url = f"https://{HOST}{path}?{urllib.parse.urlencode(p)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"ERR {type(e).__name__}: {e}"


def tok():
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def main():
    t = tok()
    assert t.startswith("u_token:"), "token 形状不对"
    cases = [
        ("A 对照-不存在路由", "/api/__no_such_path__", {}),
        ("B 目标-提交接口", "/api/runRecord/save", {}),
        ("C 对照-已知POST-only", "/api/runRecord/getList", {"page": 1, "size": 1}),
    ]
    out = []
    for label, path, form in cases:
        st, txt = call_get(path, {**form, "token": t})
        rec = {"label": label, "path": path, "http": st, "resp": txt[:400]}
        out.append(rec)
        print(f"[{label}] GET {path}\n  HTTP {st}\n  {txt[:300]}\n")
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "probe_save_route_out.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("→ probe_save_route_out.json")


if __name__ == "__main__":
    main()
