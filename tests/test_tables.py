"""표를 찾고 행·열로 복원하는 경로를 검증한다.

좌표와 문구는 끝까지 OCR 줄에서 나와야 한다. VLM은 배치만 한다.
"""
from nh_parser_fin.parse import tables
from nh_parser_fin.parse import pipeline as full_pipeline


def _line(ref, x0, y0, x1, y1, text):
    return {"line_ref": ref, "bbox": [x0, y0, x1, y1], "text": text}


def _benefit_table():
    """`14. 대출성상품`의 부가서비스 표를 실측 좌표로 재현한다.

    PaddleX가 이 표를 검출하지 못해 13줄이 미배정으로 남았고, 복구 후보
    생성기가 낱개 영역 13개로 흩뿌렸다.
    """
    return [
        _line("L01", 633, 1842, 684, 1862, "담보명"),
        _line("L02", 796, 1842, 870, 1862, "보장금액"),
        _line("L03", 984, 1842, 1035, 1862, "담보명"),
        _line("L04", 1130, 1842, 1204, 1862, "보장금액"),
        _line("L05", 1318, 1842, 1369, 1862, "담보명"),
        _line("L06", 601, 1873, 717, 1892, "24시간 사고접수"),
        _line("L07", 793, 1875, 875, 1893, "10,000,000"),
        _line("L08", 938, 1873, 1198, 1893, "일상생활배상책임 손해"),
        _line("L09", 560, 1905, 875, 1924, "국내치료비(재해사망)보장"),
        _line("L10", 937, 1905, 1198, 1924, "화재벌금비용 손해"),
        _line("L11", 1305, 1905, 1381, 1923, "휴스치료비"),
        _line("L12", 1475, 1906, 1534, 1924, "100,000"),
    ]


def test_grid_detection_finds_a_table_paddlex_never_detected():
    lines = _benefit_table()

    grids = tables.find_grids(lines)

    assert len(grids) == 1
    assert len(grids[0]) == len(lines)
    assert tables.looks_like_grid(lines)


def test_paragraph_lines_are_not_mistaken_for_a_table():
    """한 열로 흐르는 문단은 표가 아니다."""
    lines = [
        _line("L01", 100, 100, 900, 125, "※ 대출대상 : 만 19세 이상 실명의 개인"),
        _line("L02", 100, 130, 900, 155, "※ 재직기간 3개월 이상인 급여소득자"),
        _line("L03", 100, 160, 900, 185, "※ 연소득 2천만원 이상"),
        _line("L04", 100, 190, 900, 215, "※ 신용평점 일정 수준 이상"),
        _line("L05", 100, 220, 900, 245, "※ 기타 은행 내규에 따름"),
    ]

    assert tables.find_grids(lines) == []
    assert not tables.looks_like_grid(lines)


def test_prepare_page_keeps_table_lines_separate_until_product_ownership():
    """좌우 상품 경계를 알기 전에 표 하나로 합치지 않으며 줄은 모두 보존한다."""
    page = {
        "page_no": 1,
        "canvas": [1654, 2339],
        "regions": [],
        "unassigned_lines": [
            *_benefit_table(),
            _line("L90", 200, 2100, 900, 2125, "※ 자세한 내용은 영업점에 문의"),
        ],
    }

    prepared = full_pipeline._prepare_page(page)

    candidates = prepared["recovery_candidates"]
    assert all(item.get("kind") != "table" for item in candidates)
    # 페이지 전체 VLM이 상품 소유권과 table_areas를 정할 때까지 작은 후보로 두되,
    # 줄은 하나도 잃지 않는다.
    every = {ref for item in candidates for ref in item["line_refs"]}
    assert every == {
        str(line["line_ref"]) for line in prepared["raw_unassigned_lines"]
    }
    assert len(every) == 13


