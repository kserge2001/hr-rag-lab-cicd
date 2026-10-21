"""Check that this change has a new release number. Runs in the checks job.

    python ci/check_version.py

Every change that reaches main becomes a release candidate with the number in VERSION.
That number must be newer than every release already tagged, so a number is never reused:
bump the patch number for a fix, the minor number for a feature.
"""
import sys

from versioning import parse, read_version, released_versions, tag_name


def check(version, released):
    """Return a problem as text, or None when the version is new."""
    if not released:
        return None
    newest = max(released, key=parse)
    if version in released:
        return f'{tag_name(version)} is already released. Bump VERSION, for example to the next patch number.'
    if parse(version) <= parse(newest):
        return f'VERSION {version} is not newer than the latest release {tag_name(newest)}. Bump it.'
    return None


def main():
    version = read_version()
    released = released_versions()
    problem = check(version, released)
    if problem:
        sys.exit(problem)
    if released:
        newest = tag_name(max(released, key=parse))
        print(f'Release version {tag_name(version)} is new (latest release: {newest}).')
    else:
        print(f'Release version {tag_name(version)}: the first release.')


if __name__ == '__main__':
    main()
