"""HWPX 쪽 배경·떠 있는 그림을 검토 HTML 겹으로 되살린다."""

import io
import zipfile

from document_processor import DocIR, ImageIR, PageInfo, ParagraphIR, RunIR
from document_processor.models import NativeAnchor

from nh_parser_fin.ingest.hwp_layers import (
    detach_floating_images, inject_layers, page_fill_placements, read_layers,
)

_NS = (
    'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
    'xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head" '
    'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" '
    'xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"'
)


def _hwpx(*, page_fill: bool, behind_pic: bool) -> bytes:
    header = (
        f'<hh:head {_NS}><hh:refList><hh:borderFills>'
        '<hh:borderFill id="1"/>'
        '<hh:borderFill id="6"><hc:fillBrush><hc:imgBrush mode="TOTAL">'
        '<hc:img binaryItemIDRef="image1"/></hc:imgBrush></hc:fillBrush></hh:borderFill>'
        '</hh:borderFills></hh:refList></hh:head>'
    )
    fill_ref = "6" if page_fill else "1"
    pic = (
        '<hp:pic textWrap="BEHIND_TEXT" zOrder="0"><hp:sz width="61261" height="85908"/>'
        '<hp:pos treatAsChar="0" vertRelTo="PAPER" horzRelTo="PAPER" vertAlign="TOP" '
        'horzAlign="LEFT" vertOffset="4294966459" horzOffset="0"/>'
        '<hc:img binaryItemIDRef="image1"/></hp:pic>'
        if behind_pic else ""
    )
    section = (
        f'<hs:sec {_NS}><hp:p><hp:run><hp:secPr>'
        '<hp:visibility fill="SHOW_ALL"/>'
        '<hp:pagePr width="59528" height="84188"><hp:margin left="5669" right="5669" '
        'top="4252" bottom="4252" header="0" footer="0"/></hp:pagePr>'
        f'<hp:pageBorderFill type="BOTH" borderFillIDRef="{fill_ref}" fillArea="PAPER"/>'
        '<hp:pageBorderFill type="EVEN" borderFillIDRef="1" fillArea="PAPER"/>'
        f'</hp:secPr>{pic}<hp:t>본문</hp:t></hp:run></hp:p></hs:sec>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Contents/header.xml", header)
        archive.writestr("Contents/section0.xml", section)
        archive.writestr("BinData/image1.jpg", b"\xff\xd8fakejpeg")
    return buffer.getvalue()


def _docir(with_image: bool) -> DocIR:
    content = [RunIR(text="본문", page_number=1)]
    if with_image:
        content.append(ImageIR(
            image_id="img1", page_number=1,
            native_anchor=NativeAnchor(node_kind="image", debug_path="s1.p1.img1"),
        ))
    paragraph = ParagraphIR(
        content=content,
        native_anchor=NativeAnchor(node_kind="paragraph", debug_path="s1.p1"),
    )
    return DocIR(pages=[PageInfo(page_number=1, width_pt=595.28, height_pt=841.88)],
                 paragraphs=[paragraph])


_PAGE_HTML = (
    '<html><head></head><body><section class="document-page" data-page-number="1" style="">'
    '<div class="document-page__content">본문</div></section></body></html>'
)


def test_page_background_image_is_restored_behind_content():
    layers = read_layers(_hwpx(page_fill=True, behind_pic=False))
    docir = _docir(with_image=False)

    placements = page_fill_placements(docir, layers)
    html, records = inject_layers(_PAGE_HTML, docir, layers, placements)

    assert [item["kind"] for item in records] == ["page_fill"]
    assert records[0]["box_pt"] == [0.0, 0.0, 595.28, 841.88]
    # 겹은 본문 div 앞(형제)에 들어가 fitPages 축소와 node bbox 에 끼지 않는다.
    assert html.index('data-nh-layer="page_fill"') < html.index("document-page__content\">")
    assert "data:image/jpeg;base64," in html
    assert "z-index:0" in html


def test_behind_text_picture_leaves_flow_and_keeps_paper_position():
    layers = read_layers(_hwpx(page_fill=False, behind_pic=True))
    docir = _docir(with_image=True)

    placements = detach_floating_images(docir, layers)

    assert not any(isinstance(node, ImageIR) for node in docir.paragraphs[0].content)
    assert placements[0]["wrap"] == "BEHIND_TEXT"
    _, records = inject_layers(_PAGE_HTML, docir, layers, placements)
    # 4294966459 HWPUNIT = -837 → -8.37pt (용지 위로 살짝 넘친다, 006 실측과 같은 꼴)
    assert records[0]["box_pt"][:2] == [0.0, -8.37]
    assert records[0]["box_pt"][2:] == [612.61, 850.71]


def test_document_without_image_fill_gets_no_layers():
    layers = read_layers(_hwpx(page_fill=False, behind_pic=False))

    assert page_fill_placements(_docir(with_image=False), layers) == []
    assert layers["floats"] == {}
