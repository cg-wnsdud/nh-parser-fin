"""NH 광고물 파싱 실행기.

입력 렌더 → 종횡비 기반 통짜/타일 → PaddleX(서버 YAML 그대로) → 본문 우선 Region.
VLM 의미 판정(소유권·표·판독·템플릿·구분값)과 P1/P3 생성까지 항상 수행한다.

    python run.py --run-name <이름> --input "<파일 또는 폴더>"
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from nh_parser_fin.config import MEDIA_DIR, OUTPUT_ROOT, Profile
from nh_parser_fin.ingest.loader import iter_inputs, load_pages
from nh_parser_fin.ocr import paddlex as client
from nh_parser_fin.ocr import tiling, view
from nh_parser_fin.parse.adapters import build_page_evidence, dedupe_ocr_lines, digital_text_needs_ocr


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe(text: str) -> str:
    value = "".join(c if c.isalnum() or c in "-_." else "_" for c in text).strip("._")
    return value[:80] or "page"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NH 광고물 파싱 실행")
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--paddlex-url", default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--aspect-limit", type=float, default=None)
    parser.add_argument("--tile-span", type=int, default=None)
    parser.add_argument("--sizing", choices=("asis", "maxside"), default="asis")
    parser.add_argument("--max-side", type=int, default=2500)
    parser.add_argument(
        "--compact-output", action="store_true",
        help="파일별 P1/P3·렌더 이미지·HTML 보고서만 저장",
    )
    parser.add_argument(
        "--template-id", default=None,
        help="사용자가 지정한 광고 템플릿 ID. 주면 모든 상품에 이 템플릿을 쓰고, 없으면 상품별로 자동 선택",
    )
    args = parser.parse_args()
    if args.template_id is not None:
        from nh_parser_fin.review.catalog import load_catalog

        known = load_catalog().get("templates") or {}
        if args.template_id not in known:
            parser.error(
                f"알 수 없는 --template-id {args.template_id!r}. "
                f"카탈로그 템플릿: {', '.join(known)}"
            )
    return args


def _profile(args: argparse.Namespace) -> Profile:
    base = Profile.from_env()
    return Profile(
        paddlex_url=args.paddlex_url or base.paddlex_url,
        timeout=args.timeout or base.timeout,
        aspect_limit=args.aspect_limit or base.aspect_limit,
        tile_span=args.tile_span or base.tile_span,
        encode=base.encode,
    )


def _digital_lines(pdf, page_no: int, origin: dict, supplied: list[dict] | None = None) -> list[dict]:
    """선택 가능한 PDF 텍스트를 같은 페이지 픽셀 좌표로 읽는다."""
    if supplied is not None:
        return list(supplied)
    if pdf is None or origin.get("triage") not in ("structured", "hybrid"):
        return []
    if origin.get("pdf_digital_text") == "disabled":
        # PARSER_V2_DIGITAL_MODE=off: PDF 입력은 재추출하지 않는다.
        return []
    from nh_parser_fin.ingest.triage import extract_digital_lines

    px_per_pt = float(origin.get("dpi_used") or 200) / 72.0
    return [line.model_dump(mode="json") for line in extract_digital_lines(pdf[page_no - 1], px_per_pt)]


def _axis_share(a0: int, a1: int, b0: int, b1: int) -> float:
    return max(0, min(a1, b1) - max(a0, b0)) / max(1, min(a1 - a0, b1 - b0))


def _normalize(value) -> str:
    return "".join(str(value or "").split()).casefold()


def character_coverage(left: str, right: str) -> float:
    """OCR 오탈자를 허용하며 left가 right로 얼마나 설명되는지 계산한다."""
    if not left or not right:
        return 0.0
    shared = sum((Counter(left) & Counter(right)).values())
    return shared / len(left)


def _novel_visual_blocks(
    visual: list[dict], structured: list[dict],
) -> tuple[list[dict], int]:
    """구조 Region에 이미 표현된 Paddle 블록을 버리고 시각 전용 요소만 남긴다."""
    normalize = _normalize

    kept: list[dict] = []
    discarded = 0
    for block in visual:
        # Paddle의 표 HTML은 OCR 텍스트와 태그가 섞인 별도 추정 결과다. HWP DOM이
        # 이미 원본 표 행을 제공할 때 이를 한 행의 새 본문으로 붙이면 중복·태그가
        # P3까지 유출된다. 시각 요소 보완에는 순수 텍스트 블록만 사용한다.
        if "<table" in str(block.get("content") or "").casefold() and any(
            item.get("kind") == "table" and item.get("table") for item in structured
        ):
            discarded += 1
            continue
        bbox = block.get("bbox") or []
        text = normalize(block.get("content"))
        duplicate = False
        enclosed_texts: list[str] = []
        if len(bbox) == 4:
            bx0, by0, bx1, by1 = (int(value) for value in bbox)
            block_area = max(1, (bx1 - bx0) * (by1 - by0))
            for known in structured:
                other = known.get("bbox") or []
                if len(other) != 4:
                    continue
                ax0, ay0, ax1, ay1 = (int(value) for value in other)
                x_share = _axis_share(ax0, ax1, bx0, bx1)
                y_share = _axis_share(ay0, ay1, by0, by1)
                if x_share < 0.8 or y_share < 0.8:
                    continue
                known_area = max(1, (ax1 - ax0) * (ay1 - ay0))
                intersection = (
                    max(0, min(ax1, bx1) - max(ax0, bx0))
                    * max(0, min(ay1, by1) - max(ay0, by0))
                )
                known_text = normalize(known.get("content"))
                if intersection / known_area >= 0.8 and known_text:
                    enclosed_texts.append(known_text)
                area_ratio = min(block_area, known_area) / max(block_area, known_area)
                same_geometry = x_share >= 0.95 and y_share >= 0.95 and area_ratio >= 0.75
                text_already_present = bool(text and known_text and text in known_text)
                same_text_with_ocr_noise = (
                    x_share >= 0.9
                    and y_share >= 0.9
                    and len(text) >= 8
                    and character_coverage(text, known_text) >= 0.8
                    and (
                        character_coverage(known_text, text) >= 0.8
                        # 시각 블록이 구조 행의 일부만 잘라 잡은 경우도 중복이다.
                        or character_coverage(text, known_text) >= 0.85
                    )
                )
                if same_geometry or text_already_present or same_text_with_ocr_noise:
                    duplicate = True
                    break
            # Paddle은 HWP 한 페이지의 표 전체를 text 블록 하나로 되돌려주기도 한다.
            # 구조 파서는 이미 그 안을 행별 exact/display_exact bbox로 나눴으므로, 큰
            # 블록의 내용 대부분이 내부 행들의 합집합에 있으면 보완 요소가 아니라
            # 중복 컨테이너다. 문자 multiset을 쓰는 이유는 OCR의 줄 순서 뒤섞임과
            # 띄어쓰기 차이를 허용하기 위해서다.
            if (
                not duplicate
                and len(enclosed_texts) >= 3
                and len(text) >= 20
                and character_coverage(text, "".join(enclosed_texts)) >= 0.72
            ):
                duplicate = True
        if duplicate:
            discarded += 1
        else:
            kept.append(block)
    return kept, discarded


def _alnum(value) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", str(value or "")).casefold()


def _structure_lines(structure: dict | None) -> list[str]:
    """HWP 원본 구조의 문단·칸 문구를 줄 단위로 편다. 띄어쓰기는 원본 그대로다."""
    lines: list[str] = []
    for paragraph in (structure or {}).get("paragraphs") or []:
        lines.extend(str(paragraph.get("text") or "").splitlines())
    for table in (structure or {}).get("tables") or []:
        for cell in table.get("cells") or []:
            lines.extend(str(cell.get("text") or "").splitlines())
    return list(dict.fromkeys(
        " ".join(line.split()) for line in lines if len(_alnum(line)) >= 4
    ))


def _structure_text_for(render: str, lines: list[str]) -> str | None:
    """렌더 글자가 가리키는 원본 줄을 찾는다. 렌더 텍스트층은 띄어쓰기가 빠지고
    줄이 단어 중간에서 끊기므로 위치 확인에만 쓰고, 문구는 원본 줄을 쓴다."""
    target = _alnum(render)
    if not target:
        return None
    picked: list[tuple[int, int, str]] = []
    for line in sorted(lines, key=lambda value: -len(_alnum(value))):
        key = _alnum(line)
        start = target.find(key)
        if start < 0:
            continue
        end = start + len(key)
        if any(start < b and end > a for a, b, _ in picked):
            continue
        picked.append((start, end, line))
    covered = sum(b - a for a, b, _ in picked)
    if covered / len(target) < 0.8:
        return None
    return "\n".join(line for _, _, line in sorted(picked))


def _render_text_inside(bbox: list, digital_lines: list[dict]) -> str:
    """렌더 PDF 텍스트층에서 블록 안에 중심이 든 줄을 읽기 순서로 잇는다."""
    x0, y0, x1, y1 = (int(value) for value in bbox)
    inside = []
    for line in digital_lines:
        box = line.get("bbox") or []
        text = str(line.get("text") or "").strip()
        if len(box) != 4 or not text:
            continue
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            inside.append((box[1], box[0], text))
    return "\n".join(text for _, _, text in sorted(inside))


def _attach_visual_supplements(
    visual: list[dict], structured: list[dict], digital_lines: list[dict] | None = None,
    structure: dict | None = None,
) -> tuple[list[dict], int]:
    """구조 표 행 안에서 Paddle이 찾은 새 문구를 HWP 원문으로만 보완한다.

    HWP 구조 행이 놓친 글자(예: 중첩 표 바깥의 각주 문단)는 원본 구조와 렌더 PDF
    텍스트층에 있다. OCR 문구를 붙이면 오독(`적용요을`)과 중복(`|지점명]…`)이 P3로
    새므로 쓰지 않는다.
    - 행 문구가 이미 담은 내용이면 버린다.
    - 블록 안 렌더 글자로 위치를 확인하고, 그 글자에 맞는 원본 구조 줄을 붙인다.
      원본 줄을 찾지 못하면 렌더 글자를 붙인다(띄어쓰기가 빠질 수 있음).
    - 렌더 글자가 없으면 이미지 속 글자다. 행에 붙이지 않고 별도 시각 Region으로 남긴다.
    OCR 문구는 P1 ``visual_supplements``에 위치와 함께 근거로만 남는다.
    """
    lines = _structure_lines(structure)
    free: list[dict] = []
    attached = 0
    for block in visual:
        bbox = block.get("bbox") or []
        text = str(block.get("content") or "").strip()
        candidates: list[tuple[int, dict]] = []
        if len(bbox) == 4 and text:
            bx0, by0, bx1, by1 = (int(value) for value in bbox)
            block_area = max(1, (bx1 - bx0) * (by1 - by0))
            for known in structured:
                if known.get("kind") != "table" or not known.get("table"):
                    continue
                other = known.get("bbox") or []
                if len(other) != 4:
                    continue
                ax0, ay0, ax1, ay1 = (int(value) for value in other)
                intersection = (
                    max(0, min(ax1, bx1) - max(ax0, bx0))
                    * max(0, min(ay1, by1) - max(ay0, by0))
                )
                if intersection / block_area >= 0.8:
                    candidates.append((max(1, (ax1 - ax0) * (ay1 - ay0)), known))
        if not candidates:
            free.append(block)
            continue
        _, target = min(candidates, key=lambda item: item[0])
        record = {"bbox": list(bbox), "ocr_text": text, "source": "paddlex_visual"}
        if character_coverage(_normalize(text), _normalize(target.get("content"))) >= 0.8:
            record["action"] = "duplicate_dropped"
            target.setdefault("visual_supplements", []).append(record)
            attached += 1
            continue
        render = _render_text_inside(bbox, digital_lines or [])
        if not render:
            record["action"] = "kept_as_image_text"
            target.setdefault("visual_supplements", []).append(record)
            free.append(block)
            continue
        original = _structure_text_for(render, lines)
        text, kind = (original, "structure_text") if original else (render, "render_text")
        record.update(render_text=render, text=text)
        if _alnum(text) in _alnum(target.get("content")):
            record["action"] = "duplicate_dropped"
        else:
            target["content"] = f"{str(target.get('content') or '').rstrip()}\n{text}".strip()
            target["text_source"] = f"document_processor_html_with_{kind}"
            table = target.setdefault("table", {})
            notes = table.setdefault("notes", [])
            if text not in notes:
                notes.append(text)
            record["action"] = f"{kind}_attached"
        target.setdefault("visual_supplements", []).append(record)
        attached += 1
    return free, attached


def main() -> None:
    args = _args()
    profile = _profile(args)
    sources = iter_inputs(list(args.input), list(args.exclude))
    out = OUTPUT_ROOT / args.run_name
    media_dir = out / "images" if args.compact_output else MEDIA_DIR
    media_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    documents, tasks = [], []
    all_labels = [
        "paddlex_content", "digital_ocr_lines", "digital_ocr_fallback", "ocr_fallback",
        "empty", "unassigned_line",
    ]

    for source in sources:
        pdf = None
        if source.suffix.lower() == ".pdf":
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(str(source))
        page_outputs = []
        for page in load_pages(source, sizing=args.sizing, max_side=args.max_side):
            key = f"{_safe(page.doc_id)}__p{page.page_no:03d}"
            det, parsing, visual_parsing, lines = [], [], [], []
            call_seconds = 0.0
            if page.structured_route == "structured_fast":
                pieces = []
                parsing = [dict(block) for block in page.structured_blocks]
                tile_note = {
                    "decision": "structured",
                    "axis": None,
                    "pieces": 0,
                    "reason": "document_processor의 디지털 문단/표 행 bbox 사용",
                }
            else:
                pieces, tile_note = tiling.plan(
                    page.image,
                    mode="auto",
                    aspect_limit=profile.aspect_limit,
                    span_override=profile.tile_span,
                )
                for piece in pieces:
                    response = client.call(
                        piece.image,
                        url=profile.paddlex_url,
                        payload=profile.request_payload,
                        timeout=profile.timeout,
                        fmt=profile.encode,
                    )
                    call_seconds += response["seconds"]
                    if not args.compact_output:
                        _write_json(out / "raw" / f"{key}_t{piece.index:02d}.json", response["pruned"])
                    for found, target in (
                        (client.det_boxes(response["pruned"]), det),
                        (client.parsing_boxes(response["pruned"]), visual_parsing),
                        (client.ocr_lines(response["pruned"]), lines),
                    ):
                        for box in found:
                            moved = tiling.shift(box, piece.x_offset, piece.y_offset)
                            moved["piece"] = piece.index
                            target.append(moved)

            det_before = len(det)
            parsing_before = len(visual_parsing) + len(page.structured_blocks)
            lines_before = len(lines)
            if len(pieces) > 1:
                det, merged_det = tiling.dedupe(det)
                visual_parsing, merged_parsing = tiling.dedupe(visual_parsing)
                lines, merged_lines = dedupe_ocr_lines(lines)
            else:
                merged_det = merged_parsing = merged_lines = 0
            if page.structured_route == "hybrid" and page.structured_blocks:
                novel, suppressed = _novel_visual_blocks(
                    visual_parsing, page.structured_blocks,
                )
                novel, attached = _attach_visual_supplements(
                    novel, page.structured_blocks, page.digital_lines, page.hwp_structure,
                )
                parsing = [dict(block) for block in page.structured_blocks] + novel
                merged_parsing += suppressed + attached
            elif page.structured_route != "structured_fast":
                parsing = visual_parsing

            digital = _digital_lines(
                pdf, page.page_no, page.origin,
                page.digital_lines or None,
            )
            # 조각 오프셋까지 적용된 **페이지 좌표** 박스를 남긴다. `replay.py` 가
            # PaddleX 재호출 없이 조립 로직만 다시 돌릴 때 쓰는 입력이다.
            boxes_record = {
                "canvas": list(page.image.size),
                "page_no": page.page_no,
                "source_file": page.source_file,
                "parsing": parsing,
                "ocr_lines": lines,
                "digital_lines": digital,
                "structured_route": page.structured_route,
                "structured_blocks": page.structured_blocks,
                "structure_probe": page.structure_probe,
            }
            if not args.compact_output:
                _write_json(out / "boxes" / f"{key}.json", boxes_record)
            # anchor 모드(기본): 디지털 줄은 정본 조립에 넣지 않고 글자 교정 재료로만 넘긴다
            # (parse/digital_anchor.py). PDF 입력에만 적용한다. HWP를 렌더한 PDF의 텍스트층은
            # 문서 원본 글자라 primary 모드처럼 정본으로 둔다 — 빼면 OCR 문구가 HWP 구조 검증을 통과해 `□`·`▶`·
            # 띄어쓰기가 빠진 채 보존된다(실측: `12. 예금성상품-입출식 광고7 (1).hwp`).
            anchor_lines: list[dict] = []
            if page.origin.get("pdf_digital_text") == "anchor" and digital:
                anchor_lines, digital = digital, []
                if digital_text_needs_ocr(anchor_lines, lines):
                    # 글자 매핑이 깨진 텍스트층은 main과 같은 기준으로 버린다.
                    anchor_lines = []
            evidence = build_page_evidence(
                parsing,
                lines,
                page_no=page.page_no,
                canvas=list(page.image.size),
                digital_lines=digital,
            )
            if anchor_lines:
                evidence["digital_anchor_lines"] = [
                    {"text": line.get("text"), "bbox": line.get("bbox")} for line in anchor_lines
                ]
            evidence.update({
                "source_file": page.source_file,
                "origin": page.origin,
                "processing_route": page.structured_route,
                "structure_probe": page.structure_probe,
                "tiling": tile_note,
                "request_payload": profile.request_payload,
                "seconds": round(call_seconds, 3),
                "raw_counts": {
                    "det_before_dedupe": det_before,
                    "det": len(det),
                    "det_merged": merged_det,
                    "parsing_before_dedupe": parsing_before,
                    "parsing": len(parsing),
                    "parsing_merged": merged_parsing,
                    "ocr_before_dedupe": lines_before,
                    "ocr": len(lines),
                    "ocr_merged": merged_lines,
                },
                "raw_observations": {"layout_det_res": det, "parsing_res_list": parsing},
            })
            if page.hwp_structure is not None:
                evidence["hwp_structure"] = page.hwp_structure
            if not args.compact_output:
                _write_json(out / "pages" / f"{key}.json", evidence)

            media_name = f"parser_v2_{args.run_name}__{key}.png"
            media_path = media_dir / media_name
            if not media_path.exists():
                page.image.save(media_path, format="PNG", optimize=False)
            for box in parsing:
                label = str(box.get("label") or "unknown")
                if label not in all_labels:
                    all_labels.append(label)
            assigned_boxes = [
                {
                    "bbox": region["bbox"],
                    "label": region["text_source"].replace("paddlex_block_content", "paddlex_content"),
                    "content": region["text"],
                    "source": region["text_source"],
                }
                for region in evidence["regions"]
            ]
            unassigned_boxes = [
                {**line, "label": "unassigned_line"}
                for line in evidence["unassigned_lines"]
            ]
            width, height = page.image.size
            tasks.append({
                "data": {
                    "image": f"/data/local-files/?d=pages/{media_name}",
                    "source_file": page.source_file,
                    "page_no": page.page_no,
                    "run_name": args.run_name,
                },
                "predictions": [
                    {
                        "model_version": f"{args.run_name} 1-raw-parsing",
                        "result": [
                            r for i, box in enumerate(parsing)
                            if (r := view.rectangle(box, width, height, box_id=f"{key}_raw{i:03d}"))
                        ],
                    },
                    {
                        "model_version": f"{args.run_name} 2-canonical-regions",
                        "result": [
                            r for i, box in enumerate(assigned_boxes)
                            if (r := view.rectangle(box, width, height, box_id=f"{key}_reg{i:03d}"))
                        ],
                    },
                    {
                        "model_version": f"{args.run_name} 3-unassigned",
                        "result": [
                            r for i, box in enumerate(unassigned_boxes)
                            if (r := view.rectangle(box, width, height, box_id=f"{key}_un{i:03d}"))
                        ],
                    },
                ],
            })
            page_outputs.append(evidence)
            if tile_note["decision"] == "structured":
                decision = "디지털 구조"
            elif tile_note["decision"] == "whole":
                decision = "통짜"
            else:
                decision = f"{tile_note['pieces']}조각"
            print(
                f"· {page.source_file} p{page.page_no}: {decision}, "
                f"Region {len(evidence['regions'])}, 미배정 {len(evidence['unassigned_lines'])}"
            )
        documents.append({"source_file": source.name, "pages": page_outputs})

    manifest = {
        "run_name": args.run_name,
        "profile": profile.manifest(),
        "sizing": args.sizing,
        "max_side": args.max_side,
        "elapsed_seconds": round(time.time() - started, 3),
        "documents": len(documents),
        "pages": len(tasks),
    }
    _write_json(out / "manifest.json", manifest)
    _write_json(out / "media-index.json", [
        {
            "source_file": task["data"]["source_file"],
            "page_no": task["data"]["page_no"],
            "image_name": str(task["data"]["image"]).split("pages/", 1)[-1],
        }
        for task in tasks
    ])
    if not args.compact_output:
        _write_json(out / "documents.json", documents)
        _write_json(out / "label-studio.json", tasks)
        labeling_config = view.labeling_config(all_labels).replace(
            "1-det / 2-parsing / 3-ocr / 4-unassigned",
            "1-raw parsing / 2-text source / 3-unassigned",
        )
        (out / "labeling-config.xml").write_text(labeling_config, encoding="utf-8")
    from nh_parser_fin.parse.pipeline import run_full_pipeline

    p1, p3 = run_full_pipeline(
        documents, tasks, out=out, media_dir=media_dir,
        compact_output=args.compact_output, template_id=args.template_id,
    )
    print(f"P1/P3: 문서 {len(p1)}개 / {len(p3)}개")
    if args.compact_output:
        from nh_parser_fin.ocr.table_preview import write_table_previews
        from report import build as build_report

        media_map = {
            (str(task["data"]["source_file"]), int(task["data"]["page_no"])):
            media_dir / str(task["data"]["image"]).split("pages/", 1)[-1]
            for task in tasks
        }
        write_table_previews(p1, media_map, out)
        (out / "report.html").write_text(
            build_report(out, f"NH 광고물 파싱 — {args.run_name}"), encoding="utf-8",
        )
    print(f"\n완료: {out}")
    if not args.compact_output:
        print(f"Label Studio: {out / 'label-studio.json'}")


if __name__ == "__main__":
    main()
