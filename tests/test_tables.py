"""표 검증은 기존 텍스트와 좌표를 쓰고 P3에는 간결한 Region만 남긴다."""
from PIL import Image

from nh_parser_fin.parse.export import build_p1, build_p3
from nh_parser_fin.parse.visual_tables import verify_visual_tables


def _region(identifier, box, text, *, label="text", product="product_1"):
    return {
        "region_id": identifier, "bbox": box, "text": text,
        "text_source": "ocr_lines", "product_id": product,
        "layout_observation": {"label": label}, "semantic_labels": [],
        "lines": [{"line_ref": f"{identifier}/L0", "bbox": box, "text": text}],
    }


def test_paddle_false_positive_keeps_plain_region(monkeypatch):
    from nh_parser_fin.parse import visual_tables

    region = _region("p1_r001", [10, 10, 180, 80], "대출대상 직장인", label="table")
    page = {"page_no": 1, "canvas": [200, 100], "regions": [region], "table_areas": []}
    monkeypatch.setattr(visual_tables, "_verify", lambda *args: {
        "is_table": False, "member_ids": ["p1_r001"], "confidence": 0.98,
        "reason": "일반 항목 문장",
    })

    assert verify_visual_tables(Image.new("RGB", (200, 100)), page) == {
        "checked": 1, "accepted": 0,
    }
    assert "table_areas" not in page
    assert "semantic_structures" not in page
    assert region.get("kind") != "table"


def test_missed_table_groups_read_final_union_and_use_vlm_text(monkeypatch):
    from nh_parser_fin.parse import visual_tables

    regions = [
        _region("p1_r001", [10, 10, 70, 30], "조건"),
        _region("p1_r002", [100, 10, 170, 30], "우대금리"),
        _region("p1_r003", [10, 40, 70, 60], "급여이체"),
        _region("p1_r004", [100, 40, 170, 60], "0.2%p"),
        _region("p1_r005", [10, 80, 170, 100], "표 밖 각주"),
    ]
    page = {
        "page_no": 1, "canvas": [200, 120], "regions": regions,
        "table_areas": [{"member_ids": [r["region_id"] for r in regions]}],
    }
    monkeypatch.setattr(visual_tables, "_verify", lambda *args: {
        "is_table": True,
        "member_ids": [f"p1_r{i:03d}" for i in range(1, 5)],
        "confidence": 0.96, "reason": "같은 우대금리 표",
    })
    calls = []
    def read_final(image, region, parser_text, reader_text):
        calls.append((list(region["bbox"]), parser_text, reader_text))
        return {"text": "우대금리\n급여이체 | 0.2%p", "confidence": 0.94,
                "source": "corrected", "reader_text": "급여이체 0.2%p"}
    monkeypatch.setattr(visual_tables.reading, "read_visual_table", read_final)

    assert verify_visual_tables(Image.new("RGB", (200, 120)), page) == {
        "checked": 1, "accepted": 1,
    }
    assert [r["region_id"] for r in page["regions"]] == ["p1_r001", "p1_r005"]
    table = page["regions"][0]
    assert table["bbox"] == [10, 10, 170, 60]
    assert calls == [([10, 10, 170, 60], "조건 | 우대금리\n급여이체 | 0.2%p", None)]
    assert table["text"] == "우대금리\n급여이체 | 0.2%p"
    assert table["text_candidates"]["pre_table_vlm"] == calls[0][1]
    assert table["text_source"] == "vlm_table_judge"
    assert len(table["lines"]) == 4
    assert table["table_detection"]["source"] == "vlm_page"

    p1 = build_p1({"pages": [page]})
    output = build_p3(p1)
    p3_table = output["pages"][0]["regions"][0]
    assert output["contract"]["version"] == "nh-ad-region-review-input-v9"
    assert p3_table["kind"] == "table"
    assert p3_table["selected_text"] == table["text"]
    assert p3_table["text_source"] == "vlm"
    assert "table" not in p3_table and "relations" not in p3_table
    assert "semantic_structures" not in p1["pages"][0]
    assert "table_areas" not in p1["pages"][0]
    assert len(p1["pages"][0]["table_checks"]) == 1


def test_single_paddle_table_rejudges_prior_reading_with_whole_table_image(monkeypatch):
    from nh_parser_fin.parse import visual_tables

    region = _region("p1_r001", [10, 10, 180, 80], "금리\n0.2\n0.2", label="table")
    region["vlm_judge"] = {"text": "금리\n급여이체 | 0.2", "confidence": 0.96,
                           "source": "corrected"}
    page = {"page_no": 1, "canvas": [200, 100], "regions": [region]}
    monkeypatch.setattr(visual_tables, "_verify", lambda *args: {
        "is_table": True, "member_ids": ["p1_r001"], "confidence": 0.9,
        "reason": "표",
    })
    calls = []
    def read_final(image, final_region, parser_text, reader_text):
        calls.append((list(final_region["bbox"]), parser_text, reader_text))
        return {"text": "금리\n급여이체 | 0.2", "confidence": 0.96,
                "source": "corrected", "reader_text": reader_text}
    monkeypatch.setattr(visual_tables.reading, "read_visual_table", read_final)

    verify_visual_tables(Image.new("RGB", (200, 100)), page)

    assert region["text"] == "금리\n급여이체 | 0.2"
    assert calls == [([10, 10, 180, 80], "금리\n0.2\n0.2", "금리\n급여이체 | 0.2")]
    assert region["text_candidates"]["pre_table_vlm"] == "금리\n0.2\n0.2"
    assert region["table_reading"]["method"] == "table_judge_from_region"
    assert region["text_candidates"]["pre_table_judge"] == "금리\n급여이체 | 0.2"
    assert build_p3(build_p1({"pages": [page]}))["pages"][0]["regions"][0]["text_source"] == "vlm"


