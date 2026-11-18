"""--book-lane: one translator reused across books, per-book state reset, one event per book."""
import asyncio
import os
import json
import types
import unittest
from unittest import mock

from manga_translator.mode import local


class BookLaneTests(unittest.TestCase):
    def test_lane_reuses_translator_and_isolates_books(self):
        created, seen = [], []

        class FakeTranslator:
            def __init__(self):
                self.ended = 0
                created.append(self)

            def end_book(self):
                self.ended += 1

        async def fake_translate_files(paths, out, cfg, verbose=False, overwrite=False, args=None, translator=None,
                                       cancel_check=None):
            seen.append((tuple(paths), translator))
            if paths[0] == 'B':
                raise RuntimeError('boom')
            translator = translator or FakeTranslator()
            return {'success': 2, 'failed': 0, 'skipped': 0, 'total': 2, 'translator': translator}

        args = types.SimpleNamespace(input=['A', 'B', 'C'], output=None)
        with mock.patch.object(local, 'translate_files', fake_translate_files), \
                mock.patch('builtins.print') as printed:
            failed = asyncio.run(local.run_book_lane(args, config_service=None))

        # each book is its own call (no cross-book batching); one translator for the whole lane
        self.assertEqual([p for p, _ in seen], [('A',), ('B',), ('C',)])
        self.assertEqual(len(created), 1)
        self.assertIs(seen[1][1], created[0])
        self.assertIs(seen[2][1], created[0])
        self.assertEqual(created[0].ended, 3)  # end_book after every book, even the failing one
        events = [json.loads(c.args[0][len(local.LANE_EVENT_PREFIX):]) for c in printed.call_args_list
                  if c.args and str(c.args[0]).startswith(local.LANE_EVENT_PREFIX)]
        self.assertEqual([e['error'] is None for e in events], [True, False, True])
        self.assertEqual(failed, 1)

    def test_end_book_resets_book_state_but_not_models(self):
        from manga_translator.manga_translator import MangaTranslator
        from manga_translator import translators
        t = MangaTranslator.__new__(MangaTranslator)
        t._batch_contexts, t._batch_configs = [1], [2]
        t.all_page_translations, t._original_page_texts = [{'a': 'b'}], [{'a': 'a'}]
        t._colorizer_history_images, t._current_image_context = ['img'], {'subfolder': 'x'}
        t._saved_image_contexts = {'md5': {}}
        t._cleanup_gpu_memory = lambda aggressive=False: None
        cached = mock.Mock()
        with mock.patch.dict(translators.translator_cache, {'k': cached}, clear=False):
            t.end_book()
        self.assertEqual((t._batch_contexts, t._batch_configs, t.all_page_translations,
                          t._original_page_texts, t._colorizer_history_images, t._saved_image_contexts),
                         ([], [], [], [], [], {}))
        self.assertIsNone(t._current_image_context)
        cached.set_prev_context.assert_called_once_with('')


if __name__ == '__main__':
    unittest.main()


