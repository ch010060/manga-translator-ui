"""Per-stage wall time of the image pipeline (no LLM: translator=original).

Usage: .venv/bin/python scripts/bench_stages.py [config.json] img1 img2 ... [--repeat N] [--dump out.json] [--translator sakura] [--save DIR]
First image is a warm-up (model load) and is excluded from stats.
"""
import asyncio, hashlib, json, os, sys, time
import numpy as np
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import torch
from PIL import Image
import manga_translator.manga_translator as mt
from manga_translator.config import Config
from manga_translator.detection.common import CommonDetector

T = defaultdict(list)
on = [False]

def sync():
    if torch.backends.mps.is_available(): torch.mps.synchronize()
    elif torch.cuda.is_available(): torch.cuda.synchronize()

def timed_async(name, fn):
    async def w(*a, **k):
        sync(); t = time.perf_counter()
        try: return await fn(*a, **k)
        finally:
            sync()
            if on[0]: T[name].append(time.perf_counter() - t)
    return w

def timed_sync(name, fn):
    def w(*a, **k):
        sync(); t = time.perf_counter()
        try: return fn(*a, **k)
        finally:
            sync()
            if on[0]: T[name].append(time.perf_counter() - t)
    return w

M = mt.MangaTranslator
for s in ['_run_detection', '_run_ocr', '_run_textline_merge', '_run_mask_refinement',
          '_run_inpainting', '_run_text_rendering', '_batch_translate_contexts', '_run_upscaling']:
    setattr(M, s, timed_async(s.removeprefix('_run_').removeprefix('_'), getattr(M, s)))
# detection sub-models: DBNet vs YOLO OBB are both CommonDetector.detect; MangaLens is a plain function
_det = CommonDetector.detect
async def det(self, *a, **k):
    return await timed_async(f'  det.{type(self).__name__}', _det)(self, *a, **k)
CommonDetector.detect = det
mt.detect_bubbles_with_mangalens = timed_sync('  det.MangaLens(bubble)', mt.detect_bubbles_with_mangalens)

async def main(cfg_path, imgs, repeat, dump_path=None, translator='original', save_dir=None):
    cd = json.load(open(cfg_path, encoding='utf-8'))
    cd['translator']['translator'] = translator   # default 'original' isolates image stages from the LLM
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fp = os.path.join(root, cd['render']['font_path'])
    if os.path.exists(fp): cd['render']['font_path'] = fp
    params = cd.get('cli', {}).copy(); params.update(cd); params['use_gpu'] = True
    params['font_path'] = cd['render']['font_path']
    tr = M(params=params)
    keys = {'render', 'upscale', 'translator', 'detector', 'colorizer', 'inpainter', 'ocr'}
    cfg = Config(**{k: v for k, v in cd.items() if k in keys or k in ('kernel_size', 'mask_dilation_offset')})
    print('device:', tr.device)
    dump = []
    async def run(p):
        im = Image.open(p); im.name = p
        t = time.perf_counter(); ctx = await tr.translate(im, cfg); dt = time.perf_counter() - t
        img = getattr(ctx, 'result', None) or getattr(ctx, 'img_rendered', None)
        if save_dir and img is not None:
            os.makedirs(save_dir, exist_ok=True); Image.fromarray(np.asarray(img)).save(os.path.join(save_dir, os.path.basename(p)))
        dump.append({'page': os.path.basename(p),
                     'texts': [r.text for r in (getattr(ctx, 'text_regions', None) or [])],
                     'img_md5': hashlib.md5(np.asarray(img).tobytes()).hexdigest() if img is not None else None})
        return dt
    print(f'warm-up {os.path.basename(imgs[0])}: {await run(imgs[0]):.1f}s (incl. model load)')
    on[0] = True; total = 0; n = 0
    for _ in range(repeat):
        for p in imgs:
            dt = await run(p); total += dt; n += 1
            print(f'  {os.path.basename(p)}: {dt:.2f}s')
    print(f'\npages={n}  wall/page={total/n:.2f}s')
    for k, v in sorted(T.items(), key=lambda x: (x[0].startswith('  '), -sum(x[1]))):
        print(f'{k:<30}{sum(v)/n:7.2f}s/page {sum(v)/total:6.1%}  calls={len(v)}')
    if dump_path: json.dump(dump, open(dump_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

if __name__ == '__main__':
    a = sys.argv[1:]; rep = 1
    if '--repeat' in a: i = a.index('--repeat'); rep = int(a[i+1]); del a[i:i+2]
    dp = None; tl = 'original'
    if '--translator' in a: i = a.index('--translator'); tl = a[i+1]; del a[i:i+2]
    if '--dump' in a: i = a.index('--dump'); dp = a[i+1]; del a[i:i+2]
    cfg = a.pop(0) if a[0].endswith('.json') else 'examples/config-example.json'
    sd = None
    if '--save' in a: i = a.index('--save'); sd = a[i+1]; del a[i:i+2]
    asyncio.run(main(cfg, a, rep, dp, tl, sd))
