"""Give this repository's GitHub Actions pipeline its own AWS role, signed in through OIDC.

    python scripts/prepare-oidc.py             checks the policies, then creates or updates the role
    python scripts/prepare-oidc.py --dry-run   checks and shows the plan, changes nothing

What it does, in order:
  1. Finds your GitHub repository from the folder's git remote (what `git remote -v` shows).
  2. Writes two policy files to build/ so you can read them:
       github-oidc-trust.json        who may use the role: this repository, hr-staging environment only
       github-oidc-permissions.json  what the role may do: deploy and test this lab, nothing else
  3. Checks both policies: only this repository and environment, only this project's resources.
     Any problem stops the script before AWS changes.
  4. In AWS IAM:
       - the GitHub identity provider: reused if it exists, created if it does not
       - the role <project>-github-ci: created, or updated to match the two files
  5. Prints the role ARN, which document 06b saves in GitHub.

Running it again is safe. It never deletes anything, and never removes the provider's
other audiences, because other projects in the account may share the provider.
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_HOST = 'token.actions.githubusercontent.com'
AUDIENCE = 'sts.amazonaws.com'
POLICY_NAME = 'HRLabOnly'


# ---- Step 1: which repository? ----

def repository_from_git():
    """Return "owner/repo" for the GitHub repository this folder was cloned from.

    Reads the same address that `git remote -v` shows, so nothing has to be typed.
    Both address styles work:
        https://github.com/octo/hr-rag-lab.git
        git@github.com:octo/hr-rag-lab.git
    """
    address = subprocess.check_output(['git', 'remote', 'get-url', 'origin'], text=True, cwd=ROOT).strip()
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([^/]+)/(.+?)(?:\.git)?/?', address)
    if match is None:
        sys.exit(f'The origin remote is not a GitHub repository: {address}\nRerun with --repo owner/name.')
    full_name = f'{match.group(1)}/{match.group(2)}'
    print(f'Repository (from git remote origin): {full_name}')
    return full_name


def subject_prefix_from_github(full_name):
    """Work out the start of the "sub" claim in this repository's GitHub OIDC tokens.

    Repositories created after 15 July 2026 put fixed numeric IDs in the claim,
    for example repo:octo@123/hr-rag-lab@456. Older ones use names only, repo:octo/hr-rag-lab.
    The IDs and the creation date are public, so we read them from the GitHub API.
    """
    url = f'https://api.github.com/repos/{full_name}'
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            details = json.load(response)
    except (urllib.error.URLError, ValueError) as error:
        # Private repositories and network problems end up here, so ask instead.
        print(f'Could not read {url} ({error}).')
        return input('Exact OIDC subject prefix from GitHub settings: ').strip()

    owner = details['owner']['login']
    repo = details['name']
    created = details['created_at'][:10]
    if created > '2026-07-15':
        prefix = f"repo:{owner}@{details['owner']['id']}/{repo}@{details['id']}"
    else:
        prefix = f'repo:{owner}/{repo}'
    print(f'Repository created {created}, so its OIDC subject starts with: {prefix}')
    print('If you switched this repository to immutable subject claims yourself, rerun with --subject-prefix.')
    return prefix


def check_prefix_names_this_repository(prefix, full_name):
    """Refuse a prefix with wildcards, extra claims or another repository's name."""
    owner, repo = full_name.split('/')
    allowed = rf'repo:{re.escape(owner)}(?:@[0-9]+)?/{re.escape(repo)}(?:@[0-9]+)?'
    if not re.fullmatch(allowed, prefix):
        sys.exit('Subject prefix must identify exactly this repository, with no wildcards or custom claims')


# ---- Step 2: the two policy files ----

def terraform_outputs():
    text = subprocess.check_output(['terraform', '-chdir=infra/terraform', 'output', '-json'], text=True, cwd=ROOT)
    outputs = json.loads(text)
    return {name: item['value'] for name, item in outputs.items()}


def trust_policy(provider_arn, prefix):
    """Who may use the role: tokens from this repository's hr-staging environment, for AWS sign-in."""
    return {
        'Version': '2012-10-17',
        'Statement': [{
            'Effect': 'Allow',
            'Principal': {'Federated': provider_arn},
            'Action': 'sts:AssumeRoleWithWebIdentity',
            'Condition': {'StringEquals': {
                f'{PROVIDER_HOST}:aud': AUDIENCE,
                f'{PROVIDER_HOST}:sub': prefix + ':environment:hr-staging',
            }},
        }],
    }


