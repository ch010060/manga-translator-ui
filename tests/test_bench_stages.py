import asyncio
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'bench_stages.py'
spec = importlib.util.spec_from_file_location('bench_stages_under_test', SCRIPT)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class BenchStagesTests(unittest.TestCase):
    def test_import_does_not_load_pipeline_or_torch(self):
        code = ("import runpy,sys; runpy.run_path(sys.argv[1]); "
                "assert 'torch' not in sys.modules; assert 'manga_translator' not in sys.modules")
        subprocess.run([sys.executable, '-c', code, str(SCRIPT)], check=True, capture_output=True)

    def test_numpy_result_and_fallback_do_not_use_truth_value(self):
        pixels = np.zeros((3, 4, 3), dtype=np.uint8)
        self.assertIs(bench.result_image(SimpleNamespace(result=pixels)), pixels)
        self.assertIs(bench.result_image(SimpleNamespace(result=None, img_rendered=pixels)), pixels)

    def test_missing_failed_and_invalid_results_fail(self):
        for ctx in (None, SimpleNamespace(result=None),
                    SimpleNamespace(result=np.zeros((3, 4), dtype=np.uint8), translation_error='failed'),
                    SimpleNamespace(result=np.zeros((0, 4, 3), dtype=np.uint8)),
                    SimpleNamespace(result=np.zeros((3, 4, 3), dtype=np.float32))):
            with self.subTest(ctx=ctx), self.assertRaises(ValueError):
                bench.result_image(ctx)

    def test_palette_output_is_hashed_as_pixels_instead_of_indices(self):
        image = Image.new('P', (2, 2))
        image.putpalette([255, 0, 0] + [0] * 765)
        output = bench.result_image(SimpleNamespace(result=image))
        self.assertEqual(output.mode, 'RGBA')
        self.assertEqual(output.getpixel((0, 0)), (255, 0, 0, 255))
        output.close()
        image.close()

    def test_original_override_clears_chains_and_rejects_external_stages(self):
        original = {'translator': {'translator_chain': 'sakura:ENG', 'selective_translation': 'sakura:ENG'}}
        config = bench.prepare_config(original, 'original')
        self.assertEqual(config['translator'], {
            'translator': 'original', 'translator_chain': None, 'selective_translation': None})
        self.assertEqual(original['translator']['translator_chain'], 'sakura:ENG')
        for renderer in ('manga2eng', 'manga2eng_pillow', 'MANGA2ENG'):
            local = bench.prepare_config({'render': {'renderer': renderer}}, 'original')
            self.assertEqual(local['render']['renderer'], renderer)
        for bad in ({'ocr': {'ocr': 'openai_ocr'}}, {'ocr': {'use_hybrid_ocr': True}},
                    {'colorizer': {'colorizer': 'openai_colorizer'}},
                    {'render': {'renderer': 'gemini_renderer'}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bench.prepare_config(bad, 'original')

    def test_stage_capture_copies_geometry_and_mask_before_cleanup(self):
        region = SimpleNamespace(text='sample', lines=np.zeros((1, 4, 2), dtype=np.int32), panel_index=2)
        mask = np.zeros((3, 4), dtype=np.uint8)
        page = {'geometry': {}}
        bench.capture_stage(page, 'textline_merge', [region], ())
        bench.capture_stage(page, 'mask_refinement', mask, ())
        before = json.dumps(page)
        region.lines[:] = 9
        mask[:] = 255
        self.assertEqual(json.dumps(page), before)
        self.assertEqual(page['geometry']['textline_merge'][0]['panel_index'], 2)

    def test_page_load_is_eager_closes_input_and_saves_unique_pngs(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.jpg'
            Image.new('RGB', (4, 3), 'white').save(source)
            opened = []
            def open_image(stream, eager=False):
                self.assertTrue(eager)
                image = Image.open(stream)
                image.load()
                opened.append(image)
                return image
            class Translator:
                async def translate(self, image, config):
                    self.pixels = np.asarray(image).copy()
                    return SimpleNamespace(result=self.pixels, text_regions=[])
            state = {}
            translator = Translator()
            rows = [asyncio.run(bench.run_page(
                translator, None, source, 'book/input.jpg', iteration, 0,
                state, lambda: None, open_image, directory, warmup=iteration == 0))
                for iteration in (0, 1, 2)]
            self.assertTrue(all(row['ok'] for row in rows))
            self.assertEqual(len({row['saved_path'] for row in rows}), 3)
            self.assertTrue(all(Path(row['saved_path']).suffix == '.png' for row in rows))
            json.dumps(rows, allow_nan=False)
            for image in opened:
                with self.assertRaises(ValueError):
                    image.getpixel((0, 0))

    def test_missing_model_fails_without_downloading(self):
        with self.assertRaisesRegex(RuntimeError, 'downloads are disabled'):
            asyncio.run(bench.downloaded_only(SimpleNamespace(is_downloaded=lambda: False)))
        asyncio.run(bench.downloaded_only(SimpleNamespace(is_downloaded=lambda: True)))

    def test_main_dump_excludes_warmup_and_keeps_relative_paths(self):
        class Translator:
            device = 'cpu'
            def __init__(self, **kwargs):
                pass
            async def translate(self, image, config):
                return SimpleNamespace(result=np.asarray(image).copy(), text_regions=[])
        def open_image(stream, eager=False):
            self.assertTrue(eager)
            image = Image.open(stream)
            image.load()
            return image
        modules = {
            'torch': SimpleNamespace(),
            'manga_translator': SimpleNamespace(MangaTranslator=Translator),
            'manga_translator.config': SimpleNamespace(Config=lambda **kwargs: None),
            'manga_translator.utils': SimpleNamespace(open_pil_image=open_image),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text('{}', encoding='utf-8')
            images = [root / book / 'same.png' for book in ('book-a', 'book-b')]
            for image in images:
                image.parent.mkdir()
                Image.new('RGB', (4, 3)).save(image)
            dump = root / 'pages.json'
            with patch.dict(sys.modules, modules), \
                    patch.object(bench, 'instrument', return_value=contextlib.nullcontext()), \
                    contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(bench.main(config, images, 1, dump, no_sync=True))
            rows = json.loads(dump.read_text(encoding='utf-8'))
            summary = json.loads((root / 'pages.summary.json').read_text(encoding='utf-8'))
            self.assertEqual([row['page'] for row in rows], ['book-a/same.png', 'book-b/same.png'])
            self.assertEqual([row['repeat'] for row in rows], [1, 1])
            self.assertEqual(summary['warmup']['repeat'], 0)
            self.assertEqual(summary['successful_pages'], 2)
            self.assertTrue(summary['ok'])


if __name__ == '__main__':
    unittest.main()
