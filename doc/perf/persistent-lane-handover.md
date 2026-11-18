# 交接：Persistent Lane（P0）— Windows 驗證

> 分支：`feat/persistent-lane`（base：`main` @ `0a1d61f`）
> commits：`3d38358`（CLI `--book-lane`）、`3b5b423`（Batch Console lane 排程）、review 修正（記憶體保護、失敗計數、model-load 標記）
> Mac 端已完成 P0-A／P0-B（正確性）；**你的任務是 P0-C（Windows 效能與資源短測），P0-D 有條件執行**。

## TL;DR

- 問題：Batch Console 每本書開一個新程序，偵測／OCR／inpainting 模型每本書重載一次；同時 5 本 = 5 份模型與 CUDA context。
- 改法：新的進階設定「持久 Lane」（`lane_books`，0 = 關閉 = 原行為）。>0 時一個程序依序處理最多 N 本書，**模型只載入一次**，每本書仍獨立翻譯批次、獨立狀態、獨立驗證輸出。
- Mac 驗證：模型載入每 lane 1 次；翻譯批次不跨書；輸出與舊流程逐像素一致；真實 GUI＋真實模型 3 本書：1 次啟動 / 27.6s vs 舊流程 3 次啟動 / 44.7s（並行 1，不代表並行吞吐）。
- 回滾：設定改回 0 即可，舊流程程式路徑未改動。

## 工作規則（使用者要求）

1. 腳本自己跑、自己評估、最後回報；**遇到需要安裝的依賴立刻停下回報**。
2. git commit：`ch010060 <ch010060@gmail.com>`，**不加任何 agent trailer**。
3. **不要同時導入其他優化**（KV 量化、backend sampling、inpainting crop、GC 降頻），否則無法歸因。
4. 不跑 2500 頁長測；用短測決定是否接受。

## 今天發現、會影響判讀的事實（已在程式碼確認）

GUI 的預設值來自 `headless_batch_by_first_level_dirs.parse_args`：`--subprocess` **預設 True**、`memory_percent` 95、同時處理 5、批次大小 4。所以使用者 2500 頁 GUI 長跑（若未改設定）是**子程序模式**：

| 子程序模式行為 | 影響 |
|---|---|
| worker 對**每一頁**單獨呼叫 `translate_batch` | `批次大小=4` 實際無效；Sakura 每頁一個請求；批次清理（gc、aggressive CUDA cleanup、`SetProcessWorkingSetSize`）每頁執行 |
| 系統 RAM > 95% 時 worker 提前退出、父程序重開子程序 | **書中途重載整套模型**；RAM 滿載時可能反覆發生，與「速度掉下來又恢復」吻合（待證） |

注意：子程序模式是「一個子程序依序處理多頁」，不是每頁新開子程序；只有記憶體保護、`batch_per_restart` 或異常才會讓同一本書多次載入模型。

### 立即可做、零成本的證據收集（先做）
在 Windows 的 `result/batch_logs/` 搜尋 2500 頁那次的 log：
```powershell
Select-String -Path result\batch_logs\*.log -Pattern "系统内存超过限制","进程内存超过限制" | Measure-Object
Select-String -Path result\batch_logs\*.log -Pattern "系统内存超过限制" | Select-Object -First 5
```
- 次數 > 0 時，**再確認每則訊息之後是否真的出現新的 `🔄 批次 N` 行**（= 新子程序 = 模型重載）；只用超限訊息數量不能代表重載次數。回報實際重啟次數與時間分佈。

## Lane 模式與舊流程的行為差異（驗證時必須知道）

| 項目 | 舊流程（預設子程序模式） | Lane 模式 |
|---|---|---|
| 程序 | 每本書 1 個父程序 + 子程序 | 每 lane 1 個程序，依序處理 ≤ N 本 |
| 模型載入 | 每本書（+ 每次記憶體保護重啟） | 每 lane 1 次 |
| `--subprocess` | 有 | **自動移除**（lane 本身即有限生命週期 worker） |
| 95% 記憶體保護 | 有（超限 → 子程序重開） | **有**：沿用 `--memory-percent/--memory-limit`；超限時在批次／頁面邊界停止（已存頁保留），lane 不再領下一本，送 `lane_exit`；GUI 重排：執行中被中斷的書每本最多 3 次（不佔崩潰重排額度）；尚未開始的書重排不計次；連續 5 條 lane 因 RAM 退出且沒有任何書完成時停止排程，剩餘書標記「已停止」 |
| 翻譯分組 | 每頁一個 Sakura 請求 | **依 `批次大小` 分批**（同一本書內；不跨書） |
| 完成判定 | 程序退出 + 驗證輸出 | 每本書各自驗證輸出（`[LANE_EVENT]` 只是通知） |
| 中途崩潰 | 該本書異常退出 | 已完成的書保留；未輪到的書重新排隊 1 次 |

⚠️ 翻譯分組會改變：嚴格對照時，Lane 組請把 GUI「批次大小」設為 **1**（等同舊流程每頁一請求）；批次大小 4 另列一組實驗。

## P0-C：Windows 短測（固定並行數對照）

