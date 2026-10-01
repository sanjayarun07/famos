"""Notices that are not in English.

Most Indian school circulars are not, and English-only OCR does not read them
badly -- it reads them as noise. A Devanagari notice run through `-l eng`
comes back as "feren afex diaz darstt Sa".
"""
import io

import pytest

from familyos.extraction import parse as parse_mod
from familyos.extraction.ground import locate
from familyos.extraction.parse import detect_scripts, parse
from familyos.settings import settings

HINDI = [
    "विद्या मंदिर सीनियर सेकेंडरी स्कूल",
    "प्रिय अभिभावक,",
    "वार्षिक दिवस समारोह शुक्रवार 14 नवंबर 2026 को",
    "शाम 5:00 बजे विद्यालय सभागार में होगा।",
    "कृपया 7 नवंबर 2026 तक उपस्थिति की पुष्टि करें।",
]


def _devanagari_font(size: int):
    from PIL import ImageFont
    for path in ("/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc",
                 "/System/Library/Fonts/Supplemental/Kohinoor.ttc",
                 "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
                 "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return None


def _image(lines, font):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1000, 120 + 52 * len(lines)), "white")
    d = ImageDraw.Draw(im)
    y = 40
    for line in lines:
        d.text((50, y), line, font=font, fill="black")
        y += 52
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# ----------------------------------------------------------------------------
# script detection, which needs nothing installed
# ----------------------------------------------------------------------------

def test_scripts_are_detected_not_languages():
    """Hindi, Marathi and Nepali share Devanagari, so this reports the script
    and does not pretend to know the language."""
    assert detect_scripts("वार्षिक दिवस समारोह शुक्रवार नवंबर") == ["devanagari"]
    assert detect_scripts("Annual Day on Friday 14 November") == ["latin"]
    assert detect_scripts("ஆண்டு விழா வெள்ளிக்கிழமை") == ["tamil"]


def test_a_stray_english_word_does_not_make_a_notice_bilingual():
    mostly_hindi = "वार्षिक दिवस समारोह शुक्रवार नवंबर विद्यालय सभागार उपस्थिति पुष्टि PTA"
    assert detect_scripts(mostly_hindi) == ["devanagari"]
    both = "वार्षिक दिवस समारोह Annual Day Celebration Friday November"
    assert set(detect_scripts(both)) == {"devanagari", "latin"}


def test_digits_alone_say_nothing_about_script():
    assert detect_scripts("2026 200 14") == []
    assert detect_scripts("") == []


def test_the_configured_languages_reach_tesseract(monkeypatch):
    """A deployment serving Hindi-medium schools has to say so; nothing infers
    it, because Tesseract needs the language before it reads anything."""
    seen = {}

    class FakeOutput:
        DICT = "dict"

    def fake_image_to_data(_im, lang=None, output_type=None):
        seen["lang"] = lang
        return {"text": [], "conf": [], "left": [], "top": [], "width": [], "height": [],
                "block_num": [], "par_num": [], "line_num": []}

    import sys
    import types
    fake = types.ModuleType("pytesseract")
    fake.image_to_data = fake_image_to_data
    fake.Output = FakeOutput
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    monkeypatch.setattr(settings, "ocr_languages", "eng+hin")

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(buf, format="PNG")
    parse_mod._ocr(buf.getvalue(), 1.0, [])
    assert seen["lang"] == "eng+hin"


# ----------------------------------------------------------------------------
# the real thing, when the language pack is installed
# ----------------------------------------------------------------------------

def _hindi_available() -> bool:
    try:
        import pytesseract
        return "hin" in pytesseract.get_languages()
    except Exception:
        return False


needs_hindi = pytest.mark.skipif(
    not _hindi_available() or _devanagari_font(10) is None,
    reason="needs the tesseract hin pack and a Devanagari font")


@needs_hindi
def test_a_hindi_notice_is_read_and_stays_in_its_own_script(monkeypatch):
    monkeypatch.setattr(settings, "ocr_languages", "eng+hin")
    doc = parse(_image(HINDI, _devanagari_font(34)), "image/png")
    text = doc.pages[0].text

    assert doc.scripts and doc.scripts[0] == "devanagari"
    assert "वार्षिक" in text and "नवंबर" in text
    assert doc.ocr_pages == 1


@needs_hindi
def test_english_alone_reads_a_hindi_notice_as_noise(monkeypatch):
    """Not worse English -- noise. This is why the setting has to be set."""
    font = _devanagari_font(34)
    monkeypatch.setattr(settings, "ocr_languages", "eng")
    english_only = parse(_image(HINDI, font), "image/png").pages[0].text
    monkeypatch.setattr(settings, "ocr_languages", "eng+hin")
    with_hindi = parse(_image(HINDI, font), "image/png").pages[0].text

    assert "वार्षिक" not in english_only
    assert "वार्षिक" in with_hindi


@needs_hindi
def test_a_devanagari_quote_can_still_be_grounded(monkeypatch):
    """Grounding is what makes a claim checkable, and it has to work in a
    script with no letter case and no Latin word shapes."""
    monkeypatch.setattr(settings, "ocr_languages", "eng+hin")
    doc = parse(_image(HINDI, _devanagari_font(34)), "image/png")
    line = next(ln for ln in doc.pages[0].text.splitlines() if "नवंबर" in ln)

    found = locate(doc, line.strip())
    assert found is not None
    assert found.match in ("exact", "normalized", "fuzzy")
    assert found.boxes, "a grounded Devanagari claim still needs a box to highlight"