def test_build_grid_keeps_ocr_text_and_reports_unplaced_lines():
    by_ref = {line["line_ref"]: line for line in _benefit_table()[:6]}
    result = {
        "analysis": "2행 3열",
        "rows": 2, "cols": 3,
        "cells": [
            {"line_ref": "L01", "row": 0, "col": 0, "is_header": True},
            {"line_ref": "L02", "row": 0, "col": 1, "is_header": True},
            {"line_ref": "L03", "row": 0, "col": 2, "is_header": True},
            {"line_ref": "L06", "row": 1, "col": 0, "is_header": False},
            # L04, L05 는 배치되지 않았다.
        ],
        "confidence": 0.9,
    }

    grid = tables.build_grid(result, by_ref)

    assert grid["grid"] == {"rows": 2, "cols": 3}
    assert [cell["text"] for cell in grid["cells"]] == [
        "담보명", "보장금액", "담보명", "24시간 사고접수",
    ]
    assert grid["cells"][0]["bbox"] == [633, 1842, 684, 1862]
    assert sorted(grid["unplaced_line_refs"]) == ["L04", "L05"]
    assert "| 담보명 | 보장금액 | 담보명 |" in grid["text_grid"]


def test_build_grid_drops_duplicate_placement_of_the_same_line():
    by_ref = {line["line_ref"]: line for line in _benefit_table()[:2]}
    result = {
        "analysis": "", "rows": 1, "cols": 2, "confidence": 1.0,
        "cells": [
            {"line_ref": "L01", "row": 0, "col": 0, "is_header": True},
            {"line_ref": "L01", "row": 0, "col": 1, "is_header": True},
            {"line_ref": "L02", "row": 0, "col": 1, "is_header": True},
        ],
    }

    grid = tables.build_grid(result, by_ref)

    refs = [ref for cell in grid["cells"] for ref in cell["line_refs"]]
    assert refs == ["L01", "L02"]
    assert len(refs) == len(set(refs))


def test_build_grid_keeps_rows_below_declared_grid_as_table_notes():
    by_ref = {
        "H": _line("H", 10, 10, 100, 20, "구분"),
        "V": _line("V", 10, 30, 100, 40, "2% 적립"),
        "N": _line("N", 10, 60, 300, 70, "주1) 전월 실적 조건 적용"),
    }
    result = {
        "analysis": "2행 1열 표와 아래 주석", "rows": 2, "cols": 1,
        "confidence": 0.9,
        "cells": [
            {"line_ref": "H", "row": 0, "col": 0, "is_header": True},
            {"line_ref": "V", "row": 1, "col": 0, "is_header": False},
            {"line_ref": "N", "row": 2, "col": 0, "is_header": False},
        ],
    }

    grid = tables.build_grid(result, by_ref)

    assert grid["text_grid"] == "| 구분 |\n|---|\n| 2% 적립 |"
    assert grid["notes"][0]["text"] == "주1) 전월 실적 조건 적용"
    assert grid["unplaced_line_refs"] == []


def test_build_grid_moves_long_footnote_rows_out_of_an_inflated_grid():
    by_ref = {
        "H": _line("H", 10, 10, 100, 20, "우대조건"),
        "V": _line("V", 10, 30, 100, 40, "2.0%p"),
        "N1": _line("N1", 10, 70, 300, 80, "주1) 연금 입금 실적 기준"),
        "N2": _line("N2", 10, 90, 400, 100, "최근 3개월 중 2개월 이상 입금"),
    }
    result = {
        "analysis": "각주까지 4행으로 오판", "rows": 4, "cols": 1,
        "confidence": 0.9,
        "cells": [
            {"line_ref": "H", "row": 0, "col": 0, "is_header": True},
            {"line_ref": "V", "row": 1, "col": 0, "is_header": False},
            {"line_ref": "N1", "row": 2, "col": 0, "is_header": False},
            {"line_ref": "N2", "row": 3, "col": 0, "is_header": False},
        ],
    }

    grid = tables.build_grid(result, by_ref)

    assert grid["grid"] == {"rows": 2, "cols": 1}
    assert [note["text"] for note in grid["notes"]] == [
        "주1) 연금 입금 실적 기준", "최근 3개월 중 2개월 이상 입금",
    ]
    assert grid["unplaced_line_refs"] == []


