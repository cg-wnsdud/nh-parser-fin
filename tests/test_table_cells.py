"""표 셀 정본과 그로부터 만든 마크다운·행 문장, P3 표 필드."""
from nh_parser_fin.parse import table_html
from nh_parser_fin.parse.export import build_p1, build_p3
from nh_parser_fin.parse.table_cells import row_texts

# 040 적립식 우대금리 표의 VLM HTML(2단 머리글, 행·열 병합)을 줄인 것.
TWO_ROW_HEADER = """<table>
<tr><th rowspan="2">우대항목</th><th colspan="2">세부조건</th><th rowspan="2">우대금리</th></tr>
<tr><th>가입기간</th><th>우대조건</th></tr>
<tr><td rowspan="2">아동수당 수령</td><td>1년</td><td>3개월 이상 수령 시</td><td rowspan="2">1.5%p</td></tr>
<tr><td>2년 / 3년</td><td>가입기간의 1/2이상 수령 시</td></tr>
<tr><td>형제자매 가입</td><td colspan="2">형제자매가 함께 가입 시 각각 우대</td><td>0.5%p</td></tr>
</table>"""

# 심의 팀원이 든 ISA 비교표 모양: 한 값이 여러 열에 걸친다.
ISA = """<table>
<tr><th rowspan="2">구분</th><th colspan="2">ISA 신탁형</th><th colspan="3">ISA 일임형</th></tr>
<tr><th>일반형</th><th>서민형</th><th>일반형</th><th>서민형</th><th>농어민</th></tr>
<tr><td>의무가입 기간</td><td colspan="5">3년</td></tr>
</table>"""


def test_markdown_keeps_every_header_row_and_merged_value():
    markdown = table_html.to_markdown(table_html.to_grid(TWO_ROW_HEADER))

    lines = markdown.splitlines()
    assert lines[0] == "| 우대항목 | 세부조건 / 가입기간 | 세부조건 / 우대조건 | 우대금리 |"
    # 행 병합과 열 병합 모두 걸친 칸마다 값을 둔다. 비어 있으면 값이 없다고 읽힌다.
    assert lines[3] == "| 아동수당 수령 | 2년 / 3년 | 가입기간의 1/2이상 수령 시 | 1.5%p |"
    assert lines[4] == "| 형제자매 가입 | 형제자매가 함께 가입 시 각각 우대 | 형제자매가 함께 가입 시 각각 우대 | 0.5%p |"


def test_cells_keep_original_spans_for_rebuilding_the_table():
    cells = table_html.to_cells(table_html.to_grid(TWO_ROW_HEADER))

    by_text = {cell["text"]: cell for cell in cells}
    assert by_text["세부조건"] == {
        "row": 0, "col": 1, "row_span": 1, "col_span": 2, "is_header": True, "text": "세부조건",
    }
    assert by_text["1.5%p"]["row_span"] == 2
    assert by_text["형제자매가 함께 가입 시 각각 우대"]["col_span"] == 2
    assert len(cells) == 14


def test_row_texts_pair_each_value_with_its_header_path():
    cells = table_html.to_cells(table_html.to_grid(TWO_ROW_HEADER))

    assert row_texts(cells) == [
        "우대항목: 아동수당 수령 | 세부조건 / 가입기간: 1년 | 세부조건 / 우대조건: 3개월 이상 수령 시 | 우대금리: 1.5%p",
        "우대항목: 아동수당 수령 | 세부조건 / 가입기간: 2년 / 3년 | 세부조건 / 우대조건: 가입기간의 1/2이상 수령 시 | 우대금리: 1.5%p",
        "우대항목: 형제자매 가입 | 세부조건: 형제자매가 함께 가입 시 각각 우대 | 우대금리: 0.5%p",
    ]