class BookLaneMemoryAndErrorTests(unittest.TestCase):
    """GPT review P0-1 (RAM limit honoured in lanes) and P0-2 (no silent success on batch errors)."""

    def run_lane(self, fake_translate_files, over_after_books=None, over_during=None, books=('A', 'B', 'C')):
        started = []
        state = {'books_done': 0, 'current': None}

        def over():
            if over_during is not None and state['current'] == over_during:
                return 'system memory 96.0% > 95'
            if over_after_books is not None and state['books_done'] >= over_after_books:
                return 'system memory 96.0% > 95'
            return None

        def guard(percent, mb):
            self.assertEqual((percent, mb), (95, 0))  # the lane honours the configured limits
            return over

        async def tracking(paths, *a, **k):
            started.append(paths[0])
            state['current'] = paths[0]
            try:
                return await fake_translate_files(paths, *a, **k)
            finally:
                state['books_done'] += 1
                state['current'] = None

        args = types.SimpleNamespace(input=list(books), output=None, memory_percent=95, memory_limit=0)
        with mock.patch.object(local, 'translate_files', tracking), \
                mock.patch.object(local, 'memory_guard', guard), \
                mock.patch('builtins.print') as printed:
            result = asyncio.run(local.run_book_lane(args, config_service=None))
        events = [json.loads(c.args[0][len(local.LANE_EVENT_PREFIX):]) for c in printed.call_args_list
                  if c.args and str(c.args[0]).startswith(local.LANE_EVENT_PREFIX)]
        return result, started, events

    def test_memory_limit_interrupts_book_at_boundary_and_stops_lane(self):
        async def fake(paths, *a, cancel_check=None, translator=None, **k):
            if paths[0] == 'B':
                self.assertTrue(cancel_check())  # translate_batch polls this at batch/page boundaries
                raise asyncio.CancelledError('Task cancelled')
            return {'success': 2, 'failed': 0, 'skipped': 0, 'total': 2, 'translator': mock.Mock()}

        result, started, events = self.run_lane(fake, over_during='B')  # RAM crosses the limit inside B
        self.assertEqual(result, local.LANE_MEMORY_EXIT)
        self.assertEqual(started, ['A', 'B'])  # C is never taken
        self.assertEqual([(e['event'], e.get('interrupted')) for e in events],
                         [('book_done', None), ('book_done', 'memory'), ('lane_exit', None)])
        self.assertIn('interrupted', events[1]['error'])
        self.assertEqual(events[2]['remaining'], [os.path.abspath('C')])

    def test_memory_limit_between_books_does_not_take_next_book(self):
        async def fake(paths, *a, **k):
            return {'success': 2, 'failed': 0, 'skipped': 0, 'total': 2, 'translator': mock.Mock()}

        result, started, events = self.run_lane(fake, over_after_books=1)
        self.assertEqual(result, local.LANE_MEMORY_EXIT)
        self.assertEqual(started, ['A'])
        self.assertEqual(events[-1]['event'], 'lane_exit')
        self.assertEqual(events[-1]['remaining'], [os.path.abspath('B'), os.path.abspath('C')])

    def test_batch_error_reported_in_event(self):
        async def fake(paths, *a, **k):
            return {'success': 0, 'failed': 3, 'skipped': 0, 'total': 3, 'translator': mock.Mock(),
                    'error': 'RuntimeError: boom'}

        result, _, events = self.run_lane(fake, books=('A',))
        self.assertEqual(result, 1)
        self.assertEqual((events[0]['error'], events[0]['failed']), ('RuntimeError: boom', 3))


class TranslateFilesErrorCountTests(unittest.TestCase):
    """A translate_batch exception must count the pending pages as failed (was 0 before)."""

    def test_exception_counts_pending_pages(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        import manga_translator
        from manga_translator.args import create_parser
        from desktop_qt_ui.services.config_service import ConfigService

        class FailingTranslator:
            def __init__(self, params=None):
                pass

            def set_cancel_check_callback(self, callback):
                pass

            async def translate_batch(self, *a, **k):
                raise RuntimeError('boom')

        root = Path(__file__).resolve().parents[1]
        for existing in (0, 1):
            with tempfile.TemporaryDirectory() as tmp:
                book = Path(tmp) / 'book'
                (book / 'result').mkdir(parents=True)
                for name in ('001.png', '002.png', '003.png'):
                    Image.new('RGB', (8, 8), 'white').save(book / name)
                for name in ('001.png',)[:existing]:  # partial valid output + no-overwrite
                    Image.new('RGB', (8, 8), 'red').save(book / 'result' / name)
                args = create_parser().parse_args(['local', '-i', str(book), '--save-to-source-dir',
                                                   '--source-result-dir', 'result', '--no-recursive',
                                                   '--no-overwrite'])
                with mock.patch.object(manga_translator, 'MangaTranslator', FailingTranslator), \
                        mock.patch('builtins.print'):
                    stats = asyncio.run(local.translate_files(
                        args.input, None, ConfigService(str(root)), overwrite=False, args=args))
                self.assertEqual(stats['success'], 0)
                self.assertGreater(stats['failed'], 0)
                self.assertEqual(stats['failed'] + stats['skipped'], 3)
                self.assertIn('RuntimeError', stats['error'])
