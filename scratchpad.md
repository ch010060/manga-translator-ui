# manga-translator-ui 效能優化 scratchpad

> 進度紀錄，每完成一步即更新。量測工具：`scripts/bench_stages.py`（逐 stage 計時，translator=original 隔離 LLM）。

## 環境
- 機器：M5 Pro / 64GB，PyTorch 2.15 MPS
- venv：`.venv`（uv, py3.12, requirements_metal.txt），models：`models/`（自動下載）
- LLM：oMLX `http://127.0.0.1:8000/v1`，model `sakura-14b-qwen2.5-v1.0-q4km`（需 Splash 後端）
- 樣本：`doc/images/0012.png`、`doc/images/110012.png`（1440×2137），repeat 3 → 9 頁計時（第 1 張 warm-up 不計）
- Rollback：`git checkout -- <file>`；刪 `.venv` `models` 即還原環境

## 基準（2026-11-13，未改動）
wall/page = **4.12s**

| stage | s/page | 佔比 |
|---|---|---|
| inpainting | 1.41 | 34.6% |
| ocr | 0.95 | 23.0% |
| detection (DBNet 0.46 / MangaLens 0.05 / YOLO 0.02) | 0.55 | 13.3% |
| mask_refinement | 0.22 | 5.2% |
| textline_merge | 0.22 | 5.2% |
| text_rendering | 0.13 | 3.1% |
| 未歸屬 overhead | ~0.9 | ~22% |

Profile 熱點：gc.collect ×6/page 0.43s；get_image_md5 PNG 編碼 ×3/page 0.26s；OCR xpos `.to()` 0.28s；bilateralFilter d=17 0.34s；panel detection 每頁 IndexError（0.13s，功能 bug）。

## 計畫

### Phase 1 — 輸出不變的 overhead 清除（目標 −21%）
- [x] 0. 擴充 bench：`--dump` 輸出 OCR 文字 + 渲染圖 md5；基準跑兩次 dump 完全一致（MPS 確定性 OK）
- [x] P1. `gc.collect` 降頻：非 CUDA 只在批次邊界（aggressive）回收；CUDA 行為不變（無法在此驗證 CUDA，不動）
  - `manga_translator.py` `_cleanup_gpu_memory` / `_run_inpainting` / `_run_text_rendering`；`inpainting/common.py`
- [x] P2. `get_image_md5` 改 hash `size + tobytes()`；已確認只用於同次執行內的 context key 與 debug 資料夾名（含 timestamp），無跨次依賴
- [x] P3. `ocr/xpos_relative_position.py` XPOS sin/cos 表依 (len, offset, downscale, device, dtype) memoize；CPU/MPS 單元比對 `torch.equal` 一致
- [x] 驗證：dump 與基準 byte-identical；bench 結果見下
- Pass：輸出完全一致 且 wall/page ≤ 3.5s；Fail → 逐項 revert 定位
- **結果：PASS** — wall/page 4.12s → **3.33s（−19%）**，輸出 byte-identical

| stage | 基準 s/page | Phase 1 後 | 差 |
|---|---|---|---|
| inpainting | 1.43 | 1.29 | −0.14（gc 移除） |
| ocr | 0.94 | 0.74 | −0.20（XPOS 快取） |
| detection | 0.56 | 0.54 | −0.02 |
| text_rendering | 0.13 | 0.05 | −0.08（gc 移除） |
| 未歸屬 overhead | ~0.6 | ~0.26 | −0.34（gc + md5） |

註：bench 為 batch_size=1，每頁都是批次邊界；實際 batch_size=3 時 gc 次數更少，效益應 ≥ 此值。

### Phase 3 — LLM 量測（已完成，提前執行）
- [x] oMLX/Splash 不支援 Sakura（Splash 只服務指定 Qwen3.x 模型）→ 改用 llama.cpp
  - `llama-server -m ~/.omlx/models/SakuraLLM/Sakura-14B-Qwen2.5-v1.0-GGUF/sakura-14b-qwen2.5-v1.0-q4km.gguf --port 8080 -ngl 999 -c 8192`（log: /tmp/mtbench/llama.log）
  - 專案預設 `SAKURA_API_BASE=http://127.0.0.1:8080/v1`，免設定
- [x] bench 加 `--translator sakura`；0012.png ×8 頁
- **結果：含 LLM 每頁 7.66s，LLM 佔 4.10s（53.6%）** — 推翻「LLM 佔比不大」的假設
  - 每頁 1 個 request，輸出 ~110 tokens，decode ~27 tok/s（M5 Pro, 14B Q4）；prompt eval 可忽略 → 時間 = 輸出 token 數 / decode 速度
  - 註：batch_size=1；batch_size=3 時總輸出 token 不變，每頁 LLM 時間預期相近（待驗）

