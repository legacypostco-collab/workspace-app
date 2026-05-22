"""Unit tests for assistant.kyb_api_checks — 3 ТЗ paths (green/yellow/red)."""
from types import SimpleNamespace

import pytest

from assistant.kyb_api_checks import (
    check_address, check_messengers, check_opencorporates,
    check_ru_aggregator, check_sanctions, check_vies, check_website,
    evaluate_risk, run_all_checks,
)


def _kyb(**overrides):
    """Минимальный KYB-объект для check'ов."""
    defaults = dict(
        country="RU", inn="7708123456", vat_number="",
        legal_name="ООО «Тест»", warehouse_address="г. Москва, Промышленная 17",
        website="https://test.ru", phone="+74951234567",
        whatsapp="+74951234567", telegram="@test",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ── §3.1 RU агрегатор ───────────────────────────────────────────

def test_ru_aggregator_clean_company_green():
    """CleanCorp (ИНН 7708123456) — фикстура без red/yellow."""
    r = check_ru_aggregator("7708123456", "RU")
    assert r["ok"] is True
    assert r["data"]["risk_indicator"] == "green"
    assert r["signals"] == []


def test_ru_aggregator_old_company_red():
    """OldFabric (ИНН 5031000099) — ликвидация + ЕГРЮЛ-сигнал → red."""
    r = check_ru_aggregator("5031000099", "RU")
    assert r["ok"] is True
    assert r["data"]["risk_indicator"] == "red"
    msgs = [s["msg"] for s in r["signals"]]
    assert any("ликвидации" in m for m in msgs)
    assert any("ЕГРЮЛ" in m for m in msgs)


def test_ru_aggregator_not_applicable_for_non_ru():
    r = check_ru_aggregator("123", "AE")
    assert r["ok"] is False
    assert r["data"] is None


def test_ru_aggregator_unknown_inn_synthesized_green():
    """Любой неизвестный ИНН → синтез green-данных (deterministic)."""
    r = check_ru_aggregator("9999999999", "RU")
    assert r["ok"] is True
    assert r["data"]["risk_indicator"] == "green"
    assert r["data"]["ogrn"].startswith("10")


# ── §3.2 OpenCorporates ─────────────────────────────────────────

def test_opencorporates_uae_fixture():
    r = check_opencorporates("2233445", "AE")
    assert r["ok"] is True
    assert r["data"]["name"] == "Hydrolux Trading FZE"


def test_opencorporates_skipped_for_ru():
    r = check_opencorporates("7708", "RU")
    assert r["ok"] is False


def test_opencorporates_unknown_synth():
    r = check_opencorporates("UNKNOWN", "DE")
    assert r["ok"] is True
    assert r["data"]["company_number"] == "UNKNOWN"


# ── §3.3 VIES ───────────────────────────────────────────────────

def test_vies_non_eu_yellow_not_red():
    """Для AE невалидный VAT — yellow (нужна ручная сверка), не red."""
    r = check_vies("AE100123456700003", "AE")
    levels = [s["level"] for s in r["signals"]]
    assert "yellow" in levels
    assert "red" not in levels


def test_vies_default_valid_for_unknown():
    r = check_vies("DE123456789", "DE")
    assert r["data"]["valid"] is True
    assert r["signals"] == []


# ── §3.4 OpenSanctions ──────────────────────────────────────────

def test_sanctions_oldfabric_match():
    r = check_sanctions("ООО «Старый Завод»", ["Смирнов Виктор Геннадьевич"],
                         country="RU", fixture_key=("RU", "5031000099"))
    assert r["ok"] is True
    assert len(r["signals"]) >= 1
    assert r["signals"][0]["level"] == "red"


def test_sanctions_clean_company_no_match():
    r = check_sanctions("ООО «Чистая Корпорация»", ["Иванов И.И."],
                         country="RU", fixture_key=("RU", "7708123456"))
    assert r["signals"] == []


# ── §3.5 Maps ───────────────────────────────────────────────────

def test_address_industrial_no_signal():
    """Classifier ищет "ул. промышленная" / "industrial" / "промзона" в адресе."""
    r = check_address("г. Москва, ул. Промышленная д. 17")
    assert r["data"]["kind"] == "industrial"
    assert r["signals"] == []


def test_address_commercial_office_no_signal():
    """«офис» / «бизнес-центр» → commercial (без warning)."""
    r = check_address("Москва, бизнес-центр Лидер, офис 305")
    assert r["data"]["kind"] == "commercial"
    assert r["signals"] == []


def test_address_residential_yellow():
    r = check_address("г. Москва, Тверская улица 1, кв. 5")
    assert r["data"]["kind"] == "residential"
    assert any(s["level"] == "yellow" for s in r["signals"])


def test_address_empty_yellow():
    r = check_address("")
    assert any(s["level"] == "yellow" for s in r["signals"])


# ── §3.6 Website + Messengers ───────────────────────────────────

def test_website_valid_passes():
    r = check_website("https://example.com")
    assert r["data"]["reachable"] is True
    assert r["signals"] == []


def test_website_missing_yellow():
    r = check_website("")
    assert any(s["level"] == "yellow" for s in r["signals"])


def test_messenger_no_channels_yellow():
    r = check_messengers("", "", "+71234567890")
    assert any("мессенджер" in s["msg"].lower() for s in r["signals"])


def test_messenger_with_telegram_ok():
    r = check_messengers("", "@test", "+71234567890")
    assert r["signals"] == []


# ── Public façade: run_all_checks + evaluate_risk ────────────────

def test_evaluate_risk_red_wins():
    api = {
        "aggregator": {"signals": [{"level": "red", "msg": "Ликвидация"}]},
        "site":       {"signals": []},
    }
    decision, risk, reasons = evaluate_risk(api)
    assert risk == "red"
    assert decision == "auto_reject"
    assert len(reasons) == 1


def test_evaluate_risk_yellow_only():
    api = {
        "aggregator": {"signals": [{"level": "yellow", "msg": "Налоговая недоимка"}]},
    }
    decision, risk, _ = evaluate_risk(api)
    assert risk == "yellow"
    assert decision == "manual_review"


def test_evaluate_risk_no_signals_green():
    api = {"aggregator": {"signals": []}}
    decision, risk, reasons = evaluate_risk(api)
    assert risk == "green"
    assert decision == "sandbox_candidate"
    assert reasons == []


def test_run_all_checks_returns_7_sources():
    kyb = _kyb()
    results = run_all_checks(kyb)
    assert set(results.keys()) == {"aggregator", "opencorporates", "vies",
                                    "sanctions", "maps", "site", "messenger"}


def test_run_all_checks_oldfabric_full_red():
    """Полный e2e сценарий C: ликвидация + санкции → red verdict."""
    kyb = _kyb(country="RU", inn="5031000099", legal_name="ООО «Старый Завод»",
                warehouse_address="Заводская 3А")
    results = run_all_checks(kyb)
    decision, risk, reasons = evaluate_risk(results)
    assert risk == "red"
    assert decision == "auto_reject"
    assert any("ликвидации" in r for r in reasons)