def permissions_policy(outputs, account, region, partition):
    """What the role may do. Each statement names one of this lab's own resources."""
    chat = f"arn:{partition}:lambda:{region}:{account}:function:{outputs['candidate_function']}"

    def allow(actions, resources):
        return {'Effect': 'Allow', 'Action': actions, 'Resource': resources}

    return {
        'Version': '2012-10-17',
        'Statement': [
            # Deploy the chat Lambda: new code, a new version, and move the candidate/approved aliases.
            allow(['lambda:GetFunction', 'lambda:GetFunctionConfiguration', 'lambda:UpdateFunctionCode',
                   'lambda:GetAlias', 'lambda:UpdateAlias', 'lambda:CreateAlias', 'lambda:PublishVersion'],
                  [chat, chat + ':*']),
            # Read the test accounts' password, and only that secret.
            allow('secretsmanager:GetSecretValue', outputs['evaluation_users_secret_arn']),
            # Sign in as the test accounts.
            allow('cognito-idp:AdminInitiateAuth',
                  f"arn:{partition}:cognito-idp:{region}:{account}:userpool/{outputs['cognito_user_pool_id']}"),
            # Upload the React files and refresh the CloudFront cache.
            allow('s3:PutObject', f"arn:{partition}:s3:::{outputs['frontend_bucket']}/*"),
            allow('cloudfront:CreateInvalidation',
                  f"arn:{partition}:cloudfront::{account}:distribution/{outputs['cloudfront_distribution_id']}"),
            # Run the model grader.
            allow('lambda:InvokeFunction',
                  f"arn:{partition}:lambda:{region}:{account}:function:{outputs['evaluation_function']}"),
        ],
    }


def check_policies(trust, permissions, prefix, account, project):
    """Stop with an error unless both policies are as narrow as this lab needs."""
    conditions = trust['Statement'][0]['Condition']['StringEquals']
    problems = []

    # Who may use the role: only this repository, only the hr-staging environment.
    if conditions[PROVIDER_HOST + ':sub'] != prefix + ':environment:hr-staging':
        problems.append('the trust policy does not name exactly this repository and hr-staging')
    if conditions[PROVIDER_HOST + ':aud'] != AUDIENCE:
        problems.append(f'the trust policy audience is not {AUDIENCE}')

    # What the role may do: every resource belongs to this project in this account.
    for statement in permissions['Statement']:
        resources = statement['Resource']
        if isinstance(resources, str):
            resources = [resources]
        for resource in resources:
            if resource.startswith('arn:aws:s3:::'):
                # S3 bucket ARNs carry no account number, so check the bucket name instead.
                bucket = resource.removeprefix('arn:aws:s3:::').removesuffix('/*')
                if not bucket.startswith(project + '-'):
                    problems.append(f'{resource} is not one of this project\'s buckets')
                continue
            if resource.split(':')[4] != account:
                problems.append(f'{resource} is in another AWS account')
            parts = resource.split(':')
            if parts[5] == 'function':
                # A Lambda ARN is arn:aws:lambda:REGION:ACCOUNT:function:NAME, plus ":*" for its versions.
                if not parts[6].startswith(project + '-'):
                    problems.append(f'{resource} is not one of this project\'s functions')
                if len(parts) == 8 and parts[7] == '*':
                    parts = parts[:7]   # the one wildcard allowed: all versions of this one function
            if '*' in ':'.join(parts):
                problems.append(f'{resource} contains a wildcard')

    if problems:
        sys.exit('Stopped before changing AWS:\n  ' + '\n  '.join(problems))
    print(f'Checked: only {prefix} (hr-staging environment) can use the role,')
    print(f'and its {len(permissions["Statement"])} permissions are limited to {project} resources in account {account}.')


# ---- Step 3: AWS IAM ----

