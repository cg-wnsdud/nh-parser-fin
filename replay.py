"""저장해 둔 PaddleX 응답으로 Region 조립만 다시 돌린다.

PaddleX·VLM 호출 없이 텍스트 후보 선택과 줄 소유권 로직을 반복 시험할 때 쓴다.
`run.py` 가 남긴 `outputs/<실행이름>/boxes/` 를 입력으로 받는다 —
조각 오프셋까지 적용된 페이지 좌표 박스라 그대로 다시 조립할 수 있다.

    python replay.py --from <이전 실행> --run-name <새 이름>
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from nh_parser_fin.config import OUTPUT_ROOT
from nh_parser_fin.parse.adapters import build_page_evidence


def main() -> None:
    parser = argparse.ArgumentParser(description="저장된 PaddleX 응답 → Region 재조립")
    parser.add_argument("--from", dest="source", required=True, help="이전 실행 이름")
    parser.add_argument("--run-name", required=True)
    args = parser.parse_args()

    root = OUTPUT_ROOT / args.source / "boxes"
    if not root.is_dir():
        raise SystemExit(f"이전 실행의 boxes 가 없습니다: {root}")
    out = OUTPUT_ROOT / args.run_name
    totals: Counter[str] = Counter()
    pages = []
    for path in sorted(root.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        rebuilt = build_page_evidence(
            raw.get("parsing") or [],
            raw.get("ocr_lines") or [],
            page_no=int(raw.get("page_no") or 1),
            canvas=raw.get("canvas") or [0, 0],
            digital_lines=raw.get("digital_lines") or [],
        )
        rebuilt["source_file"] = raw.get("source_file")
        rebuilt["source_boxes"] = path.name
        pages.append(rebuilt)
        totals.update(rebuilt["diagnostics"])

    out.mkdir(parents=True, exist_ok=True)
    (out / "replayed-pages.json").write_text(
        json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {"source_run": args.source, "pages": len(pages), "totals": dict(totals)}
    (out / "replay-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
