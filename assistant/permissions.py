"""Map a Django User → assistant role string used for context filtering."""

def detect_user_role(user) -> str:
    """Return the assistant role for a given user.

    Priority:
      1. Superuser → admin
      2. UserProfile.role (buyer/seller)
      3. operator session subrole (logist/customs/payments/manager) → operator_X
      4. Default: buyer
    """
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
