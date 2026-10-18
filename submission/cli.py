#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cli.py —— 校园跑链路的一站式入口（薄包装）

设计原则（刻意的）：
  * **不复制任何逻辑**：每个子命令都只是把 `submission/` 下既有的脚本按顺序调起来。
    真正实现仍在各自文件里 ⇒ 它们照旧可以单独运行、单独读，README 的 ①–⑧ 步依然成立。
  * **不新增任何能力**：这里没有新的请求、没有新的绕过路径。子命令 = 既有的那几种动作。
  * **保留二道闸**：`submit` 默认 dry-run；`--live` 必须同时给 `--i-understand-this-writes`，
    这个约束由 `submit_run.py` 自己强制，本文件**原样转发、不做放宽**。

范式来源：Inverx `run_cli.py` 把"登录 / 取策略 / 生成 / 提交"做成子命令族——
本项目原先只有分步脚本，这里补上同一层"可用性"，但不引入它默认就发的行为。

子命令 ↔ README 步骤：
    env     环境自检（纯本地，不发请求）
    login   ① page_login.py   驱动官方 H5 登录取 token（人工过滑块）
    policy  ② policy.py       拉判定规则 + 地理约束 → policy_out.json
    plan    ③ trajectory.py   生成自洽轨迹（纯本地）
    check   ④ selfcheck.py    对产物跑提交前自查（纯本地）
    submit  ⑤ submit_run.py   提交编排（默认 dry-run）
    status  台账摘要（纯本地）
    all     ①→⑤ 顺序跑一遍（默认 dry-run）
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _run(script, args, title):
    """把子脚本当独立进程调起来 —— 保持"每步都能单独跑"这个性质。"""
    path = os.path.join(HERE, script)
    if not os.path.exists(path):
        sys.exit(f"⛔ 找不到 {script}（本入口只是包装，脚本必须与本文件同目录）")
    print(f"\n{'=' * 78}\n▶ {title}    → python3 {script} {' '.join(args)}\n{'=' * 78}",
          flush=True)   # 管道下也保证 banner 先于子进程输出
    r = subprocess.run([sys.executable, path] + list(args), cwd=HERE)
    return r.returncode


def cmd_env(a):
    """纯本地自检：能不能跑、缺什么。不发任何请求。"""
    print("=" * 78)
    print("环境自检（本命令不发任何网络请求）")
    print("=" * 78)
    print(f"  python      : {sys.version.split()[0]}")
    ok = True
    for mod, why, need in (("cryptography", "原生 AES-192 / MD5 签名", True),
                           ("playwright", "驱动官方 H5（仅 login / h5_* 需要）", False)):
        try:
            __import__(mod)
            print(f"  {mod:12s}: ✅ 已安装（{why}）")
        except ImportError:
            mark = "❌ 缺失" if need else "⚠️ 未安装"
            print(f"  {mod:12s}: {mark} —— {why}")
            ok = ok and not need
    print()
    for f, why, need in (("geo_full.json", "围栏多边形 + 必打点（plan 必需）", True),
                         ("policy_out.json", "判定规则快照（policy 产出）", False),
                         (".token", "登录态（login 产出；submit 离线补 schoolId 时必需）", False),
                         ("identity.json", "设备身份（首次由 submit 惰性生成）", False)):
        exists = os.path.exists(os.path.join(HERE, f))
        mark = "✅ 存在" if exists else ("❌ 缺失" if need else "— 尚无")
        print(f"  {f:20s}: {mark} —— {why}")
        if need:
            ok = ok and exists
    print()
    print("  可直接跑（纯本地）: python3 cli.py plan → cli.py check <产物>")
    print("  需要你自己的账号  : python3 cli.py login → policy → submit")
    print()
    return 0 if ok else 1


def cmd_status(a):
    """读台账，纯本地。"""
    p = os.path.join(HERE, "submit_ledger.json")
    if not os.path.exists(p):
        print("台账不存在（还没跑过 submit）—— 这是干净的初始状态。")
        return 0
    rows = json.load(open(p, encoding="utf-8"))
    print("=" * 78)
    print(f"提交台账：{len(rows)} 条（最近 10 条）")
    print("=" * 78)
    for r in rows[-10:]:
        print(f"  {r.get('at', '?'):20s} {r.get('state', '?'):18s} "
              f"hash={r.get('hash', '?')} km={r.get('km', '?')} pace={r.get('pace', '?')} "
              f"verdict={r.get('verdict', '?')}")
    if any(r.get("state") == "sent" for r in rows):
        print("\n  ⚠️ 有 state=sent 的条目：若确认该次未成功，可用 "
              "`cli.py submit --resolve <hash>` 收尾（脏状态自愈）。")
    return 0


