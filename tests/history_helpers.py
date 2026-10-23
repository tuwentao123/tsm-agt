"""Inspect native model history without assuming it is duplicated in JSON."""
import json


def session_history_view(request):
    metadata = next(message for message in request.messages
                    if message.message_id.startswith("session-context-"))
    body = json.loads(metadata.text)
    if body.get("history_format") != "native":
        return body
    assert body["recent_messages"] == [], "native dialogue must not be duplicated in metadata"
    by_id = {message.message_id: message for message in request.messages}
    body["recent_messages"] = [
        {**entry, "text": by_id[entry["native_message_id"]].text}
        for entry in body.get("history_entries", []) if entry.get("native_message_id") in by_id
    ]
    return body
