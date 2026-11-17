# Windows CUDA 驗證進度

> **最終採用：CPU組（Bcpu）**。C1廣泛GC改動已撤回；受控非LLM12頁配對減時18.5%，consumer輸出逐位元相同。含C1的30.7%／8.8%僅保留為歷史實驗，不是目前採用組保證。使用者要求快速收斂並禁止長跑，後續不再安排202頁測試。

日期：2026-11-16。分支基準：`debug/perf-optimization@79aa0cb`；對照：`main@86fe508`。

依 `.omx/plans/windows-perf-validation-20261114.md` 執行。所有模型與來源媒體保留，沒有安裝依賴、更新 llama 或套回整包 stash。

## P0 / P1 已確認

- 隔離 worktree：`.omx/worktrees/perf-b0`、`perf-bcpu`、`perf-ball`。Bcpu 完整撤回 C1 兩個檔案，其他 CPU 優化保留；共用模型目錄，沒有複製／下載模型。
- 選擇性整合 Windows CLI overrides、RAM95、profiling 與啟動器；未整合窄GC與lazy distance，未覆蓋Mac scratchpad。
- 原版／候選共同使用相同 runner／設定／fixture。12頁fixture來自已封存兩部作品，首6頁作預篩；SHA256已核對，沒有直接修改來源圖片。
- 使用者目前已有 Sakura 在8080服務、health=ok，GPU基線約15842/32607MiB；本次不操作該服務，測試皆保持同樣常駐狀態。

### P1 發現與修正

1. **ROI 濾波 dtype 邊界**：OpenCV4.13、停用IPP時float32 ROI與fullfilter出現精確像素差異。候選對非uint8保留fullfilter fallback；標準uint8優化不變。原red的3個ROI契約修正後通過。
2. **XPOS 快取失效**：原分支half→float後會重用量化前的舊table，最大誤差6.914e-06；CUDA→CPU仍保留4480B舊CUDAtable。XPOS `_apply` 清cache後，誤差0、舊CUDAtable0B。沒有對固定FP32路徑增加額外逐token操作。

### 已執行的契約測試

| 範圍 | 結果 |
|---|---|
| panel／ROI／真CRF／橫直rearrange | 7 tests、41 subtests passed；真CRF24次 |
| hash／XPOS CUDA／dtype-device生命週期 | 44 passed、無skip |
| crop0／crop mask寫回／空mask／split優先／RGBA／設定roundtrip | 9 passed，無模型載入 |
| Windows CLI／RAM95／profiling | 26 tests、9 subtests passed |

以上不等同整批性能改善或長跑驗收。XPOS新key仍會增加cache，尚未取得長跑cache上限證據。ROI非uint8 fallback優先保正確性，沒有宣稱它更快。

## 原計畫未完成項（已停止，不自動續跑）

- P0 benchmark runner fail-closed／warm-up隔離／stage摘要補強與測試。
- P2 baseline A/A、6頁B0/Bcpu預篩與條件式12頁配對，C1另測。
- P3正式subprocess／Sakura／資源長跑，只有P2通過才執行。
- P4 crop64／d9品質實驗；預設仍crop0、d17。

證據在 `.omx/tmp/panel-mask-*`、`.omx/tmp/ocr-hash-*` 及 `.omx/benchmarks/windows-perf-20261116/`。舊Windows stash `c71b44ab` 與 `.git/local-test-state/windows-pipeline-20261113T155735Z/` 仍保留。

重新執行契約測試需正式Python環境與包含`86fe508`的Git歷史（panel/mask oracle對照）；不需要RTK作為Python測試依賴。測試圖片、逐頁profile與本機worktree未納入版本控制。`scripts/bench_stages.py`保留可重現的公開stage測量入口，warm-up另列、失敗輸出拒絕、`--no-sync`不新增GPU同步；其數據仍不取代正式GUI/CLI整批wall。

