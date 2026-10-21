"""The release version: one number, from the VERSION file, used everywhere.

The VERSION file at the top of the repository holds the release number, such as 1.0.3.
The pipeline stamps it on the package, the Langfuse prompt, the Lambda version, the traces
and the Git tag, so every one of them says which release it belongs to.

Semantic versioning: MAJOR.MINOR.PATCH.
  PATCH goes up for a fix                       1.0.3 -> 1.0.4
  MINOR goes up for a new feature               1.0.4 -> 1.1.0
  MAJOR goes up for a change that breaks things 1.1.0 -> 2.0.0
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERN = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)')


def parse(version):
    """'1.0.3' -> (1, 0, 3). Raises ValueError for anything that is not MAJOR.MINOR.PATCH."""
    match = PATTERN.fullmatch(version.strip())
    if match is None:
        raise ValueError(f'VERSION must look like 1.0.3, not {version.strip()!r}')
    return tuple(int(part) for part in match.groups())


def read_version():
    """The release number in the VERSION file, checked, such as '1.0.3'."""
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    parse(version)
    return version


def tag_name(version):
    """The Git tag for a release: 'v' and the version, such as 'v1.0.3'."""
    return 'v' + version


def released_versions():
    """Every version that already has a release tag in this repository, such as ['1.0.0', '1.0.1']."""
    output = subprocess.check_output(['git', 'tag', '--list', 'v*'], text=True, cwd=ROOT)
    versions = []
    for tag in output.split():
        try:
            parse(tag[1:])
        except ValueError:
            continue   # a tag that is not a release, such as "v-test"
        versions.append(tag[1:])
    return versions
