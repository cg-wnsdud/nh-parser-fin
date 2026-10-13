"""여러 페이지·화면·파일로 나뉜 광고 한 건을 문서 하나로 다룬다."""

import sys

from PIL import Image

import run
from nh_parser_fin.ingest.loader import LabPage
from nh_parser_fin.parse import export, pipeline, semantic
from nh_parser_fin.parse.ids import normalize_region_ids


def _page(regions):
    return {"page_no": 1, "canvas": [1000, 1000], "regions": regions}


def _capture_chat(monkeypatch, response):
    calls = []

    def fake_chat_json(parts, *, schema_name, schema, max_tokens):
        calls.append({"prompt": parts[0]["text"], "schema": schema})
        return response

    monkeypatch.setattr(semantic.vlm_client, "chat_json", fake_chat_json)
    return calls


def _ownership(product_id="product_1"):
    return {
        "analysis": "", "products": [{"product_id": product_id, "name": "통장"}],
        "region_decisions": [{"region_id": "r1", "product_id": product_id, "confidence": 0.9}],
        "recovery_decisions": [], "table_areas": [], "missing_visible_text": [],
    }


def test_first_ownership_call_is_unchanged_without_known_products(monkeypatch):
    calls = _capture_chat(monkeypatch, _ownership())
    page = _page([{"region_id": "r1", "bbox": [0, 0, 10, 10]}])
    image = Image.new("RGB", (1000, 1000))

    semantic.analyze_page_ownership(image, page, [])
    semantic.analyze_page_ownership(image, page, [], known_products=[])

    assert calls[0] == calls[1]
    assert "이미 확인된 상품" not in calls[0]["prompt"]
    products = calls[0]["schema"]["properties"]["products"]["items"]["properties"]
    assert products["product_id"]["enum"] == semantic.PRODUCT_IDS[1:5]


def test_known_products_reach_the_next_call_and_make_room_for_new_ids(monkeypatch):
    calls = _capture_chat(monkeypatch, _ownership("product_5"))
    page = _page([{"region_id": "r1", "bbox": [0, 0, 10, 10]}])
    known = [
        {"product_id": f"product_{index}", "name": f"상품{index}", "product_group": "투자성"}
        for index in range(1, 5)
    ]

    result = semantic.analyze_page_ownership(
        Image.new("RGB", (1000, 1000)), page, [], known_products=known,
    )

    prompt = calls[0]["prompt"]
    assert "- product_1: 상품1 (투자성)" in prompt
    assert "화면 하나가 상품 하나는 아닙니다" in prompt
    region_enum = calls[0]["schema"]["properties"]["region_decisions"]["items"]["properties"]
    assert "product_6" in region_enum["product_id"]["enum"]
    # 넓힌 ID는 검증에서 unknown으로 바뀌지 않는다.
    assert result["region_decisions"][0]["product_id"] == "product_5"


def test_known_products_keep_first_definition_and_skip_unused_products():
    known = [{"product_id": "product_1", "name": "처음 이름"}]
    result = {
        "products": [
            {"product_id": "product_1", "name": "다른 이름"},
            {"product_id": "product_2", "name": "영역 없는 상품"},
        ],
        "region_decisions": [
            {"region_id": "r1", "product_id": "product_1"},
            {"region_id": "r2", "product_id": "product_3"},
            {"region_id": "r3", "product_id": "page_common"},
        ],
        "recovery_decisions": [],
    }

    merged = semantic.merge_known_products(known, result)

    assert [item["product_id"] for item in merged] == ["product_1", "product_3"]
    assert merged[0]["name"] == "처음 이름"
    assert known == [{"product_id": "product_1", "name": "처음 이름"}]


def test_long_page_bands_pass_products_found_earlier(monkeypatch):
    seen = []

    def fake_ownership(image, page, candidates, **kwargs):
        seen.append([item["product_id"] for item in kwargs.get("known_products") or []])
        return {
            "analysis": "", "products": [{"product_id": "product_1", "name": "적금"}],
            "region_decisions": [
                {"region_id": region["region_id"], "product_id": "product_1", "confidence": 0.9}
                for region in page["regions"]
            ],
            "recovery_decisions": [], "table_areas": [], "missing_visible_text": [],
        }

    monkeypatch.setattr(semantic, "analyze_page_ownership", fake_ownership)
    page = {
        "canvas": [1000, 3000],
        "tiling": {"decision": "split", "axis": "y", "pieces": 2},
        "regions": [
            {"region_id": "r1", "bbox": [0, 100, 500, 200]},
            {"region_id": "r2", "bbox": [0, 2600, 500, 2700]},
        ],
    }

    result = semantic.analyze_page_context(Image.new("RGB", (1000, 3000)), page, [])

    assert seen == [[], ["product_1"]]
    assert {item["product_id"] for item in result["region_decisions"]} == {"product_1"}


