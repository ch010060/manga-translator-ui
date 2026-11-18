# 交接：Persistent Lane（P0）— Windows 驗證

> 分支：`feat/persistent-lane`（base：`main` @ `0a1d61f`）
> commits：`3d38358`（CLI `--book-lane`）、`3b5b423`（Batch Console lane 排程）
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

### 立即可做、零成本的證據收集（先做）
在 Windows 的 `result/batch_logs/` 搜尋 2500 頁那次的 log：
```powershell
Select-String -Path result\batch_logs\*.log -Pattern "系统内存超过限制","进程内存超过限制" | Measure-Object
Select-String -Path result\batch_logs\*.log -Pattern "系统内存超过限制" | Select-Object -First 5
```
- 次數 > 0：確認長跑中有記憶體保護觸發的子程序重啟（每次 = 模型重載）。回報次數與時間分佈。

## Lane 模式與舊流程的行為差異（驗證時必須知道）

| 項目 | 舊流程（預設子程序模式） | Lane 模式 |
|---|---|---|
| 程序 | 每本書 1 個父程序 + 子程序 | 每 lane 1 個程序，依序處理 ≤ N 本 |
| 模型載入 | 每本書（+ 每次記憶體保護重啟） | 每 lane 1 次 |
| `--subprocess` | 有 | **自動移除**（lane 本身即有限生命週期 worker） |
| 95% 記憶體保護 | 有 | **無**（以 lane 本數上限自然回收代替） |
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
  另記：整批 wall、成功頁數、模型初始化次數（舊流程：`result/batch_logs` 中 `🔄 批次` 行數，每行 = 一個子程序；Lane：`初始化翻译器` 行數，每行 = 一個 lane）、記憶體保護重啟次數、llama-server log 的 eval tok/s。

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
| 模型重用 | Lane 組 `初始化翻译器` 次數 = lane 程序數（約 書本數 / 4），遠低於 L3 的 `🔄 批次` 次數 |
| 顯存 | Lane 組 Shared GPU Memory 無持續增長 |
| 記憶體 | 無持續上升；每個 lane 結束後回落 |
| 效能 | N3-b1 兩輪平均 wall < L3 兩輪平均，且差距大於 L3 兩輪間差異 |
| 故障 | 若有 lane 崩潰：已完成的書保留、未完成的書重排一次後完成 |

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

## 已知限制
- Lane 模式沒有 95% 記憶體保護；若 P0-C 顯示 lane 內記憶體持續上升，再在 `run_book_lane` 書與書之間加入水位檢查（提前結束讓 GUI 重排），目前不預先實作。
- 書內並行（`--concurrent`）在 Mac 上結束時會 segfault（exit 139，輸出正確），未納入 lane，勿同時開啟。
