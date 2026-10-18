#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
submit_run.py —— 提交编排（dry-run 优先 · 台账幂等 · 提交后回读）

范式全部移植自参考项目，逐条标注来源：

| 做法 | 来源 | 本文件实现 |
|---|---|---|
| **全写路径 dry-run 走同一构包代码** | Inverx A40 `run_cli.py --dry-run` | 默认就是 dry-run；`--live` 才发请求 |
| **构包后、发网络前的本地一致性断言** | Inverx A40 `validate_outdoor_record_consistency`；Neko `validate_consistency` / `validate_five_point_wrapper` | 调 `selfcheck.py` 的规则引擎，**硬否决不过就不发** |
| **6 步时序编排为单一有状态函数** | Inverx A92 `execute_full_run` | `run()` 单入口，返回 dataclass 式 dict |
| **提交后回读判定达标**（提交成功 ≠ 业务成功） | Inverx A92：`sleep(0.5)` → `get_one_record(rrid)` → `status==1` | `--live` 成功后回读 `runRecord/detail` / `getList` |
| **提交台账 + 幂等** | ★两边都**没有**（Neko 每单新 UUID，Inverx 无台账）—— 我们的机会点 | `submit_ledger.json`，同 payload 哈希默认拒绝重发 |
| **设备身份持久化** | Neko `identity.json` | `identity.py` |
| 脏状态自愈 | Inverx B72 `clean_unfinished_run` | 台账里标 `abandoned`，可 `--resolve` 收尾 |

⚠️ 授权与安全边界
  * 本脚本**默认不发任何写请求**。`--live` 必须同时给 `--i-understand-this-writes`，
    这是刻意设置的二道闸。
  * 只提交**一次**。台账保证同一 payload 不会重复提交（除非 `--force`）。
  * 不发批量、不循环、不并发。
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from login import UA, WEB_ID, WEB_SECRET, TOKEN_FILE      # noqa: E402
import identity as ident                                   # noqa: E402
import selfcheck                                           # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = "school-api.dingxinwen.cn"
LEDGER = os.path.join(HERE, "submit_ledger.json")

# 13 字段（来自 SportApiService @FormUrlEncoded 注解，顺序即注解顺序）
FORM_FIELDS = ["schoolId", "fenceId", "start", "end", "distance", "duration",
               "pace", "step", "innerNum", "outerNum", "checkInNum",
               "trajectoryUrl", "checkInJson"]


# ---------------- 传输 ----------------
def _sign_query(form):
    """H5 形状签名：md5(排序 "k=v" 直接拼接 + web_secret)，空值也计入。"""
    p = {"timestamp": str(int(time.time())), "app_id": WEB_ID}
    p.update(form)
    plain = "".join(f"{k}={p[k]}" for k in sorted(p))
    p["sign"] = hashlib.md5((plain + WEB_SECRET).encode()).hexdigest()
    return p


def http_post_json(path, body, token, retries=2, timeout=30):
    """POST：认证参数在 query，业务体为 JSON（H5 实测形状）。"""
    q = _sign_query({"token": token})
    url = f"https://{HOST}{path}?{urllib.parse.urlencode(q)}"
    data = json.dumps(body).encode()
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"User-Agent": UA, "Content-Type": "application/json; charset=utf-8",
                     "Accept": "application/json, text/plain, */*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode())
            except Exception:
                return {"code": e.code, "msg": "HTTPError"}
        except Exception as e:                       # 瞬时抖动（本项目实测过一次超时）
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    return {"code": -1, "msg": f"{type(last).__name__}: {last}"}


def http_post_form(path, form, token, retries=2, timeout=30):
    """
    POST 表单（原生 `/api/runRecord/save` 的 `@FormUrlEncoded` 形状）。
    认证参数同样在 query。
    """
    q = _sign_query({"token": token})
    url = f"https://{HOST}{path}?{urllib.parse.urlencode(q)}"
    data = urllib.parse.urlencode({k: form[k] for k in FORM_FIELDS if k in form}).encode()
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"User-Agent": UA,
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode()), data.decode()
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode()), data.decode()
            except Exception:
                return {"code": e.code, "msg": "HTTPError"}, data.decode()
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    return {"code": -1, "msg": f"{type(last).__name__}: {last}"}, data.decode()


def http_get(path, extra, token, timeout=30):
    q = _sign_query({**extra, "token": token})
    url = f"https://{HOST}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"code": e.code, "msg": "HTTPError"}
    except Exception as e:
        return {"code": -1, "msg": f"{type(e).__name__}: {e}"}


# ---------------- 台账 ----------------
def ledger_load():
    if os.path.exists(LEDGER):
        try:
            return json.load(open(LEDGER))
        except Exception:
            pass
    return []


