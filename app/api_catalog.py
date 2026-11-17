"""The shared contract for the hypothetical social service."""


OPERATIONS = {
    "social.accounts.list": {"required": [], "optional": [], "effect": False},
    "social.posts.list": {"required": ["account_id"], "optional": ["limit"], "effect": False},
    "social.posts.get": {"required": ["post_id"], "optional": [], "effect": False},
    "social.comments.list": {"required": ["post_id"], "optional": ["limit"], "effect": False},
    "social.messages.list": {"required": ["recipient_id"], "optional": ["limit"], "effect": False},
    "social.profiles.get": {"required": ["profile_id"], "optional": [], "effect": False},
    "social.posts.create": {"required": ["account_id", "text", "key"], "optional": [], "effect": True},
    "social.posts.update": {"required": ["post_id", "text", "key"], "optional": [], "effect": True},
    "social.posts.delete": {"required": ["post_id", "key"], "optional": [], "effect": True},
    "social.reply": {"required": ["comment_id", "text", "key"], "optional": [], "effect": True},
    "social.dm": {"required": ["recipient_id", "text", "key"], "optional": [], "effect": True},
}


def validate(operation, body):
    spec = OPERATIONS.get(operation)
    if spec is None:
        raise ValueError("unknown social operation")
    if any(not isinstance(body.get(key), str) or not body[key] for key in spec["required"]):
        raise ValueError("missing or invalid social request fields")
    if set(body) - set(spec["required"]) - set(spec["optional"]):
        raise ValueError("unexpected social request fields")
    if "limit" in body and (type(body["limit"]) is not int or not 1 <= body["limit"] <= 100):
        raise ValueError("limit must be an integer between 1 and 100")
    return spec


def demo_fixtures():
    """Default mock responses. A caller can override any operation."""
    return {
        "social.accounts.list": {"ok": True, "accounts": [{"id": "demo-account", "platform": "instagram", "name": "Demo shop"}]},
        "social.posts.list": {"ok": True, "posts": [{"id": "demo-post", "account_id": "demo-account", "text": "Ask us about pricing"}]},
        "social.posts.get": {"ok": True, "post": {"id": "demo-post", "account_id": "demo-account", "text": "Ask us about pricing"}},
        "social.comments.list": {"ok": True, "comments": []},
        "social.messages.list": {"ok": True, "messages": []},
        "social.profiles.get": {"ok": True, "profile": {"id": "demo-user", "name": "Demo customer"}},
        "social.posts.create": {"ok": True, "post": {"id": "created-post", "account_id": "demo-account", "text": "Created post"}},
        "social.posts.update": {"ok": True, "post": {"id": "demo-post", "account_id": "demo-account", "text": "Updated post"}},
        "social.posts.delete": {"ok": True},
        "social.reply": {"ok": True, "comment": {"id": "reply-1", "comment_id": "comment-1", "text": "Reply"}},
        "social.dm": {"ok": True, "message": {"id": "message-1", "recipient_id": "demo-user", "text": "Message", "direction": "outgoing"}},
    }
