"""Tests for the one release number: VERSION, the version check, deployment and tagging.

No AWS, Git server or Langfuse is used: the Lambda client is a fake, and the tag script is
tested on the checks that run before it creates any tag.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_version
import deploy_candidate
import release
import tag_release
from versioning import parse, read_version, tag_name


class VersionFormatTests(unittest.TestCase):

    def test_the_repository_version_is_valid(self):
        version = read_version()
        self.assertEqual(len(parse(version)), 3)

    def test_only_major_minor_patch_is_accepted(self):
        self.assertEqual(parse('1.10.3'), (1, 10, 3))
        for bad in ('1.0', 'v1.0.0', '1.0.0-beta', '01.0.0', 'one'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse(bad)

    def test_a_release_tag_is_v_and_the_version(self):
        self.assertEqual(tag_name('1.0.3'), 'v1.0.3')


class VersionCheckTests(unittest.TestCase):
    # check() returns a problem as text, or None when the version may be released.

    def test_the_first_release_needs_no_bump(self):
        self.assertIsNone(check_version.check('1.0.0', []))

    def test_a_newer_version_passes(self):
        self.assertIsNone(check_version.check('1.0.2', ['1.0.0', '1.0.1']))
        self.assertIsNone(check_version.check('1.1.0', ['1.0.9']))

    def test_a_version_already_released_fails(self):
        self.assertIn('already released', check_version.check('1.0.1', ['1.0.0', '1.0.1']))

    def test_an_older_version_fails(self):
        # Numbers compare as numbers: 1.0.10 is newer than 1.0.9.
        self.assertIn('not newer', check_version.check('1.0.9', ['1.0.10']))


class ReleaseStampTests(unittest.TestCase):

    def test_the_package_records_the_release_number(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = release.package(output_dir=temporary)
        self.assertEqual(manifest['version'], read_version())

    def test_the_lambda_version_and_alias_name_the_release(self):
        client = MagicMock()
        client.get_function_configuration.return_value = {
            'RevisionId': 'r1', 'Environment': {'Variables': {'LAB_ENVIRONMENT': 'staging'}}}
        client.update_function_code.return_value = {'CodeSha256': 'code-hash'}
        client.publish_version.return_value = {'Version': '22'}
        client.get_alias.return_value = {'RevisionId': 'a1'}
        manifest = {'version': '1.0.3', 'commit': 'a21bc86ffffffff', 'artifact_sha256': release.digest(b'zip')}

        with patch.dict(os.environ, {'GITHUB_SHA': 'a21bc86ffffffff'}):
            version = deploy_candidate.deploy(client, 'hr-chat', b'zip', manifest)

        self.assertEqual(version, '22')
        published = client.publish_version.call_args.kwargs
        self.assertEqual(published['Description'], 'v1.0.3 (commit a21bc86)')
        self.assertEqual(published['CodeSha256'], 'code-hash')   # exactly the code just uploaded
        self.assertIn('v1.0.3', client.update_alias.call_args.kwargs['Description'])


class TagReleaseTests(unittest.TestCase):
    # The tag script must refuse before creating any tag when the evidence does not match.

    def run_tagging(self, commit_in_evidence, report_line):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / 'evidence'
            evidence.mkdir()
            (evidence / 'release.json').write_text(json.dumps({'version': '1.0.3', 'commit': commit_in_evidence}))
            (evidence / 'report.md').write_text(report_line)
            with patch.object(tag_release, 'EVIDENCE', evidence), \
                 patch.dict(os.environ, {'EXPECTED_COMMIT': 'merged-commit'}), \
                 patch.object(tag_release, 'git') as git:
                with self.assertRaises(SystemExit):
                    tag_release.main()
        return git

    def test_a_failed_release_gate_is_never_tagged(self):
        git = self.run_tagging('merged-commit', 'Release gate: FAIL.')
        git.assert_not_called()

    def test_a_different_commit_is_never_tagged(self):
        git = self.run_tagging('other-commit', 'Release gate: PASS.')
        git.assert_not_called()


if __name__ == '__main__':
    unittest.main()