## P2：CPU 組已通過配對收益門檻

共同runner補強後9 tests、9 subtests通過。正式fixture固定為兩部作品的12頁，所有run使用相同FP32/2048／48px／crop0／d17與原GC策略，Sakura常駐但translator=original隔離LLM。沒有改CUDA determinism、sampling或品質閾值。

| Run | 12頁pipeline wall 秒 | 含啟動/暖機外層秒 |
|---|---:|---:|
| B0 A1 | 51.326 | 63.849 |
| Bcpu B1 | 41.806 | 53.487 |
| Bcpu B2 | 41.211 | 53.425 |
| B0 A2 | 50.530 | 61.705 |

平均pipeline耗時下降 **18.497%**，對應吞吐增加22.694%，超過原版波動；這是非LLM、單worker、指定fixture的結果，沒有宣稱整書端到端相同百分比。Pipeline wall含JSON stage摘要成本，排除最後output hash/save與讀圖；正式headless另驗。

四輪的所有detector/OCR/合併/panel文字、座標、順序、歸屬、精修mask與最終RGBA像素hash完全相同。主要節省在mask refinement（約5.90→2.57秒）、panel（約6.67→5.42秒）、OCR（約10.37→9.39秒）及未歸屬的hash等成本；inclusive stages不可直接相加。

### 原版 A/A 的 raw map 差異

原版本身的mask_raw hash不穩定，已額外兩輪保存raw masks定量：最高64/6525250像素不同，uint8最大絕對差5；同時所有上述consumer輸出完全相同。數據位於`raw-baseline-differences.json`。

獨立審查接受在12頁配對前固定gate：mask_raw保留presence/shape/dtype及差異診斷，不要求其hash相同；所有consumer結果仍逐位元相同，任何consumer差異皆停止採用。沒有將觀察到的5訂成所有漫畫通用容差，也沒有反覆重跑直到hash碰巧相同。

廣泛GC仍在單獨6頁預篩，P3正式流程與長跑尚未完成。P4品質設定尚未採用。

## P2：GC 獨立對照完成

12頁 Bcpu／Ball／Ball／Bcpu 的pipeline秒數為 **41.211／35.299／35.256／40.902**。GC額外減時14.076%；完整組相對原版兩輪平均減時 **30.731%**。全部consumer輸出逐位元相同，來源／設定／指定程式碼前後雜湊一致。

這只通過指定普通流程的短跑收益gate，GC資源安全仍待P3長跑，未宣稱HQ／replace／concurrent等所有模式都已驗證。沒有更改inpaint精度或開crop／d9。

新增OCR cache snapshot只讀現有48px model中的XPOS metadata，不初始化模型、不保存strong references、不作GPUcopy/sync。加入後P0/P1/runner契約合併重跑：**98 tests、61 subtests passed**，僅既有dependency warnings。

P3已開始12頁正式headless/subprocess/Sakura原版對照；所有測試工作均有獨立owned WindowsJob，既有使用者Sakura服務保持原狀。

## 2026-11-17 P3 恢復執行

- 上次正式命令因用量上限導致自動核准審查無法完成，沒有執行；未把它當作安全否決，也未繞過審查。
- 本次8080無服務且屬Windows排除範圍7996–8095，bind回10013。沒有改系統保留範圍或主repo設定；以相同BAT模型／參數啟動自有18080服務（PID37940），只有三個隔離worktree的`.env`與本輪harness指向它。實際worker解析endpoint已核對。
- P3 fixture的copy作book分組身份、source為已封存實體檔；先前要求虛擬copy實體存在的preflight失敗已修正，該次未啟動工作。
- `p3-b0-12c` 因新增OCR cache監測假設原版XPOS有`_rope_cache`而報AttributeError並重啟，已精確停止自有controller並作廢，所有耗時／圖片不納入採用證據。
- 監測改為兼容沒有cache的原版XPOS；任何其他監測讀取錯誤只記`monitor_error`，不破壞翻譯queue結果。harness看到monitor/worker失敗立即終止該輪。新增回歸後13 tests、12 subtests通過。
- 新正式原版對照為`p3-b0-12d`，尚在進行；前述30.731%仍僅非LLM配對結果。

