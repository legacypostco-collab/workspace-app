from __future__ import annotations

import json
from decimal import Decimal
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from django.conf import settings


def _to_decimal(value, default: Decimal = Decimal("0.00")) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception:
        return default


def _is_strict() -> bool:
    # Backward compatible with old TEUSTAT_STRICT_MODE flag.
    return bool(getattr(settings, "LOGISTICS_STRICT_MODE", False) or getattr(settings, "TEUSTAT_STRICT_MODE", False))


def _fallback_logistics_estimate(payload: dict, warning: str | None = None) -> dict:
    weight_kg = _to_decimal(payload.get("weight_kg") or 0)
    volume_m3 = _to_decimal(payload.get("volume_m3") or 0)
    mode = (payload.get("mode") or "sea").lower()
    incoterm = (payload.get("incoterm") or "FOB").upper()

    base = Decimal("120.00") + (weight_kg * Decimal("0.08")) + (volume_m3 * Decimal("18.00"))
    mode_coef = {"sea": Decimal("1.00"), "rail": Decimal("1.25"), "road": Decimal("1.45"), "air": Decimal("2.40")}.get(
        mode, Decimal("1.00")
    )
    incoterm_coef = {"FOB": Decimal("1.00"), "CIF": Decimal("1.15"), "DDP": Decimal("1.35")}.get(
        incoterm, Decimal("1.00")
    )
    total = (base * mode_coef * incoterm_coef).quantize(Decimal("0.01"))
    return {
        "ok": True,
        "provider": "internal_fallback",
        "currency": "USD",
        "cost": str(total),
        "warning": warning or "External logistics API unavailable, fallback formula used.",
    }


def _extract_cost_from_response(data: dict) -> Decimal | None:
    for key in ("total_usd", "total", "cost", "price", "amount", "rate", "quote", "estimated_cost"):
        if key in data:
            val = _to_decimal(data.get(key), default=Decimal("-1"))
            if val >= 0:
                return val
    nested = data.get("result")
    if isinstance(nested, dict):
        return _extract_cost_from_response(nested)
    nested = data.get("data")
    if isinstance(nested, dict):
        return _extract_cost_from_response(nested)
    return None


def _build_teustat_contract_payload(payload: dict) -> dict:
    contract = settings.TEUSTAT_CONTRACT_VERSION
    if contract == "teustat_v1":
        return {
            "route": {
                "origin": payload.get("origin") or "",
                "destination": payload.get("destination") or "",
            },
            "cargo": {
                "weight_kg": str(payload.get("weight_kg") or "0"),
                "volume_m3": str(payload.get("volume_m3") or "0"),
            },
            "terms": {
                "mode": (payload.get("mode") or "sea").lower(),
                "incoterm": (payload.get("incoterm") or "FOB").upper(),
                "currency": (payload.get("currency") or "USD").upper(),
            },
        }
    return payload


def _provider_settings(provider: str) -> tuple[str, str, float, dict, dict, str]:
    provider = (provider or "").strip().lower()
    if provider == "searates":
        return (
            settings.SEARATES_API_URL,
            settings.SEARATES_API_KEY,
            settings.SEARATES_TIMEOUT_SEC,
            {"Content-Type": "application/json"},
            payload_identity,
            "searates",
        )
    if provider == "freightos":
        return (
            settings.FREIGHTOS_API_URL,
            settings.FREIGHTOS_API_KEY,
            settings.FREIGHTOS_TIMEOUT_SEC,
            {"Content-Type": "application/json"},
            payload_identity,
            "freightos",
        )
    if provider == "xeneta":
        return (
            settings.XENETA_API_URL,
            settings.XENETA_API_KEY,
            settings.XENETA_TIMEOUT_SEC,
            {"Content-Type": "application/json"},
            payload_identity,
            "xeneta",
        )
    return (
        settings.TEUSTAT_API_URL,
        settings.TEUSTAT_API_KEY,
        settings.TEUSTAT_TIMEOUT_SEC,
        {"Content-Type": "application/json"},
        _build_teustat_contract_payload,
        "teustat",
    )


def payload_identity(payload: dict) -> dict:
    return payload


def _request_external(provider: str, payload: dict) -> dict:
    api_url, api_key, timeout_sec, headers, payload_builder, normalized_provider = _provider_settings(provider)
    strict_mode = _is_strict()

    if not api_url:
        if strict_mode:
            return {
                "ok": False,
                "provider": normalized_provider,
                "error": f"{normalized_provider.upper()} API URL is not configured (strict mode).",
            }
        return _fallback_logistics_estimate(payload, warning=f"{normalized_provider} api url missing; fallback used.")

    request_payload = payload_builder(payload)
    body = json.dumps(request_payload).encode("utf-8")
    req_headers = dict(headers)
    if api_key:
        req_headers["Authorization"] = f"Bearer {api_key}"

    req = Request(api_url, data=body, headers=req_headers, method="POST")
    try:
        with urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        amount = _extract_cost_from_response(data)
        if amount is None:
            if strict_mode:
                return {
                    "ok": False,
                    "provider": normalized_provider,
                    "error": f"{normalized_provider} response does not contain cost field.",
                    "raw": data,
                }
            return _fallback_logistics_estimate(payload, warning=f"{normalized_provider} response without cost; fallback used.")

        return {
            "ok": True,
            "provider": normalized_provider,
            "currency": str(data.get("currency") or payload.get("currency") or "USD"),
            "cost": str(amount.quantize(Decimal("0.01"))),
            "raw": data,
        }
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        if strict_mode:
            return {
                "ok": False,
                "provider": normalized_provider,
                "error": f"{normalized_provider} request failed in strict mode: {exc.__class__.__name__}",
            }
        return _fallback_logistics_estimate(payload, warning=f"{normalized_provider} request failed; fallback used.")


def logistics_estimate(payload: dict) -> dict:
    provider = (getattr(settings, "LOGISTICS_PROVIDER", "teustat") or "teustat").strip().lower()
    if provider == "internal":
        return _fallback_logistics_estimate(payload, warning="internal formula selected by LOGISTICS_PROVIDER=internal")
    return _request_external(provider, payload)


def teustat_logistics_estimate(payload: dict) -> dict:
    # Backward-compatible alias used by existing views.
    return logistics_estimate(payload)
