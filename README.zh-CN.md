# SVWeave

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 本文档为 `README.md` 的简体中文翻译；如与英文版有出入，一律以英文版 `README.md` 为准。

**SVWeave 是一个以 SystemVerilog 为先、面向 AI 客户端和工程师的本地代码智能层。**
它先找到相关源码（仓库图），再针对某个**具名构建**验证静态语义（按你所配置的确切 filelist、宏定义、顶层模块和参数进行 slang 展开），回答一次变更会波及什么（影响分析、git diff 影响分析、重命名预览），最后才通过一条可追溯、可拒绝、可恢复的流程（候选副本、slang 验证、人工审批、compare-and-set 提交）修改代码。

它以 Python 包、本地 CLI（`svweave`）以及位于三个沙箱化后端进程之上的**一个** MCP 网关（通过 stdio 提供 14 个 `svw_*` 工具）的形式交付。它不合并上游项目，没有 Web 服务器、没有 HTTP 端点，也不做模型训练。唯一的 UI 是某个构建的静态、自包含 HTML 图，你直接从磁盘打开即可。

## 目录

1. [它能做什么](#它能做什么)
2. [架构概览](#架构概览)
3. [状态](#状态)
4. [它不是什么](#它不是什么计划-43-非目标)
5. [仓库布局](#仓库布局)
6. [安装](#安装)
7. [快速开始](#快速开始)
8. [分析工具：影响分析、diff 影响分析、重命名预览、图](#分析工具影响分析diff-影响分析重命名预览图)
9. [在 Claude Code 和 Codex 中使用 SVWeave](#在-claude-code-和-codex-中使用-svweave)
10. [一次变更如何流转](#一次变更如何流转)
11. [构建流程](#构建流程)
12. [构建标识与状态](#构建标识与状态)
13. [配置](#配置)
14. [CLI 参考](#cli-参考)
15. [MCP 工具参考](#mcp-工具参考)
16. [受控编辑工作流](#受控编辑工作流)
17. [工具链与后端](#工具链与后端)
18. [测试与资格认定](#测试与资格认定验证)
19. [安全模型](#安全模型)
20. [已知限制](#已知限制)
21. [故障排查](#故障排查)
22. [文档](#文档)
23. [贡献](#贡献)
24. [许可证](#许可证)

## 它能做什么

| 能力 | CLI | MCP 工具 | 答案的依据 |
|---|---|---|---|
| 构建：解析 filelist、捕获不可变快照、用 slang 展开 | `svweave build` | `svw_build`、`svw_job` | 具名构建（`build_id`） |
| 查找候选文件 | `svweave query search` | `svw_search` | 仓库图或字面文本（只是候选，绝非语义真相） |
| 声明、引用、实例层次、端口连接、SVA 清单、诊断 | `svweave query symbol / references / hierarchy / connections / assertions / diagnostics` | `svw_symbol`、`svw_references`、`svw_hierarchy`、`svw_connections`、`svw_assertions` | slang，针对该构建 |
| 对模块、端口、信号或实例的一次变更会波及什么 | `svweave query impact` | `svw_impact` | 该构建的实例树和已解析的引用 |
| 一个 git diff 会波及什么 | `svweave query diff-impact` | `svw_diff_impact` | 映射到该构建快照上的 `git diff` |
| 一次重命名会做的每一处编辑，以及阻断项 | `svweave query rename-preview` | `svw_rename_preview` | 已解析的引用加上经过校验的词法扫描（只读） |
| 可浏览的层次结构及其连接图 | `svweave graph export` | （仅 CLI） | 该构建，写成一个静态 HTML 文件 |
| 安全地修改代码 | `svweave edit ...` | `svw_edit`（不能审批） | 候选副本、slang 验证、人工审批、compare-and-set |
| 状态、证据、报告 | `svweave doctor`、`svweave query evidence`、`svweave report` | `svw_status`、`svw_evidence` | 已记录的运行；EDA、仿真和形式验证保持 `NOT_RUN` |

每个答案都是一个 JSON 信封，携带构建 id、快照 id、`freshness`（`CURRENT`、`STALE`、`HISTORICAL`）、`analysis_coverage`（完整性、限制）以及溯源信息。`PARTIAL`、`STALE` 或被截断的答案从不声称某样东西不存在。

## 架构概览

<p align="center"><img src="docs/images/architecture.svg" alt="架构图：人工使用的 CLI 和 MCP 客户端接入同一个 SVWeave 网关（cli.py、server.py、带有 14 个 svw_* 工具的 gateway/tools.py、application/services.py、策略、作业管理器、溯源与错误映射）。三个适配器分别驱动 codebase-memory-mcp v0.11.0、带 Verible 的 Serena v1.7.0 以及 pyslang 12.0.0 worker，每个都运行在无网络、且未挂载原始源码的 bwrap 沙箱内。快照、候选工作区和 SQLite 数据库位于 .svweave/；策略和密钥位于仓库之外的受信目录。只有编辑协调器在审批和 compare-and-set 检查之后才会写入用户源码工作区。" width="100%"></p>

*客户端只与一个网关通信。网关掌管策略、作业、溯源和错误映射，并通过适配器驱动三个后端。每个后端都运行在 `bwrap` 沙箱中，网络关闭，且原始源码不以可写方式挂载（或根本不挂载）；pyslang worker 同样处于沙箱中。后端读取不可变快照，或写入 `.svweave/` 下的候选副本。受信目录（策略、审批 HMAC 密钥、游标密钥）位于仓库之外。用户的源码工作区只有一条访问路径：编辑协调器经过审批、compare-and-set 的写入（TB4）。虚线是信任边界 TB1 至 TB4。*

## 状态

**开发者预览版 v0.1.0-dev。UNQUALIFIED**，除非资格认定报告（`.svweave/qualification/report.json`，由 `scripts/qualify.py` 生成，证据位于 `evidence/`）显示 `ACCEPTED_STATIC_PREVIEW`。工具的“静态预览”状态绝不代表仿真、形式验证或 EDA 结果：`eda_compile`、`simulation` 和 `formal` 均为 `NOT_RUN`，除非你导入自己的证据；而导入的证据是 `EXTERNAL_DECLARED`，从不由 SVWeave 验证。依赖任何结果之前，请先阅读 `docs/limitations.md`。

**最近一次完整测试运行（2026-10-20，Linux x86_64，仅限此主机）：** 用文档所述的离线流程（`vendor/`，含 Serena）安装的一份全新副本，以 `SVWEAVE_REQUIRE_BACKENDS=1` 运行：**1066 通过，1 跳过，0 失败**；`ruff`、`mypy` 和 `svweave schema emit --check` 均无问题。它覆盖了影响分析、diff 影响分析、重命名预览和图导出工具，以及 2026-10-19/20 两轮评审的修复（见 `CHANGELOG.md`）。

**最近一次记录的资格认定运行（2026-10-14，git HEAD `b6ba45c` 加未提交的改动；上述工具加入后未重新运行）：**

| 项目 | 结果 |
|---|---|
| 门禁 | G0 `PASS_WITH_OWNER_WAIVER_OPEN`，G1 至 G5 `PASS` |
| P0 测试（通过 / 失败 / 阻塞 / 跳过） | 920 / 0 / 0 / 0 |
| 关键变异 | 22 个中 22 个被杀死，无存活 |
| SVWeave 自身代码的覆盖率 | 93.58% 语句 / 87.71% 分支（软件质量保证，不是 RTL 覆盖率） |
| EDA 编译、仿真、形式验证 | `NOT_RUN` |
| 发布决定 | **BLOCKED**，仅因三项未完成的负责人签核：`g0_waiver`、`adr_012`、`license_review` |

G0 不是单纯的 PASS，因为 codebase-memory-mcp 和 Serena 的探测结果为 `DEGRADED`（在声明的子集之内），且负责人尚未签署豁免。证据副本位于 `evidence/qualification/`。这是某一次运行的快照，不是长期有效的声明；要了解你自己代码树的状态，请重新运行 `scripts/qualify.py`。

**在真实 SoC 上试用**（2026-10-20，不属于测试套件）：一个基于 Arm SSE-315 的设计，包含 3835 个 HDL 文件（135 MiB），用时 606 s 构建完成（worker 峰值 11 GB），得到 115,552 个实例和 605,874 个端口关联；结果如实地为 `PARTIAL`（门级 PHY 模型中的编译错误、未解析的引用、6637 个空模块体）。这次运行发现并修复了 `CHANGELOG.md` 中列出的实例数组 id 冲突和信封过大问题。

| 后端（在 `toolchain.lock.json` 中锁定） | 角色 |
|---|---|
| pyslang 12.0.0（独立 worker 进程） | 感知构建的符号、引用、层次结构、端口关联、SVA 清单 |
| codebase-memory-mcp v0.11.0 | 仓库图：候选文件（绝不作为语义真相） |
| Serena v1.7.0 + Verible | 符号导航，以及在候选副本中编辑函数体 |

## 它不是什么（计划 4.3 非目标）

不是仿真器、形式验证工具，也不是 lint/签核工具；不做位级连通性、时钟/复位域、CDC/RDC、时序或功耗分析；不恢复加密源码；不静态解析 UVM factory 覆盖、`config_db` 命中或运行时拓扑；不做 SVA 证明、覆盖率收敛或签核结论；不支持 VHDL、AMS 或混合语言语义；不是多租户服务，没有远程 HTTP 端点，也不提供托管的 Web UI（只有静态图导出，ADR-014）；没有增量编译（每次构建都是完整重建）；仅支持 Linux。影响分析的计数只是计数，不是风险评分；重命名预览不等于重命名（应用重命名要经过编辑流程和人工）。

## 仓库布局

```text
SVWeave/
  svweave.yaml, toolchain.lock.json, capability_report.json, license_manifest.json, pyproject.toml
  src/svweave/
    cli.py, server.py        click CLI and MCP stdio server (both thin)
    gateway/                 MCP schemas, tool registry (14 svw_* tools), validation, dispatch
    application/             use cases shared by CLI and MCP: build, queries, search, edit, jobs,
                             impact, diff_impact, rename, graph (+ template), evidence, status,
                             report, doctor, debug bundle, init, keys
    domain/                  ids, states, error catalog + exit codes, envelope, cursor, locations
    config/                  svweave.yaml / svweave.local.yaml / policy.json models, loader, trusted dir
    manifest/                profile resolver, filelist lexer, argument ledger, slang argv builder
    snapshot/                immutable snapshot capture, inventory and identity
    adapters/slang/          pyslang worker, extraction, ingest (never loaded in the gateway)
    adapters/serena/         Serena MCP client, candidate-bound edits
    adapters/codebase_memory/  graph MCP client and search
    adapters/mcp_client.py   shared upstream MCP client
    orchestration/           backend registry, health, capability comparison, doctor checks
    editing/                 plan, candidate, diffing, approval, journal, store, state machine
    security/                bwrap sandbox, redaction, safe IO
    storage/                 SQLite, migrations, workspace lock
  schemas/                   generated JSON Schemas (manifest, local, policy, edit, evidence,
                             toolchain, mcp/<tool>.input|output.json)
  vendor/                    offline install package: locked PyPI wheels, [dev] wheels, Serena,
                             standalone CPython 3.12, SHA256SUMS (see vendor/README.md)
  fixtures/                  demo, uvm, language, filelists, negative, expected
  tests/                     unit, contract, integration, fault_injection, acceptance
  scripts/                   bootstrap_locked, probe_backends, qualify, benchmark, gen_corpus,
                             mutation_suite, gateway_lock_refresh (+ spikes/)
  docs/                      architecture, manifest, mcp_contract, operations, editing, security, ...
  evidence/                  qualification/, bench/, mutation/, m0/, dep01/ (recorded run evidence)
  .github/workflows/ci.yml   CI: fast job on PRs; real-backend job by manual dispatch only
```

| 目录 | 内容 |
|---|---|
| `fixtures/demo` | 演示设计：`rtl/`、`include/`、`sva/`，以及 profile `rtl_demo` 使用的 filelist `filelists/rtl.f` |
| `fixtures/uvm` | 小型 UVM 包和顶层（profile `uvm_smoke`；需要外部 `uvm` 根） |
| `fixtures/language` | 每个语言特性用例一个目录（作用域、ifdef、端口、接口、转义标识符、UTF-8、宏、受保护信封、stub 等） |
| `fixtures/filelists`、`fixtures/negative` | filelist 方言用例和刻意破坏的输入（循环、缺失文件） |
| `fixtures/expected` | 手写的黄金期望值（见其 README；绝不由 SVWeave 输出生成） |
| `tests/unit` | 使用 fake 的快速测试；`tests/contract` 为 MCP schema、stdio 服务器、CLI 一致性 |
| `tests/integration` | 真实的 pyslang worker、codebase-memory-mcp、Serena、沙箱、git（`integration` 标记） |
| `tests/fault_injection` | 编辑提交的崩溃与恢复；`tests/acceptance` 为端到端演示流程和文档检查 |
| `vendor/` | 无法访问 PyPI 的主机所需的一切；托管在 GitHub 上的后端由安装程序下载 |
| `evidence/` | 上述状态所引用的资格认定、基准测试和变异结果的副本 |

## 安装

所有组件都锁定在 `toolchain.lock.json` 中；安装程序（`scripts/bootstrap_locked.py`）是唯一使用网络的代码，它在使用每个 artifact 之前都会依据锁文件校验其 sha256，任何不匹配都会中止。完整说明：`docs/install.md`；已提交的离线包：`vendor/README.md`。

### 要求

* **Linux x86_64**（锁文件中唯一的平台）。其他平台未经资格认定。
* **`bwrap`**（bubblewrap），且已启用非特权用户命名空间；每个后端都在其中运行。检查：`bwrap --version && bwrap --ro-bind / / true`。没有它，后端无法运行（或只能在明确标记的降级模式下运行，见 `docs/security.md` 第 3 节）。
* **`git`**（只用一次，用于获取锁定的 Serena commit）和 **`tar`**。
* **Python 3.11 或更新版本**，用于运行安装程序。venv 本身是 **Python 3.12**：`PATH` 上要有 `python3.12`，或用 `--python PATH` 指定一个 3.12 解释器（`vendor/` 包附带一个独立的解释器）。
* **不需要 `uv`。** 安装程序会下载锁定版本的 uv release（0.12.23，锁文件的 `uv` 节），校验后解压到 `.toolchain/uv/`，之后每一条 uv 命令都只运行那个二进制文件；网关 venv 也由它自行创建（`--create-venv`）。除非你传入 `--uv PATH`（会被报告为未经校验），否则绝不会使用主机上已安装的 uv。
* RAM：HDL worker 受 `analysis.max_worker_memory_mib`（默认 8192）限制。磁盘：由 `--fetch-only` 填充的 artifact 缓存约 200 MB；`vendor/` 中 Serena 另占 35 MB、uv tarball 20 MB。

### 安装 SVWeave

所有命令都在仓库根目录运行。按你的主机网络状况选择其中一条路径。

<p align="center"><img src="docs/images/install-flow.svg" alt="安装流程。上方三条路径：(a) 在线，python3.12 scripts/bootstrap_locked.py --create-venv，安装程序下载锁文件中的每个 artifact 并校验其 sha256，可选的开发工具 wheel 取自 vendor/downloads/wheelhouse-dev；(b) 能访问 GitHub 但不能访问 PyPI，先用 (cd vendor &amp;&amp; sha256sum -c SHA256SUMS) 检查 vendor/，把 uv-cache.tar.gz、serena-src.bundle 和 CPython 3.12.15 压缩包解压到 .toolchain/state/uv-cache、.toolchain/serena-src 和 .toolchain/runtime/python，再以 --artifact-dir vendor/downloads --create-venv --python .toolchain/runtime/python/bin/python3 运行安装程序，slang、Verible、codebase-memory-mcp 和 uvm-core 仍从 GitHub 下载并校验；(c) 完全离线，在联网主机上用 --fetch-only 填好 .toolchain/downloads、serena-src 和 state/uv-cache，连同 .toolchain/ 复制整个仓库，再以 --offline --create-venv --venv-dir .venv 在无网络下从缓存安装。三条路径都进入由 toolchain.lock.json 驱动的 scripts/bootstrap_locked.py，依次执行四个步骤：1 锁定版本的 uv 0.12.23，压缩包的 sha256 在打开前检查，解压后的可执行文件再检查一次，之后每条 uv 命令都用它，并设置 UV_PYTHON_DOWNLOADS=never（--uv PATH 会被标为未校验）；2 后端压缩包 slang v12.0、Verible、codebase-memory-mcp v0.11.0 portable 和 uvm-core 2020.3.1，解压前检查、拒绝路径穿越，可执行文件再检查一次；3 网关 venv .venv，用锁定版本的 uv 建在 python3.12 或 --python 上，以 --no-index --require-hashes 安装 31 个哈希锁定的 wheel，并把源代码树链接进去；4 Serena v1.7.0，取自锁定的 commit，检查 uv.lock 和 LICENSE 的哈希，安装 8 个哈希锁定的构建 wheel，再运行 uv sync --frozen --no-dev --no-build-isolation。成功为 ok true、退出码 0，并生成一份 JSON 报告；失败为 ok false、退出码 2 并列出 errors，绝不跳过或替换。左下：生成的 .toolchain/，含 uv、runtime、slang、verible、cbm、uvm-core、serena-src、serena-venv、state 和 downloads，以及网关 .venv。右下：检查步骤 bootstrap_locked.py --verify-only、svweave init、svweave doctor、svweave doctor --probe 和 svweave build --profile rtl_demo。" width="100%"></p>

*不论走哪条路径，干活的都是同一个安装程序，由锁文件决定什么可以被接受：锁定版本的 uv 是第一个下载的东西，在它的哈希匹配之前不会运行任何其他东西；每个压缩包和 wheel 在打开前都会与 `toolchain.lock.json` 比对；网关 venv 和 Serena venv 都由安装程序自己创建，后者用哈希锁定的构建 wheel、不使用构建隔离来构建。路径 (b) 的区别只在文件来源（`vendor/`，因 sha256 匹配而被复用），路径 (c) 的区别只在何时使用网络（只有 `--fetch-only` 那一次）。每个步骤和参数在下文逐一说明；右下角的检查就是[验证安装](#验证安装)。*

**(a) 能访问 PyPI 和 GitHub 的主机**

```bash
python3.12 scripts/bootstrap_locked.py --create-venv         # pinned uv, .venv, pinned backends, Serena
.venv/bin/python scripts/bootstrap_locked.py --verify-only   # no network; compare installed state with the lock
# development tools (optional): hash-pinned wheels committed in vendor/, no index access
.toolchain/uv/uv pip install --python .venv/bin/python --offline --no-index \
    --find-links vendor/downloads/wheelhouse-dev --require-hashes -r vendor/downloads/dev-requirements.txt
```

`--create-venv` 依次做的事：下载并校验锁定版本的 uv（这是第一个下载项；哈希错误会在下载任何其他内容之前就停止运行），下载并校验锁文件中的每个归档（slang、Verible、codebase-memory-mcp、uvm-core）到 `.toolchain/`，用锁定版本的 uv 在 `python3.12`（或 `--python`）上创建 `.venv`，以 `--no-index --require-hashes` 把 31 个按哈希锁定的网关 wheel 安装进去，并把这个源码树链接进该 venv（一个 `.pth` 文件加上 `.venv/bin/svweave`），然后检出锁定的 Serena commit，把 8 个按哈希锁定的 build wheel 安装到全新的 Serena venv，并在其中运行 `uv sync --frozen --no-dev --no-build-isolation`。`--skip-serena` 会跳过 Serena（此时 `svweave doctor` 会报告 `backend:serena_exe FAIL`）。

**(b) 能访问 GitHub 但不能访问 PyPI 的主机**（计算集群的典型情况）使用已提交的 `vendor/` 包：31 个网关 wheel、`[dev]` wheel、8 个 Serena build wheel、Serena 的源码 bundle 和锁定的依赖缓存、锁定版本的 uv tarball，以及一个独立的 CPython 3.12。托管在 GitHub 上的归档（slang、Verible、codebase-memory-mcp、uvm-core）仍由安装程序下载，并依据锁文件校验：

```bash
(cd vendor && sha256sum -c SHA256SUMS)
mkdir -p .toolchain/state && tar -C .toolchain/state -xzf vendor/uv-cache.tar.gz   # Serena's locked deps
git clone vendor/serena-src.bundle .toolchain/serena-src                          # pinned Serena checkout
mkdir -p .toolchain/runtime && tar -C .toolchain/runtime -xzf vendor/runtime/cpython-*.tar.gz   # .toolchain/runtime/python/bin/python3 = 3.12.15
python3.11 scripts/bootstrap_locked.py --artifact-dir vendor/downloads --create-venv \
    --python .toolchain/runtime/python/bin/python3                         # venvs built on that interpreter
python3.11 scripts/bootstrap_locked.py --verify-only --artifact-dir vendor/downloads
# dev wheels (optional), no index access, with the pinned uv:
.toolchain/uv/uv pip install --python .venv/bin/python --offline --no-index --find-links vendor/downloads/wheelhouse-dev \
    --require-hashes -r vendor/downloads/dev-requirements.txt
```

`--python` 指定的是安装程序所创建的 venv 的*基础*解释器；不要传 `--venv-python .toolchain/runtime/python/bin/python3`（该选项指定的是 venv 自身的 Python，会把 wheel 直接装进随附的解释器）。安装程序会复用任何已存在且 sha256 正确的 artifact，因此 wheel 和 uv 都取自 `vendor/`，只有 GitHub 归档需要下载。[状态](#状态)中的测试运行就是用这套流程得到的（当时仍另外运行 `uv venv`，而非使用锁定版本的 uv）。

**(c) 完全离线：先在联网主机上缓存一次，再用 `--offline` 安装。** 联网主机需要 `git` 和 `python3.12`（或 `--python`），但不需要 venv：

```bash
# 1. connected host, in the repository directory: fill the cache, install nothing but the pinned uv
python3.12 scripts/bootstrap_locked.py --fetch-only
# 2. copy the whole repository directory, .toolchain/ included (.venv is not copied), to the offline host
# 3. offline host, same architecture: cached, hash-verified artifacts only
python3.12 scripts/bootstrap_locked.py --offline --create-venv --venv-dir .venv
.venv/bin/python scripts/bootstrap_locked.py --verify-only
```

`--fetch-only` 会把锁文件中的每个归档（含 uv tarball）、网关 wheelhouse 和 Serena build wheelhouse 存到 `.toolchain/downloads/`（可用 `--artifact-dir` 更改），把锁定的 Serena 检出存到 `.toolchain/serena-src`，并把存放 Serena 锁定依赖的 uv 缓存存到 `.toolchain/state/uv-cache`（`--uv-cache-dir`）。`--offline` 拒绝任何网络访问：缺失或不匹配的 artifact 都是错误，Serena 取自已有的检出（或 `--serena-source <本地 git 检出>`），网关 venv 只从 wheelhouse 安装。如果离线主机上的缓存位于别处，请传入 `--artifact-dir`、`--uv-cache-dir` 和 `--serena-source`。不使用 fetch-only 缓存的变体：先把四个 GitHub tarball（`slang-*`、`verible-*`、`codebase_memory_mcp-*`、`uvm_core-*`，联网时用 `--fetch-only --artifact-dir <dir>` 缓存一次）复制到 `vendor/downloads/`，然后运行 (b) 的 `vendor/` 步骤，并在安装那一行加上 `--offline`。

**成功的样子。** 每次运行都会打印一份 JSON 报告（也可用 `--report FILE` 写入文件），结尾为 `"ok": true`，退出码 0。失败的运行结尾为 `"ok": false`，退出码 2，原因列在 `errors` 中；不会有任何东西被静默跳过或替换。`--verify-only` 绝不使用网络，而是依据锁文件重新检查已安装的状态（锁定版本的 uv、二进制文件、wheel、Serena venv、版本）；其报告带有 `"verify_only": true`。`--uv PATH` 会加入一条 `warnings` 并标记 `components.uv.verified: false`。

### 验证安装

`svweave` 位于 venv 中：每个 shell 运行一次 `source .venv/bin/activate`，或直接调用 `.venv/bin/svweave`。在本仓库中（它随附演示用的 `svweave.yaml` 和 `fixtures/`）：

```bash
svweave init                       # .svweave/ and the trusted policy outside the repo; never overwrites
svweave doctor                     # config, policy, toolchain hashes, bwrap, client boundary
svweave doctor --probe             # starts each backend once with a real fixture query (slow)
svweave build --profile rtl_demo   # the shipped demo: exit 0
```

预期结果：`svweave doctor` 以退出码 0 结束，`outcome` 为 `OK`；`client_boundary` 始终是 `WARN`（`not qualified`，见 `docs/security.md` 第 4 节），而随附的 `svweave.yaml` 在你映射该外部根之前会给出 `root_mapping:uvm` `WARN`。任何 `FAIL` 都会使退出码为 4、`outcome` 为 `ERROR`；只有在跳过 Serena（`--skip-serena`）时，`backend:serena_exe FAIL` 才是预期的。`svweave build --profile rtl_demo` 以退出码 0 结束，带有 `"completeness": "COMPLETE"`、`"diagnostic_outcome": "NO_ERRORS"` 和 `"published": true`，并打印可用于[快速开始](#快速开始)查询的 `build_id` 和 `snapshot_id`；完整输出见[实例演练：随附的演示](#实例演练随附的演示)。

### 从较早的安装升级

按你当初使用的路径重新运行安装程序（`--create-venv` 会修复已有的 `.toolchain/` 和 `.venv`：venv 只在不存在时才创建，按哈希锁定的 wheel 会重新安装进去），然后运行 `--verify-only`。来自 `docs/install.md` 和 `CHANGELOG.md` 的两件事：

* 由 build backend 尚未锁定之前（2026-10-20 之前）的安装程序填充的 uv 缓存，可能含有用未锁定 backend 构建的 `proxy-tools` 或 `serena-agent` wheel，而 uv 会复用这类已构建的 wheel。升级后请删除 `.toolchain/state/uv-cache`（或你的 `--uv-cache-dir`）一次。在 `vendor/` 路径上，之后请重新解压 `vendor/uv-cache.tar.gz`：它已用锁定的 backend 和锁定版本的 uv 重新生成（由其他 uv 版本写入的缓存使用不同的缓存 bucket，离线时无法使用）。
* 文档所述的流程不再以 `uv venv` 开头；先前以该方式创建的 `.venv` 会被保留，并由 `--create-venv` 复用。

### 连接你的设计项目与 AI 客户端

这些步骤在**目标**项目（你想分析的设计）中运行，由工程师或负责安装的 AI 执行；为 AI 编写的相同步骤位于 `docs/agents/svweave-mcp-specialist.md` 的"Installing into a project"一节。下文中，`/ABS/PATH` 是这个 SVWeave 检出（因此 `/ABS/PATH/.venv/bin/svweave` 就是可执行文件），`/ABS/PATH/TO/PROJECT` 是目标项目的根目录，即存放其 `svweave.yaml` 的目录。每一步都是幂等的。

<p align="center"><img src="docs/images/ai-integration.svg" alt="接入设计项目与 AI 客户端。上方：从 SVWeave 检出有三样东西进入目标设计项目：(i) 以 svweave 名称注册的 MCP 服务器，命令为 claude mcp add svweave -- /ABS/PATH/.venv/bin/svweave --root /ABS/PATH/TO/PROJECT serve --transport stdio，或以 -s project 写入 .mcp.json，或 Codex 的 [mcp_servers.svweave] 表，只能有一个条目；(ii) 专用智能体 .claude/agents/svweave-mcp.md，name 为 svweave-mcp，复制自 docs/agents/svweave-mcp-specialist.md；(iii) docs/agents/AGENTS.snippet.md 中位于 svweave:begin 和 svweave:end 标记之间的区块，插入 AGENTS.md 或 CLAUDE.md：已有区块就替换，否则追加一次。步骤 1（svweave.yaml 工作区、外部根、第一次构建）最先做，步骤 5 检查 /mcp 显示 svweave 已连接，且 svw_status 返回 OK。中间：运行时，主智能体（Claude Code 或 Codex）读取 AGENTS.md 区块，把 HDL 结构问题交给 svweave-mcp 专用智能体；它通过每个客户端各一个的网关进程，以 MCP stdio 调用 14 个 mcp__svweave__svw_* 工具；每次调用返回一个 JSON 信封，含 build_id、snapshot_id、freshness 和 analysis_coverage。专用智能体的工作流程：svw_status、返回 job_id 的 svw_build、轮询 svw_job 直到 SUCCEEDED、以 build_id 查询，以及可选的 svw_edit。下方：启用编辑时，智能体可以读取、构建，以及准备、验证或中止编辑；它永远不能审批：提交前必须由人在真正的终端上运行 svweave edit approve。" width="100%"></p>

*把 SVWeave 安装到一个项目，会在那里留下三样东西，全都以 `svweave` 这个名称关联在一起：MCP 注册（`--root` 把该网关绑定到单个项目）、专用智能体和路由区块。正是这个区块让项目的主智能体把 SystemVerilog 结构问题交给专用智能体，而不是自己 grep；专用智能体则先构建、再查询那次构建，提出编辑时停在 `validate`，因为审批只存在于人的终端上。这些客户端配置都不在 SVWeave 测试套件的范围内（见[在 Claude Code 和 Codex 中使用 SVWeave](#在-claude-code-和-codex-中使用-svweave)）。*

**1. 把 SVWeave 指向项目。** SVWeave 不必位于设计之内：把 `svweave.yaml` 放在一个工作区目录中，并把设计作为**外部根**挂接进来（在 `svweave.yaml` 中声明时不带 `path`，在 `svweave.local.yaml` 中映射到绝对路径，并把该路径加入受信 `policy.json` 的 `allowed_roots`）：`docs/manifest.md` 第 2b 节有确切的 YAML，以及与仿真器命令行的差异（严格的 filelist、不接受仿真器选项、`timescale`、多个顶层、加密 IP、UVM）。在该工作区中：

```bash
svweave init                             # svweave.yaml template, .svweave/, policy.json in the trusted dir
$EDITOR svweave.yaml                     # profiles, filelists, tops, defines: docs/manifest.md (2b for external roots)
svweave manifest resolve --profile P     # review the argument ledger by hand
svweave build --profile P                # exit 0 and COMPLETE, or read the diagnostics
```

受信策略创建在仓库**之外**（`$XDG_CONFIG_HOME/svweave/workspaces/<key>/policy.json`，见 `docs/security.md`）；在你于该处启用之前，编辑功能保持禁用（`docs/editing.md`）。

**2. 注册 MCP 服务器**，名称为 `svweave`（专用智能体的 `mcp__svweave__*` 工具名以此为前提），并使用绝对路径，因为客户端会从它自己的工作目录启动该进程。Claude Code，在目标项目内运行：

```bash
claude mcp add svweave -- /ABS/PATH/.venv/bin/svweave --root /ABS/PATH/TO/PROJECT serve --transport stdio
```

默认作用域为 `local`；`-s project` 会在项目根目录写入等价、可共享的 `.mcp.json`：

```json
{
  "mcpServers": {
    "svweave": {
      "command": "/ABS/PATH/.venv/bin/svweave",
      "args": ["--root", "/ABS/PATH/TO/PROJECT", "serve", "--transport", "stdio"]
    }
  }
}
```

如果已存在 `svweave` 条目，就地更新它；绝不要添加第二个。只注册这一个服务器，不要注册原始的 codebase-memory-mcp 或 Serena 服务器。在小型主机上，把 `--no-graph` 加入 args（此时 `svw_search` 只使用字面文本回退）。Codex：`config.toml` 的表和 `codex mcp add` 形式见 [Codex](#codex)。

**3. 把专用智能体复制**到项目中（覆盖较旧的副本；不要更改其 `name: svweave-mcp`，第 4 步的区块会引用它）：

```bash
mkdir -p /ABS/PATH/TO/PROJECT/.claude/agents
cp /ABS/PATH/docs/agents/svweave-mcp-specialist.md /ABS/PATH/TO/PROJECT/.claude/agents/svweave-mcp.md
```

**4. 加入路由区块** `docs/agents/AGENTS.snippet.md` 到 `/ABS/PATH/TO/PROJECT/AGENTS.md`（如果项目把智能体规则放在 `CLAUDE.md`，则加到那里）。**正是这一步让该项目中的 AI 客户端知道这个 MCP 存在，并把 SystemVerilog 结构问题路由给 `svweave-mcp` 专用智能体**；少了它，智能体文件只是一个文件。区块由 `<!-- svweave:begin -->` 和 `<!-- svweave:end -->` 标记包住：如果标记已存在，把两者之间（含标记）的全部内容替换为新的区块；否则把整个区块追加到文件末尾。如果项目没有 `AGENTS.md`，就创建一个。绝不要删除或重排项目自己的文本。

```bash
# first insertion only (no markers yet); afterwards replace the text between the markers instead
cat /ABS/PATH/docs/agents/AGENTS.snippet.md >> /ABS/PATH/TO/PROJECT/AGENTS.md
```

**5. 检查。** 在 Claude Code 中，`/mcp` 会把 `svweave` 列为已连接（Codex 则用 `codex mcp list`），而让客户端调用 `svw_status` 会得到 `outcome` 为 `OK` 且含项目 `data.profiles[]` 的信封。如果 `svw_status` 失败，请阅读其 `error.code`（`docs/mcp_contract.md`）并修复原因；不要为了让它通过而修改项目的配置。这些客户端配置没有被 SVWeave 的测试套件覆盖；详情见[在 Claude Code 和 Codex 中使用 SVWeave](#在-claude-code-和-codex-中使用-svweave)。

### 安装故障排查

| 症状 | 原因与处理 |
|---|---|
| `bwrap --ro-bind / / true` 失败；`svweave doctor` 中 `isolation:bwrap FAIL` | 主机禁用了非特权用户命名空间；必须启用（由管理员设置）。没有它，后端无法运行（`docs/security.md` 第 3 节）。不要禁用沙箱。 |
| `Could not acquire lock`，或 `~/.cache/uv` 下的写入错误 | 这台主机上 uv 的缓存目录是只读的：把 `UV_CACHE_DIR` 设为可写目录（`UV_CACHE_DIR=/path/writable python3.12 scripts/bootstrap_locked.py ...`）。 |
| `sha256 mismatch for <url>: got ..., lock says ...`；`pinned uv is missing or differs from toolchain.lock.json; nothing installed` | 下载到的 artifact 不是锁文件所依据的那一个（上游已变更，或传输中被改动）。没有安装任何东西。不要修改哈希值，也不要用 `--uv` 绕过：请报告。 |
| 从 `files.pythonhosted.org` 下载失败（拒绝连接、超时），而 GitHub 下载正常 | 无法访问 PyPI：使用路径 (b)（`--artifact-dir vendor/downloads`），所有 wheel 和 uv 都取自 `vendor/`。 |
| `offline: <file> is not cached with the locked sha256 (would download ...)`；`offline: no --serena-source given and no existing serena checkout` | `--offline` 发现有 artifact 缺失。在联网主机上重新运行 `--fetch-only` 并把缓存（或该 tarball）复制过来；把 `--artifact-dir`、`--uv-cache-dir`、`--serena-source` 指向缓存实际所在的位置。 |
| `serena: git clone of --serena-source failed` | 该路径必须是安装程序进程可见的 git 检出（沙箱 bind mount）；它是用 `git clone --no-local file://...` 克隆的。 |
| `svweave doctor`：`backend:serena_exe FAIL`，退出码 4 | Serena 被跳过（`--skip-serena`）或其 venv 不存在：不带 `--skip-serena` 重新运行安装程序。只有在你选择跳过 Serena 时才是预期的。 |
| `--verify-only`：`gateway venv missing: nothing to verify` | `--verify-only` 在 `--create-venv` 之前运行，或针对了另一个 `--venv-dir`；加上 `--no-venv` 可只检查工具链。 |
| `codebase-memory-mcp` 出现 `GLIBC_2.38' not found` | 锁文件正是为此固定了 `linux-amd64-portable`（静态）版本；不要换成标准版本（`docs/install.md` 第 4 节）。 |
| Serena 无法启动；其 stderr 日志显示 `/usr/bin/env: '.../serena-venv/bin/serena': No such file or directory` | 这是沙箱尚未重建解释器符号链接链之前的版本（venv 建在 python-build-standalone 运行环境上，例如 `.toolchain/runtime/`）。更新 SVWeave 即可，不需要重建 venv。 |
| 失败的安装留下不完整的 `.toolchain/` | 重新运行同一条命令：已存在且 sha256 正确的 artifact 会被复用，其他的会重新下载并重新校验。卸载：删除 `.venv`、`.toolchain/`、`.svweave/`，以及（如需要）受信目录。 |

## 快速开始

```bash
# workspace and checks (this repository ships a demo svweave.yaml and fixtures)
#    `svweave` lives in the venv: `source .venv/bin/activate` first (or call .venv/bin/svweave)
svweave init                       # never overwrites; creates the trusted policy outside the repo
svweave doctor                     # includes the "client boundary not qualified" item
svweave doctor --probe             # real fixture probe of every backend (slow)

# resolve, build, query
svweave manifest resolve --profile rtl_demo      # review the argument ledger by hand
svweave build --profile rtl_demo                 # prints build_id and snapshot_id
svweave query hierarchy --build BUILD_ID
svweave query symbol --build BUILD_ID --name demo_lane
svweave query search --snapshot SNAPSHOT_ID --query demo_lane
svweave report --build BUILD_ID --format json

# what would a change touch?
svweave query impact --build BUILD_ID --name demo_lane
svweave query diff-impact --build BUILD_ID                       # your uncommitted git changes
svweave query rename-preview --build BUILD_ID --name demo_lane.valid_i --to valid_in
svweave graph export --build BUILD_ID                            # .svweave/exports/graph-<id>.html

# serve the MCP gateway for your client (stdio)
svweave serve --transport stdio
```

退出码：0 表示该命令成功（不是“设计 PASS”），2 表示用法/配置错误，3 表示构建错误、PARTIAL 或 BLOCKED，4 表示后端/资源/超时，5 表示 stale/冲突，6 表示策略/审批拒绝。

要分析位于别处的项目（你自己的 SoC），请把 `svweave.yaml` 放在单独的工作区中，并把设计作为外部根挂接进来：见 `docs/manifest.md` 第 2b 节。filelist 中不接受仿真器选项；常见情况（`timescale`、仅运行时选项）有对应的 profile 字段。

### MCP 客户端配置

SVWeave 从不修改客户端的配置。请把类似下面的条目加入你自己的客户端（按其格式调整；详情和工具列表见 `docs/operations.md` 和 `docs/mcp_contract.md`）：

```json
{
  "mcpServers": {
    "svweave": {
      "command": "/ABS/PATH/.venv/bin/svweave",
      "args": ["--root", "/ABSOLUTE/PATH/TO/PROJECT", "serve", "--transport", "stdio"]
    }
  }
}
```

只注册这一个服务器；不要同时注册原始的 codebase-memory-mcp 或 Serena 服务器。在小型主机上，把 `--no-graph` 加入 `args`（此时搜索只使用字面文本回退）。本地索引并非端到端离线：返回给云端模型客户端的代码片段会离开本机。完整的逐项目流程（工作区、注册、专用智能体、`AGENTS.md` 区块、检查）见[连接你的设计项目与 AI 客户端](#连接你的设计项目与-ai-客户端)的第 1 至 5 步。

## 分析工具：影响分析、diff 影响分析、重命名预览、图

这四个工具都读取一个构建并明确说明这一点：信封携带构建 id、完整性、新鲜度和限制，列表有上限但附带完整总数，它们报告的任何内容都不是风险评分。

<p align="center"><img src="docs/images/analysis-tools.svg" alt="分析工具。上方：一次构建，build_id 为 snapshot_id、config_id、toolchain_id 和模式版本的哈希，完整性为 COMPLETE 或 PARTIAL，新鲜度为 CURRENT、STALE 或 HISTORICAL；其中包含不可变的快照字节、在 profile 顶层之下展开的实例树、带有静态解析引用的声明，以及直接的端口关联。四列读取它。影响分析（svw_impact、svweave query impact）：用 --name、--symbol、--file 加 --byte 或 --instance 指定一个目标；对模块或接口返回每个实例、--depth 层内的依赖方、依赖项和引用，对端口返回每个连接处和依赖方，对 net、变量或参数返回已解析的引用和连接，对实例返回其祖先、子树和端口连接；名称有歧义时返回 AMBIGUOUS_SYMBOL。Git diff 影响分析（svw_diff_impact、svweave query diff-impact）：从 --base HEAD 到 --head（默认为工作树）的 git diff -U0，固定 argv、不经 shell、有大小上限；旧侧的行映射到快照字节，逐文件为 aligned 或 approximate；返回变更的声明、其模块的实例和依赖方，以及变更的端口和其连接处；构建之外的文件只列出，头文件不重新展开。重命名预览（svw_rename_preview、svweave query rename-preview），只读：声明、已解析的引用、具名连接的形式名称、结尾标签和 bind 目标，每一处都与快照字节核对；返回 diff、阻断项、警告、其他字面出现处，以及就绪时的 edit_request；遇到未解析引用、.* 和 .name 连接、宏位置、名称冲突、非 COMPLETE 且 CURRENT 的构建，以及具名参数覆盖时会阻断。图导出（svweave graph export --build B，仅限 CLI）：一个静态文件 .svweave/exports/graph-build.html，含实例树，以及在直接端口关联中使用同一个信号声明的实例之间的虚线连线；扇出超过 --max-fanout 16 的节点只列出，最多 --max-instances 5000 个、由浅到深，以 file:// 打开、严格 CSP，不是 netlist。下方：影响分析和 diff 影响分析返回 JSON 信封，重命名预览以其 edit_request 交给受控编辑流程，图导出则直接从磁盘用浏览器打开。" width="100%"></p>

*这四个工具都是同一次已存储构建的不同视角；没有一个会重新展开设计或触碰源代码树。它们的区别在于起点（一个声明或实例、一个 git diff、一个重命名目标、整棵树），以及交出去的东西：一个信封、一个只有[受控编辑工作流](#受控编辑工作流)才能应用的 `edit_request`，或一个静态 HTML 页面。每个工具都沿用该次构建的完整性和新鲜度，所以 `PARTIAL` 或 `STALE` 的构建得到的答案会如实标明；重命名预览只在 `COMPLETE`、`CURRENT` 的构建上才会报告“就绪”。*

### 影响分析（`svweave query impact`、`svw_impact`）

给出一个目标：一个声明（`--name`、`--symbol`，或 `--file` + `--byte`）或一个实例（`--instance`）。

| 目标 | 答案 |
|---|---|
| module / interface | 每个实例、实例化它的定义（`dependents`，最多 `--depth` 层）和它所实例化的定义（`dependencies`）、源码引用 |
| port | 绑定它的每个实例连接（实际表达式、信号、file:line）、其信号在更下层作为实参的使用、其所属模块的依赖方 |
| net、variable、parameter 等 | 已解析的引用以及引用它的端口连接 |
| instance | 祖先、其下方的内容、它自己的端口连接 |

一个端口和 slang 为它声明的隐式 net 视为同一个目标；其他任何歧义都会返回带候选项的 `AMBIGUOUS_SYMBOL`。依赖方来自展开后的树：一个从未在该构建顶层之下被实例化的模块，在这里没有依赖方。

### Git diff 影响分析（`svweave query diff-impact`、`svw_diff_impact`）

读取从 `--base`（默认 `HEAD`）到 `--head`（默认工作树）的 `git diff -U0`，并把旧版本（OLD 侧）的行映射到该构建的快照上：变更的声明、它们所属的模块、这些模块的实例和依赖方、变更的端口及其连接位置。请基于 base 修订版构建：快照字节与 base 版本相同的文件标记为 `aligned`，否则该映射被标记为近似。构建之外的文件只列出、不分析；被包含头文件中的变更不会重新展开到包含它的文件中。按定义，工作区自构建以来已经改变，因此 `STALE` 不改变退出码。`git` 以固定的参数列表运行（无 shell、无外部 diff 驱动、无 textconv、无 fsmonitor），修订版只能是普通 token，其输出读取时有大小上限和超时。与工作树比较时，git 会运行仓库配置的任何 clean/process filter，因此只要存在这类 filter 就拒绝该模式（请先提交再使用 `--head`）。只比较项目自己的子目录，即使外层仓库更大也是如此。

### 重命名预览（`svweave query rename-preview`、`svw_rename_preview`）

只读。列出一次重命名会做的每一处编辑，每一处都对照快照字节检查（该区间的文本必须恰好是旧名称）：声明、已解析的引用、具名端口连接的形参名（`.old(expr)`），结束该声明**自身**代码块的 `end<keyword> : old` 标签（module、function、task、class 等），以及对 module、interface 和 program 而言的 `bind old` 目标。它显示 diff、`blockers`、`warnings` 以及其他出现位置的词法列表；当没有任何阻断时，它返回一个 `svweave edit prepare --spec` 可原样接受的 `edit_request`（`--write-spec STEM` 会把它写到 `.svweave/exports/`）。应用重命名走受控编辑流程：slang 验证重命名后的候选，由人工审批。

遇到以下情况它会阻断，而不是猜测：关键字或无效标识符、同一作用域内的名称冲突、slang 未解析的引用、`.*`、`.name` 或 `.name()` 连接、写在宏体中或字节不匹配的位置、不是 `COMPLETE` 且 `CURRENT` 的构建、超过单个请求所能承载的编辑数量、在编辑集合之外对 module/interface/package 名称的使用（在 SSE-315 设计上，这捕获到了未展开模块中的真实实例化），slang 未解析的局部名称使用：位于其声明作用域内（会沿 generate 块和 function 向上找到所属模块）或位于被包含的头文件中（未激活的 `` `ifdef `` 分支、宏）、按名称指定的参数覆盖和参数绑定（`#(.old(...))`、`f(.old(x))`），以及不得不提前停止的出现位置扫描。

### 图导出（`svweave graph export`，仅 CLI）

```bash
svweave graph export --build BUILD_ID          # writes .svweave/exports/graph-<build>.html
```

在浏览器中打开该文件（`file://`，无需服务器）。它显示该构建展开后的实例树（双击折叠子树，搜索会展开到匹配项，拖动和滚轮用于平移和缩放），并以虚线显示从该构建的直接端口关联推导出的连接：两个实例引用同一个信号声明，方向由所声明的端口方向决定。一条线代表两个节点之间的所有信号；点击它可查看列表。被多于 `--max-fanout` 个实例（默认 16）使用的类时钟信号以枢纽形式列出，不绘制。页面携带该构建的完整性和新鲜度，并在其为 PARTIAL、STALE 或被截断（`--max-instances`，优先保留最浅层）时明确说明。它不是网表。页面没有网络访问（严格 CSP），并把源码文本视为数据。在上述 115k 实例的 SoC 上，导出耗时 27 s，绘制了最浅的 5000 个实例（38 MB）。设计依据：`docs/adr/ADR-014-static-graph-export.md`。

## 在 Claude Code 和 Codex 中使用 SVWeave

SVWeave 通过 stdio 使用 MCP，因此任何 MCP 客户端都可以启动它。下面两种配置遵循两个客户端的文档化语法（在作者所安装的版本 Claude Code 2.1.299、Codex CLI 0.162.1 上，`claude mcp add --help` 和 `codex mcp add --help` 接受这些形式）。**它们没有被 SVWeave 的测试套件覆盖**：测试只通过 Python MCP 客户端驱动网关，因此没有针对特定客户端的资格认定证据。SVWeave 从不修改客户端的配置；这些命令由你自己运行。

请使用绝对路径：客户端会从它自己的工作目录启动该进程。

**专用智能体定义。** `docs/agents/svweave-mcp-specialist.md` 是一份现成的 Claude Code 智能体定义（`name: svweave-mcp`），告诉 AI 如何正确使用这个网关：何时把问题路由给它、先构建再查询的工作流、覆盖全部 14 个 `svw_*` 工具的"问题对应工具"表、如何解读 `freshness`、`analysis_coverage` 和 `pagination`、编辑规则（智能体永远不能审批）以及它必须向用户说明的限制。`docs/agents/AGENTS.snippet.md` 是要加入项目 `AGENTS.md` 的对应区块，用 `<!-- svweave:begin -->` / `<!-- svweave:end -->` 标记包住，因此可以幂等地插入。把 SVWeave 安装到一个项目，意思就是：以 `svweave` 名称注册服务器（见下文）、把专用智能体复制到该项目的 `.claude/agents/svweave-mcp.md`、再把该区块加入项目的 `AGENTS.md`（或 `CLAUDE.md`）；确切步骤见专用智能体文件的"Installing into a project"一节。两个文件都放在 `docs/` 下，是因为本仓库的 `.gitignore` 排除了 `.claude/`。

### Claude Code

```bash
claude mcp add svweave -- /ABS/PATH/.venv/bin/svweave --root /ABS/PATH/TO/PROJECT serve --transport stdio
```

默认作用域为 `local`（仅你本人、仅此项目可见）。`-s project`（`--scope project`）会在项目根目录写入可通过 Git 共享的 `.mcp.json`；`-s user` 则让该服务器在你所有项目中可用。等价的 `.mcp.json`：

```json
{
  "mcpServers": {
    "svweave": {
      "command": "/ABS/PATH/.venv/bin/svweave",
      "args": ["--root", "/ABS/PATH/TO/PROJECT", "serve", "--transport", "stdio"]
    }
  }
}
```

在 Claude Code 中，`/mcp` 会显示该服务器及其连接状态。

### Codex

在 `~/.codex/config.toml` 中添加一个表：

```toml
[mcp_servers.svweave]
command = "/ABS/PATH/.venv/bin/svweave"
args = ["--root", "/ABS/PATH/TO/PROJECT", "serve", "--transport", "stdio"]
```

较新的 Codex CLI 版本也提供 `codex mcp add svweave -- /ABS/PATH/.venv/bin/svweave --root
/ABS/PATH/TO/PROJECT serve --transport stdio`，它会写入同样的表。`codex mcp list` 显示已注册的服务器。

### 同时使用两者

每个客户端都在同一个工作区上启动**自己的网关进程**。哪些共享、哪些不共享：

| 方面 | 行为 | 证据 |
|---|---|---|
| 读取 | 两个网关对同一个已发布构建的并发读查询给出完全相同的答案 | `tests/integration/test_api_parallel_reads.py`（API-PAR-03；以 `--no-graph` 运行，因此不覆盖图后端） |
| 构建 | 每个工作区同一时间只有一个构建，由跨进程的非阻塞 `build.lock` 强制保证。在同一个网关内，对**同一个** profile 的第二次 `svw_build` 会加入正在运行的作业（`already_running: true`）。其他任何情况都会以 `RESOURCE_LIMIT` 拒绝，而不是加入。你得到哪种细节取决于原因：持有 `build.lock` 的构建（另一个网关的 worker、在 shell 中运行的 `svweave build`）给出 `details.reason: build_in_progress`；同一网关中的不同 profile，或数据库中记录的另一个网关排队中或运行中的作业，给出 `details.active_job_id`（同一网关时还有 `active_profile`），但不给出原因。请稍后重试。窄竞态：如果 CLI 构建恰好在 `svw_build` 探测锁与作业启动之间拿到锁，该作业以 `FAILED` 和 `RESOURCE_LIMIT` 结束 | `src/svweave/application/jobs.py`、`src/svweave/storage/lock.py`（`build_lock`）；`tests/unit/test_m3b_fixes.py::test_B4_*`（该竞态是从代码中读出的，未经测试） |
| 编辑提交 | 由排他的工作区锁（`.svweave/lock`）串行化；如果另一个进程持有该锁（包括任何构建运行期间，因为构建以共享方式持有它），提交会退让（非阻塞） | `src/svweave/storage/lock.py`、`src/svweave/application/edit.py`（从代码读出，未跨两个网关测试） |
| 内存 | 每个网关都可能启动自己的 Serena 和图后端（它们延迟启动），因此内存占用可能成倍增加 | `src/svweave/orchestration/registry.py`；主机限制见 `AGENTS.md` |

在小型主机上，至少为一个客户端在 `args` 中加入 `--no-graph`（此时搜索使用字面文本回退）。在每个客户端中**只**注册这一个服务器；不要同时注册原始的 codebase-memory-mcp 或 Serena 服务器（计划 6.1），那样会绕过快照绑定和信封。本地索引并非端到端离线：返回给云端模型客户端的代码片段会离开本机。

### 智能体能做什么、不能做什么

| 通过网关 | 智能体 |
|---|---|
| `svw_status`、`svw_search`、`svw_symbol`、`svw_references`、`svw_hierarchy`、`svw_connections`、`svw_impact`、`svw_diff_impact`、`svw_rename_preview`、`svw_assertions`、`svw_evidence` | 可以读取 |
| `svw_build`、`svw_job` | 可以启动和轮询构建（运行时状态位于 `.svweave/` 下） |
| `svw_edit` `prepare` / `validate` / `abort` | 可以提出并验证候选，**前提是**在 `svweave.yaml` 中**并且**在受信策略中都启用了编辑（默认关闭） |
| 审批 | **不能**：没有审批工具，也没有审批字段。由人工在真实终端上运行 `svweave edit approve` |
| `svw_edit` `commit` | 若没有针对该确切补丁、快照和策略的有效审批记录，则被拒绝 |

**客户端边界。** Claude Code 和 Codex 有自己的 shell 和文件工具。这些工具可以在 SVWeave 之外修改源码树，而且以你的 UID 运行的智能体可以读取 `approval.key` 并伪造审批。SVWeave 的保证（compare-and-set、审批绑定、日志）**只覆盖通过网关进行的**操作；`svweave doctor` 始终报告 `client_boundary: not
qualified`。若要让审批具有约束力，请让智能体以单独的 UID 运行，或用操作系统限制禁止它访问源码树和受信目录；见 `docs/security.md` 第 4 节。

### 推荐的智能体工作流

1. 用 `svw_status` 查看后端健康状况，以及每个 profile 的当前构建及其新鲜度。
2. `svw_build {profile}` 立即返回一个 `job_id`。轮询 `svw_job {job_id}`，直到 `lifecycle` 为 `SUCCEEDED`（或 `FAILED`、`TIMED_OUT`、`CANCELLED`），然后读取 `data.result`：`completeness`（`COMPLETE`、`PARTIAL`、`BLOCKED`）、`diagnostic_outcome`、`freshness`、`published`。在有错误的设计上成功完成的作业仍然是 `PARTIAL`；不要把它当作干净的构建。`PARTIAL` 构建有一个可以查询的 `build_id`；在构建被存储之前就失败的 `BLOCKED` 作业（缺少输入、不支持的选项）则没有（见[构建流程](#构建流程)）。
3. 使用结果中的 `build_id` 进行查询（`svw_symbol`、`svw_references`、`svw_hierarchy` 等）。遵守 `analysis_coverage` 和 `freshness`；在 PARTIAL、STALE 或被截断的结果中缺失，并不证明不存在。
4. 可选：先 `svw_edit` `prepare`，再 `validate`，后者返回 diff 和一个 `patch_hash`。
5. 由人工审阅并在真实终端上运行 `svweave edit approve --transaction T --patch-hash H`。然后 `svweave edit commit --transaction T --patch-hash H` 应用它（同样的提交也可通过 `svw_edit` `commit` 进行，但仍需审批记录）。参数以 `svweave edit approve --help` 和 `svweave edit commit --help` 为准；完整流程见[受控编辑工作流](#受控编辑工作流)。

## 一次变更如何流转

演示流程（计划 1.3）真实调用了全部三个后端；没有任何部分被模拟的图、正则符号查找或固定 JSON 替代。步骤 1 到 5 是只读分析；步骤 6 到 11 是受控编辑流程，默认禁用。

<p align="center"><img src="docs/images/demo-flow.svg" alt="十一步流水线：加载 profile，不可变快照与构建，后端健康探测，图查找候选文件（SOURCE_CANDIDATE），slang 符号、层次结构和引用（HDL_STATIC_RESOLVED），Serena 编辑候选副本（LSP_RESOLVED），slang 重新验证候选，diff 与未关闭的限制（EDA、仿真和形式验证 NOT_RUN），在 TTY 上输入哈希前缀进行人工审批，带日志的 compare-and-set 提交，以及应用后快照与构建（旧构建变为 HISTORICAL）。" width="100%"></p>

*每一行标出执行工作的后端（实心圆点）、触发它的命令以及它产生的证据类型。图只提出候选文件；slang 针对具名构建给出答案；Serena 只编辑候选副本；在人工看到 diff 之前，slang 会再次验证基线和候选。审批、compare-and-set 提交和应用后构建都是网关步骤。证据类型不是排名（见[测试与资格认定](#测试与资格认定验证)）。*

每个事务都在一个状态机中流转，其合法转换是 `src/svweave/editing/machine.py` 中的一张表；任何其他转换都是 `INTERNAL` 错误。

<p align="center"><img src="docs/images/edit-state-machine.svg" alt="状态机：正常路径上依次为 PREPARED、CANDIDATE_READY、VALIDATING、VALIDATED、AWAITING_APPROVAL、APPROVED、COMMITTING、APPLIED、POST_APPLY_VERIFIED。APPROVED 和 COMMITTING 可以进入 RECOVERY_REQUIRED，后者解决为 APPLIED、ABORTED 或 CONFLICT。APPLIED 之前的任何状态都可以进入 ABORTED、EXPIRED、CONFLICT 或 VALIDATION_FAILED。post_apply_status 为 FAILED 的 APPLIED 保持 APPLIED。边上的门禁 A 至 G 分别表示审批 HMAC 与过期、config_id 复查、工作区 compare-and-set、逐文件修改前哈希以及日志 fsync。" width="100%"></p>

*正常路径为蓝色。当崩溃发生在日志写入之后、`COMMITTING` 被记录之前时，`APPROVED` 可能进入 `RECOVERY_REQUIRED`；`COMMITTING` 只能完成（`APPLIED`）或需要恢复。应用后检查失败会使事务保持 `APPLIED` 并带有 `post_apply_status: FAILED`，绝不会成为 `POST_APPLY_VERIFIED`。徽标 A 至 G 标出每次转换上强制执行的门禁（审批 HMAC 与过期、`config_id` 复查、工作区 compare-and-set、逐文件修改前哈希、日志 fsync）。命令演练见[受控编辑工作流](#受控编辑工作流)。*

## 构建流程

`svweave build --profile P` 和 MCP 工具 `svw_build` 运行同一个用例（`src/svweave/application/build.py`，`run_build`）。CLI 在前台运行它；网关在一个后台线程中运行它并返回一个 `job_id`。

<p align="center"><img src="docs/images/build-flow.svg" alt="svweave build 的十个阶段：加载并验证配置，将清单解析为 config_id，捕获不可变快照得到 snapshot_id，计算标识并检查可否复用，在 bwrap 内运行 pyslang worker，提取语义事实，检查依赖泄漏，导入暂存区并判定完整性，最后发布指针，报告信封。下方三个结果框：COMPLETE 且 NO_ERRORS 被存储并移动指针；PARTIAL，或因依赖泄漏导致的 BLOCKED，按 build_id 存储但不移动指针，退出码 3，而缺少输入或不支持的选项为 BLOCKED 且不存储任何内容；worker 超时、崩溃或因内存被杀不存储任何行，按错误码以 4 退出并保留旧构建。" width="100%"></p>

*阶段 5 和 6（蓝色）在沙箱化的 pyslang worker 中运行；其余部分都是网关用例。已发布指针最后写入，与构建行处于同一个事务中。*

### 分步说明

整个构建在 `.svweave/` 中一个排他、非阻塞的 `build.lock` 下运行。同一工作区中的第二个构建会立即以 `RESOURCE_LIMIT`（`details.reason: build_in_progress`）失败，退出码 4。下面的编号与图中一致。

1. **加载并验证** `svweave.yaml`、可选的本地覆盖文件（主机根映射，不入 Git）以及位于仓库之外的受信 `policy.json`。错误输入为 `INVALID_CONFIG`，退出码 2，发生在任何其他操作之前。
2. **解析清单**（`src/svweave/manifest/resolver.py`）：把 filelist、include 目录、宏定义、顶层模块和顶层参数展开成一份规范化的清单，以及一份记录每个 slang 参数来源的参数台账。其哈希就是 `config_id`。适配器无法遵从的选项会被拒绝（`UNSUPPORTED_OPTION`），绝不会被忽略。
3. **捕获不可变快照**（`src/svweave/snapshot/capture.py`），在共享的工作区锁下进行：枚举每个输入，对其字节做哈希（sha256），复制到 `.svweave/snapshots/<snapshot_id>/files`，并记录 include/库的搜索空间（这样，新增一个会改变所选头文件的文件也会改变 id）。捕获期间会重新检查工作区；如果它持续变化，构建以 `WORKSPACE_UNSTABLE`（退出码 5）停止。使用 `expected_snapshot_id`（MCP）时，不匹配会以 `STALE_SNAPSHOT` 停止。
4. **计算标识并检查可否复用。** `toolchain_id` 来自锁定文件；`build_id = SHA-256(snapshot_id : config_id : toolchain_id : schema version)`。如果已存在具有该 `build_id` 的 COMPLETE 构建且未给出 `--no-reuse`，则返回已存储的结果（`reused: true`），且仅当该构建仍为 CURRENT 时才把指针移向它。在给出 `--no-reuse` 且已存在 COMPLETE 构建时，已存储的构建不可变：worker 会再次运行，并将其行、完整性和诊断结果与已存储的进行比较。一致时返回已存储的构建并带 `verified_rerun: true`，不发布任何新内容；不一致则为 `INTERNAL`（退出码 4），且已存储的构建不会被替换。
5. **运行 pyslang worker**（`src/svweave/adapters/slang/runner.py`），在 bubblewrap 中：快照的只读视图、私有 `/tmp`、无网络、来自 `analysis.max_worker_memory_mib` 的 `RLIMIT_AS`、挂钟超时（`analysis.max_build_seconds`），以及独立的进程组，使超时或取消能杀死整棵进程树。没有 bwrap 时，构建以失败关闭（`BACKEND_UNAVAILABLE`），除非你选择进入降级模式，而降级会被报告。
6. **提取**声明、引用、实例、端口绑定、SVA 清单和诊断。worker 输出一个 JSON 文档，由网关依据 schema 验证。
7. **依赖泄漏检查。** 如果 slang 读取了快照和适配器自身文件之外的任何内容，构建即为 `BLOCKED`：判定结果被存储，所有语义行都被扣留，不发布任何内容。
8. **导入**暂存区并判定结果（`src/svweave/adapters/slang/ingest.py`）。只要有任何因素削弱了结论，构建就是 `PARTIAL`：带错误的诊断、未解析或不支持的引用、已声明的 stub 或黑盒边界、项目文件中不支持的节点、slang 未暴露的端口关联。否则为 `COMPLETE`。诊断结果（`NO_ERRORS`、`HAS_ERRORS` 等）单独报告。
9. **发布。** 构建行和计数在一个事务中写入并验证（暂存的行必须与导入的计数一致）；该 profile 的“当前构建”指针最后移动，并且**只针对 COMPLETE 构建**。`PARTIAL` 构建以及因依赖泄漏导致的 `BLOCKED` 构建会被存储，可通过 `build_id` 访问，但永远不会成为当前构建。失败的 worker（超时、崩溃、取消、因内存被杀、错误输出）不存储任何语义行；在分析之前就失败的构建（`MISSING_INPUT`、`UNSUPPORTED_OPTION`）什么都不存储，也没有 `build_id`。在每一种失败情况下，最后一个可用的构建保持为当前构建。
10. **报告。** 信封携带 `outcome`、`analysis_coverage`（完整性、范围、限制）、`freshness`、`provenance`、计数以及一个 `run_dir`（`.svweave/runs/<job_id>/`），其中包含 `argument_ledger.json`、`snapshot_inventory.json`、`dependency_report.json`、`diagnostics.json`、`semantic_summary.json`、`result.json` 等。

### 结果与退出码

| 结果 | 完整性 / 结果 | 指针 | CLI 退出码 |
|---|---|---|---|
| 干净的设计 | `COMPLETE`、`NO_ERRORS`，outcome `OK` | 移动（或已是当前） | 0 |
| 有错误的设计，或已声明的边界 | `PARTIAL`（作业仍为 `SUCCEEDED`），错误 `PARTIAL_ANALYSIS` | 不移动 | 3 |
| 依赖泄漏 | `BLOCKED`；判定结果存储在其 `build_id` 下，语义行被扣留 | 不移动 | 3 |
| 缺少输入或不支持的选项（`MISSING_INPUT`、`UNSUPPORTED_OPTION`） | outcome `BLOCKED`；不存储任何内容，没有 `build_id` | 不移动 | 3 |
| worker 超时、崩溃、错误输出、无沙箱 | 作业 `TIMED_OUT` 或 `FAILED`；构建结果的 outcome 为 `ERROR`，完整性为 `BLOCKED`；不存储任何行 | 不移动 | 4（按错误码） |
| worker 因内存被杀 | `RESOURCE_LIMIT`；以相同限制重试无济于事 | 不移动 | 4 |
| 另一个构建正在运行 | `RESOURCE_LIMIT`（细节见[同时使用两者](#同时使用两者)） | 不移动 | 4 |
| 已取消（仅 MCP） | 作业 `CANCELLED`；作业信封的顶层 `outcome` 为 `BLOCKED`，`data.result.outcome` 为 `ERROR`。CLI 没有取消路径：Ctrl-C 会中止进程 | 不移动 | n/a |
| 捕获期间工作区发生变化，或 `expected_snapshot_id` 不匹配 | `WORKSPACE_UNSTABLE` / `STALE_SNAPSHOT` | 不移动 | 5 |
| 错误配置 | `INVALID_CONFIG` | 不移动 | 2 |

退出码 0 表示命令成功，而不是设计“PASS”。PARTIAL 结果可以使用，但其中的缺失不能作为证明：请阅读 `analysis_coverage.limitations` 和 `data.reasons`。

### MCP 变体

`svw_build {profile}` 同步验证 profile，登记一个 `QUEUED` 作业并立即返回其 `job_id`（如果同一个 profile 已在此网关中构建，则为 `already_running: true`）。轮询 `svw_job {job_id}`；`lifecycle` 依次经过 `QUEUED`、`RUNNING`，然后是 `SUCCEEDED`、`FAILED`、`TIMED_OUT` 或 `CANCELLED`，完成的作业在 `data.result` 中携带结果摘要（`outcome`、`build_id`、`completeness`、`diagnostic_outcome`、`freshness`、`published`、`reused`、`counts`、`error`）。`SUCCEEDED` 作业仍可能是 `PARTIAL`。带 `action: cancel` 的 `svw_job` 会杀死 worker 进程树。没有队列，也没有重试：在另一个构建活动时（不同的 profile，或由另一个进程启动）请求的构建会以 `RESOURCE_LIMIT` 拒绝（仅当 `build.lock` 被持有时才有 `details.reason: build_in_progress`；见[同时使用两者](#同时使用两者)）。

### 实例演练：随附的演示

在仓库的 `svweave.yaml`、`fixtures/` 和 `toolchain.lock.json` 的一份临时副本中运行（不是在仓库本身中），事先已运行 `svweave init` 和 `svweave manifest resolve --profile
rtl_demo`。JSON 有删节（列表被截短，部分字段被删去）；数值是那次运行的真实输出，id、耗时和 RSS 因主机和检出而异。

```bash
svweave build --profile rtl_demo                 # exit 0
```

```json
{
  "outcome": "OK",
  "workspace_id": "svweave-demo",
  "snapshot_id": "7688bc870db468eab4dd2acbbb54579ddd49c5c09dcd4e81f331f75c5cdbedfa",
  "build_id": "e7bb45de61cf31a20626abb0c9c50cb04d77133bc0cd464f6d87a97bee0ebb13",
  "profile": "rtl_demo",
  "freshness": "CURRENT",
  "analysis_coverage": {
    "completeness": "COMPLETE",
    "scope": "demo_top",
    "basis": "qualified_static_subset",
    "unresolved_count": 0,
    "limitations": ["Static elaboration of the selected tops only; no runtime, simulation or formal result is represented.", "..."]
  },
  "provenance": { "backend": "slang", "evidence_kind": "HDL_STATIC_RESOLVED" },
  "data": {
    "reused": false,
    "published": true,
    "config_id": "7e5c0b93b6a10549fe398104de0afde38ae5d2111ac67894f9b633c5713d69dc",
    "completeness": "COMPLETE",
    "diagnostic_outcome": "NO_ERRORS",
    "job_lifecycle": "SUCCEEDED",
    "counts": { "declarations": 56, "references": 36, "instances": 5, "port_bindings": 26,
                "assertions": 5, "diagnostics": 0, "errors": 0, "warnings": 0, "unresolved": 0 },
    "resource_usage": { "wall_s": 0.355, "peak_rss_kib": 55120, "memory_limit_mib": 8192,
                        "timeout_s": 120.0, "isolation": "bwrap" },
    "run_dir": ".svweave/runs/652ce8a7-9a38-49c8-bbd9-60e8cb5d8365"
  },
  "error": null
}
```

在没有任何改动的情况下再次运行同一命令，不会做新的分析：输出报告 `"reused": true` 和 `"published": true`，并以 0 退出（图中的阶段 4）。`--no-reuse` 不会替换已有的 COMPLETE 构建：它会重新运行 worker 并进行比较。在同一个演示副本上实测，`svweave build --profile rtl_demo --no-reuse` 以 0 退出，带有 `verified_rerun: true`、`reused: false`、`published: true` 以及相同的 `build_id`（行为见 `src/svweave/application/build.py` 中的 `_verified_rerun`）。输出中的标识即[构建标识与状态](#构建标识与状态)中所述的那些。把 `build_id` 带入[快速开始](#快速开始)中的查询，例如 `svweave query hierarchy --build BUILD_ID`。

## 构建标识与状态

<p align="center"><img src="docs/images/identity-model.svg" alt="snapshot_id、config_id、toolchain_id 和语义 schema 版本组合成 build_id；job_id 是每次尝试一个的 uuid。五个相互正交的状态维度：作业生命周期、完整性、新鲜度、诊断结果和后端可用性，及其取值。插图显示：尽管 dir_b/common.svh 没有变化，新增 dir_a/common.svh 也会改变所选的头文件，因此查找清单是 snapshot_id 的一部分。" width="100%"></p>

*`build_id` 是 `snapshot_id : config_id : toolchain_id : semantic schema version` 的 SHA-256；输入改变会得到新的构建，重试保留 `build_id` 但获得新的 `job_id`。五个状态维度分别报告，绝不合并为单一结论：一个有错误的设计是作业 `SUCCEEDED`、分析 `PARTIAL`、诊断 `HAS_ERRORS`。插图说明了为什么查找搜索空间是 `snapshot_id` 的一部分：在 include 顺序为 `dir_a, dir_b` 时，新增 `dir_a/common.svh` 会改变所选的头文件，尽管 `dir_b/common.svh` 未被改动。*

## 配置

SVWeave 读取三个文件。`svweave.yaml` 和 `svweave.local.yaml` 只是数据：其中任何内容都不会被执行，未知字段会被拒绝，重复的 YAML 键也会被拒绝。

| 文件 | 角色 | 位置 |
|---|---|---|
| `svweave.yaml` | 项目、源码根、profile、分析和编辑设置 | 仓库根目录 |
| `svweave.local.yaml` | 外部根的主机映射（不入 Git） | 与 `svweave.yaml` 同目录 |
| `policy.json` | 受信策略 | **仓库之外**（见下文） |

<details>
<summary><code>svweave.yaml</code>：带注释，节选自本仓库的文件</summary>

```yaml
schema_version: "1.0"
project:
  id: "svweave-demo"
  root: "."
  default_profile: "rtl_demo"          # used when --profile is omitted

source_roots:                          # what SVWeave may read; every path is checked against these
  - id: "project"
    path: "."
    include: ["fixtures/**"]
    exclude: ["**/out/**", "**/waves/**"]
    writable: false                    # editing needs a writable root covered by the policy
  - id: "uvm"                          # external root: no path here, mapped in svweave.local.yaml
    include: ["**"]
    exclude: []
    writable: false

profiles:
  rtl_demo:
    cwd: "."
    language_mode: "systemverilog"
    source_inputs:
      - kind: "filelist"               # or kind: "file" (see profile uvm_smoke)
        path: "fixtures/demo/filelists/rtl.f"
        dialect: "svweave-filelist-v1"
        path_mode: "cwd"               # or "filelist" (like -f versus -F)
    compilation_units: { mode: "single" }   # single | per_file | explicit_groups
    top_modules: ["demo_top"]
    defines: [{ name: "SVW_DEMO", value: "1" }]
    include_dirs: ["fixtures/demo/include"]
    top_parameters:                    # slang -G TOP.NAME=VALUE
      - { top: "demo_top", name: "LANES", value: "2" }
    library_dirs: []
    library_files: []
    library_extensions: [".v", ".sv"]
    environment_allowlist: []          # only these variables may be expanded in paths
    generated_inputs: []               # must already exist; nothing is generated
    diagnostic_policy: "no_errors"
    # language_options:                # allowlisted slang knobs, part of config_id
    #   timescale: "1ns/1ps"
    #   std: "1800-2017"               # 1364-2005 | 1800-2017 | 1800-2023 | latest
    # ignored_options:                 # simulator run-time options declared irrelevant to analysis
    #   - {option: "+notimingcheck", reason: "simulation-only"}   # still recorded in the ledger

analysis:
  missing_input_policy: "block"
  unsupported_option_policy: "block"
  stub_policy: "explicit_only"
  max_build_seconds: 120
  max_worker_memory_mib: 8192
  # stubs:                             # owner-declared stand-ins for blackbox or encrypted IP
  #   - { module: "...", original_ip: "...", model_sha256: "<64 hex>", replaced_scope: "..." }

editing:
  enabled: false                       # also needs editing_enabled in the trusted policy
  allowed_paths: ["fixtures/demo/**"]
  max_files_per_patch: 5
  max_changed_lines: 200
  require_human_approval: true
  validate_profiles: ["rtl_demo"]      # a mandatory floor: a request may only add profiles
```

随附的文件还定义了 `rtl_demo_l4`（相同源码，`LANES` 为 4，因此 `config_id` 不同）和 `uvm_smoke`（需要 `uvm` 根）。每个文件的 JSON Schema 都在 `schemas/` 中。

</details>

**`svweave.local.yaml`** 把外部根映射到主机路径，例如 `bootstrap_locked.py` 放在 `.toolchain/` 下的锁定版 UVM 库：

```yaml
schema_version: "1.0"
root_mappings:
  - id: uvm
    path: .toolchain/uvm-core/src
```

外部根中的文件以 `@uvm/relative/path` 的形式寻址。使用未映射的外部根是一个 `MISSING_INPUT` 阻断项（`reason: root_unmapped`），因此在映射存在之前，`uvm_smoke` profile 一直是 BLOCKED。

**受信策略。** `policy.json` 位于 `$XDG_CONFIG_HOME/svweave/workspaces/<workspace_key>/policy.json`（回退到 `~/.config`），其中 `workspace_key` 是 `sha256(realpath(dir of svweave.yaml))` 的前 16 个十六进制字符。`svweave init` 创建它，且从不覆盖它。它刻意位于仓库之外：仓库内容（以及智能体写入其中的任何东西）不得能够扩大自身的权限。仓库中的文件只能收紧策略；仓库本地的策略文件会被忽略，而位于项目树内的 `--policy` 路径为 `POLICY_DENIED`。

| 字段 | 默认值 | 含义 |
|---|---|---|
| `schema_version` | 必填 | `"1.0"` |
| `allowed_roots` | 必填，绝对路径 | SVWeave 可以读取的根 |
| `writable_paths` | `[]` | 编辑可以写入的路径（仓库的 `editing.allowed_paths` 必须位于其内） |
| `approval_required` | `true` | 人工审批是强制的 |
| `network_enabled` | `false` | 后端无网络 |
| `editing_enabled` | `false` | 总开关；编辑还需要 `svweave.yaml` 中的 `editing.enabled` |
| `allow_test_approvals` | `false` | 接受未经资格认定的测试令牌审批路径；只有人工编辑此文件才能设置 |

同一个受信目录中还保存 `approval.key`（权限 0600）、`cursor.key`、审批记录以及最近一次后端探测记录。

<details>
<summary>Filelist 方言 <code>svweave-filelist-v1</code> 简述（完整规则：<code>docs/manifest.md</code>）</summary>

| 支持 | 说明 |
|---|---|
| 普通源文件路径、`-f FILE`、`-F FILE` | `-f` 相对于调用时的 cwd 解析嵌套的相对路径，`-F` 相对于该 filelist 所在目录；嵌套深度最大 32，会检测循环 |
| `+incdir+a+b`、`-I DIR`、`-IDIR` | include 目录，保留顺序，绝不排序或去重 |
| `+define+N=V+M`、`-DN=V` | 宏文本按 token 精确处理；`NAME`、`NAME=` 和 `NAME=V` 互不相同；冲突的重定义为 `INVALID_CONFIG` |
| `-v FILE`、`-y DIR`、`+libext+.v+.sv` | 库文件、库目录、库扩展名 |
| 路径 token 中的 `$VAR`、`${VAR}` | 仅当该变量在 `environment_allowlist` 中且已设置时，否则为 `MISSING_INPUT` |
| `-l`、`-logfile`、`-nologo`、`-quiet`、`-q` | 仅影响输出的选项：丢弃并记录为已忽略 |

| 拒绝 | 结果 |
|---|---|
| 任何其他 `-x` 或 `+x` 选项 | `UNSUPPORTED_OPTION`，构建为 BLOCKED（绝不静默丢弃） |
| 反引号、`$(...)`、未加引号的分号、管道、尖括号和 & 符号 | `UNSUPPORTED_OPTION`（`shell_syntax`）；不执行任何内容 |
| 块注释（`/* ... */`）、续行、把 `-D NAME=V` 写成两个参数 | 不支持 |
| 缺失的源文件、include 目录、filelist、生成的输入 | `MISSING_INPUT` |

</details>

## CLI 参考

全局选项（位于命令之前）：`--root DIR`（包含 `svweave.yaml` 的项目目录）、`--manifest FILE`、`--policy FILE`、`--local FILE`、`--request-id TEXT`、`--compact`。每个命令都输出一个 JSON 信封。

<details>
<summary>全部命令</summary>

| 命令 | 用途 |
|---|---|
| `svweave init` | 创建 `svweave.yaml` 模板、`.svweave/` 以及受信目录中的 `policy.json`；从不覆盖 |
| `svweave doctor [--probe]` | 配置、策略、后端和客户端边界检查；`--probe` 对每个后端运行一次真实的 fixture 查询（较慢） |
| `svweave manifest resolve [--profile P]` | 将 profile 解析为规范化的清单和参数台账 |
| `svweave snapshot create [--profile P]` | 捕获不可变快照 |
| `svweave snapshot check --snapshot S` | 将快照与工作区比较（STALE 时退出码 5） |
| `svweave build [--profile P] [--no-reuse]` | 解析、快照并用 slang 分析 |
| `svweave query symbol --build B` | 按 `--symbol`、`--file` + `--byte` 或 `--name` 查找声明 |
| `svweave query references --build B --symbol ID` | 已解析和未解析的出现位置，以及所搜索的作用域 |
| `svweave query hierarchy --build B` | 实例树（`--top`、`--instance`、`--depth`） |
| `svweave query impact --build B (--name N \| --symbol S \| --file F --byte N \| --instance I) [--depth N] [--limit N]` | 修改一个模块、端口、信号或实例会波及什么：实例、依赖方、端口连接、引用（计数，不是风险评分） |
| `svweave query diff-impact --build B [--base REF] [--head REF] [--repo DIR] [--depth N] [--limit N]` | 一个 git diff 在某个构建中会波及什么；diff 对照该构建的快照读取（只有标记为 `aligned` 的文件才是精确的），STALE 不改变退出码 |
| `svweave query rename-preview --build B (--name N \| --symbol S \| --file F --byte N) --to NEW [--write-spec STEM] [--limit N]` | 只读的重命名预览：经过校验的编辑位置、diff、阻断项，以及一个供 `svweave edit prepare --spec` 使用的编辑请求；从不写入源码树 |
| `svweave query connections --build B --instance I` | 直接的形参/实参端口关联（`--port`） |
| `svweave query assertions --build B` | 静态 SVA 清单（`--kind` 为 `source`、`bind` 或 `elaborated`）；执行状态始终为 `NOT_RUN` |
| `svweave query diagnostics --build B` | 编译器诊断 |
| `svweave query evidence` | 静态记录和导入的证据（`--build` 或 `--evidence`、`--type`） |
| `svweave query search --snapshot S --query Q` | 图候选或带标记的文本回退（`--scope`、`--no-graph`） |
| `svweave report --build B` | 由真实数据填充的状态报告；未评估的门禁会如实说明 |
| `svweave evidence import --manifest FILE` | 导入外部证据（存储为 `EXTERNAL_DECLARED`） |
| `svweave graph export --build B [--name N] [--max-instances N] [--max-fanout N]` | 写出 `.svweave/exports/<name>.html`：实例层次加上端口关联连接，一个自包含页面，不提供任何服务（ADR-014） |
| `svweave cache gc [--no-dry-run]` | 对快照做垃圾回收；保留最近 3 个及被引用的快照；默认为试运行 |
| `svweave debug bundle --build B [--out DIR]` | 复现包：哈希、清单、诊断摘要、脱敏路径 |
| `svweave schema emit [--check] [--out DIR]` | 写出或校验 `schemas/*.schema.json` |
| `svweave serve --transport stdio [--workspace DIR] [--no-graph]` | stdio 上的 MCP 服务器 |
| `svweave edit prepare --spec FILE` | 创建候选副本并在其中应用编辑 |
| `svweave edit validate --transaction T` | 在基线和候选上运行 slang；diff 和补丁哈希 |
| `svweave edit approve --transaction T --patch-hash H [--ttl-minutes N]` | 在 TTY 上进行人工审批 |
| `svweave edit commit --transaction T --patch-hash H [--request-key K]` | compare-and-set 应用、日志、应用后构建 |
| `svweave edit abort --transaction T` | 删除候选，保留审计记录 |
| `svweave edit recover --transaction T` | 根据日志解决一次被中断的提交（`--roll-forward`、`--rollback` 或 `--accept-current`） |
| `svweave edit show --transaction T` | 事务的状态和审计轨迹 |

</details>

每个语义 `query` 命令和 `report` 都绑定到一个不可变的构建 id。

| 退出码 | 含义 |
|---|---|
| 0 | 该命令成功（不是“设计 PASS”） |
| 2 | 用法、schema 或配置错误（`INVALID_CONFIG`） |
| 3 | 构建错误、PARTIAL 或 BLOCKED 结果、缺少输入、符号有歧义 |
| 4 | 后端不可用、能力不匹配、资源限制、超时、内部错误 |
| 5 | 过期或冲突的状态（`STALE_SNAPSHOT`、`EDIT_CONFLICT`、`RECOVERY_REQUIRED` 等） |
| 6 | 策略或审批拒绝 |

**`--allow-stale` / `--allow-partial`**（用于语义 `query` 命令）：对新鲜度为 `STALE` 或 `HISTORICAL` 的构建进行查询以 5 退出，对 `PARTIAL` 构建进行查询以 3 退出，这样脚本就不会把它误认为干净的答案。这两个参数把这两个退出码变为 0；无论如何 JSON 结果都携带 `freshness` 和 `analysis_coverage`，因此状态绝不会被隐藏。

## MCP 工具参考

`svweave serve --transport stdio` 是唯一的外部入口。会话绑定到一个工作区；没有任何工具接受文件系统根。完整契约：`docs/mcp_contract.md`；schema：`schemas/mcp/<tool>.input.json` 和 `.output.json`。

<details>
<summary>14 个工具</summary>

| 工具 | 必需输入 | 主要输出 | 只读 |
|---|---|---|---|
| `svw_status` | 无（`profile`、`probe` 可选） | 后端健康状况和能力、每个 profile 的当前构建及新鲜度、待处理的编辑、隔离情况、客户端边界 | 否（`probe:true` 会启动进程） |
| `svw_build` | `profile` | 立即返回 `job_id`；同一时间只有一个构建 | 否 |
| `svw_job` | `job_id`（`action` = `status` 或 `cancel`） | 作业生命周期、进度、结果摘要 | 否 |
| `svw_search` | `snapshot_id`、`query` | 图候选或文本回退（`SOURCE_CANDIDATE`） | 否（可能写入派生的索引缓存） |
| `svw_symbol` | `build_id` + `symbol_id`、`location{file,byte}`、`name` 之一 | 声明，或带候选项的 `AMBIGUOUS_SYMBOL` | 是 |
| `svw_references` | `build_id`、`symbol_id` | 已解析和未解析的出现位置 | 是 |
| `svw_hierarchy` | `build_id`（`instance_id`、`instance_path`、`top` 之一可选） | 先序实例树 | 是 |
| `svw_connections` | `build_id` + `instance_id` 或 `instance_path` | 直接的形参/实参关联 | 是 |
| `svw_impact` | `build_id` + `symbol_id`、`location{file,byte}`、`name`、`instance_id`、`instance_path` 之一 | 一次变更会波及什么：实例、依赖方和被依赖的定义（`depth`）、端口连接位置、引用；列表有上限但附带完整总数 | 是 |
| `svw_diff_impact` | `build_id`（`base` 默认 `HEAD`，`head` 默认工作树，`depth`、`limit`） | 一个 git diff 会波及什么：变更的声明、所属模块、实例、依赖方、变更的端口及其连接位置；逐文件的 `aligned` 标志 | 是（运行只读的 `git diff`） |
| `svw_rename_preview` | `build_id`、`new_name` + `symbol_id`、`location{file,byte}`、`name` 之一（`limit`） | 只读的重命名预览：每个编辑位置都对照快照字节校验、diff、`blockers`、`warnings`、词法上的 `unconfirmed_occurrences`，以及在没有阻断时可直接供 `svw_edit` 使用的 `edit_request` | 是 |
| `svw_assertions` | `build_id` | 静态 SVA 清单 | 是 |
| `svw_evidence` | `build_id` 或 `evidence_id` | 静态记录、导入的记录、按类型的状态 | 是 |
| `svw_edit` | `action` = `prepare`、`validate`、`commit` 或 `abort`，加上各 action 的字段 | 事务状态、diff、补丁哈希 | 否（破坏性标注仅为提示） |

`svw_edit` 不能审批：没有审批字段，且 schema 禁止未知字段。标注只是提示，不是访问控制。

</details>

**信封。** 每个结果，无论成功还是失败，都是一个信封，以 MCP `structuredContent` 和一个文本块的形式交付：`schema_version`、`request_id`、`outcome`（`OK`、`PARTIAL`、`BLOCKED` 或 `ERROR`）、`workspace_id`、`snapshot_id`、`build_id`、`profile`、`freshness`（`CURRENT`、`STALE`、`HISTORICAL` 或 `UNKNOWN`）、`analysis_coverage`（`completeness`、`scope`、`basis`、`unresolved_count`、`limitations`）、`provenance`（`backend`、`toolchain_id`、`evidence_kind`）、`data`、`pagination`（`limit`、`next_cursor`、`truncated`）、`warnings` 以及 `error`（`code`、`message`、`details`）。在 `PARTIAL`、`STALE`、`HISTORICAL` 或 `truncated=true` 的情况下，页面中缺失的条目并不能证明其不存在。

<details>
<summary>错误目录</summary>

| 代码 | 含义 | CLI 退出码 |
|---|---|---|
| `INVALID_CONFIG` | 错误配置、违反 schema 或参数约束、错误或不属于本处的游标 | 2 |
| `MISSING_INPUT` | 未知的构建、快照、作业或证据；缺失的文件或根映射 | 3 |
| `UNSUPPORTED_OPTION` | slang 适配器无法遵从的选项 | 3 |
| `PARTIAL_ANALYSIS` | 部分结果 | 3 |
| `AMBIGUOUS_SYMBOL` | 有多个带作用域的候选 | 3 |
| `PATH_NOT_ALLOWED` | 路径在允许的根、include/exclude 规则或符号链接目标规则之外 | 6 |
| `POLICY_DENIED` | 策略拒绝该操作 | 6 |
| `APPROVAL_REQUIRED`、`APPROVAL_EXPIRED` | 缺失、伪造或已过期的审批 | 6 |
| `WORKSPACE_UNSTABLE`、`STALE_SNAPSHOT` | 捕获期间工作区发生变化，或 `expected_snapshot_id` 不匹配 | 5 |
| `EDIT_CONFLICT`、`RECOVERY_REQUIRED`、`IDEMPOTENCY_CONFLICT` | 编辑状态冲突 | 5 |
| `BACKEND_UNAVAILABLE`、`CAPABILITY_MISMATCH` | 后端缺失，或工具链/能力与锁定文件不一致 | 4 |
| `BUILD_TIMEOUT`、`RESOURCE_LIMIT`、`OUTPUT_LIMIT`、`INTERNAL` | 超时、限制（也包括已有另一个构建处于活动状态）、意外异常 | 4 |

每个代码对应的确切退出码定义在 `src/svweave/domain/errors.py` 中；`docs/mcp_contract.md` 解释了三种结果类别（协议错误、工具执行错误、合法的 PARTIAL 结果）。

</details>

**分页与游标。** `limit` 默认 100，最大 500（`svw_symbol` 默认 50）。响应上限为 128 KiB：超出上限的页面会被截断，`pagination.truncated=true` 并设置 `next_cursor`（绝不会产生无效 JSON）。游标形如 `base64url(json).hmac`，用工作区的 `cursor.key` 签名，并绑定构建或快照 id、查询的哈希（不含 `limit`/`cursor`）、排序名和偏移量。来自另一个构建、查询、排序方式或工作区的游标，或被篡改的游标，会以 `INVALID_CONFIG` 拒绝。越过末尾的空页面不代表对其他查询的任何陈述。超过 64 KiB 的请求、未知字段以及文本参数中的控制字符，会在触及任何后端之前被拒绝。

## 受控编辑工作流

编辑**默认禁用**。它需要受信 `policy.json` 中的 `editing_enabled: true` **以及** `svweave.yaml` 中的 `editing.enabled: true`、一个被策略的 `writable_paths` 覆盖且 `writable: true` 的源码根，以及 `editing.allowed_paths`。详情：`docs/editing.md`。

1. **准备（Prepare）。** 编写一个绑定基础构建和快照的请求：

   ```json
   {
     "base_build_id": "<64 hex>",
     "snapshot_id": "<64 hex>",
     "edits": [
       { "kind": "symbol_replacement", "declaration_id": "<id from a symbol query>", "new_body": "..." }
     ],
     "request_key": "demo-edit-0001",
     "validate_profiles": ["rtl_demo"]
   }
   ```

   一项编辑可以是 `symbol_replacement`（声明 id，绝不是裸名称）、`slang_symbol`（通过 `qualified_name`（例如 `pkg::cls::run_phase`）或 `symbol_id` 定位的 task、function 或构造函数，以 `replacement` 给出完整的新声明，并可选地给出 `expected_sha256`；有歧义或未知的名称会被拒绝，ADR-013），或 `exact_range`（`logical_path`、`start_byte`、`end_byte`、`expected_sha256`、`replacement`）。基础构建必须为 COMPLETE 且 CURRENT。

   ```bash
   svweave edit prepare --spec spec.json            # real byte copy under .svweave/candidates/<txn>
   ```

2. **验证（Validate）。** slang 针对每个 validate profile 在基线和候选上运行；两者都必须为 `NO_ERRORS` 且 `COMPLETE`，且没有新的未豁免警告。结果是 unified diff、逐文件哈希、`patch_hash`、诊断差异以及限制（EDA、仿真和形式验证为 `NOT_RUN`）。

   ```bash
   svweave edit validate --transaction TXN
   ```

3. **审批（由人工在真实终端中进行）。** diff 显示在 stderr 上，你需要输入补丁哈希的前 12 个字符。审批记录绑定补丁哈希、基础快照、profile 集合、策略哈希、每个 profile 的 `config_id`、事务 id 和过期时间（默认 30 分钟），并使用 `approval.key` 以 HMAC-SHA256 签名。没有 MCP 审批工具。

   ```bash
   svweave edit approve --transaction TXN --patch-hash HASH
   ```

4. **提交（Commit）。** 校验审批，重新解析每个 validate profile（`config_id` 不变），重新检查路径策略和工具链，获取排他的工作区锁，将工作区与基础快照比较，写入预写日志（fsync），然后逐文件检查修改前哈希并原子替换。之后检查字节、工作区快照 id 以及每个 validate profile 的全新构建；只有到这时，旧构建才变为 `HISTORICAL`，状态变为 `POST_APPLY_VERIFIED`。如果某项检查失败，状态保持 `APPLIED` 并带有 `post_apply_status: FAILED`。重复发送提交会返回原始结果。

   ```bash
   svweave edit commit --transaction TXN --patch-hash HASH
   svweave edit show --transaction TXN              # state and audit trail
   ```

5. **中止或恢复。**

   ```bash
   svweave edit abort --transaction TXN             # candidate removed; refused while committing or applied
   svweave edit recover --transaction TXN           # after an interrupted commit
   ```

   恢复会对每个文件分类：当前字节等于修改后哈希的为*已应用*，等于修改前哈希的为*未应用*，其他情况为*冲突*。它从不覆盖不是它写入的字节；冲突的文件保留人工的版本，候选版本放到 `.svweave/recovery/<txn>/`。当每个文件都一致时，或通过显式的 `--roll-forward`、`--rollback` 或 `--accept-current`，阻断才会解除。从不使用 `git reset` 或 `git checkout`。

**会被拒绝的情况：** 用裸名称代替声明 id（`AMBIGUOUS_SYMBOL`）；写在宏体中的声明以及对 `` `define `` 指令的编辑（`macro_definition_edit_unsupported`）；包含终端控制字符、DEL 或双向文本覆盖字符的替换文本；新增或修改诊断控制指令的 diff；对 filelist 和生成输入的编辑；重命名、删除、新建文件、二进制文件、符号链接和硬链接目标；`allowed_paths` 之外的文件，或超过 `max_files_per_patch` / `max_changed_lines` 的情况；在同一文件中同时进行 Serena 编辑和精确区间编辑。Serena 只对 SystemVerilog `function` 声明进行语义编辑（ADR-012）；task 和构造函数通过 `slang_symbol` 按名称编辑，由 slang 解析并由网关作为带标签的精确区间补丁应用（ADR-013）；显式区间使用 `exact_range`。多个文件的替换不是一个原子事务；日志和恢复机制正是因此而存在。

以你的 UID 运行且拥有不受限 shell 的智能体可以伪造审批：在任何重要的地方启用编辑之前，请先阅读 `docs/security.md`。

## 工具链与后端

所有内容都锁定在 `toolchain.lock.json` 中（每个制品的确切版本、URL 和 SHA-256，以及网关 venv 的按哈希锁定的 wheelhouse）。没有任何东西从“latest”安装，能力或工具链不匹配即为 `CAPABILITY_MISMATCH`。

| 组件 | 版本 |
|---|---|
| pyslang / slang | 12.0.0 / v12.0 |
| codebase-memory-mcp | v0.11.0（`linux-amd64-portable`） |
| Serena（`serena-agent`） | v1.7.0（commit 949a27ef1e5f）；升级会改变其许可证（ADR-010） |
| Verible | v0.0-4296-g0f262651 |
| MCP Python SDK | 2.3.0 |
| Accellera UVM（`uvm-core`） | 2020.3.1 |

**引导安装。** 联网，一次性：`scripts/bootstrap_locked.py` 下载每个制品，在使用前校验其 SHA-256，解压时带路径穿越防护，并依据锁定的 `uv.lock` 构建 Serena venv。`--verify-only` 在不联网的情况下将已安装状态与锁定文件比较。对于离线机器，用 `scripts/bootstrap_locked.py --fetch-only` 准备缓存，复制仓库和 `.toolchain/`，然后运行 `scripts/bootstrap_locked.py --offline --create-venv --venv-dir .venv`，最后以 `--verify-only` 收尾。洁净室离线运行由 `tests/integration/test_dep01_cleanroom.py` 测试（在构建主机上进行网络隔离，而不是在单独的物理隔离机器上）。`scripts/gateway_lock_refresh.py` 是一个维护者工具，用于重新生成锁定文件中的网关 wheelhouse 部分。流程：`docs/install.md`。

**沙箱。** 每个后端（包括 pyslang worker）都通过一个 `bwrap` 参数数组启动：系统和工具链只读，只对其候选副本（如适用）和自身状态目录可读写，`--unshare-net`，独立的 PID 命名空间（随网关一同终止），并且没有真实的 `$HOME`。原始源码不以可写方式挂载，或根本不挂载。`bwrap` 不施加 CPU、内存或 pid 数限制，也没有 seccomp 过滤器；只有 HDL worker 有内存限制（`RLIMIT_AS`）。如果非特权用户命名空间不可用，后端会失败关闭，或仅在显式标记的降级模式下运行（ADR-011）。

**glibc 说明。** 标准的 codebase-memory-mcp 二进制需要 glibc 2.38，在较旧的主机上（例如 Enterprise Linux 8，glibc 2.28）会失败；锁定文件固定的是可移植（静态）版本，它可以在那里运行。不要在未重新探测并更新锁定文件和许可证审查的情况下替换该二进制。

**READY 或 DEGRADED。** `svweave doctor` 不启动后端；`svweave doctor --probe`（或 `scripts/probe_backends.py`，它会写入 `.svweave/probe/capability_report.json`）会启动每个真实的锁定版后端，记录协商的协议、工具列表和 schema 哈希，并运行一次真实的 fixture 查询。后端绝不会因为其进程启动了就是 READY：READY 要求具备预期的工具并对 fixture 查询给出正确答案。已记录并接受的差距使其保持 `DEGRADED`（在 2026-10-14 的运行中，codebase-memory-mcp 和 Serena 为 `DEGRADED`）；缺失或失败的后端为 `UNAVAILABLE`。回退绝不会把失败的必需后端变成成功：搜索回退为带标记的字面文本搜索，outcome 为 `PARTIAL`。

## 测试与资格认定（验证）

```bash
.venv/bin/python -m pytest tests/unit tests/contract                    # fast, no real backends
.venv/bin/python -m pytest tests/fault_injection                        # commit crash and recovery
SVWEAVE_REQUIRE_BACKENDS=1 .venv/bin/python -m pytest -m integration    # real backends, skips fail
SVWEAVE_REQUIRE_BACKENDS=1 .venv/bin/python -m pytest tests/acceptance  # end-to-end demo flow, docs check
.venv/bin/python scripts/qualify.py                                     # release gates G0-G5
```

唯一的自定义标记是 `integration`（真实的锁定版后端）。`SVWEAVE_REQUIRE_BACKENDS=1` 把缺失的后端从跳过变为失败；`qualify.py` 总是设置它，并把被跳过的测试视为门禁失败。mock 仅用于单元测试和故障注入测试。DEP-01 干净环境测试（`tests/integration/test_dep01_cleanroom.py`）还需要 `.toolchain/` 下的缓存 artifact（从 `vendor/` 安装后：`ln -s ../vendor/downloads .toolchain/downloads`），以及 `PATH` 上第一个 `python3` 为 3.11 及以上；见 `docs/install.md` 的“Running DEP-01”。静态检查：`.venv/bin/ruff check .`、`.venv/bin/mypy` 和 `.venv/bin/svweave schema emit --check`。

**门禁。** `scripts/qualify.py` 根据实际结果计算 G0 至 G5，并写入 `.svweave/qualification/report.{json,md}`：G0 锁定文件校验、后端探测和沙箱测试；G1 清单、快照、配置和路径策略；G2 语义测试套件（真实的 pyslang 和 UVM）；G3 后端测试套件和 MCP 契约；G4 编辑安全和全部关键变异；G5 每个 P0 测试套件都没有 FAIL、BLOCKED 和 SKIP，验收测试，覆盖率至少 85% 语句和 80% 分支，文档检查，以及符合计划 22.3 预算的基准测试。由未运行步骤计算出的门禁为 `NOT_EVALUATED`，绝不是 PASS。只有当每个门禁都通过且没有未关闭的阻断项时，发布决定才是 `ACCEPTED_STATIC_PREVIEW`；否则为带原因的 `BLOCKED`。

**变异测试套件。** `scripts/mutation_suite.py` 将二十二个刻意制造的缺陷（计划 20.5 的十个类别，其中一个类别有两个变体，另加十一个写入安全变异，针对恢复的 roll-forward 门禁、提交时的策略复查以及基于名称的 `slang_symbol` 编辑的目标选择规则）逐个注入仓库的一份临时副本，并运行对应的测试；当这些测试失败时，该变异即被杀死。`--only M01,M05` 可选择其中部分；除干净的失败之外的任何情况（缺少补丁锚点、超时、基线就是红的）都是 `ERROR`，绝不算被杀死。

**基准测试。** `scripts/gen_corpus.py` 写出一个确定性的合成语料库，`scripts/benchmark.py` 记录主机、语料库和原始耗时（`evidence/bench/`），然后与计划 22.3 的预算进行比较。参考语料库为规模 1.0：约 500 个文件、100,000 行；缩小的规模会如实标注。

| 目标（计划 22.3） | 预算 | 实测（规模 1.0，5 次冷运行，30 次热运行） |
|---|---|---|
| 冷启动语义构建 | 120 s | 中位数 24.3 s，最大 24.5 s |
| HDL worker 峰值内存 | 8 GiB | 0.059 GiB |
| 热符号查询 p95 | 1 s | 0.045 s |
| 热层次结构查询 p95 | 1 s | 0.020 s |
| 热引用查询 p95 | 1 s | 0.164 s |
| 热搜索 p95，文本回退 / 图 | 2 s | 0.070 s / 0.689 s |

这些数据来自 `evidence/bench/20261014T085609Z-s1.0.json`：500 个文件、100,374 行，运行于一台 Intel Xeon Gold 6138 主机，16 个逻辑 CPU、19.3 GiB 内存，这**不是**计划中的参考主机（`reference_host_met: false`）。它们不代表其他主机或真实项目的情况。不声称任何 token 或时间上的节省：计划 22.4 尚未运行。

<p align="center"><img src="docs/images/evidence-status.svg" alt="左侧：六种证据类型 SOURCE_CANDIDATE、LSP_RESOLVED、HDL_STATIC_RESOLVED、RUNTIME_OBSERVED、EXTERNAL_DECLARED 和 UNRESOLVED，各自能支持和不能支持什么。右侧：2026-10-14 资格认定运行的快照：G0 PASS_WITH_OWNER_WAIVER_OPEN，G1 至 G5 PASS，920 个 P0 测试通过，0 个失败、阻塞或跳过，22 个变异中 22 个被杀死，覆盖率 93.58% 语句和 87.71% 分支，EDA 编译、仿真和形式验证 NOT_RUN，发布决定为 BLOCKED，等待负责人签核 g0_waiver、adr_012 和 license_review。" width="100%"></p>

*证据类型表不是排名：运行时证据证明的是某一次运行，静态分析覆盖从未运行过的代码，两者互不替代。下方面板是 2026-10-14 资格认定运行（源码树 sha256 `21a97d0c...`）的带日期快照，不是长期有效的声明。静态预览门禁 G0 至 G5 与 EDA 编译、仿真和形式验证分开；后者保持 `NOT_RUN`，除非用户导入证据（存储为 `EXTERNAL_DECLARED`）。发布决定为 `BLOCKED` 仅仅是因为三项负责人签核尚未完成。*

## 安全模型

对于通过网关进行的操作，各项控制保证：

* 原始源码永远不能被后端写入；编辑发生在候选副本中，写入协调器只有在有效审批和 compare-and-set 检查之后才修改源码。
* 任何地方都不存在布尔型的审批。审批由人工 CLI 在 TTY 上创建，以 HMAC-SHA256 签名，绑定补丁、快照、profile、策略和过期时间，任何变化都会使其失效。
* 后端没有网络（`--unshare-net`）；网关没有监听 socket；只有安装步骤会下载，且会先校验 SHA-256。
* 仓库配置是数据：没有 hook、Make、Tcl、shell 替换或 `eval`。来自源码、注释、README 和工具输出的文本是数据，不能改变策略或跳过验证。
* 读取限制在允许的根之内，并检查符号链接目标；对外暴露的字符串会被脱敏，结果有大小上限。

**不**保证的内容：以相同 UID 运行且拥有不受限 shell 的智能体可以读取 `approval.key` 并伪造审批，或直接编辑工作区；拥有自己 shell 或文件工具的 MCP 客户端可以写入源码树。因此 `svweave doctor` 始终报告 `client_boundary: not qualified`，编辑默认关闭。`bwrap` 没有资源限制或 seccomp 过滤器。本地索引并非端到端离线：返回给云端模型客户端的代码片段会离开本机。详情：`docs/security.md`。

<p align="center"><img src="docs/images/backend-sandbox.svg" alt="后端沙箱与传输上限。左侧为受信的网关：security/sandbox.py 中的 SandboxRunner 构建一个 bwrap 参数数组（绝不是 shell 字符串），用 validate_spec 检查可写范围；没有可用的 bwrap 时报告 BACKEND_UNAVAILABLE，除非已允许降级模式，并标记为 degraded_no_isolation。有上限的 MCP stdio 传输保留 SDK stdio_client 的契约，但以每次 64 KiB 读取原始字节，每行 JSON-RPC 最多接受 16 MiB，每个工具结果最多 16 Mi 字符；锁定版本的后端都在此之下（cbm 自己拒绝超过约 10 MiB 的响应，Serena 把每个回答截断到 150,000 字符）。溢出时：1 终止后端的进程组（先 SIGTERM，再 SIGKILL）并回收；2 造成溢出的调用失败，行过长为 BACKEND_UNAVAILABLE、reason message_too_large，结果过长为 OUTPUT_LIMIT、reason result_too_large；3 结果过长的情况下，同一后端上的其他调用以 BACKEND_UNAVAILABLE、reason backend_terminated 失败。stderr 写入只在主机上的日志，非协议的 stdout 行为 protocol_error，协议版本必须与锁定的版本一致。右侧为 bwrap 沙箱，每个后端进程一个：namespace 参数 --die-with-parent、--new-session、--unshare-user、--unshare-pid、--unshare-ipc、--unshare-uts、--unshare-cgroup-try、--unshare-net；/usr 只读，/bin、/sbin、/lib 和 /lib64 为指向它的符号链接，只有少数 /etc 文件、/proc 和 /dev，不 bind $HOME；私有的 tmpfs /tmp，codebase-memory-mcp 在其中得到 /tmp/cbm-rt 作为 CBM_RUNTIME_DIR，因为 unix socket 路径必须符合约 108 字节的 sockaddr_un；只读 bind 锁定版本的工具链、venv 解释器的可执行文件、stdlib 和 libpython，以及快照，venv 的 bin/python 符号链接链只在 mount_providing 找不到已显示它的挂载时，才用 --symlink 重建；可读写的 bind 只有候选副本和后端的工作目录；会被拒绝的：受保护的源代码本身或其周围的可写 bind、可写的系统目录或 $HOME，以及同时以两种方式 bind 的路径；后端进程以 /usr/bin/env -i 加上明确的环境变量允许列表启动。" width="100%"></p>

*两道控制在后端边界交汇。沙箱决定后端进程能看到和写入什么：一个有 namespace、没有网络的 `/usr` 和少数 `/etc` 文件视图，只读 bind 它所需要的工具链、解释器和快照，且只有两个目录可写：它的候选副本（Serena）和它自己的工作目录；源代码由 `validate_spec()` 保护，默认根本不挂载。传输则决定后端能让网关在内存中保留多少数据：MCP SDK 的读取器没有行长上限，所以网关通过自己的有上限读取器读取后端输出，并在报告过大的消息或结果之前先终止后端。*

图中有两个容易忽略的细节。codebase-memory-mcp 通过 `CBM_RUNTIME_DIR` 下的 unix socket 协调，而 socket 路径一旦超过内核的 `sockaddr_un` 上限（约 108 字节）就会失败，较深的工作区路径很容易超过；因此沙箱在它临时存在的 `/tmp` 上给它一个短的私有目录 `/tmp/cbm-rt`。只读 bind 是按真实路径进行的，所以如果 venv 的解释器要经过一条符号链接链才能到达（python-build-standalone 运行环境有 `bin/python3 -> python3.12`），中间的链接就会丢失；Serena 沙箱会用 `--symlink` 重建它们，但只在没有任何 bind 已经显示该链接的地方，因为落在 bind 内的链接会被创建在主机上（`mount_providing()`）。上限与原因：`docs/security.md` 第 3 和第 6 节。

## 已知限制

最重要的十条；完整列表见 `docs/limitations.md`。

1. 仅支持 Linux x86_64；macOS 和 Windows 未经资格认定。
2. 没有 EDA 运行器：编译、仿真、形式验证和覆盖率均为 `NOT_RUN`，除非你导入证据。
3. 每次构建都是完整重建；不做任何增量方面的声明。
4. pyslang 没有“查找所有引用”：缺少引用并不证明不存在，每个结果都会说明其搜索的作用域。
5. 虚分派调用为 `dynamic_unresolved`（列出候选，不做选择）；不计算 UVM factory、`config_db` 和运行时拓扑。
6. 加密源码和隐式黑盒永远不会是 COMPLETE（`PARTIAL`，禁用“不存在”声明）。
7. 图后端把 `module` 和 `interface` 标记为 `Class`，且不提取端口、generate 块、实例、断言、bind 或 include；它只是一个候选查找器。
8. Verible 不暴露 `task` 或 `function new`；Serena 编辑仅对 `function` 声明经过资格认定，其他一切都是精确区间补丁。
9. 拥有 shell 的同 UID 智能体可以伪造审批；多文件写入不是原子的。
10. 性能数据只存在于已记录的语料库和主机上；第三方许可证的法律审查为 `NOT_DONE`。

## 故障排查

首先检查：`svweave doctor`（以及 `--probe`），然后是 `svweave report --build BUILD_ID`。日志：每次构建在 `.svweave/runs/<job-id>/events.jsonl`；每个服务器会话在 `.svweave/runs/serve-*/`，含 `server.log` 和 `events.jsonl`。

| 症状 | 检查 | 不要 |
|---|---|---|
| 层次结构中缺少某个实例 | `svweave manifest resolve --profile P`（顶层、文件顺序、参数），`svweave query diagnostics --build B`，然后用 `svweave query hierarchy --build B` 查看 generate 分支和未解析的模块 | 把图候选加入语义结果 |
| 找不到某个引用 | 阅读 `svweave query references` 中所搜索的作用域，检查声明 id、宏来源和动态分派 | 断定没有任何地方使用它 |
| 修改 `.svh` 没有使构建失效 | `svweave snapshot check --snapshot S`（STALE 时退出码 5），然后 `svweave build --profile P` | 只是重启某个 UI |
| 编辑了错误的 `run_phase` 或出现 `AMBIGUOUS_SYMBOL` | `svweave query symbol --build B --name X`，使用带作用域的 `declaration_id` | 全局字符串替换 |
| slang 与你的 EDA 工具结论不一致 | 比较语言模式、编译单元、宏定义、include 顺序、库和 stub；用 `svweave evidence import` 导入你的运行结果 | 仅为了让两边都变绿而修改源码 |
| 验证通过但提交被拒绝 | `svweave edit show --transaction T`；检查审批过期时间、基础快照、策略和日志 | 绕过 compare-and-set 检查 |
| 提交被中断 | `svweave edit recover --transaction T`；在解决之前，新的 prepare 和 commit 会一直被阻断 | 删除日志或运行 `git reset` |
| worker 崩溃或超时 | `svweave debug bundle --build B` 以及运行目录；当 slang 读取快照之外的内容时，构建为 BLOCKED | 无限制地重试，或返回空图 |
| 图显示旧文件或已停止 | `svweave doctor --probe`；用 `svweave query search --snapshot S --query Q --no-graph` 进行文本回退 | 信任 watcher 状态 |
| 后端无法启动 | `bwrap --version && bwrap --ro-bind / / true`；必须启用用户命名空间 | 静默禁用沙箱 |
| 静态的运行时拓扑与类树不一致 | 比较测试、种子、factory 证据和 profile | 宣布其中一方是错的 |

退出码列于 [CLI 参考](#cli-参考)；日常流程和上手流程见 `docs/operations.md`。

## 文档

| 主题 | 文件 |
|---|---|
| 已实现的架构 | `docs/architecture.md` |
| 清单、filelist 方言、快照 | `docs/manifest.md` |
| 支持的特性与语义限制 | `docs/supported_features.md` |
| 已知限制（汇总） | `docs/limitations.md` |
| MCP 工具、信封、错误目录 | `docs/mcp_contract.md` |
| 运维、退出码、证据导入 | `docs/operations.md` |
| 受控编辑 | `docs/editing.md` |
| 安全模型 | `docs/security.md` |
| 安装、离线引导安装 | `docs/install.md` |
| 资格认定报告模板 | `docs/qualification_report_template.md` |
| 决策记录 | `docs/adr/` |
| 工具链 spike 事实 | `docs/m0_spike_report.md` |
| 编码智能体规则 | `AGENTS.md` |

## 贡献

权威规范是 `SVWeave_plan.md`（中文；请勿编辑）；`AGENTS.md` 浓缩了其中的规则。最重要的几条：

* 阅读约束你的任务的那一节计划；保持需求到测试的映射。
* 工作树中未提交的改动是别人正在进行的工作：绝不回退或清理它们。除非用户明确要求，编码智能体不执行 `git commit`、push 或 merge。
* 锁定的版本、哈希和许可证位于 `toolchain.lock.json`、`capability_report.json` 和 `license_manifest.json` 中；绝不猜测缺失的 API、寄存器或 EDA 结果，也绝不随意升级后端。
* 绝不为了让测试通过而添加 stub、禁用 SVA 或忽略编译器错误。失败的测试本身就是一项发现。
* `fixtures/expected/` 中的黄金文件经过人工审查；只有在给出书面语义理由时才可修改，绝不为了让 CI 变绿而修改。
* 将各类状态分开（静态、EDA 编译、仿真、形式验证、覆盖率）；SKIP、BLOCKED 和 NOT_RUN 绝不是 PASS。
* 关于性能和 token 节省的声明需要来自 `scripts/benchmark.py` 的实测证据。
* 发布决定来自 `scripts/qualify.py`，而不是文字描述。文档中出现的每条 `svweave ...` 命令都必须存在（DOC-01 测试会检查这一点）。

## 许可证

* **SVWeave 自身的许可证：TBD by owner**（计划 3.2）。在负责人做出决定之前，本仓库不授予任何许可。
* 第三方组件由安装程序下载，不在此处再分发。锁定的 Serena v1.7.0 为 MIT；slang 和 pyslang 为 MIT；Verible 为 Apache-2.0；codebase-memory-mcp 为 MIT 并附带内置的第三方组件；MCP Python SDK 为 MIT；Accellera UVM 为 Apache-2.0。这些是工程层面的清单，不是法律审查（`THIRD_PARTY_NOTICES.md`、`license_manifest.json`；法律审查状态为 `NOT_DONE`）。将 Serena 升级到 v1.7.0 之后的版本会改变其许可证，并需要重新审查（ADR-010）。
