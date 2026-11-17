"""Example automation written against the project runtime API."""

import re

EMAIL = re.compile(r"(?<![\w@])[\w.%+\-]+@[\w\-]+(?:\.[\w\-]+)+\b", re.ASCII)
PRICING = re.compile(r"\bpricing\b", re.IGNORECASE)
LINK = "https://example.com/pricing"


def handle(event, api):
    """Reply, ask for an email, then continue on a later DM."""
    payload = event["payload"]
    state = api("state.get", {})["value"] or {}
    if event["kind"] == "comment":
        if state.get("waiting"):
            return "waiting"
        if not PRICING.search(payload.get("text", "")):
            return "completed"
        sender = payload["sender_id"]
        send(api, "social.reply", {
            "comment_id": payload["comment_id"],
            "text": "Sent you a DM!",
            "key": "comment-reply",
        })
        send(api, "social.dm", {
            "recipient_id": sender,
            "text": "What email address should I use?",
            "key": "ask-email",
        })
        api("state.put", {"value": {"waiting": True, "sender_id": sender}})
        return "waiting"

    if not state.get("waiting"):
        return "completed"
    if event["kind"] != "dm" or payload.get("sender_id") != state["sender_id"]:
        return "waiting"
    if not EMAIL.search(payload.get("text", "")):
        return "waiting"
    send(api, "social.dm", {
        "recipient_id": state["sender_id"],
        "text": f"Here is the pricing link: {LINK}",
        "key": "share-link",
    })
    api("state.put", {"value": {"waiting": False}})
    return "completed"


def send(api, operation, body):
    result = api(operation, body)
    if not result["ok"]:
        raise RuntimeError(result.get("error") or f"{operation} failed")
