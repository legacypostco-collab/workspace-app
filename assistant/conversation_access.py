from django.db.models import Q

from .models import Conversation


def accessible_conversations(user, role: str):
    if not user or not getattr(user, "is_authenticated", False):
        return Conversation.objects.none()
    return Conversation.objects.filter(
        Q(user=user, role=role)
        | Q(participant_links__user=user, participant_links__role=role),
        is_active=True,
    ).distinct()


def get_accessible_conversation(user, role: str, conversation_id):
    return accessible_conversations(user, role).get(id=conversation_id)
