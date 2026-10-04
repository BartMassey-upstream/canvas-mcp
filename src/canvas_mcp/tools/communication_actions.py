"""Bounded, confirmed discussion and inbox actions without creator access."""

import json
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_id
from ..core.client import make_canvas_request
from ..core.config import get_config
from ..core.course_policy import (
    assert_no_identity_override,
    check_student_write_allowed,
)
from ..core.path import canvas_path
from ..core.untrusted_content import (
    FENCE_LEAK_ERROR,
    contains_fence_markers,
    fence_untrusted,
    fence_untrusted_inline,
)
from ..core.validation import validate_params
from ..core.write_confirmation import (
    ConfirmationGuard,
    preview_with_token,
    redeem_confirmation,
)

_STUDENT_ACTIONS = frozenset(
    {
        "update_discussion_entry",
        "delete_discussion_entry",
        "set_discussion_subscription",
        "set_discussion_read_state",
    }
)
_GUARDS = {
    name: ConfirmationGuard(nothing_done="Nothing was changed or sent.")
    for name in (
        *_STUDENT_ACTIONS,
        "delete_discussion_topic",
        "reply_to_conversation",
        "update_conversation_settings",
        "delete_conversation",
    )
}


def _valid_id(value: Any) -> bool:
    return type(value) is int and value > 0


def _fingerprint(name: str, *values: Any) -> str:
    return _GUARDS[name].fingerprint(
        name, *(json.dumps(value, sort_keys=True, default=str) for value in values)
    )


def _preview(name: str, fingerprint: str, text: str, action: str = "update") -> str:
    return preview_with_token(_GUARDS[name], fingerprint, name, text, action=action)


async def _moderator(course_id: str | int) -> bool | None:
    response = await make_canvas_request(
        "get",
        canvas_path("courses", course_id, "permissions"),
        params={"permissions[]": ["moderate_forum"]},
    )
    if (
        not isinstance(response, dict)
        or "error" in response
        or type(response.get("moderate_forum")) is not bool
    ):
        return None
    return response["moderate_forum"] is True


async def _course_access(
    course_id: str | int, name: str, student_only: bool
) -> tuple[bool, str | None]:
    if not student_only:
        moderator = await _moderator(course_id)
        if moderator is None:
            return (
                False,
                "Error: Canvas did not establish discussion moderation permission. Nothing was changed.",
            )
        if moderator:
            return False, None
    if name not in get_config().student_write_tools:
        return (
            True,
            f"Error: '{name}' is not enabled in STUDENT_WRITE_TOOLS. Nothing was changed.",
        )
    allowed, reason = await check_student_write_allowed(course_id, name)
    return True, None if allowed else "Error: " + reason


async def _topic(course_id: str | int, topic_id: int) -> dict[str, Any] | None:
    response = await make_canvas_request(
        "get", canvas_path("courses", course_id, "discussion_topics", topic_id)
    )
    if (
        not isinstance(response, dict)
        or "error" in response
        or response.get("id") != topic_id
    ):
        return None
    return response


async def _entry(
    course_id: str | int, topic_id: int, entry_id: int
) -> dict[str, Any] | None:
    response = await make_canvas_request(
        "get",
        canvas_path("courses", course_id, "discussion_topics", topic_id, "entry_list"),
        params={"ids[]": [entry_id]},
    )
    if (
        not isinstance(response, list)
        or len(response) != 1
        or not isinstance(response[0], dict)
    ):
        return None
    entry = response[0]
    if (
        entry.get("id") != entry_id
        or entry.get("deleted")
        or not isinstance(entry.get("message"), str)
    ):
        return None
    return entry


async def _own_entry(entry: dict[str, Any]) -> bool:
    profile = await make_canvas_request("get", canvas_path("users", "self", "profile"))
    return (
        isinstance(profile, dict)
        and "error" not in profile
        and _valid_id(profile.get("id"))
        and _valid_id(entry.get("user_id"))
        and profile["id"] == entry["user_id"]
    )


def _entry_snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        key: entry.get(key)
        for key in (
            "id",
            "user_id",
            "message",
            "updated_at",
            "created_at",
            "parent_id",
            "deleted",
            "attachment",
            "attachment_id",
        )
    }