def find_provider(iam, provider_arn):
    """Return the provider's audiences, or None if the provider does not exist yet."""
    for provider in iam.list_open_id_connect_providers()['OpenIDConnectProviderList']:
        if provider['Arn'] == provider_arn:
            return iam.get_open_id_connect_provider(OpenIDConnectProviderArn=provider_arn)['ClientIDList']
    return None


def find_role(iam, role_name):
    try:
        return iam.get_role(RoleName=role_name)['Role']
    except iam.exceptions.NoSuchEntityException:
        return None


def main():
    parser = argparse.ArgumentParser(description='Create or update the pipeline\'s AWS role.')
    parser.add_argument('--repo', help='owner/name; defaults to the git remote "origin"')
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--subject-prefix', help='Exact repository subject prefix copied from GitHub settings')
    parser.add_argument('--dry-run', action='store_true', help='check and show the plan, change nothing')
    args = parser.parse_args()

    # Step 1: the repository and its token subject.
    repository = args.repo or repository_from_git()
    prefix = args.subject_prefix or subject_prefix_from_github(repository)
    check_prefix_names_this_repository(prefix, repository)

    # Step 2: build both policies from Terraform's outputs and the signed-in AWS account.
    outputs = terraform_outputs()
    account = boto3.client('sts').get_caller_identity()['Account']
    secret_arn = outputs['evaluation_users_secret_arn']
    if secret_arn.split(':')[4] != account:
        sys.exit('Terraform outputs belong to a different AWS account than the one you are signed in to')
    partition = secret_arn.split(':')[1]
    provider_arn = f'arn:{partition}:iam::{account}:oidc-provider/{PROVIDER_HOST}'
    # The chat function is named "<project>-chat", so the role is "<project>-github-ci".
    role_name = outputs['candidate_function'].removesuffix('-chat') + '-github-ci'

    trust = trust_policy(provider_arn, prefix)
    permissions = permissions_policy(outputs, account, args.region, partition)
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    (build / 'github-oidc-trust.json').write_text(json.dumps(trust, indent=2), encoding='utf-8')
    (build / 'github-oidc-permissions.json').write_text(json.dumps(permissions, indent=2), encoding='utf-8')
    print('Wrote build/github-oidc-trust.json and build/github-oidc-permissions.json')

    # Step 3: compare with what AWS already has, and say what will change.
    iam = boto3.client('iam')
    audiences = find_provider(iam, provider_arn)
    role = find_role(iam, role_name)

    # Check both policies before anything changes in AWS. Any problem stops the script here.
    project = role_name.removesuffix('-github-ci')
    check_policies(trust, permissions, prefix, account, project)

    if audiences is None:
        print(f'Creating the identity provider {PROVIDER_HOST}')
    elif AUDIENCE not in audiences:
        print(f'Adding the audience {AUDIENCE} to the existing identity provider')
    else:
        print('Identity provider already exists: reusing it')
    if role is None:
        print(f'Creating the role {role_name}')
    else:
        print(f'Role {role_name} already exists: updating it')

    if args.dry_run:
        sys.exit('Dry run: nothing was changed in AWS.')

    if audiences is None:
        iam.create_open_id_connect_provider(Url=f'https://{PROVIDER_HOST}', ClientIDList=[AUDIENCE])
    elif AUDIENCE not in audiences:
        iam.add_client_id_to_open_id_connect_provider(OpenIDConnectProviderArn=provider_arn, ClientID=AUDIENCE)

    if role is None:
        role = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust),
                               Description=f'GitHub Actions deployment role for {repository}, hr-staging only')['Role']
    else:
        iam.update_assume_role_policy(RoleName=role_name, PolicyDocument=json.dumps(trust))
    iam.put_role_policy(RoleName=role_name, PolicyName=POLICY_NAME, PolicyDocument=json.dumps(permissions))

    # Step 4: the ARN goes into GitHub in document 06b.
    (build / 'github-oidc-role-arn.txt').write_text(role['Arn'] + '\n', encoding='utf-8')
    print(f'Done. Role ARN (also saved in build/github-oidc-role-arn.txt):\n  {role["Arn"]}', flush=True)

    # Add the ARN to the file that collects every GitHub setting for document 06b.
    subprocess.run([sys.executable, str(ROOT / 'scripts' / 'github-settings.py')], cwd=ROOT)


if __name__ == '__main__':
    main()
