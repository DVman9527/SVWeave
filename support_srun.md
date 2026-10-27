# SVWeave srun 執行支援完整規劃

> 狀態：設計提案，尚未實作、尚未執行 Slurm 驗收。文件日期：2026-10-27。
> 本文假設使用者所指 `srun` 是 **Slurm Workload Manager 的 srun**；若是站內同名 wrapper，必須先確認其契約再實作。
> 本次只新增本文件，不修改功能、不提交或推送 Git、不送出 Slurm 工作。

## 1 結論與範圍

建議在既有受控 backend 執行路徑增加可選的 **Slurm execution provider**，保留 `local` 為預設。第一階段只將單次、單節點、單 task 的 pyslang 靜態分析 worker 放到計算節點；manifest、snapshot、gateway、結果驗證、SQLite 與 write coordinator 留在受信任的協調主機。後續才能考慮 codebase-memory-mcp 與 Serena 長生命週期程序。`srun` 是資源排程與程序運輸，不是新的 HDL 分析器，也不是 sandbox。

不能以「在既有 argv 前加 srun」視為完成：現行的本地主機 sandbox probe、`RLIMIT_AS`、process group 清理與 `/proc` 搜尋，都不證明計算節點上有同等保護。必須在遠端驗證 sandbox、工具鏈與資料路徑，並具備精確且可證明的 Slurm job／step 清理能力，才可執行 backend。

### 1.1 本提案包含

- 明確 opt-in 的 local／srun 選擇、站點核准的資源 profile、排程與執行期限。
- 不改變既有 worker JSON／MCP stdio 語意的執行介面。
- snapshot／toolchain／prepared-source 的可見性與 hash 驗證。
- 精確取消、失敗分類、崩潰復原、可追溯 evidence 與隔離快取。
- 配置、CLI、MCP、schema 相容策略與分階段測試／驗收。

### 1.2 明確不包含

不新增 simulation、formal、coverage、EDA license 使用、Verilator native runner、解密工具、自動 stub、任意 shell command、SSH transport、sbatch／salloc 後端、MPI、多節點、array、GPU、federation 或任意容器後端。這些若有需要須另立需求及安全審查。本提案也不授權任何實際叢集提交、安裝、政策調整或計費操作。

## 2 現況與程式碼依據

閱讀基準為 HEAD `3528124be2deaf314a6cf39299b6cf9c6c58126d` 加上當前未提交工作；**HEAD 不代表完整工作樹內容**。既有 UVM、Verilator importer、protected-source preparation 等 dirty changes 必須保留，後續實作須重新盤點。未發現本 checkout 或工作區 `.agents/skills` 的可用 `SKILL.md`。

| 實際位置 | 已有行為 | srun 規劃影響 |
|---|---|---|
| `AGENTS.md`；`SVWeave_plan.md` §6、7、20、23、28 | 單一 gateway、版本鎖定、不可弱化安全、runtime 禁止網路、測試層級分離 | 依原則設計；不修改權威計畫；Slurm 控制平面例外必須另外核准 |
| `application/build.py` | 呼叫 `run_worker`；group files 與 snapshot 為絕對路徑；驗證與 ingest 後才 publish | 插入 provider 不繞過 dependency validation／ingest／published-build 保護 |
| `adapters/slang/runner.py` | `WorkerOutcome`、`WorkerHandle`、獨立 process group、`RLIMIT_AS`、stdout 上限、stderr tail、worker rusage | 保留結果契約；增加遠端 handle 與資源統計來源，不把 launcher rusage 當遠端用量 |
| `adapters/slang/worker.py`、`protocol.py` | stdin 一個 JSON request，stdout 一個 JSON document；log／rusage 在 stderr | Slurm／helper 訊息不得污染 stdout；stdin 必須完整送達並關閉 |
| `security/sandbox.py`、`docs/adr/ADR-011-isolation-bwrap.md` | `SandboxSpec`、唯讀／可寫 mount 驗證、`env -i`、預設無網路；probe cache 是本機性質 | compute node 必須重新驗證；不能把 login node capability 當遠端證明 |
| `application/jobs.py`、`domain/states.py` | 單一 active build、相同 profile／參數 join、worker cancel；dead owner PID 標記 orphan | join 身分加入 execution profile；不能只看本機 PID 就判定遠端已停止 |
| `adapters/mcp_client.py`、`bounded_stdio.py` | MCP protocol pin、bounded JSON-RPC、close stdin／process-group teardown、token sweep | 本地 `/proc` token sweep 無法清掉遠端 daemon；MCP 必須另行 qualification |
| `orchestration/registry.py`、`application/session.py` | 統一 backend session 與 shutdown | 後續接入 provider factory，不能各 adapter 自行拼接 srun |
| `config/models.py`、`loader.py`、`context.py` | strict models；未知欄位拒絕；local config 目前只有 root mappings | 新欄位尚不可用，需 models／schemas／context 同步 |
| `application/options.py`、`cli.py`、`server.py`、`gateway/` | CLI／MCP 共用 application services | 執行選擇應進 application context，不只改 CLI |
| `domain/ids.py`、`storage/`、`application/evidence.py` | build identity、DB、分離 evidence | 新 execution namespace／attempt evidence，不能把舊快取當遠端通過 |
| `adapters/slang/uvm_calls.py`、`docs/verilator_static_supplement.md` | 工作樹中已有靜態 UVM metadata、外部 Verilator inventory、lexical preparation | 只改執行位置，不提升這些資料的證據強度 |

