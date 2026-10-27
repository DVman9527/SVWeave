# SVWeave — 第一版完整實作計畫

> **SystemVerilog & UVM Code Intelligence**  
> 以 SystemVerilog 為核心，整合 repository 關聯搜尋、build-aware HDL 靜態語意與受控 AI 輔助編輯。

| 文件欄位 | 內容 |
|---|---|
| 文件名稱 | `SVWeave_plan_v1.md` |
| 計畫版本 | `1.0` |
| 對應軟體里程碑 | `SVWeave v0.1.0`，本文稱「v1」 |
| 編寫／上游查核日期 | 2026-10-13，Asia/Taipei |
| 文件狀態 | 實作規格提案；不是已完成的產品、benchmark 或 EDA qualification report |
| 目標讀者 | DV lead、SystemVerilog／UVM 工程師、Python／MCP 開發者，以及執行實作的 coding agent |
| 已選定名稱 | **SVWeave**；不使用 VeriWeave 或只強調 Verilog 的定位 |
| 整合對象 | `MikePopoloski/slang`、`oraios/serena`、`DeusData/codebase-memory-mcp` |
| 重要底層依賴 | Serena 的 SystemVerilog backend 使用 Verible；slang 是獨立分析後端，不會自動替換 Serena 的 LSP |
| 本次交付 | 本 Markdown 計畫；未建立 repository、未 push GitHub、未執行三工具整合或 EDA simulation |

**閱讀規則：**本文中的 `SVW-*` requirement、`svw_*` MCP tool、`svweave` CLI、YAML／JSON 欄位、資料表及狀態名稱，除非明確標記「上游既有」，均為 **SVWeave 的待實作設計**。不得把範例命令當成目前已可安裝執行的成品。

---

## 目錄

