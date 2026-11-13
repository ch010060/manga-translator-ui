import os
import tempfile

from ..generic import imwrite_unicode
from .kumikolib import Kumiko


def get_panels_from_array(img_rgb, rtl=True, logger=None):
    # A PNG round-trip of a 3-channel uint8 array is lossless, so pass it directly.
    if img_rgb.ndim == 3 and img_rgb.shape[2] == 3 and img_rgb.dtype.name == 'uint8':
        k = Kumiko({'rtl': rtl})
        k.parse_image(img_rgb)
        return k.get_infos()[0]['panels']

    tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
    path = tmp.name
    tmp.close()  

    # Always use the unicode-safe writer.
    # If no logger is passed, create a default one.
    if not logger:
        import logging
        logger = logging.getLogger(__name__)
    imwrite_unicode(path, img_rgb, logger)

    k = Kumiko({'rtl': rtl})
    k.parse_image(path)
    infos = k.get_infos()

    os.unlink(path)

    return infos[0]['panels']
