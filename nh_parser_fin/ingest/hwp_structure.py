"""Kordoc HWP/HWPX 구조 출력을 파이프라인 내부 형식으로 정규화한다.

Kordoc은 문단·표·병합 셀을 잘 읽지만 화면 좌표를 주지 않는다. 이 모듈은 그 결과를
좌표 없는 구조 근거로만 만든다. 렌더링과 bbox 생성은 ``hwp_render``와 기존 PDF 경로가
담당한다.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_KORDOC = "npx --yes kordoc@4.14.1"


def _command() -> list[str]:
    configured = os.environ.get("KORDOC_COMMAND", DEFAULT_KORDOC).strip()
    command = shlex.split(configured, posix=False)
    if not command:
        raise RuntimeError("KORDOC_COMMAND가 비어 있습니다")
    executable = shutil.which(command[0]) or command[0]
    return [executable, *command[1:]]


def _cell_text(cell: dict[str, Any]) -> str:
    return str(cell.get("text") or "").strip()


def _normalize_table(
    raw: dict[str, Any], *, page_no: int, fallback_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = max(0, int(raw.get("rows") or 0))
    cols = max(0, int(raw.get("cols") or 0))
    source_id = str(raw.get("sourceId") or fallback_id)
    cells: list[dict[str, Any]] = []
    nested: list[dict[str, Any]] = []
    nonempty = 0
    merged = 0
    for row_index, row in enumerate(raw.get("cells") or []):
        for col_index, cell_value in enumerate(row or []):
            cell = cell_value if isinstance(cell_value, dict) else {}
            text = _cell_text(cell)
            row_span = max(1, int(cell.get("rowSpan") or 1))
            col_span = max(1, int(cell.get("colSpan") or 1))
            if text:
                nonempty += 1
            if row_span > 1 or col_span > 1:
                merged += 1
            item = {
                "row": row_index,
                "col": col_index,
                "row_span": row_span,
                "col_span": col_span,
                "text": text,
                "has_nested_table": any(
                    isinstance(block, dict) and block.get("type") == "table"
                    for block in cell.get("blocks") or []
                ),
            }
            cells.append(item)
            for child_index, block in enumerate(cell.get("blocks") or [], start=1):
                if not isinstance(block, dict) or block.get("type") != "table":
                    continue
                child, descendants = _normalize_table(
                    block.get("table") or {},
                    page_no=int(block.get("pageNumber") or page_no),
                    fallback_id=f"{source_id}_nested_{row_index}_{col_index}_{child_index}",
                )
                child["parent_cell"] = {"table_id": source_id, "row": row_index, "col": col_index}
                nested.extend([child, *descendants])
    slots = max(1, rows * cols)
    table = {
        "source_id": source_id,
        "page_no": page_no,
        "rows": rows,
        "cols": cols,
        "has_header": bool(raw.get("hasHeader")),
        "cells": cells,
        "nonempty_cells": nonempty,
        "merged_cells": merged,
        "density": round(nonempty / slots, 4),
    }
    # 큰 희소 병합표는 HWP 조판용 컨테이너일 가능성이 높다. 이 값은 P3 표 승격을
    # 막는 보수적 게이트이며, 원 셀 정보는 P1에 그대로 남는다.
    image_cells = sum(
        bool(re.fullmatch(r"!\[image\]\([^)]*\)", cell["text"].strip()))
        for cell in cells if cell["text"]
    )
    table["role_hint"] = (
        "layout_container"
        if (
            (slots > 30 and (table["density"] < 0.45 or merged >= 3))
            or (image_cells and (rows == 1 or cols == 1))
        )
        else "data_table_candidate"
    )
    table["text"] = "\n".join(
        cell["text"] for cell in cells if cell["text"]
    )
    return table, nested


def normalize_kordoc(raw: dict[str, Any], *, parser_version: str = "unknown") -> dict[str, Any]:
    """Kordoc 버전별 부가 필드를 버리고 안정적인 문단/표 계약만 만든다."""
    page_count = max(1, int(raw.get("pageCount") or len(raw.get("pages") or []) or 1))
    pages = [{"page_no": number, "paragraphs": [], "tables": []} for number in range(1, page_count + 1)]

    def target(page_no: int) -> dict[str, Any]:
        while len(pages) < page_no:
            pages.append({"page_no": len(pages) + 1, "paragraphs": [], "tables": []})
        return pages[max(1, page_no) - 1]

    table_counter = 0
    paragraph_counter = 0
    for block in raw.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        page_no = max(1, int(block.get("pageNumber") or 1))
        if block.get("type") == "paragraph":
            text = str(block.get("text") or "").strip()
            if text:
                paragraph_counter += 1
                target(page_no)["paragraphs"].append({
                    "source_id": f"p{page_no}_para{paragraph_counter:04d}",
                    "text": text,
                    "style": block.get("style") or {},
                })
        elif block.get("type") == "table":
            table_counter += 1
            table, nested = _normalize_table(
                block.get("table") or {}, page_no=page_no, fallback_id=f"t{table_counter}",
            )
            target(page_no)["tables"].append(table)
            for child in nested:
                target(int(child["page_no"]))["tables"].append(child)

    for page in pages:
        page["text"] = "\n".join([
            *(item["text"] for item in page["paragraphs"]),
            *(item["text"] for item in page["tables"] if item.get("text")),
        ])
    return {
        "parser": "kordoc",
        "parser_version": parser_version,
        "file_type": str(raw.get("fileType") or "hwp"),
        "page_count": len(pages),
        "pages": pages,
    }


def _parse_hwp_structure_kordoc(path: Path) -> dict[str, Any]:
    """Kordoc CLI를 별도 프로세스로 호출한다.

    문서 데이터는 로컬 프로세스의 파일 인자로만 전달된다. 이 함수는 네트워크 API에
    문서를 업로드하지 않는다. 기본 명령의 npx가 패키지를 설치할 때만 npm 통신이 생길
    수 있으므로 운영에서는 고정 설치 경로를 ``KORDOC_COMMAND``로 지정한다.
    """
    timeout = int(os.environ.get("KORDOC_TIMEOUT", "180"))
    with tempfile.TemporaryDirectory(prefix="nh-kordoc-") as directory:
        output = Path(directory) / "structure.json"
        command = [*_command(), str(path.resolve()), "--format", "json", "--output", str(output), "--silent"]
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
        if completed.returncode != 0 or not output.exists():
            detail = (completed.stderr or completed.stdout or "출력 파일 없음").strip()[-1000:]
            raise RuntimeError(f"Kordoc 구조 파싱 실패(exit={completed.returncode}): {detail}")
        raw = json.loads(output.read_text(encoding="utf-8"))
    if raw.get("success") is False:
        raise RuntimeError(f"Kordoc 구조 파싱 실패: {raw.get('error') or 'unknown error'}")
    version = os.environ.get("KORDOC_VERSION", "4.14.1")
    return normalize_kordoc(raw, parser_version=version)


def parse_hwp_structure(path: Path) -> dict[str, Any]:
    """HWP 구조는 사내 ``document-processor``를 우선하고 Kordoc으로 폴백한다.

    운영 컨테이너에 사내 패키지가 설치되어 있으면 Java/JAR 기반 HWP→HWPX 변환과
    DocIR 파싱만 로컬에서 수행한다. 패키지가 없거나 해당 문서를 읽지 못하면 기존
    Kordoc 경로를 사용한다. ``HWP_STRUCTURE_ENGINE``을 ``document_processor`` 또는
    ``kordoc``으로 지정하면 한 경로만 강제할 수 있다.
    """
    engine = os.environ.get("HWP_STRUCTURE_ENGINE", "auto").strip().lower()
    if engine not in {"auto", "document_processor", "kordoc"}:
        raise RuntimeError(f"지원하지 않는 HWP_STRUCTURE_ENGINE: {engine}")

    document_processor_error: Exception | None = None
    if engine in {"auto", "document_processor"}:
        try:
            from importlib.metadata import PackageNotFoundError, version

            from .docir_structure import hwp_structure_from_docir, load_docir

            try:
                parser_version = version("document-processor")
            except PackageNotFoundError:
                parser_version = "workspace"
            return hwp_structure_from_docir(
                load_docir(path), parser_version=parser_version,
            )
        except Exception as exc:
            document_processor_error = exc
            if engine == "document_processor":
                raise RuntimeError(f"document-processor HWP 구조 파싱 실패: {exc}") from exc

    try:
        return _parse_hwp_structure_kordoc(path)
    except Exception as kordoc_error:
        if document_processor_error is not None:
            raise RuntimeError(
                "HWP 구조 파서가 모두 실패했습니다: "
                f"document-processor={document_processor_error}; kordoc={kordoc_error}"
            ) from kordoc_error
        raise


def repartition_by_rendered_text(
    structure: dict[str, Any], rendered_page_texts: list[str],
) -> list[dict[str, Any]]:
    """Kordoc 논리 페이지를 실제 한컴 렌더 페이지에 다시 배치한다.

    HWP의 자동 쪽나눔은 Kordoc block.pageNumber에 반영되지 않는 경우가 있다. 009는
    모든 블록이 1쪽이라고 나오지만 한컴 렌더는 심의필 두 줄을 2쪽에 배치한다. PDF
    텍스트층과의 포함 관계로 문단을 실제 페이지에 옮긴다.
    """
    def norm(value: Any) -> str:
        return re.sub(r"[^0-9a-zA-Z가-힣]", "", str(value or "")).casefold()

    targets = [
        {"page_no": index + 1, "paragraphs": [], "tables": [], "text": ""}
        for index in range(len(rendered_page_texts))
    ]
    page_norms = [norm(text) for text in rendered_page_texts]

    def best_page(text: str, fallback: int) -> int:
        value = norm(text)
        if not value:
            return max(0, min(len(targets) - 1, fallback - 1))
        scores = []
        for index, page_text in enumerate(page_norms):
            if value in page_text:
                score = 1.0
            elif page_text and page_text in value:
                score = len(page_text) / len(value)
            else:
                # 긴 문단이 PDF 줄바꿈으로 갈려도 공통 문자가 많은 쪽을 택한다.
                from difflib import SequenceMatcher
                score = SequenceMatcher(None, value, page_text, autojunk=False).ratio()
            scores.append((score, index))
        score, index = max(scores, default=(0.0, max(0, fallback - 1)))
        return index if score >= 0.35 else max(0, min(len(targets) - 1, fallback - 1))

    for declared in structure.get("pages") or []:
        fallback = int(declared.get("page_no") or 1)
        for paragraph in declared.get("paragraphs") or []:
            targets[best_page(str(paragraph.get("text") or ""), fallback)]["paragraphs"].append(paragraph)
        for table in declared.get("tables") or []:
            targets[best_page(str(table.get("text") or ""), fallback)]["tables"].append(table)
    for page in targets:
        page["text"] = "\n".join([
            *(item["text"] for item in page["paragraphs"]),
            *(item["text"] for item in page["tables"] if item.get("text")),
        ])
        page["parser"] = structure["parser"]
        page["parser_version"] = structure["parser_version"]
    return targets