def test_pipeline_gives_each_page_the_products_of_earlier_pages(monkeypatch, tmp_path):
    seen = []

    def fake_context(image, page, candidates, known_products=None):
        seen.append([item["product_id"] for item in known_products or []])
        return {
            "analysis": "", "products": [{"product_id": "product_1", "name": "통장"}],
            "region_decisions": [
                {"region_id": region["region_id"], "product_id": "product_1", "confidence": 0.9}
                for region in page["regions"]
            ],
            "recovery_decisions": [], "table_areas": [], "missing_visible_text": [],
        }

    class Stop(Exception):
        pass

    def stop_after_ownership(*args, **kwargs):
        raise Stop

    monkeypatch.setattr(pipeline, "analyze_page_context", fake_context)
    monkeypatch.setattr(pipeline, "resolve_product_templates", stop_after_ownership)
    for name in ("_place_tables", "align_hwp_structure", "verify_visual_tables", "anchor_page"):
        monkeypatch.setattr(pipeline, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline.reading, "read_page", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline.vlm_client, "classify", lambda image, name: semantic_classification())
    monkeypatch.setattr(pipeline.Image, "open", lambda path: Image.new("RGB", (10, 10)))
    document = {
        "source_file": "a.png + b.png",
        "doc_id": "ad",
        "pages": [
            {"page_no": number, "canvas": [10, 10], "regions": [
                {"region_id": f"p{number}_r001", "bbox": [0, 0, 5, 5], "lines": []},
            ], "unassigned_lines": []}
            for number in (1, 2, 3)
        ],
    }
    tasks = [
        {"data": {"image": f"/x/?d=pages/p{number}.png", "source_file": "a.png + b.png", "page_no": number}}
        for number in (1, 2, 3)
    ]

    try:
        pipeline.run_full_pipeline([document], tasks, out=tmp_path, media_dir=tmp_path)
    except Stop:
        pass

    assert seen == [[], ["product_1"], ["product_1"]]


def semantic_classification():
    from nh_parser_fin.vlm.client import Classification

    return Classification(
        product_group="예금성", ad_type=None, confidence=1.0, category_source="vlm", reason="",
    )


def test_ad_group_makes_one_document_with_running_page_numbers(monkeypatch, tmp_path):
    sources = [tmp_path / "a.pdf", tmp_path / "b.png"]
    pages = {
        "a.pdf": [LabPage("a", "a.pdf", number, Image.new("RGB", (100, 100), "white"))
                  for number in (1, 2)],
        "b.png": [LabPage("b", "b.png", 1, Image.new("RGB", (100, 100), "white"))],
    }
    observed = {}

    monkeypatch.setattr(run, "iter_inputs", lambda paths, exclude: sources)
    monkeypatch.setattr(run, "load_pages", lambda path, **kwargs: pages[path.name])
    monkeypatch.setattr(run, "_digital_lines", lambda pdf, page_no, origin, supplied=None: [])
    monkeypatch.setattr(run.client, "call", lambda *args, **kwargs: {"pruned": {}, "seconds": 0.0})
    monkeypatch.setattr(run, "OUTPUT_ROOT", tmp_path / "outputs")
    monkeypatch.setattr(run, "MEDIA_DIR", tmp_path / "media")

    class FakePdf:
        def __init__(self, path):
            pass

    monkeypatch.setitem(sys.modules, "pypdfium2", type(sys)("pypdfium2"))
    sys.modules["pypdfium2"].PdfDocument = FakePdf

    def fake_full_pipeline(documents, tasks, **kwargs):
        observed.update(documents=documents, tasks=tasks)
        return [], []

    monkeypatch.setattr(pipeline, "run_full_pipeline", fake_full_pipeline)
    monkeypatch.setattr(sys, "argv", [
        "run.py", "--run-name", "demo", "--input", str(tmp_path), "--ad-group", "광고1",
    ])

    run.main()

    [document] = observed["documents"]
    assert document["doc_id"] == "광고1"
    assert document["source_file"] == "a.pdf + b.png"
    assert document["file_type"] == "mixed"
    assert [page["page_no"] for page in document["pages"]] == [1, 2, 3]
    assert [(page["source_file"], page["source_page_no"]) for page in document["pages"]] == [
        ("a.pdf", 1), ("a.pdf", 2), ("b.png", 1),
    ]
    assert {task["data"]["source_file"] for task in observed["tasks"]} == {"a.pdf + b.png"}
    assert [task["data"]["page_no"] for task in observed["tasks"]] == [1, 2, 3]


def _p1_document(pages):
    return {
        "doc_id": "doc", "source_file": "doc.png", "file_type": "png",
        "classification": {}, "template": {"template_id": "예금성상품-입출식"},
        "pages": pages,
    }


def _region(region_id):
    return {
        "region_id": region_id, "bbox": [0, 0, 10, 10], "text": "문구", "product_id": "product_1",
        "semantic_labels": [], "needs_review": False, "text_source": "vlm",
    }


def test_p3_names_the_source_file_only_for_grouped_pages():
    single = _p1_document([{"page_no": 1, "canvas": [10, 10], "source_file": "doc.png",
                            "regions": [_region("p1_x1")]}])
    grouped = _p1_document([
        {"page_no": number, "canvas": [10, 10], "source_file": name, "source_page_no": 1,
         "regions": [_region(f"p{number}_x1")]}
        for number, name in ((1, "a.png"), (2, "b.png"))
    ])
    for document in (single, grouped):
        normalize_region_ids(document["pages"])

    single_page = export.build_p3(export.build_p1(single))["pages"][0]
    grouped_pages = export.build_p3(export.build_p1(grouped))["pages"]

    assert "source_file" not in single_page and "source_page_no" not in single_page
    assert [(page["source_file"], page["source_page_no"]) for page in grouped_pages] == [
        ("a.png", 1), ("b.png", 1),
    ]
    assert [page["regions"][0]["region_id"] for page in grouped_pages] == ["p1_r001", "p2_r001"]
