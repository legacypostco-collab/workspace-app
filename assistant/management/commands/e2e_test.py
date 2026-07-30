"""End-to-end smoke test for the full B2B pipeline.

Runs the entire happy path (register → seller has parts → buyer searches →
RFQ → quote → accept → reserve → confirm → produce → ready-to-ship →
pay-balance → ship → transit → customs → delivered → completed) using
real action handlers, not HTTP.

Usage:
    python manage.py e2e_test
    python manage.py e2e_test --reset   # delete existing test artefacts first

Exits with non-zero if any step fails. Prints actionable diagnostics so we
can fix bugs in `assistant/*.py` and re-run.
"""
from __future__ import annotations

import traceback
from decimal import ROUND_CEILING, Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

User = get_user_model()


class StepFail(Exception):
    """Raised by `_call` when an action handler returns an error result."""


class Command(BaseCommand):
    help = "Full E2E pipeline smoke test."

    def execute(self, *args, **options):
        from django.conf import settings
        from django.test.utils import override_settings

        self._had_errors = False
        # Сквозной прогон не проверяет почтовый транспорт: сообщения остаются
        # в памяти процесса и не отправляются во внешние системы.
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
            ALLOWED_HOSTS=list(settings.ALLOWED_HOSTS) + ["testserver"],
            QR_SECRET=(
                getattr(settings, "QR_SECRET", "")
                or "local-e2e-qr-secret-not-for-production"
            ),
        ):
            output = super().execute(*args, **options)
        if self._had_errors:
            raise CommandError(
                "Сквозной сценарий завершился с ошибкой. "
                "Подробности находятся выше в выводе команды."
            )
        return output

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true",
                            help="Delete the previously created E2E order before re-running.")
        parser.add_argument("--verbose-cards", action="store_true",
                            help="Print full card payloads at each step (debug).")

    # ── output helpers ──────────────────────────────────────────────
    def _h(self, title):
        self.stdout.write(self.style.NOTICE(f"\n── {title} " + "─" * (70 - len(title))))

    def _ok(self, msg):
        self.stdout.write(self.style.SUCCESS(f"  ✓ {msg}"))

    def _warn(self, msg):
        self.stdout.write(self.style.WARNING(f"  ⚠ {msg}"))

    def _err(self, msg):
        self._had_errors = True
        self.stdout.write(self.style.ERROR(f"  ✗ {msg}"))

    def _info(self, msg):
        self.stdout.write(f"    {msg}")

    # ── action invocation wrapper ───────────────────────────────────
    def _call(self, handler, params, user, role, *, label, expect_ok=True):
        """Invoke an action handler. Raises StepFail on hard error.

        Returns the ActionResult-like object (or dict) returned by the handler.
        Many handlers in this codebase return either ActionResult or dict;
        we normalise to a dict view for easier assertions.
        """
        try:
            result = handler(params or {}, user, role)
        except Exception as e:
            self._err(f"{label}: exception")
            self._info(f"{type(e).__name__}: {e}")
            self._info(traceback.format_exc())
            raise StepFail(label) from e

        # Normalise: ActionResult dataclass has .text/.cards/.actions
        if hasattr(result, "text"):
            view = {"text": result.text,
                    "cards": list(result.cards or []),
                    "actions": list(result.actions or []),
                    "contextual_actions": list(getattr(result, "contextual_actions", []) or []),
                    "suggestions": list(getattr(result, "suggestions", []) or [])}
        elif isinstance(result, dict):
            view = result
        else:
            view = {"text": str(result), "cards": [], "actions": []}

        if self._verbose_cards:
            for c in view["cards"]:
                self._info(f"  card: {(c or {}).get('type', '?')} keys={list((c or {}).get('data') or {})[:6]}")

        # Heuristic: if text starts with ❌ or contains a known error marker — flag.
        txt = (view.get("text") or "").strip()
        looks_bad = txt.startswith("❌") or txt.startswith("Ошибка") or "Permission denied" in txt
        if expect_ok and looks_bad:
            self._err(f"{label}: handler returned error text")
            self._info((txt or "")[:300])
            raise StepFail(label)

        self._ok(f"{label}: {(txt or '<no text>').splitlines()[0][:80] if txt else '(no text)'}")
        return view

    def _upload_order_evidence(self, order, user, status, trigger_id):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import Client

        client = Client()
        client.force_login(user)
        response = client.post(
            f"/api/assistant/orders/{order.id}/documents/",
            {
                "status": status,
                "trigger_id": trigger_id,
                "file": SimpleUploadedFile(
                    f"{trigger_id}.pdf",
                    b"%PDF-1.4 e2e evidence",
                    content_type="application/pdf",
                ),
            },
        )
        if response.status_code != 201:
            self._err(
                f"upload evidence ({status}.{trigger_id}): "
                f"HTTP {response.status_code} {response.content[:300]!r}"
            )
            raise StepFail(f"upload evidence ({status}.{trigger_id})")
        self._ok(f"upload evidence ({status}.{trigger_id})")

    def _scan_order_qr(self, order, user, *, action="inspected"):
        from django.test import Client

        from assistant.qr_scan import encode_qr_code

        client = Client()
        client.force_login(user)
        response = client.post(
            f"/api/assistant/qr/scan/{encode_qr_code(order.id)}/",
            {"action": action},
        )
        if response.status_code != 200:
            self._err(
                f"QR scan ({order.status}.{action}): "
                f"HTTP {response.status_code} {response.content[:300]!r}"
            )
            raise StepFail(f"QR scan ({order.status}.{action})")
        self._ok(f"QR scan ({order.status}.{action})")

    # ── high-level steps ────────────────────────────────────────────
    def handle(self, *args, **opts):
        self._verbose_cards = opts["verbose_cards"]
        reset = opts["reset"]

        # Lazy imports — avoid Django bootstrap problems before settings ready.
        from assistant import actions, kp_workflow, negotiation, operator_actions
        from marketplace.models import (
            Order,
            Part,
            Quote,
            RFQ,
            TwoFactorAuth,
            UserProfile,
        )
        try:
            from assistant import payments  # noqa: F401 — verify import
        except Exception as e:
            self._err(f"Failed to import assistant.payments: {e}")
            return

        # ── STEP -1 — KYB Onboarding flow (3 companies, ТЗ paths) ──
        # Runs BEFORE the main pipeline so we verify supplier auto-checks +
        # operator review path before exercising the order pipeline below.
        self._h("STEP -1 — KYB Onboarding (3 companies — green/yellow/red)")
        # Re-seed test companies (idempotent, fresh state)
        from django.core.management import call_command

        from assistant import onboarding
        from marketplace.models import CompanyVerification
        call_command("seed_kyb_onboarding", "--reset", verbosity=0)
        # Operator user
        try:
            operator_user = User.objects.get(username="demo_operator")
        except User.DoesNotExist:
            operator_user = User.objects.filter(is_staff=True).first()

        for username, expected_status, expected_risk in [
            ("supplier_cleancorp", "pending",  "green"),
            ("supplier_hydrolux",  "pending",  "yellow"),
            ("supplier_oldfabric", "rejected", "red"),
        ]:
            kyb = CompanyVerification.objects.get(user__username=username)
            if kyb.status != expected_status or kyb.risk_indicator != expected_risk:
                self._err(f"KYB {username}: expected status={expected_status} risk={expected_risk}, "
                          f"got status={kyb.status} risk={kyb.risk_indicator}")
                return
            self._ok(f"KYB {username:25s} → status={kyb.status:10s} risk={kyb.risk_indicator}")

        # Open operator review screen for cleancorp + hydrolux
        for username in ("supplier_cleancorp", "supplier_hydrolux"):
            kyb = CompanyVerification.objects.get(user__username=username)
            try:
                self._call(onboarding.op_kyb_review,
                           {"user_id": kyb.user_id}, operator_user, "operator",
                           label=f"op_kyb_review ({username})")
            except StepFail:
                return

        # Mark some checklist items as done for cleancorp (simulate operator's 2-3 min глазами)
        kyb = CompanyVerification.objects.get(user__username="supplier_cleancorp")
        for item in ("streetview_ok", "site_ok", "bank_ok"):
            try:
                self._call(onboarding.op_kyb_check,
                           {"user_id": kyb.user_id, "item": item}, operator_user, "operator",
                           label=f"op_kyb_check ({item})")
            except StepFail:
                return

        # Operator approves cleancorp → status=verified (sandbox)
        try:
            self._call(onboarding.op_kyb_approve,
                       {"user_id": kyb.user_id, "confirmed": True},
                       operator_user, "operator",
                       label="op_kyb_approve (cleancorp → verified)")
        except StepFail:
            return
        kyb.refresh_from_db()
        if kyb.status != "verified":
            self._err(f"cleancorp should be verified, got {kyb.status}")
            return
        self._ok(f"KYB cleancorp → {kyb.status} (Песочница). Готов к первому заказу.")

        # Operator requests clarifications from hydrolux (yellow path)
        kyb_h = CompanyVerification.objects.get(user__username="supplier_hydrolux")
        try:
            self._call(onboarding.op_kyb_clarify,
                       {"user_id": kyb_h.user_id, "confirmed": True,
                        "note": "Просим прислать копию TRN UAE и фото склада с вывеской."},
                       operator_user, "operator",
                       label="op_kyb_clarify (hydrolux)")
        except StepFail:
            return
        kyb_h.refresh_from_db()
        self._ok(f"KYB hydrolux → запрос уточнений отправлен (note сохранён, status={kyb_h.status})")

        # Verify oldfabric still rejected with reason
        kyb_o = CompanyVerification.objects.get(user__username="supplier_oldfabric")
        if kyb_o.status != "rejected" or "АВТООТКАЗ" not in (kyb_o.rejection_reason or ""):
            self._err(f"oldfabric: expected rejected+АВТООТКАЗ, got status={kyb_o.status}")
            return
        self._ok(f"KYB oldfabric → {kyb_o.status} (АВТООТКАЗ): "
                 f"{(kyb_o.rejection_reason or '')[:80]}")

        # ── prerequisites: demo users ──────────────────────────────
        self._h("STEP 0 — Prerequisites (demo users + seed catalog)")
        try:
            buyer  = User.objects.get(username="demo_buyer")
            seller = User.objects.get(username="demo_seller")
        except User.DoesNotExist as e:
            self._err(f"Demo user missing: {e}. Run `manage.py seed_chat_demo` first.")
            return
        # Ensure profiles
        buyer_profile, _  = UserProfile.objects.get_or_create(user=buyer,  defaults={"role": "buyer"})
        seller_profile, _ = UserProfile.objects.get_or_create(user=seller, defaults={"role": "seller"})
        if buyer_profile.role != "buyer":
            buyer_profile.role = "buyer"; buyer_profile.save(update_fields=["role"])
        if seller_profile.role != "seller":
            seller_profile.role = "seller"; seller_profile.save(update_fields=["role"])
        import pyotp

        buyer_totp_secret = pyotp.random_base32()
        TwoFactorAuth.objects.update_or_create(
            user=buyer,
            defaults={
                "secret": buyer_totp_secret,
                "enabled": True,
                "enabled_at": timezone.now(),
                "last_totp_counter": None,
                "backup_codes": "",
            },
        )
        self._ok(f"buyer={buyer.username} (id={buyer.id})  seller={seller.username} (id={seller.id})")

        # Ensure seller has at least one part
        seller_parts = Part.objects.filter(seller=seller, availability_status="active")[:5]
        if not seller_parts:
            self._warn("Seller has no active parts — seeding 1 catalog item")
            from marketplace.models import Brand, Category
            brand, _ = Brand.objects.get_or_create(name="Caterpillar")
            category, _ = Category.objects.get_or_create(name="Hydraulics")
            part = Part.objects.create(
                seller=seller, brand=brand, category=category,
                oem_number="E2E-001", slug="e2e-001",
                title="E2E test part", description="Seal kit (E2E)",
                price=Decimal("250.00"), stock_quantity=10,
                availability_status="active",
            )
            seller_parts = [part]
        test_part = seller_parts[0]
        unit_price = Decimal(test_part.price or 0)
        if unit_price <= 0:
            self._err("У тестовой позиции должна быть положительная цена.")
            return
        quantity = max(
            2,
            int(
                (Decimal("7000.00") / unit_price).to_integral_value(
                    rounding=ROUND_CEILING,
                )
            ),
        )
        from assistant.models import Wallet, WalletTx

        wallet = Wallet.for_user(buyer)
        target_balance = max(
            Decimal("50000.00"),
            unit_price * quantity * Decimal("2"),
        )
        if wallet.balance < target_balance:
            topup_amount = target_balance - wallet.balance
            wallet.balance = target_balance
            wallet.save(update_fields=["balance", "updated_at"])
            WalletTx.objects.create(
                wallet=wallet,
                kind="topup",
                amount=topup_amount,
                description="[E2E] Локальный тестовый баланс",
                balance_after=wallet.balance,
            )
            self._ok(
                f"Local test wallet restored to ${wallet.balance:,.2f}"
            )
        self._ok(
            f"Test part: {test_part.oem_number} "
            f"(id={test_part.id}, price=${unit_price}, qty={quantity})"
        )

        # Optional reset — delete any leftover E2E orders/rfqs to keep clean state
        if reset:
            ids = list(Order.objects.filter(customer_name__startswith="[E2E]").values_list("id", flat=True))
            if ids:
                Order.objects.filter(id__in=ids).delete()
                self._warn(f"Reset: removed {len(ids)} prior E2E order(s)")

        # ── STEP 1 — buyer searches catalog ────────────────────────
        self._h("STEP 1 — Buyer searches catalog")
        try:
            search_view = self._call(
                actions.search_parts,
                {"articles": [test_part.oem_number]},
                buyer, "buyer", label="search_parts",
            )
        except StepFail:
            return

        # ── STEP 2 — buyer creates RFQ ─────────────────────────────
        self._h("STEP 2 — Buyer creates RFQ")
        try:
            rfq_view = self._call(
                actions.create_rfq,
                {"product_ids": [test_part.id], "quantity": quantity},
                buyer, "buyer", label="create_rfq",
            )
        except StepFail:
            return
        rfq = RFQ.objects.filter(created_by=buyer).order_by("-id").first()
        if not rfq:
            self._err("RFQ not created in DB")
            return
        self._ok(f"RFQ #{rfq.id} status={rfq.status} mode={getattr(rfq, 'mode', '?')}")

        # ── STEP 3 — seller submits quote (2 phases) ───────────────
        self._h("STEP 3 — Seller submits quote (preview → confirm)")
        rfq_item = rfq.items.first()
        if not rfq_item:
            self._err("RFQ has no items")
            return
        # Preview phase — handler returns a form; we'll then "submit" it with
        # the price keyed by `price_<rfq_item_id>`.
        try:
            self._call(negotiation.submit_quote,
                       {"rfq_id": rfq.id, "delivery_days": 14, "valid_days": 7},
                       seller, "seller", label="submit_quote (preview)")
        except StepFail:
            return
        # Commit phase
        commit_params = {
            "rfq_id": rfq.id,
            "delivery_days": 14,
            "valid_days": 7,
            "confirmed": True,
            f"price_{rfq_item.id}": str(test_part.price),
        }
        try:
            self._call(negotiation.submit_quote, commit_params, seller, "seller",
                       label="submit_quote (confirmed)")
        except StepFail:
            return
        quote = Quote.objects.filter(rfq=rfq, seller=seller).order_by("-id").first()
        if not quote:
            self._err("Quote not created")
            return
        self._ok(f"Quote #{quote.id} status={quote.status} total=${quote.total_amount}")

        # ── STEP 4 — buyer confirms KP + pays reserve atomically ──
        self._h("STEP 4 — Buyer accepts KP + pays 10% reserve")
        previous_order_ids = list(
            Order.objects.filter(buyer=buyer).values_list("id", flat=True)
        )
        try:
            kp_view = self._call(
                kp_workflow.confirm_kp_and_reserve,
                {"rfq_id": rfq.id, "quote_id": quote.id, "logistics_cost": "20"},
                buyer, "buyer", label="confirm_kp_and_reserve",
            )
        except StepFail:
            return
        order = (
            Order.objects.filter(buyer=buyer)
            .exclude(id__in=previous_order_ids)
            .order_by("-id")
            .first()
        )
        if not order:
            self._err(
                "Текущий RFQ не создал новый заказ после подтверждения КП; "
                f"ответ: {(kp_view.get('text') or '')[:200]}"
            )
            return
        self._ok(f"Order #{order.id} status={order.status} payment={order.payment_status} "
                 f"total=${order.total_amount} reserve=${order.reserve_amount}")
        # Mark order as E2E for cleanup
        if not (order.customer_name or "").startswith("[E2E]"):
            order.customer_name = f"[E2E] {order.customer_name or 'demo'}"
            order.save(update_fields=["customer_name"])

        # ── STEP 5 — seller advances order: reserve_paid → confirmed → in_production → ready_to_ship ──
        self._h("STEP 5 — Seller advances order through production stages")
        for expected_to in ("confirmed", "in_production", "ready_to_ship"):
            cur = Order.objects.get(id=order.id).status
            try:
                self._call(actions.advance_order, {"order_id": order.id}, seller, "seller",
                           label=f"advance_order ({cur} → {expected_to})")
            except StepFail:
                return
            order.refresh_from_db()
            if order.status != expected_to:
                self._err(f"Expected status={expected_to}, got {order.status}")
                return
            self._ok(f"order.status = {order.status}")

        # ── STEP 6 — buyer pays balance 90% ───────────────────────
        self._h("STEP 6 — Buyer pays balance 90%")
        try:
            self._call(actions.pay_final, {"order_id": order.id}, buyer, "buyer",
                       label="pay_final (preview)")
            otp_gate = self._call(
                actions.pay_final,
                {"order_id": order.id, "confirmed": True},
                buyer,
                "buyer",
                label="pay_final (2FA gate)",
            )
            if "одноразов" not in (otp_gate.get("text") or "").lower():
                self._err("Крупный платеж прошел без запроса одноразового кода.")
                return
            self._call(
                actions.pay_final,
                {
                    "order_id": order.id,
                    "confirmed": True,
                    "otp": pyotp.TOTP(buyer_totp_secret).now(),
                },
                buyer,
                "buyer",
                label="pay_final (2FA confirmed)",
            )
        except StepFail:
            return
        order.refresh_from_db()
        if order.payment_status != "paid":
            self._err(f"Expected payment_status=paid, got {order.payment_status}")
            return
        self._ok(f"order.payment_status = {order.payment_status}")

        # ── STEP 7 — seller ships (2 phases: form → submit tracking) ──
        self._h("STEP 7 — Seller ships order")
        try:
            self._upload_order_evidence(
                order,
                seller,
                "ready_to_ship",
                "invoice",
            )
            self._upload_order_evidence(
                order,
                seller,
                "ready_to_ship",
                "packing_list",
            )
            order.refresh_from_db()
            self._scan_order_qr(order, seller)
            self._call(actions.ship_order, {"order_id": order.id}, seller, "seller",
                       label="ship_order (form)")
            self._call(actions.ship_order,
                       {
                           "order_id": order.id,
                           "tracking_number": "E2E-TRK-12345",
                           "carrier": "E2E Test Carrier",
                           "carrier_phone": "+971500000000",
                           "carrier_email": "dispatch@example.test",
                       },
                       seller, "seller", label="ship_order (submit)")
        except StepFail:
            return
        order.refresh_from_db()
        if order.status != "transit_abroad":
            self._err(
                "После оформления отгрузки ожидался статус transit_abroad, "
                f"получен {order.status}."
            )
            return
        self._ok(f"order.status = {order.status}  payment={order.payment_status}")

        # ── STEP 7.5 — operator control panel BEFORE customs ──────
        self._h("STEP 7.5 — Operator dashboards & control panel (read-only checks)")
        try:
            operator = User.objects.get(username="demo_operator")
        except User.DoesNotExist:
            self._warn("demo_operator missing — using seller for operator-style advances (may fail RBAC)")
            operator = seller

        # All these handlers should return OK results for the operator role.
        # Each shows operator a view of platform state — order detail, queues,
        # SLA breaches, payments, customs, logistics statistics.
        op_views = [
            (operator_actions.op_dashboard,          {},                              "op_dashboard"),
            (operator_actions.op_queue,              {},                              "op_queue (all)"),
            (operator_actions.op_queue,              {"filter": "open"},              "op_queue (open)"),
            (operator_actions.op_sla_breach,         {},                              "op_sla_breach"),
            (operator_actions.op_order_detail,       {"order_id": order.id},          "op_order_detail"),
            (operator_actions.op_payments_dashboard, {},                              "op_payments_dashboard"),
            (operator_actions.op_payments_stats,     {},                              "op_payments_stats"),
            (operator_actions.op_logistics_stats,    {},                              "op_logistics_stats"),
            (operator_actions.op_customs_dashboard,  {},                              "op_customs_dashboard"),
        ]
        for handler, p, label in op_views:
            try:
                self._call(handler, p, operator, "operator", label=label)
            except StepFail:
                return

        # Operator note on the order (audit trail)
        try:
            self._call(operator_actions.op_add_note,
                       {"order_id": order.id, "note": "E2E: operator review — all good", "confirmed": True},
                       operator, "operator", label="op_add_note")
        except StepFail:
            return

        # ── STEP 8 — operator drives transit/customs/issuing ──────
        self._h("STEP 8 — Operator drives transit → customs → transit_rf → issuing → delivered")

        # transit_abroad set by ship_order. Now drive through customs / transit_rf / issuing / delivered.
        # Some stages have blocking triggers (qr/upload) that MUST be completed
        # before advance_order will move forward. We close them via complete_trigger.
        STAGE_TRIGGERS = {
            # Stage where these triggers live → list of trigger_ids to close
            # before advancing past it. (button-type triggers auto-complete.)
            "customs":    ["declaration"],
            "transit_rf": ["qr_rf", "ttn_rf"],
            "issuing":    ["qr_issuing"],
        }
        for expected_to in ("customs", "transit_rf", "issuing", "delivered"):
            cur = Order.objects.get(id=order.id).status

            # ── Operator customs workflow при заходе в стейдж "customs" ──
            # На этом этапе оператор обычно: ищет HS-код, присваивает его,
            # проверяет сертификаты, считает пошлину, скринит санкции и
            # выпускает груз. Делаем это PARALLELLY с закрытием триггеров,
            # чтобы проверить весь оператор-flow.
            if cur == "customs":
                self._h("STEP 8a — Operator customs workflow (HS code → certs → duty → sanctions)")
                try:
                    self._call(operator_actions.op_hs_lookup,
                               {"query": "pump"},
                               operator, "operator", label="op_hs_lookup (search pump)")
                    self._call(operator_actions.op_hs_assign,
                               {"order_id": order.id, "hs_code": "8413.50", "country": "RU", "confirmed": True},
                               operator, "operator", label="op_hs_assign (8413.50)")
                    self._call(operator_actions.op_certs_check,
                               {"order_id": order.id},
                               operator, "operator", label="op_certs_check")
                    self._call(operator_actions.op_calc_duty,
                               {"order_id": order.id, "confirmed": True},
                               operator, "operator", label="op_calc_duty")
                    self._call(operator_actions.op_sanctions_check,
                               {"order_id": order.id},
                               operator, "operator", label="op_sanctions_check")
                except StepFail:
                    return

            # Close blocking triggers at the *current* stage before advancing.
            for trig_id in STAGE_TRIGGERS.get(cur, []):
                try:
                    if trig_id.startswith("qr_"):
                        order.refresh_from_db()
                        self._scan_order_qr(order, operator)
                    else:
                        self._upload_order_evidence(
                            order,
                            operator,
                            cur,
                            trig_id,
                        )
                except StepFail:
                    return
            try:
                self._call(actions.advance_order, {"order_id": order.id}, operator, "operator",
                           label=f"advance_order ({cur} → {expected_to})")
            except StepFail:
                return
            order.refresh_from_db()
            if order.status != expected_to:
                self._err(f"Expected status={expected_to}, got {order.status}")
                return
            self._ok(f"order.status = {order.status}")

        # ── STEP 9 — buyer confirms delivery → completed ──────────
        self._h("STEP 9 — Buyer confirms delivery")
        try:
            order.refresh_from_db()
            self._scan_order_qr(order, buyer, action="received")
            self._upload_order_evidence(
                order,
                buyer,
                "delivered",
                "signed_docs",
            )
            self._call(actions.confirm_delivery, {"order_id": order.id}, buyer, "buyer",
                       label="confirm_delivery (preview)")
            self._call(
                actions.confirm_delivery,
                {"order_id": order.id, "confirmed": True},
                buyer,
                "buyer",
                label="confirm_delivery (confirmed)",
            )
        except StepFail:
            return
        order.refresh_from_db()
        if order.status != "completed":
            self._err(f"Expected status=completed, got {order.status}")
            return
        self._ok(f"FINAL: order #{order.id} status=completed  payment={order.payment_status}")

        # ── Done ────────────────────────────────────────────────────
        self._h("E2E SMOKE TEST PASSED")
        self.stdout.write(self.style.SUCCESS("All 9 pipeline steps completed without errors. ✅\n"))
