"""The guardrail wrapper, with a stand-in for the Bedrock client (no AWS needed)."""

from app import config, guardrails


class FakeBedrock:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def apply_guardrail(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def use_fake(monkeypatch, response):
    fake = FakeBedrock(response)
    monkeypatch.setattr(config, "GUARDRAIL_ID", "gr-123")
    monkeypatch.setattr(config, "GUARDRAIL_VERSION", "1")
    monkeypatch.setattr(guardrails, "bedrock", lambda: fake)
    return fake


def test_skipped_when_no_guardrail_is_configured(monkeypatch):
    monkeypatch.setattr(config, "GUARDRAIL_ID", "")
    assert guardrails.check("anything", "INPUT") == ("allowed", "anything")


def test_passes_text_through_when_the_guardrail_allows_it(monkeypatch):
    fake = use_fake(monkeypatch, {"action": "NONE", "outputs": []})
    assert guardrails.check("How many sick days?", "INPUT") == ("allowed", "How many sick days?")
    assert fake.calls[0]["source"] == "INPUT"
    assert fake.calls[0]["content"] == [{"text": {"text": "How many sick days?"}}]


def test_returns_the_refusal_when_a_policy_blocks(monkeypatch):
    use_fake(monkeypatch, {
        "action": "GUARDRAIL_INTERVENED",
        "outputs": [{"text": "Blocked."}],
        "assessments": [{"sensitiveInformationPolicy": {"piiEntities": [
            {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCKED"}]}}],
    })
    assert guardrails.check("My SSN is 536-22-1234", "INPUT") == ("blocked", "Blocked.")


def test_returns_the_masked_text_when_contact_details_are_anonymized(monkeypatch):
    use_fake(monkeypatch, {
        "action": "GUARDRAIL_INTERVENED",
        "outputs": [{"text": "Call me on {PHONE} about PTO"}],
        "assessments": [{"sensitiveInformationPolicy": {"piiEntities": [
            {"type": "PHONE", "action": "ANONYMIZED"}]}}],
    })
    assert guardrails.check("Call me on 555-201-3344 about PTO", "INPUT") == ("masked", "Call me on {PHONE} about PTO")


def test_findings_name_the_type_and_action_never_the_text(monkeypatch):
    # The chat handler adds these findings to the trace, so they must never hold the value itself.
    use_fake(monkeypatch, {
        "action": "GUARDRAIL_INTERVENED",
        "outputs": [{"text": "Blocked."}],
        "assessments": [{"sensitiveInformationPolicy": {"piiEntities": [
            {"type": "US_SOCIAL_SECURITY_NUMBER", "match": "536-22-1234", "action": "BLOCKED"}]}}],
    })
    guardrails.check("My SSN is 536-22-1234", "INPUT")
    assert guardrails.LAST_FINDINGS == ["sensitiveInformationPolicy: US_SOCIAL_SECURITY_NUMBER BLOCKED"]


def test_inside_a_trace_the_check_records_its_decision_but_not_the_question(monkeypatch, capsys):
    import json
    from app.telemetry import span

    monkeypatch.setenv("HR_TRACING_ENABLED", "true")
    monkeypatch.setenv("HR_TRACE_SYNTHETIC_CONTENT", "true")
    monkeypatch.setenv("LAB_ENVIRONMENT", "staging")
    use_fake(monkeypatch, {
        "action": "GUARDRAIL_INTERVENED",
        "outputs": [{"text": "Call me on {PHONE} about PTO"}],
        "assessments": [{"sensitiveInformationPolicy": {"piiEntities": [
            {"type": "PHONE", "action": "ANONYMIZED"}]}}],
    })
    with span("HR answer", "agent"):
        guardrails.check("Call me on 555-201-3344 about PTO", "INPUT")

    printed = capsys.readouterr().out
    check_span = json.loads(printed.splitlines()[0])["span"]
    assert check_span["name"] == "Bedrock Guardrail: question"
    assert "masked" in printed and "PHONE ANONYMIZED" in printed
    assert "555-201-3344" not in printed


def test_findings_never_use_the_matched_text(monkeypatch):
    # Word-list findings carry the matched word in "match"; only the policy's kind is kept.
    use_fake(monkeypatch, {
        "action": "GUARDRAIL_INTERVENED",
        "outputs": [{"text": "Blocked."}],
        "assessments": [{"wordPolicy": {"customWords": [{"match": "secret-word", "action": "BLOCKED"}]}}],
    })
    guardrails.check("this has secret-word in it", "INPUT")
    assert guardrails.LAST_FINDINGS == ["wordPolicy: finding BLOCKED"]
