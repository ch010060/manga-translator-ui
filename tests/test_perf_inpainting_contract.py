import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest


_yolo_dir = Path(__file__).resolve().parents[1] / '.omx' / 'tmp' / 'yolo'
_yolo_dir.mkdir(parents=True, exist_ok=True)
os.environ.setdefault('YOLO_CONFIG_DIR', str(_yolo_dir))

from manga_translator import inpainting
from manga_translator.config import Config, Inpainter, InpainterConfig


class FakeInpainter:
    def __init__(self):
        self.calls = []

    async def inpaint(self, image, mask, config, inpainting_size, verbose):
        self.calls.append((image.copy(), mask.copy(), config, inpainting_size, verbose))
        return np.full_like(image, 211)


def _dispatch(image, mask, crop_pad, fake):
    config = InpainterConfig(inpainting_crop_pad=crop_pad)
    with patch.object(inpainting, 'get_inpainter', return_value=fake):
        return asyncio.run(inpainting.dispatch(Inpainter.lama_large, image, mask, config, 64))


def test_zero_crop_pad_preserves_whole_image_path():
    image = np.arange(12 * 12 * 3, dtype=np.uint8).reshape(12, 12, 3)
    mask = np.zeros((12, 12), dtype=np.uint8)
    mask[4:6, 4:6] = 1
    fake = FakeInpainter()
    with patch.object(inpainting, '_dispatch_with_crops', new=AsyncMock()) as crops:
        result = _dispatch(image, mask, 0, fake)
    crops.assert_not_awaited()
    assert len(fake.calls) == 1
    np.testing.assert_array_equal(fake.calls[0][0], image)
    np.testing.assert_array_equal(fake.calls[0][1], mask * 255)
    np.testing.assert_array_equal(result, np.full_like(image, 211))


def test_crop_writes_only_mask_pixels_and_preserves_inputs():
    image = np.arange(20 * 20 * 3, dtype=np.uint8).reshape(20, 20, 3)
    mask = np.zeros((20, 20), dtype=np.uint8)
    mask[:2, :2] = 1
    mask[15:18, 15:18] = 127
    image_before, mask_before = image.copy(), mask.copy()
    fake = FakeInpainter()
    result = _dispatch(image, mask, 2, fake)
    expected = image.copy()
    expected[mask > 0] = 211
    np.testing.assert_array_equal(result, expected)
    np.testing.assert_array_equal(image, image_before)
    np.testing.assert_array_equal(mask, mask_before)
    assert len(fake.calls) == 2
    assert all(call[0].shape[0] < 20 and call[0].shape[1] < 20 for call in fake.calls)


def test_empty_crop_mask_skips_inpainter_and_returns_unchanged_pixels():
    image = np.full((12, 12, 3), 47, dtype=np.uint8)
    mask = np.zeros((12, 12), dtype=np.uint8)
    fake = FakeInpainter()
    result = _dispatch(image, mask, 2, fake)
    assert not fake.calls
    np.testing.assert_array_equal(result, image)
    assert not np.shares_memory(result, image)
    assert not np.any(mask)


@pytest.mark.parametrize('shape', [(16, 4, 3), (4, 16, 3)])
def test_extreme_aspect_ratio_keeps_split_priority_over_crop(shape):
    image = np.zeros(shape, dtype=np.uint8)
    mask = np.ones(shape[:2], dtype=np.uint8)
    fake = FakeInpainter()
    split = AsyncMock(return_value=np.full_like(image, 93))
    with patch.object(inpainting, '_dispatch_with_split', new=split), \
            patch.object(inpainting, '_dispatch_with_crops', new=AsyncMock()) as crops:
        result = _dispatch(image, mask, 2, fake)
    split.assert_awaited_once()
    crops.assert_not_awaited()
    assert not fake.calls
    np.testing.assert_array_equal(result, np.full_like(image, 93))


@pytest.mark.parametrize('crop_pad', [0, 2])
def test_rgba_preserves_existing_surrounding_alpha_contract(crop_pad):
    image = np.full((12, 12, 4), 64, dtype=np.uint8)
    image[5:7, 5:7, 3] = 240
    mask = np.zeros((12, 12), dtype=np.uint8)
    mask[5:7, 5:7] = 255
    before = image.copy()
    result = _dispatch(image, mask, crop_pad, FakeInpainter())
    assert result.shape == image.shape
    np.testing.assert_array_equal(result[:, :, 3], np.full((12, 12), 64, dtype=np.uint8))
    np.testing.assert_array_equal(image, before)


@pytest.mark.parametrize('crop_pad', [0, 48])
def test_crop_config_survives_serialization_roundtrip(crop_pad):
    config = Config(inpainter=InpainterConfig(inpainting_crop_pad=crop_pad))
    serialized = config.model_dump(mode='json')
    assert serialized['inpainter']['inpainting_crop_pad'] == crop_pad
    assert Config.model_validate(serialized).inpainter.inpainting_crop_pad == crop_pad
    assert InpainterConfig().inpainting_crop_pad == 0
