"""Paddle이 표라고 표시한 원본 페이지 위치를 사람이 확인할 PNG로 남긴다."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


def _safe(name: str) -> str:
    return "".join(char if char.isalnum() or char in "-_ ." else "_" for char in name).strip()[:100]


def write_table_previews(
    documents: list[dict[str, Any]],
    media: dict[tuple[str, int], Path],
    out: Path,
) -> list[dict[str, Any]]:
    """P1의 원시 레이아웃 결과만 사용한다. VLM 후보와 혼동하지 않는다."""
    preview_dir = out / "paddle-tables"
    preview_dir.mkdir(parents=True, exist_ok=True)
    index: list[dict[str, Any]] = []
    try:
        font = ImageFont.load_default(size=25)
    except TypeError:
        font = ImageFont.load_default()
    for document in documents:
        source = str(document["source_file"])
        for page in document.get("pages") or []:
            page_no = int(page["page_no"])
            original = media.get((source, page_no))
            if original is None or not original.exists():
                continue
            candidates = [
                block for block in (page.get("raw_observations") or {}).get("parsing_res_list") or []
                if str(block.get("label") or "").casefold() == "table"
                and not block.get("structured")
                and len(block.get("bbox") or []) == 4
            ]
            with Image.open(original) as source_image:
                image = source_image.convert("RGB")
            draw = ImageDraw.Draw(image)
            crops: list[str] = []
            base = f"{_safe(source)}__p{page_no:03d}"
            for candidate_no, block in enumerate(candidates, start=1):
                x0, y0, x1, y1 = (int(value) for value in block["bbox"])
                x0, y0 = max(0, x0), max(0, y0)
                x1, y1 = min(image.width, x1), min(image.height, y1)
                if x1 <= x0 or y1 <= y0:
                    continue
                crop_name = f"{base}__table{candidate_no:02d}.png"
                # 확대본에는 bbox 주변 문맥을 조금 남긴다.
                margin = 25
                with Image.open(original) as source_image:
                    crop = source_image.convert("RGB").crop((
                        max(0, x0 - margin), max(0, y0 - margin),
                        min(image.width, x1 + margin), min(image.height, y1 + margin),
                    ))
                crop.save(preview_dir / crop_name, format="PNG")
                crops.append(crop_name)
                draw.rectangle((x0, y0, x1, y1), outline="#f44336", width=max(4, image.width // 280))
                tag = f"Paddle table {candidate_no}"
                ty = max(0, y0 - 32)
                label_box = draw.textbbox((x0, ty), tag, font=font)
                draw.rectangle(label_box, fill="#f44336")
                draw.text((x0, ty), tag, fill="white", font=font)
            full_name = f"{base}__full.png"
            image.save(preview_dir / full_name, format="PNG")
            index.append({
                "source_file": source,
                "page_no": page_no,
                "whole_page_input": str((page.get("tiling") or {}).get("decision") or "") == "whole",
                "tiling": (page.get("tiling") or {}).get("decision"),
                "paddle_table_count": len(candidates),
                "candidate_bboxes": [list(block["bbox"]) for block in candidates],
                "full_image": full_name,
                "crops": crops,
            })
    (out / "paddle-table-index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return index
