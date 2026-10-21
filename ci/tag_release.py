"""Tag the commit that passed the release gate with its release number. Runs in the tag-release job.

    python ci/tag_release.py

It reads the evidence the candidate job saved, checks that this exact commit passed, and
then creates the annotated tag, such as v1.0.3, and pushes it to GitHub. A release that
failed the gate never gets a tag. A tag push does not start the pipeline again: the
workflow only runs for pushes to branches.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from versioning import parse, tag_name

EVIDENCE = Path('evidence')


def git(*arguments):
    return subprocess.run(['git', *arguments], check=True, text=True, capture_output=True).stdout.strip()


def main():
    release = json.loads((EVIDENCE / 'release.json').read_text())
    report = (EVIDENCE / 'report.md').read_text()
    commit = os.environ['EXPECTED_COMMIT']

    # Tag only the commit that was tested, and only when the gate passed.
    if release['commit'] != commit:
        sys.exit('The tested release is not the merged commit; nothing was tagged.')
    if 'Release gate: PASS.' not in report:
        sys.exit('The release gate did not pass; nothing was tagged.')
    parse(release['version'])
    tag = tag_name(release['version'])

    if git('tag', '--list', tag):
        sys.exit(f'{tag} already exists. Bump VERSION for the next release.')

    # An annotated tag records who tagged the commit, when, and why.
    git('-c', 'user.name=github-actions[bot]', '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
        'tag', '-a', tag, commit, '-m', f'Release {tag}: passed the release gate in staging')
    git('push', 'origin', tag)
    print(f'Tagged {commit[:7]} as {tag} and pushed the tag to GitHub.')


if __name__ == '__main__':
    main()