def ledger_save(rows):
    json.dump(rows[-200:], open(LEDGER, "w"), ensure_ascii=False, indent=1)
    try:
        os.chmod(LEDGER, 0o600)
    except Exception:
        pass


def payload_hash(payload):
    """只哈希业务字段（不含 token/时间戳），用于幂等判定。"""
    core = {k: payload.get(k) for k in FORM_FIELDS}
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


# ---------------- 编排 ----------------
def run(artifact_path, token, sex="male", live=False, force=False, resolve=None):
    art = json.load(open(artifact_path))
    payload = dict(art["payload"])
    traj = art["trajectory"]
    st = art.get("report", {})

    idn = ident.load()
    if payload.get("schoolId") is None:
        if not token:
            raise SystemExit("payload 缺 schoolId 且无 token，无法离线补全（先跑 page_login.py）")
        me = http_get("/api/login/personalInfo", {}, token)
        payload["schoolId"] = (me.get("data") or {}).get("schoolId")
    if payload.get("fenceId") is None:
        raise SystemExit("payload 缺 fenceId")

    ph = payload_hash(payload)
    rows = ledger_load()

    if resolve:
        for r in rows:
            if r.get("hash") == resolve:
                r["state"] = "abandoned"
                r["resolvedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        ledger_save(rows)
        print(f"台账：{resolve} 已标 abandoned（脏状态自愈，移植自 Inverx B72）")
        return {"state": "resolved"}

    dup = [r for r in rows if r.get("hash") == ph and r.get("state") in ("sent", "verified")]
    if dup and not force:
        print(f"⛔ 幂等拦截：同一 payload（hash={ph}）已在 "
              f"{dup[-1].get('sentAt')} 提交过，状态 {dup[-1].get('state')}。")
        print("   （★ 参考项目两边都没有台账，是我们加的。要强行重发加 --force）")
        return {"state": "duplicate-blocked", "hash": ph}

    print("=" * 78)
    print("提交编排 · dry-run 优先（Inverx A92 `execute_full_run` 的 6 步对应如下）")
    print("=" * 78)
    print(f"  [1] 身份         : {ident.summary(idn)}")
    print(f"  [2] 台账         : {len(rows)} 条，本次 payload hash={ph}")
    print(f"  [3] 预检(硬否决)  : 见下方自查")
    print(f"  [4] 构包         : 13 字段表单 / trajectory {sum(len(s) for s in traj)} 点")
    print(f"  [5] 提交         : POST /api/runRecord/save  （{'LIVE' if live else 'dry-run 不发'}）")
    print(f"  [6] 回读判定      : {'提交后 runRecord/detail 回读' if live else 'dry-run 跳过'}")
    print()

    # --- 第 3 步：本地一致性断言（失败即不发；规则集 owner 在 selfcheck.run_all）---
    pts = selfcheck.pts_of(traj)
    dt = st.get("sampleIntervalSec", 5.0) or 5.0
    fence, ftype = selfcheck.load_fence_for(payload["fenceId"])
    cfg = {"sex": sex, "fence": fence, "sample_interval": dt, "fence_type": ftype,
           "hash": ph}
    selfcheck.R.clear()
    risk, verdict, vetoes = selfcheck.run_all(payload, pts, cfg)
    selfcheck.print_results(selfcheck.R)
    print(f"      风险分 {risk:.3f} · 判定 {verdict}")
    if vetoes:
        print(f"\n⛔ 硬否决 {vetoes} ⇒ 拒绝发送（这是 dry-run 的价值：构包后发网络前拦下）")
        return {"state": "rejected-by-selfcheck", "vetoes": vetoes}

    # --- 第 4 步：构包（dry-run 与 live 走**同一份**代码） ---
    form = {k: payload.get(k, "") for k in FORM_FIELDS}
    for k in ("schoolId", "fenceId", "step", "innerNum", "outerNum", "checkInNum"):
        form[k] = int(form[k]) if form[k] not in ("", None) else ""
    form["distance"] = float(form["distance"])
    form["duration"] = int(form["duration"])
    print("\n  ── 待发表单（原生 @FormUrlEncoded 13 字段）──")
    for k in FORM_FIELDS:
        print(f"     {k:<16} = {form[k]!r}")
    print(f"     trajectory        = <{sum(len(s) for s in traj)} 点，"
          f"外层 {len(traj)} 段，不入表单，走 trajectoryUrl/上传通道>")

    if not live:
        print("\n★ dry-run 结束：未发送任何请求。")
        print("  live 提交需要：--live --i-understand-this-writes（写真实跑步记录）")
        rows.append({"hash": ph, "state": "dry-run", "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "fenceId": payload["fenceId"], "km": payload["distance"],
                     "pace": payload["pace"], "risk": risk, "verdict": verdict})
        ledger_save(rows)
        return {"state": "dry-run", "hash": ph, "risk": risk, "verdict": verdict}

    # --- 第 5 步：提交 ---
    # ★ 写接口**不重试**（retries=0）：网络层自动重试可能在"服务端已落库但响应超时"时造成重复写入。
    #   幂等由台账负责，不交给传输层。
    resp, sent_body = http_post_form("/api/runRecord/save", form, token, retries=0)
    print(f"\n  ← 提交响应：{json.dumps(resp, ensure_ascii=False)[:300]}")
    row = {"hash": ph, "state": "sent", "sentAt": time.strftime("%Y-%m-%d %H:%M:%S"),
           "fenceId": payload["fenceId"], "km": payload["distance"], "pace": payload["pace"],
           "respCode": resp.get("code"), "respMsg": resp.get("msg"),
           "bodyBytes": len(sent_body), "risk": risk, "verdict": verdict}
    rows.append(row); ledger_save(rows)
    if resp.get("code") != 0:
        print("  ⛔ 提交未成功（code != 0），不做回读")
        return {"state": "send-failed", "resp": resp}

    # --- 第 6 步：回读判定达标（移植自 Inverx A92 `get_one_record`） ---
    time.sleep(0.5)
    gid = resp.get("data") if isinstance(resp.get("data"), int) else None
    back = http_get("/api/runRecord/detail", {"id": gid}, token) if gid else None
    print(f"  ← 回读 detail(id={gid})：{json.dumps(back, ensure_ascii=False)[:300] if back else '无 id，跳过'}")
    lst = http_post_json("/api/runRecord/getList",
                         {"page": 1, "size": 5,
                          "startDate": payload["start"][:10] + " 00:00:00",
                          "endDate": payload["start"][:10] + " 23:59:59"}, token)
    total = ((lst.get("data") or {}).get("total"))
    print(f"  ← 回读当日记录数 total={total}")
    verified = bool(back and (back.get("data") or {}).get("id") == gid)
    row["state"] = "verified" if verified else "sent-unverified"
    row["readBackTotal"] = total
    ledger_save(rows)
    # 提交成功后写入路线历史（供后续抗"路线复用"比对；只在此处写，避免自比）
    if verified:
        try:
            hist = json.load(open(selfcheck.HISTORY)) if os.path.exists(selfcheck.HISTORY) else []
            hist = [h for h in hist if time.time() - h.get("ts", 0) <= selfcheck.TH["state_ttl_sec"]]
            hist.append({"ts": time.time(),
                         "grid": [list(g) for g in selfcheck._grid(pts)],
                         "fenceId": payload["fenceId"], "km": payload["distance"],
                         "hash": ph})
            json.dump(hist[-50:], open(selfcheck.HISTORY, "w"))
            print(f"  → 路线已写入历史（{len(hist)} 条），后续提交将与它比对相似度")
        except Exception as e:
            print(f"  （路线历史写入失败：{e}）")

    # 提交成功后锚点漂移（Neko flow.rs 同款）
    if back and (back.get("data") or {}).get("id"):
        d = back["data"]
        ident.drift_anchor(idn)
        print(f"  → 身份锚点已漂移并落盘（抗路线复用；Neko 同款做法）")
        print(f"  → 服务端判定：status={d.get('status')} reason={d.get('reason')!r} "
              f"ddPoints={d.get('ddPoints')} receive={d.get('receive')}")
    return {"state": row["state"], "resp": resp, "readBack": back}


def main():
    ap = argparse.ArgumentParser(description="提交编排（默认 dry-run，不发请求）")
    ap.add_argument("artifact", nargs="?", default="trajectory_out.json")
    ap.add_argument("--sex", choices=["male", "female"], default="male")
    ap.add_argument("--live", action="store_true", help="真发写请求（另需 --i-understand...）")
    ap.add_argument("--i-understand-this-writes", dest="confirm", action="store_true",
                    help="二道闸：确认这将写入一条真实跑步记录")
    ap.add_argument("--force", action="store_true", help="绕过台账幂等拦截")
    ap.add_argument("--resolve", metavar="HASH", help="把某条台账标为 abandoned（脏状态自愈）")
    ap.add_argument("--no-selfcheck", action="store_true", help="跳过自查（不建议）")
    a = ap.parse_args()

    if a.live and not a.confirm:
        print("⛔ 拒绝：--live 需要同时给 --i-understand-this-writes")
        return 2
    # ★ dry-run 不强制要 token（离线也能演示构包与自查）；live 才必须。
    if os.path.exists(TOKEN_FILE):
        token = open(TOKEN_FILE).read().strip()
    elif a.live:
        print("⛔ live 提交需要 .token，请先跑 page_login.py")
        return 2
    else:
        token = ""
        print("ℹ️ 未发现 .token，以离线模式做 dry-run（不联网）")
    out = run(a.artifact, token, a.sex, a.live, a.force, a.resolve)
    print(f"\n最终状态：{out.get('state')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
