"""실행 결과에서 사람이 먼저 볼 페이지를 요약한다."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="documents.json 요약")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()

    documents_path = args.run_dir / "documents.json"
    documents = json.loads(documents_path.read_text(encoding="utf-8"))
    rows = []
    totals = {
        "pages": 0,
        "split_pages": 0,
        "regions": 0,
        "canonical_lines": 0,
        "unassigned_lines": 0,
        "text_conflicts_pending_vlm": 0,
        "ocr_fallback_regions": 0,
        "empty_regions": 0,
    }
    for document in documents:
        for page in document["pages"]:
            diagnostics = page["diagnostics"]
            row = {
                "source_file": page["source_file"],
                "page_no": page["page_no"],
                "pieces": page["tiling"]["pieces"],
                "regions": diagnostics["regions"],
                "canonical_lines": diagnostics["canonical_lines"],
                "unassigned_lines": diagnostics["unassigned_lines"],
                "text_conflicts_pending_vlm": diagnostics["text_conflicts_pending_vlm"],
                "ocr_fallback_regions": diagnostics["ocr_fallback_regions"],
                "empty_regions": diagnostics["empty_regions"],
            }
            rows.append(row)
            totals["pages"] += 1
            totals["split_pages"] += int(row["pieces"] > 1)
            for key in (
                "regions",
                "canonical_lines",
                "unassigned_lines",
                "text_conflicts_pending_vlm",
                "ocr_fallback_regions",
                "empty_regions",
            ):
                totals[key] += row[key]

    priority = sorted(
        rows,
        key=lambda row: (
            row["unassigned_lines"] + row["text_conflicts_pending_vlm"],
            row["unassigned_lines"],
        ),
        reverse=True,
    )
    summary = {"totals": totals, "review_priority": priority}
    target = args.run_dir / "review-summary.json"
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(totals, ensure_ascii=False, indent=2))
    print("\n우선 확인할 페이지")
    for row in priority[:15]:
        print(
            f"- {row['source_file']} p{row['page_no']}: "
            f"미배정 {row['unassigned_lines']}, 충돌 {row['text_conflicts_pending_vlm']}, "
            f"Region {row['regions']}, {row['pieces']}조각"
        )
    print(f"\n저장: {target}")


if __name__ == "__main__":
    main()
