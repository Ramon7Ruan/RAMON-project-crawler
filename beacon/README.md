# Beacon —— 内容采集工具

> 把公开的官方数据变成「可审核的内容变更候选」。
> 需求见 `../PRD.md`，排期见 `../开发计划.md`，源实测记录见 `../docs/数据源清单.md`。

## 状态

**P-C2（采集层 + 三个适配器）已完成。** 已实现：抓取纪律层、三个源适配器、健康检查、
中性产物导出。尚未实现：七层漏斗、分级闸门、候选生成、合入（P-C3 / P-C4）。

## 环境要求

| 项 | 要求 |
|---|---|
| Python | ≥ 3.11 |
| 依赖 | `httpx`、`pydantic`、`PyYAML`；测试另需 `pytest` |
| 外部命令 | **默认不需要**。仅可选传输后端 `curl` 需要系统有 `curl` |

本工具**不依赖 Node/npm，也不依赖父项目的任何代码**——可以整体搬走或嵌入别的项目。

## 安装

```bash
# 建虚拟环境（路径随意）
python3 -m venv .venv && source .venv/bin/activate

# 装依赖
pip install httpx pydantic PyYAML pytest

# 校验安装
python -m beacon.cli --version
```

## 使用

所有命令的路径都由参数传入，默认写在当前工作目录下。

### `beacon health` —— 各源探活

```bash
python -m beacon.cli health
python -m beacon.cli health --transport curl    # 换用 curl 后端
```

输出每个源的层级（L1/L2）与探活结果。**不可用的源会在 `export` 时被跳过并记入 `failures`**，
而不是让整次采集失败。

### `beacon export` —— 取数并产出中性产物

```bash
python -m beacon.cli export --out ./out
```

产出两个文件（PRD §6.4 的 `Feed`，不含任何特定 app 的语义）：

| 文件 | 内容 |
|---|---|
| `feed.jsonl` | 每行一条 Observation（JSON） |
| `feed.meta.json` | `contractVersion`、生成时间、用到的源、**失败的源**、指标清单与计数 |

> **全部源都失败时不写任何文件**，并返回非 0。这是刻意的：一个空的 `feed.jsonl`
> 会让人以为"这次没有更新"，而真相是"这次没抓到"。

### 常用参数

| 参数 | 说明 |
|---|---|
| `--config PATH` | 源配置文件（默认：包内 `config/sources.yaml`） |
| `--cache-dir PATH` | 缓存与抓取日志目录（默认：`./.beacon-cache`） |
| `--sources a,b` | 只处理这些源 |
| `--transport httpx\|curl` | HTTP 后端，默认 `httpx` |
| `--proxy URL` | 显式指定代理 |
| `--no-proxy` | 忽略环境变量里的代理，直连 |

## 关于传输后端：为什么有两个

默认用纯 Python 的 `httpx`。另有一个可选的 `curl` 后端，原因是实测发现：

> `fred.stlouisfed.org`（CloudFront）**对 Python HTTP 栈的请求响应极不稳定**——
> 默认配置、显式代理、绕过代理、`verify=False`、`Connection: close` 全部读超时；
> 而同一 URL 用 `curl` 有时 1.9 秒就能取回。同一网络下 httpx 访问 jsdelivr、
> 东方财富、中证指数都正常，所以既不是网络不通、也不是 httpx 坏了，
> 而是**那个站点与 Python 客户端之间的组合问题**。

因此保留两条路：默认纯 Python（无外部依赖、可测），遇到这类站点时用
`--transport curl`。**默认值刻意不是 curl**——把外部命令当默认会让工具在别的机器上悄悄失效。

## 目录结构

