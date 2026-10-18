# 顶端新闻校园跑协议工程

<p align="center">
  <b>Cn.Dahebao · Android Protocol Analysis & Reverse Engineering</b>
</p>

<p align="center">
  面向「顶端新闻」Android 客户端内「校园跑」模块的协议分析与逆向研究。
</p>

---

## 项目简介

本项目面向 **顶端新闻 Android 客户端（包名 `cn.dahebao`，版本 8.5.1）** 中的「校园跑」模块，
主要用于 Android 逆向、网络协议分析及相关工程化研究。

通过对客户端的：

```text
静态分析
    +
脱壳与动态调试
    +
HTTPS 流量分析
    +
请求 / 响应字段对比
    +
服务端判定逻辑还原
```

逐步还原该模块的网络通信逻辑，并将协议能力重新实现为可独立运行、便于维护和分析的 Python 工具。

项目目前主要包含两部分：

```text
Protocol CLI
+
判据与方法论文档
```

协议实现按 **App 客户端版本独立维护**，避免不同版本之间的协议差异互相污染。
当前仓库对应 **8.5.1** 一个版本。

---

## 项目结构

```text
cn.dahebao/
│
├── submission/                        协议实现与提交链路
│   ├── cli.py                         统一入口（子命令见「功能」）
│   ├── page_login.py                  官方 H5 登录取 token
│   ├── campus_sign.py                 原生网关签名（AES-192-CBC + MD5）
│   ├── login.py                       两套鉴权形状的 HTTP 公共库
│   ├── policy.py                      判定规则与地理约束
│   ├── identity.py                    设备身份与锚点
│   ├── trajectory.py                  轨迹与统计量构造
│   ├── selfcheck.py                   提交前自查
│   ├── submit_run.py                  提交编排
│   ├── geo_full.json                  围栏多边形与必打点
│   ├── policy_out.json                判定规则快照
│   └── 样例-trajectory_out.json        样例产物
│
├── recon/                             分析工具
│   ├── campus_reverse.py              端点探测
│   ├── campus_test.py                 越权只读扫描
│   ├── campus_client.py               网关 HTTP 客户端
│   ├── h5_campus_probe.py             单页取证
│   ├── h5_campus_walk.py              逐路由驱动官方 H5
│   └── probe_save_route.py            路由存在性三例对照
│
├── docs/                              方法论文档
│
├── tools/                             通用工具
│   ├── check_report.py                报告自检器
│   ├── probe_endpoint_exists.py       端点存在性探测（通用版）
│   └── h5_walk.py                     驱动任意 H5 抄请求
│
├── skill/                             判据与验收技能（SKILL.md + references/）
│
├── .gitignore
├── requirements.txt
├── LICENSE
└── README.md
```

---

## 功能

| 模块 | 功能 |
|---|---|
| `cli.py` | 统一入口：`env` / `login` / `policy` / `plan` / `check` / `submit` / `status` / `all` |
| `page_login.py` | 驱动官方 H5 登录页并取得 token |
| `campus_sign.py` | 原生网关签名复现，并附真实样本自证 |
| `login.py` | 原生 AES 与 H5 md5 两套请求形状的公共库 |
| `policy.py` | 拉取判定规则、围栏与打卡点 |
| `identity.py` | 设备身份持久化与锚点漂移 |
| `trajectory.py` | 生成自洽的轨迹与统计量（纯本地） |
| `selfcheck.py` | 提交前自查规则引擎 |
| `submit_run.py` | 提交编排、台账幂等与提交后回读 |
| `campus_reverse.py` | 端点全量探测（方法 / 参数 / 返回模型） |
| `campus_test.py` | 越权只读扫描 |
| `campus_client.py` | 网关 HTTP 客户端（两套签名） |
| `h5_campus_probe.py` | 注入 token 观察官方页面自身请求 |
| `h5_campus_walk.py` | 逐路由驱动官方 H5，记录真实请求 |
| `probe_save_route.py` | 路由存在性判定（三例对照，零副作用） |
| `check_report.py` | 报告自检器，自带对照组 |
| `probe_endpoint_exists.py` | 通用端点存在性探测 |
| `h5_walk.py` | 驱动任意 H5 抄真实请求 |

具体文件及功能请以对应目录中的代码为准。

---

## 设计思路

项目并不是简单保存某个 HTTP 请求，而是把客户端网络协议拆分为可维护的工程结构：

```text
                     cli.py（统一入口）
                            │
        ┌───────────┬───────┴───────┬───────────┐
        ▼           ▼               ▼           ▼
     login       policy        trajectory    submit_run
        │           │               │           │
        └───────────┴───────┬───────┴───────────┘
                            │
                    login / campus_sign
                            │
                   identity / selfcheck
                            │
                        Network
```