def test_table_vlm_failure_keeps_parser_text_and_requests_review(monkeypatch):
    from nh_parser_fin.parse import visual_tables

    region = _region("p1_r001", [10, 10, 180, 80], "금리 | 0.2", label="table")
    page = {"page_no": 1, "canvas": [200, 100], "regions": [region]}
    monkeypatch.setattr(visual_tables, "_verify", lambda *args: {
        "is_table": True, "member_ids": ["p1_r001"], "confidence": 0.9,
        "reason": "표",
    })
    monkeypatch.setattr(visual_tables.reading, "read_visual_table", lambda *args: None)

    verify_visual_tables(Image.new("RGB", (200, 100)), page)

    assert region["text"] == "금리 | 0.2"
    assert region["kind"] == "table"
    assert region["table_reading"]["status"] == "blank"
    assert "table_vlm_read_blank" in region["review_reasons"]


def test_table_vlm_only_removes_unexplained_adjacent_long_repeat():
    from nh_parser_fin.parse.visual_tables import _remove_unexplained_repeats

    parser = "담보명\n담보명\n※ 한도내에서 자유롭게 상환 가능한 방식"
    vlm = (
        "담보명\n담보명\n"
        "※ 한도내에서 자유롭게 상환 가능한 방식\n"
        "※ 한도내에서 자유롭게 상환 가능한 방식"
    )

    text, removed = _remove_unexplained_repeats(vlm, parser)

    assert removed == 1
    assert text == "담보명\n담보명\n※ 한도내에서 자유롭게 상환 가능한 방식"


def test_candidate_across_products_never_merges(monkeypatch):
    from nh_parser_fin.parse import visual_tables

    page = {
        "page_no": 1, "canvas": [200, 100],
        "regions": [
            _region("p1_r001", [10, 10, 60, 30], "조건"),
            _region("p1_r002", [100, 10, 160, 30], "금리", product="product_2"),
        ],
        "table_areas": [{"member_ids": ["p1_r001", "p1_r002"]}],
    }
    monkeypatch.setattr(visual_tables, "_verify", lambda *args: {
        "is_table": True, "member_ids": ["p1_r001", "p1_r002"],
        "confidence": 0.95, "reason": "표",
    })

    verify_visual_tables(Image.new("RGB", (200, 100)), page)

    assert len(page["regions"]) == 2
    assert all(region.get("kind") != "table" for region in page["regions"])
    assert page["table_checks"][0]["accepted"] is False


def test_paddle_box_containing_duplicate_region_is_not_promoted(monkeypatch):
    from nh_parser_fin.parse import visual_tables

    page = {
        "page_no": 1, "canvas": [200, 100],
        "regions": [
            _region("p1_r001", [10, 10, 190, 90], "우대금리 급여이체 0.2%p", label="table"),
            _region("p1_r002", [20, 20, 180, 40], "급여이체 0.2%p"),
        ],
    }
    monkeypatch.setattr(visual_tables, "_verify", lambda *args: {
        "is_table": True, "member_ids": ["p1_r001"],
        "confidence": 0.98, "reason": "표처럼 보임",
    })

    assert verify_visual_tables(Image.new("RGB", (200, 100)), page)["accepted"] == 0
    assert page["table_checks"][0]["accepted"] is False
    assert page["regions"][0]["needs_review"] is True


def test_hwp_cells_stay_in_p1_but_p3_has_only_table_kind_and_text():
    region = _region("p1_r001", [10, 10, 180, 40], "대출기간 | 2년")
    region["kind"] = "table"
    region["table"] = {
        "source": "document_processor", "grid": {"rows": 1, "cols": 2},
        "cells": [
            {"row": 0, "col": 0, "text": "대출기간", "bbox": [10, 10, 70, 40]},
            {"row": 0, "col": 1, "text": "2년", "bbox": [80, 10, 180, 40]},
        ],
    }
    p1 = build_p1({"pages": [{"page_no": 1, "canvas": [200, 100], "regions": [region]}]})
    p3_region = build_p3(p1)["pages"][0]["regions"][0]

    assert len(p1["pages"][0]["regions"][0]["table"]["cells"]) == 2
    assert p3_region["kind"] == "table"
    assert p3_region["selected_text"] == "대출기간 | 2년"
    assert "table" not in p3_region
