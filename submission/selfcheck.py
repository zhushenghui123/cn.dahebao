#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selfcheck.py —— 校园跑「提交前自查」规则引擎（我们缺的那一环）

范式来源（移植自参考项目，非自创）：
  * **budaolepao/detection/**（含 18 个规则模块 + 风险打分器）——
    架构照搬：`BaseRule.evaluate(trace, config) -> RuleResult`、`RiskScorer` 加权汇总、
    阈值分档 low/medium/high。规则阈值全部取自其 `rules/config.py`。
  * **lepao3** —— 噪声自相关、速度非周期性、每圈随机偏移等自然性指标。

与参考项目的关键差异（必须调整，否则等于照抄错的）：
  参考项目校验的是**含逐点时间戳/精度/传感器**的完整 trace；
  我们的目标 `POST /api/runRecord/save` 的 `trajectory` **只有经纬度**，没有每点时间戳、
  没有精度、没有传感器字段。所以：
    - `accuracy_stability` / `sensor_consistency` / 逐点 `speed_physiological`
      **在本目标上不可用**（服务端拿不到那些输入），我们改为**聚合量一致性**校验；
    - 新增 `geometry_selfconsistent` / `no_teleport` / `pace_formula` ——
      这些才是本目标服务端**真能算**的东西（且我们已从真实记录反推出其公式）。

用法：
    python3 selfcheck.py trajectory_out.json                 # 查一份
    python3 selfcheck.py trajectory_out.json --commit-history # 通过并把网格存入历史
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(HERE, "selfcheck_history.json")

# ---------------- 阈值（对齐参考项目 budaolepao/detection/rules/config.py） ----------------
TH = {
    "max_instant_speed": 12.0,          # SpeedPhysiologicalConfig.max_instant_speed
    "max_acceleration": 10.0,           # max_acceleration
    "max_speed_variance_floor": 0.1,    # max_speed_variance：低于它 = 疑匀速注入
    "autocorr_threshold": 0.3,          # NoiseSpectrumConfig.autocorr_threshold
    "stride_length_m": 0.7,             # StepDistanceConfig.stride_length_m
    "step_ratio_range": (0.4, 2.5),     # min_ratio / max_ratio
    "min_gps_m": 500.0, "min_steps": 100,
    "jaccard_threshold": 0.9,           # TrajectorySimilarityConfig
    "grid_size": 100,
    "state_ttl_sec": 21600.0,           # 6h
    "max_outside_ratio": 0.1,           # ZoneEnforcementConfig
    "risk_thresholds": {"low": 0.3, "medium": 0.6, "high": 0.8},   # DetectionConfig
    # 本目标专属（从真实记录反推，见 docs/08-接口契约与数据模型.md 三·补 C）
    "pace_formula_tol_sec": 3.0,        # pace == round(duration/distance) 的容差
    "geom_len_tol_pct": 0.25,           # 坐标串算出的路径长 vs 上报里程 的相对容差
    "static_eps_m": 1.0,
    "valid_window": ("06:00", "22:30"),  # /api/school/config 实测
    "rule_pace_min_per_km": (3.0, 10.0),
    "rule_km": {"male": 2.5, "female": 2.0},
}

R = []          # RuleResult 收集


HARD = set()          # 硬否决规则名（失败即拒绝，不参与平均稀释）


def result(name, passed, score, detail, applicable=True, weight=1.0, hard=True):
    """
    ★ 与参考项目的**有意偏离**：budaolepao 的 RiskScorer 是简单加权平均，
    单条规则失败（如 route_novelty 打 0.8）会被十几条规则平均成 0.06，判不到 medium。
    作为"提交前闸门"这不可用。所以这里保留加权平均（作为辅助风险分），
    另外引入**硬否决**：标记 hard 的规则一旦失败，直接 reject，不参与稀释。
    """
    if hard:
        HARD.add(name)
    R.append({"rule": name, "passed": passed, "score": round(score, 4),
              "applicable": applicable, "weight": weight, "hard": hard,
              "detail": detail})


# ---------------- 几何工具 ----------------
def hav(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def pts_of(traj):
    """trajectory 是 [[{longitude,latitude},…],…]，拍平。"""
    out = []
    for seg in traj:
        for p in seg:
            out.append((float(p["longitude"]), float(p["latitude"])))
    return out


def point_in_polygon(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-18) + x1):
            inside = not inside
    return inside


# ---------------- 规则集 ----------------
def _parse_pace(v):
    """解析 "{分}′{秒:02d}″" → 秒/km。"""
    try:
        m, rest = str(v).split("\u2032", 1)
        s = rest.rstrip("\u2033")
        return int(m) * 60 + int(s)
    except Exception:
        return None


def r_pace_formula(payload, pts, cfg):
    """
    pace 必须与 duration/distance 自洽。

    ★ 必须有距离相关的容差，不能用等号：
      真实记录 <他人记录> 上报 `12′07″`，但用**下发的** distance=0.35 反推只有 12′00″。
      原因：服务端用**未四舍五入**的距离算配速，却把距离四舍五入到 2 位下发。
      ⇒ 容差 ≈ sec_per_km × (0.005 / distance)，即距离越小容差越大。
    """
    d, t = float(payload["distance"]), float(payload["duration"])
    if d <= 0 or t <= 0:
        return result("pace_formula", False, 1.0, "distance/duration 非正")
    sec = t / d                                        # 用下发（已舍入）距离反推
    got_sec = _parse_pace(payload.get("pace"))
    if got_sec is None:
        return result("pace_formula", False, 0.6, f"pace 无法解析：{payload.get('pace')!r}")
    tol = max(TH["pace_formula_tol_sec"], sec * (0.005 / d) * 1.5)
    diff = abs(got_sec - sec)
    ok = diff <= tol
    m, ss = divmod(int(round(sec)), 60)
    return result("pace_formula", ok, 0.0 if ok else 0.6,
                  f"上报 {payload.get('pace')!r}（{got_sec}s/km）vs 由 duration/distance 推算 "
                  f"{m}\u2032{ss:02d}\u2033（{sec:.1f}s/km），差 {diff:.1f}s ≤ 容差 {tol:.1f}s")


def r_mileage(payload, pts, cfg):
    need = TH["rule_km"][cfg["sex"]]
    got = float(payload["distance"])
    return result("mileage_target", got >= need, 0.0 if got >= need else 0.8,
                  f"上报 {got} km vs 达标线 {need} km（{cfg['sex']}）")


def r_pace_range(payload, pts, cfg):
    d, t = float(payload["distance"]), float(payload["duration"])
    pace = (t / 60.0) / d
    lo, hi = TH["rule_pace_min_per_km"]
    ok = lo <= pace <= hi
    return result("pace_range", ok, 0.0 if ok else 0.5,
                  f"配速 {pace:.2f} min/km vs 规则区间 [{lo}, {hi}]")


def r_time_window(payload, pts, cfg):
    t = payload["start"][11:16]
    lo, hi = TH["valid_window"]
    ok = lo <= t <= hi
    return result("time_window", ok, 0.0 if ok else 0.7,
                  f"起始 {t} vs 有效时段 {lo}~{hi}")


def r_zone(payload, pts, cfg):
    poly = cfg["fence"]
    if not poly:
        return result("zone_enforcement", True, 0.0, "未提供围栏，跳过", applicable=False)
    outside = sum(1 for p in pts if not point_in_polygon(p, poly))
    ratio = outside / len(pts) if pts else 1.0
    rep_in, rep_out = int(payload["innerNum"]), int(payload["outerNum"])
    ok = ratio <= TH["max_outside_ratio"] and rep_out == 0
    detail = (f"实测围栏外 {outside}/{len(pts)} ({ratio:.1%})，上报 inner/outer={rep_in}/{rep_out}")
    if (len(pts) - outside) != rep_in:
        return result("zone_enforcement", False, 0.9,
                      detail + " ⇒ 上报 innerNum 与实际不一致")
    return result("zone_enforcement", ok, 0.0 if ok else 0.9, detail)


def r_geometry(payload, pts, cfg):
    """坐标串累计长度 vs 上报里程（服务端能算，我们也必须自洽）。"""
    if len(pts) < 2:
        return result("geometry_selfconsistent", False, 1.0, "点数不足")
    L = sum(hav(lat1, lon1, lat2, lon2)
            for (lon1, lat1), (lon2, lat2) in zip(pts, pts[1:])) / 1000.0
    rep = float(payload["distance"])
    rel = abs(L - rep) / rep if rep else 1.0
    # 上报里程应≤坐标串长度（噪声虚增），且差距不应过大
    ok = rel <= TH["geom_len_tol_pct"] and L >= rep * 0.9
    detail = f"坐标串 {L:.3f} km vs 上报 {rep} km（偏差 {rel:.1%}）"
    return result("geometry_selfconsistent", ok, 0.0 if ok else 0.6, detail)


def r_no_teleport(payload, pts, cfg):
    dt = cfg["sample_interval"]
    worst, at = 0.0, -1
    for i, ((lon1, lat1), (lon2, lat2)) in enumerate(zip(pts, pts[1:])):
        v = hav(lat1, lon1, lat2, lon2) / dt if dt else 0
        if v > worst:
            worst, at = v, i
    ok = worst <= TH["max_instant_speed"]
    return result("no_teleport", ok, 0.0 if ok else 0.6,
                  f"最大相邻点速度 {worst:.2f} m/s（点 {at}）vs 上限 {TH['max_instant_speed']}")


def r_not_static(payload, pts, cfg):
    uniq = len({(round(p[0], 6), round(p[1], 6)) for p in pts})
    ok = uniq > max(3, len(pts) * 0.3)
    return result("not_static", ok, 0.0 if ok else 1.0,
                  f"去重后 {uniq}/{len(pts)} 个不同坐标"
                  f"{'（近乎原地，实测 <他人记录> 这种被判 status=2）' if not ok else ''}")


def r_step_consistency(payload, pts, cfg):
    d_m = float(payload["distance"]) * 1000.0
    steps = int(payload["step"])
    if steps <= 0:
        return result("step_distance", False, 1.0, "step 非正")
    implied = steps * TH["stride_length_m"]
    ratio = d_m / implied if implied else float("inf")
    stride_actual = d_m / steps
    lo, hi = TH["step_ratio_range"]
    ok = lo <= ratio <= hi
    detail = (f"{steps} 步 / {d_m:.0f} m ⇒ 步幅 {stride_actual:.2f} m，"
              f"与参考步幅 {TH['stride_length_m']} m 之比 {ratio:.2f}（允许 {lo}~{hi}）")
    return result("step_distance", ok, 0.0 if ok else 0.6, detail)


def r_noise_autocorr(payload, pts, cfg):
    """★ 白噪声会被判伪造（参考项目 noise_spectrum：lag-1 自相关低于 0.3 → 打 0.5）。"""
    if len(pts) < 52:
        return result("noise_autocorr", True, 0.0, "点数不足，跳过", applicable=False)
    disp = [hav(lat1, lon1, lat2, lon2)
            for (lon1, lat1), (lon2, lat2) in zip(pts, pts[1:])]
    mean = sum(disp) / len(disp)
    arr = [x - mean for x in disp]
    denom = sum(x * x for x in arr)
    if denom < 1e-12:
        return result("noise_autocorr", False, 1.0, "位移零方差 —— GPS 注入痕迹")
    lag1 = sum(arr[i] * arr[i + 1] for i in range(len(arr) - 1)) / denom
    ok = abs(lag1) >= TH["autocorr_threshold"]
    return result("noise_autocorr", ok, 0.0 if ok else 0.5,
                  f"位移 lag-1 自相关 {lag1:.3f}（阈值 ≥{TH['autocorr_threshold']}）"
                  f"{'：白噪声形态' if not ok else ''}")


def r_speed_variance(payload, pts, cfg):
    dt = cfg["sample_interval"]
    vs = [hav(lat1, lon1, lat2, lon2) / dt
          for (lon1, lat1), (lon2, lat2) in zip(pts, pts[1:])]
    if len(vs) < 10:
        return result("speed_variance", True, 0.0, "点数不足，跳过", applicable=False)
    m = sum(vs) / len(vs)
    var = sum((v - m) ** 2 for v in vs) / len(vs)
    ok = var >= TH["max_speed_variance_floor"]
    return result("speed_variance", ok, 0.0 if ok else 0.4,
                  f"相邻点速度方差 {var:.3f}（下限 {TH['max_speed_variance_floor']}）"
                  f"{'：疑匀速注入' if not ok else ''}")


def _grid(pts):
    gs = TH["grid_size"]
    return {(int(lon * gs * math.cos(math.radians(lat))), int(lat * gs))
            for lon, lat in pts}


def r_route_novelty(payload, pts, cfg, history):
    """★ cfg 里可带 hash：同 hash 的历史条目跳过比对（那是"同一次提交"不是"路线复用"）。"""
    """★ 路线复用检测（参考项目 trajectory_similarity，网格 Jaccard，阈值 0.9）。"""
    cur = _grid(pts)
    now = time.time()
    hist = [h for h in history if now - h.get("ts", 0) <= TH["state_ttl_sec"]]
    best, best_ts = 0.0, None
    for h in hist:
        if h.get("hash") and h.get("hash") == cfg.get("hash"):
            continue                      # ★ 同一条 payload 不与"自己"比（避免自比误报）
        prev = {tuple(g) for g in h.get("grid", [])}
        if not prev:
            continue
        inter = cur & prev
        union = cur | prev
        sim = len(inter) / len(union) if union else 0.0
        if sim > best:
            best, best_ts = sim, h.get("ts")
    if not hist:
        return result("route_novelty", True, 0.0, "无历史路线可比（首次）", applicable=False)
    ok = best <= TH["jaccard_threshold"]
    return result("route_novelty", ok, 0.0 if ok else 0.8,
                  f"与最近 {len(hist)} 条路线的最大网格 Jaccard {best:.3f}"
                  f"（阈值 {TH['jaccard_threshold']}）{'：路线复用' if not ok else ''}")


def r_checkin(payload, pts, cfg):
    n, js = int(payload["checkInNum"]), payload.get("checkInJson", "")
    if js in ("", None):                                # 实测真实记录就是空串
        return result("checkin_json", True, 0.0,
                      f"checkInNum={n}，checkInJson 为空串（与真实记录一致）")
    try:
        parsed = json.loads(js) if isinstance(js, str) else js
    except Exception:
        return result("checkin_json", False, 0.5, "checkInJson 不是合法 JSON")
    ok = isinstance(parsed, list) and len(parsed) == n
    ftype = cfg.get("fence_type")
    note = f"围栏 type={ftype}"
    if ftype == 1 and n == 0:
        note += "（全区域打卡点，无需点位打卡 ⇒ 0 合理）"
    return result("checkin_json", ok, 0.0 if ok else 0.5,
                  f"checkInNum={n}，checkInJson 条目 {len(parsed) if isinstance(parsed, list) else '?'}；{note}")


def score_and_verdict(results):
    total = wsum = 0.0
    for r in results:
        if not r["applicable"]:
            continue
        total += r["score"] * r["weight"]
        wsum += r["weight"]
    risk = total / wsum if wsum else 0.0
    t = TH["risk_thresholds"]
    verdict = ("high_risk" if risk >= t["high"] else "medium_risk" if risk >= t["medium"]
               else "low_risk" if risk >= t["low"] else "normal")
    vetoes = [r["rule"] for r in results
              if r["applicable"] and r["hard"] and not r["passed"]]
    if vetoes:
        return risk, "reject", vetoes          # 硬否决压过平均分
    return risk, verdict, []


def load_fence_for(fence_id, from_file="geo_full.json"):
    """按 fenceId 从 geo_full.json 取 (围栏多边形, type)。取不到返回 (None, None)。"""
    try:
        src = json.load(open(os.path.join(HERE, from_file)))
    except Exception:
        return None, None

    def walk(o):
        """
        在 geo_full.json（顶层是 fence_7/fence_8… 这类键）与 policy_out.json（{"raw": …}）两种
        形态里都能找到围栏。
        ★ 必须遍历**所有** dict/list 值：之前只递归 "raw" 子键，导致顶层 fence_* 永远进不去，
          围栏取不到、zone_enforcement 被静默跳过（实测踩到过）。
        """
        if isinstance(o, dict):
            d = o.get("data")
            if isinstance(d, list):
                for x in d:
                    if isinstance(x, dict) and "fence" in x:
                        yield x
            for v in o.values():
                if isinstance(v, (dict, list)):
                    yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                if isinstance(v, dict):
                    if "fence" in v and "id" in v:
                        yield v
                    else:
                        yield from walk(v)

    for fo in walk(src):
        if fo.get("id") == fence_id:
            raw = fo["fence"]
            return (raw if isinstance(raw[0][0], (int, float)) else raw[0]), fo.get("type")
    return None, None


ALL_RULES = ("r_pace_formula", "r_mileage", "r_pace_range", "r_time_window",
             "r_zone", "r_geometry", "r_no_teleport", "r_not_static",
             "r_step_consistency", "r_noise_autocorr", "r_speed_variance",
             "r_route_novelty", "r_checkin")


def run_all(payload, pts, cfg, history=None):
    """
    ★ 单一入口：跑完 13 条规则并给出 (risk, verdict, vetoes)。
    `submit_run.py` 调用本函数 —— 规则集只有这一份 owner，
    避免"两个脚本各抄一份、规则不同步"（这里之前就抄出过一份死代码 if/else）。
    """
    R.clear()
    if history is None:
        history = []
        if os.path.exists(HISTORY):
            try:
                history = json.load(open(HISTORY))
            except Exception:
                history = []
    for name in ALL_RULES:
        fn = globals()[name]
        if name == "r_route_novelty":
            fn(payload, pts, cfg, history)
        else:
            fn(payload, pts, cfg)
    return score_and_verdict(R)


def print_results(results):
    for r in results:
        mark = "—" if not r["applicable"] else ("✓" if r["passed"] else "✗")
        print(f"  [{mark}] {r['rule']:<24} {r['detail']}")


def main():
    ap = argparse.ArgumentParser(description="提交前自查（规则引擎，只读本地文件）")
    ap.add_argument("artifact", help="trajectory.py 产出的 JSON")
    ap.add_argument("--sex", choices=["male", "female"], default="male")
    ap.add_argument("--commit-history", action="store_true",
                    help="通过后把本次路线网格写入历史（供下次比相似度）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    a = ap.parse_args()

    with open(a.artifact) as f:
        art = json.load(f)
    payload = art["payload"]
    traj = art["trajectory"]
    rep = art.get("report", {})
    pts = pts_of(traj)
    dt = rep.get("sampleIntervalSec", 5.0) or 5.0

    fence, ftype = None, None
    try:                                                 # 从 geo_full.json 取围栏多边形
        gf = json.load(open(os.path.join(HERE, "geo_full.json")))
    except Exception:
        gf = None
    fid = payload.get("fenceId")

    def _fences(src):
        """从 geo_full.json / policy_out.json 两种形态里取围栏列表。"""
        if not isinstance(src, dict):
            return []
        out = []
        for k, v in src.items():
            if not isinstance(v, dict):
                continue
            d = v.get("data")
            if isinstance(d, list):
                out += [x for x in d if isinstance(x, dict) and "fence" in x]
            elif k == "raw":                             # policy_out.json 形态
                out += _fences(d)
        return out

    for fo in (_fences(gf) if gf else []):
        if fo.get("id") == fid:
            raw = fo["fence"]
            fence = raw if isinstance(raw[0][0], (int, float)) else raw[0]
            ftype = fo.get("type")
    history = []
    if os.path.exists(HISTORY):
        try:
            history = json.load(open(HISTORY))
        except Exception:
            history = []

    cfg = {"sex": a.sex, "fence": fence, "sample_interval": dt, "fence_type": ftype,
           "hash": hashlib.sha256(json.dumps(
               {k: payload.get(k) for k in ("schoolId", "fenceId", "start", "end",
                                            "distance", "duration", "pace", "step",
                                            "innerNum", "outerNum", "checkInNum",
                                            "trajectoryUrl", "checkInJson")},
               sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]}

    risk, verdict, vetoes = run_all(payload, pts, cfg, history)
    out = {"risk": round(risk, 4), "verdict": verdict, "vetoes": vetoes,
           "rules": R, "points": len(pts), "fenceId": fid}

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(f"=== 提交前自查（{len(pts)} 点，围栏 {fid}）===")
        for r in R:
            mark = "—" if not r["applicable"] else ("✓" if r["passed"] else "✗")
            print(f"  [{mark}] {r['rule']:<24} score={r['score']:<5} {r['detail']}")
        print(f"\n辅助风险分 {risk:.3f}"
              f"（分档 low≥{TH['risk_thresholds']['low']} "
              f"medium≥{TH['risk_thresholds']['medium']} high≥{TH['risk_thresholds']['high']}）")
        if vetoes:
            print(f"⛔ 硬否决命中 {len(vetoes)} 条：{', '.join(vetoes)} ⇒ **REJECT**")
            print("→ 先修上面 ✗ 的项，别提交")
        else:
            print("✅ 无硬否决 ⇒ **PASS**，结构与自然性自洽，可进入授权提交流程")

    if a.commit_history and verdict in ("normal", "low_risk"):
        history = [h for h in history if time.time() - h.get("ts", 0) <= TH["state_ttl_sec"]]
        history.append({"ts": time.time(), "grid": [list(g) for g in _grid(pts)],
                        "fenceId": fid, "km": payload["distance"],
                        "hash": cfg.get("hash")})
        json.dump(history[-50:], open(HISTORY, "w"))
        print(f"→ 已写入路线历史（{HISTORY}，{len(history)} 条）")
    return 0 if verdict in ("normal", "low_risk") else 1


if __name__ == "__main__":
    sys.exit(main())
