"""The one visibility rule (design section 5).

    can_read(p, conversation) := p.role == "owner" OR p.id IN conversation.members

Every repository query that returns conversations, messages, or anything derived from
them takes its WHERE clause from here. No endpoint builds its own filter.
"""

from .models import Conversation, Participant


def can_read(viewer: Participant, conversation: Conversation) -> bool:
    return viewer.is_owner or viewer.id in conversation.members


def can_post(sender: Participant, conversation: Conversation) -> bool:
    # The owner reads agent-to-agent DMs through the role bypass but is not a member, so
    # those stay read-only for the owner.
    return sender.id in conversation.members


def conversation_filter(viewer: Participant, column: str = "c.id") -> tuple[str, list]:
    """SQL condition limiting `column` (a conversation id) to what the viewer may read."""
    if viewer.is_owner:
        return "1=1", []
    return f"{column} IN (SELECT conversation_id FROM members WHERE participant_id = ?)", [viewer.id]


def task_filter(viewer: Participant, alias: str = "t") -> tuple[str, list]:
    """A task is readable exactly when its home conversation is (design M4 section 6)."""
    return conversation_filter(viewer, f"{alias}.conversation_id")


def artifact_filter(viewer: Participant, alias: str = "a") -> tuple[str, list]:
    """An artifact is readable when its task's conversation is. Deleted artifacts are hidden from agents."""
    clause, params = conversation_filter(viewer, f"{alias}.conversation_id")
    if not viewer.is_owner:
        clause += f" AND {alias}.deleted_at IS NULL"
    return clause, params


def message_filter(viewer: Participant, alias: str = "m") -> tuple[str, list]:
    """SQL condition for message rows. Notices (hop limit, budget) are for the owner only."""
    clause, params = conversation_filter(viewer, f"{alias}.conversation_id")
    if not viewer.is_owner:
        clause += f" AND {alias}.kind != 'notice'"
    return clause, params
