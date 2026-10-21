"""Tests for the app's model gateway client, lambda/app/model_gateway.py.

The app never calls Bedrock models directly. Every model call goes to the LiteLLM
gateway, which enforces the approved models, budget and rate limits. GatewayClient
speaks to the gateway on the app's behalf.

No real gateway is used: requests.post is replaced with a fake ("mock") that returns
a prepared response. The tests then check what the client sent and how it handled
the answer.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# The code under test lives in lambda/app, so add lambda/ to the import path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'lambda'))

from app.model_gateway import GatewayClient, GatewayPolicyDenied, GatewayUnavailable, gateway_context

# The settings the Lambda normally gets from Terraform. Only these three models are approved.
ENV = {
    'LLM_GATEWAY_URL': 'http://gateway:4000',
    'CHAT_MODEL': 'primary',
    'INLINE_JUDGE_MODEL': 'judge',
    'FALLBACK_CHAT_MODEL': 'fallback',
}

# Shortcuts for the three fakes most tests need.
KEY_PATCH = 'app.model_gateway.gateway_key'      # normally reads the key from Secrets Manager
POST_PATCH = 'app.model_gateway.requests.post'   # normally sends the HTTP request


def fake_response(status, model='primary', headers=None):
    """A fake gateway reply with the given HTTP status, in LiteLLM's response format."""
    response = Mock(status_code=status, headers=headers or {})
    response.json.return_value = {
        'model': model,
        'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 1},
    }
    return response


def send(response, purpose='hr-chat', **request):
    """Make one call through GatewayClient with a fake reply.

    Returns what the client sent to the gateway (the JSON body) and what it returned to the app.
    """
    with patch.dict(os.environ, ENV), \
         patch(KEY_PATCH, return_value='sk-test'), \
         patch(POST_PATCH, return_value=response) as post:
        result = GatewayClient(purpose=purpose).converse(modelId='primary', messages=[], **request)
    sent_body = post.call_args.kwargs['json']
    return sent_body, result


class GatewayTests(unittest.TestCase):

    # ---- Translating requests and answers ----

    def test_converse_preserves_native_tools_and_response(self):
        # The app writes requests in Bedrock's format. The client must translate them
        # into the gateway's format, send them to /chat/completions, and translate the
        # answer back, so the rest of the app never notices the gateway is there.
        request = {
            'messages': [{'role': 'user', 'content': [{'text': 'question'}]}],
            'toolConfig': {'tools': []},
            'inferenceConfig': {'maxTokens': 100},
        }
        with patch.dict(os.environ, ENV), \
             patch(KEY_PATCH, return_value='sk-test'), \
             patch(POST_PATCH, return_value=fake_response(200)) as post:
            result = GatewayClient().converse(modelId='primary', **request)

            sent_url = post.call_args.args[0]
            sent_body = post.call_args.kwargs['json']
            self.assertEqual(sent_body['messages'], [{'role': 'user', 'content': 'question'}])
            self.assertTrue(sent_url.endswith('/chat/completions'))
            self.assertEqual(result['output']['message']['role'], 'assistant')

    # ---- What happens when the gateway says no ----

    def test_budget_rate_and_auth_denials_are_not_transient(self):
        # 401 (bad key), 403 (not allowed) and 429 (over budget or rate limit) are decisions,
        # not glitches. A retry would get the same answer, so the client must stop after
        # one try and raise GatewayPolicyDenied.
        for status in (401, 403, 429):
            with self.subTest(status=status), \
                 patch.dict(os.environ, ENV), \
                 patch(KEY_PATCH, return_value='sk-test'), \
                 patch(POST_PATCH, return_value=fake_response(status)) as post:
                with self.assertRaises(GatewayPolicyDenied):
                    GatewayClient().converse(modelId='judge', messages=[])
                self.assertEqual(post.call_count, 1)

    def test_transient_failure_is_distinct_from_policy_denial(self):
        # 503 means the gateway or the model is briefly unavailable. That is a different
        # error, GatewayUnavailable, so the app can tell "try again later" from "not allowed".
        with patch.dict(os.environ, ENV), \
             patch(KEY_PATCH, return_value='sk-test'), \
             patch(POST_PATCH, return_value=fake_response(503)):
            with self.assertRaises(GatewayUnavailable):
                GatewayClient().converse(modelId='primary', messages=[])

    def test_unknown_model_and_oversize_output_are_rejected_before_network(self):
        # The client also checks the request itself, before sending anything:
        # an unapproved model, or an answer longer than the allowed token limit, is refused.
        with patch.dict(os.environ, ENV), patch(POST_PATCH) as post:
            with self.assertRaises(GatewayPolicyDenied):
                GatewayClient().converse(modelId='unapproved', messages=[])
            with self.assertRaises(GatewayPolicyDenied):
                GatewayClient().converse(modelId='primary', inferenceConfig={'maxTokens': 9999})
            post.assert_not_called()

    def test_no_endpoint_means_no_direct_bedrock_fallback(self):
        # With no gateway address configured, the client must fail to start.
        # It must never quietly call Bedrock directly, which would skip every gateway limit.
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KeyError):
                GatewayClient()

    # ---- Labels the gateway uses for its logs, limits and cache ----

    def test_requests_carry_purpose_tag_and_no_user_outside_a_conversation(self):
        # Every call is tagged with its purpose ('hr-chat', 'inline-judge', ...) so spend
        # can be split by purpose in the gateway logs. Outside a conversation there is no
        # user to name and no cache to use.
        sent_body, _result = send(fake_response(200), purpose='inline-judge')
        self.assertEqual(sent_body['metadata'], {'tags': ['inline-judge']})
        self.assertNotIn('user', sent_body)
        self.assertNotIn('cache', sent_body)

    def test_conversation_adds_opaque_user_and_cache_only_when_allowed(self):
        # Inside a conversation, the call carries an opaque user ID (a hash, never an email),
        # so per-person limits work. The cache is used only when the conversation allows it.
        with gateway_context(user='hr-user-abc', use_cache=True):
            sent_body, _result = send(fake_response(200))
        self.assertEqual(sent_body['user'], 'hr-user-abc')
        self.assertEqual(sent_body['cache'], {'use-cache': True})

        with gateway_context(user='hr-user-qa', use_cache=False):
            sent_body, _result = send(fake_response(200))
        self.assertNotIn('cache', sent_body)

    # ---- Reporting which model actually answered ----

    def test_gateway_fallback_reports_the_model_that_answered(self):
        # When the main model fails, the gateway answers with the fallback model and says so
        # in its response headers. The client must pass that on, so traces show the real model.
        headers = {
            'x-litellm-model-id': 'fallback-us-west-2',
            'x-litellm-attempted-fallbacks': '1',
            'x-litellm-response-cost': '0.00012',
        }
        _sent_body, result = send(fake_response(200, model='bedrock/fallback', headers=headers))
        self.assertEqual(result['gateway'], {
            'model': 'fallback',
            'deployment': 'fallback-us-west-2',
            'fallback': True,
            'cost_usd': '0.00012',
        })

    def test_direct_answer_is_not_reported_as_fallback(self):
        # Zero attempted fallbacks means the main model answered.
        headers = {'x-litellm-model-id': 'chat-us-east-1', 'x-litellm-attempted-fallbacks': '0'}
        _sent_body, result = send(fake_response(200, headers=headers))
        self.assertEqual(result['gateway'], {
            'model': 'primary',
            'deployment': 'chat-us-east-1',
            'fallback': False,
            'cost_usd': '',
        })


if __name__ == '__main__':
    unittest.main()