def test_value_spanning_columns_of_different_groups_names_every_column():
    grid = table_html.to_grid(ISA)

    assert table_html.to_markdown(grid).splitlines()[2] == "| 의무가입 기간 | 3년 | 3년 | 3년 | 3년 | 3년 |"
    assert row_texts(table_html.to_cells(grid)) == ["구분: 의무가입 기간 | 일반형·서민형·농어민: 3년"]


def _hwp_page():
    """069 부대비용: 바깥 레이아웃 표의 칸 안에 2×3 중첩 표. 머리글 칸만 배경이 있다."""
    outer = {"source_id": "tbl_outer", "rows": 16, "cols": 7, "cells": [
        {"row": 12, "col": 2, "row_span": 1, "col_span": 1, "text": "부대비용", "source_id": "cell_head", "background": "#E8F0FB"},
        {"row": 12, "col": 5, "row_span": 1, "col_span": 1, "text": "구분\n고정금리 …", "source_id": "cell_box", "background": None},
    ]}
    inner = {"source_id": "tbl_fee", "rows": 2, "cols": 3, "parent_cell": {"table_id": "tbl_outer", "row": 12, "col": 5}, "cells": [
        {"row": 1, "col": 1, "row_span": 1, "col_span": 1, "text": "구분", "source_id": "c11", "background": "#0063BD"},
        {"row": 1, "col": 2, "row_span": 1, "col_span": 1, "text": "고정금리", "source_id": "c12", "background": "#0063BD"},
        {"row": 1, "col": 3, "row_span": 1, "col_span": 1, "text": "변동금리", "source_id": "c13", "background": "#0063BD"},
        {"row": 2, "col": 1, "row_span": 1, "col_span": 1, "text": "적용요율", "source_id": "c21", "background": "#0063BD"},
        {"row": 2, "col": 2, "row_span": 1, "col_span": 1, "text": "0.01%", "source_id": "c22", "background": None},
        {"row": 2, "col": 3, "row_span": 1, "col_span": 1, "text": "0.01%", "source_id": "c23", "background": None},
    ]}

    def row(region_id, dom_row, ids, texts, y):
        cells = [{"col": 0, "row_span": 1, "col_span": 1, "is_header": True, "text": "부대비용",
                  "bbox": [143, 1208, 564, 1649], "node_id": "cell_head"}]
        for col, (node_id, text) in enumerate(zip(ids, texts), start=1):
            cells.append({"col": col, "row_span": 1, "col_span": 1, "is_header": False, "text": text,
                          "bbox": [500 + 300 * col, y, 800 + 300 * col, y + 32], "node_id": node_id})
        return {
            "region_id": region_id, "bbox": [143, 1208, 1415, 1649], "kind": "table",
            "text": "\n".join(["부대비용", *texts]), "text_source": "document_processor_html",
            "structured": {"surface_node_id": f"domrow:tbl_fee:{dom_row}"},
            "table": {"source": "document_processor", "grid": {"rows": 1, "cols": 4}, "cells": cells},
        }

    return {
        "page_no": 1, "canvas": [1653, 2339],
        "hwp_structure": {"paragraphs": [], "tables": [outer, inner]},
        "regions": [
            row("p1_r011", 0, ["c11", "c12", "c13"], ["구분", "고정금리", "변동금리"], 1325),
            row("p1_r012", 1, ["c21", "c22", "c23"], ["적용요율", "0.01%", "0.01%"], 1357),
        ],
    }


