from __future__ import annotations

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from assistant.permissions import detect_user_role, display_role_label

SECTION_ACCESS = {
    "dashboard": {
        "admin",
        "operator",
        "operator_manager",
        "operator_logist",
        "operator_customs",
        "operator_payment",
    },
    "finance": {"admin", "operator_payment"},
    "orders": {
        "admin",
        "operator",
        "operator_manager",
        "operator_logist",
        "operator_customs",
        "operator_payment",
    },
    "users": {"admin", "operator_manager"},
    "moderation": {"admin", "operator"},
    "catalog": {"admin", "operator"},
    "support": {"admin", "operator", "operator_manager"},
    "audit": {"admin"},
    "settings": {"admin"},
    "search": {
        "admin",
        "operator",
        "operator_manager",
        "operator_logist",
        "operator_customs",
        "operator_payment",
    },
}


def control_role(user) -> str:
    return detect_user_role(user)


def can_access(user, section: str) -> bool:
    if not user or not user.is_authenticated:
        return False
    return control_role(user) in SECTION_ACCESS.get(section, set())


def control_required(section: str):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path(), login_url="/login/")
            if not can_access(request.user, section):
                raise PermissionDenied("У вас нет доступа к этому разделу")
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def role_label(user) -> str:
    return display_role_label(control_role(user))
