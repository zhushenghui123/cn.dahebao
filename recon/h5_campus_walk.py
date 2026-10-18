#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h5_campus_walk.py —— 带着我们的 token 逐个路由走官方校园跑 H5，抓取每个页面的真实请求

目的：补全模块地图里"参数未探明"的端点（尤其 runReport/topList），
     并记录各页面的请求 schema（方法/参数/体）——**纯观察，不点任何写按钮**。

做法：
  1) 用 add_init_script 在页面脚本执行前把 token 写进 localStorage（多个候选键，
     因为 API 层读哪个键在混淆代码里不好确认；写全部最稳）
  2) 依次打开各路由，收集所有 /api/ 请求（过滤掉 app_id/timestamp/token/sign 噪声）
输出：campus_walk_out.json
"""
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "campus_walk_out.json")

ENTRY = "https://static.topnews.cn/am/campusRunning/index.html"
UA = ("Mozilla/5.0 (Linux; Android 16; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
NOISE = {"app_id", "timestamp", "token", "sign"}

ROUTES = [
    ("#/", "首页"),
    ("#/rank", "排行榜"),
    ("#/apply-list", "免跑申请列表"),
    ("#/apply", "免跑申请提交页"),
    ("#/appeal-list", "申诉列表"),
    ("#/appeal", "申诉提交页"),
    ("#/teacher", "教师页"),
    ("#/teacher-manage", "教师管理"),
    ("#/apply-audit", "教师-免跑审批"),
    ("#/appeal-audit", "教师-申诉审批"),
]


def chromium_exe():
    """交给 Playwright 自己解析浏览器路径（不硬编码版本目录）。"""
    return ""


def main():
    tok = open(TOKEN_FILE).read().strip()
    from playwright.sync_api import sync_playwright

    captured = []
    cur_route = [""]

    with sync_playwright() as p:
        launch = dict(headless=True, args=["--ignore-certificate-errors", "--no-first-run",
                                           "--no-default-browser-check"])
        if os.path.exists("/usr/bin/google-chrome-stable"):
            launch["channel"] = "chrome"
        else:
            launch["executable_path"] = chromium_exe() or None
        b = p.chromium.launch(**launch)
        ctx = b.new_context(ignore_https_errors=True, user_agent=UA,
                            viewport={"width": 393, "height": 851}, is_mobile=True, has_touch=True)

        # ★ 在页面脚本执行前把 token 铺进 localStorage（多个候选键；内联 token）
        ctx.add_init_script(
            "(function(){ try { var tok = " + json.dumps(tok) + "; "
            "['ddToken','sessionToken','securitytoken','token']"
            ".forEach(function(k){ localStorage.setItem(k, tok); }); } catch(e) {} })();")

        pg = ctx.new_page()

        def on_req(r):
            u = r.url
            if "/api/" not in u:
                return
            try:
                q = {k: v for k, v in urllib.parse.parse_qsl(
                    urllib.parse.urlparse(u).query, keep_blank_values=True) if k not in NOISE}
                body = r.post_data
                captured.append({
                    "route": cur_route[0],
                    "method": r.method,
                    "host": urllib.parse.urlparse(u).netloc,
                    "path": urllib.parse.urlparse(u).path,
                    "query": q,
                    "body": (body[:300] if body else None),
                })
            except Exception:
                pass

        pg.on("request", on_req)

        try:
            pg.goto(ENTRY, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("入口打开失败:", str(e)[:120])
        pg.wait_for_timeout(5000)

        for route, name in ROUTES:
            cur_route[0] = f"{route} ({name})"
            try:
                pg.goto(ENTRY + route, wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(4500)
            except Exception as e:
                print(f"  {route} 打开失败: {str(e)[:80]}")
            # 顺手记一下页面可见文案（判断是否真的渲染了）
            try:
                txt = pg.evaluate("()=>document.body.innerText.replace(/\\n+/g,' | ').slice(0,90)")
            except Exception:
                txt = ""
            print(f"  走过 {route:<16} 已捕获请求 {len(captured)} 条 | 页面: {txt[:60]}")

        b.close()

    # 汇总：按端点聚合
    print("\n" + "=" * 78)
    print("各路由捕获到的 API 请求（已去掉 app_id/timestamp/token/sign）")
    print("=" * 78)
    seen = {}
    for c in captured:
        key = (c["method"], c["host"], c["path"])
        seen.setdefault(key, {"routes": set(), "query": c["query"], "body": c["body"]})
        seen[key]["routes"].add(c["route"])

    for (m, h, path), v in sorted(seen.items(), key=lambda x: x[0][2]):
        print(f"\n▶ {m} https://{h}{path}")
        print(f"   路由: {', '.join(sorted(v['routes']))}")
        if v["query"]:
            print(f"   query: {json.dumps(v['query'], ensure_ascii=False)[:200]}")
        if v["body"]:
            print(f"   body : {v['body'][:200]}")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"captured": captured,
                   "aggregated": [{"method": k[0], "host": k[1], "path": k[2],
                                   "routes": sorted(v["routes"]),
                                   "query": v["query"], "body": v["body"]}
                                  for k, v in seen.items()]}, f, ensure_ascii=False, indent=1)
    print(f"\n原始结果 -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
