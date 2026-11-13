import pytest

from mib_shared import make_slug


def test_example_title():
    assert make_slug("PMK No. 11 Tahun 2025", fallback="x") == "pmk-no-11-tahun-2025"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Café Résumé Ünï", "cafe-resume-uni"),
        ("a,,;  b -- c", "a-b-c"),
        ("  --Hello World!!--  ", "hello-world"),
        ("UU Nomor 7/2021", "uu-nomor-7-2021"),
        ("MiXeD CaSe", "mixed-case"),
    ],
)
def test_normalisation(text, expected):
    assert make_slug(text, fallback="x") == expected


def test_symbols_only_gives_fallback():
    assert make_slug("!!! ???", fallback="Putusan 12") == "putusan-12"
    assert make_slug("", fallback="abc") == "abc"
    assert make_slug("中文标题", fallback="abc") == "abc"


def test_empty_fallback_gives_dokumen():
    assert make_slug("!!!", fallback="") == "dokumen"
    assert make_slug("", fallback="***") == "dokumen"


def test_long_text_cut_on_dash():
    slug = make_slug("aaa bbb ccc ddd", fallback="x", max_length=9)
    assert slug == "aaa-bbb"
    assert not slug.endswith("-")


def test_cut_exactly_at_boundary_keeps_word():
    assert make_slug("aaa bbb ccc", fallback="x", max_length=7) == "aaa-bbb"


def test_long_single_word_is_hard_cut():
    assert make_slug("a" * 50, fallback="x", max_length=10) == "a" * 10


def test_default_max_length_120():
    slug = make_slug(" ".join(["word"] * 100), fallback="x")
    assert len(slug) <= 120
    assert not slug.endswith("-")


def test_deterministic():
    title = "Peraturan Menteri Keuangan Nomor 11/PMK.03/2025"
    assert make_slug(title, fallback="x") == make_slug(title, fallback="x")