### P3 單worker正式流程結果

| Run | 12頁整批秒數 | Sakura請求／HTTP秒 | 有效輸出／worker |
|---|---:|---:|---:|
| 原版12d | 90.991 | 15／12.392 | 12/12／2 |
| 完整組12a | 81.299 | 16／12.953 | 12/12／2 |
| 原版回頭12e | 111.928 | 17／13.883 | 12/12／2 |

各worker均完成6頁、無重啟、API皆ok、來源／設定／程式碼雜湊不變。優化組此輪較兩個原版都快，但原版整批變動大，不能把一輪10.7%或兩輪平均值當成穩定端到端提升。CPU30.731%仍由非LLM配對支持。正式流程包含兩次程序／模型啟動，Sakura與啟動時間另列，不用相減假造pureGPU時間。

候選頁尾XPOS cache已取得實值：14個module、例末頁249entries／3620480bytes，約3.45MiB；這不是長跑上限證據。

額外修正harness終端驗證：最後一頁即使正常queue/exit，只要profile出現monitor_error仍拒絕。合成競態自檢通過，已完成12d/12a全事件重新audit均無此錯誤。

接續只做固定15頁的3worker原版／候選resource smoke；5worker須先有足夠headroom才測。202頁計畫尚未啟動，若進行會明示101個不同原頁重複2次，而非202個獨立頁。

## 狀態回報收斂（2026-11-17）
3worker、5本共15頁：原版75.4547秒→完整組68.7803秒（單輪減時8.846%），兩輪15/15有效，來源不變、API全ok、5workers/0重啟。RAM59.9%/59.5%，VRAM26588/26430MiB。非LLM30.731%為配對已驗收益；端到端8.846%僅此小樣本一對，不宣稱整書保證。使用者要求立即狀態回報；目前所有小樣本已結束，202頁長跑尚未開始，停止自有18080測試server後回報。5worker長跑、C1/XPOS長跑資源gate、品質實驗及最終收尾仍待辦，不標全部計畫完成。

## 最終決策：依使用者要求快速收斂，禁止長跑

- 正式保留 **Bcpu**：panel／像素hash／ROI bilateral／OCR快取與同步優化，以及Windows CLI映射／RAM95／profiling修正。保留float32 fallback與XPOS遷移清cache回歸修正。
- **撤回C1廣泛GC降頻**：root的`manga_translator.py`及`inpainting/common.py`回到`86fe508`既有清理策略，檔案bytes已對照已實測`perf-bcpu`一致。沒有捨棄其他CPU優化或使用者變更。
- 最終採用組的受控12頁非LLM配對耗時下降 **18.497%**（吞吐+22.694%），所有consumer輸出逐位元相同。
- **30.731%／正式15頁8.846%屬含C1的完整組，不能當作目前採用組的承諾。** 含Sakura的最終Bcpu整批百分比不作保證。
- 依新指示不執行202頁、不再新增候選或畫質實驗；crop0、d17、FP32維持。自有server37940已停止，無benchmark在跑。未commit/push，stash保留。
- 不宣稱長期cache上限、所有backend／模式或長書百分比已驗證。另本機8080當前屬Windows保留範圍；主repo `.env`／BAT未變更，本輪僅使用隔離18080測試環境。

## 最終提交驗證（2026-11-17）

快速回歸：99 tests、62 subtests通過，5.75秒；14項既有依賴棄用警告。未追加模型長測或原生 GUI 人工驗收。歷史 oracle 測試需要包含86fe508的 Git history；測試本身不依賴 RTK。本機啟動器與原始圖片、logs、.omx工作資料不納入 PR。