def test_partial_table_keeps_all_source_lines_as_selected_text(monkeypatch):
    lines = _benefit_table()[:6]
    region = {
        "region_id": "p1_r001", "bbox": [560, 1842, 1369, 1892],
        "kind": "table", "text": "\n".join(line["text"] for line in lines),
        "text_source": "digital_ocr_lines", "lines": lines,
    }
    page = {"page_no": 1, "regions": [region], "table_areas": []}
    monkeypatch.setattr(full_pipeline.tables, "place_cells", lambda image, item: {
        "grid": {"rows": 2, "cols": 3}, "cells": [], "notes": [],
        "unplaced_line_refs": ["L04", "L05"], "confidence": 1.0,
        "analysis": "일부 누락", "text_grid": "| 담보명 | 보장금액 |",
    })

    full_pipeline._place_tables(page, image=None)

    assert region["text"] == "\n".join(line["text"] for line in lines)
    assert region["text_source"] == "ocr_table_lines_fallback"
    assert region["needs_review"] is True
    assert "table_unplaced_lines" in region["review_reasons"]


def test_sparse_table_grid_keeps_source_lines_and_flags_review(monkeypatch):
    lines = _benefit_table()[:6]
    source = "\n".join(line["text"] for line in lines)
    region = {
        "region_id": "p1_r001", "bbox": [560, 1842, 1369, 1892],
        "kind": "table", "text": source,
        "text_source": "digital_ocr_lines", "lines": lines,
    }
    page = {"page_no": 1, "regions": [region], "table_areas": []}
    monkeypatch.setattr(full_pipeline.tables, "place_cells", lambda image, item: {
        "grid": {"rows": 6, "cols": 6},
        "cells": [
            {"row": 0, "col": 0, "text": "담보명"},
            {"row": 1, "col": 0, "text": "24시간 사고접수"},
        ],
        "notes": [], "unplaced_line_refs": [], "confidence": 1.0,
        "analysis": "과도하게 빈 격자", "text_grid": "| 담보명 |",
    })

    full_pipeline._place_tables(page, image=None)

    assert region["text"] == source
    assert region["table_cell_density"] < 0.3
    assert "table_sparse_grid" in region["review_reasons"]


def test_build_grid_returns_none_when_the_model_says_not_a_table():
    by_ref = {line["line_ref"]: line for line in _benefit_table()[:6]}

    assert tables.build_grid(
        {"analysis": "표 아님", "rows": 0, "cols": 0, "cells": [], "confidence": 0.0},
        by_ref,
    ) is None


def test_vlm_table_area_promotes_scattered_recovery_regions_without_new_bbox():
    """VLM은 위치만 알려주고 좌표는 합쳐진 OCR 줄에서 나와야 한다."""
    lines = _benefit_table()
    page = {
        "page_no": 1,
        "canvas": [1654, 2339],
        "table_areas": [{
            "member_ids": [f"p1_x{i:03d}" for i in range(1, 13)],
            "kind": "table", "note": "담보명/보장금액 표", "confidence": 0.9,
        }],
        "regions": [
            {"region_id": f"p1_x{index:03d}", "origin": "recovery", "kind": "text",
             "product_id": "product_1", "bbox": line["bbox"], "lines": [line],
             "text": line["text"]}
            for index, line in enumerate(lines, start=1)
        ],
    }

    promoted = full_pipeline._promote_vlm_table_areas(page)

    assert len(promoted) == 1
    merged = promoted[0]
    # 별도 t ID를 만들지 않고 첫 원본 Region ID를 대표로 유지한다.
    assert merged["region_id"] == "p1_x001"
    assert merged["kind"] == "table"
    assert merged["bbox"] == [560, 1842, 1534, 1924]
    assert merged["bbox_source"] == "ocr_pdf_lines"
    assert len(merged["lines"]) == 12
    # 합쳐진 원본 Region은 사라지고 줄은 새 Region 하나에만 남는다.
    assert len(page["regions"]) == 1
    assert merged["merged_from"][0] == "p1_x001"


def test_vlm_table_area_is_ignored_when_too_few_lines_sit_inside():
    page = {
        "page_no": 1,
        "canvas": [1000, 1000],
        "table_areas": [{"member_ids": ["p1_x001", "p1_x002"],
                         "kind": "table", "note": "", "confidence": 0.9}],
        "regions": [
            {"region_id": "p1_x001", "origin": "recovery", "kind": "text",
             "bbox": [10, 10, 100, 30], "lines": [_line("L01", 10, 10, 100, 30, "가")]},
            {"region_id": "p1_x002", "origin": "recovery", "kind": "text",
             "bbox": [10, 40, 100, 60], "lines": [_line("L02", 10, 40, 100, 60, "나")]},
        ],
    }

    assert full_pipeline._promote_vlm_table_areas(page) == []
    assert len(page["regions"]) == 2


