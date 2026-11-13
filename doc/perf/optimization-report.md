# manga-translator-ui 非 LLM 管線效能優化報告（交接給 Windows agent）

> 產出：2026-11-13，於 Mac（M5 Pro / MPS）實測。目標平台：Windows RTX 5090（CUDA）。
> 範圍：LLM 翻譯以外的所有階段（偵測、OCR、文字合併、mask、inpainting、渲染）。
> 進度原始紀錄：repo 根目錄 `scratchpad.md`。

## TL;DR

| 狀態 | 每頁時間（M5 Pro, 不含 LLM） | 相對基準 |
|---|---|---|
| 原始基準 | 4.12s | — |
| 套用本次程式修改（預設設定不變） | **3.24s** | **−21%**，輸出一致¹ |
| + 建議設定（inpainting 裁切 pad64 + 偵測 bilateral d=9） | **2.49s** | **−40%**，有畫質取捨，需目視確認 |

¹ 唯一差異：panel 偵測原本在 OpenCV 5.x 上每頁失敗，修好後文字閱讀順序改為依分格排序（正確行為）；渲染圖 byte-identical。

**Windows agent 待辦**：同步程式 → 跑基準 → 驗證 CUDA 專屬風險（VRAM、panel 成本）→ 以更多頁目視確認兩項建議設定 → 決定是否把建議設定改為預設。

## 為什麼重要（GPU 越快，CPU 項越關鍵）

5090 上 GPU 推論（LaMa、DBNet、YOLO、MangaLens）會大幅縮短，但 CPU 端的處理（垃圾回收、OpenCV 濾波、panel 偵測、Python 迴圈）不會。本次修改大多在 CPU 端，**在 5090 上的相對效益預期比 Mac 更大**（推論，需實測）。

## 量測方法

- 腳本：`scripts/bench_stages.py`（逐 stage 計時，每次計時前後做 `torch.cuda/mps.synchronize()`；`translator=original` 隔離 LLM）
  - ⚠️ `scripts/*` 在 `.gitignore` 內，需 `git add -f scripts/bench_stages.py` 或手動複製
- 樣本：`doc/images/0012.png`、`doc/images/110012.png`（1440×2137），repeat 3 → 9 頁，第一張 warm-up（含模型載入）不計
- 正確性：`--dump out.json` 輸出每頁 OCR 文字 + 渲染圖 md5，與基準比對；`--save DIR` 存渲染圖供目視
- 基準跑兩次 dump 完全一致 → 管線在固定裝置上是確定性的，可用 byte 比對驗證

```powershell
# Windows（專案根目錄，啟用專案 Python 環境）
python scripts\bench_stages.py doc\images\0012.png doc\images\0012.png doc\images\110012.png --repeat 3 --dump base.json
```

## 基準 profile（M5 Pro，修改前）

| stage | s/page | 佔比 | 主要成本 |
|---|---|---|---|
| inpainting | 1.41 | 34% | LaMa 整頁推論（GPU） |
| ocr | 0.95 | 23% | 48px beam search 逐 token 迴圈 |
| detection | 0.55 | 13% | DBNet 0.46（其中 bilateralFilter 0.24 為 CPU）、MangaLens 0.05、YOLO OBB 0.02 |
| mask_refinement | 0.22 | 5% | 整頁 bilateralFilter |
| textline_merge | 0.22 | 5% | panel 偵測（每頁 IndexError 後放棄） |
| text_rendering | 0.13 | 3% | 含每頁 gc.collect |
| 未歸屬 overhead | ~0.6 | ~15% | gc.collect ×~6/頁、get_image_md5 PNG 編碼 ×3/頁 |

> 使用者原先假設「YOLO / OCR 佔最大」：實測 YOLO OBB 只佔 0.4%；最大項為 inpainting 與非模型 overhead。

## 已套用的程式修改（11 檔，+119/−39，未 commit）