def _topic_snapshot(topic: dict[str, Any]) -> dict[str, Any]:
    return {
        key: topic.get(key)
        for key in (
            "id",
            "title",
            "message",
            "updated_at",
            "discussion_subentry_count",
            "assignment_id",
            "is_announcement",
            "published",
            "locked",
        )
    }


async def _write(method: str, path: str, data: dict[str, Any] | None = None) -> Any:
    if data is not None:
        assert_no_identity_override(data)
    try:
        return await make_canvas_request(method, path, data=data)
    except Exception:
        return {
            "error": "The write outcome is unknown; read the target before retrying."
        }


def _write_error(response: Any) -> str | None:
    if not isinstance(response, dict) or "error" in response:
        return "Warning: Canvas did not confirm the write. Read the target before retrying; obtain a new preview before another write."
    return None


def register_shared_communication_tools(mcp: FastMCP) -> None:
    """Register caller-only discussion state reads, without participant data."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_discussion_user_state(
        course_identifier: str | int, topic_id: int
    ) -> dict[str, Any]:
        """Read your subscription/unread state; does not mark the topic read."""
        if not _valid_id(topic_id):
            return {"error": "topic_id must be positive."}
        course_id = await get_course_id(course_identifier)
        topic = await _topic(course_id, topic_id)
        if (
            topic is None
            or type(topic.get("subscribed")) is not bool
            or topic.get("read_state") not in ("read", "unread")
        ):
            return {"error": "Canvas did not return valid discussion state."}
        result = {
            "course_id": str(course_id),
            "topic_id": topic_id,
            "subscribed": topic["subscribed"],
            "read_state": topic["read_state"],
        }
        if type(topic.get("unread_count")) is int and topic["unread_count"] >= 0:
            result["unread_count"] = topic["unread_count"]
        if topic.get("subscription_hold") in (
            "initial_post_required",
            "not_in_group_set",
            "not_in_group",
            "topic_is_announcement",
        ):
            result["subscription_hold"] = topic["subscription_hold"]
        return result


def _register_discussion_actions(
    mcp: FastMCP, student_only: bool, enabled: frozenset[str] | None = None
) -> None:
    @validate_params
    async def update_discussion_entry(
        course_identifier: str | int,
        topic_id: int,
        entry_id: int,
        message: str,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview and confirm replacing an entry or reply's complete HTML body.

        Non-moderators require the enabled student gate, course policy, and
        ownership. Entries with attachments must be edited in Canvas because
        its message-only update can remove the attachment.
        """
        if contains_fence_markers(message):
            return FENCE_LEAK_ERROR
        if not message.strip() or not _valid_id(topic_id) or not _valid_id(entry_id):
            return (
                "Error: positive topic/entry IDs and a nonempty message are required."
            )
        name = "update_discussion_entry"
        course_id = await get_course_id(course_identifier)
        student, error = await _course_access(course_id, name, student_only)
        if error:
            return error
        entry = await _entry(course_id, topic_id, entry_id)
        if entry is None:
            return "Error: Canvas did not return the requested active entry. Nothing was changed."
        if student and not await _own_entry(entry):
            return "Error: student actions may edit only your own discussion entry."
        if entry.get("attachment") or entry.get("attachment_id"):
            return "Error: edit this entry in Canvas to preserve its attachment. Nothing was changed."
        fingerprint = _fingerprint(
            name,
            course_id,
            topic_id,
            entry_id,
            student,
            _entry_snapshot(entry),
            message,
        )
        if not confirmation_token:
            return _preview(
                name,
                fingerprint,
                f"Would replace entry {entry_id} in topic {topic_id}.\nCurrent body:\n"
                + fence_untrusted(entry["message"], "discussion entry body")
                + "\nNew body:\n"
                + fence_untrusted(message, "proposed discussion entry body"),
            )
        error = redeem_confirmation(_GUARDS[name], confirmation_token, fingerprint)
        if error:
            return error
        final_student, error = await _course_access(course_id, name, student_only)
        if error or final_student != student:
            return (
                error or "Error: discussion permission changed. Obtain a new preview."
            )
        response = await _write(
            "put",
            canvas_path(
                "courses", course_id, "discussion_topics", topic_id, "entries", entry_id
            ),
            {"message": message},
        )
        if error := _write_error(response):
            return error
        if response.get("id") != entry_id or response.get("message") != message:
            return "Warning: Canvas accepted the entry update but did not echo the requested body. Read the entry before retrying."
        return f"Discussion entry {entry_id} updated."

    @validate_params
    async def delete_discussion_entry(
        course_identifier: str | int,
        topic_id: int,
        entry_id: int,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview and confirm deleting an entry/reply and its author/content.

        This removes participation that may count toward a grade. Replies use
        the same entry-ID route. Non-moderators can delete only their own entry
        with the enabled student gate and course policy.
        """
        if not _valid_id(topic_id) or not _valid_id(entry_id):
            return "Error: topic_id and entry_id must be positive."
        name = "delete_discussion_entry"
        course_id = await get_course_id(course_identifier)
        student, error = await _course_access(course_id, name, student_only)
        if error:
            return error
        entry = await _entry(course_id, topic_id, entry_id)
        if entry is None:
            return "Error: Canvas did not return the requested active entry. Nothing was deleted."
        if student and not await _own_entry(entry):
            return "Error: student actions may delete only your own discussion entry."
        fingerprint = _fingerprint(
            name, course_id, topic_id, entry_id, student, _entry_snapshot(entry)
        )
        if not confirmation_token:
            return _preview(
                name,
                fingerprint,
                f"Would delete entry {entry_id} in topic {topic_id}, including its body/author. This may affect participation credit.\n"
                + fence_untrusted(entry["message"], "discussion entry body"),
                action="delete",
            )
        error = redeem_confirmation(_GUARDS[name], confirmation_token, fingerprint)
        if error:
            return error
        final_student, error = await _course_access(course_id, name, student_only)
        if error or final_student != student:
            return (
                error or "Error: discussion permission changed. Obtain a new preview."
            )
        response = await _write(
            "delete",
            canvas_path(
                "courses", course_id, "discussion_topics", topic_id, "entries", entry_id
            ),
        )
        return _write_error(response) or f"Discussion entry {entry_id} deleted."

    @validate_params
    async def set_discussion_subscription(
        course_identifier: str | int,
        topic_id: int,
        subscribed: bool,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview and confirm your topic notification subscription.

        Canvas's initial-post/group/announcement subscription restrictions are
        respected. Non-moderators require their student gate and course policy.
        """
        return await _set_state(
            course_identifier,
            topic_id,
            subscribed,
            confirmation_token,
            "set_discussion_subscription",
            "subscribed",
            student_only,
        )

    @validate_params
    async def set_discussion_read_state(
        course_identifier: str | int,
        topic_id: int,
        read: bool,
        include_entries: bool = False,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview and confirm your read/unread state for one discussion.

        Default marks only the topic's initial text. include_entries=True also
        marks all replies, without changing manually forced-read flags.
        Non-moderators require the enabled student gate and course policy.
        """
        return await _set_state(
            course_identifier,
            topic_id,
            read,
            confirmation_token,
            "set_discussion_read_state",
            "read_all" if include_entries else "read",
            student_only,
        )

    for function in (
        update_discussion_entry,
        delete_discussion_entry,
        set_discussion_subscription,
        set_discussion_read_state,
    ):
        if enabled is None or function.__name__ in enabled:
            mcp.tool(
                annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True)
            )(function)


async def _set_state(
    course_identifier: str | int,
    topic_id: int,
    requested: bool,
    confirmation_token: str | None,
    name: str,
    suffix: str,
    student_only: bool,
) -> str:
    if not _valid_id(topic_id):
        return "Error: topic_id must be positive."
    course_id = await get_course_id(course_identifier)
    student, error = await _course_access(course_id, name, student_only)
    if error:
        return error
    topic = await _topic(course_id, topic_id)
    field = "subscribed" if suffix == "subscribed" else "read_state"
    if (
        topic is None
        or (field == "subscribed" and type(topic.get(field)) is not bool)
        or (field == "read_state" and topic.get(field) not in ("read", "unread"))
    ):
        return "Error: Canvas did not establish the current discussion state."
    if suffix == "subscribed" and requested and topic.get("subscription_hold"):
        return "Error: Canvas blocks subscribing to this discussion. Review its initial-post or group restrictions."
    snapshot = {
        key: topic.get(key) for key in ("id", "title", field, "subscription_hold")
    }
    fingerprint = _fingerprint(
        name, course_id, topic_id, student, suffix, requested, snapshot
    )
    if not confirmation_token:
        return _preview(
            name,
            fingerprint,
            f"Would set your {suffix} state to {requested} for topic {topic_id}: "
            + fence_untrusted_inline(
                topic.get("title") or "Untitled", "discussion title"
            ),
        )
    error = redeem_confirmation(_GUARDS[name], confirmation_token, fingerprint)
    if error:
        return error
    final_student, error = await _course_access(course_id, name, student_only)
    if error or student != final_student:
        return error or "Error: discussion permission changed. Obtain a new preview."
    response = await _write(
        "put" if requested else "delete",
        canvas_path("courses", course_id, "discussion_topics", topic_id, suffix),
    )
    return (
        _write_error(response)
        or f"Your discussion {suffix} state was updated to {requested}."
    )


def register_student_communication_tools(mcp: FastMCP) -> None:
    """Register only individually enabled student discussion actions."""
    enabled = frozenset(get_config().student_write_tools) & _STUDENT_ACTIONS
    _register_discussion_actions(mcp, student_only=True, enabled=enabled)


async def _conversation(
    course_id: str | int, conversation_id: int
) -> dict[str, Any] | None:
    if await _moderator(course_id) is not True:
        return None
    response = await make_canvas_request(
        "get",
        canvas_path("conversations", conversation_id),
        params={"auto_mark_as_read": False},
    )
    if (
        not isinstance(response, dict)
        or "error" in response
        or response.get("id") != conversation_id
    ):
        return None
    contexts = response.get("audience_contexts")
    courses = contexts.get("courses") if isinstance(contexts, dict) else None
    if not isinstance(courses, dict) or str(course_id) not in courses:
        return None
    if (
        not isinstance(response.get("subject"), str)
        or type(response.get("message_count")) is not int
    ):
        return None
    return response


def _conversation_snapshot(conversation: dict[str, Any]) -> dict[str, Any]:
    return {
        key: conversation.get(key)
        for key in (
            "id",
            "subject",
            "audience",
            "message_count",
            "last_message_at",
            "workflow_state",
            "private",
            "subscribed",
            "starred",
            "audience_contexts",
        )
    }


def register_educator_communication_tools(mcp: FastMCP) -> None:
    """Register confirmed educator actions; moderators are checked at runtime."""
    _register_discussion_actions(mcp, student_only=False)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_discussion_topic(
        course_identifier: str | int,
        topic_id: int,
        allow_deleting_student_work: bool = False,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview/confirm deleting a discussion, including its backing assignment.

        Requires course moderation permission. Existing participation or graded
        submissions require allow_deleting_student_work=True in both calls.
        Announcements use their separate guarded deletion tool.
        """
        if not _valid_id(topic_id):
            return "Error: topic_id must be positive."
        course_id = await get_course_id(course_identifier)
        if await _moderator(course_id) is not True:
            return "Error: confirmed discussion moderation permission is required."
        topic = await _topic(course_id, topic_id)
        if (
            topic is None
            or type(topic.get("discussion_subentry_count")) is not int
            or topic["discussion_subentry_count"] < 0
            or "assignment_id" not in topic
        ):
            return (
                "Error: Canvas did not establish the discussion's participation state."
            )
        if topic.get("is_announcement") is not False:
            return "Error: use delete_announcement_with_confirmation for announcements."
        assignment = None
        if topic["assignment_id"] is not None:
            if not _valid_id(topic["assignment_id"]):
                return "Error: Canvas returned an invalid backing assignment ID."
            assignment = await make_canvas_request(
                "get",
                canvas_path(
                    "courses", course_id, "assignments", topic["assignment_id"]
                ),
            )
            if (
                not isinstance(assignment, dict)
                or "error" in assignment
                or assignment.get("id") != topic["assignment_id"]
                or type(assignment.get("has_submitted_submissions")) is not bool
            ):
                return "Error: Canvas did not establish the backing assignment student-work state."
        if (
            topic["discussion_subentry_count"]
            or (assignment and assignment["has_submitted_submissions"])
        ) and not allow_deleting_student_work:
            return "Error: discussion has participation or student work. Pass allow_deleting_student_work=true only if removing it is intentional."
        name = "delete_discussion_topic"
        fingerprint = _fingerprint(
            name,
            course_id,
            topic_id,
            _topic_snapshot(topic),
            assignment,
            allow_deleting_student_work,
        )
        if not confirmation_token:
            return _preview(
                name,
                fingerprint,
                "Would delete discussion "
                + fence_untrusted_inline(
                    topic.get("title") or "Untitled", "discussion title"
                )
                + f" (ID {topic_id}), {topic['discussion_subentry_count']} participation entries, and backing assignment {topic['assignment_id']}.\n"
                + fence_untrusted(topic.get("message") or "", "discussion body"),
                action="delete",
            )
        error = redeem_confirmation(_GUARDS[name], confirmation_token, fingerprint)
        if error:
            return error
        if await _moderator(course_id) is not True:
            return (
                "Error: discussion moderation permission changed. Nothing was deleted."
            )
        response = await _write(
            "delete", canvas_path("courses", course_id, "discussion_topics", topic_id)
        )
        return _write_error(response) or f"Discussion topic {topic_id} deleted."

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    @validate_params
    async def reply_to_conversation(
        course_identifier: str | int,
        conversation_id: int,
        body: str,
        recipient_ids: list[int] | None = None,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview/confirm sending a reply to an existing course-context inbox thread.

        Requires course discussion moderation permission. Default replies to
        every current audience member. Explicit recipients must already belong
        to that audience; adding people, forwarding, and attachments are omitted.
        Reads never mark the thread read. No student variant is offered because
        inbox threads can span courses with different student-write policies.
        """
        if contains_fence_markers(body):
            return FENCE_LEAK_ERROR
        if not _valid_id(conversation_id) or not body.strip():
            return "Error: a positive conversation_id and nonempty body are required."
        if recipient_ids is not None and (
            not recipient_ids
            or any(not _valid_id(v) for v in recipient_ids)
            or len(set(recipient_ids)) != len(recipient_ids)
        ):
            return "Error: recipient_ids must be distinct positive user IDs."
        course_id = await get_course_id(course_identifier)
        conversation = await _conversation(course_id, conversation_id)
        if conversation is None:
            return "Error: could not verify moderation permission and this conversation's course context. Nothing was sent."
        audience = conversation.get("audience")
        if (
            not isinstance(audience, list)
            or not audience
            or any(not _valid_id(v) for v in audience)
            or len(set(audience)) != len(audience)
        ):
            return "Error: Canvas did not establish the current conversation audience. Nothing was sent."
        recipients = sorted(recipient_ids if recipient_ids is not None else audience)
        if not set(recipients) <= set(audience):
            return (
                "Error: replies may target only current conversation audience members."
            )
        name = "reply_to_conversation"
        fingerprint = _fingerprint(
            name,
            course_id,
            conversation_id,
            _conversation_snapshot(conversation),
            recipients,
            body,
        )
        if not confirmation_token:
            return _preview(
                name,
                fingerprint,
                "Would send a reply to conversation "
                + fence_untrusted_inline(
                    conversation["subject"], "conversation subject"
                )
                + f" (ID {conversation_id}). Recipients: {recipients}\nBody:\n"
                + fence_untrusted(body, "proposed conversation reply"),
                action="send",
            )
        error = redeem_confirmation(_GUARDS[name], confirmation_token, fingerprint)
        if error:
            return error
        if await _moderator(course_id) is not True:
            return "Error: course moderation permission changed. Nothing was sent."
        response = await _write(
            "post",
            canvas_path("conversations", conversation_id, "add_message"),
            {"body": body, "recipients": recipients},
        )
        if error := _write_error(response):
            return error
        messages = response.get("messages")
        if (
            response.get("id") != conversation_id
            or not isinstance(messages, list)
            or len(messages) != 1
            or not isinstance(messages[0], dict)
            or not _valid_id(messages[0].get("id"))
        ):
            return "Warning: Canvas did not confirm the new reply message. Read the thread before retrying."
        return f"Reply sent to conversation {conversation_id}; message ID {messages[0]['id']}."

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_conversation_settings(
        course_identifier: str | int,
        conversation_id: int,
        workflow_state: str | None = None,
        subscribed: bool | None = None,
        starred: bool | None = None,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview/confirm your inbox state, star, or group-thread subscription.

        Does not edit sent messages. Requires course moderation permission and
        a thread associated with that course. Subscriptions apply only to group
        conversations; Canvas's private-thread restriction is checked first.
        """
        if not _valid_id(conversation_id):
            return "Error: conversation_id must be positive."
        if workflow_state is not None and workflow_state not in {
            "read",
            "unread",
            "archived",
        }:
            return "Error: workflow_state must be read, unread, or archived."
        payload = {
            key: value
            for key, value in {
                "workflow_state": workflow_state,
                "subscribed": subscribed,
                "starred": starred,
            }.items()
            if value is not None
        }
        if not payload:
            return "Error: no conversation setting changes specified."
        course_id = await get_course_id(course_identifier)
        conversation = await _conversation(course_id, conversation_id)
        if conversation is None:
            return "Error: could not verify course moderation permission and conversation context."
        if subscribed is not None and conversation.get("private") is not False:
            return "Error: subscriptions can change only for a confirmed group conversation."
        name = "update_conversation_settings"
        fingerprint = _fingerprint(
            name,
            course_id,
            conversation_id,
            _conversation_snapshot(conversation),
            payload,
        )
        if not confirmation_token:
            return _preview(
                name,
                fingerprint,
                f"Would change your settings for conversation {conversation_id}: {json.dumps(payload, sort_keys=True)}\nSubject: "
                + fence_untrusted_inline(
                    conversation["subject"], "conversation subject"
                ),
            )
        error = redeem_confirmation(_GUARDS[name], confirmation_token, fingerprint)
        if error:
            return error
        if await _moderator(course_id) is not True:
            return "Error: course moderation permission changed. Nothing was changed."
        response = await _write(
            "put",
            canvas_path("conversations", conversation_id),
            {"conversation": payload},
        )
        if error := _write_error(response):
            return error
        if response.get("id") != conversation_id or any(
            response.get(key) != value for key, value in payload.items()
        ):
            return "Warning: Canvas did not echo the requested conversation settings. Read the thread before retrying."
        return f"Conversation {conversation_id} settings updated for your inbox."

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_conversation(
        course_identifier: str | int,
        conversation_id: int,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview/confirm removing a conversation from your own inbox view.

        Other participants retain their messages. Requires course moderation
        permission and a conversation associated with that course.
        """
        if not _valid_id(conversation_id):
            return "Error: conversation_id must be positive."
        course_id = await get_course_id(course_identifier)
        conversation = await _conversation(course_id, conversation_id)
        if conversation is None:
            return "Error: could not verify course moderation permission and conversation context."
        name = "delete_conversation"
        fingerprint = _fingerprint(
            name, course_id, conversation_id, _conversation_snapshot(conversation)
        )
        if not confirmation_token:
            return _preview(
                name,
                fingerprint,
                f"Would remove conversation {conversation_id} and its {conversation['message_count']} messages from your inbox view. Other participants keep their copies.\nSubject: "
                + fence_untrusted_inline(
                    conversation["subject"], "conversation subject"
                ),
                action="delete",
            )
        error = redeem_confirmation(_GUARDS[name], confirmation_token, fingerprint)
        if error:
            return error
        if await _moderator(course_id) is not True:
            return "Error: course moderation permission changed. Nothing was deleted."
        response = await _write("delete", canvas_path("conversations", conversation_id))
        if error := _write_error(response):
            return error
        if response.get("id") != conversation_id or response.get("message_count") != 0:
            return "Warning: Canvas did not confirm removal from your inbox. Read the thread before retrying."
        return f"Conversation {conversation_id} deleted from your inbox view."