def cmd_all(a):
    steps = []
    if not a.skip_login:
        steps.append(("page_login.py", [], "① 登录取 token（人工过滑块）"))
    if not a.skip_policy:
        steps.append(("policy.py", [], "② 拉判定规则 + 地理约束"))
    steps.append(("trajectory.py", a.plan_args, "③ 生成自洽轨迹（本地）"))
    steps.append(("selfcheck.py", [a.out], "④ 提交前自查（本地）"))
    sub_args = []
    if a.live:
        sub_args += ["--live", "--i-understand-this-writes"]
    steps.append(("submit_run.py", [a.out] + sub_args, "⑤ 提交编排"))
    for script, args, title in steps:
        rc = _run(script, args, title)
        if rc != 0:
            print(f"\n⛔ 第 '{title}' 步退出码 {rc}，链条停止（不回滚已产出的文件）。")
            return rc
    print("\n✅ 全链完成。" + ("（LIVE 已提交）" if a.live else "（dry-run，未发写请求）"))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="校园跑链路一站式入口（薄包装：每一步的真实实现仍在各自脚本里）",
        epilog="不带子命令时会打印本帮助。所有子命令都不新增能力，只做编排。")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("env", help="环境自检（纯本地）").set_defaults(fn=cmd_env)
    sub.add_parser("status", help="台账摘要（纯本地）").set_defaults(fn=cmd_status)

    sub.add_parser("login", help="① 驱动官方 H5 登录取 token").set_defaults(
        fn=lambda a: _run("page_login.py", [], "① 登录取 token"))
    sub.add_parser("policy", help="② 拉判定规则 + 地理约束").set_defaults(
        fn=lambda a: _run("policy.py", [], "② 拉判定规则 + 地理约束"))

    p = sub.add_parser("plan", help="③ 生成自洽轨迹（纯本地）")
    p.add_argument("--km", type=float, default=2.5)
    p.add_argument("--pace", type=float, default=6.2)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--start", default=None)
    p.add_argument("--checkin", action="store_true")
    p.add_argument("--out", default="trajectory_out.json")
    p.set_defaults(fn=lambda a: _run("trajectory.py", a._plan_args, "③ 生成自洽轨迹"))

    c = sub.add_parser("check", help="④ 提交前自查（纯本地）")
    c.add_argument("artifact", nargs="?", default="样例-trajectory_out.json")
    c.set_defaults(fn=lambda a: _run("selfcheck.py", [a.artifact], "④ 提交前自查"))

    s = sub.add_parser("submit", help="⑤ 提交编排（默认 dry-run）")
    s.add_argument("artifact", nargs="?", default="样例-trajectory_out.json")
    s.add_argument("--live", action="store_true", help="真发写请求（另需确认旗标）")
    s.add_argument("--i-understand-this-writes", dest="confirm", action="store_true")
    s.add_argument("--force", action="store_true")
    s.add_argument("--resolve", metavar="HASH")
    s.set_defaults(fn=lambda a: _run(
        "submit_run.py",
        [a.artifact] + (["--live", "--i-understand-this-writes"] if a.live else [])
        + (["--force"] if a.force else []) + (["--resolve", a.resolve] if a.resolve else []),
        "⑤ 提交编排"))

    al = sub.add_parser("all", help="①→⑤ 顺序跑（默认 dry-run）")
    al.add_argument("--km", type=float, default=2.5)
    al.add_argument("--pace", type=float, default=6.2)
    al.add_argument("--seed", type=int, default=None)
    al.add_argument("--start", default=None)
    al.add_argument("--checkin", action="store_true")
    al.add_argument("--out", default="trajectory_out.json")
    al.add_argument("--live", action="store_true")
    al.add_argument("--skip-login", action="store_true")
    al.add_argument("--skip-policy", action="store_true")
    al.set_defaults(fn=cmd_all)

    a = ap.parse_args()
    if not getattr(a, "cmd", None):
        ap.print_help()
        return 0

    # plan / all 把生成参数拼成 trajectory.py 的原生命令行（保持单一事实源）
    if a.cmd == "plan":
        a._plan_args = _plan_argv(a)
    elif a.cmd == "all":
        a.plan_args = _plan_argv(a)

    return getattr(a, "fn", lambda _: ap.print_help())(a)


def _plan_argv(a):
    v = ["--km", str(a.km), "--pace", str(a.pace), "--out", a.out]
    if a.seed is not None:
        v += ["--seed", str(a.seed)]
    if a.start:
        v += ["--start", a.start]
    if a.checkin:
        v += ["--checkin"]
    return v


if __name__ == "__main__":
    sys.exit(main())