def _card_table_regions():
    """`2. 카드상품` 의 2행 3열 표. 5칸이 Region 5개로 흩어져 있다.

    복구 3칸(`구분`·`적립율`·`2%`)과 PaddleX 2칸이 섞여 있어, 복구 Region 만
    보면 칸 수가 모자라 통째로 버려졌다.
    """
    cells = [
        ("p1_x001", "recovery", [477, 2315, 560, 2369], "구분"),
        ("p1_r011", "paddlex", [1175, 2315, 1521, 2372], "GS리테일 점내 가맹점"),
        ("p1_x003", "recovery", [467, 2410, 573, 2458], "적립율"),
        ("p1_x002", "recovery", [954, 2393, 1082, 2478], "2%"),
        ("p1_r012", "paddlex", [1471, 2384, 1901, 2485], "GS25, GS THE FRESH"),
    ]
    return [
        {"region_id": rid, "origin": origin, "kind": "text", "product_id": "product_1",
         "bbox": box, "text": text,
         "lines": [{"line_ref": f"p1/{rid}/L000", "bbox": box, "text": text}]}
        for rid, origin, box, text in cells
    ]


def test_vlm_table_area_merges_paddlex_cells_too():
    page = {
        "page_no": 1, "canvas": [4032, 4032],
        "table_areas": [{"member_ids": ["p1_x001", "p1_x003", "p1_x002"],
                         "kind": "table", "note": "GS리테일 적립율 표",
                         "confidence": 1.0}],
        "regions": _card_table_regions(),
    }

    promoted = full_pipeline._promote_vlm_table_areas(page)

    assert len(promoted) == 1
    merged = promoted[0]
    assert merged["region_id"] == "p1_x001"
    assert merged["kind"] == "table"
    assert merged["bbox"] == [467, 2315, 1901, 2485]
    assert merged["bbox_source"] == "ocr_pdf_lines"
    assert len(merged["lines"]) == 5
    assert sorted(merged["merged_from"]) == [
        "p1_r011", "p1_r012", "p1_x001", "p1_x002", "p1_x003",
    ]
    assert len(page["regions"]) == 1


def test_field_list_areas_are_never_merged():
    """항목명–값 나열은 행마다 구분값이 달라 합치면 라벨이 하나만 남는다."""
    page = {
        "page_no": 1, "canvas": [4032, 4032],
        "table_areas": [{"member_ids": ["p1_x001", "p1_x003", "p1_x002"],
                         "kind": "field_list", "note": "대출대상~필요서류",
                         "confidence": 1.0}],
        "regions": _card_table_regions(),
    }

    assert full_pipeline._promote_vlm_table_areas(page) == []
    assert len(page["regions"]) == 5


def test_areas_spanning_two_products_are_not_merged():
    regions = _card_table_regions()
    regions[1]["product_id"] = "product_2"
    page = {
        "page_no": 1, "canvas": [4032, 4032],
        "table_areas": [{"member_ids": ["p1_x001", "p1_x003", "p1_r011"],
                         "kind": "table", "note": "", "confidence": 1.0}],
        "regions": regions,
    }

    assert full_pipeline._promote_vlm_table_areas(page) == []



def test_growing_does_not_reach_across_to_another_column_block():
    """멀리 떨어진 다른 단은 세로로 겹쳐도 끌어오지 않는다."""
    regions = _card_table_regions()
    far = {
        "region_id": "p1_r024", "origin": "paddlex", "kind": "text",
        "product_id": "product_1", "bbox": [2411, 2376, 3660, 2565],
        "text": "생활영역 추가적립",
        "lines": [{"line_ref": "p1/p1_r024/L000",
                   "bbox": [2411, 2376, 3660, 2565], "text": "생활영역 추가적립"}],
    }
    page = {
        "page_no": 1, "canvas": [4032, 4032],
        "table_areas": [{"member_ids": ["p1_x001", "p1_x003", "p1_x002"],
                         "kind": "table", "note": "", "confidence": 1.0}],
        "regions": [*regions, far],
    }

    promoted = full_pipeline._promote_vlm_table_areas(page)

    assert "p1_r024" not in promoted[0]["merged_from"]
    assert any(r["region_id"] == "p1_r024" for r in page["regions"])