```
beacon/
├─ pyproject.toml
├─ beacon/
│  ├─ core/                 源无关层：契约、抓取纪律、传输后端
│  │  ├─ contract.py        Observation 契约（唯一内部交换格式）
│  │  ├─ fetch.py           缓存 / 限速 / 退避 / 超时 / 留痕
│  │  └─ curl_transport.py  可选的 curl 后端
│  ├─ sources/              每个源一个适配器，互不影响
│  │  ├─ base.py            适配器接口 + 健康检查
│  │  ├─ fred.py            美债收益率
│  │  ├─ eastmoney.py       中国 PMI
│  │  ├─ csindex.py         指数估值（PE）
│  │  └─ registry.py        配置 → 适配器实例
│  ├─ config/sources.yaml   源登记表（唯一来源：抓取参数都在这里）
│  └─ cli.py                health / export
└─ tests/
   ├─ fixtures/             录制的真实响应（测试不打网络）
   └─ *.py                  96 个用例
```

## 测试

```bash
python -m pytest -q
```

**全部用例不打网络**——用录制的真实响应 + 可注入的假传输。
`tests/test_independence.py` 是结构性断言（core 层禁用词扫描、不引用父项目、不依赖 Node），
它们守护的是"这个工具能否被别的 app 复用"，而不是功能本身。

## 两条硬约束（改代码前请先读）

1. **`core/` 层不得出现任何特定 app 的专有词汇**（词表见 `tests/test_independence.py`）。
   一旦出现，这个工具就永久绑死在那个 app 上。
2. **失败不产出空内容**。抓取失败必须抛异常、让整条链路停下来；
   返回 `None`/空字符串会被下游当成"数据就是 0"。

## 新增一个数据源要做什么

1. 在 `sources/` 加一个模块，实现 `fetch_raw` / `normalize` / `health_url`
2. 在 `sources/registry.py` 的 `ADAPTERS` 里登记
3. 在 `config/sources.yaml` 里补配置（`tier`、`upstream`、`base_url`、headers）
4. 在 `docs/数据源清单.md` 里记录实测结果（**未记录的源不得启用**）

`core/` 与既有 `sources/` 都不需要改 —— 这是 `tests/test_independence.py` 会验证的承诺。

---

## `beacon run` —— 日常入口

```bash
python -m beacon.cli run --content-dir /path/to/content [--out ./content-candidates]
```

它做四件事，其中只有第 4 件需要你看：

| 步骤 | 说明 |
|---|---|
| 1 取数 | 逐源抓取，**失败隔离**：一个源倒下不影响其他源，但会写进产物 |
| 2 七层漏斗 | F1 源准入 → F2 时效 → F3 结构 → F4 合理性 → F5 交叉校验 → F6 适用性白名单 → F7 分级 |
| 3 产出 | `changes.md`（人审）+ `proposals/`（仅绿/黄）+ `observations.json` + `run.meta.json` |
| 4 **你看 `changes.md`** | 每条候选：级别 + 规则编号 + 理由、改了什么（旧值→新值）、依据哪个源、校验用了什么、需要你决定什么 |

三条纪律：

* **不在映射表里的数据一律丢弃**，即使它完全正确。映射表（`beacon/config/mapping.yaml`）
  是"工具能改哪些内容"的可审计清单。
* **检查跑不了 ≠ 检查通过**。声明的校验若因数据不足无法执行，结果降黄，不静默跳过。
* **红级不产出可合入片段**。红级只在 `changes.md` 里给建议与现状对照。

> ⚠️ **当前不实现写回**（`beacon apply`）。原因见 `../docs/漏斗与分级设计.md` §7：
> 写回会让内容文件整份重排缩进，使 diff 失去审核价值。在选定方案前不实现写回——
> **一个会让 diff 变成噪音的写回，比不写回更危险。**

## 配置在哪

| 文件 | 管什么 | 改它要不要动代码 |
|---|---|---|
| `beacon/config/sources.yaml` | 源、层级、上游、超时、必需 header、HTTP 后端 | 不用 |
| `beacon/config/mapping.yaml` | 指标 → 目标 → 字段、可写白名单、校验声明、溯源文字模板 | 不用 |
| `beacon/config/thresholds.yaml` | 值域、跳变阈值、容差、发布惯例 | 不用 |
| `beacon/config/settings.yaml` | 重拉窗口、降采样、产物目录、flags 路径 | 不用 |
