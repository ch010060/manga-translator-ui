"""Image-stage benchmark; original translator isolates the main LLM call.

Usage: python scripts/bench_stages.py [config.json] img1 img2 ...
       [--repeat N] [--dump out.json] [--translator sakura] [--save DIR] [--no-sync]
The first image is also run once as warm-up, recorded only in the summary sidecar.
Stage spans are inclusive. --no-sync removes benchmark-added GPU synchronization;
wall time includes stage instrumentation, but excludes final output hashing/saving.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image


def prepare_config(config, translator):
    config = copy.deepcopy(config)
    config.setdefault('translator', {}).update(
        translator=translator, translator_chain=None, selective_translation=None)
    if config.get('ocr', {}).get('ocr', '48px') != '48px' or config.get('ocr', {}).get('use_hybrid_ocr', False):
        raise ValueError('This benchmark requires local 48px OCR without hybrid OCR')
    if config.get('colorizer', {}).get('colorizer', 'none') != 'none':
        raise ValueError('This benchmark requires colorizer=none')
    renderer = config.get('render', {}).get('renderer', 'default')
    if renderer not in ('default', 'none') and str(renderer).lower() not in ('manga2eng', 'manga2eng_pillow'):
        raise ValueError('This benchmark requires a local renderer')
    if config.get('cli', {}).get('batch_concurrent', False):
        raise ValueError('This benchmark requires batch_concurrent=false')
    return config


def array_summary(value):
    array = np.asarray(value)
    return {'shape': list(array.shape), 'dtype': str(array.dtype),
            'md5': hashlib.md5(array.tobytes()).hexdigest()}


def result_image(ctx):
    if ctx is None or getattr(ctx, 'translation_error', None) or getattr(ctx, 'error', None) \
            or getattr(ctx, 'success', None) is False:
        raise ValueError('Pipeline returned a failed context')
    image = getattr(ctx, 'result', None)
    if image is None:
        image = getattr(ctx, 'img_rendered', None)
    if image is None:
        raise ValueError('Pipeline returned no image')
    if isinstance(image, Image.Image) and image.mode == 'P':
        image = image.convert('RGBA')
    array = np.asarray(image)
    if array.dtype != np.uint8 or array.size == 0 or array.ndim not in (2, 3) \
            or (array.ndim == 3 and array.shape[2] not in (2, 3, 4)):
        raise ValueError(f'Invalid output image: shape={array.shape}, dtype={array.dtype}')
    return image


def region_summary(regions):
    result = []
    for region in regions if regions is not None else []:
        item = {'text': getattr(region, 'text', None)}
        for name in ('pts', 'lines', 'panel_index'):
            value = getattr(region, name, None)
            if value is not None:
                item[name] = np.asarray(value).tolist()
        result.append(item)
    return result


def capture_stage(page, name, result, args):
    if name == 'detection':
        page['geometry']['detection'] = region_summary(result[0])
        all_lines = getattr(args[1], 'all_detected_textlines', None)
        if all_lines is not None:
            page['geometry']['all_detected'] = region_summary(all_lines)
        page['mask_raw'] = array_summary(result[1]) if result[1] is not None else None
        page['mask'] = array_summary(result[2]) if result[2] is not None else None
    elif name in ('ocr', 'textline_merge'):
        page['geometry'][name] = region_summary(result)
    elif name == 'mask_refinement':
        page['mask'] = array_summary(result) if result is not None else None
    elif name == 'panel':
        page['geometry']['panels'] = np.asarray(result).tolist()


def timed(name, function, state, synchronize, asynchronous=False):
    def finish(page, started, status):
        synchronize()
        page['stages'].setdefault(name, []).append(
            {'wall_s': time.perf_counter() - started, 'status': status})

    if asynchronous:
        async def wrapped(*args, **kwargs):
            page = state['page']
            synchronize()
            started, status = time.perf_counter(), 'ok'
            try:
                result = await function(*args, **kwargs)
            except BaseException as exc:
                status = type(exc).__name__
                raise
            finally:
                finish(page, started, status)
            capture_stage(page, name, result, args)
            return result
    else:
        def wrapped(*args, **kwargs):
            page = state['page']
            synchronize()
            started, status = time.perf_counter(), 'ok'
            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                status = type(exc).__name__
                raise
            finally:
                finish(page, started, status)
            capture_stage(page, name, result, args)
            return result
    return wrapped


async def downloaded_only(model, *args, **kwargs):
    if not model.is_downloaded():
        raise RuntimeError(f'Missing model for {type(model).__name__}; benchmark downloads are disabled')


@contextmanager
def instrument(translator, state, synchronize):
    import manga_translator.manga_translator as mt
    from manga_translator.detection.common import CommonDetector
    from manga_translator.utils import ModelWrapper, textblock

    with ExitStack() as patches:
        patches.enter_context(patch.object(ModelWrapper, 'download', downloaded_only))
        for method in ('_run_detection', '_run_ocr', '_run_textline_merge', '_run_mask_refinement',
                       '_run_inpainting', '_run_text_rendering', '_batch_translate_contexts',
                       '_run_upscaling', '_cleanup_gpu_memory', '_cleanup_batch_memory'):
            name = method.removeprefix('_run_').removeprefix('_')
            patches.enter_context(patch.object(translator, method, timed(
                name, getattr(translator, method), state, synchronize,
                asynchronous=not method.startswith('_cleanup_'))))
        detect = CommonDetector.detect
        async def timed_detect(model, *args, **kwargs):
            return await timed('  det.' + type(model).__name__, detect, state,
                               synchronize, asynchronous=True)(model, *args, **kwargs)
        patches.enter_context(patch.object(CommonDetector, 'detect', timed_detect))
        patches.enter_context(patch.object(mt, 'detect_bubbles_with_mangalens', timed(
            '  det.MangaLens(bubble)', mt.detect_bubbles_with_mangalens, state, synchronize)))
        patches.enter_context(patch.object(textblock, 'get_panels_from_array', timed(
            'panel', textblock.get_panels_from_array, state, synchronize)))
        yield


async def run_page(translator, config, path, page_key, repeat, index, state,
                   synchronize, open_image, save_dir=None, warmup=False):
    page = {'page': page_key, 'source_path': str(path), 'repeat': repeat, 'index': index,
            'ok': False, 'stages': {}, 'geometry': {}, 'mask': None}
    state['page'] = page
    image = output = None
    try:
        started = time.perf_counter()
        with open(path, 'rb') as stream:
            image = open_image(stream, eager=True)
        image.name = str(path)
        page['read_s'] = time.perf_counter() - started
        page['input_size'] = list(image.size)
        page['input_mode'] = image.mode
        synchronize()
        started = time.perf_counter()
        try:
            ctx = await translator.translate(image, config)
        finally:
            synchronize()
            page['wall_s'] = time.perf_counter() - started
        output = result_image(ctx)
        page['output'] = array_summary(output)
        page['output']['mode'] = getattr(output, 'mode', None)
        page['img_md5'] = page['output']['md5']
        page['texts'] = [r.text for r in (getattr(ctx, 'text_regions', None) or [])]
        if save_dir:
            folder = Path(save_dir) / ('warmup' if warmup else f'repeat-{repeat:03d}')
            folder.mkdir(parents=True, exist_ok=True)
            token = hashlib.sha256(page_key.encode('utf-8')).hexdigest()[:12]
            destination = folder / f'{index:04d}-{Path(path).stem}-{token}.png'
            Image.fromarray(np.asarray(output)).save(destination, format='PNG')
            page['saved_path'] = str(destination)
        page['ok'] = True
    except Exception as exc:
        page['error'] = {'type': type(exc).__name__, 'message': str(exc)}
    finally:
        if output is not None and hasattr(output, 'close'):
            output.close()
        if image is not None:
            image.close()
    return page


async def main(cfg_path, imgs, repeat, dump_path=None, translator='original', save_dir=None, no_sync=False):
    if not imgs or repeat < 1:
        raise ValueError('At least one input image and repeat >= 1 are required')
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    with open(cfg_path, encoding='utf-8') as stream:
        cd = prepare_config(json.load(stream), translator)
    font_path = cd.get('render', {}).get('font_path')
    if font_path and (root / font_path).exists():
        cd['render']['font_path'] = str(root / font_path)
    import torch
    from manga_translator import MangaTranslator
    from manga_translator.config import Config
    from manga_translator.utils import open_pil_image

    params = cd.get('cli', {}).copy()
    params.update(cd)
    params['use_gpu'] = True
    if font_path:
        params['font_path'] = cd['render']['font_path']
    tr = MangaTranslator(params=params)
    keys = {'render', 'upscale', 'translator', 'detector', 'colorizer', 'inpainter', 'ocr',
            'kernel_size', 'mask_dilation_offset', 'force_simple_sort'}
    cfg = Config(**{k: v for k, v in cd.items() if k in keys})
    def synchronize():
        if no_sync:
            return
        if str(tr.device).startswith('cuda'):
            torch.cuda.synchronize(tr.device)
        elif str(tr.device) == 'mps':
            torch.mps.synchronize()

    paths = [Path(p).resolve(strict=True) for p in imgs]
    input_root = Path(os.path.commonpath([str(p.parent) for p in paths]))
    rows, state = [], {}
    summary = {'ok': False, 'device': str(tr.device), 'synchronized_stages': not no_sync,
               'translator': translator, 'config_sha256': hashlib.sha256(
                   json.dumps(cd, sort_keys=True).encode('utf-8')).hexdigest(),
               'wall_definition': 'translate including stage summaries; excluding read, final hash and save'}
    print('device:', tr.device)
    try:
        with instrument(tr, state, synchronize):
            async def run(index, iteration, warmup=False):
                path = paths[index]
                page = await run_page(tr, cfg, path, path.relative_to(input_root).as_posix(),
                                      iteration, index, state, synchronize, open_pil_image, save_dir, warmup)
                if warmup:
                    summary['warmup'] = page
                else:
                    rows.append(page)
                if not page['ok']:
                    raise RuntimeError(f"Benchmark page failed: {page['page']}: {page['error']}")
                print(f"{'warm-up' if warmup else '  '} {page['page']}: {page['wall_s']:.2f}s")
            await run(0, 0, warmup=True)
            for iteration in range(1, repeat + 1):
                for index in range(len(paths)):
                    await run(index, iteration)
        summary['ok'] = True
    finally:
        summary['measured_pages'] = len(rows)
        summary['successful_pages'] = sum(row['ok'] for row in rows)
        if dump_path:
            destination = Path(dump_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            for path, payload in ((destination, rows), (destination.with_suffix('.summary.json'), summary)):
                with path.open('w', encoding='utf-8') as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=1, allow_nan=False)
    total = sum(row['wall_s'] for row in rows)
    print(f'\npages={len(rows)}  wall/page={total / len(rows):.2f}s')
    totals = defaultdict(float)
    calls = defaultdict(int)
    for row in rows:
        for name, spans in row['stages'].items():
            totals[name] += sum(span['wall_s'] for span in spans)
            calls[name] += len(spans)
    for name in sorted(totals, key=lambda name: -totals[name]):
        print(f'{name:<30}{totals[name] / len(rows):7.2f}s/page {totals[name] / total:6.1%}  calls={calls[name]}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='+', help='Optional config.json followed by image paths')
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--dump')
    parser.add_argument('--translator', default='original')
    parser.add_argument('--save')
    parser.add_argument('--no-sync', action='store_true')
    args = parser.parse_args()
    config = args.paths.pop(0) if args.paths[0].endswith('.json') else 'examples/config-example.json'
    asyncio.run(main(config, args.paths, args.repeat, args.dump, args.translator, args.save, args.no_sync))
