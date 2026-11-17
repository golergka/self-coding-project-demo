from app.examples.instagram_pricing import handle


def test_pricing_conversation_waits_for_an_email_from_the_original_sender():
    value = None
    sent = []

    def api(operation, body):
        nonlocal value
        if operation == "state.get":
            return {"value": value}
        if operation == "state.put":
            value = body["value"]
        else:
            sent.append((operation, body))
        return {"ok": True}

    def comment(text):
        return handle({"kind": "comment", "payload": {
            "text": text, "comment_id": "comment-1", "sender_id": "customer",
        }}, api)

    def dm(text, sender="customer"):
        return handle({"kind": "dm", "payload": {"text": text, "sender_id": sender}}, api)

    assert comment("unrelated") == "completed"
    assert sent == []
    assert comment("Pricing, please") == "waiting"
    assert dm("hello") == "waiting"
    assert dm("someone@example.com", sender="different-user") == "waiting"
    assert len(sent) == 2
    assert dm("My email is customer@example.com") == "completed"
    assert [name for name, _ in sent] == ["social.reply", "social.dm", "social.dm"]
    assert sent[-1][1]["text"].endswith("https://example.com/pricing")
    assert dm("customer@example.com") == "completed"
    assert len(sent) == 3
