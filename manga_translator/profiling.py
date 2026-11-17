"""Opt-in worker wall-clock profiling; never records dialogue or synchronizes CUDA."""
import contextlib
import contextvars
import functools
import inspect
import itertools
import json
import os
import sys
import time
from pathlib import Path


class WorkerProfile:
    def __init__(self, directory=None):
        self.file = None
        self.page = None
        self.sequence = itertools.count(1)
        self.parent = contextvars.ContextVar('profile_parent', default=None)
        if directory:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            self.file = (directory / f'worker-{os.getpid()}-{time.time_ns()}.jsonl').open(
                'x', encoding='utf-8')

    def write(self, event, **fields):
        if self.file:
            try:
                self.file.write(json.dumps(dict(event=event, pid=os.getpid(), page=self.page,
                                               wall_ns=time.time_ns(), **fields), ensure_ascii=False) + '\n')
            except OSError as exc:
                self._disable(exc)

    def _disable(self, error):
        sink, self.file = self.file, None
        if sink is not None:
            try:
                sink.close()
            except OSError:
                pass
        try:
            print(f'WARNING: Worker profiling disabled after I/O failure; output is incomplete: {error}',
                  file=sys.stderr)
        except OSError:
            pass

    @contextlib.contextmanager
    def span(self, stage, **fields):
        if not self.file:
            yield
            return
        span_id = next(self.sequence)
        parent = self.parent.get()
        token = self.parent.set(span_id)
        started = time.perf_counter_ns()
        status = 'ok'
        try:
            yield
        except BaseException as exc:
            status = type(exc).__name__
            raise
        finally:
            elapsed = time.perf_counter_ns() - started
            self.parent.reset(token)
            self.write('span', stage=stage, span_id=span_id, parent_id=parent,
                       start_ns=started, duration_ns=elapsed, status=status, **fields)

    def wrap(self, function, stage):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def timed(*args, **kwargs):
                with self.span(stage):
                    return await function(*args, **kwargs)
        else:
            @functools.wraps(function)
            def timed(*args, **kwargs):
                with self.span(stage):
                    return function(*args, **kwargs)
        return timed

    def ocr_cache_snapshot(self):
        """Read logical XPOS cache bytes without loading OCR or touching tensor data."""
        ocr = sys.modules.get('manga_translator.ocr')
        if ocr is None:
            return None
        model = getattr(ocr.ocr_cache.get(ocr.Ocr.ocr48px), 'model', None)
        if model is None:
            return None
        from .ocr.xpos_relative_position import XPOS
        snapshot = dict(module_count=0, entries=0, tensor_bytes=0, bytes_by_device={})
        for module in model.modules():
            if not isinstance(module, XPOS):
                continue
            snapshot['module_count'] += 1
            cache = getattr(module, '_rope_cache', {})
            snapshot['entries'] += len(cache)
            for tables in cache.values():
                for tensor in tables:
                    size = tensor.numel() * tensor.element_size()
                    device = str(tensor.device)
                    snapshot['tensor_bytes'] += size
                    snapshot['bytes_by_device'][device] = snapshot['bytes_by_device'].get(device, 0) + size
        return snapshot

    def install(self, translator):
        if not self.file:
            return
        methods = {
            'translate_batch': 'pipeline', '_materialize_batch_inputs': 'materialize',
            '_run_detection': 'detect', '_run_ocr': 'ocr',
            '_run_textline_merge': 'textline_merge', '_batch_translate_texts': 'translation',
            '_run_mask_refinement': 'mask', '_run_inpainting': 'inpaint',
            '_run_text_rendering': 'render', '_save_translated_image': 'save',
            '_cleanup_gpu_memory': 'cleanup_gpu', '_cleanup_context_memory': 'cleanup_context',
            '_cleanup_batch_memory': 'cleanup_batch',
        }
        for name, stage in methods.items():
            setattr(translator, name, self.wrap(getattr(translator, name), stage))
        from .utils import ModelWrapper
        from .inpainting.common import OfflineInpainter
        from .translators.sakura import SakuraTranslator
        load = ModelWrapper.load
        @functools.wraps(load)
        async def timed_load(model, *args, **kwargs):
            with self.span('model_load', model=type(model).__name__, cached=model.is_loaded()):
                return await load(model, *args, **kwargs)
        ModelWrapper.load = timed_load
        OfflineInpainter._cleanup_memory = self.wrap(OfflineInpainter._cleanup_memory, 'cleanup_inpainter')
        SakuraTranslator._request_translation = self.wrap(SakuraTranslator._request_translation, 'sakura_request')
        from . import manga_translator as pipeline
        from .detection import default as default_detector
        from .utils import textblock
        for owner, name, stage in (
            (pipeline, 'dispatch_textline_merge', 'merge_groups'),
            (pipeline, 'sort_regions', 'region_sort'),
            (textblock, 'get_panels_from_array', 'panel_detect'),
            (textblock.langid, 'classify', 'language_classify'),
            (default_detector, 'det_batch_forward_default', 'detect_forward'),
            (default_detector, 'det_rearrange_forward', 'detect_rearrange'),
            (default_detector.dbnet_utils.SegDetectorRepresenter, '__call__', 'detect_boxes'),
            (default_detector.cv2, 'bilateralFilter', 'bilateral'),
        ):
            setattr(owner, name, self.wrap(getattr(owner, name), stage))

    def flush(self):
        if self.file:
            try:
                self.file.flush()
            except OSError as exc:
                self._disable(exc)

    def close(self):
        if self.file:
            sink, self.file = self.file, None
            try:
                sink.close()
            except OSError as exc:
                self._disable(exc)
