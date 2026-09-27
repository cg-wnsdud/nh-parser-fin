import pytest

from nh_parser_fin.ingest.hwp_render import backend_order


def test_auto_uses_local_html_then_libreoffice():
    assert backend_order("auto") == [
        "document_processor_html", "libreoffice",
    ]


def test_explicit_backend_does_not_silently_change_renderer():
    assert backend_order("html") == ["document_processor_html"]
    assert backend_order("libreoffice") == ["libreoffice"]
    with pytest.raises(RuntimeError, match="지원하지 않는 HWP_RENDER_BACKEND"):
        backend_order("hancom")
    with pytest.raises(RuntimeError):
        backend_order("unknown")

