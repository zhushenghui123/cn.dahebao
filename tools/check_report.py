#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_report.py —— 按 android-reverse 技能的输出契约自检一份报告草稿。

它不判断技术结论对不对（那需要人/需要实验），只检查**契约是否被遵守**：
结论有没有贴置信度标签、证据链有没有依据、负向结论和副作用有没有写、
有没有把凭据写进报告、偏移有没有写清是哪一种。

用法：
    python3 check_report.py report.md
    python3 check_report.py report.md --strict      # WARN 也算失败
    python3 check_report.py --selftest              # 用内置的好/坏样本自证（对照组）

退出码：有 ERROR ⇒ 1；否则 0（--strict 时有 WARN 也算 1）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---- 允许的置信度标签（两套梯子，按任务类型选其一）----------------------------
NATIVE_LABELS = {
    "static-candidate", "runtime-hit", "causal", "bypass-verified", "disproven",
}
REQUEST_LABELS = {
    "static-candidate", "traffic-observed", "hook-confirmed",
    "native-confirmed", "replay-verified",
}
ALL_LABELS = NATIVE_LABELS | REQUEST_LABELS

# ---- 报告必须有的章节（用关键词匹配，不要求标题一字不差）----------------------
REQUIRED_SECTIONS = [
    ("目标与范围",   ("目标", "范围", "总览", "项目概览")),
    ("证据",         ("证据", "发现", "判据")),
    ("证据链",       ("证据链", "证据表", "关联")),
    ("结论",         ("结论",)),
    ("实验",         ("实验", "对照")),
    ("未解/下一步",   ("未解", "下一步", "待补", "遗留", "后续")),
    ("副作用",       ("副作用", "测试边界", "影响披露")),
]

