"""Delete everything this lab created in AWS, in the right order, so billing stops.

    python scripts/tear-down.py                  deletes the stack and the pipeline's role
    python scripts/tear-down.py --release-names  also frees the two secret names at once
    python scripts/tear-down.py --dry-run        shows what it would delete, changes nothing

What it does, in order:
  1. Reads the project's names from Terraform, while Terraform can still print them.
  2. Runs `terraform destroy`. Terraform lists everything it will delete and waits for `yes`.
  3. Deletes the pipeline's IAM role from document 05 (Terraform did not create it).
  4. With --release-names: force-deletes the two secrets whose names AWS keeps reserved
     for 30 days, so you can set up again with the same project_name straight away.
  5. Deletes demo-sign-in.txt, .env.langfuse and build/github-settings.txt from this folder.
  6. Lists any AWS resource still tagged with your project. Expected: none.

The GitHub identity provider is kept: other projects in the account may use it.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
ROLE_POLICY = 'HRLabOnly'
LOCAL_FILES = ['demo-sign-in.txt', '.env.langfuse', 'build/github-settings.txt']


SAVED_NAMES = ROOT / 'build' / 'tear-down-names.json'


def project_names():
    """The names this script needs, read from Terraform and saved in case destroy stops halfway.

    A half-finished destroy can leave Terraform unable to print them, so a rerun uses the saved copy.
    """
    command = ['terraform', '-chdir=infra/terraform', 'output', '-json']
    result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
    outputs = json.loads(result.stdout or '{}')
    if 'candidate_function' in outputs:
        names = {
            'project': outputs['candidate_function']['value'].removesuffix('-chat'),
            'reserved_secrets': [outputs['evaluation_users_secret_arn']['value'],
                                 outputs['langfuse_secret_arn']['value']],
        }
        SAVED_NAMES.parent.mkdir(exist_ok=True)
        SAVED_NAMES.write_text(json.dumps(names, indent=2), encoding='utf-8')
        return names
    if SAVED_NAMES.exists():
        print(f'Terraform printed no names, so using the ones saved in build/{SAVED_NAMES.name}')
        return json.loads(SAVED_NAMES.read_text(encoding='utf-8'))
    sys.exit('Terraform has no outputs here. Run this from the folder that ran setup.')


def delete_role(iam, role_name):
    """Delete the pipeline's role. AWS requires its inline policy to go first."""
    try:
        iam.delete_role_policy(RoleName=role_name, PolicyName=ROLE_POLICY)
    except iam.exceptions.NoSuchEntityException:
        pass
    try:
        iam.delete_role(RoleName=role_name)
        print(f'Deleted the role {role_name}')
    except iam.exceptions.NoSuchEntityException:
        print(f'The role {role_name} does not exist: nothing to delete')


def resources_left(project):
    tagging = boto3.client('resourcegroupstaggingapi')
    pages = tagging.get_paginator('get_resources').paginate(TagFilters=[{'Key': 'Project', 'Values': [project]}])
    return [item['ResourceARN'] for page in pages for item in page['ResourceTagMappingList']]


def main():
    parser = argparse.ArgumentParser(description='Delete everything this lab created in AWS.')
    parser.add_argument('--release-names', action='store_true', help='force-delete the two reserved secrets')
    parser.add_argument('--dry-run', action='store_true', help='show what would be deleted, change nothing')
    args = parser.parse_args()

    # Step 1: names, read before Terraform forgets them.
    names = project_names()
    project = names['project']
    role_name = project + '-github-ci'
    reserved_secrets = names['reserved_secrets']
    print(f'Project: {project}')
    print(f'Pipeline role: {role_name}')

    if args.dry_run:
        print('\nDry run. This would:')
        print('  run terraform destroy (it lists every resource and asks for yes)')
        print(f'  delete the role {role_name} and its inline policy {ROLE_POLICY}')
        if args.release_names:
            for secret in reserved_secrets:
                print(f'  force-delete {secret}')
        print(f'  delete {", ".join(LOCAL_FILES)}')
        left = resources_left(project)
        print(f'\n{len(left)} AWS resources are tagged with Project={project} right now.')
        return

    # Step 2: Terraform deletes the stack. Its own prompt asks for yes.
    print('\nRunning terraform destroy. Check that every name carries your project_name, then type yes.\n')
    destroy = subprocess.run(['terraform', '-chdir=infra/terraform', 'destroy'], cwd=ROOT)
    if destroy.returncode != 0:
        sys.exit('terraform destroy did not finish. Run this script again: it continues from what is left.')

    # Step 3: the role from document 05.
    delete_role(boto3.client('iam'), role_name)

    # Step 4: optional, free the secret names now instead of in 30 days.
    if args.release_names:
        secrets = boto3.client('secretsmanager')
        for secret in reserved_secrets:
            try:
                secrets.delete_secret(SecretId=secret, ForceDeleteWithoutRecovery=True)
                print(f'Released {secret}')
            except secrets.exceptions.ResourceNotFoundException:
                print(f'Already gone: {secret}')

    # Step 5: local files that held keys and passwords for the deleted stack.
    for name in LOCAL_FILES:
        path = ROOT / name
        if path.exists():
            path.unlink()
            print(f'Deleted {name}')

    # Step 6: anything still tagged with the project?
    left = resources_left(project)
    if left:
        print(f'\n{len(left)} resources are still tagged Project={project}. The tag index can lag behind deletes,')
        print('so open each one in the AWS console to check whether it still exists:')
        for arn in left:
            print(f'  {arn}')
    else:
        print(f'\nNothing tagged Project={project} is left in AWS.')


if __name__ == '__main__':
    main()
