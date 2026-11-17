"""Small real-OpenCV/CRF equivalence checks against the pre-performance commit."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from manga_translator.mask_refinement import text_mask_utils as masks
from manga_translator.utils import Quadrilateral
from manga_translator.utils import panel as panels
from manga_translator.utils.panel.lib import debug
from manga_translator.utils.panel.lib.segment import Segment


ROOT = Path(__file__).resolve().parents[1]
ORACLE_COMMIT = '86fe508'


def _oracle_module(name, source_path, package=False):
    source = subprocess.run(
        ['git', 'show', f'{ORACLE_COMMIT}:{source_path}'],
        cwd=ROOT, check=True, capture_output=True).stdout
    module = types.ModuleType(name)
    module.__file__ = f'git:{ORACLE_COMMIT}:{source_path}'
    module.__package__ = name if package else name.rpartition('.')[0]
    if package:
        module.__path__ = []
    sys.modules[name] = module
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


def _panel_oracle():
    name = 'manga_translator.utils._perf_panel_oracle'
    for package in (name, name + '.lib'):
        module = types.ModuleType(package)
        module.__package__ = package
        module.__path__ = []
        sys.modules[package] = module
    sys.modules[name + '.lib.debug'] = debug
    for relative in ('lib/segment.py', 'lib/panel.py', 'lib/page.py', 'kumikolib.py'):
        _oracle_module(name + '.' + relative[:-3].replace('/', '.'),
                       'manga_translator/utils/panel/' + relative)
    return _oracle_module(name, 'manga_translator/utils/panel/__init__.py', package=True)


def _box(x1, y1, x2, y2):
    return Quadrilateral(np.asarray(
        [(x1, y1), (x2, y1), (x2, y2), (x1, y2)], dtype=np.int32), '', 1.0)


def _panel_image(long=False):
    shape = (320, 80, 3) if long else (192, 224, 3)
    image = np.full(shape, 255, dtype=np.uint8)
    rectangles = [(8, 8, 72, 100), (8, 116, 72, 208), (8, 224, 72, 312)] if long else [
        (8, 8, 104, 184), (120, 8, 216, 184)]
    for index, (x1, y1, x2, y2) in enumerate(rectangles):
        cv2.rectangle(image, (x1, y1), (x2, y2), (0, 0, 0), 3)
        cv2.rectangle(image, (x1 + 12, y1 + 12), (x1 + 25, y1 + 26),
                      (30, 80 + 30 * index, 160), -1)
    return image


class PerfPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.oracle = _panel_oracle()
        cls.oracle_segment = sys.modules[cls.oracle.__name__ + '.lib.segment'].Segment

    def test_panel_array_matches_png_oracle_for_rtl_ltr_and_views(self):
        base = _panel_image()
        fixtures = {'normal': base, 'noncontiguous': base[:, ::-1],
                    'long': _panel_image(long=True),
                    'blank_boundary': np.full((48, 64, 3), 255, dtype=np.uint8)}
        self.assertFalse(fixtures['noncontiguous'].flags.c_contiguous)
        for name, image in fixtures.items():
            for rtl in (False, True):
                with self.subTest(fixture=name, rtl=rtl):
                    before = image.copy()
                    expected = self.oracle.get_panels_from_array(image, rtl=rtl)
                    actual = panels.get_panels_from_array(image, rtl=rtl)
                    self.assertEqual(actual, expected)
                    self.assertTrue(actual)
                    np.testing.assert_array_equal(image, before)

    def test_non_rgb_uint8_inputs_preserve_png_fallback(self):
        for image in (np.full((48, 64), 180, dtype=np.uint8),
                      np.full((48, 64, 4), 255, dtype=np.uint8),
                      np.full((48, 64, 3), 40000, dtype=np.uint16)):
            with self.subTest(shape=image.shape, dtype=image.dtype):
                self.assertEqual(panels.get_panels_from_array(image),
                                 self.oracle.get_panels_from_array(image))

    def test_segment_set_matches_ordered_list_oracle(self):
        fixtures = [
            [((0, 0), (20, 0)), ((20, 0), (0, 0)), ((20, 0), (40, 0))],
            [((0, 0), (20, 0)), ((20, 0), (40, 0)), ((40, 0), (60, 0)),
             ((0, 10), (60, 10))],
            [((0, 0), (20, 20)), ((20, 20), (40, 40)),
             ((0, 40), (40, 0)), ((8, 6), (8, 6))],
        ]
        for index, endpoints in enumerate(fixtures):
            with self.subTest(fixture=index):
                expected = self.oracle_segment.union_all(
                    [self.oracle_segment(a, b) for a, b in endpoints])
                actual = Segment.union_all([Segment(a, b) for a, b in endpoints])
                self.assertEqual([(s.a, s.b) for s in actual],
                                 [(s.a, s.b) for s in expected])


class PerfMaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.oracle = _oracle_module(
            'manga_translator.mask_refinement._perf_mask_oracle',
            'manga_translator/mask_refinement/text_mask_utils.py')

    def test_uint8_bilateral_regions_match_full_filter(self):
        rng = np.random.default_rng(20261116)
        base = rng.integers(0, 256, (65, 79, 3), dtype=np.uint8)
        original_ipp = cv2.ipp.useIPP()
        self.addCleanup(cv2.ipp.setUseIPP, original_ipp)
        for ipp in (original_ipp, False):
            cv2.ipp.setUseIPP(ipp)
            for image in (base, base[:, ::-1], np.tile(base, (4, 1, 1))):
                before = image.copy()
                full = cv2.bilateralFilter(image, 17, 80, 80)
                height, width = image.shape[:2]
                for rect in ((19, 17, 23, 21), (0, 0, 17, 19),
                             (width - 17, height - 19, 17, 19), (0, 0, 1, 1)):
                    with self.subTest(ipp=ipp, shape=image.shape, rect=rect):
                        x, y, w, h = rect
                        actual = masks._bilateral_region(image, *rect)
                        np.testing.assert_array_equal(actual, full[y:y + h, x:x + w])
                        self.assertTrue(actual.flags.c_contiguous)
                np.testing.assert_array_equal(image, before)

    def test_float32_region_matches_full_filter_without_ipp(self):
        original_ipp = cv2.ipp.useIPP()
        self.addCleanup(cv2.ipp.setUseIPP, original_ipp)
        cv2.ipp.setUseIPP(False)
        rng = np.random.default_rng(20261116)
        image = rng.integers(30, 180, (65, 79, 3), dtype=np.uint8).astype(np.float32)
        image[0, 0], image[-1, -1] = -1000, 1000
        full = cv2.bilateralFilter(image, 17, 80, 80)
        for rect in ((19, 17, 23, 21), (0, 0, 17, 19), (62, 46, 17, 19)):
            with self.subTest(rect=rect):
                x, y, w, h = rect
                actual = masks._bilateral_region(image, *rect)
                np.testing.assert_array_equal(actual, full[y:y + h, x:x + w])

    def _compare_real_mask(self, image, mask, lines, rearranged=False):
        before_image, before_mask = image.copy(), mask.copy()
        real_crf = masks.dcrf.DenseCRF2D
        with patch.object(masks.dcrf, 'DenseCRF2D', wraps=real_crf) as crf, \
                contextlib.redirect_stderr(io.StringIO()):
            operation = 'complete_mask' if rearranged else '_complete_mask_core'
            expected = getattr(self.oracle, operation)(image.copy(), mask.copy(), lines)
            oracle_crf_calls = crf.call_count
            actual = getattr(masks, operation)(image, mask.copy(), lines)
        self.assertGreater(oracle_crf_calls, 0)
        self.assertEqual(crf.call_count, oracle_crf_calls * 2)
        self.assertIsNotNone(actual)
        self.assertEqual(actual.dtype, expected.dtype)
        np.testing.assert_array_equal(actual, expected)
        np.testing.assert_array_equal(image, before_image)
        np.testing.assert_array_equal(mask, before_mask)

    def test_actual_mask_matches_oracle_with_real_crf_and_page_edges(self):
        rng = np.random.default_rng(20261116)
        image = rng.integers(180, 256, (96, 112, 3), dtype=np.uint8)
        mask = np.zeros(image.shape[:2], dtype=np.uint8)
        mask[1:17, 1:17] = 255
        mask[36:52, 44:60] = 255
        mask[-17:-1, -17:-1] = 255
        image[mask > 0] = 20
        lines = [_box(-4, -4, 30, 30), _box(32, 24, 72, 64),
                 _box(82, 66, 116, 100)]
        self._compare_real_mask(image, mask, lines)
        flipped_lines = [Quadrilateral(
            np.column_stack((image.shape[1] - 1 - q.pts[:, 0], q.pts[:, 1])), '', 1.0)
            for q in lines]
        self._compare_real_mask(image[:, ::-1], mask[:, ::-1], flipped_lines)

    def test_actual_rearranged_masks_match_oracle_in_both_directions(self):
        rng = np.random.default_rng(20261116)
        image = rng.integers(180, 256, (384, 64, 3), dtype=np.uint8)
        mask = np.zeros(image.shape[:2], dtype=np.uint8)
        lines = []
        for y in (32, 160, 288):
            mask[y:y + 12, 24:36] = 255
            lines.append(_box(16, y - 8, 44, y + 24))
        image[mask > 0] = 20
        # Smaller target exercises the same rearrange path without large CPU fixtures.
        with patch.object(self.oracle, 'MASK_REARRANGE_TARGET_SIZE', 64), \
                patch.object(masks, 'MASK_REARRANGE_TARGET_SIZE', 64):
            self._compare_real_mask(image, mask, lines, rearranged=True)
            self._compare_real_mask(image.transpose(1, 0, 2), mask.T,
                                    [Quadrilateral(q.pts[:, ::-1].copy(), '', 1.0)
                                     for q in lines], rearranged=True)


if __name__ == '__main__':
    unittest.main()
