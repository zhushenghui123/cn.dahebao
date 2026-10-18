#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_endpoint_exists.py —— 用「三例对照」确认一个端点是否存在（零副作用）

为什么需要它
    单个响应说明不了任何事。要给未知量找「已知锚点」：
      A 基准：明显不存在的路径   → "不存在"长什么样
      C 基准：已知 POST-only 的读接口，用 GET 打 → "存在但方法不对"长什么样
      B 目标：待确认的写接口     → 落在哪一边
    用错误的方法打写接口是【零副作用】的：路由匹配后立刻因方法不对被拒，
    业务逻辑一行都不会执行。

用法
    export SIGN_SECRET='...'                 # 或 --sign-secret
    export TOKEN='...'                        # 或 --token-file

    # 先看会发什么，不发请求
    python3 probe_endpoint_exists.py --host api.example.com \
        --target /api/x/save --known-post-only /api/x/list --dry-run

    # 真跑（3 个 GET）
    python3 probe_endpoint_exists.py --host api.example.com \
        --target /api/x/save --known-post-only /api/x/list

签名
    --sign-mode none           只发 timestamp/app_id（多数接口够用）
    --sign-mode md5-sorted     经典方案：md5( 按 key 排序的 "k=v" 直接拼接 + secret )
    其它方案请自行改 _sign()

⚠️ 纪律
    · 只发 3 个 GET；不做枚举。
    · 偶发超时/异常必须重测才能定性 —— 别把噪声当信号。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")


def _sign(params: dict, mode: str, secret: str) -> dict:
    """按需给 params 追加 sign。未知方案请改这里。"""
    if mode == "none":
        return params
    if mode == "md5-sorted":
        if not secret:
            sys.exit("--sign-mode md5-sorted 需要 --sign-secret 或环境变量 SIGN_SECRET")
        plain = "".join(f"{k}={params[k]}" for k in sorted(params))
        params = dict(params)
        params["sign"] = hashlib.md5((plain + secret).encode()).hexdigest()
        return params
    sys.exit(f"未知 --sign-mode: {mode}")


def call_get(host: str, path: str, params: dict, timeout: int = 15):
    url = f"https://{host}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), url
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), url
    except Exception as e:                      # 超时/连接问题：要重测，不能直接定性
        return -1, f"ERR {type(e).__name__}: {e}", url


def verdict(txt: str) -> str:
    """把响应映射成人类可读的判断（规则见 docs/03 §3 的反查表）。"""
    t = txt.strip()
    if not t:
        return "空响应"
    if t.startswith("<") or "Not Found" in t and "{" not in t:
        return "非 JSON（疑似网关/拦截页）"
    try:
        d = json.loads(t)
    except Exception:
        return "非 JSON"
    code = d.get("code")
    msg = str(d.get("msg", ""))
    if code == 403 and "请求类型" in msg:
        return "★ 路径存在，方法不对"
    if code == 1 and ("无权限" in msg or "权限" in msg):
        return "路径存在、方法对，鉴权拒绝"
    if code == 0:
        return "路径存在，全通"
    if code in (403, 404):
        return "路径不存在或不可达"
    if code == 500:
        return "到了业务层但异常（常见原因：缺必填参数）"
    return f"code={code} 未分类"


def main() -> int:
    ap = argparse.ArgumentParser(description="三例对照：确认端点是否存在（零副作用）")
    ap.add_argument("--host", required=True, help="目标主机（不含 https://）")
    ap.add_argument("--target", required=True, help="待确认的路径（通常是写接口）")
    ap.add_argument("--known-post-only", required=True, help="已知 POST-only 的路径，用作 C 基准")
    ap.add_argument("--nonexistent", default="/api/__no_such_path__", help="A 基准路径")
    ap.add_argument("--app-id", default="", help="可选：公共 app_id")
    ap.add_argument("--sign-mode", default="none", choices=["none", "md5-sorted"])
    ap.add_argument("--sign-secret", default=os.environ.get("SIGN_SECRET", ""))
    ap.add_argument("--token", default=os.environ.get("TOKEN", ""))
    ap.add_argument("--token-file", default="")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要发送的请求，不发")
    ap.add_argument("--out", default="probe_endpoint_exists_out.json")
    args = ap.parse_args()

    token = args.token
    if args.token_file:
        token = open(args.token_file, encoding="utf-8").read().strip()

    cases = [
        ("A 基准·不存在", args.nonexistent, {}),
        ("C 基准·存在但方法错", args.known_post_only, {"page": 1, "size": 1}),
        ("B 目标", args.target, {}),
    ]

    out, rows = [], []
    for label, path, form in cases:
        p = {"timestamp": str(int(time.time()))}
        if args.app_id:
            p["app_id"] = args.app_id
        if token:
            p["token"] = token
        p.update(form)
        p = _sign(p, args.sign_mode, args.sign_secret)

        if args.dry_run:
            safe = {k: ("<TOKEN>" if k == "token" else v) for k, v in p.items()}
            print(f"[{label}] GET https://{args.host}{path}\n  ?{urllib.parse.urlencode(safe)}\n")
            continue

        st, txt, url = call_get(args.host, path, p)
        v = verdict(txt)
        rows.append((label, st, v, txt[:200]))
        out.append({"label": label, "path": path, "http": st,
                    "verdict": v, "resp": txt[:400], "url_masked": url.split("token=")[0]})
        print(f"[{label}]  HTTP {st}  →  {v}")
        print(f"  {txt[:220]}\n")

    if args.dry_run:
        print("（dry-run：以上 3 条请求未发送）")
        return 0

    print("=" * 66)
    print("判读：B 与 C 同形 ⇒ 目标【存在】但只接受其它方法；")
    print("      B 与 A 同形 ⇒ 目标【不存在】；B 与两者都不同 ⇒ 需要重测排除抖动。")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
