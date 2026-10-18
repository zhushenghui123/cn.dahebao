#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h5_campus_probe.py —— 打开官方校园跑 H5，用原生桥注入我们的 token，观察它真实发出的 API 请求

依据（H5 逆向笔记）：
  · 入口 https://static.topnews.cn/am/campusRunning/index.html
  · token 由原生通过 `window.ddWebToNativeGetToken(token)` 推给 H5（不是 H5 去要）
  · H5 侧没有签名逻辑，baseURL 不硬编码（相对路径或原生注入）

因此：我们只要在页面里调用那个桥函数，就能让 H5 以「已登录」身份跑起来，
然后从网络层读出它请求的真实主机 / 路径 / 头 / 体 —— 这是最直接的地面真值。

凭据红线：token 从 .token 读，输出里一律打码。
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ENTRY = "https://static.topnews.cn/am/campusRunning/index.html"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")
UA = ("Mozilla/5.0 (Linux; Android 16; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")


def chromium_exe():
    """交给 Playwright 自己解析浏览器路径（不硬编码版本目录）。"""
    return ""


def main():
    tok = open(TOKEN_FILE).read().strip()
    short = f"{tok[:7]}…{tok[-4:]}"

    from playwright.sync_api import sync_playwright

    api_reqs = []
    all_hosts = set()

    with sync_playwright() as p:
        launch = dict(headless=True,
                      args=["--ignore-certificate-errors", "--no-first-run",
                            "--no-default-browser-check"])
        if os.path.exists("/usr/bin/google-chrome-stable"):
            launch["channel"] = "chrome"
        else:
            launch["executable_path"] = chromium_exe() or None
        b = p.chromium.launch(**launch)
        ctx = b.new_context(ignore_https_errors=True, user_agent=UA,
                            viewport={"width": 393, "height": 851},
                            is_mobile=True, has_touch=True)
        pg = ctx.new_page()

        def on_req(r):
            try:
                h = r.url.split("/")[2] if "://" in r.url else ""
                all_hosts.add(h)
                if "/api/" in r.url or "/am/" in r.url and r.resource_type in ("xhr", "fetch"):
                    rec = {
                        "method": r.method,
                        "url": r.url.replace(tok, "***"),
                        "headers": {k: (v.replace(tok, "***") if tok in str(v) else v)
                                    for k, v in (r.headers or {}).items()},
                        "post": (r.post_data or "").replace(tok, "***")[:400],
                    }
                    api_reqs.append(rec)
            except Exception:
                pass

        pg.on("request", on_req)

        print(f"打开 {ENTRY} …")
        try:
            pg.goto(ENTRY, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("  打开失败:", str(e)[:150])
        pg.wait_for_timeout(4000)

        # 用原生桥把 token 注进去（App 就是这么给的）
        for fn in ("ddWebToNativeGetToken", "ddWebToNativeGetTokenNoLogin"):
            got = pg.evaluate("""(args) => {
                const [fn, tok] = args;
                if (typeof window[fn] === 'function') { try { window[fn](tok); return 'called'; } catch(e){ return 'err:'+e; } }
                return 'missing';
            }""", [fn, tok])
            print(f"  桥 {fn}: {got}   (token {short})")

        pg.wait_for_timeout(8000)

        ls = pg.evaluate("()=>({keys:Object.keys(localStorage), href:location.href, title:document.title})")
        print("\n页面:", json.dumps(ls, ensure_ascii=False)[:400])

        print(f"\n捕获到 /api/ 请求 {len(api_reqs)} 条：")
        for r in api_reqs[:20]:
            print(f"  {r['method']} {r['url']}")
            interesting = {k: v for k, v in r["headers"].items()
                           if k.lower() in ("token", "authorization", "sessiontoken", "securitytoken",
                                            "content-type", "cookie", "x-token", "custom_request_header")}
            if interesting:
                print(f"      headers: {json.dumps(interesting, ensure_ascii=False)[:300]}")
            if r["post"]:
                print(f"      body: {r['post'][:200]}")

        print(f"\n访问过的域名: {sorted(all_hosts)[:20]}")
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