1. [專案定位與第一版成果](#s01)
2. [假設、限制與實作原則](#s02)
3. [上游能力查核與授權](#s03)
4. [v1 範圍與功能矩陣](#s04)
5. [使用情境與需求追蹤](#s05)
6. [系統架構與責任邊界](#s06)
7. [部署、依賴與版本鎖定](#s07)
8. [Build manifest 與 filelist adapter](#s08)
9. [Snapshot、快取與一致性](#s09)
10. [slang semantic adapter](#s10)
11. [codebase-memory-mcp adapter](#s11)
12. [Serena adapter 與受控編輯](#s12)
13. [資料模型與證據規則](#s13)
14. [MCP API contract](#s14)
15. [CLI 與操作流程](#s15)
16. [UVM、SVA 與 EDA 邊界](#s16)
17. [Repository 與模組配置](#s17)
18. [分階段開發與 release gates](#s18)
19. [可直接拆 issue 的工作清單](#s19)
20. [驗證策略與測試矩陣](#s20)
21. [SystemVerilog／UVM／SVA fixtures](#s21)
22. [CI、品質與效能量測](#s22)
23. [安全、IP 保護與操作權限](#s23)
24. [Observability 與 debugging runbook](#s24)
25. [風險、降級與停止條件](#s25)
26. [交付物、完成定義與 release checklist](#s26)
27. [後續版本與架構決策](#s27)
28. [給 coding agent 的實作指令](#s28)
29. [官方來源與查核紀錄](#s29)

---

<a id="s01"></a>
## 1. 專案定位與第一版成果

### 1.1 一句話定義

**SVWeave 是 SystemVerilog-first 的本地程式碼 intelligence layer：讓 AI 先找到相關 source，再依指定 build 查證靜態語意，最後在可追蹤、可拒絕、可復原的流程中修改程式碼。**

### 1.2 三個核心問題

| 問題 | v1 提供的解法 | 不得宣稱的結果 |
|---|---|---|
| AI 反覆 grep、讀大量無關 `.sv`／`.svh` | repository graph 與 symbol navigation 縮小範圍 | 未量測前不得宣稱固定 token 節省率 |
| 同一 source 在不同 define／parameter 下有不同結構 | 明確 build profile、snapshot 與 slang elaboration 結果 | 不把 source-level graph 當成 elaborated hierarchy |
| AI 修改錯 scope、舊版本或未驗證的檔案 | 候選工作目錄、expected hash、人工批准、套用紀錄 | 不把靜態分析通過當成 simulation／formal PASS |

### 1.3 第一版必須展示的完整流程

```text
載入 demo profile
    -> 建立一致的 source snapshot
    -> 三個後端完成 capability／health 檢查
    -> graph 找到候選檔案
    -> slang 回答指定 build 的 symbol／hierarchy／reference
    -> Serena 在候選目錄修改指定 method 或 declaration
    -> 對候選目錄重新執行 slang
    -> 顯示 diff、影響範圍與尚未完成的驗證
    -> 人工批准指定 patch
    -> CAS 檢查後套用至工作目錄
    -> 重新建立目前 snapshot 與分析狀態
```

完整 demo 必須真正呼叫三個後端；不能以 mock graph、regex symbol lookup 或固定 JSON 取代其中一個，卻宣告三工具整合完成。

### 1.4 第一版的產品形態

採用 **Python orchestration package + 本地 CLI + 單一對外 MCP gateway + 獨立後端 processes**。不製作 Web UI、不訓練模型、不建立自有 LSP、不合併三個上游 repository。

`SVWeave v0.1.0` 是可重現的 IP-scale developer preview。第一個真實 SoC 專案即使尚未完成導入，也可發佈 developer preview，但必須清楚列出尚未 qualification 的限制；不得改稱 production-qualified。

<a id="s02"></a>
## 2. 假設、限制與實作原則

### 2.1 本版採用的工程假設

| ID | 假設 |
|---|---|
| A01 | 使用者有權讀取及分析目標 source；不假設任何商用 VIP 或 encrypted IP 可取得明文。 |
| A02 | 第一個目標是一個有明確 top／filelist 的 IP build，加上一個真正引用 UVM library 的小型靜態 fixture。 |
| A03 | 參考開發環境採 Linux x86_64；Windows 使用 Linux host／WSL2 連線方式屬後續 qualification，不宣稱原生完整支援。 |
| A04 | Gateway 建議使用 Python 3.12；Serena 使用獨立環境，實際可用版本由 M0 compatibility spike 凍結。 |
| A05 | v1 為單一使用者、單一 active workspace；允許多個 build profiles，但一次僅一個工作目錄寫入 transaction。 |
| A06 | v1 不要求本機 GPU。模型位於既有 MCP client；模型／API 成本不屬本工具效能數據。 |
| A07 | 目前未提供公司 source、實際 filelist、EDA invocation 或 license 設定；不虛構 register、目錄、IP 型號或公司規範。 |
| A08 | 所有資源上限、效能門檻與預設值均為設計目標，需量測後確認，並非已達成數字。 |

### 2.2 不可違反的設計原則

1. **Build-aware，而非只看副檔名。** `.sv` 可讀取不代表指定 configuration 已成功分析。
2. **Evidence-aware，而非「多數工具同意就算正確」。** 各後端只能在自身能力範圍提供證據。
3. **No silent fallback。** 解析失敗、缺檔、unsupported option、失效快取都要明確回報。
4. **No direct writes to source from upstream backends。** 原始工作目錄只能由 SVWeave write coordinator 修改。
5. **No source upload by default。** 本地索引不等於模型呼叫完全離線，必須分別管理。
6. **No false completion。** `NOT_RUN`、`BLOCKED`、`PARTIAL`、`FAIL` 不得被轉成 PASS。
7. **Small validated scope first。** v1 支援清單以 fixtures 和 qualification report 為準，不沿用上游廣告式能力宣稱。

<a id="s03"></a>
## 3. 上游能力查核與授權

### 3.1 查核結果與 SVWeave 的使用方式

| 元件 | 2026-10-13 可查證事項 | SVWeave v1 使用方式 | 仍須實測 |
|---|---|---|---|
| slang／pyslang | 上游提供 SystemVerilog parsing、type checking、elaboration、AST JSON 與 Python bindings。[S01][S02][S03] | 以 pyslang 為主要 semantic extraction 路徑；CLI 作為獨立 reproduction／diagnostic 路徑 | 所需 AST nodes、source mapping、真正 UVM fixture 與記憶體消耗 |
| Serena | 上游提供 MCP navigation／editing；SystemVerilog backend 原始碼使用 `verible-verilog-ls`。[S04][S05] | 透過獨立 MCP process 呼叫已 probe 的工具；候選目錄編輯 | 同名 method、macro、多檔 reference、工具 schema 與範圍定位 |
| codebase-memory-mcp | README 列出 SystemVerilog／Verilog，但置於尚未 benchmark 的支援語言區；HDL 不在完整 Hybrid LSP 清單。[S08] | repository 索引與候選關係；HDL 結果預設為 source-level evidence | module／interface／class／task 的實際抽取與索引 completeness |
| Verible | Serena backend 以它作為 LSP executable，並可能在 PATH 找不到時下載。[S05] | 預先安裝並 pin；不得在受限制執行環境臨時下載 | pinned binary 的 LSP capabilities 與 source range |
| MCP | 官方規格提供 tools discovery、schema 與 result contract；tool annotations 不是獨立安全邊界。[S12][S13] | Gateway 使用官方 Python SDK 的 server／client 能力。[S14] | 與選定 client、兩個上游 MCP server 的協商及 cancellation |

**不能沿用的假設：**codebase-memory-mcp 的一般 graph 效能不是 SystemVerilog benchmark；Serena 的通用 refactoring 能力不是所有 SystemVerilog 語法的保證；slang 能產生 AST 也不代表 SVWeave 已有完整 reference index。

### 3.2 必須加入 M0 的授權查核

| 元件 | 目前官方授權資訊 | 計畫要求 |
|---|---|---|
| slang | MIT。[S01] | 保留 notices，記錄 binary／binding 與 transitive dependencies。 |
| codebase-memory-mcp | MIT；需另外納入第三方 notices。[S08][S09] | 檢查 grammar／runtime assets 的第三方清單。 |
| Serena SolidLSP | MIT。[S06] | 不因它是 MIT，就推論整個 Serena application 也是 MIT。 |
| Serena application | 現行主線為 GPL-3.0-or-later；官方 LICENSE 區分元件。[S06] | 對外 package／container／修改版發佈前，針對實際版本與組合方式完成授權審查。 |
| Verible | Apache-2.0。[S10] | 收錄授權與 notices。 |

選擇 process-level adapter 是為了降低耦合與升級成本，**不是「使用 subprocess 就自動免除所有授權義務」的法律結論**。v1 不直接複製 Serena application 程式碼。SVWeave 自有程式碼的發佈授權由 owner 在 M0 確認；未確認前，不對外宣稱整套皆可用 MIT 發佈。

目前查核讀取的是浮動主線／官方文件，不是已測試的 release bundle。M0 必須建立帶 exact commit、artifact SHA-256 與 license snapshot 的 `toolchain.lock.json`；文件不得虛構已 qualification 的版本。

<a id="s04"></a>
## 4. v1 範圍與功能矩陣

### 4.1 必須交付：P0

| 功能 | v1 邊界 |
|---|---|
| Project／profile 設定 | 顯式 top、ordered sources、include dirs、defines、受控 filelist dialect、compilation units。 |
| Snapshot 與失效檢查 | dirty source、untracked input、`.svh`、generated source、external roots 與搜尋路徑成員變化均可追蹤。 |
| Static build／diagnostics | 一次完整解析／elaboration；不要求真正 incremental compiler。 |
| Symbol／reference | fixture 範圍內的 package、module、interface、class、task、function、field 與 resolved reference。 |
| Hierarchy | 指定 top 下的 module／interface instance、parameter 與 generate scope。 |
| Port connections | 直接 instance port association 與 expression；不是完整 netlist dataflow。 |
| SVA inventory | property／sequence／assertion declaration 或 instance 的位置及可解析 binding；不執行 proof。 |
| Repository search | 真正整合 codebase-memory-mcp；缺漏明示，必要時提供標記清楚的文字搜尋降級。 |
| Serena navigation／edit | 以已 probe 的讀取與編輯工具操作候選目錄；正式來源由 gateway 套用。 |
| MCP／CLI | 同一 domain layer、schema、權限與一致性規則；不能 CLI 繞過 MCP 檢查。 |
| Evidence／audit | 每項查詢與修改附帶 snapshot、build、來源、限制及執行紀錄。 |
| Tests／packaging | 真實三後端 integration test、negative tests、可重現環境與操作文件。 |

### 4.2 非 release blocker：P1

完整 EDA runner、自動匯入 UVM topology／factory／config trace、跨 profile 影響比較、多個公司 filelist dialect、semantic rename、完整 library mapping、互動式圖譜 UI、macOS／Windows 原生 qualification。

P1 可以先留 schema extension 或使用說明，但不得以空函式加測試 mock 宣稱已完成。v1 可匯入基本 EDA evidence manifest；未執行 EDA 不阻擋「靜態工具 preview」，但禁止聲稱 EDA qualification。

### 4.3 明確排除：P2／Non-goals

- 全 SoC signal bit-level connectivity、clock/reset domain analysis、CDC／RDC／timing／power signoff。
- 還原 encrypted source、替代商用 simulator／formal engine。
- 靜態確定所有 UVM factory override、config_db 命中、runtime component topology。
- 自動證明 SVA、推導 functional coverage closure 或自動產生 signoff 結論。
- 重寫 Verible、讓 Serena 直接改成 slang LSP、修改 codebase-memory-mcp 私有 DB schema。
- 多租戶服務、遠端公開 HTTP endpoint、模型訓練、向量搜尋服務或自主 push／merge。

### 4.4 語言支援宣告

| 語法／情境 | v1 宣告 |
|---|---|
| `.sv`／`.svh` | 核心目標；header 必須在正確 compilation context 中解析，不獨立當作 design unit。 |
| `.v`／`.vh` | 可納入 profile 的相容 RTL inputs；不由副檔名猜測原始 compiler language mode。 |
| package／class inheritance／parameterized class | 對固定 fixtures 建立靜態關係；runtime dispatch 另標 `dynamic_unresolved`。 |
| interface／modport | 查 source declarations 與 instance port association；不等於建立實際 UVM virtual interface 綁定。 |
| constraints／covergroup | 可列 declaration 及 source；不求解 constraints、不回報 coverage 數值。 |
| DPI／PLI | 靜態 signature 或邊界記錄；不執行 foreign code，也不分析 C/C++ runtime 行為。 |
| bind／generate／macro | 在選定 build 的受測範圍內解析；macro expansion 必須保存 origin，不直接編輯展開後文字。 |
| VHDL／AMS／mixed-language | v1 不提供完整語意；缺少邊界模型則回報 BLOCKED 或局部結果。 |

<a id="s05"></a>
## 5. 使用情境與需求追蹤

### 5.1 優先 user stories

**US01：確認實際 instantiate 哪個 module。**使用者指定 profile 與 scope，取得 instance path、module definition、parameter 值及原始位置；同時指出未解析黑盒。

**US02：避免改錯同名 UVM method。**先找完整 class scope，再對 `run_phase`／`write` 的 declaration 做定位；不使用裸 method 名直接替換。

**US03：修改 interface 或資料寬度前找候選影響。**Graph 找候選，slang 查已解析的型別及直接連接；monitor／scoreboard 等動態依賴仍保留候選清單。

**US04：確認 define／header 修改是否使快取失效。**更新 `.svh` 或新增優先 include path 中的同名 header 後，舊結果不能冒充目前 build。

**US05：修正小範圍 source 並留下證據。**在候選目錄修改、分析、顯示 diff，由人批准後套用；最終分開列出 static／EDA／simulation 狀態。

### 5.2 可追蹤 requirements

| Requirement | 要求 | 核心驗收分類 |
|---|---|---|
| SVW-R01 | SystemVerilog-first 命名、README 與 capability report | DOC |
| SVW-R02 | manifest 保留 sources／incdirs／defines／CU 語意 | BLD |
| SVW-R03 | unsupported options／missing inputs 不得靜默忽略 | BLD／NEG |
| SVW-R04 | build 識別包含內容、configuration、toolchain／extractor | SNP |
| SVW-R05 | source／macro／LSP location 正確映射 | LOC |
| SVW-R06 | scope-aware definition／reference | SEM |
| SVW-R07 | build-specific hierarchy／generate／parameters | SEM |
| SVW-R08 | 直接 port binding 不假冒完整 connectivity | SEM |
| SVW-R09 | UVM 靜態／runtime 證據分離 | UVM |
| SVW-R10 | SVA inventory 不假冒 assertion PASS | SVA |
| SVW-R11 | graph 與 Serena 皆以真實 backend 接入 | INT |
| SVW-R12 | partial／stale／truncated 查詢有明確 metadata | API／NEG |
| SVW-R13 | 舊結果及錯 scope 不得直接用於編輯 | EDT |
| SVW-R14 | backend 不能直接寫原始 source | SEC／EDT |
| SVW-R15 | 人工批准綁定 patch 與 base content | EDT／SEC |
| SVW-R16 | 多檔寫入有 journal／crash recovery，不覆蓋人類後續修改 | EDT |
| SVW-R17 | MCP negotiation／schema／cancellation／pagination 可測 | API |
| SVW-R18 | 日誌、diagnostics 與 evidence 可重現且不洩密 | OBS／SEC |
| SVW-R19 | toolchain 可 pin、offline 安裝、capability probe | DEP／INT |
| SVW-R20 | release 區分 static preview 與 EDA qualification | REL |

<a id="s06"></a>
## 6. 系統架構與責任邊界

### 6.1 對外只有一個受控入口

```text
┌─────────────────────────────────────────────────────────────┐
│ MCP Client / Human CLI                                      │
│ 讀取、分析、提案；不能直接呼叫 backend 的 unrestricted tools │
└───────────────────────────┬─────────────────────────────────┘
                            │ stdio / local CLI
┌───────────────────────────▼─────────────────────────────────┐
│ SVWeave Gateway                                             │
│ schemas | policy | job manager | provenance | error mapping  │
├───────────────────┬───────────────────┬─────────────────────┤
│ Graph adapter     │ Serena adapter    │ HDL semantic adapter│
│ MCP client        │ MCP client        │ worker IPC          │
└────────┬──────────┴─────────┬─────────┴──────────┬──────────┘
         ▼                    ▼                    ▼
 codebase-memory-mcp      Serena MCP            pyslang worker
 dedicated state          + Verible LSP          + slang CLI
         │                    │                    │
         └───────────────┬────┴────────────────────┘
                         ▼
            Immutable analysis snapshot
            / Candidate editing workspace
                         │
        ┌────────────────▼──────────────────┐
        │ Manifest + Snapshot + Semantic DB │
        │ Edit coordinator + Journal        │
        └────────────────┬──────────────────┘
                         │ approved, checked write only
                         ▼
                User source workspace
```

先前討論的「並列 MCP」保留為後端分工，不代表成品把所有寫入權限直接交給 AI。三個 raw servers 同時暴露給 client 可用於 M0 開發診斷，但 **不是具有受控編輯保證的 release mode**。

### 6.2 模組責任

| 模組 | 責任 | 禁止承擔 |
|---|---|---|
| Gateway | 驗證 request、路由、政策、狀態、schema、backend session | 自行猜測 HDL 語意或以 LLM confidence 修正事實 |
| Manifest resolver | profile 解析、ordered events、path／CU／option 分類 | 執行 Make／Tcl／shell 以「自動找到設定」 |
| Snapshot manager | 封存來源、依賴集合、hash、dirty detection | 把 Git SHA 當完整 source identity |
| Slang adapter | diagnostics、symbol、reference、hierarchy、port／SVA extraction | simulation／formal PASS |
| Graph adapter | 候選檔案與 source relations、backend coverage metadata | build-specific negative proof |
| Serena adapter | symbol navigation；在 candidate 操作已允許 editing tools | 直接 commit 原始工作目錄 |
| Write coordinator | compare-and-swap、批准驗證、套用、journal、復原 | 自動 commit／push Git，或覆蓋使用者後續修改 |
| Evidence store | 保存來源、build／run、診斷與結果範圍 | 把不同 profile／seed 結果合併成一個 PASS |

### 6.3 不共用上游私有資料庫

codebase-memory-mcp 的 `query_graph` 是唯讀 query 介面，不是通用圖譜寫入 API。[S08] v1 保留上游自己的 state；slang 結果存到 SVWeave 的 SQLite。以 source location／scope 做明確 crosswalk，不把 AST 直接灌入上游 DB。

### 6.4 隔離與信任模型

Gateway 為本地 trusted coordinator；repository content、工具輸出、提示文字與 backend response 都不能更改它的 policy。Backend 僅取得指定 snapshot／candidate 及自己的 state 目錄，不取得原始工作目錄寫入掛載。

僅隱藏 MCP tools 或設 read-only mode 不構成 OS sandbox。[S07] 正式受控寫入模式必須通過 filesystem／process isolation qualification；若 host 無法提供隔離，工具只能以明確降級模式運行，不對外宣稱已具備相同保護。

<a id="s07"></a>
## 7. 部署、依賴與版本鎖定

### 7.1 建議組成

| 項目 | 設計選擇 |
|---|---|
| Gateway | Python 3.12，typed domain models；Pydantic／JSON Schema 驗證 |
| MCP | 官方 Python SDK；server 與 upstream client adapters 分離 |
| HDL worker | 獨立 Python process，載入 pinned pyslang，避免 native crash 帶走 gateway |
| CLI | 與 MCP 共用 application services；CLI framework 選定後 pin |
| Local metadata | SQLite，migration schema；放本地磁碟，不預設把 live DB 放 NFS |
| Serena | 獨立 virtual environment，專用設定與 HOME／state，單一 active project |
| Graph backend | pinned executable＋必要 runtime assets；專用 state／daemon isolation |
| QA | pytest、type checker、linter、schema checks、integration／fault injection |
| Isolation | M0 選定一種 Linux process／filesystem sandbox；network 預設關閉 |

Serena 是 stateful server；上游文件指出同一 instance 一次僅一個 active project。[S11] v1 不讓多個不同 candidate 共用一個 Serena instance 並交錯 `activate_project`；採序列化或各自 instance。

### 7.2 `toolchain.lock.json` 最低欄位

```json
{
  "schema_version": "1.0",
  "qualification_state": "UNQUALIFIED",
  "platform": "linux-x86_64",
  "python_minor": "3.12",
  "components": {
    "slang": {"commit": null, "artifact_sha256": null},
    "pyslang": {"version": null, "wheel_sha256": null},
    "serena": {"commit": null, "dependency_lock_sha256": null},
    "verible": {"version": null, "artifact_sha256": null},
    "codebase_memory_mcp": {"commit": null, "artifact_sha256": null},
    "mcp_sdk": {"version": null, "wheel_sha256": null}
  },
  "capability_report_sha256": null,
  "license_manifest_sha256": null
}
```

這是未填寫的 plan template；`null` 不可用於 qualified release。執行 `doctor` 時需檢查：版本、hash、相依檔案、可執行權限、offline resources、protocol、tools schema、language server capabilities。若 slang CLI 與 pyslang 使用不同版本，標為未配對，不得把兩者結果宣稱為同一 backend version。

### 7.3 安裝與啟動政策

首次安裝可以使用經批准的網路；正常分析及 editing 不得自動更新 binary、下載 grammar 或更改 client 全域設定。Serena 找不到 Verible 時可能觸發下載，故必須在啟動前驗證 pinned Verible 位於受控 PATH。[S05]

Gateway、Serena、pyslang 不共用一個任意升級的 virtual environment。不得把 `main`、`latest` 或無 version 的 install command 當成 release reproduction procedure。

### 7.4 Backend contract probe

保存 initialization、協商 protocol version、`tools/list`、工具 input/output schemas、Serena LSP capabilities、graph schema／索引狀態與一個真實 fixture query。禁止只因 server process 存活就回報 READY。

若上游版本只提供不符合安全要求的通用 REPL，v1 禁止將該 REPL 透傳給 AI；改用可控 tool-interface 版本／設定，並重跑 qualification。[S15]

<a id="s08"></a>
## 8. Build manifest 與 filelist adapter

### 8.1 設定檔分工

| 檔案 | 性質 | 是否由 repository 自動取得執行權限 |
|---|---|---|
| `svweave.yaml` | 專案／profile 宣告 | 否；僅資料，不能執行 shell 或提升權限 |
| `svweave.local.yaml` | host-specific root mapping，通常不進 Git | 否；由人建立並依 schema 驗證 |
| `toolchain.lock.json` | 已 qualification 的工具 pin | 否；artifact hash 仍須檢查 |
| `policy.json` | 允許 roots、寫入範圍、批准方式、network | 由 trusted CLI 建立，不能被 repo config 放寬 |
| `.svweave/manifests/<id>.json` | resolved manifest、argument ledger | 產物，不手動編輯 |

未知欄位一律 schema error，避免拼字錯誤被靜默忽略。Profile 只能收緊 trusted policy，不能增加可讀根目錄、允許網路或取消批准。

### 8.2 最小完整 manifest 範例

以下是 SVWeave 自訂格式，不是三個上游的原生 configuration。路徑與 top 均為本計畫 demo；需由 fixtures 工作項目建立。

```yaml
schema_version: "1.0"
project:
  id: "svweave-demo"
  root: "."
  default_profile: "rtl_demo"

source_roots:
  - id: "project"
    path: "."
    include: ["fixtures/**"]
    exclude: ["**/out/**", "**/waves/**"]
    writable: false

profiles:
  rtl_demo:
    cwd: "."
    language_mode: "systemverilog"
    source_inputs:
      - kind: "filelist"
        path: "fixtures/demo/filelists/rtl.f"
        dialect: "svweave-filelist-v1"
        path_mode: "cwd"
    compilation_units:
      mode: "single"
    top_modules: ["demo_top"]
    defines:
      - name: "SVW_DEMO"
        value: "1"
    include_dirs: ["fixtures/demo/include"]
    top_parameters:
      - top: "demo_top"
        name: "DATA_W"
        value: "32"
      - top: "demo_top"
        name: "LANES"
        value: "2"
    library_dirs: []
    library_files: []
    library_extensions: [".v", ".sv"]
    environment_allowlist: []
    generated_inputs: []
    diagnostic_policy: "no_errors"

analysis:
  missing_input_policy: "block"
  unsupported_option_policy: "block"
  stub_policy: "explicit_only"
  max_build_seconds: 120
  max_worker_memory_mib: 8192

editing:
  enabled: false
  allowed_paths: ["fixtures/demo/**"]
  max_files_per_patch: 5
  max_changed_lines: 200
  require_human_approval: true
  validate_profiles: ["rtl_demo"]
```

`source_roots[].writable` 只能指原始 source 是否列入可提案範圍，不代表 backend 得到實際寫入權。範例維持 false 與 editing disabled；啟用 editing 必須由 trusted CLI 設定政策。`max_*` 是本案初始限制，不是 upstream 參數。

`compilation_units.mode` 支援 `single`、`per_file`、`explicit_groups`。`explicit_groups` 必須帶有有序 groups，每個 group 明確列 sources、include dirs、defines、library；不能把多次 EDA compile command 粗略串成 single unit。

### 8.3 Filelist 支援與拒絕規則

| 輸入 | v1 處理 |
|---|---|
| Source file path | 保留順序、原始 token、origin file／line、resolved path。 |
| `-f file` | 依指定 invocation cwd 解析 filelist 中相對路徑；nested 規則保留。 |
| `-F file` | 依該 filelist 位置解析相對路徑。 |
| `+incdir+...`、`-I ...` | 有序 include directories，不排序、不任意去重改變優先次序。 |
| `+define+NAME=VALUE`、`-D...` | 保留字串及來源；含引號／特殊字元須做 token-level tests。 |
| `-v`、`-y`、`+libext+...` | 轉換為已測試的 library inputs；載入範圍必須可封存。 |
| Env variables | 僅 allowlist 且已提供者可展開；缺值回報，不讀取任意環境。 |
| Comments／quoted paths | 依 SVWeave dialect 的明文規則解析；不得使用 Python `eval` 或 shell。 |
| Nested cycle／無限 recursion | BLOCKED，回報完整 include chain；預設最大深度 32。 |
| Make／Tcl／shell substitution／任意 executable | 不執行；要求匯出的 literal filelist 或 resolved manifest。 |
| 未支援 vendor option | BLOCKED，列出 option、來源與必要處置；不自動丟棄。 |
| 已確認純輸出／log option | 只有 allowlist 明確分類者可忽略，且寫入 `ignored_options` ledger。 |
| Native compiled library／複雜 library map | 沒有可用 source 或未通過 mapping fixture 時 BLOCKED；不等價轉成空 module。 |

slang 官方對 `-f`／`-F`、compilation units 與 `-C` 的說明是 adapter 測試依據；不是對所有 EDA filelist dialect 的通用保證。[S02]

### 8.4 Resolution algorithm

```text
1. 驗證 schema、profile、trusted roots、toolchain。
2. 以 profile cwd 建立 path context；禁止執行任意命令。
3. 遞迴解析 source inputs，產生有序 argument/event ledger。
4. 驗證 paths、env、quotes、重複／衝突 macro、CU 與 library 定義。
5. 分類每個 option：semantic / diagnostic / approved-ignore / unsupported。
6. 將全部來源與可能參與 include/library lookup 的核准檔案封存。
7. 生成 normalized manifest 與 slang adapter input。
8. 執行分析並收集實際 dependencies、diagnostics 與 unresolved items。
9. 比對實際讀取依賴與封存集合；有外漏或變動即拒絕發布結果。
```

v1 不實作 profile inheritance，避免 merge precedence 不透明。重複且衝突的 macro／parameter 設定預設拒絕；只有 dialect 已測試的 override 行為才能被保留並記錄。不得「為了讓 build 過」自動添加 `UVM_NO_DPI`、忽略 unknown modules 或建立 stubs。

### 8.5 Generated inputs 與工具差異

Generated source 必須在 build 前存在，連同 generator command ID、已知 input hashes、output hashes 一起記錄；缺少 generator provenance 可標記 `provenance_incomplete`，但不得假裝知道如何重建。v1 不自動執行 repository generator。

對 slang 特有 macro、vendor extension、timeunit／timescale、語言模式與 legacy compatibility flags，保存 `semantic_deviations`。必要時可建立獨立的 analysis profile，但必須與原 EDA profile 區分；不能修改 source 或偷偷設 define 後，仍宣稱與原 build 相同。

<a id="s09"></a>
## 9. Snapshot、快取與一致性

### 9.1 四種 identity 不混用

```text
snapshot_id  = SHA256(有序／標準化的檔案 inventory + source bytes hashes
                      + root/path mapping + lookup search-space inventory)

config_id    = SHA256(normalized build semantics，保留所有有意義順序)

toolchain_id = SHA256(binary／wheel／asset hashes + adapter/extractor version)

build_id     = SHA256(snapshot_id + config_id + toolchain_id + semantic_schema_version)
```

`job_id` 是每次 attempt 的唯一 ID，不等於 deterministic `build_id`。同一輸入重試可以有同一 build ID，但需保留不同 job 的 timeout／crash／成功紀錄。

JSON object keys 可 canonical sort；source、include path、macro events、compilation groups 等 array **不可為了穩定 hash 任意排序**。

### 9.2 Snapshot 建立策略

v1 採保守做法：複製允許 roots 中所有符合 input／header／library lookup 規則的檔案至 immutable snapshot；不只複製 Git tracked files，也不只追蹤上次編譯讀到的 headers。外部 UVM／VIP source 由顯式 root mapping 納入；不允許隱藏的 filesystem dependency。

Hash 前後核對 inventory 與內容，若 source 在 capture 中改變則有限次重試，仍不穩定時回報 `WORKSPACE_UNSTABLE`。Snapshot 不使用可被後端改到原檔的 hardlink；symlink 解析須符合 trusted roots，並記錄實際 target。

### 9.3 為何需要 lookup inventory

```text
include search order = dir_a, dir_b
原本 common.svh 只存在 dir_b
之後新增 dir_a/common.svh
```

即使 `dir_b/common.svh` 完全未改，實際選中的 header 已可能不同。因此新增／刪除候選檔案、library search 成員變化與 negative lookup 也必須讓相關 snapshot 失效。

### 9.4 Path mapping 與 `__FILE__`

保存 `logical_path`、`original_path`、`snapshot_path` 三者關係。Sandbox 優先以穩定 logical mount path 呈現 source，避免搬到隨機 temp 路徑改變 `__FILE__` 等可觀察字串。Source-map 本身不能修復已改變的語意；無法保持 path semantics 時必須記錄差異，不能宣稱 bit-for-bit 等價。

### 9.5 State 分離

| 維度 | 值 | 意義 |
|---|---|---|
| Job lifecycle | `QUEUED / RUNNING / SUCCEEDED / FAILED / CANCELLED / TIMED_OUT` | 工作有沒有完成，不代表設計正確。 |
| Analysis completeness | `COMPLETE / PARTIAL / BLOCKED` | 僅相對於宣告的 profile、queries 與支援範圍。 |
| Freshness | `CURRENT / STALE / HISTORICAL / UNKNOWN` | 與目前工作目錄的關係。 |
| Diagnostic outcome | `NO_ERRORS / HAS_ERRORS / NOT_AVAILABLE` | 是否有工具 error；warning 依 policy 評估。 |
| Backend availability | `READY / DEGRADED / UNAVAILABLE` | 某個後端是否可用。 |

成功執行 compiler，但來源有 errors：job 可為 `SUCCEEDED`，analysis 為 `PARTIAL`，diagnostics 為 `HAS_ERRORS`；不得把 job success 顯示成「設計 PASS」。

### 9.6 Cache／publish policy

唯讀查詢綁定 immutable build ID。新結果先寫 staging DB transaction，完整驗證後才更新 published pointer；失敗不能覆蓋上一個可用 snapshot。對舊 build 的顯式查詢可回傳 `HISTORICAL`，但它不能用於目前 source 的 edit commit。

v1 對任何 relevant input 變化執行完整 semantic rebuild；不宣稱使用增量 elaboration。Graph 若支援 auto-sync，仍需對照 snapshot ID，不能把 watcher 狀態當作一致性證明。Source inventory 每次 publish／commit 都重新核對。

GC 僅刪除無 active job、無 pending edit、無 evidence retention reference 的 snapshots。預設保留最近 3 個成功 snapshot 及所有尚未結案 transaction；容量上限由本地 policy 設定，不能默默刪除必要證據。

<a id="s10"></a>
## 10. slang semantic adapter

### 10.1 技術路徑

採 **pyslang worker → 自訂 extraction model → SQLite／JSON**。CLI AST JSON 用於原型、重現與交叉核對，不把 raw AST 格式直接公開為 SVWeave API。[S01][S03]

M0 先驗證 pinned pyslang 暴露所需 AST／symbol／source-map 能力；若某個核心 accessor 不可用，應提明確 ADR，選擇小型 C++ helper 或經測試的 CLI extraction。不得在無 API 時臨時以 regex 冒充 resolved references。

### 10.2 Worker pipeline

```text
收到受驗證 manifest / snapshot
  -> 建立 compiler driver / source manager
  -> 載入 compilation groups、defines、library、top／parameter
  -> parse、name resolution、elaboration
  -> 完整收集 diagnostics，處理 lazy analysis 的邊界
  -> extraction：declarations -> references -> hierarchy -> bindings -> SVA
  -> 收集實際 dependency 與 unsupported-node inventory
  -> 檢查一致性、序列化 DTO、發布 build result
```

上述是實作流程，不是宣稱特定 Python method 名稱已查證。必須以 pinned binding 的 examples／tests 實作，不自行猜 API signatures。需要保留 compilation／source manager 生命週期，不能保存失去 owner 的 native object reference；進程間只傳明確 DTO。

### 10.3 P0 extraction 清單

| 類別 | 欄位／範圍 |
|---|---|
| Declaration | kind、name、qualified scope、file/span、CU、library、type、macro origin。 |
| Class／member | inheritance、parameter specialization、task／function、static member、constraint／covergroup declarations。 |
| Reference | source occurrence、resolved target、reference kind、resolution status；動態或無法解析者保留原因。 |
| Instance | instance path、definition ID、parent、parameters、generate context、是否 blackbox／stub。 |
| Port association | port declaration、direction、actual expression、references in expression、width/type 若可取得。 |
| SVA | property／sequence、assert／assume／cover kind、scope、source、clock/reset expression 若可解析、bind target。 |
| Diagnostic | severity、code、message、primary location、related locations、macro expansion notes。 |

### 10.4 Reference completeness 的限制

Query 必須回傳 `searched_scope` 與 `resolution_basis`。對 library 中未展開的 code、未使用的 generic specialization、未分析的 inactive branch、runtime virtual dispatch，不宣稱已找到「所有 reference」。Positive resolved edges 可以使用；負向結論需確認範圍完整且功能受支援。

不使用 arbitrary confidence 百分比替代狀態。例：`resolved_static`、`candidate_source`、`dynamic_unresolved`、`unsupported`、`inactive_for_profile`。

### 10.5 Port connections 的正確定位

```text
module port P -> actual expression {bus_hi, bus_lo}
```

v1 保存 expression 與引用的 symbols；可以列寬度，但不把 `{bus_hi,bus_lo}` 壓成單一 signal，也不把同名 signal 當已連接。方向是 port 的宣告方向，不等於整個設計的唯一 driver／sink。Clocking block、tran／alias、force/release、跨層 continuous assignment 的完整追蹤不在 v1 保證內。

### 10.6 CLI 重現範例

僅在 M0 選定的 slang release 支援這些已公布選項時使用；`rtl.f` 路徑以專案根目錄為 cwd。以下是上游 CLI，不是 SVWeave 已完成工具。[S03]

```bash
mkdir -p .svweave/debug
slang -f fixtures/demo/filelists/rtl.f \
  -I fixtures/demo/include \
  -DSVW_DEMO=1 \
  --single-unit \
  --top demo_top \
  --ast-json .svweave/debug/design.ast.json \
  --ast-json-source-info \
  --all-deps .svweave/debug/dependencies.txt
```

正式 runner 必須用 argument array、指定 cwd、timeout、resource limits；不把上述 command 拼成不受控 shell string。AST JSON 檔存在不等於分析完整。

<a id="s11"></a>
## 11. codebase-memory-mcp adapter

### 11.1 P0 接入能力

透過上游公開 MCP tools 探索與使用 `index_repository`、`index_status`、`search_graph`、`get_graph_schema`、`get_code_snippet`；若版本提供 `check_index_coverage`／`search_code` 可額外使用。實際 schema 由 M0 probe 凍結，不能根據工具名字自行猜參數。[S08]

所有 query 附帶 SVWeave 對應 snapshot ID、upstream project ID、index generation／fingerprint。若上游無法直接提供內容 fingerprint，SVWeave 必須在自己的 immutable snapshot 上建立索引並保存對應，不從相同名字推定是相同內容。

### 11.2 特別處理

- 專用 state／daemon；不連到使用者其他 repository 的全域 daemon。
- 不暴露 `delete_project`、任意 ADR writes、安裝 hooks 或跨 repo linking 等不需要的能力。
- 不修改 graph schema、不直接 SQL 寫入上游 DB。
- 遇到 indexing 非同步／polling，adapter 處理其 lifecycle；Gateway 的 query 不可搶讀半成品。
- 上游 check 顯示「沒有已知缺漏」不等於證明所有 source／symbols 完整。[S08]

### 11.3 Fallback

Graph 不可用時，`svw_search` 可做 bounded literal text search 或查自身 declaration index，但 `backend_used` 必須寫出真實來源，`degraded=true`。對使用者保留可用性，不代表三工具 release gate 已通過。包含 regex 的查詢要限制 pattern／執行時間，不允許無界 catastrophic regex。

v1 不承諾把 RTL instantiation 映射成上游通用 `CALLS`；正確的 hierarchy 關係由 slang index 提供。

<a id="s12"></a>
## 12. Serena adapter 與受控編輯

### 12.1 與 Serena 的整合方式

P0 使用 MCP process adapter，不 import Serena application internals。啟動前指定專用 project／configuration／state，禁用不必要 dashboard、shell、REPL、activation hooks 與任意 project switching；實際 flag／config 欄位以 pinned 版本為準。[S11][S15]

`find_symbol`、reference lookup、symbol replacement 等只是預期能力，必須從 `tools/list` 與 fixture probe 建立能力表。若 SystemVerilog backend 對某種 operation 不可靠，只允許已通過的子集。

### 12.2 編輯分成三條路徑

| 路徑 | 適用條件 | 執行 |
|---|---|---|
| Serena symbol edit | scope／range 唯一，且 grammar 在 qualification 內 | 在 candidate 由 Serena 實際產生修改，再由 gateway 驗證 diff。 |
| Exact-range patch | semantic edit 不支援，但 original bytes／span 明確 | Gateway 在 candidate 依 expected hash／bytes 做 replacement；標記不是 Serena semantic edit。 |
| Refuse | macro expansion only、ambiguous scope、stale／partial authority、超出 policy | 回傳具體 reason；不自行挑選看起來最像的 target。 |

v1 至少有一個真實 SystemVerilog method replacement 必須走第一條，證明 Serena 的 editing integration 不是空殼。一般跨專案 rename 不在 P0。

### 12.3 Transaction state machine

```text
PREPARED -> CANDIDATE_READY -> VALIDATING -> VALIDATED
                                           │
                                           ▼
                                      AWAITING_APPROVAL
                                           │
                                           ▼
                                        APPROVED
                                           │
                                           ▼
                                       COMMITTING
                                           │
                          ┌────────────────┴────────────────┐
                          ▼                                 ▼
                       APPLIED                        RECOVERY_REQUIRED
                          │
                          ▼
                 POST_APPLY_VERIFIED

任何未套用狀態可 -> ABORTED / EXPIRED / CONFLICT / VALIDATION_FAILED
```

### 12.4 Prepare／validate／commit 的強制檢查

**Prepare：**明確綁定 base snapshot／build、decl ID 或 exact source span、original hash、replacement text、允許 profiles。複製為獨立 candidate；不與原檔 share writable inode。

**Validate：**核對 Serena 僅修改允許 files；產生 patch hash、行數及 file count；重新分析所有 `validate_profiles`。預設 baseline 和 candidate 都必須 `NO_ERRORS + COMPLETE`，且不得引入新的 non-waived warning。若 baseline 不完整，仍可提供修改建議／candidate diff，但 v1 不自動 commit 該修復。

**Approve：**使用 trusted 本地 CLI，由人核准 patch hash、base snapshot、profile set、policy hash、expiry。批准儲存在 agent 不可改寫的本地記錄；MCP 的 `approved: true` 不算批准。任何 patch 或 policy 改動都使批准失效。

**Commit：**重新核對目前所有 relevant inputs 與 base snapshot，取得 workspace lock，逐檔驗證 expected hash，建立 journal，再套用。缺少 approval、過期、TOCTOU conflict、toolchain 改變或 candidate 未驗證時拒絕。

### 12.5 多檔寫入與復原

一般 filesystem 的多個 file replace **不是整體原子 transaction**。v1 僅能做到逐檔 atomic replace、write-ahead journal、cooperative workspace lock 與 interrupted-write recovery；不能聲稱其他 editor 永遠看不到中間狀態。

分析服務只讀 immutable snapshots，所以不會把半套用工作目錄發布為完整新 build。恢復時僅在檔案仍等於本 transaction 的 after-hash 時才可還原；若人類已再修改，標記 conflict，保留兩份內容，不盲目 rollback。禁止用 `git reset --hard` 或 `git checkout .` 清理工作目錄。

v1 edit 限制為既有文字檔 replacement；新增／刪除／rename、binary、symlink／hardlink target edits 不在自動 commit 範圍。

### 12.6 套用後驗證

Commit 後重建 workspace snapshot；確認 bytes 等於 candidate 的預期改動，再重新分析或重用經嚴格 content／config／toolchain 一致性證明的結果。原 build 保留為 HISTORICAL。正式更新結果要列：files changed、patch hash、static diagnostics delta、EDA／simulation 的 `NOT_RUN` 或 evidence ID。

<a id="s13"></a>
## 13. 資料模型與證據規則

### 13.1 Source、declaration 與 instance identity

| ID | 建立基礎 | 注意事項 |
|---|---|---|
| `source_id` | snapshot＋logical file path＋file bytes hash | 同一檔案在不同 snapshot 不假設是相同內容。 |
| `declaration_id` | source occurrence＋declaration span＋kind＋qualified scope＋CU／library＋macro context | 同一 header 在不同 CU／macro 情境可對應不同 semantic declarations。 |
| `symbol_id` | build＋declaration／specialization | 與上游 graph node ID 分開，不從名稱拼字推定相等。 |
| `instance_id` | build＋完整 instance path＋specialization | 同一 module definition 多次 instantiate 是多個 instance。 |
| `reference_id` | build＋source occurrence＋reference kind＋target／unresolved reason | 同名 identifier 不自動合併。 |
| `evidence_id` | immutable evidence record content hash | 外部證據的 hash 只證明記錄未改，不證明內容本身真實。 |

跨 snapshot matching 是另一個明確操作，不重用舊 declaration span 當成新版本位置。v1 不實作自動猜測 source identity 的跨版本 semantic merge。

### 13.2 主要 SQLite tables

| Table | 主要欄位／index |
|---|---|
| `workspaces` | workspace ID、trusted root mapping、policy hash |
| `snapshots` | snapshot ID、inventory hash、capture metadata、retention refs |
| `source_files` | snapshot ID、logical path、bytes hash、size、origin |
| `builds` | build ID、config／toolchain／extractor IDs、completeness、diagnostic outcome |
| `jobs` | job ID、build ID、lifecycle、timings、exit code、error |
| `declarations` | declaration／symbol ID、scope、kind、source span、type、CU |
| `references` | occurrence、target ID、kind、resolution status；index on target ID |
| `instances` | instance ID、parent、path、definition、specialization；index on build/path |
| `port_bindings` | instance、formal port、actual expression、referenced symbols、types |
| `assertions` | source／elaborated scope、assertion kind、property／sequence／bind metadata |
| `diagnostics` | build/job、severity、code、locations、message、suppression metadata |
| `backend_indexes` | snapshot、backend/version、upstream project、index state、schema hash |
| `crosswalks` | upstream node ID、source/symbol candidates、match method、ambiguity |
| `edit_transactions` | base/candidate、patch hash、state、approval ID、journal location |
| `evidence` | type、producer、build/run identity、artifacts、trust class、outcome |

不要求建立 generic graph DB。關係用 foreign keys 與明確 tables 表達，v1 先滿足已定義 queries。SQLite connection／transaction 由 storage layer 控制；大型 raw AST 只作除錯 artifacts，不常駐 DB。

### 13.3 Evidence categories

| Evidence kind | 可支持的敘述 | 不能支持 |
|---|---|---|
| `SOURCE_CANDIDATE` | 某 source 中有名稱／語法／候選關係 | 指定 build 已實際使用它 |
| `LSP_RESOLVED` | pinned LSP 在指定 snapshot 對該查詢的結果 | 獨立保證完整 elaborated connectivity |
| `HDL_STATIC_RESOLVED` | slang 對指定 profile／snapshot／scope 解析得到的關係 | runtime factory／config_db 或 simulation PASS |
| `RUNTIME_OBSERVED` | 某個 test、seed、run 的實際記錄 | 對所有 tests／seeds 都成立 |
| `EXTERNAL_DECLARED` | 外部提供者聲稱的結果及 artifacts | 已被 SVWeave 執行或獨立驗證 |
| `UNRESOLVED` | 存在限制／缺失／動態依賴 | 不存在該依賴 |

這不是可混用的單一高低排名。Runtime 可證明該次執行觀察；static 可分析未執行的程式範圍；兩者不能互相取代。

### 13.4 Source location contract

內部 canonical span 為 **original file bytes 的 zero-based、half-open `[start_byte,end_byte)`**。對外可另提供 human-readable line／column，但要附 `position_encoding` 與 base。LSP 的 line／character 是 zero-based，character 計數受協商 encoding 影響；不能把 UTF-16 character 當 UTF-8 byte。[S18]

至少保存：original file/span、spelling location、macro expansion／invocation location、logical source path、file hash。`line` directive 的顯示位置與實際可編輯檔案位置必須分開。CRLF、UTF-8 中文註解、tab、escaped identifier、重複 include 皆有 fixture。

遇到一個 occurrence 對應多個 macro origins，read query 可列出候選，但 editing 必須拒絕 ambiguous origin。不得修改預處理暫存 AST 的虛擬文字。

<a id="s14"></a>
## 14. MCP API contract

### 14.1 Transport 與版本政策

v1 對外使用本地 stdio；不提供公開 HTTP service。採官方 SDK 處理 protocol framing，不自行手寫 JSON-RPC parser。MCP 版本與 compatibility 行為由 M0 對選定 client／backend 實測後 pin；2026-10-13 的官方 latest 文件導向 `2026-07-28`，不能據此假設所有上游皆已相容。[S12][S13]

較舊 MCP revision 的 initialization 與新 revision 的 per-request metadata 不同；adapter 必須依 SDK／協商結果處理，不把某一年代的 handshake 寫成所有後端的固定假設。[S13]

### 14.2 共通規則

- Session 綁定一個已授權 workspace；API 不接受任意 server filesystem root。
- Semantic query 必須提供 `build_id`；source／graph query 必須提供 `snapshot_id`。
- `limit` 預設 100、上限 500；最大文字回應預設 128 KiB。超過時回傳 cursor 與 `truncated=true`。
- Input schema 禁止未知欄位；IDs、paths、ranges、enum、字串大小全部檢查。
- 大型 build 使用 job ID 查詢／取消；禁止 background job 無限重試或繞過資源限制。
- Protocol error、tool execution error、合法的 PARTIAL domain result 必須可區分。
- stderr 放 logs；stdout 只放符合所用 transport 的 protocol 訊息。[S13]
- Cursor 綁定 build/snapshot、query hash、排序方式與偏移，不可跨 query 混用。

### 14.3 v1 對外工具清單

所有下列 `svw_*` 名稱皆為自訂待實作 API；不是任何上游現成工具。

| Tool | 必要輸入／選項 | 主要輸出 | 寫入性質 |
|---|---|---|---|
| `svw_status` | 可選 `profile` | backend health、capabilities、目前 build、freshness、pending edit | 唯讀 |
| `svw_build` | `profile`；可選 `expected_snapshot_id`、`reuse` | job ID，完成後有 build ID、diagnostics | 寫 metadata／cache，不改 source |
| `svw_job` | `job_id`、`action=status/cancel` | lifecycle、progress、error | cancel 可影響 worker |
| `svw_search` | `snapshot_id`、`query`；scope／limit／cursor | graph/source candidates、backend、限制 | 唯讀 |
| `svw_symbol` | `build_id`；`symbol_id` 或 `location` 二選一 | declaration、type、scope、origins、候選 ambiguity | 唯讀 |
| `svw_references` | `build_id`、`symbol_id`；scope／limit／cursor | resolved 與 unresolved occurrences，分析範圍 | 唯讀 |
| `svw_hierarchy` | `build_id`；`instance_id` 或 top；depth／limit | instance tree、parameters、generate、blackboxes | 唯讀 |
| `svw_connections` | `build_id`、`instance_id`；可選 port | direct formal/actual associations | 唯讀 |
| `svw_assertions` | `build_id`；scope／limit／cursor | source/elaborated SVA inventory、binding、未知部分 | 唯讀 |
| `svw_edit` | `action=prepare/validate/commit/abort`；見下節 | transaction、diff、validation、apply state | 需保守標記為可能改寫 |
| `svw_evidence` | `build_id` 或 `evidence_id`；type filter | provenance、static／external run 狀態、artifact metadata | 唯讀 |

Tool annotation 必須誠實，但不是權限控制。`svw_build` 有 metadata side effects，`svw_edit` 混合動作，不能全部標成 read-only。[S12]

### 14.4 `svw_edit` discriminated schema

| Action | 必要欄位 | 行為 |
|---|---|---|
| `prepare` | base build／snapshot、`edits[]`、`validate_profiles[]`、request key | 建立 candidate；`edits` 為 symbol replacement 或 exact-range replacement。 |
| `validate` | transaction ID | 固定 candidate hash 後分析；回傳 patch／diagnostics，進入待批准。 |
| `commit` | transaction ID、expected patch hash | 只查 trusted approval store；不接受 AI 自稱批准。 |
| `abort` | transaction ID | 清理未套用 candidate，保留 audit；不撤銷已套用的人類修改。 |

每個 branch 以 `oneOf`／discriminator 明確驗證，不用一個全部 optional 的巨大 object。`prepare` 與 `commit` 具 idempotency key；同 key 不同 payload 回 `IDEMPOTENCY_CONFLICT`。成功後重送 commit 回原結果，不再次修改。

### 14.5 共通結果 envelope 範例

以下為示意內容，ID 並非真實專案結果。`analysis_coverage` 指查詢／抽取範圍，不是 UVM functional coverage。

```json
{
  "schema_version": "1.0",
  "request_id": "req-demo-001",
  "outcome": "OK",
  "workspace_id": "svweave-demo",
  "snapshot_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "build_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "profile": "rtl_demo",
  "freshness": "CURRENT",
  "analysis_coverage": {
    "completeness": "COMPLETE",
    "scope": "demo_top",
    "basis": "qualified_static_subset",
    "unresolved_count": 0,
    "limitations": ["No runtime or formal verification is represented."]
  },
  "provenance": {
    "backend": "slang",
    "toolchain_id": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "evidence_kind": "HDL_STATIC_RESOLVED"
  },
  "data": {
    "items": [{"instance_path": "demo_top.g_lane[0].u_lane"}]
  },
  "pagination": {"limit": 100, "next_cursor": null, "truncated": false},
  "warnings": [],
  "error": null
}
```

當 `PARTIAL`、`STALE` 或 `truncated=true` 時，client 不得把沒有出現在當前 page 的資料解釋成不存在。若工具請求成功但 input build 有 errors，仍回完整 diagnostics metadata，而不是拋成模糊 server crash。

### 14.6 Error catalog

| Code | Expected handling |
|---|---|
| `INVALID_CONFIG` | 回報欄位／位置，不啟動 backend。 |
| `MISSING_INPUT` | 列必要 file／env／include chain，停止完整 build。 |
| `UNSUPPORTED_OPTION` | 列 option origin；不刪除後重試。 |
| `PATH_NOT_ALLOWED` | 拒絕，不洩漏核准 roots 外檔案內容。 |
| `WORKSPACE_UNSTABLE` | capture／commit 中來源改變；要求建立新 snapshot。 |
| `STALE_SNAPSHOT` | read 可顯式 historical；edit 拒絕。 |
| `AMBIGUOUS_SYMBOL` | 列 scoped candidates；不自動挑一個編輯。 |
| `PARTIAL_ANALYSIS` | 提供有效局部結果及缺失，不宣稱 completeness。 |
| `BACKEND_UNAVAILABLE` | 報出 backend／版本／原始 reason，允許有標記的 read fallback。 |
| `CAPABILITY_MISMATCH` | 未知 schema／缺少必要能力，停用對應功能。 |
| `BUILD_TIMEOUT`／`RESOURCE_LIMIT` | 終止 worker tree，保留 job evidence，不發布完整結果。 |
| `EDIT_CONFLICT` | 保留原始及 candidate，不覆蓋。 |
| `APPROVAL_REQUIRED`／`APPROVAL_EXPIRED` | 不改原始 source。 |
| `POLICY_DENIED` | 列明違反規則；不得降級繞過。 |
| `RECOVERY_REQUIRED` | 阻擋新寫入，依 journal 逐檔核對。 |
| `OUTPUT_LIMIT` | bounded data／cursor；不截斷成無效 JSON。 |

<a id="s15"></a>
## 15. CLI 與操作流程

### 15.1 計畫中的 CLI

```text
svweave init
svweave doctor
svweave manifest resolve --profile <profile>
svweave snapshot create --profile <profile>
svweave build --profile <profile>
svweave query search --snapshot <id> --query <text>
svweave query hierarchy --build <id> --top <top>
svweave query symbol --build <id> --file <path> --byte <offset>
svweave query references --build <id> --symbol <id>
svweave query connections --build <id> --instance <id>
svweave query assertions --build <id> --scope <scope>
svweave serve --transport stdio
svweave edit prepare --spec <json>
svweave edit validate --transaction <id>
svweave edit approve --transaction <id> --patch-hash <hash>
svweave edit commit --transaction <id> --patch-hash <hash>
svweave edit abort --transaction <id>
svweave edit recover --transaction <id>
svweave evidence import --manifest <json>
svweave report --build <id> --format json
svweave cache gc --dry-run
```

這些是 v1 待實作 command contract。`init` 不覆寫既有檔案、不修改全域 MCP client、不自動註冊 Git hooks。`edit approve` 只允許 trusted human session；MCP agent 不透過 shell 或檔案寫入獲得同等權限。

### 15.2 Read-only 導入順序

執行 `doctor` → 建立 manifest → resolve ledger → 人工核對 top／file order／CU → build → 查詢 sample symbols／hierarchy → 確認 graph／Serena 對同一 snapshot 工作 → 輸出 qualification report。以上完成前，editing 維持 disabled。

### 15.3 日常修改順序

先查目前 status；對 CURRENT build 取得完整 source scope；prepare candidate；validate；review diff 與 limitations；人核准；commit；post-apply verify；必要時在既有 EDA 環境執行選定 tests，再匯入 evidence。AI 不自行因 static passed 而更改測試完成狀態。

### 15.4 CLI exit code 設計

| Code | 意義 |
|---|---|
| 0 | 該 command 的明確成功條件成立；不是泛指設計 PASS。 |
| 2 | schema／使用方式／configuration 錯誤。 |
| 3 | build 有 errors、PARTIAL 或 BLOCKED，未達 `build` 成功條件。 |
| 4 | backend／resource／timeout 問題。 |
| 5 | stale／edit conflict／recovery required。 |
| 6 | policy／approval 拒絕。 |

查詢 historical／partial read 若使用者明確允許，可以 exit 0 並在 structured result 標示；預設 strict 的 `build` 不採此規則。文件與 tests 必須對各 command 固定語意。

<a id="s16"></a>
## 16. UVM、SVA 與 EDA 邊界

### 16.1 UVM 靜態分析可以交付什麼

v1 必須真實引用一個 pinned UVM source library，分析 component／sequence class、inheritance、task／function、parameterized classes、factory macro 展開與 `config_db` call sites。不是只做一組名稱含 `uvm_` 的假 class。

P0 能回答「哪裡宣告／呼叫／引用」及「此 build 可解析的型別是什麼」。不能只看到 `type_id::create` 就保證實際 instance type；factory instance／type override 會影響建立結果。[S16]

`uvm_config_db::set/get` 的靜態位置與實際命中／precedence 不同。[S17] 有 `set` 並不代表某個 `get` 必然成功；未找到 literal `set` 也不代表 runtime 一定失敗。

### 16.2 v1 runtime evidence 規劃

先保留外部 evidence manifest 的 import／read，不在 P0 實作所有 simulator log parsers。v1 import 必須做 schema、artifact hashes、path policy、source/build 關聯檢查；沒有受信任 runner 的 producer record 時標記 `EXTERNAL_DECLARED`。

```json
{
  "schema_version": "1.0",
  "kind": "simulation",
  "producer": "external_runner",
  "trust_class": "EXTERNAL_DECLARED",
  "profile": "uvm_smoke",
  "snapshot_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "build_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "test_name": "svw_smoke_test",
  "seed": "12345",
  "run_id": "demo-run-001",
  "tool_version": "TO_BE_RECORDED_BY_RUNNER",
  "result": "NOT_RUN",
  "artifacts": []
}
```

未提供 source snapshot 或無法證明其對應時，只能保存 unlinked evidence，不能附加為目前 build 的 PASS。Hash 不能證明 report 由真正 simulator 產生；來源可信度與內容完整性必須分開。

### 16.3 SVA 靜態與執行結果

| 靜態 inventory | Simulation／formal evidence |
|---|---|
| declaration、assert／assume／cover 類型 | property pass／fail |
| clock／disable expression source | antecedent activation／vacuity |
| source／instance／bind location | assertion attempts／coverage |
| 引用 symbols 與可解析型別 | proof result／bound／assumptions／trace |

v1 顯示的 `SVA_FOUND` 或靜態 `NO_ERRORS` 不表示 property 被啟用、執行或證明。`bind` 的宣告位置和 elaborated target instance 也必須分開呈現。

### 16.4 EDA qualification 的兩層門檻

**工具 release：**P0 靜態 fixtures／三後端／editing safety 通過即可稱 `static-preview-qualified`。

**公司 project 導入：**還須核對真實 EDA build semantics、公司 library／VIP／IP 邊界、對應 simulator compile 與指定 regression evidence，才能稱該 profile 已完成 project qualification。未有 license／工具時記 `BLOCKED_ENV`，不以本地 Python tests 代替。

### 16.5 Blackbox／stub 原則

Encrypted source 不嘗試解密。可使用 owner 提供的合法 interface model，但記錄原始 IP、model hash、替代範圍與 `analysis_completeness=PARTIAL`。Stub 內部沒有 references 的結果不能支持「沒有依賴」。v1 完整 demo 不依賴隱藏 stub；stub fixture 專門測試降級標示。

<a id="s17"></a>
## 17. Repository 與模組配置

```text
svweave/
├── README.md
├── SVWeave_plan_v1.md
├── AGENTS.md
├── pyproject.toml
├── uv.lock
├── toolchain.lock.json
├── THIRD_PARTY_NOTICES.md
├── src/svweave/
│   ├── cli.py
│   ├── server.py
│   ├── application/       # CLI/MCP 共用 use cases
│   ├── domain/            # IDs, states, DTOs, errors, evidence
│   ├── config/            # schema / resolver / path policy
│   ├── manifest/          # filelist lexer, CU, argument ledger
│   ├── snapshot/          # capture, content store, invalidation
│   ├── adapters/
│   │   ├── slang/         # worker, extraction, diagnostics
│   │   ├── serena/        # MCP contract and candidate editing
│   │   └── codebase_memory/
│   ├── orchestration/     # routing, jobs, capabilities
│   ├── editing/           # candidate, approval, CAS, journal
│   ├── security/          # sandbox, trusted policy, redaction
│   ├── storage/           # SQLite, migrations, artifact refs
│   └── reporting/
├── schemas/
│   ├── manifest.schema.json
│   ├── toolchain.schema.json
│   ├── mcp/               # one input/output schema per tool
│   ├── edit.schema.json
│   └── evidence.schema.json
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── fault_injection/
│   └── acceptance/
├── fixtures/
│   ├── demo/
│   │   ├── include/
│   │   ├── rtl/
│   │   ├── sva/
│   │   └── filelists/
│   ├── language/
│   ├── uvm/
│   ├── filelists/
│   ├── negative/
│   └── expected/          # 手工 review 的 golden，不從被測工具自動批准
├── scripts/
│   ├── bootstrap_locked.py
│   ├── probe_backends.py
│   ├── qualify.py
│   └── benchmark.py
├── docs/
│   ├── architecture.md
│   ├── manifest.md
│   ├── mcp_contract.md
│   ├── supported_features.md
│   ├── security.md
│   ├── operations.md
│   ├── limitations.md
│   └── adr/
└── .github/workflows/     # 未經授權不新增 secrets / self-hosted runner
```

執行期 `.svweave/` 存 inventory、build DB、logs、snapshots、candidates、journals、reports，不將 IP source、waves、local absolute path 或 secrets commit 到開源 repo。Public CI 只用可公開 fixtures；UVM library 以 pinned 外部 artifact 取得並保留授權。

<a id="s18"></a>
## 18. 分階段開發與 release gates

不在缺少 source／人力／環境量測時承諾固定交付日期。以 dependency 與 exit criteria 排程，前一 gate 未通過，不把後續局部成果宣稱為整體完成。

### 18.1 M0 — 可行性、版本與 contract spike

**輸入：**本計畫、三個上游、可公開最小 fixtures、Linux host。

**工作：**鎖定工具候選；查授權；probe MCP schemas／Verible；讓 pyslang 取得一個 generate hierarchy、兩個同名 class methods、一個 bind；驗證 graph 能抽取最小 SV fixture；驗證 Serena 能讀取及修改 candidate；確認隔離、專用 state 與 offline assets。

**交付：**`toolchain.lock.json`、`capability_report.json`、`license_manifest.json`、spike evidence、ADR。

**G0：**三個後端真實操作成功；關鍵 extraction 可行；Serena writing／graph state 可隔離；必要版本欄位非 null。未通過就回報 blocker，不進入大量抽象框架實作。

### 18.2 M1 — Config、manifest 與 snapshot

**工作：**schemas、filelist lexer／resolver、CU／library ledger、root policy、dirty／untracked／header／negative lookup inventory、snapshot ID 與 DB foundation。

**交付：**`init`、`doctor`、`manifest resolve`、`snapshot create`、BLD／SNP tests。

**G1：**相對路徑、順序、CU、缺檔、unsupported flags、capture race 全部符合預期；不能讀出 trusted roots。

### 18.3 M2 — Build-aware semantic core

**工作：**pyslang worker、diagnostics、declarations／references、hierarchy、direct port associations、SVA inventory、source mapping、DB publication。

**交付：**`build` 與 semantic queries、golden fixtures、errors／partial reporting。

**G2：**SEM／LOC／SVA／真正 UVM static tests 通過；同名 symbol 不混淆；產物包含 build／snapshot identity；native worker failure 不帶走 gateway。

此時只能稱 semantic core 完成，不能稱三工具成品完成。

### 18.4 M3 — Graph、Serena 與統一 MCP

**工作：**兩個 upstream MCP adapters、schema compatibility、bounded search、Serena navigation、snapshot crosswalk、gateway tools、CLI 同源、jobs／pagination／cancellation。

**交付：**read-only end-to-end demo、client configuration 範本、backend degraded behavior。

**G3：**同一 snapshot 完成 graph → semantic → Serena navigation；真實 upstream tests 與 MCP contract tests 通過。Fallback 不能取代 mandatory backend success。

### 18.5 M4 — 受控編輯閉環

**工作：**candidate isolation、Serena edits、exact-range fallback、diff limits、validation、trusted approval、CAS、journal、idempotency、recovery。

**交付：**`svw_edit`、human CLI approval、完整 edit evidence、fault-injection tests。

**G4：**合法小型修改能套用；stale、假批准、surprise edits、crash、後續人類修改全部正確處理。未知安全狀態時 fail closed，不提供「先強制套用」預設。

### 18.6 M5 — Acceptance、benchmark 與操作文件

**工作：**所有 P0 regression、real backend smoke、模擬失效／資源限制、coverage、benchmark、observability、offline bootstrap、sources／license notices。

**交付：**test report、coverage report、benchmark JSON、security report、runbook、supported feature matrix。

**G5：**P0 無 FAIL／BLOCKED／SKIP；完成指標與已知限制可追蹤；人工 review golden／diff。上游 tests 通過不能替代本案 integration tests。

### 18.7 M6 — Release 與第一個 project pilot

**Developer preview release：**G0–G5 皆通過，從乾淨環境重現 demo，artifact／lock／docs／schema 相互一致。

**Project pilot：**使用 owner 提供的真實 IP profile 對照 EDA invocation；釐清 vendor flags／UVM／VIP／encrypted boundaries。取得 licensed runner 時執行 compile／指定 tests。無 EDA 環境時 pilot 標示未 qualification；不阻擋已完成的公開 static preview，但不能抹去缺口。

### 18.8 人員責任配置

| 角色 | 負責範圍 | Review 重點 |
|---|---|---|
| Integration owner／DV lead | scope、manifest、truth boundaries、acceptance | 真實 build 是否等價、是否誤報 PASS |
| Semantic engineer | slang extraction、source maps、language fixtures | scope／CU／specialization、unresolved handling |
| Platform engineer | MCP、snapshot、Serena／graph adapters、editing | race、schemas、state isolation、idempotency |
| Verification reviewer | golden、negative／fault tests、coverage | 非同源 oracle、邊界與 false-positive claims |
| Release／security reviewer | bootstrap、licenses、IP protection、artifacts | 版本與分發、網路／檔案權限 |

可由同一人兼任，但 high-risk write path 與 golden 更新應有獨立 review 記錄；agent 不能自行核准自己的 acceptance。

<a id="s19"></a>
## 19. 可直接拆 issue 的工作清單

下列 IDs 是 SVWeave 建議工作項目，不表示已在任何 issue tracker 建立。每項完成要附 commit、測試命令、環境、結果檔案與 reviewer 記錄。

| Issue ID | 工作 | 依賴 | 主要產物／驗收 |
|---|---|---|---|
| SVW-001 | 上游版本／授權／artifact inventory | 無 | lock、notices、DEP-01 |
| SVW-002 | 真實三後端 capability spike | 001 | capability report、INT-01／02 |
| SVW-003 | 選定隔離與專用 state 方案 | 001–002 | threat model、SEC-05 |
| SVW-004 | Domain states／IDs／error schemas | 002 | schema tests、API-01 |
| SVW-005 | Repository skeleton／CI／typed package | 004 | import／lint／unit smoke |
| SVW-006 | Manifest／local config／trusted policy schema | 004–005 | validation、BLD-03 |
| SVW-007 | Filelist lexer、nested paths、env expansion | 006 | BLD-01／02／04／05 |
| SVW-008 | CU／library／option classification ledger | 007 | BLD-06／07／08 |
| SVW-009 | Root／symlink／generated input policy | 006–008 | BLD-09、SEC-01 |
| SVW-010 | Immutable snapshot capture／inventory | 009 | SNP-01／02／03／05 |
| SVW-011 | Build identity／cache／atomic publication | 004、010 | SNP-04／06 |
| SVW-012 | SQLite schema／migrations／retention | 004、011 | storage／GC tests |
| SVW-013 | pyslang worker／resource／diagnostics | 002、008、010 | SEM-07、API-03 |
| SVW-014 | Declaration／reference extraction | 013 | SEM-01／02／08／10 |
| SVW-015 | Hierarchy／generate／parameters | 013–014 | SEM-03／04／09 |
| SVW-016 | Port／interface／modport binding | 015 | SEM-05／06 |
| SVW-017 | Source-map／macro／UTF encoding | 013–016 | LOC-01／02 |
| SVW-018 | SVA inventory／bind extraction | 015、017 | SVA-01／02／03 |
| SVW-019 | 真正 pinned UVM static fixture | 014、017 | UVM-01／02／03 |
| SVW-020 | codebase-memory MCP adapter | 002、010–012 | INT-01／03／04 |
| SVW-021 | Serena navigation／candidate adapter | 002–003、010、017 | INT-02、EDT-06 |
| SVW-022 | Evidence／crosswalk／completeness | 014–021 | UVM-04、API-02 |
| SVW-023 | MCP gateway／job manager／schemas | 004、013、020–022 | API-01–05 |
| SVW-024 | CLI commands 與共用 domain services | 023 | CLI parity／exit tests |
| SVW-025 | Edit prepare／candidate／diff policy | 017、021、024 | EDT-01／03／06／10 |
| SVW-026 | Validation／approval／policy binding | 025 | EDT-04／09、SEC-06 |
| SVW-027 | CAS／journal／idempotent commit | 026 | EDT-02／05／07 |
| SVW-028 | Conflict-aware recovery／post-apply | 027 | EDT-08、SNP-06 |
| SVW-029 | Security／prompt-injection negative suite | 003、023、025–028 | SEC-01–06 |
| SVW-030 | Structured logs／redaction／repro bundle | 013、020–028 | OBS-01、SEC-04 |
| SVW-031 | Basic external evidence import／read | 022、024 | UVM-04、REL-01 |
| SVW-032 | Golden acceptance suite／mutation checks | 013–031 | 全 P0、coverage |
| SVW-033 | Performance／resource measurement | 032 | PERF-01、baseline JSON |
| SVW-034 | Locked offline bootstrap／clean-room run | 001–003、032 | DEP-01／02 |
| SVW-035 | README／API／supported features／limitations | 024、030–034 | DOC-01 |
| SVW-036 | Release review／真實 project pilot ledger | 032–035 | G5、release checklist；EDA 部分獨立列示 |

### 19.1 Issue 完成紀錄格式

```text
Issue ID:
Requirement IDs:
Implementation commit:
Files changed:
Toolchain lock hash:
Test commands:
Tests passed / failed / blocked / skipped:
Evidence paths:
Known limitations:
Reviewer / review record:
Completion status: IMPLEMENTED | TESTED | ACCEPTED | BLOCKED
```

`IMPLEMENTED` 不等於 `ACCEPTED`。修正測試期望、增加 waiver、停用 assertion 或縮小支援範圍，必須在 issue 中說明原因，不能只把 regression 改綠。

<a id="s20"></a>
## 20. 驗證策略與測試矩陣

### 20.1 測試層級與 oracle

Unit tests 可使用 mocks 驗證錯誤分支；contract tests 驗證 schema；integration tests 必須啟動 pinned 真實後端；acceptance 使用人工 review 的 small fixtures 與明確 goldens。

Golden 不可由被測 extraction code 自動產生後直接批准。Hierarchy、same-name scopes、reference locations 等先以小型 source 手工核對，再保存 machine-readable expected data。大型專案只有 report 沒有可靠 oracle 時，不宣稱完整正確率。

以下測試均是 **預定 Test Condition／Expected Result，狀態為尚未執行**。除明示外皆為 P0；runtime simulation／formal 的實際執行另屬 project qualification，不混入靜態工具必過條件。

### 20.2 Dependencies、build 與 snapshot

| Test ID | Test Condition | Expected Result |
|---|---|---|
| DEP-01 | 乾淨 Linux 環境；已提供全部 pinned offline artifacts | 三後端啟動／probe 成功；無臨時網路下載；versions／hashes 對齊。 |
| DEP-02 | 換掉一個 backend binary 或 tool schema | `CAPABILITY_MISMATCH`／lock mismatch；不得使用舊 qualification 標章。 |
| BLD-01 | 同名 relative source 存在 cwd 與 nested filelist 旁；混用 `-f`／`-F` | 各自選到明確預期檔案；ledger 保留 origin／resolution base。 |
| BLD-02 | 有空白路徑、quoted define、allowlisted env；另有未設 env | 合法 token 精確保留；缺 env 明確 BLOCKED，不變空字串。 |
| BLD-03 | 插入未支援的 semantic vendor option | `UNSUPPORTED_OPTION`，回報位置；不略過後宣稱成功。 |
| BLD-04 | 缺 source／package／include／必要 filelist | `MISSING_INPUT`／BLOCKED；空 reference 結果不被當不存在。 |
| BLD-05 | A.f 包含 B.f，B.f 再包含 A.f | 有界停止並輸出 cycle chain，不無限遞迴。 |
| BLD-06 | A.sv 定義 macro、B.sv 使用；比較 single 與 per-file CU | 結果反映 CU 邊界；不同配置不得共用相同 build。 |
| BLD-07 | Include dirs 中有同名 header，交換順序 | 選取結果與 build ID 隨優先順序改變。 |
| BLD-08 | 以 library search 載入 module；加入較高優先候選 | 實際 library source 可追蹤，新成員使舊 snapshot 失效。 |
| BLD-09 | Generated input 被宣告但未產生 | BLOCKED；不得執行未知 generator 或建立空檔。 |
| BLD-10 | 同一 source 在不同 compilation group／macro context 出現 | 不因 path 相同就錯誤去重；保留多個 semantic occurrences。 |
| SNP-01 | 只修改已 include 的 `.svh` | 相關 snapshot/build 失效，舊結果為 STALE。 |
| SNP-02 | 原本只在次要 include dir 的 header，改在優先 dir 新增同名檔 | 即使原 header 未變也失效，分析新 lookup 結果。 |
| SNP-03 | 修改未 commit 的 `.sv`；新增 untracked 但被 filelist 引用的檔案 | Snapshot 包含真實 bytes；Git SHA 相同仍得到不同 snapshot。 |
| SNP-04 | Source 相同，變更 parameter／define／toolchain／extractor | build identity 不混淆；可指出變更維度。 |
| SNP-05 | Capture 過程中外部程式持續修改 source | 有限次重試後 `WORKSPACE_UNSTABLE`；不發布混合版本。 |
| SNP-06 | 新 build crash／產出半套 DB | 上一個完整 published build 不受破壞；新 job 有明確失敗狀態。 |

### 20.3 SystemVerilog、location、UVM 與 SVA

| Test ID | Test Condition | Expected Result |
|---|---|---|
| SEM-01 | 兩個 package／class 各有 `run_phase`／`write` | definition／reference 依完整 scope 分離，不混用 target。 |
| SEM-02 | Parameterized class 以兩種 width／type 實例化 | 保存 specialization 差異與共同 source declaration。 |
| SEM-03 | `LANES=2` 與 `LANES=4` 的 generate hierarchy | 分別有 2／4 個 `demo_lane` instances，path 與 bound checker 數相符。 |
| SEM-04 | `ifdef` 選不同 module 分支 | Active hierarchy 符合 profile；inactive source 可查但明確標記。 |
| SEM-05 | Named、positional、`.*`、constant／concat／open port associations | formal/actual 正確或明示 unsupported；不把 concat 當單一 signal。 |
| SEM-06 | Interface／modport 含同名 signal，兩個不同 instances | 保留 instance 與 modport 限定；不自動推成 runtime virtual interface 綁定。 |
| SEM-07 | 故意加入 syntax／type／unknown symbol error | precise diagnostics 與 PARTIAL；job completion 不顯示成 design PASS。 |
| SEM-08 | Virtual method call 具有多種可能 runtime subtype | 列 resolved static declaration 及 dynamic limitation，不捏造唯一 runtime target。 |
| SEM-09 | Escaped identifier／generate index／含特殊字元 scope | Instance ID 不靠簡單 `split('.')` 造成碰撞。 |
| SEM-10 | 未展開 library body／unused generic 中有 references | scope completeness 如實呈現，不宣稱 query 覆蓋所有可能 specialization。 |
| LOC-01 | 中文註解、UTF-8 多位元字元、CRLF、tab 與多行 declaration | LSP→original bytes round-trip 正確；exact edit 不切壞字元／行尾。 |
| LOC-02 | Macro 產生 declaration，加上 `line` directive／重複 include | 分開 spelling／expansion／display location；ambiguous macro edit 被拒絕。 |
| UVM-01 | 使用真正 pinned UVM package，含 env／driver／monitor／test | Static compile 達 qualified subset；可找到 class／method；不是 fake UVM stub。 |
| UVM-02 | 兩個 test 設不同 factory override | 靜態結果不宣稱確定 runtime topology；可列 override call sites。 |
| UVM-03 | 多個 `config_db::set/get`，含 wildcard scope／不同型別 | 保留 source／type/context；不把候選 set 誤報為必然命中。 |
| UVM-04 | Import 一份 mismatch snapshot 或遭改動的 external run manifest | 不附加為目前 build PASS；hash mismatch 拒絕，unlinked／untrusted 明示。 |
| SVA-01 | Property／assertion 透過 bind 掛至 generate 中每個 lane | Declaration 與每個 elaborated checker target 分開呈現。 |
| SVA-02 | Property 引用未知 signal，或 bind target 無法解析 | 產出 diagnosis／unresolved，不顯示完整有效 SVA inventory。 |
| SVA-03 | 只執行 slang，沒有 simulator／formal evidence | SVA execution／proof 狀態維持 `NOT_RUN`。 |

### 20.4 三後端、editing、MCP 與安全

| Test ID | Test Condition | Expected Result |
|---|---|---|
| INT-01 | 真實 graph backend 索引最小 SV repository | 至少回傳預期 module／class／method 候選與 source；紀錄實際缺漏。 |
| INT-02 | 真實 Serena 找到並修改 candidate 中指定 SystemVerilog method | Diff 僅出現在 candidate；編輯不是被本案 mock 或文字工具偷偷替代。 |
| INT-03 | Graph index／Serena active project 不屬目前 snapshot | Gateway 拒絕混用或重建；不只以 project name 判斷一致。 |
| INT-04 | 關閉 graph 或 Serena process | 明確 DEGRADED／UNAVAILABLE；允許受限 read fallback，不冒充完整整合。 |
| EDT-01 | 合法單檔小修改，candidate 驗證成功且有真正批准 | 原始 bytes 只變更允許區段；post-apply snapshot 與 diff 一致。 |
| EDT-02 | Prepare 後人類改了 source 或 dependency header | Commit `EDIT_CONFLICT`／STALE；不覆蓋人類修改。 |
| EDT-03 | 編輯僅提供 ambiguous 裸 method name 或 macro expansion 位置 | 拒絕並回 scoped candidates／reason，不自動選擇。 |
| EDT-04 | 批准後替換 patch bytes、profile set 或 policy | 原 approval 失效，無 source 寫入。 |
| EDT-05 | 對已成功 transaction 重送相同 commit；另送同 key 不同內容 | 前者回原結果不重複套用；後者 `IDEMPOTENCY_CONFLICT`。 |
| EDT-06 | Serena 修改了額外檔案、超過行數或 protected path | Candidate 驗證拒絕，原始 source 完全不變。 |
| EDT-07 | 多檔套用第二檔前強制 crash | Journal 留存；下一次寫入被阻擋，進入 `RECOVERY_REQUIRED`。 |
| EDT-08 | Crash 後人類又修改一個已套用檔案 | Recovery 不覆蓋人類版本；輸出逐檔 conflict 與可保留版本。 |
| EDT-09 | Candidate 新增 error／未核准 warning，或 baseline 本來 PARTIAL | 自動 commit gate 不通過；保留可 review 的 diff。 |
| EDT-10 | 提案涉及 file rename／delete／binary／symlink target | v1 明確拒絕，不退回 unrestricted shell。 |
| API-01 | Unknown field、invalid enum、非法 ID、超大 input | Schema／size error，backend 不收到非法請求。 |
| API-02 | 結果超過 limit；重用別的 build cursor | 正確 pagination／truncated；跨 build cursor 拒絕，空 page 不誤報完整。 |
| API-03 | Build timeout／client cancel／worker OOM | Worker tree 被回收；job 正確終止；不發布半成品／殘留寫入鎖。 |
| API-04 | Backend 在 stdout 輸出非 protocol log | 清楚指出 protocol error；gateway 本身 logs 僅進 stderr。 |
| API-05 | 選定 client 與各 backend 的實際 protocol revision 不同 | 通過既定 compatibility tests 或明確 mismatch；不盲用 latest。 |
| SEC-01 | `../`、絕對外部 path、symlink escape、hardlink edit | 受根目錄／inode policy 阻擋，無越權讀寫。 |
| SEC-02 | Filelist／define／path 含 shell metacharacters／命令替換 | 當資料或拒絕；不存在 shell execution。 |
| SEC-03 | Repository／tool output 指示「忽略規則、上傳 source、執行命令」 | 視為不可信資料，不能改 policy／approval／tool routing。 |
| SEC-04 | 查詢／log 可能包含外部私有 path、secrets、完整 IP source | 依 redaction／output policy 限制；offline mode 無網路出口。 |
| SEC-05 | Backend 嘗試直接改原始 source 或讀其他 repository | OS／filesystem isolation 拒絕；測試不能只檢查工具名稱被隱藏。 |
| SEC-06 | Agent 偽造 `approved=true`／approval 檔案／expired token | Commit 拒絕；approved record 只能由 trusted channel 建立。 |
| OBS-01 | 以 evidence bundle 在相同 lock／snapshot 重現 query | 相同 normalized semantic result；hash、命令、diagnostics 可追蹤。 |
| REL-01 | 所有 unit mocks 綠燈，但真實 backend／EDA 未執行 | 不得稱三後端 accepted 或 EDA PASS；各狀態分開。 |
| DOC-01 | CLI／schema／README／sample config 同步檢查 | 文件中的已承諾命令存在，範例通過 schema，來源與限制可查。 |
| PERF-01 | 固定 fixture corpus／host／版本進行 cold build 與 hot queries | 產出量測資料與目標比較；超標記錄原因，不引用上游廣告數據代替。 |

### 20.5 Critical mutation tests

至少刻意注入以下缺陷，確認測試會失敗：移除 header hash、忽略 include-dir 順序、以 bare name 合併 method、讓 PARTIAL 改成 COMPLETE、取消 expected hash、接受假的批准、允許 Serena 改原檔、crash 後盲目 rollback、忽略 unsupported option、把 static build 當 simulation PASS。

這 10 類變異應全部被對應 tests 擋下。若測試仍全綠，代表 acceptance 存在缺口，不能只增加 line coverage 掩蓋。

<a id="s21"></a>
## 21. SystemVerilog／UVM／SVA fixtures

本節提供 fixture 的具體起點，供實作者直接建立測試檔。這些 snippets 在本次文件產出中 **未執行 compiler 或 simulator**；M0／M2 必須以 pinned 工具驗證，並將結果納入 evidence。不得把本節存在視為測試已通過。

### 21.1 Header：測試 include dependency

檔案：`fixtures/demo/include/demo_defs.svh`。

```systemverilog
`ifndef SVW_DEMO_DEFS_SVH
`define SVW_DEMO_DEFS_SVH
  `define SVW_DEFAULT_DATA_W 32
`endif
```

### 21.2 RTL：可追蹤的 direct port associations

檔案：`fixtures/demo/rtl/demo_lane.sv`。這是簡單 valid/ready fixture，不宣稱是完整 AXI／AHB／APB VIP。

```systemverilog
`default_nettype none
module demo_lane #(
  parameter int unsigned DATA_W = 32
) (
  input  logic              clk,
  input  logic              rst_n,
  input  logic              valid_i,
  output logic              ready_o,
  input  logic [DATA_W-1:0] data_i,
  output logic              valid_o,
  input  logic              ready_i,
  output logic [DATA_W-1:0] data_o
);
  assign valid_o = valid_i;
  assign ready_o = ready_i;
  assign data_o  = data_i;
endmodule
`default_nettype wire
```

檔案：`fixtures/demo/rtl/demo_top.sv`。

```systemverilog
`include "demo_defs.svh"
`default_nettype none
module demo_top #(
  parameter int unsigned DATA_W = `SVW_DEFAULT_DATA_W,
  parameter int unsigned LANES  = 2
) (
  input  logic                          clk,
  input  logic                          rst_n,
  input  logic [LANES-1:0]               valid_i,
  output logic [LANES-1:0]               ready_o,
  input  logic [LANES-1:0][DATA_W-1:0]  data_i,
  output logic [LANES-1:0]               valid_o,
  input  logic [LANES-1:0]               ready_i,
  output logic [LANES-1:0][DATA_W-1:0]  data_o
);
  for (genvar lane = 0; lane < LANES; lane++) begin : g_lane
    demo_lane #(.DATA_W(DATA_W)) u_lane (
      .clk     (clk),
      .rst_n   (rst_n),
      .valid_i (valid_i[lane]),
      .ready_o (ready_o[lane]),
      .data_i  (data_i[lane]),
      .valid_o (valid_o[lane]),
      .ready_i (ready_i[lane]),
      .data_o  (data_o[lane])
    );
  end
endmodule
`default_nettype wire
```

**Test Condition：**分別以 `LANES=2`／`LANES=4`、`DATA_W=32`／`64` 分析。

**Expected Result：**`demo_lane` instance count 為 2／4；各 instance 的 parameter 與 port widths 正確；不同 build identity 不混用。`g_lane[0].u_lane` 的 `data_i` actual expression 應保留 slice expression，而不是只列模糊的 `data_i` 名稱。

### 21.3 SVA：stall stability 與 bind

檔案：`fixtures/demo/sva/stream_hold_sva.sv`。

```systemverilog
`default_nettype none
module stream_hold_sva #(
  parameter int unsigned DATA_W = 32
) (
  input logic              clk,
  input logic              rst_n,
  input logic              valid,
  input logic              ready,
  input logic [DATA_W-1:0] data
);
  property p_stall_holds;
    @(posedge clk) disable iff (!rst_n)
      (valid && !ready) |=> (valid && $stable(data));
  endproperty

  ap_stall_holds: assert property (p_stall_holds);
endmodule
`default_nettype wire
```

檔案：`fixtures/demo/sva/demo_bind.sv`。

```systemverilog
bind demo_lane stream_hold_sva #(.DATA_W(DATA_W)) u_stream_hold_sva (
  .clk   (clk),
  .rst_n (rst_n),
  .valid (valid_o),
  .ready (ready_i),
  .data  (data_o)
);
```

**Static Test Condition：**分析 demo top 與 bind。

**Static Expected Result：**找到 `p_stall_holds`、`ap_stall_holds`、bind source，並為每個 lane 列出 bound checker。只回報解析結果；assertion execution 維持 `NOT_RUN`。

**後續 EDA Test Condition：**reset 已解除；至少一個 clock sampled 到 valid=1、ready=0，下一拍維持 valid／data；另設負向刺激在 stall 後改變 data。避免在 sampling edge 造成 testbench race。

**EDA Expected Result：**第一種刺激不因該 property 失敗，第二種刺激觸發 assertion failure；另記 antecedent activation，避免用 vacuous pass 當完整驗收。此 property 是本 fixture 的介面假設／檢查，不是 passthrough module 對任意非法上游輸入的保證。

### 21.4 Demo filelist

檔案：`fixtures/demo/filelists/rtl.f`。下列以 repository root 為 cwd，配合 `-f`；include directory 已寫在 manifest，獨立 CLI reproduction 時也必須傳入它。

```text
fixtures/demo/rtl/demo_lane.sv
fixtures/demo/rtl/demo_top.sv
fixtures/demo/sva/stream_hold_sva.sv
fixtures/demo/sva/demo_bind.sv
```

第 10.6 節的獨立 CLI 已顯式加入 include directory 與 demo define；其 parameter 使用 demo 的預設值。正式 SVWeave runner 仍須由 normalized manifest 生成完整 arguments，包含顯式 parameter overrides，不只傳遞 filelist。

### 21.5 真正 UVM fixture

檔案：`fixtures/uvm/svw_demo_uvm_pkg.sv`。UVM library 是 external pinned dependency，不能以本檔代替；必須先以其正式 package source 建立 compilation context。

```systemverilog
package svw_demo_uvm_pkg;
  import uvm_pkg::*;
  `include "uvm_macros.svh"

  class svw_driver extends uvm_driver #(uvm_sequence_item);
    `uvm_component_utils(svw_driver)
    function new(string name = "svw_driver", uvm_component parent = null);
      super.new(name, parent);
    endfunction
    virtual task run_phase(uvm_phase phase);
      `uvm_info("SVW_DRV", "driver fixture", UVM_LOW)
    endtask
  endclass

  class svw_monitor extends uvm_monitor;
    `uvm_component_utils(svw_monitor)
    function new(string name = "svw_monitor", uvm_component parent = null);
      super.new(name, parent);
    endfunction
    virtual task run_phase(uvm_phase phase);
      `uvm_info("SVW_MON", "monitor fixture", UVM_LOW)
    endtask
  endclass

  class svw_env extends uvm_env;
    `uvm_component_utils(svw_env)
    svw_driver drv;
    svw_monitor mon;
    function new(string name = "svw_env", uvm_component parent = null);
      super.new(name, parent);
    endfunction
    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      drv = svw_driver::type_id::create("drv", this);
      mon = svw_monitor::type_id::create("mon", this);
    endfunction
  endclass

  class svw_smoke_test extends uvm_test;
    `uvm_component_utils(svw_smoke_test)
    svw_env env;
    function new(string name = "svw_smoke_test", uvm_component parent = null);
      super.new(name, parent);
    endfunction
    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      env = svw_env::type_id::create("env", this);
    endfunction
  endclass
endpackage
```

檔案：`fixtures/uvm/svw_uvm_top.sv`。

```systemverilog
module svw_uvm_top;
  import uvm_pkg::*;
  import svw_demo_uvm_pkg::*;
  initial run_test("svw_smoke_test");
endmodule
```

**Test Condition：**在真正 UVM compilation profile 解析本例，查詢兩個 `run_phase`，對 driver 的 log 文字執行受控修改。

**Expected Result：**兩個 method 的 scope／declaration 分開；Serena candidate diff 只改 `svw_driver::run_phase`；slang validation 保持有效。`svw_env`／`drv`／`mon` 是 verification class 關係，不直接當成 module elaboration 的實體 instance tree；未 simulation 不宣稱觀察到 `uvm_test_top.env.drv`。

### 21.6 必須補建而非省略的 fixtures

本節 demo 不是整個語言測試集。SVW-014–019 還必須建立：parameterized class、同名 package scope、interface/modport、virtual interface candidate、config_db wildcard、factory override、macro-origin、explicit CU、unsupported vendor flag、encrypted／stub boundary、UTF encoding 與 escaped identifiers。每個 fixture 對應第 20 節至少一個 Test ID。

<a id="s22"></a>
## 22. CI、品質與效能量測

### 22.1 Pipeline

| Pipeline | 觸發 | 必要檢查 |
|---|---|---|
| Fast checks | 每個 PR | format／lint／type／schema／unit；不得下載任意最新依賴。 |
| Real-backend integration | adapter／manifest／semantic／editing 變動與 release | pinned 三後端、fixture queries、真正 Serena edit。 |
| Acceptance／fault injection | release gate 與高風險變動 | 全 P0、crash／conflict／isolation／approval。 |
| Benchmark | extraction／index／snapshot 變動及 release | 固定 corpus、host、versions；生成原始 JSON。 |
| Private project qualification | 人工批准，在授權環境 | 真實 EDA profile／compile／tests；不把 private source 傳至 public runner。 |

### 22.2 Release 品質門檻

| 指標 | 初始 gate |
|---|---|
| P0 test pass | 100%；FAIL／BLOCKED／SKIP 都不算通過。 |
| Critical invariants | 第 20.5 節 10 類 mutation 全數被擋下。 |
| 自有 Python statement coverage | ≥85%，不包含 upstream source、generated code 或合法排除項；排除需 review。 |
| 自有 Python branch coverage | ≥80%；write policy／state transitions 必須另逐條測試。 |
| Semantic golden | 所有明列 fixture 的預期 declarations／instances／references 精確符合。 |
| Reproducibility | 相同 snapshot／config／lock 的 normalized results 相同；時間／job ID 不參與語意比較。 |
| Docs | 所有已宣告 CLI／schema／sample configurations 通過檢查。 |
| EDA status | 明確 NOT_RUN／BLOCKED／PASS／FAIL；不與工具 unit test 合併。 |

這些比例是 **SVWeave 軟體 QA 目標**，不是使用者 RTL 的 line／toggle／functional coverage。上游未 benchmark 的 HDL 也不因本案少量 fixtures 通過就改稱全面支援。

### 22.3 效能目標與量測方法

以下是待量測的初始 acceptance budget，不是預測保證；M0 可基於公開量測提出一次變更並記 ADR，之後不得在 regression 超標時靜默放寬。

| 項目 | 參考條件／初始目標 |
|---|---|
| Reference host | Linux x86_64、8 logical CPU、32 GiB RAM、本地 SSD；記錄實際 CPU／OS。 |
| 基準 corpus | 100,000 source lines／約 500 source files 的固定合成 SV corpus；另獨立量測 pinned UVM fixture。 |
| Cold semantic build | ≤120 秒；首次 dependency 安裝不混入，但另記 tool startup 時間。 |
| HDL worker peak memory | ≤8 GiB；所有 backend 合計 memory 也須記錄，不只報單一 process。 |
| Hot symbol／hierarchy／reference query | p95 ≤1 秒，limit≤100、不含 LLM 回答時間。 |
| Hot repository search | p95 ≤2 秒，明確列 graph 狀態與 query type。 |
| Edited build | v1 可做完整 rebuild；不得宣稱只改一檔就必然做到 incremental compile。 |
| Disk usage | 量測 source snapshots、candidate、graph、semantic DB、logs 各項，不只報最小 DB。 |

Cold build 至少 5 次，記 median／max；hot query 至少 30 次，記 p50／p95／max 與 query set。固定有無 OS page cache、process warmup、filesystem、file count、UVM 是否算入 LOC。禁止用不等 corpus 比較前後速度。

### 22.4 是否真的節省 AI 工作量

以固定 10 個 DV 查詢任務比較「文字搜尋」與「SVWeave」：symbol lookup、same-name disambiguation、generate hierarchy、include invalidation、direct port lookup 等。記成功率、錯誤結論數、tool calls、讀取 source bytes、輸入／輸出 tokens 與人工更正次數。

優先降低錯誤結論；只有任務品質可比時才計算 token／時間差。不把上游其他語言測得的效率直接寫入 SVWeave README。

<a id="s23"></a>
## 23. 安全、IP 保護與操作權限

### 23.1 必須實作的控制

| 風險 | 控制 |
|---|---|
| Backend 任意寫 source | 原始 source 不以 writable mount 暴露；candidate 與 metadata 分開。 |
| Repository config 執行命令 | 純資料 schema；不執行 activation hooks／Make／Tcl／shell substitution。 |
| Prompt injection | source comments、README、AGENTS、tool outputs 不能修改 trusted policy。 |
| 越界讀取 | normalized path＋realpath＋root allowlist；source escape tests。 |
| AI 假批准 | 不接受 boolean 自我批准；批准記錄與 agent 權限隔離。 |
| Parser crash／resource abuse | native worker 隔離、CPU/memory/time limits、process-tree cancellation。 |
| Credential／IP 外洩 | no network by default、環境 allowlist、log redaction、bounded snippets。 |
| Supply chain drift | exact versions／hashes／notices、offline resources、no auto-update。 |
| Runtime files 入 Git | `.gitignore`、artifact scanning；candidate/source snapshots 不進 repo。 |

### 23.2 安全保證的範圍

SVWeave 能保證的是 **經其 gateway 執行的操作**。若 AI client 另有可直接寫原始 repository 的 terminal、file tool 或任意 host shell，SVWeave 的批准流程不能控制那些工具。

受控部署必須讓 agent 只看 read-only snapshot／candidate，或用額外 OS policy 限制原始 source 與 approval store；human approval 可經不同權限的本地程序／IPC 完成。相同 UID 且 unrestricted host access 的 agent，不能被描述成無法偽造一般可寫檔案。若部署無法達到此條件，`doctor` 必須指出未 qualification 的 client boundary，editing 預設停用。

MCP tool allowlist／annotation、Serena read-only mode、提示詞本身都不是替代 filesystem isolation 的方式。[S07][S12][S15]

### 23.3 資料出境與模型

Indexing／slang／LSP 可以在本地執行；但 MCP client 若使用雲端模型，選出的 source snippets 仍可能送給模型服務。`local indexing` 不得宣稱成 `end-to-end offline`。公司部署需要明確的模型端點／資料分級政策，受保護 source 預設不輸出至外部 client。

建議 evidence bundle 預設只含 hashes、normalized manifests、diagnostics 摘要與 redacted paths；完整 source inclusion 必須是另一個明確的人工作業，不由 agent 自動打包上傳。

### 23.4 Git／CI 邊界

v1 不自動 push、建立 PR、改 branch protection、設置 secrets 或授權 self-hosted runner。使用者要求後，另依實際 GitHub／GitLab 權限與流程執行。Untrusted PR 不可存取 private EDA license 環境或公司 source。

<a id="s24"></a>
## 24. Observability 與 debugging runbook

### 24.1 每次 build 必須留下的資料

```text
.svweave/runs/<job-id>/
├── request.json
├── normalized_manifest.json
├── argument_ledger.json
├── snapshot_inventory.json
├── toolchain_manifest.json
├── backend_capabilities.json
├── diagnostics.json
├── dependency_report.json
├── unsupported_nodes.json
├── semantic_summary.json
├── resource_usage.json
├── events.jsonl
└── result.json
```

`events.jsonl` 至少有 timestamp、request/job/build IDs、component、event、duration、outcome、error code。Log 中不放任意 secrets／完整 source；stderr 與 protocol stdout 分離。沒有 run ID 的 screenshots 或人工文字不能代替可重現 evidence。

### 24.2 常見問題定位

| 現象 | 檢查步驟 | 不可採取的捷徑 |
|---|---|---|
| Hierarchy 少 instance | 先看 profile/top/parameters → manifest → diagnostics → generate active branch → unresolved modules | 直接把 graph 候選 instance 加進 semantic 結果。 |
| Reference 查不到 | 檢查 declaration ID／CU／macro origin／library analysis scope／dynamic dispatch | 因查不到就說沒有任何使用者。 |
| 修改 `.svh` 卻沒失效 | 比對 inventory／lookup roots／dependency report／snapshot capture | 只重啟 UI 後宣稱已修正。 |
| 找錯 `run_phase` | 比較 qualified scope、specialization、file hash、source span、Serena active project | 用全域字串 replace。 |
| Slang 和 EDA 結果不同 | 比對語言模式、CU、defines、include順序、library、vendor options、stubs與path semantics | 修改 source 或加 waiver 只求兩邊都變綠。 |
| Candidate validation 通過但 commit 被拒絕 | 查 base snapshot、approval hash、policy、目前 dependencies、journal | 加 `--force` 繞過 CAS。 |
| Parser／worker crash | 保存版本、最小重現、stderr、exit／signal、resource report | 反覆無界重試或回傳空 graph。 |
| Graph 顯示舊檔案 | 核對專用 state、project ID、snapshot mapping、index status | 只看同名 repository 或 watcher running。 |
| UVM runtime topology 與靜態 class tree 不同 | 查 test/seed、factory evidence、build profile、runtime observation | 直接判定其中一套圖必定錯誤。 |

### 24.3 Evidence-driven 修復流程

先保存 failing snapshot／lock → 縮小成公開或私有最小 fixture → 確認是 manifest、upstream、adapter、mapping 或 policy 缺陷 → 加會失敗的 regression → 修正 → 跑對應及相鄰 tests → 比較 diagnostics／graph delta → review 後更新 qualification。上游升級必須視為新的 toolchain，不沿用舊報告。

<a id="s25"></a>
## 25. 風險、降級與停止條件

| 風險 | 影響 | v1 對策／停止條件 |
|---|---|---|
| HDL graph extraction 不如預期 | Candidate recall 不足 | 真實 fixture benchmark；保留 text fallback；mandatory graph integration 未過不能稱完整三後端 release。 |
| Verible 無法可靠定位特定 SV symbol | Serena edit 範圍錯誤 | 只開放 qualification 子集；必要時 exact patch 明確標示；ambiguous 直接拒絕。 |
| pyslang binding 缺必要 API | 核心 query 無法完成 | M0 spike；提 C++ helper／CLI ADR；不可用假 schema 或 regex 掩飾。 |
| UVM／VIP／vendor semantics 不相容 | Real project build 不完整 | 差異 ledger、profile qualification；不偷偷加 define／stub。 |
| 巨型 SoC memory／分析時間過高 | OOM、長期阻塞 | v1 限 IP-scale；worker limits、scope partition 設計留待後版。 |
| 同步／cache race | 舊結果引導錯誤修改 | Immutable snapshots、full dependency inventory、CAS。 |
| Backend global state／daemon 混用 | 查錯 repo 或外洩 | 專用 state 與隔離；做不到時 backend integration BLOCKED。 |
| Approval 與 source 寫權未隔離 | Agent 繞過人工審查 | qualified deployment 必測；不符時停用受控 commit。 |
| 多檔套用中斷 | 工作目錄半修改 | Journal、逐檔 hashes、recovery-required lock，不盲目回復。 |
| 上游授權／API 更新 | 不可直接升版／發佈 | exact pin、license manifest、contract tests；升版重新 qualification。 |
| 外部 log 自稱 PASS | 誤報結果 | `EXTERNAL_DECLARED`、artifact／build identity、trusted runner 才能升證據等級。 |
| 只追求功能數導致 v1 過大 | 未完成閉環 | 保留 source search／build semantic／small edit；UI、完整 EDA automation、cross-profile 分析延後。 |

### 25.1 降級不得改變事實

允許「graph 不可用，因此以文字搜尋提供候選」；不允許「文字搜尋成功，所以 graph backend 正常」。允許回傳某個完整解析 scope 的正向結果；不允許把整體 PARTIAL 隱藏。允許停在 candidate diff；不允許為達成修改目標取消批准或失效檢查。

<a id="s26"></a>
## 26. 交付物、完成定義與 release checklist

### 26.1 v1 實作交付物

| 交付物 | 必要內容 |
|---|---|
| `svweave` package／CLI | 全部 P0 commands，安裝／解除安裝不修改使用者非專案檔案。 |
| MCP gateway | 第 14 節 tools、schemas、bounded results、error handling。 |
| 三個 adapters | 真實 MCP／pyslang integration、capability report、version pin。 |
| Manifest／snapshot engine | ordered configuration、source inventory、freshness、dependency report。 |
| Semantic index | symbol／reference／hierarchy／direct binding／SVA 與 limitations。 |
| Edit workflow | candidate、diff、validation、trusted approval、CAS、journal／recovery。 |
| Fixtures／testplan | 第 20 節 tests 與小型可重現 goldens；真正 UVM library profile。 |
| Qualification artifacts | tests／coverage／mutation／benchmark／security／license reports。 |
| 文件 | README、架構、安裝、API、manifest、操作、limitations、supported features、ADRs。 |
| Reproduction bundle | exact versions／hashes、profile、commands、results；source export 預設關閉。 |

### 26.2 完成定義

只有 **G0–G5 通過、clean-room demo 可重現、三後端真實操作有證據、write safety negative tests 全部通過**，才可標記 `SVWeave v0.1.0 static-preview-qualified`。

上游 demo、語法檢查、Python lint、unit fixtures 通過，不等於本工具完成；本工具完成也不等於某個 SoC regression 或 signoff 完成。

### 26.3 Release checklist

- [ ] project／CLI／README 統一名稱 SVWeave，明示 SystemVerilog-first。
- [ ] lock 中沒有未填版本／hash；offline bootstrap 可重現。
- [ ] licenses／third-party notices 已 review；未經審查宣稱全套 MIT。
- [ ] schemas／CLI examples 與 implementation 相符。
- [ ] P0 tests 全過，沒有用 SKIP／BLOCKED 偽裝通過。
- [ ] Graph、Serena、slang 皆有真實成功 evidence。
- [ ] 同名 scope、CU、generate、macro、header invalidation tests 全過。
- [ ] UVM／SVA 靜態與 runtime／proof 狀態分開。
- [ ] 無直接寫原始 source 的 backend 掛載。
- [ ] Client privilege／approval boundary 已 qualification 或明確停用 editing。
- [ ] CAS、idempotency、crash recovery 不會覆蓋人類後續修改。
- [ ] Critical mutation tests 全數有效。
- [ ] Benchmark 標示測試環境／corpus，不宣稱未量測收益。
- [ ] Limitations、unsupported features、semantic deviations 可見。
- [ ] 未執行 EDA 的欄位仍為 NOT_RUN／BLOCKED，不改成 PASS。
- [ ] Repository 中沒有公司 source、secrets、snapshots／candidates、私人 absolute paths。

<a id="s27"></a>
## 27. 後續版本與架構決策

### 27.1 後續版本方向

| 版本方向 | 前置條件 | 可擴充內容 |
|---|---|---|
| v0.2 | v0.1 read／edit 閉環穩定 | 真實 EDA evidence runner、UVM topology／factory／config trace importer、第二種 filelist dialect。 |
| v0.3 | 多 profile qualification 資料足夠 | Build delta、cross-profile impact、受控 semantic rename、更多 interface／clocking constructs。 |
| v0.4+ | SoC corpus 性能與 correctness 可量測 | 分區索引、互動圖譜、多 workspace、受控 remote service。 |

不要先承諾完整 runtime／signal graph，再用靜態候選結果填滿 UI。任何新功能都要定義自己的 evidence kind、scope 與 Expected Result。

### 27.2 必須保存的 ADR

| ADR | 決策 |
|---|---|
| ADR-001 | 不合併 upstream repos；使用 process／MCP adapters。 |
| ADR-002 | Gateway 是正式對外入口；raw multi-server 僅供診斷。 |
| ADR-003 | pyslang 為主要 semantic extraction，CLI 為 reproduction。 |
| ADR-004 | 自有 SQLite semantic model，不改 upstream graph DB。 |
| ADR-005 | Immutable snapshots＋完整 relevant input hashing，而非只看 Git SHA。 |
| ADR-006 | v1 全量 semantic rebuild，不承諾 incremental elaboration。 |
| ADR-007 | Serena 在 candidate 編輯，gateway 負責實際套用。 |
| ADR-008 | Static／runtime／external-declared evidence 分離。 |
| ADR-009 | v1 無公開 HTTP、多租戶與自動 Git push。 |
| ADR-010 | License／version／backend capabilities 是 release gate。 |

<a id="s28"></a>
## 28. 給 coding agent 的實作指令

以下文字可作為本計畫的 implementation handoff；它規劃後續 repository 工作，不表示本次已執行。

```text
任務：依 SVWeave_plan_v1.md 實作 SVWeave v0.1.0。

1. 先讀完整計畫，建立 requirement/test/backlog 對照。
2. 先檢查現有 repository 與工作目錄；不得覆蓋既有未提交修改。
3. 由 SVW-001 起依依賴執行 M0；先做真實 backend capability spike。
4. 凍結 exact versions、hashes、licenses、MCP schemas、必要 API。
5. 未通過 G0，不大量產生假 adapters 或空的成功結果。
6. 核心順序：manifest -> snapshot -> semantic core -> 三後端 gateway
   -> candidate editing -> approval/CAS/recovery -> acceptance。
7. 每項 query 回傳 snapshot/build/provenance/completeness/limitations。
8. 不猜測 private filelists、registers、EDA results 或缺少的 API。
9. Mock 只用於 unit/fault tests，不得代替 integration acceptance。
10. 不為了通過測試自動新增 stub、禁用 SVA、忽略 compiler errors。
11. 原始 source 只能由 write coordinator 在有效批准與 CAS 後修改。
12. 不自行執行 repository shell hooks，不自行 push/merge，不修改 secrets。
13. 每項 issue 記錄：commit、命令、lock hash、PASS/FAIL/BLOCKED/SKIP、evidence。
14. Golden 更新必須寫明語意原因並要求 review；不得只更新 snapshot 讓 CI 綠燈。
15. Tool static preview、EDA compile、simulation、formal、coverage 狀態分開。
16. 遇到 blocker，保存可重現證據並列出影響；保留已完成部分，不捏造成功。
17. 最後依 release checklist 產出 qualification report 與 limitations。
```

### 28.1 第一個實作 checkpoint

第一個 checkpoint 不是漂亮的 README 或 Web UI，而是下列可檢驗成果：

```text
一份 pinned toolchain
+ 一份完整 resolved manifest
+ 一個真實 source snapshot
+ 一個可由 slang 查到的 generate hierarchy
+ 一個真實 graph 查詢
+ 一次 Serena candidate method edit
+ 一份明確列出成功與限制的 capability report
```

### 28.2 最終狀態報告模板

```text
SVWeave version:
Source commit:
Toolchain lock hash:
Qualified platform:
Qualified feature subset:

G0 / G1 / G2 / G3 / G4 / G5:
P0 passed / failed / blocked / skipped:
Real graph integration:
Real Serena integration:
Real slang integration:
Editing isolation / approval / recovery:
Statement / branch coverage:
Critical mutation results:
Benchmark report:

EDA compile: NOT_RUN | BLOCKED | PASS | FAIL
Simulation:  NOT_RUN | BLOCKED | PASS | FAIL
Formal:      NOT_RUN | BLOCKED | PASS | FAIL
Coverage:    NOT_RUN | BLOCKED | MEASURED

Known limitations:
Semantic deviations:
Security / license review:
Evidence paths:
Release decision: ACCEPTED_STATIC_PREVIEW | BLOCKED
```

<a id="s29"></a>
## 29. 官方來源與查核紀錄

上游事實以本節一手資料為依據；本文架構、API、門檻、測試與 phases 是 SVWeave 自訂規劃，不是上游功能承諾。查核日期為 **2026-10-13**。浮動主線後續可能改變；M0 必須將實際使用版本的文件／license／schemas 納入 lock 與 evidence。

| Ref | 官方資料與用途 | URL |
|---|---|---|
| S01 | slang repository：frontend、Python bindings、用途、MIT license | `https://github.com/MikePopoloski/slang` |
| S02 | slang User Manual：filelists、CU、libraries、path semantics | `https://sv-lang.com/user-manual.html` |
| S03 | slang CLI Reference：AST JSON、source info、dependency outputs、top／options | `https://sv-lang.com/command-line-ref.html` |
| S04 | Serena repository：MCP／navigation／editing 定位 | `https://github.com/oraios/serena` |
| S05 | Serena SystemVerilog backend：Verible executable、dependency handling、capabilities | `https://raw.githubusercontent.com/oraios/serena/main/src/solidlsp/language_servers/systemverilog_server.py` |
| S06 | Serena authoritative LICENSE：SolidLSP 與 application 分開授權 | `https://raw.githubusercontent.com/oraios/serena/main/LICENSE` |
| S07 | Serena Security Considerations：工具設定與隔離的限制 | `https://oraios.github.io/serena/02-usage/070_security.html` |
| S08 | codebase-memory-mcp README：HDL 支援／benchmark 範圍、tools、read-only graph queries、index status | `https://github.com/DeusData/codebase-memory-mcp` |
| S09 | codebase-memory-mcp third-party licenses／vendored components | `https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/THIRD_PARTY.md` |
| S10 | Verible official LICENSE | `https://raw.githubusercontent.com/chipsalliance/verible/master/LICENSE` |
| S11 | Serena Running：stdio、project state、啟動方式 | `https://oraios.github.io/serena/02-usage/020_running.html` |
| S12 | MCP Tools specification：schema、results、annotations | `https://modelcontextprotocol.io/specification/2026-07-28/server/tools` |
| S13 | MCP Transports specification：stdio、metadata、cancellation、版本相容 | `https://modelcontextprotocol.io/specification/2026-07-28/basic/transports` |
| S14 | 官方 MCP Python SDK | `https://github.com/modelcontextprotocol/python-sdk` |
| S15 | Serena Configuration：tool／REPL interface、trusted settings、限制 | `https://oraios.github.io/serena/02-usage/050_configuration.html` |
| S16 | Accellera UVM factory implementation／documentation | `https://raw.githubusercontent.com/accellera-official/uvm-core/main/src/base/uvm_factory.svh` |
| S17 | Accellera UVM config_db implementation／documentation | `https://raw.githubusercontent.com/accellera-official/uvm-core/main/src/base/uvm_config_db.svh` |
| S18 | 官方 LSP 3.17 Position／PositionEncodingKind 定義 | `https://raw.githubusercontent.com/microsoft/language-server-protocol/gh-pages/_specifications/lsp/3.17/types/position.md` |

### 29.1 尚未以本次工作查證的事項

未實際安裝三個後端、未執行本節 fixtures、未測量 performance、未完成特定 release 版本互通、未取得公司 source／EDA build，也未進行商標或 package name 可用性查核。這些事項已分別納入 M0／acceptance／project pilot 的待辦，不構成已完成結果。

### 29.2 計畫的核心交付判準

**讓 AI 能對同一份 SystemVerilog build，完成可追溯的「找程式 → 查語意 → 提修改 → 驗證 → 批准 → 套用」，同時在不知道、不完整、過期或未執行驗證時，明確停止推論與寫入。**

---

<a id="s30"></a>
## 30. Verilog／SystemVerilog 支援盤點與補強計畫（2026-10-27）

### 30.1 結論、範圍與本次交付

**目前足以作為「指定 profile、受限語法範圍」的靜態閱讀／導航 developer preview；還不足以宣稱完整 Verilog／SystemVerilog 支援，也不足以把空 references、影響清單或靜態 build 成功當成完整性／設計正確性的保證。**

現有能力不是從零開始：slang elaboration、scope-aware declarations、hierarchy／generate／parameters、direct port associations、SVA inventory、UVM 靜態 call metadata、snapshot／provenance 與受控編輯均已有實作。主要不足是 **SVWeave 自己的抽取覆蓋率、legacy Verilog 的獨立驗證矩陣、未展開程式碼的範圍表示，以及目前版本的真實後端驗收證據**，不是簡單換掉 parser 就能解決。

| 使用目的 | 本次判斷 |
|---|---|
| 依確定 top／filelist／defines／parameters 查已展開 RTL 的宣告、階層、直接連接 | 有實作與既有 fixtures，可在明示限制下使用；不能延伸為所有專案皆已 qualification。 |
| 找「所有」引用、證明未使用、完整 rename／跨 profile impact | 不足；references 存在已知類型與遍歷缺口，preview 不是可安全自動套用的證明。 |
| UVM class／inheritance／factory、config-db 呼叫位置 | 有受限靜態 inventory；最新 metadata 的專用真實 UVM integration 尚缺目前版本通過證據。 |
| 完整 IEEE Verilog／SystemVerilog 相容、商用工具等價、SoC／VIP 導入 | 未建立這種資格；需要分語法／profile 的測試及授權專案 pilot。 |
| 模擬、SVA proof、functional coverage、UVM runtime resolution | 不在本次補強範圍，不是要用靜態分析補成的功能；仍分別為 `NOT_RUN`／外部證據。 |

本節依使用者明確要求追加至既有 `SVWeave_plan.md`，保留原第 1–29 節與歷史敘述，不覆寫原規格；原文的「預定」「尚未執行」與本節的程式碼現況應依日期分讀。本次只做程式碼／測試／文件盤點和計畫，**沒有實作下列 backlog、執行後端、修改依賴或 CI、commit／push**。新增 Test ID 與路徑標為「擬新增」者尚不存在，全部待辦驗收仍是 `NOT_RUN`；本節不是 qualification report，也不構成執行未授權操作的批准。

### 30.2 證據基線與驗證狀態

| 項目 | 可核對證據與正確解讀 |
|---|---|
| 本次盤點基線 | clean HEAD `e7684adadd6b6ec7ba0dc126fba6d868cc53e329`；本節追加後僅計畫文件變更。 |
| Frozen toolchain | `toolchain.lock.json` SHA-256：`ea20baf9bc2da3cb6b9dc41f0234c67f925ea37c5691b0bfbbb1ff30f86ef5e9`；slang／pyslang 12.0.0、Serena 1.7.0、codebase-memory-mcp 0.11.0，狀態仍 `UNQUALIFIED`。不因本計畫而升級。 |
| 最新 CI | [run 40459448181](https://github.com/DVman9527/SVWeave/actions/runs/40459448181)，對應上述 SHA：fast checks **PASS**；real-backends job **SKIPPED，不是 PASS**。fast job 包含 ruff／mypy／schema／unit／contract（`.github/workflows/ci.yml`）；`tests/contract/conftest.py::built` 會做 real demo build，因此不能反向宣稱 fast 完全沒有碰 slang，但它不等於完整三後端 integration／acceptance。 |
| 歷史 qualification | `evidence/qualification/report.md`／`report.json`（2026-10-14）記錄 920 tests、G1–G5 PASS，基線卻是 `b6ba45c747cb479cc975a5177794171791f994e0 +dirty`，lock 為 `5c02676b048119d50c2705d65e8d90e906f0f46f3325906e418849a62b3e828c`。不能移植成目前 SHA 的結果；該報告自己仍是 `Release decision: BLOCKED`，G0 waiver／ADR-012／license review 未完成。 |
| 最新 UVM 增補 | `evidence/uvm-static-call-metadata/report.md`（2026-10-22）：53 focused pure tests PASS；專用 real UVM regression `test_UVM_02_03_named_arguments_and_same_name_negative` 因 mandatory bwrap 不可用而 **BLOCKED／NOT_RUN**。目前 fast CI 綠燈沒有提供這個 integration case 的通過證據。 |
| Verilator supplement | `evidence/verilator-static-supplement/README.md`：importer／application／UVM metadata／ingest 合計 102 focused tests PASS，屬 unit／interchange；native Verilator integration、額外 UVM coverage **NOT_RUN**。不得拿 `.v` JSON golden 解碼代替 slang 的 Verilog 語意驗收。 |
| 本次檢查方式 | 只讀 Git identity、檔案與現有 evidence；未重跑上述 tests 或任何 HDL backend。文件追加只驗證原內容完整保留、差異範圍、路徑與 `git diff --check`。 |

**執行前置阻擋：目前不可直接 dispatch real-backends workflow。** `.github/workflows/ci.yml:107` 尚有 `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 || true`，會全域放寬主機安全限制；fast job 已採 `scripts/ci_prepare_bwrap_apparmor.py` 的 narrow official bwrap profile 路徑，但 real-backends 尚未對齊。先經授權修正該 job、保留 mandatory sandbox probe／fail-closed，再取得適用的執行授權；不得直接跑舊 job、關閉全域 AppArmor 限制、改走 in-process extraction 或 degraded fallback 迴避隔離。

### 30.3 已實作、有限支援、尚未驗證的能力矩陣

「已實作」表示在本次 HEAD 找到實際 code／test，不表示這次重新執行通過；「有限支援」與「未驗證」也不等於 slang 本身不支援該語法。

| 領域 | 目前狀態與界線 | 主要程式碼／既有測試 |
|---|---|---|
| Manifest／preprocessing context | ordered inputs、`-f/-F`、include／define／library、single／per-file／explicit CU、top parameters 已實作；未知選項 fail closed。`std` 可選 `1364-2005/1800-2017/1800-2023/latest`，但不是逐標準完整驗證。 | `config/models.py::LanguageOptions/Profile`、`manifest/resolver.py`、`manifest/slang_args.py`、`adapters/slang/args.py`；`tests/unit/test_manifest.py`、`test_slang_args.py`、`tests/integration/test_build_identity.py`。 |
| 宣告／階層／直接連接 | modules／interfaces／packages／classes／task／function、parameter specialization、generate、instance arrays、escaped names、port styles 有實作／fixtures；不是 bit-level netlist、driver/load 或 transitive dataflow。 | `adapters/slang/extract.py`、`extract.py::_walk_instance/_port_bindings`、`application/queries.py`；`tests/integration/test_sem.py`、`test_instance_arrays.py`、`test_extractor_dto.py`；`fixtures/language/sem*`。 |
| Reference index | 訪問已 elaborated 的 source occurrences，有靜態／dynamic／unresolved 分類與 searched scope；不完整，詳見 G1–G3。`COMPLETE` 不代表所有語法／所有程式碼的 references 都完整。 | `adapters/slang/ingest.py::STATIC_LIMITATIONS`、`extract.py`；`application/queries.py:574–590` 已有 scope 與空結果警示；`test_sem.py`、`test_slang_ingest.py`。 |
| Source location／macro | original byte spans、UTF-8／LSP 轉換、spelling／expansion／display location、ambiguous macro／token-paste 拒絕有實作。不是任意 macro 可安全重寫。 | `adapters/slang/locator.py`、`domain/locations.py`；`tests/integration/test_loc.py`、`tests/unit/test_locations.py`、`test_edit_slang_symbol.py`。 |
| Source `defparam`／VCS compat | **已實作，不應再列成完全缺失**：可解析 defparam target reference；slang elaboration 反映 override 值；未解析 target 保留 unsupported。`compat: vcs` 為顯式 profile deviation，不保證所有 VCS extensions。 | `extract.py::_h_defparam`、`manifest/slang_args.py::language_args`；`tests/integration/test_vcs_compat_defparam.py`，包括 `string.match()` 仍報錯的 negative case。 |
| UVM | pinned UVM classes／inheritance／virtual dispatch candidates、factory／config-db allowlisted resolved call metadata 已實作；16 arguments 等 bounds 有標示。未涵蓋所有 wrappers／generic bodies，也不推論 runtime。 | `adapters/slang/uvm_calls.py`、`extract.py`；`tests/unit/test_uvm_call_metadata.py`、`tests/integration/test_uvm.py`、`fixtures/uvm/svw_uvm_static_calls.sv`。最新 integration 狀態見 30.2。 |
| SVA／constraints／coverage／DPI | property／sequence／assertion／bind inventory、constraint／covergroup declarations、部分 DPI signature 有實作；checker instances／bind target lists／clocking references 等仍有缺口。不是 assertion execution、constraint solving、coverage measurement 或 foreign-code 執行。 | `adapters/slang/extract.py::_sva_sources/_binds/_record_assertion`、`extract.py`；`tests/integration/test_sva.py`、`test_sem.py`；`fixtures/language/sem_classmembers`、`sva02_unresolved`。 |
| Graph／editing／rename | graph 是候選來源；Serena function-only、task／constructor 可用 slang_symbol 受控路徑；rename 是 read-only preview，仍需 candidate validation／人類批准／CAS。不能說 task 全不可編輯，也不能把 slang edit 計為 Serena 能力。 | `application/impact.py`、`rename.py`、`editing/`；`docs/adr/ADR-012-*`、`ADR-013-*`；`tests/integration/test_rename_preview.py`、`test_edt_slang_symbol.py`、`test_int_serena.py`。 |
| Encrypted IP／Verilator | encrypted/stub boundary 保持 PARTIAL；prepare-source 只產生保留 offsets 的衍生 bytes 與 UNKNOWN gap report。Verilator 只讀取外部 5.052 JSON，`EXTERNAL_DECLARED/PARTIAL`、unbound，不執行工具。 | `application/source_preparation.py`、`application/supplement.py`、`adapters/verilator/`；`tests/unit/test_protected_source.py`、`test_verilator_importer.py`、`test_supplement_application.py`。 |

以上程式碼路徑除 `tests/`、`fixtures/`、`docs/` 外均相對於 `src/svweave/`。

### 30.4 主要不足與優先順序

| Gap | 優先 | 不足、實際影響與證據 | 處理方向 |
|---|---|---|---|
| G1：coverage 判讀粒度不足 | P0 | `ingest.py:24–33` 固定列出 dimension／return-type／class scope-prefix omissions；`658–672` 僅因實際收集到的 unsupported／dropped occurrences 等判 PARTIAL。這些系統性遺漏並未證明每次都會計數，不能把 build `COMPLETE` 視為 reference 完整。 | 先建立 feature／query／scope coverage contract、負向結論 gate，再擴大抽取；保留既有 searched-scope／empty-result 警示。 |
| G2：重要型別／常數依賴抽取不足 | P1 | packed/unpacked dimensions、subroutine return-type、`Class::member` 前綴、enum／struct member type 等未完整建 reference，可能漏掉改 width／typedef／class 名的影響。見 `extract.py::_h_subroutine/_type_ref`、`ingest.py::STATIC_LIMITATIONS`、`docs/supported_features.md` §4。 | 逐種建立手審 source-span golden 與 resolved target；不能用 folded numeric width 或字串比對捏造 semantic edge。 |
| G3：只遍歷特定 elaborated scope | P1 | unused generic bodies、未展開 definitions／library、inactive branches、只作 static-call scope 的 class specialization body 不在完整 reference 遍歷內。見 `docs/supported_features.md` §4、SEM-10。 | 明列 source-only／inactive／elaborated coverage，先補可由 pinned API 正確取得的 call-only specialization；不能靠強制 instantiate 全部 generic 改變 build。 |
| G4：legacy Verilog 專用證據不足 | P1 | `fixtures/language` 是 `.sv/.svh`；本次未找到 `.v/.vh`＋1364 mode 的專用 slang golden matrix。接受副檔名或 std knob 不等於實測 ANSI/non-ANSI ports、implicit nets、signed widths、UDP/config/specify 等。 | 建立明確受支援／僅 inventory／unsupported 的 legacy corpus；先覆蓋一般 RTL，不以完整 gate-level/timing 語意為交付目標。 |
| G5：port／internal symbol 對使用者不一致 | P1 | `output reg` 可有 port 與 variable 雙 declaration；`docs/manifest.md` 說明 port query 可能零 references。`queries.py::references` 查單一 declaration；`impact.py::_twins/_with_twins` 與 rename 已有部分 twin 處理，但不是統一 reference 模型。 | 定義可追溯 alias／related-symbol 查詢契約；不能單靠同名合併跨 scope／instance 訊號；空 port refs 不得解讀為 unused。 |
| G6：較少見 SV／SVA／library 語法 | P2；真實目標 profile 用到者升 P1 | clocking-block signals、bind target instance lists、checker instances、primitive/config blocks、DPI import/export 邊界未完整抽取；explicit CU groups 含 library inputs 直接 `CAPABILITY_MISMATCH`（`adapters/slang/args.py:63–70`）。 | 先建立最小 fixture 與 unsupported inventory，再按 pilot 需要補 semantic API；不可宣稱 declarations 可見即完整支援全部 references。 |
| G7：真實導入 context 差異 | P1 | macro 組合 include 未必在 snapshot directive scan 發現，dependency leak 會阻擋；vendor filelists 不能任意吞選項；profile 的 language_mode 目前固定 `systemverilog`，std 是全 profile knob，未提供已驗證的 per-file mode/library mapping。 | 測 manifest→snapshot→worker end-to-end 的 context parity；保留 fail-closed，沒有 API 證據就記支援限制，不強加 free-form flags。 |
| G8：目前版 qualification 與文件落差 | P0 | fast PASS／real job SKIPPED；歷史報告 identity 不同；latest UVM wiring 有歷史 sandbox blocker。`docs/limitations.md` 的「ignored options 不可配置」已落後 `Profile.ignored_options`；泛稱所有 omission 都會計 unsupported 也須修正。 | 安全 CI gate、zero-skip evidence、目前 SHA 的 integration／acceptance、claim-to-test 文件同步；不得只增加語法而不重新 qualification。 |

**不列為靜態支援缺陷的 non-goals：**UVM factory 真正選擇、config-db 命中與 precedence、phase scheduling、runtime virtual dispatch、simulation、formal、coverage closure、bit-level connectivity、CDC/RDC、encrypted source 解密。slang 保留為權威 semantic backend；不以 Verilator 取代，也不把外部 JSON inventory 當作 G1–G7 已補齊。

### 30.5 可拆 issue 的 requirement → test → acceptance 對照

下表每列都是待辦；「既有」測試是回歸錨點，不代表已覆蓋新需求。新 tests 必須經受限真實 worker／gateway 驗證，不可以為了避開 sandbox 而直接呼叫 extractor。Golden 由小型 source 手工核對並附語意理由，不由被測 extractor 自動批准；採 closed-scope 對照，同時檢查缺少與多餘的 declarations/references，不只 assert 某個節點存在。新增 tests 必須接入 `scripts/qualify.py` 的 requirement-module mapping，而非僅存放於 tests 目錄。

| 新 requirement／原規格關聯 | 交付與可測 acceptance criteria | 程式碼位置／測試映射 |
|---|---|---|
| **HDL-R01：query coverage 可判讀**（SVW-R06/R12；§10.4） | 對已知 dimension/type omission、inactive branch、unloaded library、project/external-root unsupported 各有 fixture；正向 edge 仍可讀，但 unknown／未搜尋範圍不得支持「無引用／無影響／可刪除」。即使 build COMPLETE，回應仍可辨識該功能 coverage。與舊 envelope／schema 保持相容或明確版本化；truncation／STALE 不放寬。 | `adapters/slang/ingest.py`、`application/queries.py`、`semantic_common.py`、`impact.py`、`rename.py`；既有 `tests/unit/test_slang_ingest.py`、`test_semantic_envelope.py`、`test_rename_pure.py`、`tests/integration/test_sem.py`；擬新增 **HDL-COV-01/02**：`tests/integration/test_reference_coverage.py`。 |
| **HDL-R02：補 dimensions 與 type references**（SVW-R05/R06） | packed/unpacked range 內每個 parameter、typedef return type、class scope prefix、enum/struct member type 的 target、scope、source bytes 精確符合 golden；nested typedef／同名 scopes／macro／重複 specialization 不錯連或重複。不可由常數折疊結果反推原引用；binding API 無法提供者明示 unsupported。 | `adapters/slang/extract.py`、`locator.py`、`protocol.py`、`ingest.py`；擬新增 `fixtures/language/refs_types_dims/`、人工 golden、`tests/integration/test_reference_types.py`，**HDL-REF-01..04**；保留 SEM-01/02、LOC-01/02 回歸。 |
| **HDL-R03：未遍歷 scope 與 specialization**（SVW-R06/R09；SEM-10） | 同一 generic class「作型別使用」「只由 `C#(T)::f()` 呼叫」「完全 unused」三組對照；可解析者 target 正確、declaration 不膨脹、source occurrence 去重且 specialization context 可追溯。inactive code 不冒充 active；未訪問 body 有可查限制，資源上限／循環不造成無界遍歷。 | `adapters/slang/extract.py`、`extract.py::_drain_specs/_do_spec`、`application/queries.py`；既有 `fixtures/language/sem02_paramclass/`、`fixtures/language/sem10_unused/`、`tests/integration/test_uvm.py`；擬新增 `tests/integration/test_generic_scope_coverage.py`，**HDL-SCOPE-01..03**。 |
| **HDL-R04：legacy Verilog qualification**（SVW-R02/R06/R07/R08；§4.4） | `.v`＋included `.vh` 在明確 `std: 1364-2005` 下測 ANSI/non-ANSI ports、wire/reg/implicit-net 規則、signed width、continuous/procedural assignment 的引用、function/task、generate、parameter/defparam、library lookup；可見宣告／reference／hierarchy／diagnostics 對上手審 golden。對 SV-only syntax 在 Verilog mode 的接受/拒絕行為與 pinned driver 一致；mode 改變 config/build ID，不能由 suffix 猜 mode。UDP/specify/config 若不支援須明示，不要求模擬 timing。 | `config/models.py`、`manifest/slang_args.py`、`adapters/slang/args.py`；既有 `test_vcs_compat_defparam.py`、`test_build_identity.py`；擬新增 `fixtures/verilog/`、`tests/integration/test_verilog_modes.py`，**HDL-VLOG-01..05**。 |
| **HDL-R05：port identity 與 related refs**（SVW-R06/R08） | `output reg`、non-ANSI ports、inout、interface/modport、兩個同名不同 scope/instance signals：選 port 或 internal symbol 可解釋相關 declarations／ref scope；合併若提供須明示 basis 並可回溯，不偷併同名 symbol。named/positional/implicit/wildcard/open/concat/array associations 不遺失原 expression；不產生 bit-level connectivity 承諾。 | `adapters/slang/extract.py::_port_bindings`、`application/queries.py`、`impact.py::_twins`、`rename.py`；既有 SEM-05/06、`test_instance_arrays.py`、`test_impact.py`、`test_rename_preview.py`；擬新增 `tests/integration/test_port_symbol_identity.py`，**HDL-PORT-01/02**。 |
| **HDL-R06：SVA／clocking／DPI／特殊節點**（SVW-R06/R10/R12） | clocking signal、`bind mod : instance_list`、checker、DPI import/export、primitive/config 各有正/負 fixture；逐項選「resolved static inventory」或帶 kind/location/reason 的 unsupported，不靜默消失。bind source 與 elaborated target 分離、unknown target 明確診斷；SVA execution/proof 始終 NOT_RUN。 | `adapters/slang/extract.py`、`extract.py::_sva_sources/_binds`、`ingest.py`；既有 `test_sva.py`、SEM classmembers、`test_extractor_dto.py`；擬新增 `tests/integration/test_static_language_edges.py`，**HDL-EDGE-01..06**。 |
| **HDL-R07：build context parity**（SVW-R02/R03/R04；BLD/SNP） | 對單一／per-file／explicit CU、include 順序、macro include、同檔不同 CU、top-scoped override、std/timescale/compat 建 identity 與診斷對照；macro include 要麼被合法完整 capture，要麼明確 BLOCKED，絕不漏依賴仍 publish。group＋library 沒有經 probe 的解法前繼續拒絕；新可忽略選項不能吞 source/argument，所有 deviation 入 ledger。 | `manifest/resolver.py`、`snapshot/capture.py`、`adapters/slang/args.py`、`application/build.py`；既有 `test_manifest.py`、`test_snapshot.py`、`test_slang_args.py`、`test_fix_round_b.py`、`test_build_identity.py`；擬新增 `tests/integration/test_build_context_matrix.py`，**HDL-BLD-01..04**。 |
| **HDL-R08：最新 UVM wiring 真實驗收**（SVW-R09；UVM-01/02/03） | 在 pinned 2020.3.1 UVM 下，named/ordered/empty/omitted args、allowlisted factory/config-db calls、同名非 UVM `set/get` negative、args truncation 的 real parser→DTO→storage→query 結果符合 unit contract；未知 wrappers 保留一般 call，不假分類。`static_call_site`／`runtime_resolution=not_computed` 不變，不能填 defaults、推測 config 命中。 | `adapters/slang/uvm_calls.py`、`extract.py`、`application/queries.py`；**優先重跑既有** `tests/integration/test_uvm.py::test_UVM_02_03_named_arguments_and_same_name_negative` 與 `tests/unit/test_uvm_call_metadata.py`；新增 corner fixtures 對應 **HDL-UVM-01/02**。 |
| **HDL-R09：下游 impact／rename／edit 不超額宣稱**（SVW-R08/R11/R13/R14/R15） | G1–G7 新 positive edges 會出現在 impact/preview，未支援／macro／ambiguous／inactive 範圍不變成安全負向判斷。header/macro 的 includer 影響須標候選或重建驗證；outer/imported-scope shadowing 負測須比較 candidate reference target，不能只看無 compiler error。Serena task/constructor 不可假成功；slang_symbol 保持 distinct provenance。candidate 重建失敗、錯 scope、STALE、未批准或 CAS mismatch 均不寫 original；graph candidate 永不升格為 resolved edge。 | `application/impact.py`、`diff_impact.py`、`rename.py`、`editing/`；既有 `test_impact.py`、`test_diff_impact.py`、`test_rename_preview.py`、`test_edt_slang_symbol.py`、`test_int_serena.py`、`tests/fault_injection/`；擬新增 **HDL-SAFE-01/02** 到相關 suites，涉及 write safety 的新規則亦納入 `scripts/mutation_suite.py`。 |
| **HDL-R10：安全且不可假綠的 CI／qualification**（SVW-R11/R19/R20；§22/23） | real job 移除全域 userns 限制關閉操作，對齊 approved narrow bwrap profile 與 mandatory probe；測 sandbox 不可用時確實 FAIL/BLOCKED。當 `SVWEAVE_REQUIRE_BACKENDS=1` 缺 UVM/任何 mandatory backend，不可經 skipif 綠燈；required tests 的 skip/error/fail 均阻 gate。保存 exact SHA／source identity／lock／test counts／JUnit／probe／qualification report。 | `.github/workflows/ci.yml`、`scripts/ci_prepare_bwrap_apparmor.py`、`scripts/qualify.py`、`tests/integration/conftest.py`、`tests/integration/test_repo_profiles.py::test_shipped_uvm_smoke_profile_builds_against_the_pinned_library`；既有 `tests/unit/test_ci_sandbox_gate.py`、`test_ci_bwrap_apparmor.py`；擬新增 **HDL-CI-01..03**：workflow safety、missing-UVM fail、zero-skip result parsing。 |
| **HDL-R11：Verilator／protected supplement 不污染權威索引**（SVW-R12/R18；§10.1/16.5） | 既有 import、hash/tamper/path/resource bounds、erased WIDTH-reference negative、source preparation offset/UNKNOWN tests 持續通過。即使 supplement 與 slang 名稱相同也不可寫入 resolved references、改 completeness/published build 或提高 edit eligibility；不啟動 Verilator、不執行 simulation。額外 coverage 只能另做逐項證據比較，不能預設補洞。 | `application/supplement.py`、`source_preparation.py`、`adapters/verilator/`；既有 `test_verilator_importer.py`、`test_supplement_application.py`、`test_protected_source.py`；擬新增 cross-store negative **HDL-SUP-01**。 |
| **HDL-R12：文件／宣告同步**（SVW-R01/R12/R20；DOC） | 每個宣告都有 feature、profile/std、實作位置、positive/negative test、最近 evidence SHA／狀態；區分 implemented／partial／unsupported／not validated。修正 ignored-options、reference counting、task editing／rename-preview 等過時表述，不改原歷史規格來偽造過去狀態；README 三語與 capability/limitations 一致。 | `docs/supported_features.md`、`docs/limitations.md`、`docs/manifest.md`、`README*.md`、`capability_report.json`；既有 `tests/unit/test_gateway_cli.py::test_DOC_01_*`、schema tests；擬新增 **HDL-DOC-01** feature-to-evidence consistency check。 |

### 30.6 分階段補強與 gate

依賴順序：**A → B → C → D**；E 為獨立可選 supplement 保護，不能替 A–D 解鎖。以 gate 而非未量測工期承諾進度；一般 RTL 與高頻 type references 優先於較罕見語法。

| Phase | 優先／需求 | 工作與完成 gate |
|---|---|---|
| **A：先建立可信 baseline** | P0；R01/R08/R10/R12 | 先修復 real CI 安全前置條件並獲適用授權；先分類已知 coverage，避免 COMPLETE 被誤讀；修復 required mode 的 skip 漏洞，建立 feature→test→evidence 表。在合法 sandbox host 跑既有 pinned semantic/UVM／三後端 suites，取得目前 source 的逐項結果。缺 host、依賴、授權或 pending sign-off 就記 BLOCKED；不能用 fast 綠燈越過。 |
| **B：常用 RTL 與型別依賴** | P1；R02/R04/R05/R07/R09 | 先寫手審 positive/negative fixtures，再逐項補 dimension/type refs、legacy Verilog mode matrix、port identity、build context。每個 issue 必須經真實 worker→gateway query 驗證、location/source bytes 精確、舊 SEM/LOC/BLD/SNP 不回歸；只針對通過 subset 擴大支援宣告。 |
| **C：進階 SV／UVM／SVA 邊界** | P1/P2；R03/R06/R08/R09 | 先處理實際目標 profile 阻擋的 call-only specialization／clocking／bind/checker，其他按 corpus evidence 排序。pinned API 不足先做最小 capability spike/ADR，再決定 helper 或明確 unsupported；不猜 API、不換 backend、不增加 stub 讓測試過。所有 runtime 欄位仍 not_computed/NOT_RUN。 |
| **D：重新 qualification 與受限 pilot** | P0 release gate；R10/R12＋全部已承諾 subset | 在 exact source＋lock 上完成 real probes、unit/contract、integration、fault-injection、acceptance、critical mutations、coverage、必要 benchmark，由 `scripts/qualify.py` 計算狀態；required cases 不可 SKIP。保留原 §22.2 門檻。用有授權且可重現的 RTL/UVM profile 做 read-only pilot；無可靠 golden 的大專案只報 gaps，不報全面正確率。未有 EDA 不妨礙限定 static subset，但不得宣稱 EDA/project qualification。 |
| **E：可選外部 inventory 守界** | P2；R11 | 只維護 import/query 與 protected preparation 的純處理、安全和 provenance tests。若日後提出新的差異比較，先定義 artifact 身分／缺漏／不可信來源規則再另行批准；目前不新增 native Verilator runner，不模擬，也不參與 slang completeness 判定。 |

每個 issue 的完成證據至少包含：requirement/Test IDs、完整 source SHA 或 dirty source hash、lock hash、host/platform、profile/std/CU/defines/tops、命令、PASS/FAIL/BLOCKED/SKIP/NOT_RUN 計數、golden review 理由、diagnostics／unsupported inventory、可重現 artifact 路徑。新 extractor 修改必須更新其 identity，不能沿用舊 build cache 或舊 qualification。遇資源上限不以縮小 corpus 的結果冒充原規模 benchmark。

**建議第一批 issues，依序：**

1. `HDL-R10`：安全 CI 前置條件＋mandatory/no-skip gates（不直接 dispatch 現有 real job）。
2. `HDL-R01/R12`：coverage matrix／空結果契約與過時宣告修正。
3. `HDL-R08`：既有最新 UVM integration 的真實驗收，先關閉已知未驗證事項。
4. `HDL-R04/R05`：Verilog `.v/.vh` corpus＋port/internal-symbol 關係。
5. `HDL-R02`：dimension／return-type／scope-prefix references；完成後再擴大 generic／進階語法。

### 30.7 可宣布「足夠」的停止條件

不以「所有 SystemVerilog 都支援」作不可能核對的完成宣告。對一個明確的**靜態使用情境＋profile 集合**，只有在下列條件成立後，才可宣布該 subset 足夠：

- 使用者需要的語法／查詢都有測試與已知缺口分類；G1 的負向結論風險可由機器讀取，未搜尋範圍不被隱藏。
- 該 subset 的正／負 golden 與 API tests 在當前 SHA／lock 的真實受限 worker 通過；required integration／acceptance 無 SKIP，三後端降級明列而非冒稱 READY。
- manifest/context、original source spans、snapshot freshness、query completeness、candidate approval/CAS/recovery 的既有安全不變。
- `scripts/qualify.py` 產生可追溯結果；G0 waiver／ADR-012／license 等 owner 決策仍由 owner 處理，代理不得代簽或改成 PASS。
- EDA compile／simulation／formal／RTL coverage 與軟體 QA 分開；本計畫不要求執行 simulation，也不把 absence of EDA evidence 藏起來。

**本次收斂結果：維持 developer preview／UNQUALIFIED 的保守定位，先補證據與 reference coverage，再補高頻 Verilog／SystemVerilog 語意；不是移除 slang、加入模擬器或擴寫成 full-language signoff 工具。**