各步骤都可以单独运行，`cli.py` 只是把同一批动作收进一个入口。

上层入口尽量只处理业务：

```text
一个功能
   ↓
一个脚本
   ↓
一个清晰入口
```

底层公共能力统一负责：

```text
两套签名的构造与校验
请求编码与传输
设备身份与会话
地理围栏与轨迹构造
提交前一致性自查
```

具体实现请以 `submission/` 与 `recon/` 中的代码为准。

---

## 环境

推荐环境：

```text
Python 3.10+
Windows / Linux / macOS
```

常用 Python 依赖：

```bash
pip install cryptography playwright
playwright install chromium
```

部分研究场景可能还需要：

```text
ADB
Android 真机 / 模拟器
apktool / jadx
Frida
```

依赖说明见 `requirements.txt`。

---

# 快速开始

## 1. Clone

```bash
git clone https://github.com/zhushenghui123/cn.dahebao.git
cd cn.dahebao/submission
```

## 2. 环境自检

```bash
python3 cli.py env
```

检查依赖与数据文件是否齐备，并列出当前可以直接运行的步骤。本命令不发起网络请求。

## 3. 登录

```bash
python3 cli.py login
```

驱动官方 H5 登录页，人工完成滑块后取得 token，写入 `.token`（权限 0600）。

`.token`、`identity.json`、`submit_ledger.json` 均在 `.gitignore` 中，不会进入版本库。

## 4. 判定规则与地理约束

```bash
python3 cli.py policy
```

拉取判定规则、围栏多边形与打卡点，写入 `policy_out.json`。

## 5. 生成轨迹

```bash
python3 cli.py plan --km 2.5 --pace 6.2 --checkin
```

生成一份在服务端校验下自洽的轨迹与统计量，输出 `trajectory_out.json`。本步骤只生成本地数据，不发起请求。

仓库内的 `样例-trajectory_out.json` 就是由固定参数生成的，可逐字节复现：

```bash
python3 trajectory.py --km 2.5 --pace 6.2 --seed 20261016 --checkin \
    --start "2026-10-16 09:12:00" --out 样例-trajectory_out.json
```

## 6. 提交前自查

```bash
python3 cli.py check 样例-trajectory_out.json
```

在发请求之前做本地一致性检查。硬否决不通过则不建议继续。

## 7. 提交

```bash
python3 cli.py submit 样例-trajectory_out.json
```

默认 **dry-run**，只做构包与预检，不发起写请求。

确需真实提交时：

```bash
python3 cli.py submit 样例-trajectory_out.json --live --i-understand-this-writes
```

## 8. 台账与回读

```bash
python3 cli.py status
```

查看提交台账。提交成功后 `submit_run.py` 会回读记录，确认业务侧是否真的生效。

## 9. 分析工具

```bash
cd ../recon
python3 campus_reverse.py       # 端点探测
python3 h5_campus_walk.py       # 逐路由驱动官方 H5
python3 probe_save_route.py     # 路由存在性对照
python3 campus_test.py          # 越权只读扫描
```

---

# 研究结论

本次分析共记录 **3 个 P0、2 个 P1、2 个 P2** 与 1 条负向结论，明细与复现方式见 `docs/09`：

```text
F-01  runRecord/detail 水平越权，可跨校读取他人完整轨迹        P0
F-02  applyFor/detail 水平越权，可读他人请假事由与附件          P0
F-07  trajectoryUrl 无来源校验，兼 SSRF 面                     P0
F-03  提交时服务端不重算客户端自报的里程/时长/配速/步数          P1
F-08  打卡点随机化下发不能防绕过                                P1
F-04  排行榜全量下发学生姓名/头像，教师名册一次全量              P2
F-05  campus/detail、fence/detail 未按学校隔离                  P2
F-06  教师端 6 个端点正确拒绝学生 token（负向结论）              —
```

服务端已做对的一点：打卡点是否达标由服务端从轨迹自行重算，不采信客户端上报的 `checkInNum`。

---

# 文档

```text
docs/01  方法论总览           docs/06  复盘与教训
docs/02  加固识别与脱壳验收     docs/07  对标与范式迁移
docs/03  协议与鉴权           docs/08  接口契约与数据模型
docs/04  服务端判定与缺陷类型   docs/09  测试报告与缺陷明细
docs/05  证据与判据纪律
```

`skill/` 是一份可复用的判据与验收技能（`SKILL.md` + 6 篇 references），可直接放入 Codex / DSH / Claude 等技能目录使用。

---

# HTTPS 流量分析

`recon/h5_campus_walk.py` 通过 Playwright 驱动官方 H5 页面，记录页面自身发出的请求。
相比手工构造请求，这样可以先确认：

