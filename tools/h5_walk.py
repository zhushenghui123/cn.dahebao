#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h5_walk.py —— 让官方 H5 自己跑一遍，从网络层抄下它发出的真实请求

为什么需要它
    「主机别猜、方法别猜、参数别猜」这三个坑，一次都能绕开：
    业务载体（H5）自己发出的请求**一定是对的** —— 它没有猜的余地，
    必须在代码里写死一个能用的地址，否则它自己也活不了。

核心做法（三件事）
    ① 注入登录态：在页面脚本执行前把 token 铺进 localStorage 的多个候选键
       （「它到底读哪个键」在混淆代码里不好确认 ⇒ 写全部最省事，副作用为零）
    ② 逐路由访问：SPA 是 hash 路由 + 代码分割，**没访问过的路由不会发请求**
    ③ 记账：把 /api/ 请求按 (方法, 主机, 路径) 聚合，滤掉认证噪声

两种拦截用法（别搞混）
    on("request")       只记账，请求照发出网  → 走【读】路由时用
    route(...)+abort()  在出网前掐断          → 碰【有副作用】的请求时用

用法
    pip install playwright && playwright install chromium
    TOKEN='...' python3 h5_walk.py --entry https://static.example.com/app/index.html \
        --routes '#/,#/rank,#/list' --wait 4500

⚠️ 纪律
    · 只走【读】路由；带表单/提交按钮的页面可以打开，但**不要点提交**。
    · 本工具默认不产生任何写操作；它只观察。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse

NOISE = {"app_id", "timestamp", "token", "sign", "nonce", "ts"}   # 认证噪声，记账时滤掉


def _chromium(playwright):
    """优先用系统真 Chrome（反自动化特征更少），否则退到 Playwright 自带。"""
    for cand in ("/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"):
        if os.path.exists(cand):
            return playwright.chromium.launch(headless=True, channel="chrome",
                                              args=["--ignore-certificate-errors",
                                                    "--no-first-run",
                                                    "--no-default-browser-check"])
    return playwright.chromium.launch(headless=True,
                                      args=["--ignore-certificate-errors",
                                            "--no-first-run"])


def main() -> int:
    ap = argparse.ArgumentParser(description="驱动官方 H5 抄真实请求（只读）")
    ap.add_argument("--entry", required=True, help="H5 入口地址")
    ap.add_argument("--routes", default="#/", help="逗号分隔的 hash 路由，按顺序走")
    ap.add_argument("--token", default=os.environ.get("TOKEN", ""))
    ap.add_argument("--token-file", default="")
    ap.add_argument("--wait", type=int, default=4500, help="每个路由等多久（ms）")
    ap.add_argument("--out", default="h5_walk_out.json")
    args = ap.parse_args()

    token = args.token
    if args.token_file:
        token = open(args.token_file, encoding="utf-8").read().strip()
    if not token:
        sys.exit("需要 --token 或环境变量 TOKEN")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("需要 playwright：pip install playwright && playwright install chromium")

    ua = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
    routes = [r.strip() for r in args.routes.split(",") if r.strip()]
    captured, cur = [], [""]

    with sync_playwright() as pw:
        browser = _chromium(pw)
        ctx = browser.new_context(ignore_https_errors=True, user_agent=ua,
                                  viewport={"width": 393, "height": 851},
                                  is_mobile=True, has_touch=True)
        # ① 注入：多候选键，一次全写（成本为零，比"确认它读哪个键"便宜）
        ctx.add_init_script(
            "(function(){try{var t=" + json.dumps(token) + ";"
            "['ddToken','sessionToken','securitytoken','token','u_token']"
            ".forEach(function(k){localStorage.setItem(k,t);});}catch(e){}})();")

        page = ctx.new_page()

        def on_req(req):
            if "/api/" not in req.url:
                return
            u = urllib.parse.urlparse(req.url)
            query = {k: v for k, v in
                     urllib.parse.parse_qsl(u.query, keep_blank_values=True)
                     if k not in NOISE}
            captured.append({
                "route": cur[0], "method": req.method, "host": u.netloc,
                "path": u.path, "query": query,
                "body": (req.post_data or "")[:300] or None,
            })

        page.on("request", on_req)

        try:
            page.goto(args.entry, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("入口打开失败:", str(e)[:150])
        page.wait_for_timeout(args.wait)

        # ② 逐路由访问（不访问就不会有请求）
        for r in routes:
            cur[0] = r
            sep = "" if "#" in args.entry else "#"
            url = f"{args.entry}{sep}{r}" if not args.entry.endswith("#") else f"{args.entry}{r}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(args.wait)
            except Exception as e:
                print(f"  {r} 打开失败: {str(e)[:80]}")
            # ③ 判据：页面有没有真的渲染（渲染成功 = 登录态有效 + 参数齐）
            try:
                txt = page.evaluate("()=>document.body.innerText.replace(/\\n+/g,' | ').slice(0,80)")
            except Exception:
                txt = ""
            print(f"  走过 {r:<18} 已捕获 {len(captured):>3} 条 | 页面: {txt[:50]}")

        browser.close()

    # 聚合：只看「它到底在跟谁要什么」
    seen = {}
    for c in captured:
        key = (c["method"], c["host"], c["path"])
        e = seen.setdefault(key, {"routes": set(), "query": c["query"], "body": c["body"]})
        e["routes"].add(c["route"])

    print("\n" + "=" * 74)
    print("各路由捕获到的 API 请求（已滤掉 app_id/timestamp/token/sign）")
    print("=" * 74)
    for (m, h, p), v in sorted(seen.items(), key=lambda x: x[0][2]):
        print(f"\n▶ {m} https://{h}{p}")
        print(f"   路由: {', '.join(sorted(v['routes']))}")
        if v["query"]:
            print(f"   query: {json.dumps(v['query'], ensure_ascii=False)[:200]}")
        if v["body"]:
            print(f"   body : {v['body'][:200]}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"captured": captured,
                   "aggregated": [{"method": k[0], "host": k[1], "path": k[2],
                                   "routes": sorted(v["routes"]),
                                   "query": v["query"], "body": v["body"]}
                                  for k, v in seen.items()]},
                  f, ensure_ascii=False, indent=1)
    print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
