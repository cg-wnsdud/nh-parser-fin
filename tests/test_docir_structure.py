from types import SimpleNamespace

from PIL import Image

from nh_parser_fin.ingest.docir_structure import hwp_structure_from_docir, pdf_page_structure
from nh_parser_fin.ingest.hwp_html import dom_rows_to_blocks, inject_review_surface_api
from nh_parser_fin.parse.adapters import build_page_evidence
from nh_parser_fin.parse.pipeline import _place_tables, _prepare_page


class RunIR:
    def __init__(self, text, bbox, node_id):
        self.text = text
        self.bbox = bbox
        self.node_id = node_id
        self.page_number = 1


class TableCellIR:
    def __init__(self, text, bbox, node_id):
        self.text = text
        self.bbox = bbox
        self.node_id = node_id
        self.cell_style = SimpleNamespace(rowspan=1, colspan=1)


class TableIR:
    def __init__(self, rows):
        self.node_id = "table-1"
        self.page_number = 1
        self._rows = rows

    def iter_cell_positions(self):
        for row, values in enumerate(self._rows):
            for col, cell in enumerate(values):
                yield row, col, cell


class ParagraphIR:
    def __init__(self, content, bbox=None):
        self.content = content
        self.bbox = bbox


def _bbox(left, bottom, right, top):
    return SimpleNamespace(left_pt=left, bottom_pt=bottom, right_pt=right, top_pt=top)


def _fixture_docir():
    title = ParagraphIR([
        RunIR("샘플 대출", _bbox(10, 170, 90, 190), "run-title"),
    ], _bbox(10, 170, 90, 190))
    table = TableIR([
        [
            TableCellIR("대출기간", _bbox(10, 130, 30, 150), "cell-h1"),
            TableCellIR("2년", _bbox(30, 130, 90, 150), "cell-v1"),
        ],
        [
            TableCellIR("상환방법", _bbox(10, 100, 30, 130), "cell-h2"),
            TableCellIR("만기일시상환", _bbox(30, 100, 90, 130), "cell-v2"),
        ],
    ])
    return SimpleNamespace(
        pages=[SimpleNamespace(
            page_number=1,
            width_pt=100,
            height_pt=200,
            parse_status="parsed",
        )],
        paragraphs=[title, ParagraphIR([table])],
    )


def test_pdf_docir_builds_exact_row_regions_in_canvas_coordinates():
    result = pdf_page_structure(
        _fixture_docir(),
        page_no=1,
        canvas=(1000, 2000),
        digital_lines=[
            {"text": "샘플 대출"},
            {"text": "대출기간 2년"},
            {"text": "상환방법 만기일시상환"},
        ],
    )

    assert result.route == "structured_fast"
    assert result.metrics["table_rows"] == 2
    assert [block["content"] for block in result.blocks] == [
        "샘플 대출",
        "대출기간 | 2년",
        "상환방법 | 만기일시상환",
    ]
    assert result.blocks[0]["bbox"] == [100, 100, 900, 300]
    assert result.blocks[1]["bbox"] == [100, 500, 900, 700]
    assert result.blocks[1]["table"]["cells"][0]["is_header"] is True
    assert result.blocks[1]["bbox_source"] == "document_processor_pdf_cells"


def test_structured_table_survives_evidence_and_skips_vlm_cell_reconstruction():
    structured = pdf_page_structure(
        _fixture_docir(), page_no=1, canvas=(1000, 2000), digital_lines=[]
    )
    page = build_page_evidence(
        structured.blocks,
        [],
        page_no=1,
        canvas=[1000, 2000],
        digital_lines=[],
    )
    prepared = _prepare_page(page)
    row = next(region for region in prepared["regions"] if region.get("kind") == "table")

    assert row["origin"] == "document_processor"
    assert row["bbox_source"] == "document_processor_pdf_cells"
    assert row["text_source"] == "document_processor"

    _place_tables(prepared, Image.new("RGB", (1000, 2000), "white"))

    assert row["table_status"] == "complete"
    assert row["table"]["source"] == "document_processor"
    assert row["table"]["cells"][1]["text"] == "2년"


def test_scan_like_docir_page_stays_on_visual_route():
    docir = _fixture_docir()
    docir.pages[0].parse_status = "skipped"

    result = pdf_page_structure(docir, page_no=1, canvas=(1000, 2000))

    assert result.route == "visual"


def test_hwp_docir_preserves_rows_cells_and_full_text_without_bbox():
    docir = _fixture_docir()
    docir.source_doc_type = "hwp"

    structure = hwp_structure_from_docir(docir, parser_version="test")

    assert structure["parser"] == "document_processor"
    assert structure["page_count"] == 1
    table = structure["pages"][0]["tables"][0]
    assert table["rows"] == 2
    assert table["cols"] == 2
    assert [cell["text"] for cell in table["cells"]] == [
        "대출기간", "2년", "상환방법", "만기일시상환",
    ]
    assert "상환방법" in structure["pages"][0]["text"]


def test_hwp_review_html_exposes_dom_bbox_and_highlight_api():
    html = inject_review_surface_api(
        '<html><head></head><body><div class="document-page" data-page="1">'
        '<p data-node-id="p1">대출기간</p></div></body></html>'
    )

    assert "window.nhReviewSurface" in html
    assert "getBoundingClientRect" in html
    assert "data-nh-highlight" in html
    assert html.index("nh-review-surface-style") < html.index("</head>")


def test_hwp_dom_rows_become_separate_display_exact_regions():
    blocks = dom_rows_to_blocks(
        [
            {
                "node_id": "domrow:main:4",
                "page_no": 1,
                "bbox": [100.0, 300.0, 700.0, 360.0],
                "cells": [
                    {"node_id": "h", "col": 1, "col_span": 1, "row_span": 1,
                     "bbox": [100.0, 300.0, 250.0, 360.0], "text": "상환방법"},
                    {"node_id": "v", "col": 2, "col_span": 2, "row_span": 1,
                     "bbox": [250.0, 300.0, 700.0, 360.0], "text": "만기일시상환"},
                ],
            },
            {
                "node_id": "domrow:main:5",
                "page_no": 1,
                "bbox": [100.0, 360.0, 700.0, 420.0],
                "cells": [
                    {"node_id": "h2", "col": 1, "col_span": 1, "row_span": 1,
                     "bbox": [100.0, 360.0, 250.0, 420.0], "text": "준비서류"},
                    {"node_id": "v2", "col": 2, "col_span": 2, "row_span": 1,
                     "bbox": [250.0, 360.0, 700.0, 420.0], "text": "신분증"},
                ],
            },
        ],
        page_no=1,
        canvas=(1400, 2000),
        page_css_size=(700.0, 1000.0),
    )

    assert [block["content"] for block in blocks] == [
        "상환방법\n만기일시상환", "준비서류\n신분증",
    ]
    assert blocks[0]["bbox"] == [200, 600, 1400, 720]
    assert blocks[1]["bbox"] == [200, 720, 1400, 840]
    assert blocks[0]["bbox_quality"] == "display_exact"
    assert blocks[0]["table"]["cells"][1]["col_span"] == 2
