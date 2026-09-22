"""NH 광고물 파싱 실행기.

입력 렌더 → 종횡비 기반 통짜/타일 → PaddleX(서버 YAML 그대로) → 본문 우선 Region.
``--with-vlm`` 이면 VLM 의미 판정(소유권·표·판독·템플릿·구분값)과 P1/P3 까지 잇는다.

    python run.py --run-name <이름> --with-vlm --input "<파일 또는 폴더>"
"""
from __future__ import annotations

import argparse
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
            pieces, tile_note = tiling.plan(
                page.image,
                mode="auto",
                aspect_limit=profile.aspect_limit,
                span_override=profile.tile_span,
            )
            det, parsing, lines = [], [], []
            call_seconds = 0.0
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
                    (client.parsing_boxes(response["pruned"]), parsing),
                    (client.ocr_lines(response["pruned"]), lines),
                ):
                    for box in found:
                        moved = tiling.shift(box, piece.x_offset, piece.y_offset)
                        moved["piece"] = piece.index
                        target.append(moved)

            det_before, parsing_before, lines_before = len(det), len(parsing), len(lines)
            if len(pieces) > 1:
                det, merged_det = tiling.dedupe(det)
                parsing, merged_parsing = tiling.dedupe(parsing)
                lines, merged_lines = dedupe_ocr_lines(lines)
            else:
                merged_det = merged_parsing = merged_lines = 0

            digital = _digital_lines(
                pdf, page.page_no, page.origin,
                page.digital_lines if source.suffix.lower() in (".hwp", ".hwpx") else None,
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
            decision = "통짜" if tile_note["decision"] == "whole" else f"{tile_note['pieces']}조각"
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
