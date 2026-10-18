#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
page_login.py —— 用【官方 H5 登录页】完成登录，我们只在旁边读 token

为什么这么做（而不是自己拼请求）：
  我们已经把官方请求逐字节复刻了（含空 token= 字段、签名公式经逐字符比对通过），
  但自己拼请求还要承担 Cookie/设备指纹/风控等一切不确定因素。
  官方页面自己发请求 → 自己过风控 → 成功后把 token 写进 cookie `Admin-Token`。
  我们只做三件事：填号、填验证码、读 cookie。

  这条路同时也是一个**判定器**：
    · 官方页面能收到短信 → 环境没问题，是请求细节问题（我们已对齐）
    · 官方页面也收不到  → 100% 是服务端风控/号段/IP 问题，跟我们无关，停止猜形状

凭据红线：手机号/验证码只用 getpass 从终端输入，不回显、不打印、不落盘；
         token 只显示前后各 4 位，落盘 .token（0600）。

用法：
  python3 page_login.py            # 无头
  python3 page_login.py --headed   # 有头（想肉眼看流程）
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from login import norm_phone  # noqa: E402

TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")
COOKIE_NAME = "Admin-Token"
LOGIN_URL = "https://m.topnews.cn/login"


def chromium_exe() -> str:
    """交给 Playwright 自己解析浏览器路径（不硬编码版本目录）。"""
    return ""


