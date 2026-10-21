"""Tests for ci/rollback_lab.py, which moves the staging `approved` alias back one release.

A fake Lambda client stands in for AWS, so no real alias moves. Each test sets up the
alias the way ci/promote_lab.py leaves it, runs the rollback, and checks what happened.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rollback_lab


class FakeLambda:
    """Just enough of boto3's Lambda client for the rollback."""

    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, alias=None, staging_versions=('7', '8')):
        self.alias = alias                      # None means no `approved` alias exists
        self.staging_versions = staging_versions
        self.updates = []

    def get_alias(self, FunctionName, Name):
        if self.alias is None:
            raise self.exceptions.ResourceNotFoundException()
        return self.alias

    def get_function_configuration(self, FunctionName, Qualifier):
        environment = 'staging' if Qualifier in self.staging_versions else 'production'
        return {'Environment': {'Variables': {'LAB_ENVIRONMENT': environment}}}

    def update_alias(self, **arguments):
        self.updates.append(arguments)


def promoted(current, previous):
    """An `approved` alias as ci/promote_lab.py leaves it after a promotion."""
    return {'FunctionVersion': current, 'RevisionId': 'rev-1',
            'Description': f'Lab approved release pointer; previous approved version: {previous}'}


class RollbackTests(unittest.TestCase):

    def test_moves_approved_back_to_the_recorded_version(self):
        client = FakeLambda(alias=promoted(current='8', previous='7'))
        moved = rollback_lab.roll_back(client, 'hr-chat')
        self.assertEqual(moved, ('8', '7'))
        update = client.updates[0]
        self.assertEqual(update['FunctionVersion'], '7')
        self.assertEqual(update['RevisionId'], 'rev-1')   # refuses if the alias moved meanwhile
        # The new description records 8, so running the rollback again swaps back.
        self.assertIn('previous approved version: 8', update['Description'])

    def test_stops_when_there_is_no_approved_alias(self):
        client = FakeLambda(alias=None)
        with self.assertRaises(SystemExit):
            rollback_lab.roll_back(client, 'hr-chat')
        self.assertEqual(client.updates, [])

    def test_stops_after_the_first_promotion(self):
        # The first passing run creates the alias, so there is no earlier version yet.
        alias = {'FunctionVersion': '7', 'RevisionId': 'rev-1',
                 'Description': 'Lab approved release pointer; previous approved version: none'}
        client = FakeLambda(alias=alias)
        with self.assertRaises(SystemExit):
            rollback_lab.roll_back(client, 'not-a-function-with-a-saved-file')
        self.assertEqual(client.updates, [])

    def test_refuses_a_version_outside_staging(self):
        client = FakeLambda(alias=promoted(current='8', previous='3'), staging_versions=('8',))
        with self.assertRaises(SystemExit):
            rollback_lab.roll_back(client, 'hr-chat')
        self.assertEqual(client.updates, [])


if __name__ == '__main__':
    unittest.main()
