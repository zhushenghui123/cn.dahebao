#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trajectory.py —— 校园跑「自洽轨迹」生成器（对标社区 A 组核心竞争力：数据自然性）

为什么需要它：
    客户端只负责采集与上报，判定权在服务端。所以"模拟"的本质不是绕过客户端，
    而是产出一份**在服务端校验下自洽**的 (轨迹 + 统计量)。

已实测的格式契约（来自真实记录，见 docs/08-接口契约与数据模型.md 三·补 C）：
    distance  单位 km（float，服务端下发时四舍五入 2 位；内部计算须用未舍入值）
    duration  单位 秒（long）
    pace      字符串 "{分}′{秒:02d}″"，Unicode ′(U+2032) ″(U+2033)
    step      步数（int）
    trajectory = [[{"longitude":…,"latitude":…}, …], …]   ← 外层分段，内层点对象
    innerNum / outerNum  围栏内 / 外点数
    checkInNum / checkInJson  打卡点数与其 JSON
    start / end  "yyyy-MM-dd HH:mm:ss"

自然性设计（社区血泪教训的落地）：
    1. 路径限制在**服务端下发的围栏多边形**内（我们自己不拟合，直接用它的）
    2. 速度曲线：起跑加速 → 途中稳态微波动 → 冲刺减速（不是匀速直线，匀速是典型判据）
    3. GPS 噪声：高斯漂移 σ≈4~8m（真实 GPS 一定有抖动）
    4. 步频耦合：步数由"实际位移 ÷ 步幅"推出，步幅由速度与步频反算，
       保证 step 与 distance 互相对得上（社区死穴：纯定位方案步频必错）
    5. 配速落在学校规则区间内（/api/school/config 下发）

⚠️ 合规边界：本脚本只产出**本地数据与自检报告**，不发起任何网络请求。
   "提交"是另一件事，需显式授权（见 submit_run.py）。
