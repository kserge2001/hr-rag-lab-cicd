"""Deploy the tested package to the staging chat Lambda, then move its candidate alias.

    python ci/deploy_candidate.py

1. Checks the package is the one release.py built, for the commit being released.
2. Uploads the code, then publishes it as a new numbered Lambda version. The version's
   description names the release, such as "v1.0.3 (commit a21bc86)", so the Lambda
   console shows which release each version is.
3. Moves the candidate alias to that version, so the staging app and the tests use it.
"""
import json
import os
from pathlib import Path

import boto3

from release import digest


def deploy(client, function, artifact, manifest):
    if digest(artifact) != manifest['artifact_sha256']:
        raise ValueError('Candidate artifact hash mismatch')
    if manifest.get('commit') != os.environ['GITHUB_SHA']:
        raise ValueError('Candidate commit mismatch')
    config = client.get_function_configuration(FunctionName=function)
    if config.get('Environment', {}).get('Variables', {}).get('LAB_ENVIRONMENT') != 'staging':
        raise ValueError('Deployment requires explicitly marked isolated staging Lambda')

    # Upload the new code. RevisionId makes this fail safely if someone changed the function meanwhile.
    uploaded = client.update_function_code(FunctionName=function, ZipFile=artifact, RevisionId=config['RevisionId'])
    client.get_waiter('function_updated_v2').wait(FunctionName=function)

    # Publish it as a numbered version that names the release. CodeSha256 makes sure the
    # version holds exactly the code just uploaded.
    release_name = 'v' + manifest['version']
    published = client.publish_version(
        FunctionName=function,
        CodeSha256=uploaded['CodeSha256'],
        Description=f"{release_name} (commit {manifest['commit'][:7]})",
    )
    version = published['Version']

    alias = client.get_alias(FunctionName=function, Name='candidate')
    client.update_alias(FunctionName=function, Name='candidate', FunctionVersion=version,
                        RevisionId=alias['RevisionId'], Description=f'Release under test: {release_name}')
    return version


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / 'build/release.json').read_text())
    version = deploy(boto3.client('lambda'), os.environ['HR_STAGING_FUNCTION'],
                     (root / 'build/function.zip').read_bytes(), manifest)
    manifest['lambda_version'] = version
    (root / 'build/release.json').write_text(json.dumps(manifest, indent=2))
    print(f"Deployed release v{manifest['version']} as Lambda version {version}; candidate points to it.")