| stage | s/page | 佔比 |
|---|---|---|
| LLM（sakura） | 4.10 | 53.6% |
| inpainting | 1.45 | 18.9% |
| ocr | 0.80 | 10.4% |
| detection | 0.54 | 7.0% |
| 其他 | 0.77 | 10.1% |

### 範圍調整（2026-11-13 10:45）
- 使用者指示：LLM 不在範圍（實際生產機為 Windows RTX 5090，LLM 快 10× 以上），只評估 LLM 以外瓶頸
- **生產平台是 CUDA** → 優先處理 CPU-bound 項（5090 上 GPU 階段大幅縮短，CPU 項佔比上升）
- ⚠️ Phase 1 的 P1（gc 降頻）刻意保留 CUDA 行為 → **在 5090 上沒有效果**；P2/P3 跨平台有效

### Phase 1 後非 LLM 熱點（M5 Pro，3.33–3.42s/page，cProfile）
| 項目 | s/page | 類型 | 5090 上預期 |
|---|---|---|---|
| inpainting LaMa 推論 | 1.29 | GPU | 大幅縮短 |
| OCR 48px beam search | 0.74 | 混合：逐 token 迴圈 + `nonzero()` 每步同步 + Python | 縮短有限（launch/sync-bound） |
| CUDA 上每頁 gc.collect ×~6 | (~0.43，MPS 已移除) | CPU | **不變，P1 未套用 CUDA** |
| bilateralFilter d=17（偵測 0.21 + mask 0.10） | 0.31 | CPU (OpenCV) | 不變 |
| sort_regions（panel 偵測 LSD，且每頁 IndexError） | 0.22 | CPU | 不變 |
| mask_refinement 其餘 | ~0.12 | CPU | 不變 |
| DBNet forward | 0.15 | GPU | 縮短 |
| rendering | 0.09 | CPU | 不變 |
| MangaLens / YOLO | 0.07 | GPU | 縮短 |
- 注意：5090 上的實際比例未量測，以上「預期」為依類型推論；需在 Windows 跑 bench 確認

### 目標再調整（2026-11-13 10:50）
- 使用者目前無 Windows 可跑；**在 Mac 上完成 workflow 最佳化與實測**，最後產出交接報告給 Windows agent（CUDA 需驗證項目標註）

### Phase 2 進度
- [x] **T1 P1 統一至 CUDA**：所有裝置 gc.collect 只在批次邊界；CUDA 每頁仍保留 `empty_cache`（`_cleanup_gpu_memory(gc_collect=False)`）
  - ⚠️ Windows agent 需驗證：長批次 VRAM 不持續上升；若上升，`_run_inpainting` 改 `gc_collect=True`
- [x] **T2 panel 偵測 IndexError**：根因 = OpenCV 5.0 的 `LineSegmentDetector.detect` 輸出由 (N,1,4) 改 (N,4)；requirements 只寫 `>=4.10` → Mac 裝到 5.0.0
  - 修正：`utils/panel/lib/page.py` `dlines[0].reshape(-1, 4)`（4.x / 5.x 相容）
  - 修好後 panel 偵測實際成本 0.37s/page（純 CPU）→ Windows 若為 OpenCV 4.x，**目前每頁就在付這個成本**
  - 優化（輸出完全一致）：`segment.py union_all` 的 `used` list→set（同 __eq__ 語意）；`panel/__init__.py` 3 通道 uint8 直接傳 array，免暫存 PNG 往返 → **0.37 → 0.27s**
  - 評估未採用：先縮圖再偵測（1280 邊：0.21s，2 頁結果一致；1024 邊漏 panel）→ 安全邊際不足，列為 Windows agent 可選實驗
  - 新基準 base2：OCR 文字集合相同、輸出圖 byte-identical，僅文字順序改為 panel 排序（預期行為）；wall 3.33s
- [x] **T3 bilateralFilter**
  - mask_refinement：改為逐區塊濾波（區塊 + d//2 邊界），305 個矩形含四邊 **bit-identical**；整體輸出與 base2 一致 → mask_refinement **0.21 → 0.13s**（已採用）
  - 偵測 d=17→9（未改預設，待確認）：detection 0.54→0.34s、wall 3.38→3.06s；OCR 僅少了誤判的 `￡ェ`（擬聲詞「ガチャッ」）
    - 目視對照 `ab_bilateral_d9.jpg`：泡泡內一致；d=17 把擬聲詞誤判為文字並抹除改寫成亂碼，d=9 保留原圖 → d=9 在此樣本較佳
    - 風險：僅 2 頁，可能在其他頁漏掉真文字 → Windows agent 以更多頁驗證
