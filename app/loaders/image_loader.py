"""图片加载器。

图片没有「文字层」，只能靠视觉模型（VLM）来读，所以这里的职责不是抽取，而是：

1. **校验**：确认这确实是一张能解码的图片（而不是改名的文件）；
2. **记录元信息**：格式、尺寸、是否透明 —— 前端要展示「这是什么图」；
3. **预处理**：把过大的图片缩到视觉模型的可用尺寸。

第 3 点经常被忽略却很关键：手机拍的照片动辄 4000×3000、几 MB，
直接 base64 塞进请求会超出多数接口的体积上限（或者被网关掐掉），
报出来的错误还往往语焉不详。**在本地先缩到 1600px 长边**能稳定解决这一类问题，
而且对字段抽取的准确率几乎没有影响 —— 视觉模型本来就会自己缩放。
"""

from __future__ import annotations

import io

from ..ir import Block, Document


class ImageDependencyError(RuntimeError):
    """缺少 Pillow。"""


class InvalidImage(ValueError):
    """文件不是可解码的图片。"""


def _require_pillow():
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise ImageDependencyError(
            "解析图片需要 Pillow，请执行：pip install -r requirements.txt"
        ) from exc
    return Image


def load_image(name: str, data: bytes) -> Document:
    """校验图片并生成 ``Document`` 骨架（真正的字段抽取由视觉模型完成）。"""
    Image = _require_pillow()

    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()  # 只校验完整性，verify 之后对象不可再用
    except Exception as exc:
        raise InvalidImage(f"无法识别为图片：{exc}") from exc

    # verify 之后必须重新打开才能读属性
    with Image.open(io.BytesIO(data)) as img:
        width, height = img.size
        fmt = (img.format or "unknown").lower()
        mode = img.mode
        has_alpha = mode in {"RGBA", "LA", "P"}

    doc = Document(name=name, kind="image", pages=1)
    doc.meta.update(
        {
            "format": fmt,
            "width": width,
            "height": height,
            "mode": mode,
            "has_alpha": has_alpha,
            "bytes": len(data),
            # 图片没有文字层，必须显式标记，否则用户只会看到空字段
            "needs_ocr": True,
        }
    )
    doc.blocks = [Block(kind="image", image_index=0, text="", page=1)]
    doc.add_warning(
        "图片需要通过视觉模型（VLM）读取内容。未配置视觉模型时只能读取图片元信息，"
        "无法抽取字段。"
    )
    return doc


def prepare_for_vision(
    data: bytes, *, max_edge: int = 1600, quality: int = 85
) -> tuple[bytes, str]:
    """把图片缩放到视觉模型友好的尺寸，返回 ``(字节, MIME)``。

    - 长边超过 ``max_edge`` 时等比缩小；
    - 带透明通道的图转成白底 JPEG（多数视觉接口不接受 PNG 透明通道，
      而透明区域被渲染成黑色会让文字读不出来）；
    - 原本就不大的图原样返回，避免无谓的重编码损失。
    """
    Image = _require_pillow()

    with Image.open(io.BytesIO(data)) as img:
        width, height = img.size
        has_alpha = img.mode in {"RGBA", "LA", "P"}
        needs_resize = max(width, height) > max_edge
        if not needs_resize and not has_alpha:
            # 小图直接用原字节，不做重编码
            return data, _guess_mime(img.format)

        if needs_resize:
            scale = max_edge / float(max(width, height))
            new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
            img = img.resize(new_size, Image.LANCZOS)

        if has_alpha:
            # 合成到白底，否则透明区域在不同实现里可能变成黑色
            background = Image.new("RGB", img.size, (255, 255, 255))
            converted = img.convert("RGBA")
            background.paste(converted, mask=converted.split()[-1])
            img = background
        elif img.mode not in {"RGB", "L"}:
            img = img.convert("RGB")

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def _guess_mime(fmt: str | None) -> str:
    mapping = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "JPG": "image/jpeg",
        "GIF": "image/gif",
        "BMP": "image/bmp",
        "WEBP": "image/webp",
        "TIFF": "image/tiff",
    }
    return mapping.get((fmt or "").upper(), "image/png")


__all__ = [
    "ImageDependencyError",
    "InvalidImage",
    "load_image",
    "prepare_for_vision",
]
