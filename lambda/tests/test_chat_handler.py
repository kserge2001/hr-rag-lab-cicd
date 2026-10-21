"""The chat Lambda, called with the same events API Gateway sends.
The model is replaced by a stand-in here, so these tests cost nothing."""

import json

import pytest

from app import assistant, chat_handler


def event(route, email=None, body=None, path_id=None):
    authorizer = {"jwt": {"claims": {"email": email}}} if email else {}
    return {
        "routeKey": route,
        "pathParameters": {"id": path_id} if path_id else None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": authorizer},
    }


def call(*args, **kwargs):
    result = chat_handler.handler(event(*args, **kwargs), None)
    return result["statusCode"], json.loads(result["body"])


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    def fake_answer(question, history, employee, question_checked=False, question_guardrail=None):
        return f"Answer for {employee['full_name']} ({len(history)} earlier messages)", []

    monkeypatch.setattr(assistant, "answer", fake_answer)


def test_health_needs_no_sign_in():
    assert call("GET /api/health") == (200, {"status": "ok"})


def test_no_token_claims_means_401():
    status, _ = call("GET /api/me")
    assert status == 401


def test_unknown_email_means_403():
    status, _ = call("GET /api/me", email="stranger@example.com")
    assert status == 403


def test_me_returns_the_signed_in_employee():
    status, body = call("GET /api/me", email="qa.employee@northwind.example")
    assert status == 200
    assert body["full_name"] == "Casey Test"


def test_chat_saves_history_and_continues_it():
    status, first = call("POST /api/chat", email="qa.employee@northwind.example", body={"question": "Hi"})
    assert status == 200
    conversation = first["conversation_id"]

    _, second = call(
        "POST /api/chat",
        email="qa.employee@northwind.example",
        body={"question": "And?", "conversation_id": conversation},
    )
    assert second["answer"] == "Answer for Casey Test (2 earlier messages)"

    status, messages = call("GET /api/conversations/{id}/messages", "qa.employee@northwind.example", path_id=str(conversation))
    assert status == 200 and len(messages) == 4


def test_people_can_delete_their_own_chat():
    _, first = call("POST /api/chat", email="qa.employee@northwind.example", body={"question": "Delete me"})
    conversation = str(first["conversation_id"])

    assert call("DELETE /api/conversations/{id}", "qa.employee@northwind.example", path_id=conversation)[0] == 200
    status, _ = call("GET /api/conversations/{id}/messages", "qa.employee@northwind.example", path_id=conversation)
    assert status == 404


def test_nobody_can_delete_someone_elses_chat():
    _, first = call("POST /api/chat", email="qa.employee@northwind.example", body={"question": "Keep me"})
    conversation = str(first["conversation_id"])

    status, _ = call("DELETE /api/conversations/{id}", "qa.hr@northwind.example", path_id=conversation)
    assert status == 404
    status, messages = call("GET /api/conversations/{id}/messages", "qa.employee@northwind.example", path_id=conversation)
    assert status == 200 and len(messages) == 2


def test_nobody_can_open_someone_elses_chat():
    _, first = call("POST /api/chat", email="qa.employee@northwind.example", body={"question": "Private"})
    conversation = first["conversation_id"]

    status, _ = call("GET /api/conversations/{id}/messages", "qa.hr@northwind.example", path_id=str(conversation))
    assert status == 404
    status, _ = call(
        "POST /api/chat",
        email="qa.hr@northwind.example",
        body={"question": "Continue", "conversation_id": conversation},
    )
    assert status == 404


def test_empty_question_is_rejected():
    status, body = call("POST /api/chat", email="qa.employee@northwind.example", body={"question": "  "})
    assert status == 400 and body["detail"] == "Type a question first."
