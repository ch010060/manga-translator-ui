# 交接：非 LLM 管線效能優化（給 Windows agent 接手）

> **Windows 最終採用結論（2026-11-17）**：保留 CPU 優化，撤回 C1 廣泛 GC 降頻，維持既有清理策略。固定12頁配對非LLM耗時下降 **18.5%**，consumer圖片／OCR／排序／精修mask完全一致。下方 Mac 數據及 W0–W6 為原始交接紀錄；後續以 `doc/perf/windows-validation-results.md` 的最終決策為準，**不再安排長跑**。
>
> crop仍0、偵測bilateral仍17、實際測試設定FP32。含C1的30.7%與正式15頁8.8%不屬於最終採用組的收益承諾。短批次含Sakura波動大，不宣稱整書加速比例。

## 原始 Mac 交接紀錄

> 分支：`debug/perf-optimization`（base：`main` @ `86fe508`）
> 前一位 agent 在 Mac（M5 Pro / MPS）完成實作與實測；**你的任務是在 Windows RTX 5090（CUDA）上驗證、補完並收斂**。
> 背景與完整數據：`doc/perf/optimization-report.md`；逐步過程紀錄：`scratchpad.md`。

## TL;DR

- 目標：縮短 **LLM 以外**的每頁處理時間（偵測、OCR、文字合併、mask、inpainting、渲染）。LLM 不在範圍內。
- Mac 實測（不含 LLM）：原始 4.12s/頁 → 本分支預設設定 **3.24s（−21%）** → 加兩項建議設定 **2.49s（−40%）**。
- 本節記錄 Mac 交接當時的結果；後續 CUDA 驗證與採用範圍見 Windows 報告。
- 兩項建議設定（inpainting 裁切、偵測濾波 d=9）有畫質取捨，**預設未開啟**，需使用者以更多頁目視決定。

## 工作規則（使用者明確要求，務必遵守）

1. **腳本自己跑、自己評估，最後回報結果**；不要把命令丟給使用者執行。
2. **遇到需要安裝的依賴或環境問題，立刻停下回報**，不要先跑很久最後才說有問題。
3. **git commit**：author / committer 一律 `ch010060 <ch010060@gmail.com>`；**不加任何 agent trailer**（不要 `Co-Authored-By`、不要 session 連結）。
   ```powershell
   git config user.name ch010060
   git config user.email ch010060@gmail.com
   ```
4. 未經使用者目視同意，**不要把有畫質取捨的設定改成預設**。
5. 不要把 PoC 當 production；每項修改都要有 pass/fail 依據與 rollback 方式。
6. 回報用繁體中文；先講結論與數字，再講細節。

## 目前狀態

| 項目 | 狀態 | CUDA 驗證 |
|---|---|---|
| C1 `gc.collect` 只在批次邊界 | **已撤回，保留原策略** | 未做長跑，不採用 |
| C2 `get_image_md5` 改 hash 原始像素 | 已實作 | 低風險 |
| C3 OCR XPOS 位置編碼 memoize | 已實作 | 低風險（需確認輸出一致） |
| C4 panel 偵測 OpenCV 5.x 相容 | 已實作 | ❌ 待確認 OpenCV 版本 |
| C5 panel 偵測加速（set / 免暫存 PNG） | 已實作 | 低風險 |
| C6 mask refinement 逐區塊濾波 | 已實作 | 低風險 |
| C7 inpainting 裁切策略（設定，預設關） | 已實作 | ❌ 待測速度與畫質 |
| C8 OCR beam search 減少同步 | 已實作 | 低風險 |
| S1 `inpainting_crop_pad: 64` 設為預設 | **待決定** | ❌ |
| S2 偵測 bilateral `d=17→9` | **待決定**（未改程式） | ❌ |

修改檔案一覽：見 `doc/perf/optimization-report.md`「已套用的程式修改」表；`git diff main...debug/perf-optimization --stat` 可列出。

## 任務清單（依序執行；可合併的已合併）

### W0. 同步分支與確認環境
```powershell
cd <repo>
git fetch origin
git switch debug/perf-optimization
git log -1 --format="%h %an <%ae> %s"     # 應為 01b07b7 perf: cut non-LLM pipeline time ...

# Python 環境（依安裝方式擇一）
conda activate manga-env                   # 命名環境
# 或：$py = ".\conda_env\python.exe"        # 路徑環境（以下 python 改用 & $py）
python -c "import torch,cv2;print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), cv2.__version__)"
```
- Pass：CUDA 可用；記錄 torch / CUDA / OpenCV 版本到回報。
- **OpenCV 版本很重要**：4.x 代表 panel 偵測在 main 上本來就有運作（每頁約 0.37s CPU 成本）；5.x 代表 main 上它每頁失敗。
- 若環境缺套件 → 停下回報（規則 2）。

