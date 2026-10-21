"""Amazon Bedrock Guardrails: check the question before answering, and the answer before sending it.

The guardrail (created by Terraform in bedrock.tf) blocks harmful content and
prompt-injection attempts. For personal data it does one of two things:

  BLOCK      Social Security, card and bank account numbers. The whole text is replaced.
  ANONYMIZE  Email addresses and phone numbers. Only that part is replaced, for
             example with {EMAIL}, and the rest of the question is still answered.

We call it with the ApplyGuardrail API, separately from the model, so the same
checks work whatever model writes the answer.

Until the guardrail exists GUARDRAIL_ID is empty and the checks are skipped,
so the assistant still answers while you are part way through building the stack.
"""

from contextlib import nullcontext
from functools import cache

from app import config
from app.telemetry import inside_trace, span, update_current_span

# What is stored and traced in place of a blocked question, so the blocked value
# never reaches the chat history, the request log or Langfuse.
REMOVED = "[Removed: the message contained sensitive information]"

# What the most recent check found, such as "sensitiveInformationPolicy: PHONE ANONYMIZED".
# Types only, never the text. The question is checked before its trace starts,
# so the chat handler reads this to add the findings to the trace afterwards.
LAST_FINDINGS = []


@cache
def bedrock():
    import boto3

    return boto3.client("bedrock-runtime", region_name=config.AWS_REGION)


def was_blocked(response):
    """True when any guardrail policy blocked the text, False when it only masked parts of it."""
    for assessment in response.get("assessments", []):
        # One entry per policy: content filters, sensitive information, and so on.
        for policy in assessment.values():
            if not isinstance(policy, dict):
                continue
            # Each policy lists its findings, for example piiEntities or filters.
            for findings in policy.values():
                if not isinstance(findings, list):
                    continue
                for finding in findings:
                    if isinstance(finding, dict) and finding.get("action") == "BLOCKED":
                        return True
    return False


def findings(response):
    """Every finding as "policy: type ACTION", for example "contentPolicy: PROMPT_ATTACK BLOCKED"."""
    found = []
    for assessment in response.get("assessments", []):
        for policy_name, policy in assessment.items():
            if not isinstance(policy, dict):
                continue
            for items in policy.values():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict) or "action" not in item:
                        continue
                    # "type" or "name" says what kind of data it was. Never use "match":
                    # for word and pattern filters it holds the matched text itself.
                    kind = item.get("type") or item.get("name") or "finding"
                    found.append(f"{policy_name}: {kind} {item['action']}")
    return found


def check(text, source):
    """Check one piece of text.

    source  "INPUT" for the employee's question, "OUTPUT" for the answer.

    Returns (status, text_to_use):
      "allowed"  nothing found; text_to_use is the original text
      "masked"   personal data was replaced, for example with {PHONE}; the rest is kept
      "blocked"  the text must not be used; text_to_use is the guardrail's refusal message
    """
    global LAST_FINDINGS
    LAST_FINDINGS = []
    if not config.GUARDRAIL_ID:
        return "allowed", text

    # Inside a trace (the answer check), the check is its own step. The question is
    # checked before its trace starts, so it records nothing here.
    if inside_trace():
        step = span("Bedrock Guardrail: " + ("answer" if source == "OUTPUT" else "question"), "guardrail")
    else:
        step = nullcontext()

    with step:
        response = bedrock().apply_guardrail(
            guardrailIdentifier=config.GUARDRAIL_ID,
            guardrailVersion=config.GUARDRAIL_VERSION,
            source=source,
            content=[{"text": {"text": text}}],
        )
        LAST_FINDINGS = findings(response)

        if response["action"] != "GUARDRAIL_INTERVENED":
            status, result = "allowed", text
        else:
            replacement = " ".join(output["text"] for output in response.get("outputs", []))
            if was_blocked(response):
                status, result = "blocked", replacement or "Sorry, I can't help with that request."
            else:
                status, result = "masked", replacement or text

        # Tracing: the decision and what kinds of data it found, never the text it checked.
        # The answer the employee receives is content, so it is kept only when allowed.
        if status == "allowed":
            level = None
        else:
            level = "WARNING"
        # Only the answer is recorded: the question could hold the very value that was blocked.
        if source == "OUTPUT":
            sent_on = {"text_sent_on": result}
        else:
            sent_on = None
        update_current_span(
            output=sent_on,
            metadata={"checked": source, "decision": status, "findings": LAST_FINDINGS or "none",
                      "guardrail_id": config.GUARDRAIL_ID, "guardrail_version": config.GUARDRAIL_VERSION},
            level=level,
        )
    return status, result
