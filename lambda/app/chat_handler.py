"""The chat API: one Lambda function behind Amazon API Gateway.

API Gateway checks the Cognito token BEFORE this code runs. A request without a
valid token never reaches us. API Gateway then passes the token's claims in the
event, so the signed-in email comes from there: never from the request body,
never from a header the browser could fake.

Routes (defined in terraform/api_gateway.tf):

    GET  /api/health                             no sign-in needed
    GET  /api/me                                 who is signed in
    GET  /api/conversations                      their chats
    GET  /api/conversations/{id}/messages        one chat, if it's theirs
    DELETE /api/conversations/{id}               delete one chat, if it's theirs
    POST /api/chat                               ask a question
    POST /api/feedback                           thumbs up or down on one answer
"""

import base64
import json
import re

from botocore.exceptions import BotoCoreError, ClientError

from app import assistant, chat_history, employees, guardrails
from app.prompt_registry import release_identity
from app.model_gateway import GatewayPolicyDenied, GatewayUnavailable, gateway_context
from app.telemetry import conversation_context, current_user, last_trace_id, record_score, span, update_current_span


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", **release_identity()},
        "body": json.dumps(body, default=str),
    }


def signed_in_email(event):
    """The email from the verified Cognito token, as API Gateway passed it."""
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    return claims.get("email")


def read_body(event):
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body)


def source_names(sources):
    names = set()
    for source in sources:
        names.add(f"{source['title']}, {source['section']}")
    return sorted(names)


def handler(event, context):
    route = event.get("routeKey", "")

    if route == "GET /api/health":
        return respond(200, {"status": "ok"})

    email = signed_in_email(event)
    if not email:
        return respond(401, {"detail": "Sign in first."})

    employee = employees.find_by_email(email)
    if employee is None:
        detail = "You don't have an HR assistant account. Contact People Operations."
        return respond(403, {"detail": detail})

    if route == "GET /api/me":
        return respond(200, employee)

    if route == "GET /api/conversations":
        return respond(200, chat_history.list_conversations(employee["employee_id"]))

    if route == "GET /api/conversations/{id}/messages":
        return get_messages(event, employee)

    if route == "DELETE /api/conversations/{id}":
        return delete_conversation(event, employee)

    if route == "POST /api/chat":
        return chat(event, employee)

    if route == "POST /api/feedback":
        return feedback(event, employee)

    return respond(404, {"detail": "Not found."})


def feedback(event, employee):
    """A person rates one answer. The rating becomes a score on that answer's Langfuse trace.

    Body: {"conversation_id": 12, "trace_id": "...", "helpful": true, "comment": "optional"}
    Reviewers can then list every unhelpful answer in Langfuse and open its full trace.
    """
    try:
        body = read_body(event)
        conversation_id = int(body.get("conversation_id"))
    except (ValueError, TypeError):
        return respond(400, {"detail": "The feedback wasn't valid."})
    trace_id = str(body.get("trace_id") or "")
    helpful = body.get("helpful")
    if not re.fullmatch(r"[0-9a-f]{32}", trace_id) or not isinstance(helpful, bool):
        return respond(400, {"detail": "The feedback wasn't valid."})

    # Only the person who owns the conversation may rate its answers.
    if not chat_history.get_messages(conversation_id, employee["employee_id"]):
        return respond(404, {"detail": "Conversation not found."})

    # A comment is checked like a question: personal data is masked, a blocked comment is dropped.
    comment = str(body.get("comment") or "").strip()[:1000]
    if comment:
        try:
            status, safe_comment = guardrails.check(comment, "INPUT")
        except (BotoCoreError, ClientError):
            safe_comment, status = "", "blocked"
        if status == "blocked":
            comment = ""
        else:
            comment = safe_comment

    if helpful:
        value = 1
    else:
        value = 0
    record_score(trace_id, "user_feedback", value, comment or None)
    return respond(200, {"recorded": True})


def delete_conversation(event, employee):
    try:
        conversation_id = int((event.get("pathParameters") or {}).get("id", ""))
    except ValueError:
        return respond(404, {"detail": "Conversation not found."})
    if not chat_history.delete_conversation(conversation_id, employee["employee_id"]):
        # Same answer for someone else's chat as for a missing one
        return respond(404, {"detail": "Conversation not found."})
    return respond(200, {"deleted": conversation_id})