### W1. 基準對照：main vs 本分支（速度 + 輸出一致性）
用 worktree 取得 main，兩邊用**同一個 Python 環境**、同樣樣本跑。
```powershell
git worktree add ..\mt-main main
Copy-Item scripts\bench_stages.py ..\mt-main\scripts\    # scripts/* 被 gitignore，main 沒有這支

$imgs = "doc\images\0012.png","doc\images\0012.png","doc\images\110012.png"
# 若使用者提供真實章節資料夾，改用 10–20 頁實際漫畫（較具代表性）

Push-Location ..\mt-main; python scripts\bench_stages.py @imgs --repeat 3 --dump ..\perf_main.json  | Tee-Object ..\perf_main.log; Pop-Location
python scripts\bench_stages.py @imgs --repeat 3 --dump ..\perf_branch.json | Tee-Object ..\perf_branch.log
```
比對：
```powershell
python -c "import json;a=json.load(open('../perf_main.json',encoding='utf-8'));b=json.load(open('../perf_branch.json',encoding='utf-8'));print('text set same:',all(sorted(x['texts'])==sorted(y['texts']) for x,y in zip(a,b)));print('order same:',all(x['texts']==y['texts'] for x,y in zip(a,b)));print('img same:',all(x['img_md5']==y['img_md5'] for x,y in zip(a,b)))"
```
- Pass：
  - 本分支 wall/page ≤ main
  - OCR 文字集合相同、渲染圖 md5 相同
  - OpenCV 4.x 時「順序」也應相同；OpenCV 5.x 時順序不同屬預期（C4 修好 panel 排序）
- Fail：用 `git diff main -- <file>` 逐檔 revert 定位（`git checkout main -- <file>`），找出造成差異的修改後回報。
- 記錄：兩邊每個 stage 的 s/page；特別看 **CPU 項**（textline_merge、mask_refinement、未歸屬 overhead）在 5090 上的佔比。

### W2. C1 的 VRAM 長批次驗證（CUDA 專屬風險）
C1 把每頁的 `gc.collect()` 移到批次邊界；CUDA 每頁仍做 `torch.cuda.empty_cache()`。
- 用 GUI 或 `scripts/headless_batch_by_first_level_dirs.py` 跑 **≥200 頁**長批次，另開視窗記錄顯存：
  ```powershell
  nvidia-smi --query-gpu=timestamp,memory.used --format=csv -l 5 > ..\vram.csv
  ```
- Pass：顯存在批次之間回到平台期，不持續上升；沒有 OOM。
- Fail → `manga_translator/manga_translator.py` 的 `_run_inpainting` 內：
  `self._cleanup_gpu_memory(aggressive=True, gc_collect=False)` 改成 `gc_collect=True`，重測並回報。

### W3. S1 inpainting 裁切：速度 + 畫質
設定位置：使用者實際設定檔 `examples/config.json`（GUI 讀寫的檔案）的 `inpainter` 區塊；bench 用複本，不要直接改使用者的檔。
```powershell
python -c "import json;c=json.load(open('examples/config-example.json',encoding='utf-8'));c['inpainter']['inpainting_crop_pad']=64;json.dump(c,open('../cfg_crop64.json','w',encoding='utf-8'),ensure_ascii=False)"
python scripts\bench_stages.py ..\cfg_crop64.json @imgs --repeat 3 --save ..\img_crop64 | Tee-Object ..\perf_crop64.log
python scripts\bench_stages.py @imgs --repeat 1 --save ..\img_full
```
- 速度：記錄 inpainting s/page（Mac：1.27 → 0.73）。CUDA 上單次推論 overhead 應更低，預期效益相近或更好，需實測。
- 畫質：用 20–30 頁不同類型（大面積文字、網點背景、擬聲詞、振假名多）做 `img_full` vs `img_crop64` 對照圖給使用者看（可參考 `doc/perf/ab_inpaint_crop.jpg` 的作法：取差異最大區塊並排）。
- 也測 `inpainting_crop_pad: 32`（Mac：0.52s，振假名區殘影略多）供使用者選。
- 使用者同意後才改預設；兩種改法擇一：
  1. 只改 `examples/config-example.json` / 使用者 `config.json`（最小變更），或
  2. 改 `manga_translator/config.py` `InpainterConfig.inpainting_crop_pad` 預設值