| # | 修改 | 檔案 | 效益（M5 Pro） | 輸出 | CUDA 需驗證 |
|---|---|---|---|---|---|
| C1 | `gc.collect` 只在批次邊界執行（所有裝置）；CUDA 每頁仍保留 `empty_cache` | `manga_translator.py`（`_cleanup_gpu_memory` 新增 `gc_collect` 參數、`_run_inpainting`、`_run_text_rendering`）、`inpainting/common.py` | ~−0.36s（profile 推估） | 一致 | **是**：長批次 VRAM |
| C2 | `get_image_md5` 改 hash `size + tobytes()`，免整頁 PNG 編碼 | `utils/generic.py` | ~−0.25s（profile 推估） | 一致（hash 值改變，僅用於同次執行內 key / debug 資料夾名，已確認無跨次依賴） | 否 |
| C3 | OCR XPOS 旋轉位置編碼表 memoize（每 decode step 原本 CPU 重算再搬 GPU） | `ocr/xpos_relative_position.py` | OCR −0.20s | 一致（CPU/MPS 單元測試 `torch.equal`） | 否 |
| C4 | panel 偵測 OpenCV 5.x 相容：`dlines[0].reshape(-1, 4)` | `utils/panel/lib/page.py` | 功能修復 | 閱讀順序改為 panel 排序 | **是**：見下 |
| C5 | panel 偵測加速：`union_all` 的 `used` list→set（同 `__eq__` 語意）；3 通道 uint8 直接傳 array 免暫存 PNG 往返 | `utils/panel/lib/segment.py`、`utils/panel/__init__.py`、`lib/page.py` | 0.37→0.27s | panel 結果完全一致 | 否 |
| C6 | mask refinement bilateralFilter 改逐區塊（區塊 + d//2 邊界） | `mask_refinement/text_mask_utils.py`（`_bilateral_region`） | 0.21→0.13s | bit-identical（305 矩形含四邊驗證） | 否 |
| C7 | inpainting 裁切策略（新設定，**預設關閉**） | `config.py`（`inpainter.inpainting_crop_pad`）、`inpainting/__init__.py`（`_dispatch_with_crops`、`_mask_crop_boxes`） | 見建議設定 S1 | 預設不變 | 否 |
| C8 | OCR beam search 移除逐 i 的 tensor membership 同步 | `ocr/model_48px.py` | ~−0.01s | 一致 | 否 |

### C4 重點：panel 偵測在 Windows 可能一直都在付成本
- 根因：`requirements_*.txt` 只寫 `opencv-contrib-python>=4.10.0`；OpenCV 5.0 的 `LineSegmentDetector.detect` 回傳形狀由 `(N,1,4)` 改為 `(N,4)`
- Mac 新裝環境拿到 5.0.0 → 每頁 IndexError → 退回簡單排序（等於這功能從沒運作）
- **Windows 若是 OpenCV 4.x，panel 偵測一直有成功，每頁成本約 0.37s（純 CPU，修改前）**；C5 後約 0.27s
- 驗證：`python -c "import cv2; print(cv2.__version__)"`

## 建議設定（有畫質取捨，未改預設）

| # | 設定 | 效益（M5 Pro） | 證據 | 風險 |
|---|---|---|---|---|
| S1 | `inpainter.inpainting_crop_pad: 64` | inpainting 1.27→0.67–0.73s（−42%） | `doc/perf/ab_inpaint_crop.jpg`：差異最大 8 區，pad64 ≈ 整頁；全頁平均像素差 0.2–0.33/255 | LaMa 依賴全域上下文，大面積遮罩可能紋理不連續 |
| S1' | `inpainting_crop_pad: 32` | →0.52s（−59%） | 同上；振假名區殘影略多 | 較 S1 高 |
| S2 | 偵測 bilateralFilter `d=17→9`（`detection/default.py` 第 80 行附近 `cv2.bilateralFilter(image, 17, 80, 80)` → `9`） | detection 0.54→0.34s | `doc/perf/ab_bilateral_d9.jpg`：泡泡內一致；d=17 把擬聲詞「ガチャッ」誤判為文字並抹除改寫成亂碼，d=9 保留原圖 | 只有 2 頁樣本，可能在其他頁漏掉淡色或小字 |

