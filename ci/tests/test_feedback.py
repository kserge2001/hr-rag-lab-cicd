"""Tests for user feedback: telemetry.record_score() and the exporter that sends scores to Langfuse.

A person's thumbs up or down becomes a "user_feedback" score on the answer's trace.
No real Langfuse or AWS is used: the exporter's network call and secret are replaced with fakes.
"""
import base64
import contextlib
import gzip
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'lambda'))

from app import trace_exporter
from app.telemetry import record_score

TRACE_ID = 'a' * 32
TRACING_ON = {'HR_TRACING_ENABLED': 'true'}
CONTENT_ON = {'HR_TRACING_ENABLED': 'true', 'HR_TRACE_SYNTHETIC_CONTENT': 'true', 'LAB_ENVIRONMENT': 'staging'}


def printed_score(environment, comment):
    captured = io.StringIO()
    with patch.dict(os.environ, environment), contextlib.redirect_stdout(captured):
        record_score(TRACE_ID, 'user_feedback', 0, comment)
    return json.loads(captured.getvalue())['score']


class RecordScoreTests(unittest.TestCase):

    def test_a_vote_has_a_fixed_id_so_a_new_vote_replaces_the_old_one(self):
        score = printed_score(TRACING_ON, None)
        self.assertEqual(score['id'], TRACE_ID + '-user_feedback')
        self.assertEqual(score['traceId'], TRACE_ID)
        self.assertEqual((score['value'], score['dataType']), (0, 'BOOLEAN'))

    def test_a_comment_is_kept_only_with_the_content_switch_and_is_redacted(self):
        # Switch off: the vote is recorded, the comment is not.
        self.assertNotIn('comment', printed_score(TRACING_ON, 'wrong figure'))
        # Switch on: the comment is recorded, with any key replaced.
        score = printed_score(CONTENT_ON, 'wrong figure sk-lf-example-secret')
        self.assertIn('wrong figure', score['comment'])
        self.assertNotIn('sk-lf-example-secret', score['comment'])


def log_batch(*records):
    """A CloudWatch Logs subscription event, the way the exporter Lambda receives it."""
    data = {'messageType': 'DATA_MESSAGE',
            'logEvents': [{'message': json.dumps(record)} for record in records]}
    return {'awslogs': {'data': base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()}}


class ExporterTests(unittest.TestCase):

    def test_scores_go_to_the_langfuse_scores_api(self):
        score = {'id': TRACE_ID + '-user_feedback', 'traceId': TRACE_ID, 'name': 'user_feedback',
                 'value': 0, 'dataType': 'BOOLEAN', 'comment': 'wrong figure', 'extra': 'dropped'}
        secrets = MagicMock()
        secrets.get_secret_value.return_value = {'SecretString': json.dumps({'public_key': 'pk', 'secret_key': 'sk'})}
        sent = []

        def fake_urlopen(request, timeout):
            sent.append((request.full_url, json.loads(request.data)))
            return MagicMock()

        environment = {'LANGFUSE_SECRET_ARN': 'arn:secret', 'LANGFUSE_BASE_URL': 'https://us.cloud.langfuse.com'}
        with patch.dict(os.environ, environment), \
             patch('app.trace_exporter.boto3.client', return_value=secrets), \
             patch('app.trace_exporter.urllib.request.urlopen', side_effect=fake_urlopen):
            result = trace_exporter.handler(log_batch({'trace_record': 'hr_score', 'hr_score': True, 'score': score}), None)

        self.assertEqual(result, {'exported': 0, 'scores': 1})
        url, body = sent[0]
        self.assertEqual(url, 'https://us.cloud.langfuse.com/api/public/scores')
        self.assertEqual(body['value'], 0)
        self.assertNotIn('extra', body)   # only the fields Langfuse expects are sent


if __name__ == '__main__':
    unittest.main()
