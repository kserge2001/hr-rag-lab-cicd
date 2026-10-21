"""Move the staging `approved` alias back to the version it pointed to before the last promotion.

    python ci/rollback_lab.py

ci/promote_lab.py writes the version it replaced into the alias description, for example
"Lab approved release pointer; previous approved version: 7". This script reads that number
from AWS, checks it, and moves `approved` back. It never deletes anything.

Running it twice in a row swaps back again, because each rollback records the version it left.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
RECORD = re.compile(r'previous approved version: (\d+)')
RELEASE = re.compile(r'v\d+\.\d+\.\d+')   # a release name such as v1.0.3, in a description


def release_of(description):
    """The release named in a Lambda description, or 'an unnamed release' for older versions."""
    match = RELEASE.search(description or '')
    if match:
        return match.group(0)
    return 'an unnamed release'


def staging_function():
    """The chat Lambda's name: from the pipeline's variable, or from Terraform on your computer."""
    name = os.environ.get('HR_STAGING_FUNCTION')
    if name:
        return name
    command = ['terraform', '-chdir=infra/terraform', 'output', '-raw', 'candidate_function']
    return subprocess.check_output(command, text=True, cwd=ROOT).strip()


def previous_version(alias, function):
    """The version recorded before the last promotion, or None if there is no record."""
    match = RECORD.search(alias.get('Description', ''))
    if match:
        return match.group(1)
    # Aliases promoted before the description record existed: use the file the pipeline saved.
    saved = ROOT / 'build' / 'previous-approved.json'
    if saved.exists():
        record = json.loads(saved.read_text())
        if record.get('function') == function and str(record.get('version', '')).isdigit():
            return str(record['version'])
    return None


def roll_back(client, function):
    """Move `approved` to the recorded previous version. Returns (from_version, to_version)."""
    try:
        alias = client.get_alias(FunctionName=function, Name='approved')
    except client.exceptions.ResourceNotFoundException:
        sys.exit('There is no approved version yet. A passing run on main creates it.')

    current = alias['FunctionVersion']
    target = previous_version(alias, function)
    if target is None:
        sys.exit('No earlier approved version is recorded. Roll back after a second passing run.')
    if target == current:
        sys.exit(f'approved already points to version {current}. Nothing to roll back.')

    # Only restore a version of this isolated staging function.
    config = client.get_function_configuration(FunctionName=function, Qualifier=target)
    if config.get('Environment', {}).get('Variables', {}).get('LAB_ENVIRONMENT') != 'staging':
        sys.exit(f'Version {target} is not marked as isolated staging. Not restoring it.')

    # The version's own description names its release, such as "v1.0.2 (commit a21bc86)".
    target_release = release_of(config.get('Description'))
    client.update_alias(
        FunctionName=function,
        Name='approved',
        FunctionVersion=target,
        RevisionId=alias['RevisionId'],   # fails safely if someone moved the alias meanwhile
        Description=f'Lab approved release pointer; release {target_release}; previous approved version: {current}',
    )
    print(f'approved now points to {target_release}; it pointed to {release_of(alias.get("Description"))}.')
    return current, target


def main():
    function = staging_function()
    moved_from, moved_to = roll_back(boto3.client('lambda'), function)
    print(f'Rolled back {function}: approved moved from version {moved_from} to version {moved_to}.')
    print('The next passing run on main moves it forward again.')


if __name__ == '__main__':
    main()
