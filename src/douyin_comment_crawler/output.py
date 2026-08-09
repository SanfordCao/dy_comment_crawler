from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import json
import re

from PIL import Image, ImageOps


def safe_path_part(value: Any, fallback: str = "unknown") -> str:
    normalized = re.sub(r"[^\w.-]+", "-", str(value or ""), flags=re.UNICODE)
    normalized = normalized.strip("-")[:80]
    return normalized or fallback


def create_run_directory(
    output_root: Path,
    aweme_id: str,
    now: datetime | None = None,
) -> Path:
    root = output_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    base_name = f"{timestamp}-{safe_path_part(aweme_id, 'unknown-video')}"

    for suffix in range(10_000):
        name = base_name if suffix == 0 else f"{base_name}-{suffix}"
        run_directory = root / name
        try:
            run_directory.mkdir()
            return run_directory
        except FileExistsError:
            continue
    raise RuntimeError("无法创建唯一的输出目录")


def write_result_json(run_directory: Path, result: dict[str, Any]) -> Path:
    json_path = run_directory / "comments.json"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return json_path


def extension_from_content_type(content_type: str | None, source_url: str) -> str:
    normalized = (content_type or "").split(";", maxsplit=1)[0].strip().lower()
    type_map = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
        "image/bmp": ".bmp",
    }
    if normalized in type_map:
        return type_map[normalized]

    try:
        suffix = Path(urlparse(source_url).path).suffix.lower()
    except ValueError:
        suffix = ""
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def normalize_downloaded_image(
    content: bytes,
    content_type: str | None,
    source_url: str,
) -> tuple[bytes, str, str | None, str | None]:
    """Convert WebP bytes to a broadly viewable JPEG; leave other formats intact."""
    extension = extension_from_content_type(content_type, source_url)
    normalized_type = (content_type or "").split(";", maxsplit=1)[0].strip().lower()
    if normalized_type != "image/webp" and extension != ".webp":
        return content, extension, content_type, None

    with Image.open(BytesIO(content)) as source:
        source.seek(0)
        image = ImageOps.exif_transpose(source)
        if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
            rgba = image.convert("RGBA")
            converted = Image.new("RGB", rgba.size, "white")
            converted.paste(rgba, mask=rgba.getchannel("A"))
        else:
            converted = image.convert("RGB")

        target = BytesIO()
        converted.save(target, format="JPEG", quality=92, optimize=True)

    return target.getvalue(), ".jpg", "image/jpeg", "image/webp"
