"""Tests for the release package, the evaluation report and the inline judge.

The `checks` job runs these on every push and pull request, before anything is
deployed. They need no AWS access and make no model calls: every model call is
replaced with a fake answer (a "mock"), so the tests are fast and repeatable.

Run them yourself from the project folder:

    python -m unittest discover -s ci/tests -v
"""
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

# The code under test lives in ci/ and lambda/, so add both folders to the import path.
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ci'))
sys.path.insert(0, str(ROOT / 'lambda'))

from release import validate_prompt, package             # ci/release.py: checks and packages the backend
from report import wilson, summarize                     # ci/report.py: writes the release report
from app.inline_judge import validate_answer, SAFE_REPLY, assess   # lambda/app/inline_judge.py

# validate_answer() reads these settings. The model name is fake because no model is called.
JUDGE_ON = {'INLINE_JUDGE_ENABLED': 'true', 'INLINE_JUDGE_MODEL': 'test'}

# validate_answer(question, draft, messages, employee, client) needs a model client.
# Every model call is mocked, so a plain object() stands in for the Bedrock client.
FAKE_CLIENT = object()


class ReleaseTests(unittest.TestCase):

    # ---- The system prompt (lambda/app/prompts/hr_system.txt) ----

    def test_real_prompt_parses(self):
        # The real prompt must contain exactly the three allowed variables and fill in cleanly.
        # validate_prompt() raises an error if not, which fails this test.
        prompt_text = (ROOT / 'lambda/app/prompts/hr_system.txt').read_text()
        validate_prompt(prompt_text)

    def test_extra_variable_rejected(self):
        # A prompt that asks for an unknown variable, such as {secret}, must be refused.
        # Otherwise a prompt edit could pull data into the model that nobody approved.
        with self.assertRaises(ValueError):
            validate_prompt('{full_name} {job_title} {department} {secret}')

    # ---- The deployment package (build/function.zip) ----

    def test_package_includes_the_approved_policy_manifest(self):
        # The zip must carry the same policy list (manifest.json) that was reviewed,
        # so the deployed app only trusts approved policy documents.
        with tempfile.TemporaryDirectory() as temporary:
            package(output_dir=temporary)
            with zipfile.ZipFile(pathlib.Path(temporary) / 'function.zip') as archive:
                packaged = json.loads(archive.read('data/policy_manifest.json'))
        approved = json.loads((ROOT / 'lambda/data/documents/manifest.json').read_text())
        self.assertEqual(packaged, approved)

    def test_package_contains_exact_prompt_and_identity(self):
        # The release record (release.json) stores a fingerprint (SHA-256 hash) of the prompt.
        # This proves the prompt inside the zip is exactly the one the record describes,
        # so a test result can always be traced to the exact prompt that produced it.
        with tempfile.TemporaryDirectory() as temporary:
            manifest = package(output_dir=temporary)
            with zipfile.ZipFile(pathlib.Path(temporary) / 'function.zip') as archive:
                prompt = archive.read('app/prompts/hr_system.txt')
                identity = json.loads(archive.read('app/release.json'))
        self.assertEqual(hashlib.sha256(prompt).hexdigest(), manifest['prompt_sha256'])
        self.assertEqual(identity['dataset_sha256'], manifest['dataset_sha256'])

    # ---- The inline judge: a second model call that checks each answer before the employee sees it ----

    def test_judge_retries_once_and_falls_back(self):
        # The judge fails the draft twice. The app should:
        #   1. ask the judge (fail), 2. rewrite the draft once, 3. ask the judge again (fail),
        #   4. give up and send the safe reply instead of an unverified answer.
        always_fail = {'pass': False, 'reason': 'unsupported'}
        with patch.dict('os.environ', JUDGE_ON), \
             patch('app.inline_judge.assess', return_value=always_fail) as judge, \
             patch('app.inline_judge.converse_text', return_value='revised') as revision:
            answer = validate_answer('q', 'draft', [], {}, FAKE_CLIENT)
            self.assertEqual(answer, SAFE_REPLY)
            self.assertEqual(judge.call_count, 2)       # judged twice
            self.assertEqual(revision.call_count, 1)    # rewritten once

    def test_judge_error_withholds_draft(self):
        # If the judge itself crashes, the draft was never checked, so it must not be sent.
        with patch.dict('os.environ', JUDGE_ON), \
             patch('app.inline_judge.assess', side_effect=ValueError):
            answer = validate_answer('q', 'draft', [], {}, FAKE_CLIENT)
            self.assertEqual(answer, SAFE_REPLY)

    def test_judge_pass_returns_draft(self):
        # When the judge approves, the employee gets the draft unchanged.
        with patch.dict('os.environ', JUDGE_ON), \
             patch('app.inline_judge.assess', return_value={'pass': True, 'reason': 'supported'}):
            answer = validate_answer('q', 'draft', [], {}, FAKE_CLIENT)
            self.assertEqual(answer, 'draft')

    def test_judge_does_not_start_with_insufficient_response_budget(self):
        # With only 2 seconds left before the request times out, there is no time to judge.
        # The app sends the safe reply at once instead of starting a call it cannot finish.
        with patch.dict('os.environ', JUDGE_ON), \
             patch('app.inline_judge.assess') as judge:
            answer = validate_answer('q', 'draft', [], {}, FAKE_CLIENT, remaining_seconds=2)
            self.assertEqual(answer, SAFE_REPLY)
            judge.assert_not_called()

    # The next three tests feed assess() different raw judge replies.
    # Models sometimes wrap JSON in a code fence or add <thinking> tags; those are formatting, so accept them.
    # Any extra free text around the JSON is refused, because the verdict must be unambiguous.

    def test_judge_accepts_complete_fenced_json(self):
        raw = '<thinking>provider formatting</thinking>\n```json\n{"pass":true,"reason":"supported"}\n```'
        with patch('app.inline_judge.converse_text', return_value=raw):
            verdict = assess(FAKE_CLIENT, 'test', 'q', 'a', [])
            self.assertTrue(verdict['pass'])

    def test_judge_accepts_single_line_fenced_json(self):
        raw = '```json {"pass":true,"reason":"supported"}```'
        with patch('app.inline_judge.converse_text', return_value=raw):
            verdict = assess(FAKE_CLIENT, 'test', 'q', 'a', [])
            self.assertTrue(verdict['pass'])

    def test_judge_rejects_extra_unstructured_verdict_text(self):
        raw = 'Probably fine {"pass":true,"reason":"supported"}'
        with patch('app.inline_judge.converse_text', return_value=raw), self.assertRaises(ValueError):
            assess(FAKE_CLIENT, 'test', 'q', 'a', [])

    # ---- The release report (ci/report.py) ----

    def test_repeated_runs_do_not_inflate_sample_size(self):
        # The pipeline asks each question twice. Two passing runs of one case count as 1 case, not 2.
        # Counting runs instead of cases would make the result look more certain than it is.
        two_runs_of_one_case = [{'case_id': 'a', 'passed': True}] * 2
        report = summarize({'expected': 2, 'records': two_runs_of_one_case})
        self.assertIn('1/1', report)

    def test_missing_report_fails(self):
        # Two answers were expected and none arrived. That must be an error, never a pass.
        with self.assertRaises(ValueError):
            summarize({'expected': 2, 'records': []})

    def test_interval_not_zero_error_guarantee(self):
        # Even when all 43 cases pass, the lower end of the 95% confidence range stays below 100%.
        # A small test set cannot prove the app never makes a mistake, and the report must not claim it.
        lower_bound, _upper_bound = wilson(43, 43)
        self.assertLess(lower_bound, 1)


if __name__ == '__main__':
    unittest.main()