def test_hwp_row_regions_share_one_table_with_source_coordinates_and_headers():
    p3 = build_p3(build_p1({"pages": [_hwp_page()]}))
    page = p3["pages"][0]
    header_row, value_row = (region["table"] for region in page["regions"])

    assert page["tables"] == [{
        "table_id": "tbl_fee", "source": "hwp", "rows": 2, "cols": 3, "header_rows": 1,
        "header_source": "hwp_style", "region_ids": ["p1_r011", "p1_r012"],
    }]
    assert [cell["is_header"] for cell in header_row["cells"]] == [True, True, True]
    assert [(c["row"], c["col"], c["is_header"], c["text"]) for c in value_row["cells"]] == [
        (1, 0, True, "적용요율"), (1, 1, False, "0.01%"), (1, 2, False, "0.01%"),
    ]
    # 부모 표의 표제어는 이 표의 칸이 아니므로 context로 분리된다.
    assert value_row["context"] == [{"text": "부대비용", "bbox": [143, 1208, 564, 1649]}]
    assert value_row["row_texts"] == ["구분: 적용요율 | 고정금리: 0.01% | 변동금리: 0.01%"]
    assert header_row["row_texts"] == []
    # 화면 셀 상자는 그대로 전달한다.
    assert value_row["cells"][0]["bbox"] == [800, 1357, 1100, 1389]
    # 기존 문장 형식은 바꾸지 않는다.
    assert page["regions"][1]["selected_text"] == "부대비용\n적용요율\n0.01%\n0.01%"


def test_hwp_table_without_fill_contrast_marks_no_headers():
    page = _hwp_page()
    for cell in page["hwp_structure"]["tables"][1]["cells"]:
        cell["background"] = None

    p3 = build_p3(build_p1({"pages": [page]}))

    assert p3["pages"][0]["tables"][0]["header_source"] == "none"
    assert not any(c["is_header"] for r in p3["pages"][0]["regions"] for c in r["table"]["cells"])


def test_old_p1_visual_table_rebuilds_cells_from_selected_html():
    region = {
        "region_id": "p1_r016", "bbox": [550, 1265, 1562, 1814], "kind": "table",
        "text": "| … |", "text_source": "vlm_table_html",
        "visual_table": {"format": "grid", "header_rows": 2, "rows": [], "merged": []},
        "table_html_attempt": {"status": "selected", "html": TWO_ROW_HEADER},
        "digital_anchor": {"corrections": [{"from": "아동수당 수령", "to": "아동수당 수령주1)"}]},
    }
    p3 = build_p3(build_p1({"pages": [{"page_no": 1, "canvas": [1654, 2339], "regions": [region]}]}))
    table = p3["pages"][0]["regions"][0]["table"]

    assert table["table_id"] == "p1_t001" and table["source"] == "vlm"
    assert len(table["cells"]) == 14
    # 최종 문구에 적용된 디지털 글자 교정을 칸에도 적용한다.
    assert "아동수당 수령주1)" in [cell["text"] for cell in table["cells"]]
    assert "bbox" not in table["cells"][0]


# 025 대출성: 표 전체 폭 칸이 구역 제목이다(`• 운전자금`, `• 시설자금`).
SECTIONS = """<table>
<tr><th colspan="4">• 운전자금</th></tr>
<tr><th>상환방법</th><td>일시상환/혼합상환</td><td>할부상환</td><td>종합(마이너스)통장</td></tr>
<tr><th colspan="4">• 시설자금</th></tr>
<tr><th>상환방법</th><td>일시상환/혼합상환</td><td colspan="2">할부상환</td></tr>
</table>"""


def test_full_width_section_titles_are_written_once_and_prefix_their_rows():
    grid = table_html.to_grid(SECTIONS)

    assert table_html.header_rows(grid) == 0
    assert table_html.to_markdown(grid).splitlines()[2:] == [
        "| • 운전자금 |  |  |  |",
        "| 상환방법 | 일시상환/혼합상환 | 할부상환 | 종합(마이너스)통장 |",
        "| • 시설자금 |  |  |  |",
        "| 상환방법 | 일시상환/혼합상환 | 할부상환 | 할부상환 |",
    ]
    assert row_texts(table_html.to_cells(grid)) == [
        "[• 운전자금] 상환방법: 일시상환/혼합상환 | 할부상환 | 종합(마이너스)통장",
        "[• 시설자금] 상환방법: 일시상환/혼합상환 | 할부상환",
    ]
