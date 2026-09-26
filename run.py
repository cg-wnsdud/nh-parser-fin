"""NH 광고물 파싱 실행기.

입력 렌더 → 종횡비 기반 통짜/타일 → PaddleX(서버 YAML 그대로) → 본문 우선 Region.
``--with-vlm`` 이면 VLM 의미 판정(소유권·표·판독·템플릿·구분값)과 P1/P3 까지 잇는다.

    python run.py --run-name <이름> --with-vlm --input "<파일 또는 폴더>"
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from nh_parser_fin.config import MEDIA_DIR, OUTPUT_ROOT, Profile
from nh_parser_fin.ingest.loader import iter_inputs, load_pages
from nh_parser_fin.ocr import paddlex as client
from nh_parser_fin.ocr import tiling, view
from nh_parser_fin.parse.adapters import build_page_evidence, dedupe_ocr_lines


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
        "--with-vlm", action="store_true",
        help="기존 fc87 Gemma로 소유권·텍스트 Judge·복수 라벨을 판정하고 P1/P3까지 생성",
    )
    return parser.parse_args()


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
    from nh_parser_fin.ingest.triage import extract_digital_lines

    px_per_pt = float(origin.get("dpi_used") or 200) / 72.0
    return [line.model_dump(mode="json") for line in extract_digital_lines(pdf[page_no - 1], px_per_pt)]


def _axis_share(a0: int, a1: int, b0: int, b1: int) -> float:
    return max(0, min(a1, b1) - max(a0, b0)) / max(1, min(a1 - a0, b1 - b0))


def _novel_visual_blocks(
    visual: list[dict], structured: list[dict],
) -> tuple[list[dict], int]:
    """구조 Region에 이미 표현된 Paddle 블록을 버리고 시각 전용 요소만 남긴다."""
    normalize = lambda value: "".join(str(value or "").split()).casefold()

    def character_coverage(left: str, right: str) -> float:
        """OCR 오탈자를 허용하며 left가 right로 얼마나 설명되는지 계산한다."""
        if not left or not right:
            return 0.0
        shared = sum((Counter(left) & Counter(right)).values())
        return shared / len(left)

    kept: list[dict] = []
    discarded = 0
    for block in visual:
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


def _attach_visual_supplements(
    visual: list[dict], structured: list[dict],
) -> tuple[list[dict], int]:
    """구조 표 행 안의 신규 시각 문구를 별도 겹침 Region 대신 행 주석으로 합친다."""
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
        target["content"] = f"{str(target.get('content') or '').rstrip()}\n{text}".strip()
        target["text_source"] = "document_processor_html_with_visual_supplement"
        table = target.setdefault("table", {})
        notes = table.setdefault("notes", [])
        if text not in notes:
            notes.append(text)
        target.setdefault("visual_supplements", []).append({
            "bbox": list(bbox), "text": text, "source": "paddlex_visual",
        })
        attached += 1
    return free, attached


def main() -> None:
    args = _args()
    profile = _profile(args)
    sources = iter_inputs(list(args.input), list(args.exclude))
    out = OUTPUT_ROOT / args.run_name
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
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
                    novel, page.structured_blocks,
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
            _write_json(out / "boxes" / f"{key}.json", {
                "canvas": list(page.image.size),
                "page_no": page.page_no,
                "source_file": page.source_file,
                "parsing": parsing,
                "ocr_lines": lines,
                "digital_lines": digital,
                "structured_route": page.structured_route,
                "structured_blocks": page.structured_blocks,
                "structure_probe": page.structure_probe,
            })
            evidence = build_page_evidence(
                parsing,
                lines,
                page_no=page.page_no,
                canvas=list(page.image.size),
                digital_lines=digital,
            )
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
            _write_json(out / "pages" / f"{key}.json", evidence)

            media_name = f"parser_v2_{args.run_name}__{key}.png"
            media_path = MEDIA_DIR / media_name
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
    _write_json(out / "documents.json", documents)
    _write_json(out / "label-studio.json", tasks)
    labeling_config = view.labeling_config(all_labels).replace(
        "1-det / 2-parsing / 3-ocr / 4-unassigned",
        "1-raw parsing / 2-text source / 3-unassigned",
    )
    (out / "labeling-config.xml").write_text(labeling_config, encoding="utf-8")
    if args.with_vlm:
        from nh_parser_fin.parse.pipeline import run_full_pipeline

        p1, p3 = run_full_pipeline(
            documents, tasks, out=out, media_dir=MEDIA_DIR,
        )
        print(f"P1/P3: 문서 {len(p1)}개 / {len(p3)}개")
    print(f"\n완료: {out}")
    print(f"Label Studio: {out / 'label-studio.json'}")


if __name__ == "__main__":
    main()