### 準備
```powershell
git fetch origin
git switch feat/persistent-lane
git log -3 --format="%h %an <%ae> %s"   # 3b5b423 / 3d38358 / 0a1d61f
python -m pytest tests\test_book_lane.py tests\test_batch_panel_lane.py -q
```
- Fixture：60–100 頁、約 10 本（固定同一批書，記錄書名與頁數），Sakura 常駐並已暖機，覆寫輸出開啟（或每輪前清輸出）。
- 量測（每輪都要）：
  ```powershell
  typeperf "\GPU Process Memory(*)\Dedicated Usage" "\GPU Process Memory(*)\Shared Usage" "\Memory\Available MBytes" -si 5 -o run_<name>.csv
  nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv -l 5 > vram_<name>.csv
  ```
  - WDDM 下 `nvidia-smi` 的逐程序顯存欄位可能顯示 N/A，**以 typeperf 的 GPU Process Memory 計數器為主**。
  - 另記：整批 wall、成功頁數、**實際模型載入次數**（`result/batch_logs` 中 `[model-load]` 行數；兩種模式都會印，逐模型類別統計）、記憶體保護觸發次數（舊流程 `系统内存超过限制`；lane `lane_exit`）、llama-server log 的 eval tok/s。
  ```powershell
  Select-String -Path result\batch_logs\*.log -Pattern "\[model-load\]" | Group-Object { ($_.Line -split ' ')[1] } | Select Name, Count
  ```

### 組別（同時處理固定 3）
| 組 | 設定 |
|---|---|
| L3 | 同時處理 3、持久 Lane 0（舊流程，子程序模式維持預設） |
| N3-b1 | 同時處理 3、持久 Lane 4、批次大小 1 |
| N3-b4 | 同時處理 3、持久 Lane 4、批次大小 4（翻譯分組不同，另行判讀） |

順序 L3 → N3-b1 → N3-b1 → L3（ABBA），N3-b4 最後單跑。

### Gate
| 項目 | Pass |
|---|---|
| 正確性 | 所有書 `已完成`、輸出頁數完整；無跨書（lane log 中每本書各自一段 `批量翻译完成`） |
| 模型重用 | Lane 組每類模型的 `[model-load]` 次數 = lane 程序數（約 書本數 / 4），遠低於 L3 |
| 顯存 | Shared GPU Memory 不隨 Dedicated 逼近上限而異常增長，且其增長時段與 Sakura tok/s 下降無對應；Dedicated 峰值低於容量 |
| 記憶體 | 無持續上升；每個 lane 結束後回落 |
| 效能 | N3-b1 兩輪平均 wall < L3 兩輪平均，且差距大於 L3 兩輪間差異 |
| 故障 | 若有 lane 崩潰：已完成的書保留、未完成的書重排一次後完成 |
| RAM 中斷續跑 | 若有 `lane_exit`（RAM）：被中斷的書續跑後頁數完整、無重複輪迴；不覆寫模式下中斷前已存的頁未被重寫 |

## P0-D（有條件）
只有 P0-C 全部通過、且 Lane 3 的 Dedicated GPU 峰值仍有 ≥ 4 GB 餘裕時，再測同時處理 4（持久 Lane 4）。不測 5，除非 4 也有餘裕。

## 回報格式
```
P0-C 結果：PASS / FAIL / 待使用者決定
- 2500 頁舊 log：記憶體保護重啟 N 次（時間分佈）
- wall：L3 a/b，N3-b1 a/b，N3-b4
- 模型初始化次數：L3 x，N3 y
- GPU Shared 峰值 / RAM 峰值 / Sakura tok/s 範圍
- 異常與 log 路徑
- 建議：是否把持久 Lane 設為預設（建議值 N）
```

## 檔案地圖
| 用途 | 位置 |
|---|---|
| CLI lane | `manga_translator/mode/local.py`：`run_book_lane`、`translate_files(translator=...)` |
| 書本狀態重設 | `manga_translator/manga_translator.py`：`MangaTranslator.end_book()` |
| GUI 排程 | `scripts/batch_panel.py`：`schedule`、`lane_events`、`finalize_lane`、`terminate_job` |
| 設定預設 | `scripts/headless_batch_by_first_level_dirs.py`：`--lane-books`（預設 0） |
| 測試 | `tests/test_book_lane.py`、`tests/test_batch_panel_lane.py` |

## 合併前必須在 Windows 跑的測試（雲端缺 torch 無法執行）
```powershell
python -m pytest tests\test_book_lane.py tests\test_batch_panel_lane.py tests\test_batch_panel_p0.py tests\test_batch_panel_p1.py tests\test_batch_panel_p2.py tests\test_batch_panel_v2.py tests\test_batch_first_level_dirs.py -q
```
- `test_book_lane.py` 的 `end_book` 與 `TranslateFilesErrorCountTests` 需要 torch（翻譯核心），只能在本機環境跑。
- 雲端（Linux，無 torch）已跑：GUI 相關 113 passed；2 個環境性失敗（V15 需 Windows Qt platform；`default_config_path` 需本機 `examples/config.json`，未改動前同樣失敗）。

## 已知限制 / 待辦
- GUI 逐書狀態要等整條 lane 結束才結算（一條 lane 5 本時，前 4 本會顯示執行中直到第 5 本結束）。不影響正確性，排在 P0-C 之後處理（review P1）。
- 記憶體重排：執行中被中斷的書每本最多 3 次；未開始的書不計次；連續 5 次 RAM 退出且無書完成 → 停止排程（全域煞車）。
- 覆寫模式（force）下重排會重做該書已完成的頁（測試鎖定為已知取捨，且保證會結束）；不覆寫模式會保留已存頁、只補缺頁。
- RAM 保護不等於 VRAM 保護：Shared GPU Memory 遷移可能在 RAM < 95% 時就發生；`cancel_check` 也無法中斷進行中的單次 GPU 推論。VRAM 由 P0-C 的 Dedicated／Shared 時序驗證。
- 書內並行（`--concurrent`）在 Mac 上結束時會 segfault（exit 139，輸出正確），未納入 lane，勿同時開啟。
