# SVWeave

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 本文是 `README.md` 的简体中文译本；如与英文版有出入，以英文版 `README.md` 为准。

**SVWeave 是一个以 SystemVerilog 为先的本地代码智能层。** 它让 AI 客户端先找到相关源码（仓库图），再针对某个**具名构建**验证静态语义（slang 展开），最后才通过一条可追溯、可拒绝、可恢复的流程（候选副本、验证、人工审批、compare-and-set 提交）修改代码。

它以 Python 包、本地 CLI（`svweave`）以及位于三个相互独立的后端进程之上的**一个** MCP 网关的形式交付。它不合并上游项目，没有 Web UI、没有 HTTP 端点，也不做模型训练。

## 目录

1. [架构概览](#架构概览)
2. [状态](#状态)
3. [它不是什么（计划 4.3 非目标）](#它不是什么计划-43-非目标)
4. [仓库布局](#仓库布局)
5. [快速开始](#快速开始)
6. [在 Claude Code 和 Codex 中使用 SVWeave](#在-claude-code-和-codex-中使用-svweave)
7. [一次变更如何流转](#一次变更如何流转)
8. [构建流程](#构建流程)
9. [构建标识与状态](#构建标识与状态)
10. [配置](#配置)
11. [CLI 参考](#cli-参考)
12. [MCP 工具参考](#mcp-工具参考)
13. [受控编辑工作流](#受控编辑工作流)
14. [工具链与后端](#工具链与后端)
15. [测试与资格认定（验证）](#测试与资格认定验证)
16. [安全模型](#安全模型)
17. [已知限制](#已知限制)
18. [故障排查](#故障排查)
19. [文档](#文档)
20. [贡献](#贡献)
21. [许可证](#许可证)

## 架构概览

<p align="center"><img src="docs/images/architecture.svg" alt="架构图：人工使用的 CLI 和 MCP 客户端接入同一个 SVWeave 网关（cli.py、server.py、带有 11 个 svw_* 工具的 gateway/tools.py、application/services.py、策略、作业管理器、溯源与错误映射）。三个适配器分别驱动 codebase-memory-mcp v0.11.0、带 Verible 的 Serena v1.7.0 以及 pyslang 12.0.0 worker，每个都运行在无网络、且未挂载原始源码的 bwrap 沙箱内。快照、候选工作区和 SQLite 数据库位于 .svweave/；策略和密钥位于仓库之外的受信目录。只有编辑协调器在审批和 compare-and-set 检查之后才会写入用户源码工作区。" width="100%"></p>

*客户端只与一个网关通信。网关掌管策略、作业、溯源和错误映射，并通过适配器驱动三个后端。每个后端都运行在 `bwrap` 沙箱中，网络关闭，且原始源码不以可写方式挂载（或根本不挂载）；pyslang worker 同样处于沙箱中。后端读取不可变快照，或写入 `.svweave/` 下的候选副本。受信目录（策略、审批 HMAC 密钥、游标密钥）位于仓库之外。用户的源码工作区只有一条访问路径：编辑协调器经过审批、compare-and-set 的写入（TB4）。虚线是信任边界 TB1 至 TB4。*

## 状态

**开发者预览版 v0.1.0-dev。UNQUALIFIED**，除非资格认定报告（`.svweave/qualification/report.json`，由 `scripts/qualify.py` 生成，证据位于 `evidence/`）显示 `ACCEPTED_STATIC_PREVIEW`。工具的"静态预览"状态绝不代表仿真、形式验证或 EDA 结果：`eda_compile`、`simulation` 和 `formal` 均为 `NOT_RUN`，除非你导入自己的证据；而导入的证据是 `EXTERNAL_DECLARED`，从不由 SVWeave 验证。依赖任何结果之前，请先阅读 `docs/limitations.md`。

**最近一次记录的资格认定运行（2026-10-14，源码树 sha256 `21a97d0c...`，git HEAD `b6ba45c` 加未提交的改动，Linux x86_64，仅限此主机）：**

| 项目 | 结果 |
|---|---|
| Gates | G0 `PASS_WITH_OWNER_WAIVER_OPEN`，G1 至 G5 `PASS` |
| P0 测试（通过 / 失败 / 阻塞 / 跳过） | 920 / 0 / 0 / 0 |
| 关键变异 | 22 个中 22 个被杀死，无存活 |
| SVWeave 自身代码的覆盖率 | 93.58% 语句 / 87.71% 分支（软件质量保证，不是 RTL 覆盖率） |
| EDA compile、simulation、formal | `NOT_RUN` |
| 发布决定 | **BLOCKED**，仅因三项未完成的负责人签核：`g0_waiver`、`adr_012`、`license_review` |

G0 不是单纯的 PASS，因为 codebase-memory-mcp 和 Serena 的探测结果为 `DEGRADED`（在声明的子集之内），且负责人尚未签署豁免。证据副本位于 `evidence/qualification/`（`report.md`、`report.json`、`mutation.json`）和 `evidence/bench/20261014T085609Z-s1.0.json`。这是某一次运行的快照，不是长期有效的声明；要了解你自己代码树的状态，请重新运行 `scripts/qualify.py`。

| 后端（在 `toolchain.lock.json` 中锁定） | 角色 |
|---|---|
| pyslang 12.0.0（独立 worker 进程） | 感知构建的符号、引用、层次结构、端口关联、SVA 清单 |
| codebase-memory-mcp v0.11.0 | 仓库图：候选文件（绝不作为语义真相） |
| Serena v1.7.0 + Verible | 符号导航，以及在候选副本中编辑函数体 |

## 它不是什么（计划 4.3 非目标）

不是仿真器、形式验证工具，也不是 lint/签核工具；不做位级连通性、时钟/复位域、CDC/RDC、时序或功耗分析；不恢复加密源码；不静态解析 UVM factory 覆盖、`config_db` 命中或运行时拓扑；不做 SVA 证明、覆盖率收敛或签核结论；不支持 VHDL、AMS 或混合语言语义；不是多租户服务，也没有远程 HTTP 端点；没有增量编译（每次构建都是完整重建）；仅支持 Linux。

## 仓库布局

```text
SVWeave/
  svweave.yaml, toolchain.lock.json, capability_report.json, license_manifest.json, pyproject.toml
  src/svweave/
    cli.py, server.py        click CLI and MCP stdio server (both thin)
    gateway/                 MCP schemas, tool registry (11 svw_* tools), validation, dispatch
    application/             use cases shared by CLI and MCP (services, build, queries, search, edit,
                             jobs, evidence, status, report, doctor, debug bundle, init, keys)
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
| `tests/integration` | 真实的 pyslang worker、codebase-memory-mcp、Serena、沙箱（`integration` 标记） |
| `tests/fault_injection` | 编辑提交的崩溃与恢复；`tests/acceptance` 为端到端演示流程和文档检查 |
| `evidence/` | 上述状态所引用的资格认定、基准测试和变异结果的副本 |

## 快速开始

安装要求和离线流程见 `docs/install.md`。

```bash
# 1. install the package and the pinned backends (the only step that uses the network)
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python scripts/bootstrap_locked.py
.venv/bin/python scripts/bootstrap_locked.py --verify-only

# 2. workspace and checks (this repository ships a demo svweave.yaml and fixtures)
svweave init                       # never overwrites; creates the trusted policy outside the repo
svweave doctor                     # includes the "client boundary not qualified" item
svweave doctor --probe             # real fixture probe of every backend (slow)

# 3. resolve, build, query
svweave manifest resolve --profile rtl_demo      # review the argument ledger by hand
svweave build --profile rtl_demo                 # prints build_id and snapshot_id
svweave query hierarchy --build BUILD_ID
svweave query symbol --build BUILD_ID --name demo_lane
svweave query search --snapshot SNAPSHOT_ID --query demo_lane
svweave report --build BUILD_ID --format json

# 4. serve the MCP gateway for your client (stdio)
svweave serve --transport stdio
```

退出码：0 表示该命令成功（不是"设计 PASS"），2 表示用法/配置错误，3 表示构建错误、PARTIAL 或 BLOCKED，4 表示后端/资源/超时，5 表示 stale/冲突，6 表示策略/审批拒绝。

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

只注册这一个服务器；不要同时注册原始的 codebase-memory-mcp 或 Serena 服务器。在小内存主机上，把 `--no-graph` 加入 `args`（此时搜索只使用字面文本回退）。本地索引并非端到端离线：返回给云端模型客户端的代码片段会离开本机。

## 在 Claude Code 和 Codex 中使用 SVWeave

SVWeave 通过 stdio 使用 MCP，因此任何 MCP 客户端都可以启动它。下面两种配置遵循两个客户端的文档化语法（在作者所安装的版本 Claude Code 2.1.299、Codex CLI 0.162.1 上，`claude mcp add --help` 和 `codex mcp add --help` 接受这些形式）。**它们没有被 SVWeave 的测试套件覆盖**：测试只通过 Python MCP 客户端驱动网关，因此没有针对特定客户端的资格认定证据。SVWeave 从不修改客户端的配置；这些命令由你自己运行。

请使用绝对路径：客户端会从它自己的工作目录启动该进程。

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

每个客户端都会在同一个工作区上启动**它自己的网关进程**。哪些共享、哪些不共享：

| 方面 | 行为 | 证据 |
|---|---|---|
| 读取 | 两个网关针对同一个已发布的构建并发响应读取查询，结果一致 | `tests/integration/test_api_parallel_reads.py`（API-PAR-03；使用 `--no-graph` 运行，因此不覆盖图后端） |
| 构建 | 每个工作区同一时间只能有一个构建，由跨进程的非阻塞 `build.lock` 强制保证。在同一个网关内，针对**同一个** profile 的第二次 `svw_build` 会并入正在运行的作业（`already_running: true`）。其他情况一律以 `RESOURCE_LIMIT` 拒绝，不会并入。你得到哪种详情取决于原因：持有 `build.lock` 的构建（另一个网关的 worker、shell 中的 `svweave build`）给出 `details.reason: build_in_progress`；同一网关内不同的 profile，或数据库中记录的另一个网关的排队中/运行中作业，给出 `details.active_job_id`（同一网关还有 `active_profile`），不带 reason。请稍后重试。窄竞态：如果 CLI 构建在 `svw_build` 的锁探测与作业启动之间获取了锁，该作业会以 `FAILED` 和 `RESOURCE_LIMIT` 结束 | `src/svweave/application/jobs.py`、`src/svweave/storage/lock.py`（`build_lock`）；`tests/unit/test_m3b_fixes.py::test_B4_*`（该竞态是通过阅读代码得出的，未经测试） |
| 编辑提交 | 由独占的工作区锁（`.svweave/lock`）串行化；如果另一个进程持有该锁，提交会（非阻塞地）退避，包括任何构建运行期间，因为构建以共享方式持有该锁 | `src/svweave/storage/lock.py`、`src/svweave/application/edit.py`（已阅读，未跨两个网关测试） |
| 内存 | 每个网关都可能启动自己的 Serena 和图后端（它们惰性启动），因此内存占用可能成倍增加 | `src/svweave/orchestration/registry.py`；主机限制见 `AGENTS.md` |

在小内存主机上，至少给其中一个客户端的 `args` 加上 `--no-graph`（此时搜索使用字面文本回退）。每个客户端中都**只**注册这一个服务器；不要同时注册原始的 codebase-memory-mcp 或 Serena 服务器（计划 6.1），那样会绕过快照绑定和信封。本地索引并非端到端离线：返回给云端模型客户端的代码片段会离开本机。

### 智能体能做什么、不能做什么

| 通过网关 | 智能体 |
|---|---|
| `svw_status`、`svw_search`、`svw_symbol`、`svw_references`、`svw_hierarchy`、`svw_connections`、`svw_assertions`、`svw_evidence` | 可以读取 |
| `svw_build`、`svw_job` | 可以启动并轮询构建（运行时状态位于 `.svweave/` 下） |
| `svw_edit` `prepare` / `validate` / `abort` | 可以提出并验证候选副本，**但前提是**在 `svweave.yaml` **和**受信策略中都启用了编辑（默认关闭） |
| 审批 | **不能**：没有 approve 工具，也没有审批字段。由人工在真实终端上运行 `svweave edit approve` |
| `svw_edit` `commit` | 若没有针对该确切补丁、快照和策略的有效审批记录，则被拒绝 |

**客户端边界。** Claude Code 和 Codex 有它们自己的 shell 和文件工具。这些工具可以在 SVWeave 之外修改源码树，而以你的 UID 运行的智能体可以读取 `approval.key` 并伪造审批。SVWeave 的保证（compare-and-set、审批绑定、日志）只覆盖**通过网关**进行的操作；`svweave doctor` 始终报告 `client_boundary: not
qualified`。若要获得有约束力的审批，请让智能体在单独的 UID 下运行，或使用操作系统层面的限制，禁止其访问源码树和受信目录；参见 `docs/security.md` 第 4 节。

### 推荐的智能体工作流

1. 用 `svw_status` 查看后端健康状况，以及每个 profile 的当前构建及其新鲜度。
2. `svw_build {profile}` 立即返回 `job_id`。轮询 `svw_job {job_id}`，直到 `lifecycle` 为 `SUCCEEDED`（或 `FAILED`、`TIMED_OUT`、`CANCELLED`），然后读取 `data.result`：
   `completeness`（`COMPLETE`、`PARTIAL`、`BLOCKED`）、`diagnostic_outcome`、`freshness`、
   `published`。存在错误的设计即使作业成功，仍是 `PARTIAL`；不要把它当作干净的构建。`PARTIAL` 构建有可查询的 `build_id`；在构建被存储之前就失败的 `BLOCKED` 作业（缺失输入、不支持的选项）则没有 `build_id`（见[构建流程](#构建流程)）。
3. 使用结果中的 `build_id` 进行查询（`svw_symbol`、`svw_references`、`svw_hierarchy` 等）。
   要遵循 `analysis_coverage` 和 `freshness`；在 PARTIAL、STALE 或被截断的结果中查不到某项，不能证明它不存在。
4. 可选：先 `svw_edit` `prepare`，再 `validate`，后者会返回 diff 和 `patch_hash`。
5. 人工审阅后，在真实终端上运行 `svweave edit approve --transaction T --patch-hash H`。然后 `svweave edit commit --transaction T --patch-hash H` 应用它（同样的提交也可通过 `svw_edit` `commit` 完成，它仍然需要审批记录）。这些标志以 `svweave edit approve --help` 和 `svweave edit commit --help` 为准；完整流程见[受控编辑工作流](#受控编辑工作流)。

## 一次变更如何流转

演示流程（计划 1.3）确实调用了全部三个后端；没有任何东西被模拟图、正则符号查找或固定 JSON 替代。第 1 至 5 步是只读分析；第 6 至 11 步是受控编辑循环，默认关闭。

<p align="center"><img src="docs/images/demo-flow.svg" alt="十一步流水线：加载 profile、不可变快照与构建、后端健康探测、图找出候选文件（SOURCE_CANDIDATE）、slang 符号/层次结构/引用（HDL_STATIC_RESOLVED）、Serena 编辑候选副本（LSP_RESOLVED）、slang 重新验证候选副本、diff 以及 EDA、仿真和形式验证为 NOT_RUN 的开放限制、在 TTY 上带哈希前缀的人工审批、带日志的 compare-and-set 提交，以及应用后的快照与构建（旧构建变为 HISTORICAL）。" width="100%"></p>

*每一行都标明了做这项工作的后端（实心圆点）、触发它的命令，以及它产出的证据类型。图只提出候选文件；slang 针对具名构建作答；Serena 只编辑候选副本；在人工看到 diff 之前，slang 会对基线和候选副本再次验证。审批、compare-and-set 提交和应用后的构建都是网关步骤。证据类型不是排名（见[测试与资格认定](#测试与资格认定验证)）。*

每个事务都要经过一个状态机，其合法转换是 `src/svweave/editing/machine.py` 中的一张表；任何其他转换都是 `INTERNAL` 错误。

<p align="center"><img src="docs/images/edit-state-machine.svg" alt="状态机：正常路径为 PREPARED、CANDIDATE_READY、VALIDATING、VALIDATED、AWAITING_APPROVAL、APPROVED、COMMITTING、APPLIED、POST_APPLY_VERIFIED。APPROVED 和 COMMITTING 可以转到 RECOVERY_REQUIRED，后者可解决为 APPLIED、ABORTED 或 CONFLICT。APPLIED 之前的任何状态都可以转到 ABORTED、EXPIRED、CONFLICT 或 VALIDATION_FAILED。post_apply_status 为 FAILED 的 APPLIED 仍停留在 APPLIED。边上的关卡 A 至 G 分别对应审批 HMAC 与过期、config_id 复核、工作区 compare-and-set、逐文件 before-hash 以及日志 fsync。" width="100%"></p>

*正常路径为蓝色。`APPROVED` 在日志已写入但 `COMMITTING` 尚未记录之前发生崩溃时，可转到 `RECOVERY_REQUIRED`；`COMMITTING` 只能完成（`APPLIED`）或需要恢复。应用后检查失败时，事务保持 `APPLIED`，并带有 `post_apply_status: FAILED`，绝不会变成 `POST_APPLY_VERIFIED`。徽标 A 至 G 标示每次转换上强制执行的关卡（审批 HMAC 与过期、`config_id` 复核、工作区 compare-and-set、逐文件 before-hash、日志 fsync）。命令的逐步演练见[受控编辑工作流](#受控编辑工作流)。*

## 构建流程

`svweave build --profile P` 和 MCP 工具 `svw_build` 运行同一个用例（`src/svweave/application/build.py`，`run_build`）。CLI 在前台运行它；网关则在一个后台线程中运行它并返回 `job_id`。

<p align="center"><img src="docs/images/build-flow.svg" alt="svweave build 的十个阶段：加载并验证配置、将清单解析为 config_id、捕获不可变快照得到 snapshot_id、计算标识并检查是否复用、在 bwrap 内运行 pyslang worker、提取语义事实、检查依赖泄漏、导入暂存区并判定完整性、最后发布指针、报告信封。下方有三个结果框：COMPLETE 且 NO_ERRORS 会被存储并移动指针；PARTIAL，或因依赖泄漏而 BLOCKED，按 build_id 存储但不移动指针，退出码为 3，而缺失输入或不支持的选项则为 BLOCKED 且不存储任何内容；worker 超时、崩溃或内存被杀不存储任何行，按错误码退出 4，并保留旧构建。" width="100%"></p>

*第 5 和第 6 阶段（蓝色）在沙箱化的 pyslang worker 中运行；其余都是网关用例。发布指针最后写入，与构建行处于同一个事务中。*

### 分步说明

整个构建在 `.svweave/` 中的独占、非阻塞 `build.lock` 下运行。同一工作区中的第二次构建会立即以 `RESOURCE_LIMIT` 失败（`details.reason: build_in_progress`），退出码 4。下面的编号与示意图一致。

1. **加载并验证** `svweave.yaml`、可选的本地覆盖文件（主机根映射，不在 Git 中）以及位于仓库之外的受信 `policy.json`。错误的输入会在其他任何事情发生之前以 `INVALID_CONFIG` 失败，退出码 2。
2. **解析清单**（`src/svweave/manifest/resolver.py`）：把 filelist、include 目录、define、顶层模块和顶层参数展开为一份规范化的清单，以及一份记录每个 slang 参数来源的参数账本。它的哈希就是 `config_id`。适配器无法遵守的选项会被拒绝（`UNSUPPORTED_OPTION`），绝不会被忽略。
3. **捕获不可变快照**（`src/svweave/snapshot/capture.py`），期间持有共享的工作区锁：枚举每个输入，对其字节做哈希（sha256），复制到 `.svweave/snapshots/<snapshot_id>/files`，并记录 include/库的搜索空间（这样，新增一个会改变所选头文件的文件，也会改变该 id）。捕获期间会重新检查工作区；如果它持续变化，构建会以 `WORKSPACE_UNSTABLE`（退出码 5）停止。通过 MCP 传入 `expected_snapshot_id` 时，不匹配会以 `STALE_SNAPSHOT` 停止。
4. **计算标识并检查复用。** `toolchain_id` 来自锁定的锁文件；`build_id = SHA-256(snapshot_id : config_id : toolchain_id : schema version)`。如果存在具有此 `build_id` 的 COMPLETE 构建，且未给出 `--no-reuse`，则返回已存储的结果（`reused: true`），并且只有当该构建仍为 CURRENT 时，指针才会移动到它。给出 `--no-reuse` 且已存在 COMPLETE 构建时，已存储的构建是不可变的：worker 会再次运行，并把它的行、完整性和诊断结果与已存储的进行比较。一致则返回已存储的构建，带 `verified_rerun: true`，且不发布任何新内容；不一致则为 `INTERNAL`（退出码 4），已存储的构建不会被替换。
5. **运行 pyslang worker**（`src/svweave/adapters/slang/runner.py`），位于 bubblewrap 内：快照的只读视图、私有的 `/tmp`、无网络、由 `analysis.max_worker_memory_mib` 决定的 `RLIMIT_AS`、墙钟超时（`analysis.max_build_seconds`），以及它自己的进程组，使超时或取消能杀死整棵进程树。没有 bwrap 时，构建会失败即拒绝（fail closed，`BACKEND_UNAVAILABLE`），除非你选择启用降级模式，而该模式会被如实报告。
6. **提取**声明、引用、实例、端口绑定、SVA 清单和诊断。worker 打印一个 JSON 文档，网关根据 schema 对其进行验证。
7. **依赖泄漏检查。** 如果 slang 读取了快照和适配器自身文件之外的任何内容，构建即为 `BLOCKED`：判定结果被存储，所有语义行被扣留，也不发布任何内容。
8. **导入**暂存区并确定结果（`src/svweave/adapters/slang/ingest.py`）。当任何因素削弱了结论时，构建为 `PARTIAL`：带错误的诊断、未解析或不支持的引用、声明的 stub 或 blackbox 边界、项目文件中不支持的节点、slang 未暴露的端口关联。否则为 `COMPLETE`。诊断结果（`NO_ERRORS`、`HAS_ERRORS` 等）单独报告。
9. **发布。** 构建行和计数在同一个事务中写入并经过验证（暂存的行必须与导入的计数相符）；该 profile 的"当前构建"指针最后移动，并且**仅对 COMPLETE 构建**移动。`PARTIAL` 构建，以及因依赖泄漏而 `BLOCKED` 的构建，会被存储并可通过 `build_id` 寻址，但绝不会成为当前构建。失败的 worker（超时、崩溃、取消、内存被杀、输出错误）不存储任何语义行；在分析之前就失败的构建（`MISSING_INPUT`、`UNSUPPORTED_OPTION`）不存储任何内容，也没有 `build_id`。在所有失败情形下，最近一个可用的构建都保持为当前构建。
10. **报告。** 信封携带 `outcome`、`analysis_coverage`（完整性、范围、限制）、`freshness`、`provenance`、计数，以及一个 `run_dir`（`.svweave/runs/<job_id>/`），其中存放 `argument_ledger.json`、`snapshot_inventory.json`、`dependency_report.json`、`diagnostics.json`、`semantic_summary.json`、`result.json` 等。

### 结果与退出码

| 结果 | 完整性 / outcome | 指针 | CLI 退出码 |
|---|---|---|---|
| 干净的设计 | `COMPLETE`、`NO_ERRORS`、outcome `OK` | 已移动（或已是当前） | 0 |
| 有错误的设计，或有声明的边界 | `PARTIAL`（作业仍为 `SUCCEEDED`），错误 `PARTIAL_ANALYSIS` | 未移动 | 3 |
| 依赖泄漏 | `BLOCKED`；判定结果按其 `build_id` 存储，语义行被扣留 | 未移动 | 3 |
| 缺失输入或不支持的选项（`MISSING_INPUT`、`UNSUPPORTED_OPTION`） | outcome `BLOCKED`；不存储任何内容，无 `build_id` | 未移动 | 3 |
| worker 超时、崩溃、输出错误、无沙箱 | 作业 `TIMED_OUT` 或 `FAILED`；构建结果的 outcome 为 `ERROR`，完整性为 `BLOCKED`；不存储任何行 | 未移动 | 4（按错误码） |
| worker 因内存被杀 | `RESOURCE_LIMIT`；以同样的限制重试没有帮助 | 未移动 | 4 |
| 另一个构建正在运行 | `RESOURCE_LIMIT`（详情见[同时使用两者](#同时使用两者)） | 未移动 | 4 |
| 已取消（仅 MCP） | 作业 `CANCELLED`；作业信封的顶层 `outcome` 为 `BLOCKED`，`data.result.outcome` 为 `ERROR`。CLI 没有取消路径：Ctrl-C 会中止进程 | 未移动 | 不适用 |
| 捕获期间工作区变化，或 `expected_snapshot_id` 不匹配 | `WORKSPACE_UNSTABLE` / `STALE_SNAPSHOT` | 未移动 | 5 |
| 配置错误 | `INVALID_CONFIG` | 未移动 | 2 |

退出码 0 表示命令成功，而不是设计"PASS"。PARTIAL 结果可以使用，但其中查不到的内容并不能证明不存在：请阅读 `analysis_coverage.limitations` 和 `data.reasons`。

### MCP 变体

`svw_build {profile}` 会同步验证 profile，登记一个 `QUEUED` 作业并立即返回其 `job_id`（如果同一 profile 已在此网关中构建，则返回 `already_running: true`）。轮询 `svw_job {job_id}`；`lifecycle` 依次变为 `QUEUED`、`RUNNING`，然后是 `SUCCEEDED`、`FAILED`、`TIMED_OUT` 或 `CANCELLED`，完成的作业在 `data.result` 中携带结果摘要（`outcome`、`build_id`、`completeness`、`diagnostic_outcome`、`freshness`、`published`、`reused`、`counts`、`error`）。`SUCCEEDED` 的作业仍可能是 `PARTIAL`。带 `action: cancel` 的 `svw_job` 会杀死 worker 进程树。没有队列，也没有重试：在另一个构建处于活动状态（不同的 profile，或由另一个进程启动）时请求构建，会以 `RESOURCE_LIMIT` 被拒绝（只有持有 `build.lock` 时才有 `details.reason: build_in_progress`；见[同时使用两者](#同时使用两者)）。

### 实例演练：随附的演示

请在仓库的 `svweave.yaml`、`fixtures/` 和 `toolchain.lock.json` 的一份一次性副本中运行（不要在仓库本身中运行），并先运行 `svweave init` 和 `svweave manifest resolve --profile
rtl_demo`。下面的 JSON 经过缩略（列表被截断，部分字段被省略）；其中的值是该次运行的真实输出，而 id、耗时和 RSS 会因主机和检出版本而异。

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

原样再次运行同一条命令，没有做新的分析：输出报告了 `"reused": true` 和 `"published": true`，退出码为 0（示意图的第 4 阶段）。`--no-reuse` 不会替换已存在的 COMPLETE 构建：它会重新运行 worker 并做比较。在同一份演示副本上实测，`svweave build --profile rtl_demo --no-reuse` 退出码为 0，带有 `verified_rerun: true`、`reused: false`、`published: true` 以及相同的 `build_id`（行为见 `src/svweave/application/build.py` 中的 `_verified_rerun`）。输出中的各个标识即[构建标识与状态](#构建标识与状态)中所述的那些。把 `build_id` 带入[快速开始](#快速开始)中的查询，例如 `svweave query hierarchy --build BUILD_ID`。

## 构建标识与状态

<p align="center"><img src="docs/images/identity-model.svg" alt="snapshot_id、config_id、toolchain_id 和语义 schema 版本合成 build_id；job_id 是每次尝试的 uuid。五个正交的状态维度：作业生命周期、完整性、新鲜度、诊断结果和后端可用性，及其取值。插图表明：新增 dir_a/common.svh 会改变所选的头文件，尽管 dir_b/common.svh 没有变化，因此查找清单是 snapshot_id 的一部分。" width="100%"></p>

*`build_id` 是 `snapshot_id : config_id : toolchain_id : semantic schema version` 的 SHA-256；输入改变会产生新的构建，而重试会保持 `build_id` 不变但获得新的 `job_id`。五个状态维度分别报告，绝不会合并成单一结论：存在错误的设计是作业 `SUCCEEDED`、分析 `PARTIAL`、诊断 `HAS_ERRORS`。插图说明了为什么查找搜索空间是 `snapshot_id` 的一部分：在 include 顺序为 `dir_a, dir_b` 时，新增 `dir_a/common.svh` 会改变所选的头文件，尽管 `dir_b/common.svh` 没有被改动。*

## 配置

SVWeave 读取三个文件。`svweave.yaml` 和 `svweave.local.yaml` 只是数据：其中的任何内容都不会被执行，未知字段会被拒绝，重复的 YAML 键也会被拒绝。

| 文件 | 作用 | 位置 |
|---|---|---|
| `svweave.yaml` | 项目、源码根、profile、分析和编辑设置 | 仓库根目录 |
| `svweave.local.yaml` | 外部根的主机映射（不在 Git 中） | 与 `svweave.yaml` 相邻 |
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

随附的文件还定义了 `rtl_demo_l4`（相同的源码，`LANES` 为 4，因此 `config_id` 不同）和 `uvm_smoke`（需要 `uvm` 根）。每个文件的 JSON Schema 都在 `schemas/` 中。

</details>

**`svweave.local.yaml`** 把外部根映射到主机路径，例如 `bootstrap_locked.py` 放在 `.toolchain/` 下的固定版本 UVM 库：

```yaml
schema_version: "1.0"
root_mappings:
  - id: uvm
    path: .toolchain/uvm-core/src
```

外部根中的文件以 `@uvm/relative/path` 的形式寻址。使用未映射的外部根会产生 `MISSING_INPUT` 阻塞（`reason: root_unmapped`），因此在映射存在之前，`uvm_smoke` profile 一直是 BLOCKED。

**受信策略。** `policy.json` 位于 `$XDG_CONFIG_HOME/svweave/workspaces/<workspace_key>/policy.json`（回退到 `~/.config`），其中 `workspace_key` 是 `sha256(realpath(dir of svweave.yaml))` 的前 16 个十六进制字符。`svweave init` 会创建它，且从不覆盖它。它刻意放在仓库之外：仓库内容（以及智能体写入其中的任何内容）不得能够扩大它自己的权限。仓库文件只能收紧策略；仓库本地的策略文件会被忽略，而位于项目树内的 `--policy` 路径会产生 `POLICY_DENIED`。

| 字段 | 默认值 | 含义 |
|---|---|---|
| `schema_version` | 必填 | `"1.0"` |
| `allowed_roots` | 必填，绝对路径 | SVWeave 可以读取的根 |
| `writable_paths` | `[]` | 编辑可以写入的路径（仓库的 `editing.allowed_paths` 必须保持在其内部） |
| `approval_required` | `true` | 必须有人工审批 |
| `network_enabled` | `false` | 后端无网络 |
| `editing_enabled` | `false` | 总开关；编辑还需要 `svweave.yaml` 中的 `editing.enabled` |
| `allow_test_approvals` | `false` | 接受未经资格认定的测试令牌审批路径；只有人工编辑此文件才能设置 |

同一个受信目录还存放 `approval.key`（模式 0600）、`cursor.key`、审批记录以及最近一次后端探测记录。

<details>
<summary>Filelist 方言 <code>svweave-filelist-v1</code> 简述（完整规则：<code>docs/manifest.md</code>）</summary>

| 支持 | 说明 |
|---|---|
| 普通源文件路径、`-f FILE`、`-F FILE` | `-f` 相对于调用时的 cwd 解析嵌套的相对路径，`-F` 则相对于该 filelist 所在目录；嵌套深度最大 32，会检测循环 |
| `+incdir+a+b`、`-I DIR`、`-IDIR` | include 目录，保持顺序，绝不排序或去重 |
| `+define+N=V+M`、`-DN=V` | 宏文本逐 token 精确；`NAME`、`NAME=` 和 `NAME=V` 各不相同；冲突的重定义是 `INVALID_CONFIG` |
| `-v FILE`、`-y DIR`、`+libext+.v+.sv` | 库文件、库目录、库扩展名 |
| 路径 token 中的 `$VAR`、`${VAR}` | 仅当该变量在 `environment_allowlist` 中且已设置，否则为 `MISSING_INPUT` |
| `-l`、`-logfile`、`-nologo`、`-quiet`、`-q` | 仅与输出相关的选项：被丢弃并记录为已忽略 |

| 拒绝 | 结果 |
|---|---|
| 任何其他 `-x` 或 `+x` 选项 | `UNSUPPORTED_OPTION`，构建为 BLOCKED（绝不会被悄悄丢弃） |
| 反引号、`$(...)`、未加引号的分号、管道符、尖括号和 & 符号 | `UNSUPPORTED_OPTION`（`shell_syntax`）；不执行任何内容 |
| 块注释（`/* ... */`）、续行、拆成两个参数的 `-D NAME=V` | 不支持 |
| 缺失的源文件、include 目录、filelist、生成的输入 | `MISSING_INPUT` |

</details>

## CLI 参考

全局选项（位于命令之前）：`--root DIR`（包含 `svweave.yaml` 的项目目录）、
`--manifest FILE`、`--policy FILE`、`--local FILE`、`--request-id TEXT`、`--compact`。每个命令都打印一个 JSON 信封。

<details>
<summary>所有命令</summary>

| 命令 | 用途 |
|---|---|
| `svweave init` | 创建 `svweave.yaml` 模板、`.svweave/`，并在受信目录中创建 `policy.json`；从不覆盖 |
| `svweave doctor [--probe]` | 配置、策略、后端和客户端边界检查；`--probe` 对每个后端运行一次真实的 fixture 查询（慢） |
| `svweave manifest resolve [--profile P]` | 把 profile 解析为规范化清单和参数账本 |
| `svweave snapshot create [--profile P]` | 捕获不可变快照 |
| `svweave snapshot check --snapshot S` | 将快照与工作区比较（STALE 时退出码为 5） |
| `svweave build [--profile P] [--no-reuse]` | 解析、快照并用 slang 分析 |
| `svweave query symbol --build B` | 通过 `--symbol`、`--file` + `--byte` 或 `--name` 查找声明 |
| `svweave query references --build B --symbol ID` | 已解析和未解析的出现位置，附带搜索范围 |
| `svweave query hierarchy --build B` | 实例树（`--top`、`--instance`、`--depth`） |
| `svweave query connections --build B --instance I` | 直接的形参/实参端口关联（`--port`） |
| `svweave query assertions --build B` | 静态 SVA 清单（`--kind` 为 `source`、`bind` 或 `elaborated`）；执行状态始终为 `NOT_RUN` |
| `svweave query diagnostics --build B` | 编译器诊断 |
| `svweave query evidence` | 静态记录和导入的证据（`--build` 或 `--evidence`、`--type`） |
| `svweave query search --snapshot S --query Q` | 图候选或带标记的文本回退（`--scope`、`--no-graph`） |
| `svweave report --build B` | 由真实数据填充的状态报告；未评估的关卡会如实说明 |
| `svweave evidence import --manifest FILE` | 导入外部证据（以 `EXTERNAL_DECLARED` 存储） |
| `svweave cache gc [--no-dry-run]` | 对快照做垃圾回收；保留最近 3 个及被引用的快照；默认为试运行 |
| `svweave debug bundle --build B [--out DIR]` | 复现包：哈希、清单、诊断摘要、脱敏后的路径 |
| `svweave schema emit [--check] [--out DIR]` | 写入或验证 `schemas/*.schema.json` |
| `svweave serve --transport stdio [--workspace DIR] [--no-graph]` | stdio 上的 MCP 服务器 |
| `svweave edit prepare --spec FILE` | 创建候选副本并在其中应用编辑 |
| `svweave edit validate --transaction T` | 对基线和候选副本运行 slang；diff 和补丁哈希 |
| `svweave edit approve --transaction T --patch-hash H [--ttl-minutes N]` | 在 TTY 上的人工审批 |
| `svweave edit commit --transaction T --patch-hash H [--request-key K]` | compare-and-set 应用、日志、应用后构建 |
| `svweave edit abort --transaction T` | 移除候选副本，保留审计记录 |
| `svweave edit recover --transaction T` | 根据日志解决被中断的提交（`--roll-forward`、`--rollback` 或 `--accept-current`） |
| `svweave edit show --transaction T` | 事务的状态和审计轨迹 |

</details>

每个语义 `query` 命令和 `report` 都绑定到一个不可变的 build id。

| 退出码 | 含义 |
|---|---|
| 0 | 该命令成功（不是"设计 PASS"） |
| 2 | 用法、schema 或配置错误（`INVALID_CONFIG`） |
| 3 | 构建错误、PARTIAL 或 BLOCKED 结果、缺失输入、有歧义的符号 |
| 4 | 后端不可用、能力不匹配、资源限制、超时、内部错误 |
| 5 | 状态陈旧或冲突（`STALE_SNAPSHOT`、`EDIT_CONFLICT`、`RECOVERY_REQUIRED` 等） |
| 6 | 策略或审批拒绝 |

**`--allow-stale` / `--allow-partial`**（用于语义 `query` 命令）：对新鲜度为 `STALE` 或 `HISTORICAL` 的构建进行查询，退出码为 5；对 `PARTIAL` 构建进行查询，退出码为 3，因此脚本不会把它误当成干净的答案。这两个标志会把这两个退出码变为 0；无论如何，JSON 结果都会携带 `freshness` 和 `analysis_coverage`，因此状态绝不会被隐藏。

## MCP 工具参考

`svweave serve --transport stdio` 是唯一的外部入口。会话绑定到一个工作区；没有任何工具接受文件系统根。完整契约：`docs/mcp_contract.md`；schema：
`schemas/mcp/<tool>.input.json` 和 `.output.json`。

<details>
<summary>11 个工具</summary>

| 工具 | 必填输入 | 主要输出 | 只读 |
|---|---|---|---|
| `svw_status` | 无（`profile`、`probe` 可选） | 后端健康状况与能力、每个 profile 的当前构建与新鲜度、待处理的编辑、隔离、客户端边界 | 否（`probe:true` 会启动进程） |
| `svw_build` | `profile` | 立即返回 `job_id`；同一时间只有一个构建 | 否 |
| `svw_job` | `job_id`（`action` = `status` 或 `cancel`） | 作业生命周期、进度、结果摘要 | 否 |
| `svw_search` | `snapshot_id`、`query` | 图候选或文本回退（`SOURCE_CANDIDATE`） | 否（可能写入派生的索引缓存） |
| `svw_symbol` | `build_id` + `symbol_id`、`location{file,byte}`、`name` 之一 | 声明，或带候选项的 `AMBIGUOUS_SYMBOL` | 是 |
| `svw_references` | `build_id`、`symbol_id` | 已解析和未解析的出现位置 | 是 |
| `svw_hierarchy` | `build_id`（`instance_id`、`instance_path`、`top` 之一，可选） | 前序遍历的实例树 | 是 |
| `svw_connections` | `build_id` + `instance_id` 或 `instance_path` | 直接的形参/实参关联 | 是 |
| `svw_assertions` | `build_id` | 静态 SVA 清单 | 是 |
| `svw_evidence` | `build_id` 或 `evidence_id` | 静态记录、导入的记录、按类型划分的状态 | 是 |
| `svw_edit` | `action` = `prepare`、`validate`、`commit` 或 `abort`，外加各 action 对应的字段 | 事务状态、diff、补丁哈希 | 否（destructive 注解只是提示） |

`svw_edit` 不能审批：没有审批字段，而且 schema 禁止未知字段。注解只是提示，不是访问控制。

</details>

**信封。** 每个结果，无论成功或失败，都是一个以 MCP `structuredContent` 和文本块两种形式交付的信封：`schema_version`、`request_id`、`outcome`
（`OK`、`PARTIAL`、`BLOCKED` 或 `ERROR`）、`workspace_id`、`snapshot_id`、`build_id`、`profile`、`freshness`
（`CURRENT`、`STALE`、`HISTORICAL` 或 `UNKNOWN`）、`analysis_coverage`（`completeness`、`scope`、`basis`、
`unresolved_count`、`limitations`）、`provenance`（`backend`、`toolchain_id`、`evidence_kind`）、
`data`、`pagination`（`limit`、`next_cursor`、`truncated`）、`warnings` 和 `error`（`code`、
`message`、`details`）。在 `PARTIAL`、`STALE`、`HISTORICAL` 或 `truncated=true` 的情况下，页面中缺失的条目不能证明其不存在。

<details>
<summary>错误目录</summary>

| 错误码 | 含义 | CLI 退出码 |
|---|---|---|
| `INVALID_CONFIG` | 配置、schema 或参数违规，游标错误或来自别处 | 2 |
| `MISSING_INPUT` | 未知的构建、快照、作业或证据；缺失文件或根映射 | 3 |
| `UNSUPPORTED_OPTION` | slang 适配器无法遵守的选项 | 3 |
| `PARTIAL_ANALYSIS` | 部分结果 | 3 |
| `AMBIGUOUS_SYMBOL` | 多个带作用域的候选项 | 3 |
| `PATH_NOT_ALLOWED` | 路径位于允许的根之外、违反 include/exclude 规则或符号链接目标规则 | 6 |
| `POLICY_DENIED` | 策略拒绝该操作 | 6 |
| `APPROVAL_REQUIRED`、`APPROVAL_EXPIRED` | 审批缺失、被伪造或已过期 | 6 |
| `WORKSPACE_UNSTABLE`、`STALE_SNAPSHOT` | 捕获期间工作区变化，或 `expected_snapshot_id` 不匹配 | 5 |
| `EDIT_CONFLICT`、`RECOVERY_REQUIRED`、`IDEMPOTENCY_CONFLICT` | 编辑状态冲突 | 5 |
| `BACKEND_UNAVAILABLE`、`CAPABILITY_MISMATCH` | 后端缺失，或工具链/能力与锁文件不一致 | 4 |
| `BUILD_TIMEOUT`、`RESOURCE_LIMIT`、`OUTPUT_LIMIT`、`INTERNAL` | 超时、限制（包括已有另一个构建处于活动状态）、意外异常 | 4 |

每个错误码的确切退出码定义在 `src/svweave/domain/errors.py` 中；`docs/mcp_contract.md` 解释了三种结果类型（协议错误、工具执行错误、合法的 PARTIAL 结果）。

</details>

**分页与游标。** `limit` 默认为 100，最大为 500（`svw_symbol` 默认为 50）。响应上限为 128 KiB：超出上限的页面会被截断，`pagination.truncated=true` 并设置 `next_cursor`（绝不会出现无效 JSON）。游标是 `base64url(json).hmac`，用工作区的 `cursor.key` 签名，并绑定 build 或 snapshot id、查询的哈希（不含 `limit`/`cursor`）、排序名称和偏移量。来自另一个构建、查询、排序顺序或工作区的游标，或被篡改的游标，会以 `INVALID_CONFIG` 被拒绝。越过末尾的空页并不是对其他查询的论断。超过 64 KiB 的请求、未知字段以及文本参数中的控制字符，会在触及任何后端之前就被拒绝。

## 受控编辑工作流

编辑**默认关闭**。它需要受信 `policy.json` 中的 `editing_enabled: true`，**并且**需要 `svweave.yaml` 中的 `editing.enabled: true`、一个 `writable: true` 且被策略的 `writable_paths` 所覆盖的源码根，以及 `editing.allowed_paths`。详情：`docs/editing.md`。

1. **准备（Prepare）。** 写一个绑定基础构建和快照的请求：

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

   一个编辑可以是 `symbol_replacement`（声明 id，绝不是裸名称）、`slang_symbol`
   （按 `qualified_name`（例如 `pkg::cls::run_phase`）或 `symbol_id` 定位的 task、function 或构造函数，以整个新声明作为 `replacement`，并带可选的 `expected_sha256`；有歧义或未知的名称会被拒绝，ADR-013），或者 `exact_range`
   （`logical_path`、`start_byte`、`end_byte`、`expected_sha256`、`replacement`）。基础构建必须是 COMPLETE 且 CURRENT。

   ```bash
   svweave edit prepare --spec spec.json            # real byte copy under .svweave/candidates/<txn>
   ```

2. **验证（Validate）。** slang 对每个 validate profile 的基线和候选副本运行；两者都必须是 `NO_ERRORS` 且 `COMPLETE`，并且没有新的未豁免警告。结果包括统一 diff、逐文件哈希、`patch_hash`、诊断差异以及限制（EDA、仿真和形式验证为 `NOT_RUN`）。

   ```bash
   svweave edit validate --transaction TXN
   ```

3. **审批（Approve，由人在真实终端中完成）。** diff 显示在 stderr 上，你需要输入补丁哈希的前 12 个字符。该记录绑定补丁哈希、基础快照、profile 集合、策略哈希、每个 profile 的 `config_id`、事务 id 和过期时间（默认 30 分钟），并使用 `approval.key` 以 HMAC-SHA256 签名。没有 MCP 的 approve 工具。

   ```bash
   svweave edit approve --transaction TXN --patch-hash HASH
   ```

4. **提交（Commit）。** 验证审批，重新解析每个 validate profile（`config_id` 不变），重新检查路径策略和工具链，获取独占的工作区锁，将工作区与基础快照比较，写入预写日志（fsync），然后逐文件检查 before-hash 并原子地替换。之后会检查字节、工作区快照 id 以及每个 validate profile 的一次全新构建；只有这时旧构建才变为 `HISTORICAL`，状态变为 `POST_APPLY_VERIFIED`。如果某项检查失败，状态保持 `APPLIED`，并带有 `post_apply_status: FAILED`。重新发送提交会返回最初的结果。

   ```bash
   svweave edit commit --transaction TXN --patch-hash HASH
   svweave edit show --transaction TXN              # state and audit trail
   ```

5. **中止或恢复。**

   ```bash
   svweave edit abort --transaction TXN             # candidate removed; refused while committing or applied
   svweave edit recover --transaction TXN           # after an interrupted commit
   ```

   恢复会对每个文件分类：当前字节等于 after-hash 的为*已应用*，等于 before-hash 的为*未应用*，其他情形为*冲突*。它绝不会覆盖不是自己写入的字节；冲突的文件保留人工的版本，候选版本则放入 `.svweave/recovery/<txn>/`。当每个文件都一致，或通过显式的 `--roll-forward`、`--rollback` 或 `--accept-current` 时，该阻塞会被解除。绝不使用 `git reset` 或 `git checkout`。

**被拒绝的内容：** 用裸名称代替声明 id（`AMBIGUOUS_SYMBOL`）；写在宏体中的声明，以及对 `` `define `` 指令的编辑（`macro_definition_edit_unsupported`）；含有终端控制字符、DEL 或双向覆盖字符的替换文本；添加或更改诊断控制指令的 diff；对 filelist 和生成的输入的编辑；重命名、删除、新文件、二进制文件、符号链接和硬链接目标；位于 `allowed_paths` 之外，或超过 `max_files_per_patch` / `max_changed_lines` 的文件；同一文件中同时存在 Serena 编辑和 exact-range 编辑。Serena 仅对 SystemVerilog 的 `function` 声明执行语义编辑（ADR-012）；task 和构造函数通过 `slang_symbol` 按名称编辑，由 slang 解析，网关作为带标签的 exact-range 补丁应用（ADR-013）；显式范围使用 `exact_range`。多个文件的替换不是一个原子事务；日志和恢复正是因此而存在。

如果智能体以你的 UID 运行并拥有不受限制的 shell，审批可能被伪造：在任何重要场合启用编辑之前，请阅读 `docs/security.md`。

## 工具链与后端

一切都固定在 `toolchain.lock.json` 中（每个制品的确切版本、URL 和 SHA-256，外加用于网关 venv 的哈希固定 wheelhouse）。没有任何东西从"latest"安装，工具链或能力不匹配即为 `CAPABILITY_MISMATCH`。

| 组件 | 版本 |
|---|---|
| pyslang / slang | 12.0.0 / v12.0 |
| codebase-memory-mcp | v0.11.0（`linux-amd64-portable`） |
| Serena（`serena-agent`） | v1.7.0（commit 949a27ef1e5f）；升级会改变其许可证（ADR-010） |
| Verible | v0.0-4296-g0f262651 |
| MCP Python SDK | 2.3.0 |
| Accellera UVM（`uvm-core`） | 2020.3.1 |

**引导安装（Bootstrap）。** 联网，一次性：`scripts/bootstrap_locked.py` 下载每个制品，使用前校验其 SHA-256，在防路径穿越保护下解压，并根据锁定的 `uv.lock` 构建 Serena venv。`--verify-only` 在不联网的情况下将已安装状态与锁文件比较。对于离线机器，先用 `scripts/bootstrap_locked.py --fetch-only` 准备缓存，复制仓库和 `.toolchain/`，然后运行
`scripts/bootstrap_locked.py --offline --create-venv --venv-dir .venv`，最后以 `--verify-only` 收尾。全新环境（clean-room）的离线运行由 `tests/integration/test_dep01_cleanroom.py` 测试（在构建主机上做了网络隔离，而不是在单独的物理隔离机器上）。`scripts/gateway_lock_refresh.py` 是维护者工具，用于重新生成锁文件中的网关 wheelhouse 部分。流程：`docs/install.md`。

**沙箱。** 每个后端，包括 pyslang worker，都通过 `bwrap` 参数数组启动：系统和工具链只读，只对其候选副本（如适用）和自己的状态目录可读写，`--unshare-net`，拥有随网关一同终止的独立 PID 命名空间，且没有真实的 `$HOME`。原始源码不以可写方式挂载，或者根本不挂载。`bwrap` 不施加 CPU、内存或 pid 限制，也没有 seccomp 过滤器；只有 HDL worker 有内存限制（`RLIMIT_AS`）。如果无特权用户命名空间不可用，后端会失败即拒绝（fail closed），或只在明确标记的降级模式下运行（ADR-011）。

**glibc 说明。** 标准的 codebase-memory-mcp 二进制文件需要 glibc 2.38，在较旧的主机（如 glibc 2.28 的 Enterprise Linux 8）上会失败；锁文件固定了可移植（静态）构建，它可以在那里运行。未重新探测并更新锁文件和许可证审查之前，不要更换该二进制文件。

**READY 还是 DEGRADED。** `svweave doctor` 不会启动后端；`svweave doctor --probe`（或 `scripts/probe_backends.py`，它会写入 `.svweave/probe/capability_report.json`）会启动每个真实的固定版本后端，记录协商的协议、工具列表和 schema 哈希，并运行一次真实的 fixture 查询。后端绝不会仅因进程已启动就成为 READY：READY 要求具备预期的工具，并对 fixture 查询给出正确的答案。已记录且被接受的缺口使其保持为 `DEGRADED`（在 2026-10-14 的运行中，codebase-memory-mcp 和 Serena 为 `DEGRADED`）；缺失或失败的后端为 `UNAVAILABLE`。回退绝不会把失败的必需后端变成成功：搜索会回退到带标记的字面文本搜索，outcome 为 `PARTIAL`。

## 测试与资格认定（验证）

```bash
.venv/bin/python -m pytest tests/unit tests/contract                    # fast, no real backends
.venv/bin/python -m pytest tests/fault_injection                        # commit crash and recovery
SVWEAVE_REQUIRE_BACKENDS=1 .venv/bin/python -m pytest -m integration    # real backends, skips fail
SVWEAVE_REQUIRE_BACKENDS=1 .venv/bin/python -m pytest tests/acceptance  # end-to-end demo flow, docs check
.venv/bin/python scripts/qualify.py                                     # release gates G0-G5
```

唯一的自定义标记是 `integration`（真实的固定版本后端）。`SVWEAVE_REQUIRE_BACKENDS=1` 会把缺失的后端从跳过变为失败；`qualify.py` 总是设置它，并把被跳过的测试视为关卡失败。Mock 仅用于单元测试和故障注入测试。静态检查：
`.venv/bin/ruff check .`、`.venv/bin/mypy` 和 `.venv/bin/svweave schema emit --check`。

**关卡（Gates）。** `scripts/qualify.py` 根据实际结果计算 G0 至 G5，并写入
`.svweave/qualification/report.{json,md}`：G0 为锁文件验证、后端探测和沙箱测试；G1 为清单、快照、配置和路径策略；G2 为语义套件（真实的 pyslang 和 UVM）；G3 为后端套件和 MCP 契约；G4 为编辑安全和所有关键变异；G5 为每个 P0 套件均无 FAIL、BLOCKED 和 SKIP、验收、覆盖率至少 85% 语句和 80% 分支、文档检查，以及在计划 22.3 预算内的基准测试。由未运行的步骤计算出的关卡为 `NOT_EVALUATED`，绝不是 PASS。只有当每个关卡都通过且没有未解决的阻塞项时，发布决定才是 `ACCEPTED_STATIC_PREVIEW`；否则为 `BLOCKED` 并附带原因。

**变异套件（Mutation suite）。** `scripts/mutation_suite.py` 把二十二个刻意引入的缺陷（计划 20.5 的十个类别、一个带两种变体的类别，外加针对恢复前滚关卡、提交时策略复核以及基于名称的 `slang_symbol` 编辑的目标选择规则的十一个额外写入安全变异）逐个注入到仓库的一份一次性副本中，并运行映射的测试；当这些测试失败时，该变异即被杀死。`--only M01,M05` 可选择其中一部分；任何不是干净失败的情形（缺失补丁锚点、超时、基线为红）都是 `ERROR`，绝不算被杀死。

**基准测试（Benchmark）。** `scripts/gen_corpus.py` 写出确定性的合成语料库，`scripts/benchmark.py` 记录主机、语料库和原始耗时（`evidence/bench/`），然后将它们与计划 22.3 的预算比较。参考语料库为 scale 1.0：约 500 个文件、100,000 行；缩小的规模会被如实标注。

| 目标（计划 22.3） | 预算 | 实测（scale 1.0，5 次冷启动，30 次热运行） |
|---|---|---|
| 冷启动语义构建 | 120 s | 中位数 24.3 s，最大 24.5 s |
| HDL worker 峰值内存 | 8 GiB | 0.059 GiB |
| 热 symbol 查询 p95 | 1 s | 0.045 s |
| 热 hierarchy 查询 p95 | 1 s | 0.020 s |
| 热 references 查询 p95 | 1 s | 0.164 s |
| 热 search p95，文本回退 / 图 | 2 s | 0.070 s / 0.689 s |

这些数据来自 `evidence/bench/20261014T085609Z-s1.0.json`：500 个文件、100,374 行，在一台拥有 16 个逻辑 CPU 和 19.3 GiB 内存的 Intel Xeon Gold 6138 主机上，该主机**不是**计划的参考主机（`reference_host_met: false`）。它们不能说明其他主机或真实项目的情况。不声称节省了任何 token 或时间：计划 22.4 尚未运行。

<p align="center"><img src="docs/images/evidence-status.svg" alt="左侧：六种证据类型 SOURCE_CANDIDATE、LSP_RESOLVED、HDL_STATIC_RESOLVED、RUNTIME_OBSERVED、EXTERNAL_DECLARED 和 UNRESOLVED，以及各自能支持和不能支持的内容。右侧：2026-10-14 资格认定运行的快照：G0 PASS_WITH_OWNER_WAIVER_OPEN，G1 至 G5 PASS，通过 920 个 P0 测试且 0 个失败、阻塞或跳过，22 个变异中 22 个被杀死，覆盖率 93.58% 语句和 87.71% 分支，EDA compile、simulation 和 formal 为 NOT_RUN，发布决定为 BLOCKED，等待负责人签核 g0_waiver、adr_012 和 license_review。" width="100%"></p>

*证据类型表不是排名：运行时证据只证明一次运行，静态分析覆盖从未运行过的代码，两者都无法取代对方。下方面板是 2026-10-14 资格认定运行的带日期快照（源码树 sha256 `21a97d0c...`），不是长期有效的声明。静态预览的关卡 G0 至 G5 与 EDA compile、simulation 和 formal 分开管理，后三者保持 `NOT_RUN`，除非用户导入证据，并以 `EXTERNAL_DECLARED` 存储。发布决定为 `BLOCKED`，仅仅是因为三项负责人签核尚未完成。*

## 安全模型

对于经过网关的操作，这些控制措施保证了什么：

* 原始源码从不对后端可写；编辑发生在候选副本中，写入协调器只有在有效的审批和 compare-and-set 检查之后才会更改源码。
* 任何地方都不存在布尔式的审批。审批由人工 CLI 在 TTY 上创建，用 HMAC-SHA256 签名，绑定到补丁、快照、profile、策略和过期时间，并因任何改动而失效。
* 后端没有网络（`--unshare-net`）；网关没有监听套接字；只有安装步骤会下载内容，并且会先校验 SHA-256。
* 仓库配置是数据：没有钩子、Make、Tcl、shell 替换或 `eval`。来自源码、注释、README 和工具输出的文本都是数据，不能更改策略或跳过验证。
* 读取限于允许的根，并带有符号链接目标检查；呈现的字符串会被脱敏，结果有大小上限。

**不**保证的内容：以相同 UID 运行并拥有不受限制 shell 的智能体可以读取 `approval.key` 并伪造审批，或直接编辑工作区；而带有自己的 shell 或文件工具的 MCP 客户端可以写入源码树。因此 `svweave doctor` 始终报告 `client_boundary: not qualified`，并且编辑默认关闭。`bwrap` 没有资源限制或 seccomp 过滤器。本地索引并非端到端离线：返回给云端模型客户端的代码片段会离开本机。详情：`docs/security.md`。

## 已知限制

十项最重要的限制；完整汇总见 `docs/limitations.md`。

1. 仅支持 Linux x86_64；macOS 和 Windows 未经资格认定。
2. 没有 EDA 运行器：compile、simulation、formal 和覆盖率均为 `NOT_RUN`，除非你导入证据。
3. 每次构建都是完整重建；不声称有增量能力。
4. pyslang 没有"查找所有引用"：查不到某个引用并不能证明它不存在，每个结果都会注明其搜索范围。
5. 虚分派调用为 `dynamic_unresolved`（列出候选项，但不选择）；UVM factory、`config_db` 和运行时拓扑不做计算。
6. 加密源码和隐式 blackbox 永远不会是 COMPLETE（`PARTIAL`，不存在性断言被禁用）。
7. 图后端把 `module` 和 `interface` 标记为 `Class`，并且不提取端口、generate 块、实例、断言、bind 或 include；它只是候选文件查找器。
8. Verible 不暴露 `task` 或 `function new`；Serena 编辑仅对 `function` 声明经过资格认定，其他一切都是 exact-range 补丁。
9. 同 UID 且带 shell 的智能体可以伪造审批；多文件写入不是原子的。
10. 性能数据只适用于所记录的语料库和主机；第三方许可证的法律审查为 `NOT_DONE`。

## 故障排查

首先检查：`svweave doctor`（以及 `--probe`），然后是 `svweave report --build BUILD_ID`。日志：每个构建对应 `.svweave/runs/<job-id>/events.jsonl`；每个服务器会话对应 `.svweave/runs/serve-*/`，其中有 `server.log` 和 `events.jsonl`。

| 症状 | 检查 | 不要 |
|---|---|---|
| 层次结构缺少某个实例 | `svweave manifest resolve --profile P`（顶层、文件顺序、参数）、`svweave query diagnostics --build B`，然后用 `svweave query hierarchy --build B` 查看 generate 分支和未解析的模块 | 把图候选项加入语义结果 |
| 找不到某个引用 | 阅读 `svweave query references` 中的搜索范围，检查声明 id、宏来源和动态分派 | 就此断定没有任何东西使用它 |
| 修改 `.svh` 后构建没有失效 | `svweave snapshot check --snapshot S`（STALE 时退出码为 5），然后 `svweave build --profile P` | 只是重启某个 UI |
| 改错了 `run_phase`，或出现 `AMBIGUOUS_SYMBOL` | `svweave query symbol --build B --name X`，使用带作用域的 `declaration_id` | 全局字符串替换 |
| slang 与你的 EDA 工具结果不一致 | 比较语言模式、编译单元、define、include 顺序、库和 stub；用 `svweave evidence import` 导入你自己的运行结果 | 仅为让两边都变绿而修改源码 |
| 验证通过但提交被拒绝 | `svweave edit show --transaction T`；检查审批过期时间、基础快照、策略和日志 | 绕过 compare-and-set 检查 |
| 被中断的提交 | `svweave edit recover --transaction T`；在解决之前，新的 prepare 和 commit 保持阻塞 | 删除日志或运行 `git reset` |
| worker 崩溃或超时 | `svweave debug bundle --build B` 和运行目录；当 slang 读取快照之外的内容时，构建为 BLOCKED | 无限制地重试，或返回空图 |
| 图显示旧文件，或图已宕机 | `svweave doctor --probe`；用 `svweave query search --snapshot S --query Q --no-graph` 使用文本回退 | 相信 watcher 状态 |
| 后端无法启动 | `bwrap --version && bwrap --ro-bind / / true`；必须启用用户命名空间 | 悄悄禁用沙箱 |
| 静态的运行时拓扑与类树不一致 | 比较测试、种子、factory 证据和 profile | 断言其中某一方是错的 |

退出码列于 [CLI 参考](#cli-参考)；日常使用和上手流程见 `docs/operations.md`。

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
| 决策 | `docs/adr/` |
| 工具链探索（spike）事实 | `docs/m0_spike_report.md` |
| 面向编码智能体的规则 | `AGENTS.md` |

## 贡献

权威规范是 `SVWeave_plan.md`（中文；请勿编辑）；`AGENTS.md` 浓缩了其中的规则。最重要的几条：

* 阅读管辖你任务的计划章节；维护需求到测试的映射。
* 工作树中未提交的改动是他人的在途工作：绝不要回退或清理它们。除非用户说了，否则编码智能体不得 `git commit`、推送或合并。
* 固定的版本、哈希和许可证记录在 `toolchain.lock.json`、`capability_report.json` 和
  `license_manifest.json` 中；绝不要猜测缺失的 API、寄存器或 EDA 结果，也绝不要随意升级后端。
* 绝不要为了让测试通过而添加 stub、禁用 SVA 或忽略编译器错误。失败的测试本身就是一项发现。
* `fixtures/expected/` 中的黄金文件由人工审阅；只有在有书面的语义理由时才能修改，绝不为了让 CI 变绿。
* 把各种状态分开（静态、EDA compile、simulation、formal、覆盖率）；SKIP、BLOCKED 和
  NOT_RUN 绝不是 PASS。
* 性能和节省 token 的声明需要来自 `scripts/benchmark.py` 的实测证据。
* 发布决定来自 `scripts/qualify.py`，而不是文字描述。文档中展示的每条 `svweave ...` 命令都必须存在（DOC-01 测试会检查这一点）。

## 许可证

* **SVWeave 自身的许可证：由负责人决定（TBD）**（计划 3.2）。在负责人做出决定之前，本仓库不授予任何许可。
* 第三方组件由安装程序下载，不在此处再分发。固定版本的 Serena v1.7.0 为 MIT；slang 和 pyslang 为 MIT；Verible 为 Apache-2.0；codebase-memory-mcp 为 MIT，并带有内置（vendored）组件；MCP Python SDK 为 MIT；Accellera UVM 为 Apache-2.0。这些是工程清单，而不是法律审查（`THIRD_PARTY_NOTICES.md`、`license_manifest.json`；法律审查状态 `NOT_DONE`）。将 Serena 升级到 v1.7.0 之后的版本会改变其许可证，并需要新的审查（ADR-010）。