- **待補**：GUI 是否顯示 / 保存這個新欄位未驗證。確認 GUI 存檔後 `inpainting_crop_pad` 不會被丟掉；若要在 GUI 顯示，需在 `desktop_qt_ui/locales/*.json` 補 `label_inpainting_crop_pad`、`desc_inpainter_inpainting_crop_pad`。

### W4. S2 偵測 bilateralFilter d=17→9：速度 + 偵測品質
目前未改程式。測試方式：暫時修改 `manga_translator/detection/default.py`（約第 80 行）：
```python
cv2.bilateralFilter(image, 17, 80, 80)   # →  cv2.bilateralFilter(image, 9, 80, 80)
```
跑 bench（`--dump` + `--save`），與 d=17 比較。
- 速度：Mac detection 0.54 → 0.34s（濾波為 CPU，5090 上效益應相近）。
- 品質：比對每頁 OCR 文字是否**漏掉真正的文字**（小字、淡色字、網點上的字）。Mac 樣本中 d=9 唯一的差異是不再把擬聲詞「ガチャッ」誤判成文字（`doc/perf/ab_bilateral_d9.jpg`，d=9 較好），但只有 2 頁。
- 使用者同意後才採用；採用時建議改成設定欄位而不是寫死常數（例如 `DetectorConfig` 新增欄位，預設維持 17 或改 9 依使用者決定）。
- 未採用 → `git checkout -- manga_translator/detection/default.py` 還原。

### W5. OpenCV 版本固定（建議，需使用者決定）
根因：`requirements_*.txt` 只寫 `opencv-contrib-python>=4.10.0`，新環境會裝到 5.x，而 5.x 改了 `LineSegmentDetector.detect` 輸出形狀。C4 已修 panel 偵測，但專案其他 `cv2` 呼叫未全面檢查 5.x 相容性。
- 選項 A：requirements 改 `opencv-contrib-python>=4.10.0,<5`
- 選項 B：維持 `>=4.10`，在 5.x 環境跑一輪完整功能測試
- 先回報使用者目前 Windows 的版本與建議，不要自行修改 requirements。

### W6（選做，W1–W4 完成後）CUDA 專屬的下一輪候選
依 W1 量到的 5090 profile 決定是否值得做：
- inpainting 精度：CUDA 上 bf16（`inpainting_precision`）速度與畫質。**不要用 fp16**（LaMa 的 FFC 中間值過大，fp16 會崩壞）。
- OCR：Mac 上判定為逐步同步 / launch bound（beam 5→2 只快 0.07s），CUDA 上可用 `torch.profiler` 確認是否相同。
- panel 偵測先縮圖（1280 長邊在 2 頁樣本結果一致、0.37→0.21s；1024 長邊會漏 panel）：用更多頁驗證安全邊際。
- 若 W1 顯示某個 CPU 項在 5090 上佔比最高，優先處理它。

## 回報格式（每完成一個 W 回報一次）

```
W<n> 結果：PASS / FAIL / 待使用者決定
- 數據：main vs branch（s/page，各 stage）
- 輸出一致性：文字集合 / 順序 / 圖 md5
- 異常：（有就列，附 log 路徑）
- 下一步：
```

## 工具與檔案地圖

| 用途 | 路徑 |
|---|---|
| 逐 stage 計時、輸出比對、存圖 | `scripts/bench_stages.py`（`--repeat N` `--dump x.json` `--save DIR` `--translator X`；第一個參數可為 config json） |
| 從 GUI log 算 stage 佔比 | `scripts/stage_timing.py result/log_*.txt` |
| 完整數據與證據 | `doc/perf/optimization-report.md` |
| A/B 對照圖 | `doc/perf/ab_inpaint_crop.jpg`、`doc/perf/ab_bilateral_d9.jpg` |
| 過程紀錄 | `scratchpad.md`（接手後請繼續在最下方「進度紀錄」追加） |
| inpainting 裁切實作 | `manga_translator/inpainting/__init__.py`：`_mask_crop_boxes`、`_dispatch_with_crops` |
| gc 策略 | `manga_translator/manga_translator.py`：`_cleanup_gpu_memory(aggressive, gc_collect)` |

## Rollback

- 整個分支不用：`git switch main`
- 單一修改：`git checkout main -- <file>`（各修改對應檔案見報告）
- C7 裁切設定預設為 0，不 revert 也不影響行為
- 清掉 W1 的 worktree：`git worktree remove ..\mt-main`