- [x] **T4 inpainting 裁切策略**（新 config `inpainter.inpainting_crop_pad`，預設 0 = 原行為）
  - 實作：`inpainting/__init__.py` `_dispatch_with_crops` + `_mask_crop_boxes`；只寫回 mask 像素
  - 第一版「重疊即合併成外接矩形」產生 1342×824 大框 → pad64 僅 1.26→1.05s、pad128 反而 1.64s
  - 改為「合併後面積 ≤ 兩框面積和才合併」：成本 ≈ 面積線性（單次呼叫 overhead ~0.03s）
  - **結果：pad64 inpainting 1.26→0.73s（−42%，wall 2.79s）；pad32 → 0.52s（−59%，wall 2.51s）**
  - 目視 `ab_inpaint_crop.jpg`（差異最大的 8 區）：pad64 ≈ 整頁；pad32 部分振假名區殘影略多；全頁平均像素差 0.2–0.33/255
  - 建議：pad64 為預設候選；Windows agent 以更多頁目視確認
- [x] **T5 OCR**：`model_48px.py` 逐 i membership 改 set（輸出一致，增益 ~0.01s）；beam 5→3→2：OCR 0.71→0.67→0.64s，文字一致 → 增益小，判定為逐步同步 / launch bound，不改
- [x] **T6 交接報告**：`doc/perf/optimization-report.md`（含 A/B 圖 `doc/perf/*.jpg`）
- **最終**：預設設定 3.24s（−21%）；+ pad64 + d=9 → **2.49s（−40%）**

### Phase 2（修訂）— 依新數據重排
- [ ] ~~P6~~（LLM 相關，暫緩）開 `batch_concurrent`（既有四執行緒流水線）讓 LLM 與影像階段重疊**
  - 理想 wall ≈ max(LLM 4.1, 影像 3.3) ≈ 4.1–4.5s/page（約 −40%）
  - 風險：llama.cpp Metal 與 torch MPS 共用 GPU 互搶；先實測再判斷；輸出應與序列模式一致（需比對）
- [ ] **P4b. inpainting 改裁切策略**：目前整頁縮到 1408×2048 全圖推論再只貼回 mask
  - 實測 mask 只佔頁面 11–13%；連通區塊 +64px 邊界的裁切區佔 37–41%（+32px：25–28%，+128px：57–62%）
  - 預估 inpainting 1.45 → ~0.6–0.9s；且裁切區可用原解析度，不必縮圖
  - 風險：LaMa 依賴全域上下文（FFC），邊界太小可能紋理不連續 → 目視 A/B（pad 64 / 128）
- [ ] P4. inpainting_size 2048 → 1536（若 P4b 效果不足才考慮）
- [ ] P5. bilateralFilter d 17 → 9（−6%，需比對偵測框）
- Pass：使用者目視接受；P6 需輸出一致

### 不做
- LaMa fp16（模型崩壞）/ bf16（MPS 模擬變慢）；YOLO OBB（僅 0.4%）
- panel detection IndexError：屬功能 bug，另案處理（修好不會變快）

## 進度紀錄
- 2026-11-14 Windows 接手盤點完成：目前 `debug/perf-optimization@79aa0cb`；正式環境 Python3.12.13／torch2.8.0+cu128／CUDA12.8 可用／OpenCV4.13.0／threads16。未執行本分支效能測試，未修改production。統整計畫：`.omx/plans/windows-perf-validation-20261114.md`。先驗 panel／ROI／OCR／hash 等價與收益，再單獨加入廣泛GC；6頁預篩通過才擴樣與一次長跑。Windows stash `c71b44ab` 保留；RAM95／CLI overrides／profiling與啟動器需選擇性整合，不能整包套回。Mac21%／40%不是Windows端到端收益。
- 2026-11-13 10:20 建立計畫，開始 Phase 1
- 2026-11-13 10:35 Phase 1 完成並驗證（−19%，輸出一致）；未 commit，`git diff` 4 檔 +26/−18
- 規則：途中遇到需安裝的依賴，先停下回報使用者，不先跑完
- 2026-11-13 11:20 Phase 2 完成，交接報告寫入 doc/perf/
- 2026-11-13 10:55 範圍改為非 LLM；重新 profile，熱點依 GPU/CPU 分類
- 2026-11-13 10:45 Phase 3 完成：LLM 佔 54%；Phase 2 重排（P6 並行、P4b 裁切優先）

### 2026-11-17 最終 commit 收斂
最終採用 Bcpu，C1 已撤回原 GC 策略；12頁非 LLM 配對減時18.5%。含 C1 的30.7%及正式單輪8.8%不作最終收益承諾。commit 前99 tests及62 subtests通過（5.75秒）。crop=0、d=17、測試 FP32維持；不再長跑。分項 commits推送 feat/windows-pipeline-performance 並開至 main 的 PR。本機啟動器、圖片、logs、worktree與.omx資料不納入。
