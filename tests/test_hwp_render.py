import pytest

from nh_parser_fin.ingest.hwp_render import backend_order


def test_linux_prefers_reconstructed_html_before_libreoffice():
    assert backend_order("auto", platform="posix") == [
        "document_processor_html", "libreoffice",
    ]


def test_windows_keeps_hancom_as_highest_fidelity_renderer():
    assert backend_order("auto", platform="nt") == [
        "hancom", "document_processor_html", "libreoffice",
    ]


def test_explicit_backend_does_not_silently_change_renderer():
    assert backend_order("html", platform="posix") == ["document_processor_html"]
    assert backend_order("libreoffice", platform="nt") == ["libreoffice"]
    with pytest.raises(RuntimeError):
        backend_order("unknown", platform="posix")

