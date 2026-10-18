from __future__ import annotations

import base64
import binascii
import warnings
from io import BytesIO

from PIL import Image, UnidentifiedImageError

AVATAR_MAX_BYTES = 64 * 1024
AVATAR_MAX_BODY_BYTES = 96 * 1024
_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


def normalize_avatar(value: str) -> tuple[str, bytes]:
    header, separator, encoded = value.partition(",")
    media_type = header.removeprefix("data:").removesuffix(";base64").lower()
    if not separator or header != f"data:{media_type};base64" or media_type not in _FORMATS:
        raise ValueError("请选择 PNG、JPEG 或 WebP 图片。")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("头像编码无效。") from error
    if not raw or len(raw) > AVATAR_MAX_BYTES:
        raise ValueError("头像文件不能超过 64 KiB。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                if (
                    image.format != _FORMATS[media_type]
                    or image.width != image.height
                    or not 1 <= image.width <= 192
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ValueError("头像必须是边长不超过 192 像素的静态正方形图片。")
                image.load()
                # A new pixel-only image prevents EXIF, comments and profiles surviving encoding.
                clean = Image.new("RGBA", image.size)
                clean.paste(image.convert("RGBA"))
                output = BytesIO()
                clean.save(output, format="WEBP", quality=82, method=4)
                normalized = output.getvalue()
    except (
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise ValueError("无法读取这张图片。") from error
    if not normalized or len(normalized) > AVATAR_MAX_BYTES:
        raise ValueError("压缩后的头像仍然过大，请选择更简单的图片。")
    return "image/webp", normalized
