import asyncio
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from manga_translator.profiling import WorkerProfile
from manga_translator.mode.local import apply_cli_overrides, run_local_mode
from manga_translator.mode.subprocess_manager import worker_translate_batch


class OCRCacheSnapshotTests(unittest.TestCase):
    def test_legacy_xpos_without_cache_reports_zero_entries(self):
        import torch
        from manga_translator.config import Ocr
        from manga_translator.ocr import ocr_cache
        from manga_translator.ocr.xpos_relative_position import XPOS
        model = torch.nn.ModuleList([XPOS(8)])
        del model[0]._rope_cache
        with patch.dict(ocr_cache, {Ocr.ocr48px: SimpleNamespace(model=model)}, clear=True):
            result = WorkerProfile().ocr_cache_snapshot()
        self.assertEqual(result, dict(module_count=1, entries=0, tensor_bytes=0, bytes_by_device={}))

    def test_missing_cache_and_unloaded_model_do_not_instantiate_ocr(self):
        from manga_translator.config import Ocr
        get_ocr = Mock(side_effect=AssertionError('snapshot must not instantiate OCR'))
        for cache in ({}, {Ocr.ocr48px: SimpleNamespace()}):
            with self.subTest(cache=bool(cache)), patch.dict(sys.modules, {
                    'manga_translator.ocr': SimpleNamespace(
                        Ocr=Ocr, ocr_cache=cache, get_ocr=get_ocr)}):
                self.assertIsNone(WorkerProfile().ocr_cache_snapshot())
        with patch.dict(sys.modules, {'manga_translator.ocr': None}):
            self.assertIsNone(WorkerProfile().ocr_cache_snapshot())
        get_ocr.assert_not_called()

    def test_real_cpu_xpos_cache_counts_and_tensor_bytes_are_json_values(self):
        import torch
        from manga_translator.config import Ocr
        from manga_translator.ocr import ocr_cache
        from manga_translator.ocr.xpos_relative_position import XPOS
        model = torch.nn.ModuleList([XPOS(8) for _ in range(14)])
        model[0]._rope_cache[(3, 0, False, torch.device('cpu'), torch.float32, torch.float32)] = (
            torch.zeros((3, 8)), torch.ones((3, 8)))
        model[1]._rope_cache[(2, 1, True, torch.device('cpu'), torch.float16, torch.float16)] = (
            torch.zeros((2, 8), dtype=torch.float16), torch.ones((2, 8), dtype=torch.float16))
        with patch.dict(ocr_cache, {Ocr.ocr48px: SimpleNamespace(model=model)}, clear=True):
            result = WorkerProfile().ocr_cache_snapshot()
        self.assertEqual(result, dict(module_count=14, entries=2, tensor_bytes=256,
                                      bytes_by_device={'cpu': 256}))
        self.assertEqual(json.loads(json.dumps(result)), result)
        self.assertEqual(len(model[0]._rope_cache), 1)
        self.assertEqual(len(model[1]._rope_cache), 1)

    def test_snapshot_does_not_keep_unloaded_modules_or_tensors_alive(self):
        import torch
        from manga_translator.config import Ocr
        from manga_translator.ocr import ocr_cache
        from manga_translator.ocr.xpos_relative_position import XPOS
        profile = WorkerProfile()
        owner = SimpleNamespace(model=torch.nn.ModuleList([XPOS(8)]))
        owner.model[0]._rope_cache['entry'] = (torch.zeros((2, 8)), torch.ones((2, 8)))
        model_ref = weakref.ref(owner.model)
        xpos_ref = weakref.ref(owner.model[0])
        tensor_ref = weakref.ref(owner.model[0]._rope_cache['entry'][0])
        with patch.dict(ocr_cache, {Ocr.ocr48px: owner}, clear=True):
            result = profile.ocr_cache_snapshot()
            del owner.model
            self.assertIsNone(profile.ocr_cache_snapshot())
        self.assertEqual(result['tensor_bytes'], 128)
        self.assertIsNone(model_ref())
        self.assertIsNone(xpos_ref())
        self.assertIsNone(tensor_ref())


