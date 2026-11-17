import io
import os
from pathlib import Path

import pytest
import torch
from PIL import Image


_yolo_dir = Path(__file__).resolve().parents[1] / '.omx' / 'tmp' / 'yolo'
_yolo_dir.mkdir(parents=True, exist_ok=True)
os.environ.setdefault('YOLO_CONFIG_DIR', str(_yolo_dir))

from manga_translator.ocr.xpos_relative_position import XPOS
from manga_translator.utils.generic import get_image_md5


def _legacy_xpos(module, x, offset=0, downscale=False):
    # Frozen uncached forward from 86fe508, independent of the cached helpers.
    length = x.shape[1]
    min_pos = -(length + offset) // 2
    max_pos = length + offset + min_pos
    scale = module.scale ** torch.arange(min_pos, max_pos).to(module.scale).div(module.scale_base)[:, None]
    seq_len, dim = scale.shape
    inv_freq = 1.0 / (10000 ** (torch.arange(dim) / dim))
    positions = torch.einsum('i,j->ij', torch.arange(seq_len, dtype=torch.float), inv_freq).to(scale)
    sin, cos = torch.sin(positions), torch.cos(positions)
    if scale.shape[0] > length:
        scale, sin, cos = scale[-length:], sin[-length:], cos[-length:]
    if downscale:
        scale = 1 / scale
    sin = torch.repeat_interleave(sin * scale, 2, dim=1)
    cos = torch.repeat_interleave(cos * scale, 2, dim=1)
    rotated = torch.stack((-x[:, :, 1::2], x[:, :, ::2]), dim=-1).flatten(-2)
    return (x * cos) + (rotated * sin)


def _cache_bytes(module):
    return sum(t.numel() * t.element_size() for tables in module._rope_cache.values() for t in tables)


def test_image_hash_uses_pixels_independent_of_png_encoding_and_metadata():
    image = Image.frombytes('RGB', (4, 3), bytes(range(36)))
    copies = []
    for compression in (0, 9):
        output = io.BytesIO()
        image.save(output, format='PNG', compress_level=compression)
        output.seek(0)
        copies.append(Image.open(output))
    copies[0].info['comment'] = 'metadata does not identify pixels'
    hashes = {get_image_md5(item) for item in (image, image.copy(), *copies)}
    assert len(hashes) == 1
    assert len(hashes.pop()) == 8
    changed = image.copy()
    changed.putpixel((0, 0), (255, 254, 253))
    assert get_image_md5(changed) != get_image_md5(image)


def test_image_hash_includes_dimensions_even_when_raw_bytes_match():
    pixels = bytes(range(36))
    assert get_image_md5(Image.frombytes('RGB', (4, 3), pixels)) != get_image_md5(
        Image.frombytes('RGB', (2, 6), pixels)
    )


@pytest.mark.parametrize('mode', ['L', 'RGBA', 'P'])
def test_image_hash_preserves_rgb_conversion_contract(mode):
    image = Image.new(mode, (4, 3))
    image.putpixel((1, 1), (41, 82, 123, 17) if mode == 'RGBA' else 42)
    assert get_image_md5(image) == get_image_md5(image.convert('RGB'))
    assert image.mode == mode


@pytest.mark.parametrize('device,module_dtype,input_dtype', [
    ('cpu', torch.float32, torch.float32),
    ('cpu', torch.float64, torch.float64),
    ('cuda', torch.float32, torch.float32),
    ('cuda', torch.float16, torch.float16),
    ('cuda', torch.bfloat16, torch.bfloat16),
    ('cuda', torch.float32, torch.float16),
])
@pytest.mark.parametrize('length,offset,downscale', [
    (1, 0, False), (1, 1, True), (1, 254, False),
    (7, 0, True), (16, 3, False), (255, 0, True),
])
def test_xpos_matches_legacy_on_cold_and_cached_calls(device, module_dtype, input_dtype, length, offset, downscale):
    if device == 'cuda' and not torch.cuda.is_available():
        pytest.skip('CUDA unavailable')
    module = XPOS(80, 320).to(device=device, dtype=module_dtype)
    x = torch.linspace(-1, 1, 2 * length * 80, device=device, dtype=input_dtype).reshape(2, length, 80)
    with torch.inference_mode():
        expected = _legacy_xpos(module, x, offset, downscale)
        actual = module(x, offset, downscale)
        assert actual.dtype == expected.dtype
        assert torch.equal(actual, expected)
        entries, tensor_bytes = len(module._rope_cache), _cache_bytes(module)
        assert entries == 1
        assert tensor_bytes == 2 * length * 80 * module.scale.element_size()
        pointers = tuple(t.data_ptr() for tables in module._rope_cache.values() for t in tables)
        # Batch size and pixel values do not belong in a positional-table key.
        for value in (x, x[:1] * 0.5, x[:1] * -1):
            assert torch.equal(module(value, offset, downscale), _legacy_xpos(module, value, offset, downscale))
        assert len(module._rope_cache) == entries
        assert _cache_bytes(module) == tensor_bytes
        assert pointers == tuple(t.data_ptr() for tables in module._rope_cache.values() for t in tables)


def test_xpos_cache_separates_sequence_offset_and_downscale():
    module = XPOS(80, 320)
    with torch.inference_mode():
        for length, offset, downscale in [(1, 0, False), (7, 0, False), (7, 3, False), (7, 3, True)]:
            x = torch.ones(2, length, 80)
            assert torch.equal(module(x, offset, downscale), _legacy_xpos(module, x, offset, downscale))
    assert len(module._rope_cache) == 4


def test_xpos_cache_keeps_correct_results_across_dtype_and_device_moves():
    module = XPOS(80, 320)
    modes = [('cpu', torch.float32), ('cpu', torch.float64)]
    if torch.cuda.is_available():
        modes += [('cuda', torch.float32), ('cuda', torch.float16), ('cpu', torch.float32)]
    with torch.inference_mode():
        for device, dtype in modes:
            module.to(device=device, dtype=dtype)
            assert not module._rope_cache
            x = torch.ones(2, 7, 80, device=device, dtype=dtype)
            assert torch.equal(module(x, 3, True), _legacy_xpos(module, x, 3, True))
            assert all(t.device == module.scale.device for tables in module._rope_cache.values() for t in tables)


def test_xpos_lossy_dtype_roundtrip_regenerates_tables():
    module = XPOS(80, 320)
    x = torch.ones(2, 7, 80)
    with torch.inference_mode():
        module(x, 3, True)
        module.half().float()
        assert not module._rope_cache
        assert torch.equal(module(x, 3, True), _legacy_xpos(module, x, 3, True))
