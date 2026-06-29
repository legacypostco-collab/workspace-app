"""Tests: матрица прав action-действий по ролям.

Проверяем can_execute() — функцию-gatekeeper в assistant/actions.py.

Ключевые инварианты:
  1. Buyer не может вызывать seller-only действия
  2. Buyer/seller не могут вызывать operator-only действия
  3. Operator не может вызывать admin-wildcard действия (они у него тоже нет через ROLE_ACTIONS)
  4. Admin (wildcard "*") проходит всё
  5. KAM исключения: op_resolve_dispute, op_kyb_approve и др. — недоступны KAM
  6. Неизвестная роль → всё заблокировано
"""
import pytest

from assistant.actions import can_execute, ROLE_ACTIONS


# ── Вспомогательные наборы ─────────────────────────────────────────────────

SELLER_ONLY_ACTIONS = [
    "respond_rfq",
    "upload_pricelist",
    "submit_quote",
    "advance_order",
    "seller_dashboard",
    "seller_finance",
    "request_payout",
]

OPERATOR_ONLY_ACTIONS = [
    "op_dashboard",
    "op_queue",
    "op_assign",
    "op_approve_kp",
    "op_kyb_approve",
    "op_kyb_reject",
    "op_confirm_topup",
    "op_resolve_dispute",
    "op_assign_carrier",
]

# Действия недоступные KAM (исполнительные writes — только оператор)
KAM_EXCLUDED_ACTIONS = [
    "op_resolve_dispute",
    "op_kyb_approve",
    "op_kyb_reject",
    "op_confirm_topup",
    "op_assign_carrier",
    "op_approve_kp",
    "op_compose_kp",
    "send_rfq_to_suppliers",
    "op_assign",
]

COMMON_BUYER_ACTIONS = [
    "search_parts",
    "create_rfq",
    "get_rfq_status",
    "get_my_deals",
    "get_orders",
    "pay_reserve",
    "get_balance",
    "topup_wallet",
]


# ── 1. Buyer не имеет seller-only действий ────────────────────────────────


@pytest.mark.parametrize("action", SELLER_ONLY_ACTIONS)
def test_buyer_cannot_execute_seller_only(action):
    assert not can_execute(action, "buyer"), (
        f"buyer не должен иметь доступ к '{action}'"
    )


# ── 2. Buyer/seller не имеют operator-only действий ──────────────────────


@pytest.mark.parametrize("action", OPERATOR_ONLY_ACTIONS)
def test_buyer_cannot_execute_operator_actions(action):
    assert not can_execute(action, "buyer"), (
        f"buyer не должен иметь доступ к '{action}'"
    )


@pytest.mark.parametrize("action", OPERATOR_ONLY_ACTIONS)
def test_seller_cannot_execute_operator_actions(action):
    assert not can_execute(action, "seller"), (
        f"seller не должен иметь доступ к '{action}'"
    )


# ── 3. Operator имеет operator-only действия ─────────────────────────────


@pytest.mark.parametrize("action", OPERATOR_ONLY_ACTIONS)
def test_operator_can_execute_operator_actions(action):
    assert can_execute(action, "operator"), (
        f"operator должен иметь доступ к '{action}'"
    )


# ── 4. Admin проходит всё ────────────────────────────────────────────────


@pytest.mark.parametrize("action", SELLER_ONLY_ACTIONS + OPERATOR_ONLY_ACTIONS + COMMON_BUYER_ACTIONS)
def test_admin_can_execute_everything(action):
    assert can_execute(action, "admin"), (
        f"admin должен иметь доступ к '{action}'"
    )


# ── 5. Buyer имеет базовые действия ─────────────────────────────────────


@pytest.mark.parametrize("action", COMMON_BUYER_ACTIONS)
def test_buyer_has_common_actions(action):
    assert can_execute(action, "buyer"), (
        f"buyer должен иметь доступ к '{action}'"
    )


# ── 6. Seller наследует buyer-действия ───────────────────────────────────


@pytest.mark.parametrize("action", COMMON_BUYER_ACTIONS)
def test_seller_inherits_buyer_actions(action):
    assert can_execute(action, "seller"), (
        f"seller должен иметь доступ к buyer-действию '{action}'"
    )


# ── 7. KAM не имеет исполнительных writes ────────────────────────────────


@pytest.mark.parametrize("action", KAM_EXCLUDED_ACTIONS)
def test_kam_excluded_from_executive_actions(action):
    assert not can_execute(action, "operator_manager"), (
        f"KAM (operator_manager) не должен иметь доступ к '{action}'"
    )


# ── 8. Operator_logist/customs/payment имеют core-действия ──────────────


@pytest.mark.parametrize("role", ["operator_logist", "operator_customs", "operator_payment"])
@pytest.mark.parametrize("action", ["op_dashboard", "op_queue", "get_orders", "audit_log"])
def test_operator_subroles_have_core_actions(role, action):
    assert can_execute(action, role), (
        f"{role} должен иметь доступ к '{action}'"
    )


# ── 9. Неизвестная роль → всё заблокировано ──────────────────────────────


@pytest.mark.parametrize("action", COMMON_BUYER_ACTIONS + OPERATOR_ONLY_ACTIONS)
def test_unknown_role_blocked(action):
    assert not can_execute(action, "unknown_role"), (
        f"unknown_role не должен иметь доступ к '{action}'"
    )


# ── 10. ROLE_ACTIONS не содержит дублей внутри одной роли ──────────────


def test_no_duplicate_actions_per_role():
    for role, actions in ROLE_ACTIONS.items():
        if actions == ["*"]:
            continue
        dupes = [a for a in set(actions) if actions.count(a) > 1]
        assert not dupes, f"Роль '{role}' содержит дубли: {dupes}"


# ── 11. Критические финансовые действия только у operator/admin ──────────


FINANCIAL_WRITE_ACTIONS = [
    "op_confirm_topup",
    "op_reject_topup",
]


@pytest.mark.parametrize("action", FINANCIAL_WRITE_ACTIONS)
@pytest.mark.parametrize("role", ["buyer", "seller", "operator_manager"])
def test_financial_writes_blocked_for_non_operators(action, role):
    assert not can_execute(action, role), (
        f"'{role}' не должен иметь финансовое действие '{action}'"
    )


@pytest.mark.parametrize("action", FINANCIAL_WRITE_ACTIONS)
def test_financial_writes_allowed_for_operator(action):
    assert can_execute(action, "operator"), (
        f"operator должен иметь финансовое действие '{action}'"
    )