def get_messages(event, employee):
    try:
        conversation_id = int((event.get("pathParameters") or {}).get("id", ""))
    except ValueError:
        return respond(404, {"detail": "Conversation not found."})

    rows = chat_history.get_messages(conversation_id, employee["employee_id"])
    if not rows:
        # Someone else's chat looks exactly like a chat that doesn't exist
        return respond(404, {"detail": "Conversation not found."})
    for row in rows:
        row["sources"] = source_names(row["sources"])
    return respond(200, rows)


def chat(event, employee):
    try:
        body = read_body(event)
    except (ValueError, UnicodeDecodeError):
        return respond(400, {"detail": "The request wasn't valid JSON."})

    question = str(body.get("question") or "").strip()
    if not question:
        return respond(400, {"detail": "Type a question first."})

    conversation_id = body.get("conversation_id")
    history = []
    if conversation_id is not None:
        history = chat_history.get_messages(conversation_id, employee["employee_id"])
        if not history:
            return respond(404, {"detail": "Conversation not found."})

    # Check the question with the guardrail before it is stored or traced anywhere.
    # A blocked question is stored as a placeholder. A masked question keeps its
    # placeholders, such as {PHONE}, everywhere: history, logs, Langfuse and the model.
    try:
        status, safe_question = guardrails.check(question, "INPUT")
    except (BotoCoreError, ClientError) as error:
        print(f"guardrail unavailable: {error!r}")  # goes to CloudWatch Logs
        return respond(502, {"detail": "The assistant is unavailable right now. Try again in a moment."})
    if status == "blocked":
        stored_question = guardrails.REMOVED
    else:
        stored_question = safe_question
    # What the question check found, by type only, for the trace below.
    question_guardrail = {"decision": status, "findings": guardrails.LAST_FINDINGS or "none"}

    # Tags let you filter traces in Langfuse: by role, and people apart from test accounts.
    email = signed_in_email(event) or ""
    is_test_account = email.lower().startswith("qa.")
    if is_test_account:
        trace_tags = [employee["role"], "test-account"]
    else:
        trace_tags = [employee["role"], "person"]

    # Allocate the conversation before inference so even the first trace is grouped.
    # A failed request may leave an empty conversation for diagnosis.
    if conversation_id is None:
        conversation_id = chat_history.start_conversation(employee['employee_id'], stored_question)

    if status == "blocked":
        # The guardrail's refusal message is the answer. The model is never called.
        # The trace records that it happened and what kind of data it found, never the question.
        with conversation_context(employee['employee_id'], conversation_id, tags=trace_tags + ["blocked"]):
            with span("Blocked question", "guardrail"):
                update_current_span(metadata=question_guardrail, level="WARNING",
                                    status_message="The guardrail blocked the question before any model call")
        chat_history.add_message(conversation_id, "user", stored_question)
        chat_history.add_message(conversation_id, "assistant", safe_question, [])
        return respond(200, {"conversation_id": conversation_id, "answer": safe_question, "sources": []})

    try:
        # Pipeline test accounts (qa.*) are treated differently from people:
        # LiteLLM feature 4, caching: people get cached answers, test accounts never do.
        # LiteLLM features 8 and 10: model calls for a person carry their opaque user
        # ID, so the gateway shows their spend and applies the per-person limit.
        # Test accounts send no ID, so the pipeline is bounded by the team and key
        # limits instead of a person's.
        with conversation_context(employee['employee_id'], conversation_id, tags=trace_tags):
            if is_test_account:
                gateway_user = None
            else:
                gateway_user = current_user()
            with gateway_context(user=gateway_user, use_cache=not is_test_account):
                answer, sources = assistant.answer(safe_question, history, employee, question_checked=True,
                                                   question_guardrail=question_guardrail)
    except GatewayPolicyDenied as error:
        # LiteLLM features 5, 6 and 10: the gateway refused the call because a budget
        # or rate limit was reached, for this person, the team or the key.
        print(f"gateway limit reached: {error!r}")  # goes to CloudWatch Logs
        detail = "You've reached the assistant's usage limit. Wait a minute and try again."
        return respond(429, {"detail": detail})
    except (BotoCoreError, ClientError, GatewayUnavailable) as error:
        print(f"assistant unavailable: {error!r}")  # goes to CloudWatch Logs
        detail = "The assistant is unavailable right now. Try again in a moment."
        return respond(502, {"detail": detail})

    chat_history.add_message(conversation_id, "user", stored_question)
    chat_history.add_message(conversation_id, "assistant", answer, sources)

    return respond(200, {
        "conversation_id": conversation_id,
        "answer": answer,
        "sources": source_names(sources),
        # Feedback on this answer is attached to this trace in Langfuse.
        "trace_id": last_trace_id(),
    })
