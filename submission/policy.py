#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
policy.py —— 校园跑「判定规则 + 地理约束」单一事实源（对标 Inverx policy_cli 抽象）

设计原则（抄社区公认最佳实践）：
    **先拉规则，再按规则造数据** —— 不要猜阈值，不要让服务端用 400 告诉你哪里错了。

拉取（全部 GET，纯读，无副作用）：
    /api/login/personalInfo     本人档案（schoolId / 性别 / 学号）
    /api/school/config          ★ 判定规则（时段 / 达标里程 / 配速上下限 / 每日次数）
    /api/semester/currSemester  当前学期
    /api/semester/detail        学期边界
    /api/login/semesterTask     学期任务量与进度
    /api/campus/getList         校区列表
    /api/fence/getList          围栏（type=1 普通多边形 / type=2 镂空）
    /api/campus/detail          校区中心点

产出：policy_out.json（结构化）+ 终端规则单

⚠️ 凭据红线：token 从 .token 读取，只显示长度与掩码，不打印值；姓名/学号掩码。
"""
import json
import os
import sys
import time
from urllib.parse import urlencode
import urllib.error
import urllib.request
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from login import UA, WEB_ID, WEB_SECRET, TOKEN_FILE  # noqa: E402

HOST = "school-api.dingxinwen.cn"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy_out.json")


def call(path, form=None, timeout=30, retries=2):
    """H5 形状 GET：sign = md5(排序 "k=v" 拼接 + web_secret)，空值也计入。"""
    form = dict(form or {})
    for attempt in range(retries + 1):
        p = {"timestamp": str(int(time.time())), "app_id": WEB_ID}
        p.update(form)
        plain = "".join(f"{k}={p[k]}" for k in sorted(p))
        p["sign"] = hashlib.md5((plain + WEB_SECRET).encode()).hexdigest()
        url = f"https://{HOST}{path}?{urlencode(p)}"
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                return {"code": e.code, "msg": "HTTPError"}
        except Exception as e:
            if attempt == retries:
                return {"code": -1, "msg": f"{type(e).__name__}: {e}"}
            time.sleep(2)          # 瞬时抖动（实测过一次超时）→ 退避重试


def token():
    with open(TOKEN_FILE) as f:
        t = f.read().strip()
    if not t.startswith("u_token:"):
        raise SystemExit("token 形状不对（应为 u_token: 前缀）")
    return t


def mask(s, keep=2):
    s = str(s)
    return s[:keep] + "*" * max(0, len(s) - keep * 2) + s[-keep:] if len(s) > keep * 2 else "*" * len(s)


def data_of(r):
    return r.get("data") if r.get("code") == 0 else None


def main():
    t = token()
    print(f"token: u_token:***（{len(t)} 字符，已掩码）\n")

    got, fails = {}, []
    plan = [
        ("personalInfo", "/api/login/personalInfo", {}),
        ("schoolConfig", "/api/school/config", {}),
        ("currSemester", "/api/semester/currSemester", {}),
        ("semesterTask", "/api/login/semesterTask", {}),
        ("campusList", "/api/campus/getList", {}),
        ("schoolList", "/api/school/getList", {}),
    ]
    for key, path, form in plan:
        r = call(path, {**form, "token": t})
        got[key] = r
        ok = r.get("code") == 0
        print(f"  {'✓' if ok else '✗'} {path:<32} {r.get('msg')}")
        if not ok:
            fails.append((path, r))
        time.sleep(0.8)

    stu = data_of(got.get("personalInfo", {})) or {}
    cfg = data_of(got.get("schoolConfig", {})) or {}
    sem = data_of(got.get("currSemester", {})) or {}
    task = data_of(got.get("semesterTask", {})) or {}

    # 学期边界（currSemester 未给 start/end 时补 detail）
    if sem and "start" not in sem and sem.get("id"):
        r = call("/api/semester/detail", {"id": sem["id"], "token": t})
        sem = data_of(r) or sem
        got["semesterDetail"] = r
        time.sleep(0.8)

    # 围栏（针对本人档案里的 schoolId 对应校区全量拉）
    fences = []
    campuses = data_of(got.get("campusList", {})) or []
    for c in campuses[:3]:
        r = call("/api/fence/getList", {"campusId": c["id"], "token": t})
        got[f"fenceList_{c['id']}"] = r
        for f in (data_of(r) or []):
            fences.append({**f, "campusName": c.get("name")})
        time.sleep(0.8)

    sex = stu.get("sex")
    sex_key = "male" if str(sex) in ("1", "male", "M", "男") else "female"
    rules = {
        "school": cfg.get("name"),
        "schoolId": cfg.get("id"),
        "validWindow": f"{cfg.get('dayStartTime')} ~ {cfg.get('dayEndTime')}",
        "dayValidTimes": cfg.get("dayValidTimes"),
        "targetMileageKm": cfg.get(f"{sex_key}Mileage"),
        "paceRangeMinPerKm": [cfg.get(f"{sex_key}MaxPace"), cfg.get(f"{sex_key}MinPace")],
        "sexAssumed": sex_key,
    }
    geo = {
        "campuses": [{"id": c["id"], "name": c.get("name"), "fenceNum": c.get("fenceNum")} for c in campuses],
        "fences": [{"id": f["id"], "campusId": f.get("campusId"), "name": f.get("name"),
                    "type": f.get("type"),
                    "vertices": (len(f["fence"]) if isinstance(f.get("fence"), list) else None),
                    "nested": bool(f.get("fence") and isinstance(f["fence"][0], list)
                                   and f["fence"][0] and isinstance(f["fence"][0][0], list))}
                   for f in fences],
    }
    out = {
        "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rules": rules,
        "task": task,
        "semester": {k: sem.get(k) for k in ("id", "name", "start", "end")},
        "student": {"schoolId": stu.get("schoolId"), "sex": sex,
                    "name": mask(stu.get("name") or ""),
                    "sequence": mask(stu.get("sequence") or ""),
                    "department": stu.get("department"), "clazz": stu.get("clazz")},
        "geo": geo,
        "raw": got,
        "fails": [{"path": p, "resp": r} for p, r in fails],
    }
    with open(OUT, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("\n=== 判定规则（服务端权威值）===")
    for k, v in rules.items():
        print(f"  {k:<20} {v}")
    print("\n=== 学期任务 ===")
    print(f"  {out['semester'].get('name')}  {out['semester'].get('start')} ~ {out['semester'].get('end')}")
    print(f"  要求 {task.get('semesterTimes')} 次 / {task.get('semesterMileage')} km"
          f"   已跑 {task.get('myTimes')} 次 / {task.get('myMileage')} km")
    print("\n=== 地理约束 ===")
    print(f"  校区 {len(geo['campuses'])} 个：{[c['name'] for c in geo['campuses']]}")
    print(f"  围栏 {len(geo['fences'])} 条：")
    for f in geo["fences"]:
        print(f"    - id={f['id']} type={f['type']} {f['name']}"
              f"（顶点 {f['vertices']}{'，嵌套镂空' if f['nested'] else ''}）")
    if fails:
        print(f"\n⚠️ {len(fails)} 个接口未成功：{[p for p, _ in fails]}")
    print(f"\n→ {os.path.basename(OUT)}")


if __name__ == "__main__":
    main()