S1 裁切策略要點：
- 原實作把整頁縮到 ≤2048 送 LaMa，再只貼回 mask 像素；mask 實際只佔頁面 11–13%
- 裁切框 = mask 連通區塊外擴 pad；**重疊框只有在合併後面積 ≤ 兩框面積和時才合併**（第一版直接合併外接矩形會產生 1342×824 大框，反而變慢：pad128 1.64s）
- 成本 ≈ 面積線性，單次推論 overhead ~0.03s（MPS）；CUDA 上 per-call overhead 應更低（未驗證）

## 評估後不採用

| 項目 | 原因 |
|---|---|
| 優化 YOLO OBB | 只佔 0.02s/頁（0.4%） |
| LaMa fp16 | 社群移植經驗：LaMa 的 FFC 中間值過大，fp16 會崩壞 |
| LaMa bf16 | PyTorch MPS 上 bf16 走模擬路徑，有變慢案例；CUDA 上可另測 |
| OCR beam 5→2 | OCR 0.71→0.64s，增益小；OCR 是逐步同步 / launch bound，不是算力 bound |
| panel 偵測先縮圖 | 1280 邊：0.21s 且 2 頁結果一致；1024 邊漏 panel → 安全邊際不足（可在 Windows 用更多頁再評估） |
| `batch_concurrent` 讓 LLM 與影像並行 | 屬 LLM 範疇，本次不處理 |

## Windows agent 驗證清單

- [ ] 1. 同步 11 個修改檔 + `scripts/bench_stages.py`（被 gitignore，需 `git add -f` 或手動複製）
- [ ] 2. 記錄環境：`python -c "import torch,cv2;print(torch.__version__,torch.version.cuda,cv2.__version__)"`
- [ ] 3. 修改前後各跑一次 bench（`git stash` 取得修改前），記錄 wall/page 與各 stage
  - Pass：修改後 wall/page 不高於修改前；`--dump` 的 OCR 文字集合一致、渲染圖 md5 一致（若 OpenCV 4.x，順序也應一致）
- [ ] 4. **C1 VRAM 驗證**：用 GUI 或 headless 跑 ≥200 頁長批次，觀察 `nvidia-smi` 顯存
  - Pass：顯存在批次間回到平台期，不持續上升
  - Fail → `_run_inpainting` 的 `_cleanup_gpu_memory(aggressive=True, gc_collect=False)` 改為 `gc_collect=True`
- [ ] 5. **S1 / S2 目視驗證**：挑 20–30 頁不同風格（擬聲詞多、網點多、大面積文字）
  - S1：設定 `"inpainter": {..., "inpainting_crop_pad": 64}`，`--save` 輸出與 pad 0 對照
  - S2：改 `detection/default.py` 的 d=9，比對 `--dump` 的 OCR 文字是否漏字
  - 決定是否改為預設
- [ ] 6. 建議另開 issue：`requirements_*.txt` 固定 OpenCV 主版本（例如 `<5`），或確認所有 `cv2` 呼叫與 5.x 相容

## Rollback

- 全部：`git checkout -- manga_translator/`
- 單項：依上表檔案個別 `git checkout -- <file>`（C7 的設定預設關閉，不 revert 也不影響行為）

## 量測原始數據（M5 Pro，wall/page）

| 階段 | 數值 |
|---|---|
| 基準（兩次） | 4.12s / 4.18s |
| C1(MPS)+C2+C3（三項合計實測 −0.79s） | 3.33s |
| + C4/C5（panel 實際運作） | 3.33s |
| + C6 | 3.38s（mask 0.21→0.13；量測雜訊 ±0.05） |
| + C8，最終預設 | 3.24–3.27s |
| 最終 + S1(pad64) + S2(d=9) | 2.49s |
| 參考：S1 pad32 + d=17 | 2.51s |