"""
import argparse
import json
import math
import os
import random
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))

# --- 常量（社区/实测参照值） ---
EARTH_R = 6371000.0            # 地球半径 m
SAMPLE_INTERVAL = 5            # 采样间隔秒（社区实测约 5s 一点）
GPS_NOISE_M = 6.0              # GPS 高斯噪声 σ（社区参照 8m，取 6m 更保守）
CADENCE_RANGE = (150, 180)     # 真实跑步步频 spm
STRIDE_RANGE = (0.95, 1.45)    # 步幅 m


def lonlat_to_xy(lng, lat, lat0):
    """等距圆柱投影（米）。纬度跨度小，误差可忽略。"""
    return lng * 111320.0 * math.cos(math.radians(lat0)), lat * 111320.0


def xy_to_lonlat(x, y, lat0):
    return x / (111320.0 * math.cos(math.radians(lat0))), y / 111320.0


def haversine(p1, p2):
    """两点 (lng,lat) 距离，米。"""
    lng1, lat1 = p1
    lng2, lat2 = p2
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2)
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def point_in_polygon(pt, poly):
    """射线法。poly = [[lng,lat], …]"""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-18) + x1):
            inside = not inside
    return inside


def polygon_centroid(poly):
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))


def shrink_polygon(poly, margin_m):
    """把多边形朝质心收缩 margin_m 米，得到一个安全的"内部环"。"""
    cx, cy = polygon_centroid(poly)
    lat0 = cy
    out = []
    for lng, lat in poly:
        x, y = lonlat_to_xy(lng, lat, lat0)
        cx_m, cy_m = lonlat_to_xy(cx, cy, lat0)
        dx, dy = x - cx_m, y - cy_m
        d = math.hypot(dx, dy)
        if d < 1e-9:
            out.append([lng, lat])
            continue
        k = max(0.0, (d - margin_m)) / d
        nx, ny = cx_m + dx * k, cy_m + dy * k
        out.append(list(xy_to_lonlat(nx, ny, lat0)))
    return out


def polygon_path_length(poly):
    return sum(haversine(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))


def speed_series(n, dt, base_v, rng):
    """
    随机速度序列（移植自 lepao3 `lepao/track.py::math_track` 的已验证速度模型）。

    ★ 为什么不用单一正弦：
      lepao3 的模块注释明确写着模板"速度含停走段，**无 sin 周期**"。
      纯周期信号在频域上极干净；而反作弊里的 `noise_spectrum` / `speed_physiological`
      会查"速度方差过小（<0.1）"与"自相关异常"。真实跑步速度是**长周期起伏 + 随机噪声
      + 偶发停走**的叠加。

    模型（lepao3 常量）：
      v = base_v·(1 + 0.12·sin(2πt/period + φ)) + N(0, 0.22)
      period ∈ U(150, 230) s        ← 长周期起伏，不是几秒的正弦
      起跑 30 s 线性加速到全速
      偶发停走段（等红灯/系鞋带）：p≈0.004/采样点，持续 5~25 s，速度降到 15%
      钳位 v ∈ [0.02, 6.2] m/s
    """
    period = rng.uniform(150, 230)
    phase = rng.uniform(0, 2 * math.pi)
    vs = []
    pause_left = 0.0
    drift = 0.0
    for i in range(n):
        t = i * dt
        v = base_v * (1 + 0.12 * math.sin(2 * math.pi * t / period + phase))
        # ★ 速度的随机成分必须是**平滑（自相关）**的，不能是白噪声：
        #   位移 = 速度×dt + 噪声增量，而"对 AR(1) 噪声做差分"会把自相关差掉。
        #   所以决定"相邻点位移自相关"的其实是**速度序列的平滑度**。
        #   Neko 的速度白噪声只有 σ=0.008（几乎全平滑）；lepao3 的 N(0,0.22) 在我们
        #   更大的采样间隔下会主导位移波动 → 实测自相关掉到 0.06~0.28（会被判白噪声）。
        drift = 0.90 * drift + rng.gauss(0, base_v * 0.055)   # 平滑漂移项
        v += drift + rng.gauss(0, 0.05)                       # 少量白噪声（拟测量抖动）
        if pause_left > 0:
            v *= 0.15                                    # 停走段
            pause_left -= dt
        elif rng.random() < 0.004:
            pause_left = rng.uniform(5, 25)
        if t < 30:                                       # 起跑加速
            v *= 0.55 + 0.45 * (t / 30)
        vs.append(max(0.02, min(v, 6.2)))
    return vs


def kalman_smooth(pts, q=1e-9, r=2.5e-9, lat0=None):
    """
    一维卡尔曼平滑（逐轴），复刻客户端自己的 `PathSmoothTool.kalmanFilter`。

    ★ 为什么必须做这一步（本次实测得出的关键结论）：
      实测（见下）表明 —— 速度序列 lag1=+0.88、纯运动位移 lag1=+0.84 都很平滑，
      但**一旦叠加 GPS 噪声，位移的 lag1 自相关立刻掉到 0.05**（加大噪声自相关 ρ 也救不回来，
      只从 0.047 升到 0.071）。因为"相邻点位移"是 AR(1) 噪声的**差分**，差分本身是白的。
      ⇒ 靠调噪声参数**永远**过不了 budaolepao 那条 `noise_spectrum`（阈值 0.3）。
      正解是真实客户端本来就做的事：**上传前做卡尔曼滤波**（`PathSmoothTool`）。
      平滑后的残差很小、且随运动平滑变化 ⇒ 位移自相关回到运动本身的水平。

    q: 过程噪声（越小越信任匀速模型 ⇒ 越平滑）；r: 观测噪声
    """
    if len(pts) < 3:
        return list(pts)
    out_x, out_y = [], []
    for axis in (0, 1):
        x = pts[0][axis]
        p_ = 1.0
        res = []
        for i, pt in enumerate(pts):
            p_ += q
            k = p_ / (p_ + r)
            x = x + k * (pt[axis] - x)
            p_ = (1 - k) * p_
            res.append(x)
        (out_x if axis == 0 else out_y).extend(res)
    return list(zip(out_x, out_y))


def build_route(ring, rng, need_m, lap_offset_m=6.0, wobble=0.16, waypoints=None):
    """
    把内环变成"可用于多圈行走"的折线（移植自 lepao3 `math_track`）。

    三个让路线不"完美"的技巧（**也是抗 `trajectory_similarity` 的手段**）：
      1. 每圈整体加随机偏移 ±lap_offset_m（lepao3 用 ±0.00006°，约 ±6m）
      2. 相邻顶点之间插入**中点并做垂向抖动**（lepao3 wobble=0.16），
         使路径不再是规则多边形
      3. 方向随机反转 + 起点随机旋转

    返回 (route, cum)，cum 为累计里程（米）。
    """
    order = list(ring)
    if rng.random() < 0.5:
        order = order[::-1]
    k = rng.randrange(len(order))
    order = order[k:] + order[:k]

    lat0 = sum(p[1] for p in order) / len(order)
    def perp_jitter(a, b):
        d = haversine(a, b)
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        ax, ay = lonlat_to_xy(a[0], a[1], lat0)
        bx, by = lonlat_to_xy(b[0], b[1], lat0)
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / L, dx / L                          # 法向
        mag = wobble * d * rng.uniform(0.5, 1.0) * (1 if rng.random() < 0.5 else -1)
        return xy_to_lonlat(lonlat_to_xy(mx, my, lat0)[0] + nx * mag,
                            lonlat_to_xy(mx, my, lat0)[1] + ny * mag, lat0)

    route = []
    lap = 0
    peri = polygon_path_length(order)
    wps = list(waypoints or [])
    while peri * (lap + 1) < need_m * 1.25:
        off_m = (rng.uniform(-lap_offset_m, lap_offset_m),
                 rng.uniform(-lap_offset_m, lap_offset_m))
        seq = []
        for p in order:
            x, y = lonlat_to_xy(p[0], p[1], lat0)
            seq.append(xy_to_lonlat(x + off_m[0], y + off_m[1], lat0))
        # ★ 穿必经点（对标 Neko 的 A* 穿必经点 / Inverx 把 check_point 当必经点）：
        #   把每个 waypoint 插进环上的随机位置，使折线"绕过去再回来"。
        #   到 waypoint 的两段**不加垂向抖动**（否则可能偏出打卡半径）。
        # ★ 必经点必须放在**路线前段**：walk 只走 ideal_m（≈ 目标里程），
        #   而 build_route 造出的路线长约 1.25×目标 —— 插在中后段的必经点会被**走不到**。
        #   （这个 bug 导致过"打卡点最近距 32.1m > r=30"的假阴性。）
        #   所以只在第 1 圈插入，且紧跟在首个顶点之后 ⇒ 必定落在实际行走区间内。
        if wps and lap == 0:
            # ★ 必经点要按「最近邻」排序后再插：直接按原顺序插会串出一条很长的锯齿前导，
            #   后几个点落到实际行走距离之外 ⇒ 漏命中（实测 3 个种子漏 2 个点，约 200m 未达）。
            #   最近邻把前导压到接近最短路径（候选圆数量有限，串起来的里程远小于达标所需）。
            rest = list(wps)
            cur = seq[0]
            ordered = []
            while rest:
                nxt = min(rest, key=lambda q: haversine(cur, q))
                ordered.append(nxt); rest.remove(nxt); cur = nxt
            ins = 1
            for wp in ordered:
                seq.insert(ins, wp); ins += 1
        for i in range(len(seq)):
            a, b = seq[i], seq[(i + 1) % len(seq)]
            route.append(a)
            if any(haversine(a, w) < 1.0 or haversine(b, w) < 1.0 for w in wps):
                continue                       # waypoint 相邻段不加抖动
            route.append(perp_jitter(a, b))
        lap += 1
    route.append(route[0])

    cum = [0.0]
    for i in range(1, len(route)):
        cum.append(cum[-1] + haversine(route[i - 1], route[i]))
    return route, cum


def route_at(route, cum, s):
    """按累计里程 s 在折线上取点（二分查找，移植自 lepao3 `route_at`）。"""
    s = min(max(s, 0.0), cum[-1] - 0.01)
    lo, hi = 0, len(cum) - 1
    while lo < hi:
        m = (lo + hi) // 2
        if cum[m] < s:
            lo = m + 1
        else:
            hi = m
    i = max(lo - 1, 0)
    seg = (cum[i + 1] - cum[i]) or 1.0
    t = (s - cum[i]) / seg
    a, b = route[i], route[i + 1]
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def ar1_noise(rng, n, sigma_m=1.6, rho=0.86, lat0=34.8):
    """
    AR(1) 自相关 GPS 漂移（移植自 lepao3 `math_track` 的 jl/jn 递推）。

    ★ 为什么必须这样：
      反作弊规则 `noise_spectrum` 判定 **lag-1 自相关 ≈ 0 = 白噪声 = 伪造**（打分 0.5）。
      真实 GPS 漂移是**自相关的**（误差缓慢游走、有惯性）。lepao3 的写法：
          jl = jl·0.86 + N(0, 1.6)
          jn = jn·0.86 + N(0, 1.9)/cos(lat)
      即一阶自回归，自相关 ρ≈0.86 —— 远高于 0.3 的阈值。
    返回 [(dlng, dlat), …]（单位：度）。
    """
    out = []
    jl = jn = 0.0
    for _ in range(n):
        jl = jl * rho + rng.gauss(0, sigma_m)
        jn = jn * rho + rng.gauss(0, sigma_m * 1.19)
        out.append((jl / (111320.0 * max(math.cos(math.radians(lat0)), 1e-6)),
                    jn / 111320.0))
    return out


def dist_to_boundary(pt, poly):
    """点到多边形所有边的最短距离（米）——用于判断内缩是否"真"到位。"""
    best = float("inf")
    lat0 = pt[1]
    px, py = lonlat_to_xy(pt[0], pt[1], lat0)
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        ax, ay = lonlat_to_xy(a[0], a[1], lat0)
        bx, by = lonlat_to_xy(b[0], b[1], lat0)
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        cx, cy = ax + t * dx, ay + t * dy
        best = min(best, math.hypot(px - cx, py - cy))
    return best


def _safe_point(ring_pt, lat0, rng, fence_poly, safe_anchor, max_tries=12):
    """
    在围栏内生成一个"带 GPS 噪声且不越界"的点。

    ★ 为什么要拒绝重采样 + 兜底夹取：
      "朝质心收缩"对**凹多边形**（如带缺口的学校围栏）**不等于真内缩**，
      所以边缘附近总会有少量噪声点被甩出围栏。真实 App 会用定位精度阈值
      （本项目实测 40/60m）把这类点滤掉，表现为 `outerNum=0`。
      这里等价地把越界点拉回来，并记录被夹取的点数供自查。
    """
    lng, lat = ring_pt
    x, y = lonlat_to_xy(lng, lat, lat0)
    last = (x, y)
    for _ in range(max_tries):                          # 拒绝重采样
        last = (x + rng.gauss(0, GPS_NOISE_M), y + rng.gauss(0, GPS_NOISE_M))
        c = xy_to_lonlat(*last, lat0)
        if point_in_polygon(c, fence_poly):
            return c, False
    # 兜底：先试「环上原位置附近的小抖动」，都越界就直接用深水锚点
    ax, ay = lonlat_to_xy(safe_anchor[0], safe_anchor[1], lat0)
    for _ in range(6):
        c = xy_to_lonlat(ax + rng.gauss(0, 2.0), ay + rng.gauss(0, 2.0), lat0)
        if point_in_polygon(c, fence_poly):
            return c, True
    return safe_anchor, True


def inscribed_circle(poly, grid=70):
    """
    求多边形内最大内切圆（近似）：粗网格找最深的内部点 → 局部坐标下降细化。
    返回 (center(lng,lat), radius_m)。

    ★ 为什么需要它：对**凹多边形**（如带缺口的围栏），"朝质心收缩"**不是真内缩**，
      内环仍会穿过细窄处，噪声点必然越界。而**圆一定是凸的、一定在多边形内**，
      所以"绕圈跑"是任意形状下都成立的兜底轨迹形态（现实中也确有人在操场绕圈）。
    """
    lngs = [p[0] for p in poly]
    lats = [p[1] for p in poly]
    x0, x1 = min(lngs), max(lngs)
    y0, y1 = min(lats), max(lats)
    best, best_d = None, -1.0
    for i in range(grid):
        for j in range(grid):
            c = (x0 + (x1 - x0) * (i + 0.5) / grid, y0 + (y1 - y0) * (j + 0.5) / grid)
            if not point_in_polygon(c, poly):
                continue
            d = dist_to_boundary(c, poly)
            if d > best_d:
                best, best_d = c, d
    if best is None:
        return polygon_centroid(poly), 0.0
    px, py = best
    for _ in range(8):                                  # 局部细化
        step = max(best_d * 0.25, 1e-7)
        improved = False
        for dx, dy in ((step, 0), (-step, 0), (0, step), (0, -step),
                       (step, step), (-step, -step), (step, -step), (-step, step)):
            c = (px + dx, py + dy)
            if not point_in_polygon(c, poly):
                continue
            d = dist_to_boundary(c, poly)
            if d > best_d:
                best_d, px, py, improved = d, c[0], c[1], True
        if not improved:
            break
    return (px, py), best_d


def _walk_along(route, cum, ideal_m, avg_speed, rng, lat0, fence_poly, safe_anchor,
                pace_factor=None, check_in_points=None):
    """
    沿给定折线走 ideal_m 米，产出带 AR(1) 噪声的点列。
    返回 (点列, 时长秒, 被夹取点数, 速度序列)。

    pace_factor 移植自 lepao3：每次运行 U(0.82, 1.18) 的个人配速差异，
    这既是自然性，也是抗 `trajectory_similarity` 的手段之一。
    """
    pf = pace_factor if pace_factor is not None else rng.uniform(0.82, 1.18)
    base_v = avg_speed / pf                            # 使平均配速≈目标
    # 先估时长，再据时长定采样点数（迭代 2 次即可收敛）
    total_t = ideal_m / base_v
    for _ in range(3):
        n = max(4, int(total_t / SAMPLE_INTERVAL))
        dt = total_t / n
        vs = speed_series(n + 1, dt, base_v, rng)
        # 末段减速（剩余 <100m）——lepao3 同款
        acc = 0.0
        for i, v in enumerate(vs):
            remaining = ideal_m - acc
            if remaining < 100:
                vs[i] = v * max(0.18, remaining / 100)
            acc += vs[i] * dt
        total_t = n * dt

    step_m = sum(vs[i] * dt for i in range(1, len(vs))) / max(1, len(vs) - 1)
    # ★ 噪声幅度必须相对"每步位移"来定：真实 GPS 误差与步长同量级。
    #   若固定一个绝对值（如 1.6m），在步长 13m 时会被速度波动淹没，
    #   位移序列的 lag-1 自相关会掉到 0.3 以下 → 被判"白噪声=伪造"（实测 0.221）。
    sigma_m = max(1.6, 0.28 * step_m)

    def build(rho_, sigma_):
        noises = ar1_noise(rng, len(vs), sigma_m=sigma_, rho=rho_, lat0=lat0)
        _pts, _clamped, acc = [], 0, 0.0
        for i, v in enumerate(vs):
            if i > 0:
                acc += v * dt
            bp = route_at(route, cum, acc)
            dlng, dlat = noises[i]
            cand = (bp[0] + dlng, bp[1] + dlat)
            if point_in_polygon(cand, fence_poly):
                _pts.append(cand)
            else:
                q, was = _safe_point(bp, lat0, rng, fence_poly, safe_anchor)
                _clamped += int(was or True)
                _pts.append(q)
        return _pts, _clamped

    def lag1(ps):
        d = [haversine(ps[i], ps[i + 1]) for i in range(len(ps) - 1)]
        if len(d) < 4:
            return 0.0
        m = sum(d) / len(d)
        a = [x - m for x in d]
        den = sum(x * x for x in a)
        return 0.0 if den < 1e-12 else sum(a[i] * a[i + 1] for i in range(len(a) - 1)) / den

    # 闭环择优：目标是**同时**满足
    #   (a) 位移自相关 ≥ 0.35（过 anti-白噪声 检测）
    #   (b) 必打点全部命中（卡尔曼平滑会把"绕去打卡点"的尖峰抹平 ⇒ 必须把它纳入择优）
    # ★ 这是本轮踩到的坑：只按自相关择优，会把 4/5 号种子选成"平滑过头、没走到打卡点"的方案。
    cps = list(check_in_points or [])

    def hits_of(ps):
        n = 0
        for c in cps:
            ctr, rad = tuple(c["center"]), float(c["radius"])
            if ps and min(haversine(q, ctr) for q in ps) <= rad:
                n += 1
        return n

    need_hits = len(cps)
    all_cands = []
    for rho_ in (0.86, 0.94):
        raw, cl_ = build(rho_, sigma_m)
        for r_ in (2.5e-9, 4.0e-9, 6.0e-9):
            sm = kalman_smooth(raw, q=1e-9, r=r_)
            fixed = []
            for bp, sp in zip(raw, sm):
                fixed.append(sp if point_in_polygon(sp, fence_poly) else bp)
            ac = lag1(fixed)
            h = hits_of(fixed)
            # 排序键：先看是否全命中，再看命中数、自相关是否达标、平滑强度（越小越保形）
            key = (h == need_hits and abs(ac) >= 0.35, h, abs(ac) >= 0.35, h, -r_)
            all_cands.append((key, fixed, cl_, ac, h))
            if key[0]:
                break
    all_cands.sort(key=lambda t: t[0], reverse=True)
    key, pts, clamped, best_ac, best_hits = all_cands[0]
    return pts, total_t, clamped, vs


def _walk_circle(center, radius_m, ideal_m, avg_speed, rng, lat0, fence_poly,
                 check_in_points=None):
    """绕内切圆跑（兜底形态）——同样用随机速度序列 + AR(1) 噪声。"""
    circ = 2 * math.pi * radius_m
    ring = [xy_to_lonlat(*lonlat_to_xy(center[0], center[1], lat0), lat0) for _ in range(1)]
    # 用圆的正多边形近似当折线，复用同一条行走逻辑
    N = 720
    poly = []
    cx, cy = lonlat_to_xy(center[0], center[1], lat0)
    for k in range(N):
        th = 2 * math.pi * k / N
        r = radius_m * (1.0 + 0.05 * math.sin(th * 3.0))
        poly.append(xy_to_lonlat(cx + r * math.cos(th), cy + r * math.sin(th), lat0))
    route, cum = build_route(poly, random.Random(12345), ideal_m * 1.4,
                             lap_offset_m=2.0, wobble=0.03,
                             waypoints=[tuple(c["center"]) for c in (check_in_points or [])])
    anchor = xy_to_lonlat(cx, cy, lat0)
    if not point_in_polygon(anchor, fence_poly):
        anchor = max(poly, key=lambda q: dist_to_boundary(q, fence_poly))
    pts, total_t, clamped, _ = _walk_along(route, cum, ideal_m, avg_speed, rng, lat0,
                                           fence_poly, anchor,
                                           check_in_points=check_in_points)
    del circ, ring
    return pts, total_t, clamped


def generate(fence_poly, target_km, pace_min_per_km, start_dt=None, seed=None,
             margin_m=None, split_segments=1, include_holes=None, check_in_points=None):
    """
    返回 (payload, trajectory, report)。

    ★ 两个必须"解"出来的量（本函数的核心价值）：

    1. **里程标定**：`target_km` 指**上报里程**（服务端看到的数）。GPS 噪声会虚增实测距离
       （σ=6m × 每 5s 一点，实测虚增 15~20%），所以迭代反解"理想行走里程"。
       标定从**下方**收敛（`target ≤ real ≤ target+0.03`），避免四舍五入到 2 位后跌破达标线。

    2. **围栏内缩边距自适应**：噪声会把点甩出围栏（`outerNum`），而围栏形状越"瘦"越容易。
       所以从 35m 起逐步加大内缩，直到 `outerNum == 0`（全部落在围栏内）。
    """
    rng = random.Random(seed)
    lat0 = sum(p[1] for p in fence_poly) / len(fence_poly)
    avg_speed = 1000.0 / (pace_min_per_km * 60.0)

    margins = [margin_m] if margin_m else [35, 45, 55, 70, 90, 120, 150]

    def _calibrate(walk):
        """从下方收敛到 [target, target+0.05]；**绝不接受低于目标**的候选。"""
        rng_ = random.Random(seed)
        ideal = target_km * 1000.0
        cand = None
        for _ in range(15):
            pts_, t_, cl_ = walk(ideal, rng_)[:3]
            km = sum(haversine(pts_[i], pts_[i + 1]) for i in range(len(pts_) - 1)) / 1000.0
            err = km - target_km
            if cand is None:
                cand = (err, pts_, t_, cl_, ideal)
            elif err >= 0 and (cand[0] < 0 or err < cand[0]):
                cand = (err, pts_, t_, cl_, ideal)
            elif cand[0] < 0 and err < 0 and err > cand[0]:
                cand = (err, pts_, t_, cl_, ideal)
            if target_km <= km <= target_km + 0.05:
                cand = (err, pts_, t_, cl_, ideal)
                break
            ideal *= target_km / km if km else 1.0
            if km < target_km:
                ideal *= 1.015
        return cand

    _need_hits = len(check_in_points or [])

    def _hits(ps):
        n = 0
        for c in (check_in_points or []):
            ctr, rad = tuple(c["center"]), float(c["radius"])
            if ps and min(haversine(q, ctr) for q in ps) <= rad:
                n += 1
        return n

    def _score(clamped, outer, pts_):
        """排序键：① 必打点是否全命中（最重要）② 外点数 ③ 夹取数 ④ 越界+夹取总量"""
        h = _hits(pts_)
        return (h < _need_hits, outer > 0, clamped > 0, h * -1, clamped + outer)

    target_pace_min = pace_min_per_km
    # ★ pace_factor 必须**每次生成只掷一次**：若每次 _walk_along 都重掷，
    #   配速标定就是在追一个移动的目标，永远收敛不了（实测配速漂到 7′17″）。
    pace_factor = rng.uniform(0.82, 1.18)

    def _calibrate_pace(walk_factory):
        """
        ★ 配速外环标定（**这层是必需的，不是装饰**）：
          停走段、起跑加速、末段减速都会把实际时长抬高 ⇒ 上报配速必然比"名义平均配速"慢。
          只标定里程会得到 8'12" 的配速（目标 6'12"）。所以要在里程标定之外，
          再按 (实测配速 / 目标配速) 迭代缩放速度，直到上报配速命中目标。

        ★ 返回**迭代过程中偏差最小的那一版**，不是最后一次的：
          缩放速度会改变时长 ⇒ 改变采样点数 ⇒ 换掉噪声实现，
          所以"配速 → 缩放系数"是个**带噪声的映射**，定点迭代会来回震荡；
          取最后一跳等于取到震荡的波峰（实测个别种子因此偏到 4′58″ / 7′10″）。
          更新式取 0.7 次幂做阻尼，抑制震荡。
        """
        scale = 1.0
        best_res, best_scale, best_dev = None, scale, float("inf")
        for _ in range(8):
            res = _calibrate(walk_factory(avg_speed * scale))
            _, pts_, t_, _, _ = res
            km = sum(haversine(pts_[i], pts_[i + 1])
                     for i in range(len(pts_) - 1)) / 1000.0
            if km <= 0:
                break
            rep_pace = (t_ / 60.0) / km                  # min/km
            dev = abs(rep_pace - target_pace_min) / target_pace_min
            if dev < best_dev:
                best_res, best_scale, best_dev = res, scale, dev
            if dev <= 0.03:
                break
            scale *= (rep_pace / target_pace_min) ** 0.7
        return best_res, best_scale, best_dev

    best = None                                          # (pts,t,ideal,inner,innerN,outerN,margin,clamped,shape)
    for margin in margins:
        inner = shrink_polygon(fence_poly, margin)
        if polygon_path_length(inner) < 200:             # 缩没了，放弃
            break
        safe_anchor = max(inner, key=lambda q: dist_to_boundary(q, fence_poly))
        if not point_in_polygon(safe_anchor, fence_poly):
            safe_anchor = max(fence_poly, key=lambda q: dist_to_boundary(q, fence_poly))
        # ★ 路线种子必须**每次不同**：反作弊的 `trajectory_similarity` 用网格 Jaccard
        #   （阈值 0.9）检测"重复使用同一路线"。固定种子 ⇒ 每次轨迹几乎重合 ⇒ 被判 route reuse。
        #   所以默认由 seed 派生（无 --seed 时取随机值），只有显式固定 seed 才复现同一路线。
        wps = [tuple(c["center"]) for c in (check_in_points or [])]
        route, cum = build_route(inner, random.Random(seed if seed is not None else rng.randrange(1 << 30)),
                                 target_km * 1350.0, waypoints=wps)
        (_, pts, t, clamped, ideal_m), _sp, _dev = _calibrate_pace(
            lambda av: (lambda im, r_: _walk_along(route, cum, im, av, r_, lat0,
                                                   fence_poly, safe_anchor,
                                                   pace_factor=pace_factor,
                                                   check_in_points=check_in_points)))
        inner_num = sum(1 for p in pts if point_in_polygon(p, fence_poly))
        if include_holes:
            for hole in include_holes:
                inner_num -= sum(1 for p in pts if point_in_polygon(p, hole))
        outer_num = len(pts) - inner_num
        cur = (pts, t, ideal_m, inner, inner_num, outer_num, margin, clamped, "ring",
               _sp, _dev)
        if best is None or _score(clamped, outer_num, pts) < _score(best[7], best[5], best[0]):
            best = cur
        if outer_num == 0 and clamped == 0 and _hits(pts) == _need_hits:
            break                                         # 环策略全达标，收工

    # 凹多边形兜底：内切圆跑圈（圆一定在多边形内 ⇒ 结构上保证 0 外点 / 0 夹取）
    if best is None or best[7] > 0 or best[5] > 0:
        center, R = inscribed_circle(fence_poly)
        R_eff = R - 4 * GPS_NOISE_M
        if R_eff > 25:
            (_, pts, t, clamped, ideal_m), _sp, _dev = _calibrate_pace(
                lambda av: (lambda im, r_: _walk_circle(center, R_eff, im, av, r_,
                                                        lat0, fence_poly,
                                                        check_in_points=check_in_points)[:3]))
            inner_num = sum(1 for p in pts if point_in_polygon(p, fence_poly))
            if include_holes:
                for hole in include_holes:
                    inner_num -= sum(1 for p in pts if point_in_polygon(p, hole))
            outer_num = len(pts) - inner_num
            circ = (pts, t, ideal_m, None, inner_num, outer_num, int(R_eff),
                    clamped, "circle", _sp, _dev)
            if best is None or _score(clamped, outer_num, pts) < _score(best[7], best[5], best[0]):
                best = circ

    # ★ 取**胜出候选自己的** speedScale / 标定偏差：原先 speed_scale = _sp 拿的是
    #   最后一轮 margin 的返回值，与 best 并非同一次标定（普通多 margin 场景下即报告失真）。
    (pts, total_t, ideal_m, inner, inner_num, outer_num, margin, clamped, shape,
     speed_scale, pace_dev) = best
    if inner is None:                                    # 圆策略：内环用 bbox 占位统计
        inner = [center]

    real_m = sum(haversine(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    real_km = real_m / 1000.0
    duration_s = int(round(total_t))

    avg_v = real_m / duration_s if duration_s else 0.0
    cadence = rng.uniform(*CADENCE_RANGE)
    stride = min(max(avg_v / (cadence / 60.0), STRIDE_RANGE[0]), STRIDE_RANGE[1])
    steps = int(round(real_m / stride))

    pace_sec_per_km = duration_s / real_km if real_km else 0
    pm, ps = divmod(int(round(pace_sec_per_km)), 60)
    pace_str = f"{pm}\u2032{ps:02d}\u2033"

    start_dt = start_dt or default_start(duration_s)
    end_dt = start_dt + timedelta(seconds=duration_s)

    # 轨迹：外层分段、内层 {"longitude","latitude"} 点对象（与真实记录一致）
    def mk(seg):
        return [{"longitude": round(p[0], 15), "latitude": round(p[1], 15)} for p in seg]

    if split_segments > 1:
        size = max(1, len(pts) // split_segments)
        trajectory = [mk(pts[i:i + size]) for i in range(0, len(pts), size)]
    else:
        trajectory = [mk(pts)]

    payload = {
        "schoolId": None,                                # 由调用方填（档案 schoolId）
        "fenceId": None,                                 # 由调用方填（目标围栏 id）
        "start": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "end": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "distance": round(real_km, 2),
        "duration": duration_s,
        "pace": pace_str,
        "step": steps,
        "innerNum": inner_num,
        "outerNum": outer_num,
        "checkInNum": 0,
        "trajectoryUrl": "",         # 实测真实记录也是空串 ⇒ 该字段可能非必填（待 live 验证）
        "checkInJson": "",          # 实测真实记录为**空串**（<他人记录> 即使 checkInNum=3 也是 ''）
    }
    # ★ 打卡点几何实测：每个打卡点到轨迹的最近距离 vs 半径（服务端就是这么算的）
    ci = []
    for c in (check_in_points or []):
        ctr, rad = tuple(c["center"]), float(c["radius"])
        dmin = min(haversine(p, ctr) for p in pts) if pts else 1e9
        ci.append({"center": [round(ctr[0], 6), round(ctr[1], 6)], "radius": rad,
                   "minDistM": round(dmin, 1), "hit": dmin <= rad})
    report_ci = {"checkInPointCount": len(ci), "checkInsHit": sum(1 for x in ci if x["hit"]),
                 "checkInDetail": ci}

    report = {
        "targetKm": target_km,
        "reportedKm": round(real_km, 2),
        "calibratedIdealKm": round(ideal_m / 1000.0, 3),
        "noiseInflationPct": round((real_km / (ideal_m / 1000.0) - 1) * 100, 1),
        "durationSec": duration_s,
        "paceStr": pace_str,
        "paceSecPerKm": round(pace_sec_per_km, 1),
        "paceTargetMinPerKm": target_pace_min,
        "paceDeviationFrac": round(pace_dev, 4),
        "steps": steps,
        "strideM": round(stride, 3),
        "cadenceSpm": round(cadence, 1),
        "points": len(pts),
        "innerNum": inner_num,
        "outerNum": outer_num,
        "fenceVertices": len(fence_poly),
        "innerRingVertices": len(inner),
        "usedMarginM": margin,
        "speedScale": round(speed_scale, 3),
        "shape": shape,
        "clampedPoints": clamped,
        "allPointsIdentical": len(set((round(p[0], 6), round(p[1], 6)) for p in pts)) <= 2,
        "sampleIntervalSec": round(total_t / max(1, (len(pts) - 1)), 2),
        "startInValidWindow": None,                      # 由 check_rules 填
        **report_ci,
    }
    payload["checkInNum"] = report_ci["checkInsHit"]      # 自报值 = 几何实测命中数
    return payload, trajectory, report


# 本校区实测规则（/api/school/config）：06:00–22:30、每日 1 次、配速 3~10、男 2.5km/女 2.0km
VALID_WINDOW = ("06:00", "22:30")
RULE_PACE = (3.0, 10.0)
RULE_KM = {"male": 2.5, "female": 2.0}


def default_start(duration_s, rng=None):
    """把起始时间放进有效时段内（默认今天，太晚则回退到昨天的同一时段）。"""
    now = datetime.now()
    today_start = now.replace(hour=9, minute=int((rng or random).random() * 50) + 5,
                              second=int((rng or random).random() * 59), microsecond=0)
    if today_start + timedelta(seconds=duration_s) <= now:
        return today_start
    return today_start - timedelta(days=1)


def check_rules(report, payload, sex_key="male"):
    """服务端规则自查（对标 budaolepao 的提交前检测模块）。"""
    warns = []
    pace_min = report["paceSecPerKm"] / 60.0
    report["paceWithinRule"] = RULE_PACE[0] <= pace_min <= RULE_PACE[1]
    if not report["paceWithinRule"]:
        warns.append(f"配速 {report['paceStr']} ({pace_min:.2f} min/km) 超出规则 "
                     f"{RULE_PACE[0]}~{RULE_PACE[1]} min/km")
    if report.get("paceDeviationFrac", 0.0) > 0.03:
        warns.append(
            f"配速标定偏差 {report['paceDeviationFrac'] * 100:+.1f}%（目标 "
            f"{report.get('paceTargetMinPerKm', 0):.2f} min/km）未收敛到 ±3%："
            f"外环迭代在「配速→缩放系数」这个噪声映射上不保证收敛，换 --seed 重生成")
    need = RULE_KM[sex_key]
    report["meetsMileage"] = report["reportedKm"] >= need
    if not report["meetsMileage"]:
        warns.append(f"里程 {report['reportedKm']} km 未达 {sex_key} 达标线 {need} km")
    t = payload["start"][11:16]
    report["startInValidWindow"] = VALID_WINDOW[0] <= t <= VALID_WINDOW[1]
    if not report["startInValidWindow"]:
        warns.append(f"起始时间 {t} 不在有效时段 {VALID_WINDOW[0]}~{VALID_WINDOW[1]} 内")
    if report["outerNum"] > 0:
        warns.append(f"有 {report['outerNum']} 个点在围栏外（实测这类会被判无效）")
    if report.get("clampedPoints"):
        warns.append(f"{report['clampedPoints']} 个噪声点越界被拉回（内缩边距 {report['usedMarginM']}m 对凹多边形不足）")
    if report["allPointsIdentical"]:
        warns.append("轨迹点近乎全相同（= 站着不动；实测 <他人记录> 即此形态 → status=2）")
    if not (STRIDE_RANGE[0] <= report["strideM"] <= STRIDE_RANGE[1]):
        warns.append(f"步幅 {report['strideM']}m 不在合理区间 {STRIDE_RANGE}")
    if payload["duration"] <= 0:
        warns.append("duration 非正")
    return warns


def load_fence(from_file="geo_full.json", fence_id=None, campus_id=None):
    """从 policy/geo 结果里取围栏多边形；type=2（镂空）返回多环，取外环为主环。"""
    with open(os.path.join(HERE, from_file)) as f:
        d = json.load(f)
    cands = []
    for k, v in d.items():
        if not k.startswith("fence_"):
            continue
        for f_ in (v.get("data") or []):
            cands.append(f_)
    if not cands:                                        # policy_out.json 形态
        for k, v in (d.get("raw") or {}).items():
            if k.startswith("fenceList"):
                cands += (v.get("data") or [])
    if fence_id is not None:
        cands = [c for c in cands if c["id"] == fence_id]
    if campus_id is not None:
        cands = [c for c in cands if c.get("campusId") == campus_id]
    if not cands:
        raise SystemExit("没找到匹配围栏")
    f_ = cands[0]
    raw = f_["fence"]
    # type=1: [[lng,lat], …]    type=2: [[[lng,lat], …], …]（外环 + 镂空环）
    if isinstance(raw[0][0], (int, float)):
        return f_, raw, []
    outer = raw[0]
    holes = [r for r in raw[1:]]
    return f_, outer, holes


def main():
    ap = argparse.ArgumentParser(description="校园跑自洽轨迹生成器（只生成，不发请求）")
    ap.add_argument("--km", type=float, default=2.5, help="**上报**里程 km（默认 2.5 = 本校区男生达标线）")
    ap.add_argument("--pace", type=float, default=6.2, help="目标配速 分钟/km（本校区允许 3~10）")
    ap.add_argument("--sex", choices=["male", "female"], default="male", help="决定达标里程")
    ap.add_argument("--fence-id", type=int, default=None)
    ap.add_argument("--campus-id", type=int, default=None)
    ap.add_argument("--geo", default="geo_full.json")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--start", default=None, help="起始时间 'YYYY-mm-dd HH:MM:SS'")
    ap.add_argument("--checkin", action="store_true",
                    help="让轨迹穿过围栏配置的必打点（fence.checkInPoint）")
    ap.add_argument("--school-id", type=int, default=None,
                    help="填进 payload.schoolId（你的 schoolId，可在 policy_out.json 里看）")
    ap.add_argument("--out", default="trajectory_out.json")
    a = ap.parse_args()

    fence, poly, holes = load_fence(a.geo, a.fence_id, a.campus_id)
    if holes:
        print(f"⚠️ 该围栏有 {len(holes)} 个镂空环（type=2），已取外环为主环，镂空仅用于点数扣减")
    start_dt = datetime.strptime(a.start, "%Y-%m-%d %H:%M:%S") if a.start else None

    cps = None
    if a.checkin:
        cps = fence.get("checkInPoint") or []
        if not cps:
            print("⚠️ 该围栏没有配置 checkInPoint（type=2 或未设置），将生成普通轨迹")
    payload, traj, rep = generate(poly, a.km, a.pace, start_dt, a.seed,
                                  include_holes=holes, check_in_points=cps)
    payload["fenceId"] = fence["id"]
    if a.school_id is not None:
        payload["schoolId"] = a.school_id
    warns = check_rules(rep, payload, a.sex)

    print(f"围栏 id={fence['id']} name={fence.get('name')} 顶点={len(poly)}"
          f"{'  +镂空 %d 环' % len(holes) if holes else ''}")
    print("\n=== 自检报告 ===")
    for k, v in rep.items():
        print(f"  {k:<22} {v}")
    print("\n=== 提交体（13 字段，trajectory 已省略）===")
    print(json.dumps({**payload, "trajectory": f"<{rep['points']} 点，已写入 {a.out}>"},
                     ensure_ascii=False, indent=1))

    with open(os.path.join(HERE, a.out), "w") as f:
        json.dump({"payload": payload, "trajectory": traj, "report": rep,
                   "fence": {"id": fence["id"], "name": fence.get("name"),
                             "campusId": fence.get("campusId")}}, f,
                  ensure_ascii=False, indent=1)   # 美化：人工可读，且"复现命令产出 == 仓库样例"
    print(f"\n→ {a.out}（payload + trajectory + report）")

    if rep.get("checkInPointCount"):
        print("\n=== 必打点命中（几何实测）===")
        for i, x in enumerate(rep["checkInDetail"]):
            print(f"  打卡点{i+1} {x['center']} r={x['radius']}m  最近点距 {x['minDistM']}m  "
                  f"{'✅命中' if x['hit'] else '❌未命中'}")
        print(f"  → 命中 {rep['checkInsHit']}/{rep['checkInPointCount']}（payload.checkInNum 已同步为该值）")

    print("\n=== 提交前自查 ===" if warns else "\n=== 提交前自查：全部通过 ===")
    for w in warns:
        print("  ⚠️ " + w)


if __name__ == "__main__":
    main()