`application/runner.py` 是 Envelope／例外轉換邊界，不是 OS process launcher；不要因名稱相似而把 scheduler 邏輯放在這裡。

## 3 建議架構與信任邊界

```text
CLI / MCP
  -> application services / trusted policy / JobManager
  -> manifest + immutable snapshot + ExecutionRequest
  -> ExecutionProvider
       local -> existing sandbox + worker
       srun  -> trusted scheduler launcher
                 -> compute-node trusted bootstrap
                 -> verified bwrap sandbox
                 -> fixed allowlisted worker argv
  <- bounded stdout + separate control evidence + bounded stderr
  -> protocol/hash/dependency validation -> local DB ingest -> publish
```

### 3.1 介面責任

以下型別與目錄名稱皆為**擬議**，不是現有 API：

- `execution/models.py`：`ExecutionRequest` 包含 backend kind、固定 argv、`SandboxSpec`、stdin 來源、resource limits、deadlines、snapshot／toolchain identity、request／attempt ID。
- `execution/local.py`：維持現有語意與預設，不因抽象化而降低 stdout cap、memory limit 或 descendant cleanup。
- `execution/slurm.py`：產生 allowlisted argv、管理 scheduler 身分／狀態／取消與 evidence；不理解 HDL 語意。
- `execution/remote_bootstrap.py`：受鎖定且唯讀部署的可信程式，在 compute node 驗證 request、路徑、工具 hash 與 bwrap，設定遠端 rlimit、禁 core dump，啟動固定 backend；不接受任意程式路徑或可執行 shell 片段。
- `ExecutionHandle`：`cancel()`、`wait()`、狀態及 ownership；cancel 必須冪等、thread-safe。結果相容 `WorkerOutcome`，附加 transport evidence；scheduler 結束不等於分析成功。
- control record：bootstrap 經獨立受限 sidecar 回報協定版本、attempt nonce、job／step、node、remote-ready、sandbox／toolchain 驗證結果；不把控制資料混入 backend stdout。

control 目錄須在 backend sandbox 外，權限受限、no-symlink、安全原子寫入、有大小上限，並與 scheduler 查詢交叉驗證。來源文字或 backend 輸出不可授權新的 job ID。nonce 只提供關聯，不是假設可以抵抗相同 UID 的 unrestricted host access；保留原計畫對同 UID 信任邊界的限制。

### 3.2 網路例外是前置決策

**P0 編輯邊界**：`application/edit.py::_run_profile_validation` 會對 baseline 與 candidate 呼叫 build（含 `publish=False`）。第一階段明確拒絕在 srun execution context 啟動 editing／candidate validation；read-only static build 才可使用 srun。不能只搬一般 build，卻讓受控編輯偷偷採不同 provider。日後解除此限制，必須驗證 baseline／candidate 使用相同已核准 provider、隔離 state，且保留 approval、CAS、journal、recovery 與不發布 candidate 的保證。不得自動 fallback local 來繞過此限制。

目前 `AGENTS.md` 禁止 runtime network。Slurm client／daemon／stdio transport 必須通訊，與「完全無 runtime 網路」不同。因此啟用 srun 前，需 owner 明確核准**最小範圍的 trusted scheduler 控制平面例外**及相應 ADR／操作政策；未核准時保持 BLOCKED，不能因本文件而自動開放。

例外僅限站點核准 scheduler endpoints／通訊需求與受信任 launcher。backend bwrap 仍使用 network isolation；不設定整體 `network_enabled=true` 來解決 srun，不讓 backend 讀 scheduler 憑證、SSH agent、cloud token 或 approval key。工具鏈仍離線，不執行 module shell hook、自動下載或 pip install。

## 4 支援模式與工作所有權

第一個可發布版本僅支援單一 cluster、`nodes=1`、`ntasks=1`、同步 srun。CPU 可配置不表示 worker 能線性加速，也不得把增加 task 數當作並行化 JSON 協定。

| 模式 | 建議階段 | 所有權及限制 |
|---|---|---|
| `local` | 保持預設 | 不要求安裝 Slurm，不查詢 scheduler |
| `existing_allocation` | P0 先驗證 | 顯式選擇並核對 allocation；只擁有自己建立的 step；絕不取消外層 allocation 或其他 step |
| `new_allocation` | P0 安全閘門通過後 | srun 取得專屬 allocation；必須能在 pending 階段可靠取得與驗證 ID，才可宣稱支援 queue timeout／cancel |
| 長生命週期 MCP | P1 | 每個 backend 自己的受控 step／state；不可先假設已支援 |

