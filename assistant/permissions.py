"""Map a Django User → assistant role string used for context filtering."""

# Допустимые роли, которые можно явно выбрать в UI (toggle в topbar чата).
# Любой demo-аккаунт может переключаться между ними — это удобно для
# демонстрации сценариев, не плодя пользователей.
SWITCHABLE_ROLES = {"buyer", "seller", "operator"}


def _normalize_override(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip().lower()
    if v in SWITCHABLE_ROLES:
        return v
    if v.startswith("operator_"):
        return v
    return None


def detect_user_role(user, *, request=None, override: str | None = None) -> str:
    """Return the assistant role for a given user.

    Если передан явный `override` (через тело запроса или заголовок), либо
    в сессии есть `assistant_role_override` — используем его. В противном
    случае идём по обычной логике (профиль / эвристика по username).

    Priority:
      0. Explicit override (UI toggle) — buyer / seller / operator
      1. Superuser → admin
      2. UserProfile.role (buyer/seller)
      3. operator session subrole (logist/customs/payments/manager) → operator_X
      4. Default: buyer
    """
    explicit = _normalize_override(override)
    if not explicit and request is not None:
        explicit = (
            _normalize_override(request.headers.get("X-Assistant-Role"))
            or _normalize_override(getattr(request, "session", {}).get("assistant_role_override"))
        )
    if explicit:
        return explicit

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

    # Operator subrole detection — try common attributes first
    op_sub = getattr(user, "operator_role", None) or getattr(profile, "operator_role", None) if profile else None
    if op_sub:
        return f"operator_{op_sub}"

    # Try username heuristic for demo accounts
    name = (user.username or "").lower()
    if "operator" in name or "logist" in name:
        return "operator_logist"
    if "buyer" in name:
        return "buyer"
    if "seller" in name:
        return "seller"

    return "buyer"
