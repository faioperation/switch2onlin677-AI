"""
core/image_utils.py
====================
Image normalization utilities shared by the chat endpoint and the
/convert-image utility endpoint.

Handles PNG, JPEG, WEBP, GIF, and HEIC/HEIF image types.
HEIC images are automatically converted to JPEG before being sent to OpenAI.
"""
from __future__ import annotations

import base64
import re
from io import BytesIO

from fastapi import HTTPException
from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener as _register
    _register()
except ImportError:
    pass  # HEIC support unavailable — HEIC uploads will fail gracefully

# ── MIME type sets ─────────────────────────────────────────────────────────────

SUPPORTED_IMAGE_MIMES: frozenset[str] = frozenset({
    "image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif",
})

HEIC_IMAGE_MIMES: frozenset[str] = frozenset({
    "image/heic", "image/heif", "image/heic-sequence",
    "image/heif-sequence", "image/x-heic", "image/x-heif",
})

_GENERIC_MIMES: frozenset[str] = frozenset({
    "", "application/octet-stream", "binary/octet-stream",
})

_HEIF_BRANDS: frozenset[bytes] = frozenset({
    b"heic", b"heix", b"hevc", b"hevx",
    b"heim", b"heis", b"hevm", b"hevs",
    b"mif1", b"msf1",
})


# ── Detection helpers ──────────────────────────────────────────────────────────

def looks_like_heif(image_bytes: bytes) -> bool:
    """Return True if the raw bytes look like a HEIC/HEIF file by magic bytes."""
    if len(image_bytes) < 12:
        return False
    return image_bytes[4:8] == b"ftyp" and (
        image_bytes[8:12] in _HEIF_BRANDS
        or any(brand in image_bytes[12:64] for brand in _HEIF_BRANDS)
    )


def _pil_to_jpeg_data_url(image_bytes: bytes) -> str:
    """Convert arbitrary image bytes → JPEG data URL via Pillow."""
    image = Image.open(BytesIO(image_bytes))
    image = ImageOps.exif_transpose(image)

    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        bg = Image.new("RGB", image.size, (255, 255, 255))
        bg.paste(image, mask=image.split()[-1])
        image = bg
    else:
        image = image.convert("RGB")

    buf = BytesIO()
    image.save(buf, format="JPEG", quality=90)
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"


# ── Public API ─────────────────────────────────────────────────────────────────

def normalize_image_for_openai(data_url: str) -> str:
    """
    Accept any image data URL and return one that OpenAI's vision API can consume.
    HEIC/HEIF images are converted to JPEG. Supported types are passed through.
    Raises HTTP 400 for unsupported or corrupt images.
    """
    if not data_url or not data_url.startswith("data:"):
        return data_url

    match = re.match(r"data:(.*?);base64,(.*)$", data_url, re.DOTALL)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid image format.")

    mime_type   = match.group(1).lower()
    base64_data = re.sub(r"\s+", "", match.group(2))

    if mime_type in SUPPORTED_IMAGE_MIMES:
        return data_url

    try:
        image_bytes = base64.b64decode(base64_data)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid image data. Please upload JPG, PNG, WEBP, GIF, or HEIC. Error: {exc}",
        )

    is_heic = mime_type in HEIC_IMAGE_MIMES or (
        mime_type in _GENERIC_MIMES and looks_like_heif(image_bytes)
    )

    if not is_heic:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image type: {mime_type or 'unknown'}. "
                   "Please upload JPG, PNG, WEBP, GIF, or HEIC.",
        )

    try:
        return _pil_to_jpeg_data_url(image_bytes)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not process HEIC image. Please upload JPG or PNG. Error: {exc}",
        )


def make_db_thumbnail(image_bytes: bytes, max_size: int = 300) -> str | None:
    """
    Compress raw image bytes into a small JPEG data URL for DB history storage.
    Returns None if the image cannot be processed (non-critical path).
    """
    try:
        img = Image.open(BytesIO(image_bytes))
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        buf = BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=75)
        return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"
    except Exception:
        return None