官方文件指出 srun 可取得 allocation 或在既有 allocation 建立 step；`--jobid` 指定既有 allocation。[Slurm srun](https://slurm.schedmd.com/srun.html)

**不得靠 ambient `SLURM_JOB_ID` 默默改變模式**。既有 allocation 是借用，不是本工具所有；父 job 剩餘期限、CPU／memory 不足時明確拒絕。新 allocation 模式在 allocation 環境下應拒絕模糊的巢狀行為，而非清除變數後猜測 scheduler 會另配資源。任何擴充都須測試站點行為。

### 4.1 Pending 階段的 ID 取得是發布阻擋項

compute-node sidecar 只有啟動後才存在，**不足以取消尚未啟動的 job**。P0 spike 必須驗證站點版本提供的 allocation notification 或查詢契約。可用每次唯一、無敏感資料的 correlation 名稱／comment 輔助查詢，但名字本身不構成取消權限；需核對 UID、cluster、提交時間、完整 ID、attempt 與 ownership ledger，且只能接受唯一匹配。

若只能解析本地化的人類 stderr、無法排除 query race／重複名稱／ID 重用，則 `new_allocation` 不可交付；先只提供通過資格驗證的既有 allocation step 模式。不要以新增 sbatch／salloc、廣泛 scancel 或最新 beta flag 偷渡替代方案。既有 allocation 的 step-start race 也須驗證 pending step 與 launcher 結束後無遺留。

## 5 配置與資源模型

### 5.1 配置歸屬與優先序

建議 trusted workspace policy 定義允許的 provider、站點 profile、executable 絕對路徑、cluster、資源上下限與控制平面例外。專案 manifest 不得指定任意 executable、account 或 shell。`svweave.local.yaml` 可存放已核准 profile 的預設選擇與本地路徑，但它是 repository-adjacent data，不是授權來源。

解析順序為：安全預設 → 站點核准 profile → local 選擇 → 顯式 CLI 選擇；最後一律與 trusted policy 取交集，越界拒絕而非截斷。MCP 使用啟動時的核准設定，不能讓 untrusted tool request 自由提供 Slurm flags。缺省、舊配置一律 local；顯式 srun 失敗不自動 fallback local。

以下僅為**預計 schema 的概念範例**，現行 strict loader 不接受，不能直接貼入配置：

```yaml
execution:
  provider: srun
  site_profile: reviewed_site
  allocation_mode: existing_allocation
  resources:
    nodes: 1
    tasks: 1
    cpus_per_task: 2
    memory_mib: 10240
    walltime_seconds: 300
  deadlines:
    queue_seconds: 120
    startup_seconds: 30
    cancel_grace_seconds: 10
```

範例數值不是站點建議值；資源與計費上限需管理員確認。

### 5.2 欄位與檢核

| 擬議欄位 | 行為 |
|---|---|
| partition、account、qos、constraint、reservation | 可選，只有核准 profile 中的值；不接受任意 flags／逗號多 cluster／控制字元 |
| cpus_per_task | 正整數且有站點上限；預設 1；對應明確的 worker thread policy |
| nodes、tasks | P0 固定 1，拒絕其他值，避免多份 JSON 混合 |
| memory_mib | 正整數、有上限；P0 選 per-node memory，不同時提供 mem-per-cpu／mem-per-gpu；拒絕「0 無上限」 |
| walltime_seconds | 有限正值；scheduler 配額包含啟動／清理餘裕，不能取代 worker deadline |
| queue_seconds、startup_seconds | 分開計時；不能讓 queue 消耗原有分析 CPU 工作期限 |
| cancel_grace_seconds | 有限，且有 cleanup reconciliation 上限；超過後標記未確認而非成功 |
| executable/runtime roots | trusted profile 指定絕對路徑與 hash，不從 repository PATH 尋找 |
| shared staging root | 核准專用目錄；禁止 HOME／原始 source／approval store 的寬廣掛載 |

現有 `analysis.max_build_seconds`（預設 120）保留 local 語意；srun 下定義為 remote worker 開始後的分析上限。`max_worker_memory_mib`（預設 8192）仍為 worker address-space 限制；Slurm memory request 是另一層配額，需包含 Python／bwrap／bootstrap 餘裕，不把兩者當完全相同的 RSS 指標。local 的 preexec rlimit 不會限制遠端 worker；bootstrap 必須重新設定，並檢查站點 cgroup／memory enforcement。

## 6 站點部署與資料存取前提

啟用前由站點 operator 確認：

1. 核准提交主機、Slurm client／server 版本、認證機制、允許的 partition／account／qos、scheduler 查詢與 cancellation 權限。只記錄狀態，不將 credential 寫入文件或 evidence。
2. compute node 的 Linux／架構／Python ABI、pyslang 與 runtime assets 均符合 lock；不是只在 login node 通過 verify。禁止在工作啟動時下載依賴。
3. 每種可被分配的 node class 能用必要 user／mount／PID／network namespaces 與 bwrap；遠端 probe 不得使用本地 `_PROBE_CACHE` 的結論。異質節點不以單一成功節點代表全部。
4. snapshot、group files、唯讀工具鏈與 bootstrap 在 compute node 有一致可見路徑。P0 只接受相同絕對路徑的共享 immutable inputs；本地 `/tmp`／venv 可見不代表遠端可見。
5. gateway 的 live SQLite、journal、approval store 留在本地可靠磁碟。共享 staging 僅有 immutable inputs、控制紀錄與輸出；不把整個 `.svweave` 或含 SQLite WAL 的 runtime state 搬到 NFS。
6. 若現有 state-dir 布局把 snapshot／DB 放同一 root，新增有明確 hash manifest 的 staging 複本，而非整個目錄共享。以 temp 寫入、驗證 hash、原子發布；P0 不做任意路徑映射。建立／驗證 dependency closure，缺 group file／include 就拒絕。`build_invocation` 與 group-file 路徑必須直接針對最終共享 staging root 產生，再 hash 並封存 request；不可複製一份仍含本地不可見絕對路徑的 request。這是提交端與 compute 端使用相同最終路徑，不是任意路徑轉換。
7. 分享路徑經 canonical path／root allowlist／no-symlink 規則；提交及 compute 端都驗證。snapshot 在 queue 期間固定，不能到執行時重新讀原始 workspace。返回結果仍須通過既有 dependency checks。
8. bootstrap 寫入專用 per-attempt scratch，backend 只獲必要 mount；清理不得沿使用者 symlink 或刪其他 attempt。磁碟配額、atomic rename、共享 FS coherency 與 inode 權限須測試。

**不支援非共享 FS 的隱式自動傳檔**。日後若要 staging／path translation，必須另設受限打包、hash verification、路徑對映與 location round-trip 測試；不能用 `scp`、shell tar 或 srun executable broadcast 假裝已解決完整 snapshot 與工具鏈依賴。

## 7 啟動與 stdio 協定

### 7.1 命令建構

使用 argv array 與 `shell=False`；launcher、bootstrap、backend executable 均取自受信任 profile。拒絕 `bash -lc`、`eval`、`module load`、command template、任意 `extra_args`。值以單一 argument 傳遞，不依賴 shell quoting；包含換行、NUL、非法資源格式或可改變命令結構的值須拒絕。

Slurm 相關選項只從 typed model 映射；P0 預計使用單 task、明確 cwd、有限 time／memory／CPU 與站點支援的非 MPI 模式。禁止 `--pty`、`--label`、輸出重導到 backend stdout 的控制訊息、`--multi-prog`、`--external-launcher`、重複 executable 與任意 job arrays。不要為了即時輸出盲目加會改變 terminal 行為的選項；由 backend 的 flush／unbuffered runtime 與實測處理。

### 7.2 環境分兩層

- scheduler launcher 環境：只帶站點必要的認證／設定與受控 allocation metadata；清除非必要 `SBATCH_*`、`SRUN_*`、`SLURM_*` 等隱含選項。既有 allocation 必要變數只能在已驗證模式下加入。
- compute bootstrap／worker 環境：固定 runtime 路徑、locale、允許的 worker variables；進入 bwrap 後仍 `env -i`。scheduler authentication 不得傳入 backend。
- 官方 `--export` 的 ALL／NONE／明確清單具有不同規則；採核准明確清單並對站點版本驗證，不能假設預設不會帶使用者環境。[Slurm srun](https://slurm.schedmd.com/srun.html)

### 7.3 資料與控制通道

slang stdin 原樣傳送一份 `WorkerRequest` 後 EOF；stdout 原樣保留單一 JSON document。保持原有最大輸出限制、stderr tail、逐步 drain 與 backpressure，不先收完整無限輸出。無效 JSON、額外 stdout banner、部分文件、截斷、重複 task 輸出均 fail closed；禁止以「過濾看似 log 的行」修復協定。

Slurm／bootstrap diagnostics 走 bounded stderr；control sidecar 不接收來源程式碼指令，也不塞入 JSON-RPC。父端與遠端需要相符的 request hash、protocol version、attempt ID 才接受結果。stdio 斷線標記 transport failure；不可將本機 launcher rc=0 或 scheduler COMPLETED 當作有效 worker 結果。

P1 MCP 另外驗證：initialize 在 remote-ready 後開始計時、newline framing、16 MiB message cap、UTF-8 chunk 邊界、stderr log cap、EOF、背壓與 daemon teardown。Serena 的單一 active project／candidate 專用 session 不變；codebase-memory 的獨立 state／daemon 不可跨 project 混用。所有 `SandboxRunner.run_once` 與 adapter helper calls 也必須有一致位置與清理契約，不能只搬主 server。

## 8 狀態機與時間限制

保留公開 `JobLifecycle` 的 QUEUED／RUNNING／SUCCEEDED／FAILED／CANCELLED／TIMED_OUT；新增獨立 execution phase／scheduler detail，避免舊 client 立即失去 enum 相容性。

```text
VALIDATING -> SUBMITTING -> WAITING_RESOURCES -> STARTING
 -> SANDBOX_VERIFIED -> WORKER_RUNNING -> COLLECTING -> VERIFIED
                               \-> CANCELLING -> terminal / CLEANUP_UNCONFIRMED
```

- `queue_seconds`：從 submission 開始涵蓋 allocation／step 等待；`startup_seconds` 從 scheduler 實際開始後至 bootstrap ready；worker execution timeout 從可信開始訊號計算，使用 monotonic clock。
- 啟動訊號尚未取得時不能把所有等待假稱排隊；若 scheduler state 不可觀測，保留 UNKNOWN phase 並受 overall deadline 保護。
- `--immediate=<seconds>` 可限制資源等待，但不是完整端到端 watchdog。`--time` 是 scheduler 時限，具有站點與分鐘粒度行為；仍需 gateway 與遠端 supervisor 的精確 deadline。[Slurm srun](https://slurm.schedmd.com/srun.html)
- overall deadline 包括 queue、startup、execution、資料收集與清理；取消耗盡時結果含 `cleanup_confirmed=false`，不能抹掉遠端可能仍活著的風險。
- timeout、Ctrl-C、MCP cancel、stdout overflow、gateway shutdown 都走同一 cancellation path。成功返回前亦檢查遠端子程序已結束。
- P0 不自動重試／requeue。PREEMPTED、NODE_FAIL、scheduler disconnect 及 OOM 留下獨立原因；人工明確重跑建立新的 attempt，不覆寫前次 evidence。

### 8.1 錯誤映射

| 情境 | 對外分類建議 | 必須保留 |
|---|---|---|
| 未知配置、超配額 | 現有 INVALID_CONFIG／POLICY_DENIED | 驗證欄位，未提交工作 |
| srun 不存在、認證或路徑／sandbox 不可用 | BACKEND_UNAVAILABLE，BLOCKED qualification | failure phase 與 redacted reason |
| 排隊／啟動／執行期限 | 現有 BUILD_TIMEOUT 與 TIMED_OUT，新增 detail reason | queue_timeout／startup_timeout／execution_timeout |
| worker／Slurm memory 限制 | RESOURCE_LIMIT，有證據才說 OOM | raw scheduler state、signal、worker 訊息與來源 |
| cancel | CANCELLED 意圖與 cleanup status 分離 | operator request、實際目標、終止確認 |
| invalid／truncated stdout | BACKEND_UNAVAILABLE 或既有 output-limit 分類 | 協定失敗，不 ingest |
| scheduler state 不明／斷線 | BACKEND_UNAVAILABLE，cleanup UNKNOWN | 不猜測已停止或已成功 |
| HDL diagnostics | 沿用原有 analysis 判定 | 不因 scheduler 成功忽略 compiler errors |

不直接套用本地「SIGKILL 可能等於 memory limit」的推斷到 Slurm；被取消、節點失效與 OOM 可能呈現相似 return code。新 domain error 若確有需要，必須同步 enum、CLI exit mapping、MCP schema 與契約測試。

## 9 取消與復原

### 9.1 精確目標

ownership ledger 至少含 SVWeave job／attempt、cluster、UID、Slurm job／step ID、allocation ownership、提交時間、nonce、建立與取消狀態。程序 PID 不是 scheduler 身分。本地 owner 身分需包含 host identity、程序 start identity 與 attempt，防止 PID 重用或跨主機同 PID 被誤認為仍存活。

1. 取消前查核 ledger 與 scheduler 資訊；重複取消不重送不明範圍操作。
2. 借用 allocation 僅能針對本 attempt 的 `job_id.step_id`；禁止取消 parent job、batch／extern step、其他使用者或同使用者其他工作。
3. 自建且確認所有權的專屬 allocation 才可取消完整 job；pending 階段也要先有可靠 ID。
4. 可先對已驗證目標傳 TERM，經 grace 後終止，再查詢是否達 terminal；具體 signal 策略按站點驗證。`scancel` 接受 job／step 精確識別且有所有權限制。[Slurm scancel](https://slurm.schedmd.com/scancel.html)
5. 不使用 `scancel -u`、名稱 wildcard、partition filter 或全使用者 `pkill`。Slurm 的同 UID 權限不等於本 application 擁有使用者全部 jobs。
6. local launcher 的 process-group kill 只是最後清掉本地程序，不是遠端 cancellation 證據；先建立／保存 scheduler 清理狀態，避免殺掉唯一可用 handle。

### 9.2 Crash recovery

現行 `sweep_orphans` 只處理本地 owner PID；新增 srun 時須先讀 durable ledger，對自己擁有的 job／step reconcile，不能只把 DB row 改 FAILED 就釋放 snapshot。若 gateway 斷線，遠端 supervisor 應有有限期限及經驗證的父連線／lease 失效策略，scheduler walltime 是最後保險，不是立刻清理保證。

不明 submission 結果不得自動重新 submit，先查核原 attempt。查詢／scancel 失敗採有限退避、保留證據及人工處理資訊；不要以密集 polling 壓垮 controller。舊 ID 重用、同名工作、延遲 accounting、狀態競態都需 negative tests。清理未確認時 pin 相關 inputs 與 evidence，禁止 GC 或宣稱沒有 orphan。operator 的人工修復也應指定 ledger 中確切目標，不能擴大取消範圍。

## 10 Evidence 與快取

每次執行均記錄 request／attempt ID、snapshot／config／build ID、toolchain lock hash、bootstrap version/hash、provider、site profile digest、remote platform、sandbox mode、Slurm version、cluster／job／step、資源請求與實際配額、queue／startup／execution 時間、launcher rc、worker rc／signal、scheduler state、cancel／cleanup 狀態、結果 hash 與限制。

路徑／node／account 等欄位按現有 redaction policy 處理；不保存整份 environment、憑證或完整敏感 source。錯誤也留 evidence。worker rusage、scheduler accounting 與 local launcher rusage 分欄，不可相加後假裝同一測量。accounting 延遲或不可用填 UNKNOWN，不填 0。[Slurm sacct](https://slurm.schedmd.com/sacct.html)

現行 `compute_build_id(snapshot_id, config_id, toolchain_id)` 可能在進入 runner 前命中已完成 build。因此第一階段需：

- 新增 execution fingerprint／namespace，涵蓋 provider、驗證後 runtime／sandbox identity、bootstrap 與影響執行的政策；選擇納入 cache key 或增設嚴格 eligibility 欄位，migration 設計需先審查。
- 預設禁止 local 與 srun、不同 node runtime／toolchain／isolation 互用結果；舊 cache 缺遠端 provenance 不可作 srun qualification。
- 共享 immutable semantic identity 與 execution attempt 是不同概念。相同 HDL 結果可比較，但不可因此覆寫實際執行來源；日後若允許跨 provider reuse，須明示 `reused_from` 及並非本次遠端執行。
- 未通過完整 ingest／dependency／hash 驗證，不發布任何新 COMPLETE build；上一個完整 published build 保持不變。
- failed／cancelled／PARTIAL／UNKNOWN cleanup 不提升為成功 cache。`JobManager` join 同時核對 effective execution profile／resources／expected snapshot／reuse，不合則拒絕。

## 11 UVM 與 Verilator 邊界

### 11.1 UVM

現有 UVM call metadata 是 `static_call_site`，`runtime_resolution=not_computed`。srun 只改執行地點，不提供 factory override 的 runtime 選擇、config-db hit、phase 執行、virtual dispatch 或 constraint solve。驗收要比較 local／remote 的靜態 metadata 與 limitation，不能要求本功能產生模擬結果。缺定義、package 或工具錯誤不可用 stub、略過 SVA 或吞 diagnostic 修補。

### 11.2 Verilator 外部 inventory

目前工作樹中的功能只匯入外部 5.052 JSON inventory，不啟動 Verilator。保持 `EXTERNAL_DECLARED`、`PARTIAL`、`runtime_resolution=not_computed` 及未綁定 snapshot／build 的語意；檔案 hash 不認證 producer。不能因檔案由 Slurm share 取得而改標 verified semantic evidence。importer 仍在 local gateway 執行，不自動搬到 srun；native Verilator 產生器是另案。

### 11.3 Protected source preparation

現有 lexical preparation 只產生 derived source 與 report，不解密、不編譯、不改 original。若後續經明確核准將 derived source 作為靜態分析輸入，必須攜帶 original／derived hash、preparation ID、gap range／原因及 sidecar；明確記錄 transformed input，不能冒充原始 snapshot 的完整分析。

保持 CR/LF／byte location 邊界與 original include context；搬到共享目錄不可偷偷改 relative include 解析。encrypted definitions／interfaces／ports 仍 UNKNOWN；缺定義錯誤保持錯誤。沒有 gap 也只代表 lexical preparation，不能證明語意完整。目前 preparation 不自動替換 manifest，也不自動將 sidecar 綁進外部 importer；srun 不能暗中補成已驗證關聯。

所有路徑均維持 static analysis／EDA compile／simulation／formal／coverage 分離；未執行者為 NOT_RUN，不能因 Slurm job 成功而變 PASS。

## 12 CLI 與相容性

- 舊 CLI、MCP tool input、`schema_version: 1.0` 的 local config 必須繼續工作；新增欄位採 optional default 或明確版本遷移，不強迫所有使用者安裝 Slurm。
- 擬議 CLI 介面：`--execution-provider`、`--execution-profile` 及 allocation selection；名稱與層級待實作評審，**目前不存在，本文不提供可直接執行的 svweave 指令範例**。
- 提供未來的配置解析／argv preview 與 preflight：不提交工作的檢查和會消耗 allocation 的 remote probe 必須清楚分開。一般 doctor 不可默默排程新 job；remote probe 必須顯式授權。
- MCP build／job 原有結果保留；scheduler metadata 優先新增到 versioned detail，而非擅改 lifecycle enum。既有錯誤 exit code 仍相容。
- models／generated schemas／CLI help／README 三語／operations／security／limitations／tests 需同批更新；新選項在實作前不得移入現有「可執行範例」。
- rollback 為選回 local 並禁用新的 submission，不刪除既有 ledger；已提交遠端工作須先 reconcile，不能靠切設定遺棄。

## 13 需求與測試對照

下表全部是**未執行的驗收計畫**。Mocks 僅作 unit／fault injection，不可代替真 Slurm 與 pinned backend integration。

| ID | 需求與情境 | 預期結果／層級 |
|---|---|---|
| SRUN-01 | 舊設定與未安裝 Slurm 的主機 | local 行為／CLI／MCP 相容；unit、contract、local regression |
| SRUN-02 | 明確 srun；缺 executable／權限／認證 | fail closed，不 fallback local、不提交第二份；unit、integration |
| SRUN-03 | 資源、account、flag injection、空白路徑、NUL／換行 | typed model 拒絕越界；合法 argv 原樣；unit／security |
| SRUN-04 | compute node 缺 bwrap／namespace／工具 hash 不符 | backend 未啟動；BLOCKED；真遠端 negative integration |
| SRUN-05 | worker 嘗試寫 original／讀 approval／連外 | OS 拒絕，原始 hashes 不變；security integration |
| SRUN-06 | snapshot／include／group file 不共享、hash 改變 | 拒絕，不讀 live source 補救；unit、integration |
| SRUN-07 | queue timeout、startup timeout、worker timeout | 三種 detail 可區分；遠端清理確認；fault、integration |
| SRUN-08 | cancel queued／starting／running／collecting race | 精確 job／step，冪等，無遺留；fault、integration |
| SRUN-09 | 借用 allocation 中有另一個無關 step | 僅本 step 被取消，parent／其他 step 存活；acceptance 必測 |
| SRUN-10 | gateway crash、client disconnect、submit 回應遺失 | durable reconcile；無重複 submit、無錯殺；fault、integration |
| SRUN-11 | 舊 ID／重複名稱／偽造 sidecar／權限不符 | 不接受或取消陌生工作；unit、security integration |
| SRUN-12 | worker crash、OOM、node failure、preempt | 狀態／原因正確，不把 SIGKILL 一律當 OOM；fault、integration |
| SRUN-13 | stdout flood、巨型 JSON、banner、截斷、stderr flood | 有界記憶體／log、拒絕 ingest、精確取消；unit、integration |
| SRUN-14 | 同 snapshot local vs srun 真 pyslang | 比較正規化 semantic rows／diagnostics／limitations，不要求 timing／job ID 相同；integration |
| SRUN-15 | cache 已存在、不同 provider／runtime／sandbox | 不錯用舊 cache；attempt provenance 完整；unit、contract |
| SRUN-16 | 同 profile 不同 execution args 的 concurrent request | 不誤 join；single-writer／build lock 保持；unit、fault |
| SRUN-17 | 失敗 build／cleanup UNKNOWN／GC | 保留舊 published build 與受 pin 證據；fault |
| SRUN-18 | UVM static metadata 真 fixture | 不新增 runtime claims；靜態結果對齊；integration |
| SRUN-19 | Verilator import／prepared gaps 回歸 | 原始檔不變、PARTIAL／UNKNOWN／NOT_RUN 不提升；unit、contract |
| SRUN-20 | 遠端 MCP initialize／call／EOF／daemon children | protocol pin、bounded transport、完整 cleanup；P1 真三後端 integration |
| SRUN-21 | policy 網路例外未批准 | 不啟動 srun；worker 不因例外取得 network；security |
| SRUN-22 | 文件／schema／CLI／migration | schema emit check、help 與 DOC-01、舊配置 fixtures；contract |
| SRUN-23 | 關鍵安全檢查被移除的 mutation | write safety／cancel ownership／sandbox bypass 測試必失敗；mutation |
| SRUN-24 | 站點資源成本與 cleanup accounting | 測量 queue／exec／CPU／RSS，關聯精確 IDs；不虛構效能收益；acceptance |

既有回歸測試的擴充入口包含 `tests/unit/test_slang_runner.py`、`tests/unit/test_gateway_jobs.py`、`tests/unit/test_m3b_fixes.py`、`tests/unit/test_sandbox.py`、`tests/integration/test_api_semantic.py` 與 `scripts/mutation_suite.py`；實作時沿用既有測試 ID。另新增 P0 srun editing 拒絕測試，證明不會執行 baseline／candidate 或進入批准／套用階段。

每筆 evidence 記錄 commit＋dirty-tree digest、命令、lock hash、host／Slurm profile、PASS／FAIL／BLOCKED／SKIP／NOT_RUN、結果路徑。站點不可用為 BLOCKED 或 NOT_RUN，不能把 mock 通過標成 integration PASS。

## 14 分階段交付與驗收閘門

### P0A 設計與站點確認

交付 ADR、typed schema 草案、威脅模型、profile 欄位、資料流與 fixture 計畫。確認 §15 問題，owner 核准 scheduler 網路例外與測試 allocation 額度。**通過條件**：能說明所有權、資料可見性與取消目標；無 credential／IP 被加入 repo。

### P0B 受控 capability spike

經另行授權才使用小型 synthetic fixture 跑真 Slurm。驗證 remote bwrap、toolchain hash、單 task stdio、共享 path、memory／timeout、兩種 allocation mode 的 ID 取得與 cancellation。**通過條件**：borrowed allocation 的無關 step 不受影響；全部自有工作無遺留；new allocation pending ID 不可靠就明確排除該模式，而非降低標準。

### P0C Local 抽象與 slang 整合

先抽出 local provider 並通過原測試，再加入 srun worker、execution ledger、phase／timeout／evidence／cache 邊界。CLI／MCP 使用相同 service。**通過條件**：SRUN-01 至 19、21 至 24 適用項通過，真實 remote 結果與既有語意驗證對齊，無修改 original 或升級工具鏈。

### P1 長生命週期 backend

在需求確認後納入 registry、MCP transport、codebase-memory／Serena helper processes 與 remote state lifecycle。**通過條件**：真三後端 probe、初始化、查詢、cancel、正常退出後的 child／daemon 清理全部有證據；不以 slang 成功宣稱三後端皆支援。

### P2 文件與 qualification

更新既有 docs／CLI help／schema、station operator runbook、故障復原、資源／成本量測，執行原有 lint、mypy、schema check、unit／contract／fault、真 backend integration／acceptance、mutation 與 qualification 流程。保留 local 及 srun 各自 qualification scope；沒有真 Slurm evidence 不標支援完成。效能比較需相同資料／工具鏈／資源，分開 queue 與 execution；不預先承諾加速倍率。

## 15 待站點與使用者確認

1. `srun` 是否為原生 Slurm，或公司 wrapper？精確版本與支援範圍為何？
2. 只需要 pyslang worker，還是最終也需要 graph／Serena？gateway 是否允許留在提交主機？
3. 常用模式是在已取得 allocation 內啟動，還是允許本工具取得新 allocation？允許哪些 account／partition／qos、資源與計費上限？
4. 哪些提交主機可連 controller／compute stdio？是否核准 §3.2 最小控制平面網路例外？
5. 是否允許 bwrap／unprivileged namespaces；可分配 node class 是否一致？若不允許，本提案預設 BLOCKED，不自動採無隔離或別種容器。
6. snapshot／工具鏈／venv 是否共享同絕對路徑？DB／journal 可否維持本地磁碟？staging root、容量、保留期限與清理責任為何？
7. 是否有站點核准的 pending job ID 通知／query 方法、step 查詢與 accounting？能否證明 job ownership 與可靠清理？
8. queue／startup／execution／cleanup 各期限是多少？站點 preemption／requeue／KillWait／memory enforcement 如何設定？
9. 是否允許在隔離測試 allocation 中建立無關 companion step 以驗證不誤殺？誰核准真測試與 evidence 的敏感資訊處理？
10. 是否另有 native Verilator 或 EDA compile 需求？若有，應另立任務，不能混進本次 static-only srun scope。

## 16 參考資料與本次驗證

### Repository 依據

- [AGENTS.md](AGENTS.md)；[權威計畫](SVWeave_plan.md) §6／7／20／23／28。
- [Sandbox 實作](src/svweave/security/sandbox.py)；[slang runner](src/svweave/adapters/slang/runner.py)；[worker protocol](src/svweave/adapters/slang/protocol.py)。
- [Build service](src/svweave/application/build.py)；[JobManager](src/svweave/application/jobs.py)；[config models](src/svweave/config/models.py)。
- [MCP client](src/svweave/adapters/mcp_client.py)；[bounded transport](src/svweave/adapters/bounded_stdio.py)。
- [Verilator 與 protected-source 工作樹文件](docs/verilator_static_supplement.md)；[UVM 靜態 metadata](src/svweave/adapters/slang/uvm_calls.py)。

### 官方 Slurm 依據

於 2026-10-27 查閱官方文件；線上內容可能比部署版本新，實作前仍須以站點版本的 man page／capability probe 驗證。

- [srun 官方手冊](https://slurm.schedmd.com/srun.html)：allocation／step、resource flags、環境、stdio、時間與 return value。
- [scancel 官方手冊](https://slurm.schedmd.com/scancel.html)：job／step 精確取消與 signal 範圍。
- [Job launch design](https://slurm.schedmd.com/job_launch.html)：launcher、daemon、step 終止流程。
- [sacct 官方手冊](https://slurm.schedmd.com/sacct.html)：accounting／exit evidence；可用性依站點設定。
- [Quick start](https://slurm.schedmd.com/quickstart.html)：job 與 step 的操作概念。

特別注意：查閱版本的 srun 文件含 beta `--async`，其 `--parsable` 依賴 async；**本提案不採用它們，也不把它們當所有 Slurm 版本的既有能力**。同步 stdin/stdout worker 不應因取得 ID 而改成 detached async 模式。[Slurm srun](https://slurm.schedmd.com/srun.html)

### 本次實際完成與未執行事項

本次只閱讀程式碼、工作樹狀態、規範及官方文件，撰寫此規劃並做文件／變更範圍檢查。沒有執行 Slurm 工作、native backend、simulation 或 feature tests；所有 srun integration／acceptance 狀態為 **NOT_RUN**。文件完成不等於功能完成，現有未提交變更與其 qualification 狀態不因本文件改變。
