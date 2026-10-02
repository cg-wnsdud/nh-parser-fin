"""폼 XObject 안 글자도 페이지 좌표로 줄을 만든다 — Chromium 이 인쇄한 HWP 렌더 PDF."""

import pypdfium2 as pdfium

from nh_parser_fin.ingest.triage import _space_before_next, extract_digital_lines


def _form_pdf() -> bytes:
    """본문을 축소·y뒤집기 폼 하나에 넣은 PDF. Chromium 인쇄본과 같은 배치다.

    `(AB) Tj 60 0 Td (CD) Tj` 사이 공백은 pdfium 이 만들어 넣는 공백이라 소속 TEXT
    런이 없다. 옛 코드는 이 공백을 폼 좌표의 런 상자에 맞춰 붙여 아래 줄로 보냈다.
    """
    form = (b"BT /H 40 Tf 1 0 0 -1 40 120 Tm (AB) Tj 60 0 Td (CD) Tj ET\n"
            b"BT /H 40 Tf 1 0 0 -1 40 900 Tm (EF) Tj 60 0 Td (GH) Tj ET\n")
    content = b"q 0.24 0 0 -0.24 20 290 cm /F1 Do Q\n"
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]"
        b"/Resources<</XObject<</F1 4 0 R>>>>/Contents 5 0 R>>",
        b"<</Type/XObject/Subtype/Form/BBox[0 0 1200 1200]/Resources<</Font<</H 6 0 R>>>>"
        b"/Length %d>>stream\n" % len(form) + form + b"endstream",
        b"<</Length %d>>stream\n" % len(content) + content + b"endstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


def test_lines_inside_form_xobject_keep_their_spaces_and_height():
    page = pdfium.PdfDocument(_form_pdf())[0]

    lines = extract_digital_lines(page, 1.0)

    assert [line.text for line in lines] == ["AB CD", "EF GH"]
    # 옛 코드: 첫 줄이 `ABCD` [29, 31, 62, 226] — 공백 하나가 아래 줄 높이까지 끌고 왔다.
    assert all(line.bbox[3] - line.bbox[1] < 20 for line in lines)


def _char(ch, box, owner=None):
    return (ch, box, box, None, owner)


def test_space_drawn_inside_next_glyph_moves_to_previous_glyph_end():
    # 004-예금성 렌더 실측 모양: `C` 끝 364, `다` 시작 381, 공백 상자 383(다음 글자 안쪽).
    chars = [_char("C", (338, 254, 364, 287), 1), _char(" ", (383, 286, 383, 286)),
             _char("다", (381, 250, 421, 291), 2)]

    moved = _space_before_next(chars, 1)

    assert moved[1] == (364, 286, 364, 286)


def test_space_already_between_glyphs_is_untouched():
    chars = [_char("]", (220, 399, 227, 428), 1), _char(" ", (230, 416, 230, 416)),
             _char("N", (233, 403, 251, 425), 2)]

    assert _space_before_next(chars, 1) is chars[1]
