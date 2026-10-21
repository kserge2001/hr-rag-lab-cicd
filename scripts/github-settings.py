"""Collect every value the GitHub environment hr-staging needs into one file.

    python scripts/github-settings.py

Writes build/github-settings.txt: each variable and secret name exactly as GitHub expects it,
with its value on the next line. Document 06b copies from this one file instead of looking
in five places. Setup (document 02) and scripts/prepare-oidc.py (document 05) run this
script when they finish, so the file fills up as you go. Run it yourself at any time.

The values come from:
  Terraform outputs               function names, secret, Cognito, bucket, CloudFront, app address
  frontend/.env.production        the two public Cognito settings the React build needs
  .env.langfuse                   the Langfuse address and key pair
  build/github-oidc-role-arn.txt  the pipeline's AWS role, written in document 05

The file holds the Langfuse secret key. build/ is git-ignored, so it never reaches GitHub,
but keep it out of screenshots and chat.
"""
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'build' / 'github-settings.txt'
MISSING = 'MISSING'


def read_env_file(path):
    """Read NAME=value lines from a file like .env.langfuse. Returns {} if it does not exist."""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.strip().startswith('#'):
            name, value = line.split('=', 1)
            values[name.strip()] = value.strip().strip('"')
    return values


def terraform_outputs():
    command = ['terraform', '-chdir=infra/terraform', 'output', '-json']
    result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    return {name: item['value'] for name, item in json.loads(result.stdout).items()}


def aws_region():
    """The region set in terraform.tfvars, the same one setup used."""
    tfvars = ROOT / 'infra' / 'terraform' / 'terraform.tfvars'
    if tfvars.exists():
        match = re.search(r'^\s*aws_region\s*=\s*"([^"]+)"', tfvars.read_text(encoding='utf-8'), re.M)
        if match:
            return match.group(1)
    return MISSING + ': set aws_region in infra/terraform/terraform.tfvars (document 02)'


def role_arn():
    saved = ROOT / 'build' / 'github-oidc-role-arn.txt'
    if saved.exists():
        return saved.read_text(encoding='utf-8').strip()
    return MISSING + ': run python scripts/prepare-oidc.py (document 05)'


def main():
    outputs = terraform_outputs()
    frontend = read_env_file(ROOT / 'frontend' / '.env.production')
    langfuse = read_env_file(ROOT / '.env.langfuse')

    def output(name):
        return outputs.get(name) or MISSING + ': run bash scripts/setup-lab.sh (document 02)'

    def from_file(values, name, file_name, step):
        return values.get(name) or f'{MISSING}: {name} not found in {file_name} ({step})'

    # Same order as the list in document 06b, step 3.
    variables = [
        ('AWS_REGION', aws_region()),
        ('AWS_STAGING_ROLE_ARN', role_arn()),
        ('HR_STAGING_FUNCTION', output('candidate_function')),
        ('HR_EVALUATION_FUNCTION', output('evaluation_function')),
        ('HR_STAGING_ORIGIN', output('app_url')),
        ('HR_TEST_USERS_SECRET_ARN', output('evaluation_users_secret_arn')),
        ('HR_COGNITO_POOL_ID', output('cognito_user_pool_id')),
        ('HR_COGNITO_CLIENT_ID', output('cognito_client_id')),
        ('HR_STAGING_FRONTEND_BUCKET', output('frontend_bucket')),
        ('HR_STAGING_DISTRIBUTION_ID', output('cloudfront_distribution_id')),
        ('VITE_COGNITO_AUTHORITY', from_file(frontend, 'VITE_COGNITO_AUTHORITY', 'frontend/.env.production', 'document 02')),
        ('VITE_COGNITO_DOMAIN', from_file(frontend, 'VITE_COGNITO_DOMAIN', 'frontend/.env.production', 'document 02')),
        ('LANGFUSE_BASE_URL', from_file(langfuse, 'LANGFUSE_BASE_URL', '.env.langfuse', 'document 02, step 3')),
        ('ENABLE_DEEPEVAL', 'true'),
        ('ENABLE_EVIDENTLY', 'true'),
    ]
    secrets = [
        ('LANGFUSE_PUBLIC_KEY', from_file(langfuse, 'LANGFUSE_PUBLIC_KEY', '.env.langfuse', 'document 02, step 3')),
        ('LANGFUSE_SECRET_KEY', from_file(langfuse, 'LANGFUSE_SECRET_KEY', '.env.langfuse', 'document 02, step 3')),
    ]

    lines = [
        'GitHub settings for the environment hr-staging',
        f'Written by scripts/github-settings.py on {datetime.now():%Y-%m-%d %H:%M}. Run it again to refresh.',
        'Copy each value exactly as shown. This file holds the Langfuse secret key: keep it private.',
        '',
        '=== ENVIRONMENT VARIABLES ===',
        'Settings > Environments > hr-staging > Environment variables > Add environment variable',
        '',
    ]
    for name, value in variables:
        lines += [name, value, '']
    lines += [
        '=== ENVIRONMENT SECRETS ===',
        'Settings > Environments > hr-staging > Environment secrets > Add environment secret',
        '',
    ]
    for name, value in secrets:
        lines += [name, value, '']

    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text('\n'.join(lines), encoding='utf-8')

    missing = [name for name, value in variables + secrets if value.startswith(MISSING)]
    print(f'GitHub settings: wrote build/github-settings.txt ({len(variables)} variables, {len(secrets)} secrets).')
    for name in missing:
        print(f'  still missing: {name}')


if __name__ == '__main__':
    main()
