"""--book-lane: one translator reused across books, per-book state reset, one event per book."""
import asyncio
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

        async def fake_translate_files(paths, out, cfg, verbose=False, overwrite=False, args=None, translator=None):
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
