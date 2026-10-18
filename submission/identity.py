#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
identity.py —— 设备身份持久化（移植自 NekoSportsWorldTool `identity.json`）

为什么需要（参考项目实证）：
    Neko 的 `HeaderIdentity` 有 11 个字段并**随请求发出**：platform / device_id / idfa /
    os_version / device_name / manufacturer / anchor_lat / anchor_lon / app_install_time /
    mac_address / city。它做对的关键两点：
      1. **惰性补齐 + 立即落盘**：缺字段就补，补完马上写文件 —— 保证身份跨会话稳定；
      2. **锚点每次跑步后随机漂移 ±200m 并持久化**（`flow.rs:124-141`）——
         既让起点自然变化（抗"路线复用"检测），又保持"这个人一直在这一带活动"的一致性。

我们目标的对应物：
    - 原生网关基础参数里有 `deviceId` —— 就是我们该持久化的东西。
      实测 3 条 oracle 样本里出现的 2 个 deviceId 取值均为**31 位小写 hex**
      （既非 16 字节，也非 32 位）；
      本文件生成 **16 字节（32 位 hex）**：网关把它当**不透明字符串**，未见格式校验。
      两者长度不一致，如实记录于此，不做"改写样本以对齐格式"。
    - H5 侧 localStorage 有 `h5DeviceId`；
    - 客户端上报里没有 lat/lon，但**围栏锚点**可直接用于路线生成的自然偏移。

⚠️ 凭据红线：本文件只存**设备标识与时间**，绝不存 token / 手机号 / 密码。
"""
import hashlib
import json
import os
import random
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "identity.json")

ANCHOR_DRIFT_M = 200.0          # 每次提交后锚点漂移幅度（Neko 同值）
MAC_OUI = "08:a4:"              # Neko 用固定 OUI + 随机后 3 字节（我们同样固定前缀）

DEFAULTS = {
    "platform": "android",
    "device_id": None,          # 生成 16 字节 hex（32 位）；实测样本为 31 位 hex，见文件头说明
    "idfa": None,
    "os_version": "14",
    "device_name": None,
    "manufacturer": None,
    "anchor_lat": None,
    "anchor_lon": None,
    "app_install_time": None,   # epoch ms
    "mac_address": None,
    "city": None,
    "created_at": None,
    "updated_at": None,
}

# 参考项目里出现过的真机型号池（避免固定成同一个"设备"）
DEVICE_POOL = [
    ("24129PN74C", "Xiaomi", "14"),
    ("V2324A", "vivo", "14"),
    ("PHK110", "OPPO", "14"),
    ("ALN-AL00", "HUAWEI", "12"),
    ("SM-S9280", "samsung", "14"),
]


def _rand_hex(n_bytes, rng):
    return "".join(f"{rng.randrange(256):02x}" for _ in range(n_bytes))


def _rand_mac(rng):
    return MAC_OUI + ":".join(f"{rng.randrange(256):02x}" for _ in range(3))


def load(path=PATH):
    """
    读取身份；缺失字段**惰性补齐并立即落盘**（Neko 的做法）。
    anchor 未给时不自动生成（由 set_anchor 显式设定，避免拿到一个真空锚点）。
    """
    rng = random.Random()
    d = dict(DEFAULTS)
    if os.path.exists(path):
        try:
            with open(path) as f:
                d.update(json.load(f))
        except Exception:
            pass
    changed = False
    if not d.get("device_id"):
        d["device_id"] = _rand_hex(16, rng); changed = True
    if not d.get("idfa"):
        d["idfa"] = _rand_hex(8, rng); changed = True
    if not d.get("mac_address"):
        d["mac_address"] = _rand_mac(rng); changed = True
    if not d.get("device_name"):
        name, man, osv = rng.choice(DEVICE_POOL)
        d["device_name"], d["manufacturer"], d["os_version"] = name, man, osv
        changed = True
    if not d.get("app_install_time"):
        # Neko: 安卓 now-90d、iOS now-3d
        d["app_install_time"] = int((datetime.now() - timedelta(days=90)).timestamp() * 1000)
        changed = True
    if not d.get("city"):
        d["city"] = "郑州"; changed = True
    if not d.get("created_at"):
        d["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S"); changed = True
    d["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if changed or not os.path.exists(path):
        save(d, path)
    return d


def save(d, path=PATH):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    # ★ chmod 必须在 rename **之后**做：rename 会带上 .tmp 的权限，
    #   之前只在写 tmp 前 chmod 的话，最终文件会是默认 0644（实测踩到过）。
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def set_anchor(d, lat, lon, path=PATH):
    d["anchor_lat"], d["anchor_lon"] = round(lat, 6), round(lon, 6)
    save(d, path)
    return d


def drift_anchor(d, path=PATH, meters=ANCHOR_DRIFT_M, rng=None):
    """
    每次提交后把锚点随机漂移 ±meters 并落盘（Neko `flow.rs:124-141` 同款）。
    作用：连续多次提交时起点自然变化，避免"每次都在同一个点起步"这种机械特征。
    """
    rng = rng or random.Random()
    if d.get("anchor_lat") is None:
        return d
    import math
    dl = meters / 111320.0
    dn = meters / (111320.0 * math.cos(math.radians(d["anchor_lat"])))
    d["anchor_lat"] = round(d["anchor_lat"] + rng.uniform(-dl, dl), 6)
    d["anchor_lon"] = round(d["anchor_lon"] + rng.uniform(-dn, dn), 6)
    save(d, path)
    return d


def summary(d):
    """给用户看的摘要 —— device_id/mac 只显示前后各 4 位（不回显完整标识）。"""
    def m(v, k=4):
        v = str(v or "")
        return v[:k] + "…" + v[-k:] if len(v) > k * 2 else "***"
    return (f"device_id={m(d['device_id'])}  mac={m(d['mac_address'])}  "
            f"device={d['device_name']}/{d['manufacturer']}  os={d['os_version']}  "
            f"anchor=({d.get('anchor_lat')}, {d.get('anchor_lon')})  "
            f"install={datetime.fromtimestamp(d['app_install_time'] / 1000):%Y-%m-%d}")


if __name__ == "__main__":
    d = load()
    print("身份已就绪（0600，仅设备标识，无凭据）：")
    print("  " + summary(d))
    print(f"  → {PATH}")
