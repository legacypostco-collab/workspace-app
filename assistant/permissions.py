"""Map a Django User → assistant role string used for context filtering.

SECURITY (P0-1, прод-аудит 2026-05-21):
  Override роли через UI-toggle / X-Assistant-Role / session разрешён ТОЛЬКО
  если у пользователя реально есть права на эту роль. Раньше любой buyer мог
  POST /api/assistant/role/ {"role":"operator"} и получить доступ ко всем
  op_* actions (refund, KYB-approve и пр.) — эскалация привилегий до
  оператора. См. assistant/views.py:RoleSwitchView.
"""


SWITCHABLE_ROLES = {"buyer", "seller", "operator", "admin"}
ROLE_LABELS = {
    "buyer": "Покупатель",
    "seller": "Продавец",
    "operator": "Оператор",
    "admin": "Администратор",
}


def _normalize_override(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip().lower()
    if v in SWITCHABLE_ROLES:
        return v
    if v.startswith("operator_"):
        return v
    return None


def _user_real_role(user) -> str:
    """Реальная роль пользователя из БД / superuser-флага. Без override."""
    if not user or not user.is_authenticated:
        return "anon"
    if user.is_superuser:
        return "admin"
    profile = getattr(user, "userprofile", None) or getattr(user, "profile", None)
    if profile:
        role = (getattr(profile, "role", "") or "").lower()
        if role in ("seller", "buyer"):
            return role
        if role == "operator" or getattr(profile, "operator_role", None):
            operator_role = (getattr(profile, "operator_role", "") or "").strip()
            return f"operator_{operator_role}" if operator_role else "operator"
    # Имя пользователя не является источником полномочий. Даже в режиме
    # разработки роль берётся из профиля или явно выданной UserRole.
    return "buyer"


def _role_base(role: str | None) -> str:
    role = (role or "").lower()
    if role.startswith("operator"):
        return "operator"
    return role


def user_allowed_roles(user) -> list[str]:
    """Роли, реально выданные пользователю.

    `UserProfile.role` остаётся основной ролью для обратной совместимости.
    Дополнительные роли хранятся в `marketplace.UserRole`.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return ["buyer"]
    if getattr(user, "is_superuser", False):
        return ["admin", "buyer", "seller", "operator"]

    roles: list[str] = []
    primary = _user_real_role(user)
    if _role_base(primary) in SWITCHABLE_ROLES:
        roles.append(primary)

    try:
        extra = (
            user.roles.filter(is_enabled=True)
            .values_list("role", "operator_role")
            .order_by("role", "operator_role")
        )
        for role, operator_role in extra:
            if role == "operator" and operator_role:
                value = f"operator_{operator_role}"
            else:
                value = role
            if _role_base(value) in SWITCHABLE_ROLES and value not in roles:
                roles.append(value)
    except Exception:
        pass

    return roles or ["buyer"]


def user_allowed_role_tabs(user) -> list[dict[str, str]]:
    seen = set()
    tabs = []
    for role in user_allowed_roles(user):
        base = _role_base(role)
        if base not in SWITCHABLE_ROLES or base in seen:
            continue
        seen.add(base)
        tabs.append({"role": base, "label": ROLE_LABELS.get(base, base)})
    return tabs or [{"role": "buyer", "label": ROLE_LABELS["buyer"]}]


def _is_demo_account(user) -> bool:
    """Определить тестовую учётную запись только в режиме разработки."""
    from django.conf import settings as _s
    if not getattr(_s, "DEBUG", False):
        return False
    name = (user.username or "").lower() if user and user.is_authenticated else ""
    return name.startswith(("demo_", "test_"))


def _override_allowed(user, requested: str) -> bool:
    """Разрешён ли pereзапрошенный override для этого user.

    Правила:
    - buyer → seller / operator: ЗАПРЕЩЕНО (эскалация). Исключение —
      demo-аккаунт в DEBUG.
    - seller → operator: ЗАПРЕЩЕНО. Demo в DEBUG — OK.
    - любой → своя же роль или подроль: OK (no-op override).
    - admin / superuser: может всё.
    - operator → operator_logist / operator_customs / etc.: OK.
    """
    if not requested:
        return False
    allowed = user_allowed_roles(user)
    real = _user_real_role(user)
    if real == "admin":
        return True
    if requested in allowed:
        return True
    # Вкладка интерфейса называется просто «Оператор». Для пользователя с
    # предметной подролью она означает возврат именно в его подроль, а не
    # повышение до общего оператора с полным набором команд.
    if requested == "operator" and any(
        role.startswith("operator_") for role in allowed
    ):
        return True
    # SECURITY: даже demo-аккаунты НЕ должны переключать роль (buyer→seller→operator)
    # без явного логина. Раньше тут было `if _is_demo_account(user): return True`,
    # что позволяло demo_buyer одним кликом смотреть кабинет оператора.
    # Теперь любая смена роли = смена аккаунта = ввод пароля.
    return False


def _resolve_override(user, requested: str) -> str:
    """Map the generic operator tab back to an actually granted subrole."""
    if requested != "operator":
        return requested
    allowed = user_allowed_roles(user)
    if "operator" in allowed:
        return "operator"
    return next(
        (role for role in allowed if role.startswith("operator_")),
        requested,
    )


def detect_user_role(user, *, request=None, override: str | None = None) -> str:
    """Return the assistant role for a given user.

    Override (через тело запроса, X-Assistant-Role или session) применяется
    ТОЛЬКО если пользователь имеет на это право (см. `_override_allowed`).
    Иначе возвращаем реальную роль из БД.
    """
    explicit = _normalize_override(override)
    if not explicit and request is not None:
        explicit = (
            _normalize_override(request.headers.get("X-Assistant-Role"))
            or _normalize_override(getattr(request, "session", {}).get("assistant_role_override"))
        )
    if explicit and _override_allowed(user, explicit):
        return _resolve_override(user, explicit)

    if not user or not user.is_authenticated:
        return "buyer"
    if user.is_superuser:
        return "admin"

    profile = getattr(user, "userprofile", None) or getattr(user, "profile", None)
    if profile:
        role = getattr(profile, "role", "")
        if role == "seller":
            return "seller"
        if role == "buyer":
            return "buyer"
        if role == "operator":
            # Оператор: с суброллю → operator_<sub> (KAM=manager), без → общий operator.
            op_sub = (getattr(profile, "operator_role", "") or "").strip()
            return f"operator_{op_sub}" if op_sub else "operator"

    # Operator subrole detection — try common attributes first
    op_sub = getattr(user, "operator_role", None) or getattr(profile, "operator_role", None) if profile else None
    if op_sub:
        return f"operator_{op_sub}"

    # Try username heuristic for demo accounts (только в DEBUG).
    # Суб-роль определяется суффиксом: demo_operator_logist / _customs / _payment / _manager.
    # Без суффикса demo_operator → general operator (полный набор).
    if _is_demo_account(user):
        name = (user.username or "").lower()
        if "buyer" in name:
            return "buyer"
        if "seller" in name:
            return "seller"
        if "operator" in name or "logist" in name:
            # Точные суффиксы → суб-роль
            if name.endswith("_logist") or "logist" in name:
                return "operator_logist"
            if name.endswith("_customs") or "_customs_" in name:
                return "operator_customs"
            if name.endswith("_payment") or "_payment_" in name or name.endswith("_payments"):
                return "operator_payment"
            if name.endswith("_manager") or "_manager_" in name:
                return "operator_manager"
            # demo_operator без суффикса → general operator (полный доступ)
            return "operator"

    return "buyer"
