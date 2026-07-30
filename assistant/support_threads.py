from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.translation import gettext as _

from marketplace.models import UserProfile

from .models import Conversation, ConversationParticipant, Message
from .permissions import detect_user_role


def _available_operator():
    User = get_user_model()
    manager_ids = UserProfile.objects.filter(
        role="operator", operator_role="manager"
    ).values_list("user_id", flat=True)
    operator_ids = UserProfile.objects.filter(role="operator").values_list("user_id", flat=True)
    return (
        User.objects.filter(id__in=manager_ids, is_active=True).order_by("id").first()
        or User.objects.filter(id__in=operator_ids, is_active=True).order_by("id").first()
    )


def _notify_support_after_commit(*, user, title: str, body: str, conversation_id) -> None:
    def send():
        from .actions import _notify

        _notify(
            user,
            kind="system",
            title=title,
            body=body,
            url=f"/chat/?conv={conversation_id}",
        )

    transaction.on_commit(send)


@transaction.atomic
def create_support_conversation(*, requester, requester_role: str, context: str,
                                operator=None, kind: str = "request") -> Conversation:
    if kind not in {"request", "complaint", "kam"}:
        raise ValueError("Недопустимый тип обращения.")
    operator = operator or _available_operator()
    title = _("Поддержка · %(context)s") % {"context": context}
    conv = Conversation.objects.create(
        user=requester,
        role=requester_role,
        category="support",
        title=title[:200],
        support_status="open" if operator else "waiting_operator",
        support_kind=kind,
        assigned_operator=operator,
    )
    ConversationParticipant.objects.create(
        conversation=conv,
        user=requester,
        role=requester_role,
    )
    if operator:
        operator_role = detect_user_role(operator)
        ConversationParticipant.objects.create(
            conversation=conv,
            user=operator,
            role=operator_role,
        )
    Message.objects.create(
        conversation=conv,
        role=Message.Role.SYSTEM,
        content=(
            _("Обращение передано оператору. Все следующие сообщения в этом "
              "разговоре видят оба участника.")
            if operator
            else _("Обращение зарегистрировано и ожидает назначения оператора.")
        ),
    )
    if operator:
        _notify_support_after_commit(
            user=operator,
            title=_("Новое обращение в поддержку"),
            body=f"{requester.username}: {context}",
            conversation_id=conv.id,
        )
    return conv


def is_human_support(conversation: Conversation) -> bool:
    return bool(
        conversation
        and conversation.category == "support"
        and conversation.participant_links.exists()
    )


@transaction.atomic
def post_support_message(conversation: Conversation, sender, role: str, content: str):
    if conversation.support_status == "closed":
        raise PermissionError("Обращение закрыто. Создайте новое обращение в поддержку.")
    participant = ConversationParticipant.objects.filter(
        conversation=conversation,
        user=sender,
        role=role,
    ).exists()
    if not participant:
        raise PermissionError("Пользователь не является участником обращения.")

    message = Message.objects.create(
        conversation=conversation,
        sender=sender,
        role=Message.Role.USER,
        content=content,
    )
    conversation.support_status = (
        "waiting_user" if conversation.assigned_operator_id == sender.id
        else "waiting_operator"
    )
    conversation.save(update_fields=["support_status", "updated_at"])

    recipients = ConversationParticipant.objects.filter(
        conversation=conversation,
    ).exclude(user=sender).select_related("user")
    for link in recipients:
        _notify_support_after_commit(
            user=link.user,
            title=_("Новое сообщение в поддержке"),
            body=content[:200],
            conversation_id=conversation.id,
        )
    return message