```text
Endpoint
Request Parameters
Headers
Signature
Encryption
Session
```

再决定哪些能力需要实现。抓包产物在 `.gitignore` 中排除。

---

# 隐私与数据安全

请 **不要** 将真实用户敏感信息提交到公开仓库，包括但不限于：

```text
手机号
密码
Token
Cookie
Session
Device ID
Android ID
IMEI / OAID
真实姓名
学号
真实 GPS 轨迹
个人运动数据
```

运行过程中生成的账号、Session、设备参数及调试信息应只保存在本地。
提交 Issue、Pull Request、日志或截图之前，请自行检查并删除其中的个人信息、认证信息与设备标识。

本仓库中保留的设备标识为 `campus_sign.py` 中签名自证样本携带的 **2 个 deviceId**（分布在 3 条样本里）。
两者均来自同一型号的模拟器（`google sdk_gphone64_x86_64`），不是任何真实用户的设备。
保留的原因是这三条样本为 App 自行签发的真实 URL，是签名算法的自证依据，替换后即失去证据意义。
其余个人字段（姓名、学号、用户主键）均已脱敏。

---

# Issue / Pull Request

欢迎提交：

```text
Bug Report
Protocol Change
Version Compatibility
Code Improvement
Documentation
Reverse Engineering Research
```

如果客户端更新导致协议失效，建议 Issue 中至少提供：

```text
App Version
Android Version
Python Version
Error Message
Minimal Reproduction
```

请不要在 Issue 中公开账号、密码、Token、Cookie、完整 Session 或个人身份信息。

---

# 免责声明与使用限制

本项目主要用于：

```text
Android Reverse Engineering
Network Protocol Analysis
Python Protocol Engineering
Application Security Research
Android HTTPS Traffic Analysis
```

以及本人设备、本人账号或获得明确授权环境中的安全研究与技术学习。

请遵守所在地法律法规、目标服务的相关规则及授权范围。

## 一、授权环境限制

本项目仅限在以下环境中用于技术研究：

```text
本人合法所有的设备
本人账号
本地模拟器
已获得明确授权的测试环境
```

请勿在未经授权的第三方账号、设备或系统中使用。

## 二、严禁商业接单与非法牟利

本项目为免费开源的技术研究项目。

严禁任何个人、团队、工作室或组织利用本项目，包括但不限于：

```text
源码
修改版本
衍生版本
编译产物
```

进行：

```text
商业接单
收费代跑
按次收费
包月收费
倒卖源码
倒卖程序
付费引流
建立收费群
付费教程捆绑
```

等直接或间接的商业牟利行为。

上述限制体现项目作者的使用立场。实际使用者仍须遵守适用法律法规、相关协议及许可证条款。

本项目采用 MIT 许可。上述禁令是作者的立场表达，不属于 MIT 的许可附加条件，对下游不具有许可层面的约束力；
如需真正可执行的限制，应改用非 OSI 认证的许可证。

## 三、官方联系与停更机制

本项目旨在进行客户端逆向与网络协议工程化研究，无意侵犯目标软件开发团队及相关权利人的合法权益。

本项目尊重目标软件开发团队及相关权利人的：

```text
知识产权
合法权益
正常运营
```

如果软件官方或相关权利人就项目内容提出合理的权利主张，并与本人取得联系：

```text
收到通知
    ↓
核实相关情况
    ↓
停止维护
    ↓
停止版本适配
    ↓
停止 Release 更新
    ↓
根据实际情况归档或下架仓库
```

本人将第一时间配合处理。

## 四、免责条款

### 合规使用

使用者必须自行遵守：

```text
所在国家或地区法律法规
所在高校规章制度
目标软件用户协议
实际测试环境授权范围
```

### 禁止滥用

严禁将本项目用于：

```text
未经授权访问他人账号
未经授权获取他人数据
破坏计算机信息系统
干扰目标服务器正常运行
侵犯第三方合法权益
其他违法或违规行为
```

### 风险自担

本项目按照 `AS-IS` 形式提供，不附带任何明示或暗示的担保。

使用者应自行承担因下载、安装、使用、修改或分发本项目产生的风险，包括但不限于：

```text
客户端异常
数据异常
账号限制
账号封禁
校规处分
经济损失
法律责任
```

本项目作者及贡献者不对因使用本项目产生的直接或间接损失承担责任。
上述声明不排除法律规定不得排除或限制的责任。

---

# Credits

感谢开源社区中在「跑步类 App 协议分析」方向持续公开研究的项目，其中的工程做法与分析思路为本项目提供了参照，
详见 `docs/07`。

感谢 `apktool` / `jadx` / `frida` / Playwright / cryptography 等工具的维护者。

---

# License

MIT，详见 [LICENSE](LICENSE)。