def mask(s: str) -> str:
    if not s:
        return "***"
    return f"{s[:4]}…{s[-4:]}（长度 {len(s)}）" if len(s) > 12 else f"***（长度 {len(s)}）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true",
                    help="无头运行（注意：滑块必须由人拖，无头时无法完成）")
    ap.add_argument("--selftest", action="store_true",
                    help="用非法号自检 UI 接线（页面会自行拦下，绝不发短信）")
    ap.add_argument("--no-chrome", action="store_true",
                    help="不用系统真 Chrome，改用 Playwright 自带 Chromium（阿里滑块通过率更低）")
    args = ap.parse_args()

    from getpass import getpass
    from playwright.sync_api import sync_playwright

    print("=" * 72)
    print("官方 H5 登录页驱动（我们只读 token，请求由官方页面自己发）")
    if not args.headless:
        print("  ⚠ 会打开一个浏览器窗口 —— 登录时若弹滑块，请在窗口里手动拖一下")
    print("=" * 72)

    if args.selftest:
        phone = "10000000000"          # 页面正则校验会拦下 → 零副作用
        print("  （--selftest：非法号，仅验证 UI 接线，不会发出任何请求）")
    else:
        phone = norm_phone(getpass("  ① 手机号（不回显、不落盘）: ").strip())
    if not phone:
        print("  本地格式校验不通过，中止。")
        return 2
    print(f"     → 尾号 {phone[-4:]}")

    api = []
    netlog = []
    console = []

    with sync_playwright() as p:
        # 用**真 Chrome**（本机 /usr/bin/google-chrome-stable）而不是 Playwright 自带 Chromium：
        # 阿里云验证码对自带 Chromium 的自动化特征很敏感，真 Chrome 通过率高得多
        launch_kw = dict(headless=args.headless,
                         args=["--ignore-certificate-errors",
                               "--no-first-run", "--no-default-browser-check",
                               "--disable-features=Translate,OptimizationHints",
                               "--disable-blink-features=AutomationControlled"])
        use_chrome = os.path.exists("/usr/bin/google-chrome-stable") and not args.no_chrome
        if use_chrome:
            launch_kw["channel"] = "chrome"
            print("  浏览器：真 Chrome（channel=chrome）")
        else:
            launch_kw["executable_path"] = chromium_exe() or None
            print("  浏览器：Playwright 自带 Chromium")
        b = p.chromium.launch(**launch_kw)
        ctx = b.new_context(
            ignore_https_errors=True,
            user_agent=("Mozilla/5.0 (Linux; Android 16; Pixel 8) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"),
            viewport={"width": 393, "height": 851}, is_mobile=True, has_touch=True)

        def on_resp(r):
            u = r.url
            if any(k in u for k in ("sendCode", "captchaLogin", "/user/sms/", "secretLogin")):
                try:
                    api.append((u.split("/")[-1], r.status, r.text()[:220]))
                except Exception:
                    pass
            # 阿里云验证码的验证请求（判断滑块到底有没有被服务端接受）
            if "aliyun" in u or "captcha" in u.lower():
                try:
                    netlog.append((r.status, u[:150], (r.text() or "")[:120]))
                except Exception:
                    netlog.append((r.status, u[:150], "(body 读取失败)"))

        ctx.on("response", on_resp)
        # ★ 官方登录成功后回调里有 `uni.showToast(...); $router.back()` —— 页面会立刻导航走，
        #   页面一换我们就读不到 localStorage 了（上一版就是这样把 token 丢掉的）。
        #   两个对策：① 把 `history.back` 挡成空操作；② 每 500ms 把 localStorage.ddToken 镜像进 cookie
        #   （cookie 按域保存，页面怎么跳都能用 ctx.cookies() 读回来）
        ctx.add_init_script("""
        (() => {
          // ① 拦住官方回调里的 $router.back()，页面不再跳走
          try { window.history.back = function(){ console.log('[patch] history.back 被拦下'); }; } catch(e) {}
          // ② ★关键：hook setItem，token 写入的**那一瞬间**就同步镜像进 cookie
          //   （定时轮询会来不及 —— 官方写完 localStorage 立刻导航，实测 500ms 轮询都没抢到）
          try {
            const _set = Storage.prototype.setItem;
            Storage.prototype.setItem = function(k, v) {
              try {
                if (k === 'ddToken' && v) {
                  document.cookie = 'ddTokenMirror=' + encodeURIComponent(v) + ';path=/;max-age=7200';
                  console.log('[patch] ddToken 已镜像到 cookie, 长度 ' + String(v).length);
                }
              } catch(e) {}
              return _set.apply(this, arguments);
            };
          } catch(e) {}
          // ③ 兜底轮询（万一 setItem 走的不是 Storage.prototype）
          setInterval(() => {
            try {
              const t = window.localStorage.getItem('ddToken');
              if (t) document.cookie = 'ddTokenMirror=' + encodeURIComponent(t) + ';path=/;max-age=7200';
            } catch(e) {}
          }, 500);
        })();
        """)
        pg = ctx.new_page()
        pg.on("console", lambda m: console.append(f"{m.type}: {m.text[:160]}") if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: console.append("pageerror: " + str(e)[:200]))

        print(f"\n  打开 {LOGIN_URL} …")
        last_err = None
        for attempt in range(3):
            try:
                pg.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
                last_err = None
                break
            except Exception as e:
                last_err = e
                print(f"    第 {attempt+1} 次打开失败（{str(e)[:80]}），重试…")
                pg.wait_for_timeout(3000)
        if last_err:
            print("  ✗ 打不开登录页：", str(last_err)[:200])
            b.close()
            return 1
        pg.wait_for_timeout(3500)

        inputs = pg.locator("input")
        n = inputs.count()
        print(f"  页面输入框数: {n}")
        if n == 0:
            print("  ✗ 登录页没渲染出来，把页面文字发我。")
            b.close()
            return 1

        inputs.nth(0).fill(phone)

        # 勾选协议：真正的勾选框是 uni-view.agreeBtn（class 在 noAgree/agree 之间切），
        # 千万别点 .alert 里的 《协议》链接 —— 那会跳到协议正文页，按钮就找不到了（上一版踩过）
        before_url = pg.url
        agreed = None
        for sel in ['.agreeBtn', 'uni-view.agreeBtn', '.agreeBtn.noAgree']:
            loc = pg.locator(sel)
            if loc.count():
                try:
                    loc.first.click(timeout=3000)
                    agreed = sel
                    break
                except Exception:
                    continue
        if agreed:
            pg.wait_for_timeout(800)
            if pg.url != before_url:                      # 万一还是跳走了，退回登录页
                pg.go_back(wait_until="domcontentloaded")
                pg.wait_for_timeout(2500)
                print("  （协议点击跳走了，已返回登录页）")
            state = pg.evaluate("""()=>{
              const e=document.querySelector('.agreeBtn');
              return e? (e.className||'').toString() : '(无)';
            }""")
            print(f"  ① 勾选协议（{agreed}）→ class = {state}")
        else:
            print("  ⚠ 没找到协议勾选框")

        clicked = False
        for sel in ['.getRegCode', 'uni-text.getRegCode', '.buttonContain', 'text=获取验证码']:
            loc = pg.locator(sel)
            if loc.count():
                try:
                    loc.first.click(timeout=3000)
                    print(f"  ② 已点「获取验证码」（{sel}）")
                    clicked = True
                    break
                except Exception:
                    continue
        if not clicked:
            print("  ⚠ 没找到「获取验证码」按钮")

        pg.wait_for_timeout(5000)
        print("  发码相关响应：")
        for u, st, body in api[-3:]:
            print(f"    [{st}] {u} → {body[:180]}")
        if not api:
            print("    （页面没发出任何发码请求 —— 可能被前端校验拦下了）")
            print("    页面提示：", pg.evaluate("()=>document.body.innerText.replace(/\\n+/g,' | ').slice(0,180)"))

        if args.selftest:
            print("\n  ✅ 接线自检通过：勾选框与发码按钮都能点到，且非法号被前端拦下。")
            b.close()
            return 0

        try:
            code = getpass("\n  ③ 收到短信后输入验证码（不回显、不落盘；收不到就直接回车）: ").strip()
        except Exception:
            code = (input("  ③ 验证码（无法隐藏回显，收不到直接回车）: ") or "").strip()
        if code:
            try:
                if pg.locator("input").count() > 1:
                    pg.locator("input").nth(1).fill(code)
                else:
                    pg.locator("input").first.fill(code)
                print("  已填入验证码，页面上报中…")
            except Exception as e:
                print("  填码失败:", str(e)[:120])

        # ★ 官方流程：captchaLogin 返回 data.loginStatus==1 ⇒ 需要人机校验（阿里云滑块），
        #    页面会自动点开 #buttonAli；滑块由**人在浏览器窗口里拖**，拖成功后页面会再调一次
        #    captchaLogin（带 captchaVerifyParam），这次才写 localStorage.ddToken
        print("\n  ④ 浏览器窗口里若弹出滑块/验证框，请手动拖一下完成它。")
        print("     每 30 秒会自动重试一次（重新提交验证码 → 再弹滑块），最多 180 秒。")

        token = ""
        last_resubmit = 0
        for i in range(180):
            pg.wait_for_timeout(1000)
            try:
                vals = pg.evaluate("""()=>({
                    dd: window.localStorage.getItem('ddToken')||'',
                    adm: (document.cookie.match(/Admin-Token=([^;]+)/)||[])[1]||'',
                    hasBtnAli: !!document.getElementById('buttonAli'),
                    capEl: (document.getElementById('captcha-element')||{}).outerHTML?
                           document.getElementById('captcha-element').outerHTML.length : 0,
                    txt: (document.body.innerText||'').replace(/\\n+/g,' | ').slice(0,140)
                })""")
            except Exception:
                vals = {}
            token = vals.get("dd") or vals.get("adm") or ""
            if not token:
                # 页面可能已经导航走（evaluate 返回空）→ 改从 cookie 里拿镜像
                try:
                    import urllib.parse as _up
                    ck = {c["name"]: c["value"] for c in ctx.cookies()}
                    if ck.get("ddTokenMirror"):
                        token = _up.unquote(ck["ddTokenMirror"])
                        vals["src"] = "cookie 镜像"
                    elif ck.get(COOKIE_NAME):
                        token = ck[COOKIE_NAME]
                        vals["src"] = "cookie Admin-Token"
                except Exception:
                    pass
            if token:
                print(f"      ✅ 第 {i+1}s 拿到 token（来源：{vals.get('src') or ('localStorage.ddToken' if vals.get('dd') else 'cookie')}）")
                break

            if (i + 1) % 30 == 0:
                print(f"      …{i+1}s | #buttonAli存在={vals.get('hasBtnAli')} "
                      f"captcha元素长度={vals.get('capEl')}")
                print(f"        页面文字: {vals.get('txt')}")
                # 自动重试：清空再填回验证码 → 触发 watch → 重新 submit → 服务器再回 loginStatus=1 → 再弹滑块
                if code and i - last_resubmit >= 25:
                    try:
                        inp = pg.locator("input").nth(1) if pg.locator("input").count() > 1 else pg.locator("input").first
                        inp.fill("")
                        pg.wait_for_timeout(300)
                        inp.fill(code)
                        last_resubmit = i
                        print("        （已重新提交验证码，等滑块再弹一次）")
                    except Exception:
                        pass

        # 兜底：即使页面已跳走，storage_state 里仍保留各域名的 localStorage
        if not token:
            try:
                stt = ctx.storage_state()
                for o in stt.get("origins", []):
                    if "topnews" in o.get("origin", ""):
                        for kv in o.get("localStorage", []):
                            if kv.get("name") in ("ddToken", "Admin-Token") and kv.get("value"):
                                token = kv["value"]
                                print(f"      ✅ 从 storage_state({o['origin']}) 取到 {kv['name']}")
                                break
                    if token:
                        break
            except Exception as e:
                print("     storage_state 读取失败:", str(e)[:100])

        print("\n  登录相关响应：")
        for u, st, body in api[-4:]:
            print(f"    [{st}] {u} → {body[:200]}")
        if netlog:
            print("\n  人机校验（阿里云）相关请求 —— 判断滑块有没有被服务端接受：")
            for st, u, body in netlog[-8:]:
                print(f"    [{st}] {u} → {body[:110]}")
        else:
            print("\n  （没有捕获到任何阿里云验证请求 ⇒ 滑块可能压根没提交成功）")
        if console:
            print("\n  页面 console（最后 8 条）：")
            for c in console[-8:]:
                print(f"    {c}")
        b.close()

    if token:
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        os.chmod(TOKEN_FILE, 0o600)
        print(f"\n  ✅ token 已写入 {TOKEN_FILE}（0600）  token = {mask(token)}")

        # 立刻验证哪种形态能被校园跑接口接受（H5 里 App 注入的是 `u_token:<...>` 前缀格式）
        from login import call
        cands = [("原样", token)]
        if ":" in token:
            cands.append(("去掉前缀", token.split(":", 1)[1]))
        for label, cand in cands:
            st, txt = call("api.topnews.cn", "/api/semester/currSemester", {"token": cand})
            ok = "请登录" not in txt
            print(f"     验证[{label}] -> [{st}] {txt[:140]}  {'✅ 可用' if ok else '✗ 仍需登录'}")
        print("\n  把上面 ✅ 的那行告诉我，我按那条形态去跑校园跑端点。")
        return 0

    print("\n  ✗ 没拿到 token。把上面的响应发我 —— 这一步的输出是决定性证据：")
    print("     · 若发码响应是 code:0/成功但仍收不到短信 ⇒ 服务端/运营商侧问题")
    print("     · 若发码被拒 ⇒ 环境风控（IP/号段），该换网络而非继续改请求")
    return 1


if __name__ == "__main__":
    sys.exit(main())
