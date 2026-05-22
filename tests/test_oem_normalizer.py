"""Unit-тесты oem_normalizer — rule-based нормализация артикулов."""
from assistant.oem_normalizer import (
    canonical_brand,
    expand_query_for_db,
    match_against_candidates,
    normalize_oem,
)


# ── базовые нормализации ──────────────────────────────────────

def test_strip_separators():
    assert "7079958030" in normalize_oem("707-99-58030")
    assert "7079958030" in normalize_oem("707.99.58030")
    assert "7079958030" in normalize_oem("707 99 58030")
    assert "7079958030" in normalize_oem("707/99/58030")


def test_strip_leading_zeros():
    out = normalize_oem("0707995803")
    assert "0707995803" in out
    assert "707995803" in out  # без leading zero


def test_caterpillar_bidirectional():
    """CAT-265-0235 и 265-0235 находят друг друга через нормализацию."""
    a = normalize_oem("CAT-265-0235", brand="Caterpillar")
    b = normalize_oem("265-0235", brand="Caterpillar")
    # Должны иметь хотя бы один общий канонический вариант
    assert set(a) & set(b), f"no overlap between {a} and {b}"


def test_replacement_suffix_E_kept_as_variant():
    """707-99-58030E (заменитель) и 707-99-58030 (оригинал) связаны."""
    repl = normalize_oem("707-99-58030E")
    orig = normalize_oem("707-99-58030")
    # base 707-99-58030 присутствует в обоих списках
    assert "707-99-58030" in repl
    assert "707-99-58030" in orig


def test_empty_input():
    assert normalize_oem("") == []
    assert normalize_oem(None) == []
    assert normalize_oem("   ") == []


def test_canonical_brand_normalization():
    assert canonical_brand("Caterpillar") == "cat"
    assert canonical_brand("CAT") == "cat"
    assert canonical_brand("cat.") == "cat"
    assert canonical_brand("Komatsu®") == "komatsu"
    assert canonical_brand("") == ""
    assert canonical_brand(None) == ""


def test_no_double_prefix():
    """KM-PC400 + brand Komatsu НЕ должен превратиться в KM-KM-PC400."""
    out = normalize_oem("KM-PC400", brand="Komatsu")
    assert all("KM-KM-" not in v for v in out), out


# ── expand_query_for_db ───────────────────────────────────────

def test_freetext_query_not_normalized():
    """Свободный текст «гидроцилиндр стрелы» не должен бить на части."""
    assert expand_query_for_db("гидроцилиндр стрелы") == ["гидроцилиндр стрелы"]


def test_oem_token_expanded():
    out = expand_query_for_db("707-99-58030")
    assert "707-99-58030" in out
    assert "7079958030" in out


# ── match_against_candidates (cross-check) ────────────────────

def test_match_via_normalized_form():
    """Если в БД хранится `7079958030`, query `707-99-58030` должен матчить."""
    candidates = normalize_oem("707-99-58030")
    assert match_against_candidates(candidates, "7079958030")
    assert match_against_candidates(candidates, "707-99-58030")
    assert not match_against_candidates(candidates, "265-0235")


def test_dedup_preserves_order():
    """Первый кандидат — самая точная форма (raw input)."""
    out = normalize_oem("707-99-58030")
    assert out[0] == "707-99-58030"
