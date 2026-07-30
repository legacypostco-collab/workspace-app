from __future__ import annotations

import hashlib
import re
import secrets

from django.core.cache import cache
from django.db import transaction


LINK_TTL_SECONDS = 10 * 60
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def _token_key(token: str) -> str:
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    return f"telegram_link:{digest}"


def create_link_token(user) -> str:
    token = secrets.token_urlsafe(32)
    cache.set(_token_key(token), int(user.pk), LINK_TTL_SECONDS)
    return token


def consume_link_token(token: str, chat_id: int | str):
    token = (token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not _TOKEN_RE.fullmatch(token) or not chat_id.lstrip("-").isdigit():
        return None, "invalid"

    user_id = cache.get(_token_key(token))
    if not user_id:
        return None, "expired"

    lock_digest = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()
    lock_key = f"telegram_link_lock:{lock_digest}"
    if not cache.add(lock_key, "1", timeout=15):
        return None, "busy"

    try:
        from django.contrib.auth import get_user_model
        from marketplace.models import UserProfile

        with transaction.atomic():
            user = (
                get_user_model()
                .objects.select_for_update()
                .filter(pk=user_id, is_active=True)
                .first()
            )
            if not user:
                cache.delete(_token_key(token))
                return None, "expired"
            profile = UserProfile.objects.select_for_update().filter(
                user=user
            ).first()
            if not profile:
                cache.delete(_token_key(token))
                return None, "profile_missing"
            if UserProfile.objects.exclude(user=user).filter(
                notif_telegram_chat_id=chat_id
            ).exists():
                cache.delete(_token_key(token))
                return None, "already_linked"

            profile.notif_telegram_chat_id = chat_id
            profile.notif_telegram_enabled = True
            profile.save(
                update_fields=[
                    "notif_telegram_chat_id",
                    "notif_telegram_enabled",
                ]
            )
        cache.delete(_token_key(token))
        return user, "linked"
    finally:
        cache.delete(lock_key)