# ---- 凭据形态（写进报告就要报警，不回显值）------------------------------------
SECRET_PATTERNS = [
    ("长十六进制串(>=32)", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
    ("JWT 形态",           re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("手机号形态",         re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("token 实值",         re.compile(r"\btoken\s*[=:]\s*[\"']?[A-Za-z0-9_\-]{16,}")),
]

# ---- 偏移基准词：裸 0x 地址必须能说出它相对谁 -----------------------------------
OFFSET_BASIS = re.compile(
    r"so!|file offset|image offset|\bRVA\b|load base|runtime|IDA|Ghidra|基址|偏移|"
    r"file_offset|image_offset|load_base",
    re.I,
)
BARE_ADDR = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")

# ---- 运行时证据必须能复现：PID / ABI / 时间 -----------------------------------
RUNTIME_ANNOT = re.compile(r"\bPID\b|pid=|ABI|abi=|时间|timestamp|观测于", re.I)

WRITE_HINTS = re.compile(r"POST|提交|写操作|创建|save|写入|上传")

# 副作用章节的两条合格形态：① 明确声明无写操作  ② 写到能据此清理
NO_WRITE = re.compile(r"无写操作|未产生|全部只读|仅只读|本轮只读|没有写|无写")
CLEANUP = re.compile(r"清除|清理|重置|删除|披露|撤回")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warns: list[str] = []
        self.oks: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    def ok(self, msg: str) -> None:
        self.oks.append(msg)


def sections(text: str) -> dict[str, str]:
    """把 markdown 按二级标题切开；键是标题文本。"""
    out: dict[str, str] = {}
    cur = "_preamble"
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            out[cur] = "\n".join(buf)
            cur = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    out[cur] = "\n".join(buf)
    return out


def find_section(secs: dict[str, str], names: tuple[str, ...]) -> str | None:
    for title, body in secs.items():
        if any(n in title for n in names):
            return body
    return None


def check(text: str) -> Report:
    r = Report()
    secs = sections(text)

    # 1) 必需章节
    for label, keys in REQUIRED_SECTIONS:
        if find_section(secs, keys) is None:
            r.err(f"缺章节：{label}")
    if not r.errors:
        r.ok(f"章节齐全（{len(REQUIRED_SECTIONS)} 项）")

    # 2) 结论区：每条结论必须带标签
    body = find_section(secs, ("结论",)) or ""
    HR = ("---", "***", "___", "———")
    items = [ln.strip() for ln in body.splitlines()
             if ln.strip()
             and not ln.strip().startswith(("|", "#", ">"))
             and ln.strip() not in HR]
    items = [i for i in items if re.match(r"^([\-\*\d]|．|。)", i) or "：" in i]
    if items:
        unlabeled = [i for i in items if not any(f"`{l}`" in i or l in i for l in ALL_LABELS)]
        if unlabeled:
            r.err(f"结论缺置信度标签：{len(unlabeled)}/{len(items)} 条（例：{unlabeled[0][:40]}…）")
        else:
            r.ok(f"结论全部带标签（{len(items)} 条）")
    else:
        r.warn("结论区没有解析到条目（可能格式特殊，请人工确认）")

    # 3) 标签是否在允许集合内
    used = set()
    for m in re.finditer(r"标签[：:]\s*`?([A-Za-z\-]+)`?", text):
        used.add(m.group(1))
    bad = {u for u in used if u not in ALL_LABELS}
    if bad:
        r.err(f"使用了未定义的标签：{sorted(bad)}")
    elif used:
        r.ok(f"标签取值合法（{sorted(used)}）")

    # 4) 证据链表的"证据/依据"列非空
    if "|" in body or True:
        chain = find_section(secs, ("证据链",)) or ""
        rows = [ln for ln in chain.splitlines() if ln.strip().startswith("|")]
        if len(rows) >= 2:
            header = [c.strip() for c in rows[0].strip("|").split("|")]
            data = rows[2:] if len(rows) > 2 else []
            idx = None
            for i, h in enumerate(header):
                if "证据" in h or "依据" in h:
                    idx = i
                    break
            if idx is None:
                r.warn("证据链表没有『证据/依据』列")
            else:
                empty = 0
                for row in data:
                    cells = [c.strip() for c in row.strip("|").split("|")]
                    if len(cells) > idx and cells[idx] in ("", "-", "—", "N/A"):
                        empty += 1
                if empty:
                    r.err(f"证据链表有 {empty} 行『依据』为空（标签会退化成形容词）")
                else:
                    r.ok(f"证据链依据列非空（{len(data)} 行）")

    # 5) 副作用章节：要么明确声明"无写操作"，要么写到"能据此清理"的程度
    #    判据：必须出现清理动作（清除/重置/删除/披露）或明确列出记录 id
    se = find_section(secs, ("副作用",))
    if se is not None:
        body_se = se.strip()
        if NO_WRITE.search(body_se):
            r.ok("副作用章节已声明：本轮无写操作")
        elif CLEANUP.search(body_se) or re.search(r"\bid\s*[=:]\s*\d+", body_se):
            r.ok("副作用章节写到了可清理的程度")
        else:
            r.err("副作用章节既没声明『无写操作』，也没写到清理方式"
                  "（需要：记录 id + 清除/重置/删除/披露）")

    # 6) 凭据
    hit = []
    for name, pat in SECRET_PATTERNS:
        if pat.search(text):
            hit.append(name)
    if hit:
        r.err(f"报告里有疑似凭据（只报类型不回显值）：{hit}")
    else:
        r.ok("未见凭据形态")

    # 7) 偏移纪律
    bad_off = []
    for ln in text.splitlines():
        if BARE_ADDR.search(ln) and not OFFSET_BASIS.search(ln):
            bad_off.append(ln.strip()[:70])
    if bad_off:
        r.warn(f"有 {len(bad_off)} 行裸地址没写清基准（例：{bad_off[0]}）")

    # 8) 运行时证据可复现
    if "runtime-hit" in text and not RUNTIME_ANNOT.search(text):
        r.warn("出现 runtime-hit 但没看到 PID / ABI / 时间标注（不可复现）")
    return r


def show(r: Report, strict: bool) -> int:
    for m in r.oks:
        print(f"  \033[32mOK\033[0m    {m}")
    for m in r.warns:
        print(f"  \033[33mWARN\033[0m  {m}")
    for m in r.errors:
        print(f"  \033[31mERROR\033[0m {m}")
    print(f"\n  小计：ERROR {len(r.errors)} / WARN {len(r.warns)} / OK {len(r.oks)}")
    if r.errors:
        return 1
    return 1 if (strict and r.warns) else 0


# ------------------------------------------------------------------ 内置对照组样本
GOOD = """## 目标与范围
- 目标：com.example.app 8.5.1，主机 school-api.example.com
- 目标是什么：确认提交接口是否存在  不算什么：不构造有效记录
- 授权依据：委托方书面授权，仅读

## 证据
- 静态：`SportApiService` 注解，13 字段
- 运行时：PID 12345 / ABI x86_64 / 观测于 10-16 09:50，`invoke` 1→283
- 请求：GET /api/runRecord/detail?<他人记录> 回读
- native：libtarget.so!0x12345（load base 0x7f3a1b2c0000）

## 证据链
| 环节 | 证据 | 文件/函数/偏移 | 置信度标签 |
|---|---|---|---|
| 定址 | 响应含我的真实学期名与 id | school-api `semester/currSemester` | traffic-observed |
| 不重算 | 回读四字段与上报逐字相同 | `runRecord/detail` | causal |
| 排除 | 同法堵 A，症状未变 | —— | disproven |

## 结论
1. 提交接口存在且为 POST-only —— 标签：causal
2. 服务端不重算里程/配速 —— 标签：causal
3. root 检测不是退出原因 —— 标签：disproven

## 实验（含负向）
| 实验 | 改前 | 改了什么 | 改后 | 结论 |
|---|---|---|---|---|
| 探测 B | 只读 | 用 GET 打写接口 | 403 不支持 | 存在，POST-only |
| 堵 A | 1.4s 自杀 | 堵分支 A | 仍自杀 | disproven |

## 未解/下一步
- `status=3` 语义未知，需平台确认

## 副作用
- 无写操作（本轮全部只读）
"""

BAD = """## 结论
1. 主机是对的
2. 签名已经验证过了
3. 下一步加 token

## 证据
- 我打了几个接口，token=abcdef1234567890abcdef
- 关键偏移 0x71179 处有密钥
"""

SUBTLE = """## 目标与范围
- 目标：某 App

## 证据
- 静态：dex 里看到 saveRunPath

## 证据链
| 环节 | 证据 | 文件/函数/偏移 | 置信度标签 |
|---|---|---|---|
| 定址 |  | school-api | traffic-observed |
| 复刻 | 与样本一致 | RequestParamInterceptor | native-confirmed |

## 结论
1. 服务端不重算里程 —— 标签：causal
2. 打卡点实算 —— 没有验证

## 实验
| 实验 | 改前 | 改了什么 | 改后 | 结论 |
|---|---|---|---|---|
| 提交 | 只读 | POST 一次 | status=1 | 链路成立 |

## 未解/下一步
- 无

## 副作用
- 产生 1 条有效记录 id=r-poC，请委托方清除
"""


def selftest() -> int:
    print("=" * 74)
    print("对照组自证：好样本应当 0 ERROR；坏样本应当被抓到")
    print("=" * 74)
    fails = 0

    print("\n[样本 A · GOOD] 期望：0 ERROR")
    ra = check(GOOD)
    for m in ra.errors:
        print(f"  \033[31mERROR\033[0m {m}")
    for m in ra.warns:
        print(f"  \033[33mWARN\033[0m  {m}")
    if ra.errors:
        print("  ✗ 好样本被误判 ⇒ 脚本有假阳性")
        fails += 1
    else:
        print("  ✓ 通过（无误报）")

    print("\n[样本 B · BAD] 期望：抓到 缺章节/无标签/凭据 等")
    rb = check(BAD)
    for m in rb.errors:
        print(f"  \033[31mERROR\033[0m {m}")
    if len(rb.errors) < 3:
        print(f"  ✗ 只抓到 {len(rb.errors)} 条，判据太弱")
        fails += 1
    else:
        print(f"  ✓ 抓到 {len(rb.errors)} 条")

    print("\n[样本 C · SUBTLE] 期望：抓到『结论缺标签』与『依据为空』")
    rc = check(SUBTLE)
    for m in rc.errors:
        print(f"  \033[31mERROR\033[0m {m}")
    blob = " ".join(rc.errors)
    need = ("缺置信度标签", "『依据』为空")
    miss = [n for n in need if n not in blob]
    if miss:
        print(f"  ✗ 漏抓：{miss}")
        fails += 1
    else:
        print("  ✓ 两个隐蔽问题都抓到了")

    print("\n" + "=" * 74)
    print("自证结果：" + ("全部通过 ✅" if fails == 0 else f"有 {fails} 项未通过 ❌"))
    return 0 if fails == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", nargs="?", help="报告文件（markdown）")
    ap.add_argument("--strict", action="store_true", help="WARN 也算失败")
    ap.add_argument("--selftest", action="store_true", help="用内置样本自证")
    args = ap.parse_args()

    if args.selftest or not args.report:
        return selftest()

    p = Path(args.report)
    if not p.is_file():
        print(f"读不到文件：{p}")
        return 2
    print(f"检查：{p}\n")
    return show(check(p.read_text(encoding="utf-8")), args.strict)


if __name__ == "__main__":
    sys.exit(main())
