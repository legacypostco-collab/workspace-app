"""E2E: demo-login + chat-first основные элементы для трёх ролей.

Demo-аккаунты обычно имеют существующие conversations, так что chat-first
сразу открывает последний разговор (welcome-stage скрыт). Тесты проверяют
устойчивые элементы: topbar, sidebar, role-toggle, и заголовок (даже если
hidden, его текст должен матчить роль).
"""
from __future__ import annotations

from playwright.sync_api import Page, expect


def test_buyer_landing_renders_chat_ui(buyer_page: Page):
    """Buyer landing: sidebar + topbar + аватар + welcomeTitle с buyer-текстом."""
    page = buyer_page
    expect(page.locator("#sidebar")).to_be_attached(timeout=10000)
    expect(page.locator("#topAvatar")).to_be_visible()
    # welcomeTitle всегда в DOM (может быть hidden если есть convs)
    title_text = page.locator("#welcomeTitle").inner_text(timeout=5000)
    assert "запчасть" in title_text.lower() or "найти" in title_text.lower(), \
        f"buyer title mismatch: {title_text!r}"


def test_seller_landing_has_seller_title(seller_page: Page):
    """Seller-роль: title содержит «работе» (что в работе сегодня)."""
    page = seller_page
    title_text = page.locator("#welcomeTitle").inner_text(timeout=5000)
    assert "работе" in title_text.lower() or "сегодня" in title_text.lower(), \
        f"seller title mismatch: {title_text!r}"


def test_operator_landing_has_operator_title(operator_page: Page):
    """Operator-роль: title упоминает платформу."""
    page = operator_page
    title_text = page.locator("#welcomeTitle").inner_text(timeout=5000)
    assert "платформ" in title_text.lower() or "работе" in title_text.lower(), \
        f"operator title mismatch: {title_text!r}"


def test_role_toggle_visible_in_sidebar(buyer_page: Page):
    """Role-toggle (Покупатель/Продавец/Оператор) присутствует в sidebar."""
    page = buyer_page
    expect(page.locator("#roleToggle")).to_be_visible(timeout=5000)
    # 3 кнопки роли
    expect(page.locator(".role-tab")).to_have_count(3)


def test_topbar_bell_renders(buyer_page: Page):
    """Колокольчик в topbar рендерится."""
    page = buyer_page
    expect(page.locator("#topBell")).to_be_visible(timeout=5000)
