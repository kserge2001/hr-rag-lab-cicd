"""Tracing: each step of a request becomes one span, printed as a JSON line for Langfuse.

How it works:
  - @observed('name', kind) above a function, or `with span('name', kind):` around a block,
    records that step as a span: its timing, its parent step, and whether it failed.
  - Inside a step, update_current_span(...) adds what happened: input, output, metadata,
    model settings, token use and cost. It works like the Langfuse SDK function of the same name.
  - Each finished span is printed as one JSON line. Lambda sends printed lines to CloudWatch
    Logs, and lambda/app/trace_exporter.py forwards them to Langfuse. The chat Lambda
    never holds Langfuse keys and never waits for Langfuse.

Two privacy rules:
  - Content (questions, messages, passages, answers) is recorded only when
    HR_TRACE_SYNTHETIC_CONTENT=true in the staging lab, and always passes through redact().
  - Metadata must hold facts about the content, never the content itself: counts, names,
    scores, decisions. A value the guardrail blocked is never recorded, only its type.
"""
import contextvars
import functools
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager

from app.prompt_registry import release_identity

# Tokens and keys that must never reach a trace, even inside synthetic content.
_SECRET = re.compile(
    r"(?:sk-lf-[A-Za-z0-9-]+|AKIA[A-Z0-9]{16}|Bearer\s+[A-Za-z0-9._~-]+"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

# Longest text kept for one input or output. Longer text is cut, and the cut is marked.
CONTENT_LIMIT = 20000

# The span being recorded right now, and the conversation it belongs to.
_current = contextvars.ContextVar('hr_trace', default=None)
_identity = contextvars.ContextVar('hr_trace_identity', default=None)


def redact(value):
    """Replace anything that looks like a token or key with [REDACTED]."""
    return _SECRET.sub('[REDACTED]', str(value))


def tracing_enabled():
    return os.getenv('HR_TRACING_ENABLED', 'false') == 'true'


def content_allowed():
    """True only in the staging lab with the synthetic-content switch on."""
    return (os.getenv('HR_TRACE_SYNTHETIC_CONTENT', 'false') == 'true'
            and os.getenv('LAB_ENVIRONMENT') == 'staging')


@contextmanager
def conversation_context(employee_id, conversation_id, tags=None):
    """Group every span inside this block under one Langfuse user and session.

    The IDs are hashes, so Langfuse never sees the employee ID. Stable opaque
    identifiers are still pseudonymous data, not anonymized data.
    tags, such as ["employee", "person"], let you filter traces in Langfuse.
    """
    namespace = os.getenv('AWS_LAMBDA_FUNCTION_NAME', 'hr-rag-lab')
    user = hashlib.sha256(f'{namespace}:user:{employee_id}'.encode()).hexdigest()[:24]
    session = hashlib.sha256(f'{namespace}:user:{employee_id}:chat:{conversation_id}'.encode()).hexdigest()[:24]
    token = _identity.set({'user': 'hr-user-' + user, 'session': 'hr-chat-' + session, 'tags': list(tags or [])})
    try:
        yield
    finally:
        _identity.reset(token)


def inside_trace():
    """True while a span is being recorded, so a new span would join that trace."""
    return tracing_enabled() and _current.get() is not None


def current_user():
    """The opaque user ID for the conversation being answered, or None."""
    grouping = _identity.get()
    if grouping is None:
        return None
    return grouping['user']


def attribute(key, value):
    return {'key': key, 'value': {'stringValue': str(value)}}


def _content(value):
    """JSON text for an input or output: redacted, and cut at CONTENT_LIMIT."""
    text = redact(json.dumps(value, default=str, ensure_ascii=False))
    if len(text) > CONTENT_LIMIT:
        text = text[:CONTENT_LIMIT] + f'... [cut: {len(text)} characters in total]'
    return text


def update_current_span(input=None, output=None, metadata=None, level=None, status_message=None,
                        model=None, model_parameters=None, usage=None, cost_usd=None):
    """Add details to the span being recorded right now. Does nothing outside a span.

    input, output      content: kept only when content_allowed()
    metadata           a dict of facts, shown under Metadata in Langfuse
    level              "DEBUG", "DEFAULT", "WARNING" or "ERROR"; Langfuse highlights the last two
    model, model_parameters, usage, cost_usd    for model calls (kind "generation")
    """
    state = _current.get()
    if state is None:
        return
    extra = state['extra']
    if input is not None and content_allowed():
        extra['langfuse.observation.input'] = _content(input)
    if output is not None and content_allowed():
        extra['langfuse.observation.output'] = _content(output)
    for key, value in (metadata or {}).items():
        if not isinstance(value, str):
            value = json.dumps(value, default=str)
        extra[f'langfuse.observation.metadata.{key}'] = redact(value)[:CONTENT_LIMIT]
    if level:
        extra['langfuse.observation.level'] = level
    if status_message:
        extra['langfuse.observation.status_message'] = redact(status_message)[:2000]
    if model:
        extra['langfuse.observation.model.name'] = model
    if model_parameters:
        extra['langfuse.observation.model.parameters'] = json.dumps(model_parameters)
    if usage:
        extra['langfuse.observation.usage_details'] = json.dumps(usage)
    if cost_usd not in (None, ''):
        extra['langfuse.observation.cost_details'] = json.dumps({'total': float(cost_usd)})


_last_trace = {'id': None}   # the newest trace this Lambda started, for the chat response


def last_trace_id():
    """The ID of the trace the most recent request created, or None when tracing is off.

    The chat API returns it with the answer, so feedback on that answer joins the same trace.
    """
    return _last_trace['id']


def record_score(trace_id, name, value, comment=None):
    """Attach a score, such as a person's thumbs up (1) or down (0), to a trace.

    Printed like a span, so the exporter Lambda sends it to Langfuse. The score ID is fixed
    per trace and name, so a changed vote replaces the earlier one instead of adding a second.
    A comment is content: it is kept only when content is allowed, and always redacted.
    """
    if not tracing_enabled():
        return
    score = {'id': f'{trace_id}-{name}', 'traceId': trace_id, 'name': name,
             'value': value, 'dataType': 'BOOLEAN', 'environment': 'staging'}
    if comment and content_allowed():
        score['comment'] = redact(comment)[:1000]
    print(json.dumps({'trace_record': 'hr_score', 'hr_score': True, 'score': score}, separators=(',', ':')))


def _start(name, kind):
    """Open a span under the current one, and return what _finish needs to close it."""
    parent = _current.get()
    if parent:
        trace_id = parent['trace']
    else:
        trace_id = uuid.uuid4().hex
        _last_trace['id'] = trace_id
    state = {'trace': trace_id, 'id': uuid.uuid4().hex[:16], 'extra': {}}
    token = _current.set(state)
    return {'name': name, 'kind': kind, 'parent': parent, 'state': state, 'token': token, 'start': time.time_ns()}


def _base_attributes(kind):
    """What every span carries: its type, the release, and the conversation it belongs to."""
    attrs = [attribute('langfuse.observation.type', kind),
             attribute('langfuse.environment', 'staging'),
             attribute('langfuse.trace.name', 'HR application request')]
    grouping = _identity.get()
    if grouping:
        attrs.append(attribute('langfuse.user.id', grouping['user']))
        attrs.append(attribute('langfuse.session.id', grouping['session']))
        if grouping['tags']:
            tag_values = [{'stringValue': tag} for tag in grouping['tags']]
            attrs.append({'key': 'langfuse.trace.tags', 'value': {'arrayValue': {'values': tag_values}}})
    identity = release_identity()
    prompt_version = identity['x-hr-prompt-version']
    if prompt_version.isdigit():
        readable = 'hr-policy-system v' + prompt_version
    else:
        readable = 'hr-policy-system unpublished'
    # The release field shows the release number, such as v1.0.3, so traces can be filtered by release.
    attrs += [attribute('langfuse.release', identity.get('x-hr-release', identity['x-hr-commit'])),
              attribute('langfuse.observation.metadata.commit', identity['x-hr-commit']),
              attribute('langfuse.version', readable),
              attribute('langfuse.observation.metadata.prompt_version', prompt_version),
              attribute('langfuse.observation.metadata.release_stage', 'candidate'),
              attribute('langfuse.observation.metadata.prompt_sha256', identity['x-hr-prompt-sha256']),
              attribute('langfuse.observation.metadata.lambda_version', identity.get('x-hr-lambda-version', 'unknown'))]
    # Model calls link to the exact prompt version in Langfuse's Prompts page.
    if kind == 'generation' and prompt_version.isdigit():
        attrs.append(attribute('langfuse.observation.prompt.name', 'hr-policy-system'))
        attrs.append({'key': 'langfuse.observation.prompt.version', 'value': {'intValue': prompt_version}})
    return attrs


def _finish(opened, status):
    """Close the span and print it as one JSON line."""
    _current.reset(opened['token'])
    attrs = _base_attributes(opened['kind'])
    for key, value in opened['state']['extra'].items():
        attrs.append(attribute(key, value))
    record = {'traceId': opened['state']['trace'], 'spanId': opened['state']['id'], 'name': opened['name'],
              'kind': 1, 'startTimeUnixNano': str(opened['start']), 'endTimeUnixNano': str(time.time_ns()),
              'attributes': attrs, 'status': status}
    if opened['parent']:
        record['parentSpanId'] = opened['parent']['id']
    print(json.dumps({'trace_record': 'hr_otel', 'hr_otel': True, 'span': record}, separators=(',', ':')))


@contextmanager
def span(name, kind='span'):
    """Record the block inside `with span(...)` as one step. Errors are recorded by type only."""
    if not tracing_enabled():
        yield
        return
    opened = _start(name, kind)
    status = {'code': 1}
    try:
        yield
    except Exception as error:
        # The error message can contain private data, so only its type is kept.
        status = {'code': 2, 'message': type(error).__name__}
        opened['state']['extra']['langfuse.observation.level'] = 'ERROR'
        raise
    finally:
        _finish(opened, status)


def observed(name, kind='span'):
    """Record each call of the decorated function as one step.

    The function's arguments and return value are never recorded automatically.
    The function itself decides what to record, with update_current_span().
    """
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            if not tracing_enabled():
                return fn(*args, **kwargs)
            with span(name, kind):
                return fn(*args, **kwargs)
        return wrapped
    return decorate