class WorkerProfilingTests(unittest.TestCase):
    def test_nested_spans_keep_parent_and_record_failure_without_swallowing(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = WorkerProfile(directory)
            profile.page = 'sample.png'
            with self.assertRaisesRegex(ValueError, 'failure'):
                with profile.span('page'):
                    with profile.span('read'):
                        raise ValueError('failure')
            profile.close()
            rows = [json.loads(line) for line in next(Path(directory).glob('*.jsonl')).read_text().splitlines()]
            child, parent = rows
            self.assertEqual(child['parent_id'], parent['span_id'])
            self.assertIsNone(parent['parent_id'])
            self.assertEqual(parent['status'], 'ValueError')
            self.assertEqual(child['page'], 'sample.png')
            self.assertGreaterEqual(parent['duration_ns'], child['duration_ns'])

    def test_async_wrapper_preserves_result_and_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = WorkerProfile(directory)
            async def result():
                return 42
            async def cancelled():
                raise asyncio.CancelledError()
            self.assertEqual(asyncio.run(profile.wrap(result, 'request')()), 42)
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(profile.wrap(cancelled, 'request')())
            profile.close()
            rows = [json.loads(line) for line in next(Path(directory).glob('*.jsonl')).read_text().splitlines()]
            self.assertEqual([r['status'] for r in rows], ['ok', 'CancelledError'])

    def test_disabled_profile_has_no_output_and_does_not_patch_translator(self):
        profile = WorkerProfile()
        profile.install(object())
        with profile.span('page'):
            pass
        self.assertIsNone(profile.file)

    def test_sink_io_failures_disable_output_and_warn_once(self):
        for operation in ('write', 'flush', 'close'):
            with self.subTest(operation=operation):
                profile = WorkerProfile()
                sink = profile.file = Mock()
                getattr(sink, operation).side_effect = OSError('disk unavailable')
                sink.close.side_effect = OSError('disk unavailable')
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    if operation == 'write':
                        profile.write('event')
                    else:
                        getattr(profile, operation)()
                    profile.write('ignored')
                    profile.flush()
                    profile.close()
                self.assertIsNone(profile.file)
                sink.close.assert_called_once()
                self.assertEqual(stderr.getvalue().count('WARNING:'), 1)
                self.assertIn('output is incomplete', stderr.getvalue())

    def test_failed_span_write_preserves_result_error_and_cancellation(self):
        for error in (None, ValueError('inference failure'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                profile = WorkerProfile()
                profile.file = Mock()
                profile.file.write.side_effect = OSError('disk unavailable')
                async def inference():
                    if error is not None:
                        raise error
                    return 42
                with contextlib.redirect_stderr(io.StringIO()):
                    if error is None:
                        self.assertEqual(asyncio.run(profile.wrap(inference, 'request')()), 42)
                    else:
                        with self.assertRaises(type(error)) as caught:
                            asyncio.run(profile.wrap(inference, 'request')())
                        self.assertIs(caught.exception, error)
                self.assertIsNone(profile.file)

    def test_sink_io_failures_still_send_worker_results(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / 'sample.png'
            image_path.write_bytes(b'fixture')
            for operation in ('write', 'flush', 'close', 'ocr_cache'):
                with self.subTest(operation=operation):
                    profile = WorkerProfile()
                    profile.file = Mock()
                    sink = profile.file
                    if operation == 'ocr_cache':
                        profile.ocr_cache_snapshot = Mock(side_effect=RuntimeError('cache reader failed'))
                    else:
                        getattr(profile.file, operation).side_effect = OSError('disk unavailable')
                    translator = SimpleNamespace(
                        translate_batch=AsyncMock(return_value=[SimpleNamespace(success=True)]),
                        _get_cuda_memory_snapshot=Mock(return_value=None))
                    queue = Mock()
                    stderr = io.StringIO()
                    with patch('manga_translator.profiling.WorkerProfile', return_value=profile), \
                            patch.object(profile, 'install'), \
                            patch('manga_translator.MangaTranslator', return_value=translator), \
                            patch('manga_translator.utils.open_pil_image', return_value=Mock()), \
                            patch('manga_translator.utils.init_logging'), \
                            patch('manga_translator.utils.set_log_level'), \
                            patch('manga_translator.mode.subprocess_manager.get_memory_usage_mb', return_value=0), \
                            patch('manga_translator.mode.subprocess_manager.get_system_memory_percent', return_value=0), \
                            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                        worker_translate_batch([str(image_path)], directory, None, False, False, 0, 1,
                                               {'cli': {}}, 0, 95, queue)
                    queue.put.assert_called_once_with({
                        'status': 'success', 'completed': [str(image_path)], 'failed': []})
                    if operation == 'ocr_cache':
                        self.assertIn('monitor_error', ''.join(call.args[0] for call in sink.write.call_args_list))
                    else:
                        self.assertIn('output is incomplete', stderr.getvalue())

    def test_invalid_profile_destination_reports_worker_error_instead_of_hanging_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'not-a-directory'
            path.write_text('fixture')
            queue = Mock()
            with patch.dict('os.environ', MANGA_PROFILE_DIR=str(path)), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                worker_translate_batch([], directory, None, False, False, 0, 0,
                                       {'cli': {}}, 0, 95, queue)
            self.assertEqual(queue.put.call_args.args[0]['status'], 'error')

    def test_cli_overrides_preserve_absent_values_and_accept_sentinels(self):
        config = {'cli': {'use_gpu': True, 'batch_size': 8, 'attempts': 2, 'batch_concurrent': False}}
        apply_cli_overrides(config, SimpleNamespace(batch_size=None, attempts=None, concurrent=False))
        self.assertEqual(config['cli']['batch_size'], 8)
        self.assertTrue(config['cli']['use_gpu'])
        apply_cli_overrides(config, SimpleNamespace(use_gpu=False, batch_size=4, attempts=-1,
                                                   concurrent=True, format='png'))
        self.assertEqual(config['cli'], {'use_gpu': False, 'batch_size': 4, 'attempts': -1,
                                        'batch_concurrent': True, 'format': 'png'})

    def test_subprocess_entry_passes_effective_overrides_to_worker_manager(self):
        config = {'cli': {'use_gpu': True, 'batch_size': 8, 'attempts': 2, 'batch_concurrent': False}}
        service = SimpleNamespace(get_config=lambda: SimpleNamespace(model_dump=lambda: copy.deepcopy(config)))
        modules = {
            'desktop_qt_ui.services.config_service': SimpleNamespace(ConfigService=lambda root: service),
            'desktop_qt_ui.services.file_service': SimpleNamespace(
                FileService=lambda: SimpleNamespace(_natural_sort_key=str)),
        }
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'sample.png'
            image.write_bytes(b'fixture')
            args = SimpleNamespace(config=None, subprocess=True, verbose=False, overwrite=True,
                                   input=[str(image)], output=str(Path(directory) / 'output'),
                                   batch_size=4, attempts=-1, concurrent=True, memory_percent=95)
            manager = AsyncMock(return_value=(1, 0))
            with patch.dict(sys.modules, modules), patch(
                    'manga_translator.mode.subprocess_manager.translate_with_subprocess', manager), \
                    contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(run_local_mode(args))
            effective = manager.call_args.kwargs['config_dict']['cli']
            self.assertEqual(effective['batch_size'], 4)
            self.assertEqual(effective['attempts'], -1)
            self.assertTrue(effective['batch_concurrent'])
            self.assertTrue(effective['use_gpu'])
            self.assertEqual(manager.call_args.kwargs['memory_limit_percent'], 95)
