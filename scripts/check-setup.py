"""Check everything setup-lab.sh needs before it installs or creates anything.

    python scripts/check-setup.py

Uses only the standard library and the AWS CLI, so it runs before the virtual
environment exists. Lists every problem it finds, then exits 1 if there were any.
"""
import base64
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TFVARS = ROOT / 'infra/terraform/terraform.tfvars'
ENV_FILE = ROOT / '.env.langfuse'
STATE = ROOT / 'infra/terraform/terraform.tfstate'
problems = []


def problem(message):
    problems.append(message)


def run(*command):
    """Run a command and return (succeeded, stdout, last stderr line)."""
    path = shutil.which(command[0])
    if not path:
        return False, '', f'{command[0]} not found'
    result = subprocess.run([path, *command[1:]], capture_output=True, text=True)
    error = (result.stderr.strip().splitlines() or [''])[-1]
    return result.returncode == 0, result.stdout.strip(), error


def aws(region, *args):
    ok, out, error = run('aws', *args, '--region', region, '--output', 'json')
    return ok, (json.loads(out) if ok and out else {}), error


def version_tuple(text):
    return tuple(int(part) for part in re.findall(r'\d+', text)[:3])


def check_tools():
    if sys.version_info < (3, 10):
        problem(f'Python {sys.version.split()[0]} is too old. Install Python 3.12.')
    try:
        import ensurepip, venv  # noqa: F401
    except ImportError:
        problem('Python cannot create virtual environments. On Ubuntu or Debian run: sudo apt install python3-venv')

    ok, out, _ = run('aws', '--version')
    if not ok:
        problem('AWS CLI not found. Install AWS CLI version 2.')
    elif not out.startswith('aws-cli/2'):
        problem(f'AWS CLI version 2 is required; found {out.split()[0]}.')

    ok, out, _ = run('terraform', 'version', '-json')
    if not ok:
        problem('Terraform not found. Install Terraform 1.9 or later.')
    elif version_tuple(json.loads(out)['terraform_version']) < (1, 9):
        problem(f'Terraform {json.loads(out)["terraform_version"]} is too old. Install 1.9 or later.')

    ok, out, _ = run('node', '--version')
    if not ok or not shutil.which('npm'):
        problem('Node.js and npm not found. Install Node.js 22.')
    else:
        major = version_tuple(out)[0]
        # The frontend build tool (Vite 6) supports Node 18, 20 and 22 or later.
        if major < 18 or major in (19, 21):
            problem(f'Node.js {out} is not supported by the frontend build. Install Node.js 22.')


def read_settings(path, pattern):
    settings = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        match = re.match(pattern, line)
        if match:
            settings[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return settings


def check_files():
    """Return (terraform settings, Langfuse settings), or None if a file is missing."""
    missing = [path for path in (TFVARS, ENV_FILE) if not path.exists()]
    for path in missing:
        name = path.relative_to(ROOT).as_posix()
        problem(f'Missing {name}. Create it from {name}.example as described in section 2 of the guide.')
    if missing:
        return None

    tf = read_settings(TFVARS, r'^\s*(\w+)\s*=\s*("[^"]*"|[^#\s]+)')
    lf = read_settings(ENV_FILE, r'^\s*(LANGFUSE_\w+)\s*=\s*(.*)$')

    project = tf.get('project_name', '')
    if 'yourname' in TFVARS.read_text(encoding='utf-8'):
        problem('Replace "yourname" in infra/terraform/terraform.tfvars with your own identifier.')
    # OpenSearch policy names such as <project>-encryption are limited to 32 characters.
    elif not re.fullmatch(r'[a-z][a-z0-9-]{2,20}', project):
        problem(f'project_name "{project}" must be 3 to 21 characters: lowercase letters, numbers and hyphens, starting with a letter.')

    prefix = tf.get('cognito_domain_prefix', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,62}', prefix):
        problem(f'cognito_domain_prefix "{prefix}" must be 3 to 63 lowercase letters, numbers and hyphens.')
    elif re.search('aws|amazon|cognito', prefix):
        problem(f'cognito_domain_prefix "{prefix}" cannot contain "aws", "amazon" or "cognito".')

    public, secret = lf.get('LANGFUSE_PUBLIC_KEY', ''), lf.get('LANGFUSE_SECRET_KEY', '')
    base_url = lf.get('LANGFUSE_BASE_URL', '').rstrip('/')
    if 'replace-me' in public + secret or not public.startswith('pk-lf-') or not secret.startswith('sk-lf-'):
        problem('Enter your Langfuse keys in .env.langfuse: the public key starts pk-lf-, the secret key sk-lf-.')
    elif not base_url.startswith('https://'):
        problem('Set LANGFUSE_BASE_URL in .env.langfuse to your project endpoint, for example https://us.cloud.langfuse.com.')
    else:
        check_langfuse(base_url, public, secret)
    if base_url and tf.get('langfuse_base_url', '').rstrip('/') != base_url:
        problem(f'langfuse_base_url in terraform.tfvars must match LANGFUSE_BASE_URL in .env.langfuse ({base_url}).')
    return tf, lf


def check_langfuse(base_url, public, secret):
    token = base64.b64encode(f'{public}:{secret}'.encode()).decode()
    request = urllib.request.Request(base_url + '/api/public/projects', headers={'Authorization': 'Basic ' + token})
    try:
        urllib.request.urlopen(request, timeout=20)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            problem(f'Langfuse at {base_url} rejected the keys in .env.langfuse. Check the keys, and that the URL matches your project region.')
        else:
            problem(f'Langfuse at {base_url} answered HTTP {error.code}. Check LANGFUSE_BASE_URL.')
    except (urllib.error.URLError, TimeoutError) as error:
        problem(f'Cannot reach Langfuse at {base_url}: {getattr(error, "reason", error)}.')


def deployed_from_this_folder():
    try:
        return bool(json.loads(STATE.read_text(encoding='utf-8')).get('resources'))
    except (FileNotFoundError, ValueError):
        return False
    except OSError:
        return True  # Present but unreadable, for example locked by an editor: assume a rerun rather than block it.


def check_aws(tf):
    region = tf.get('aws_region') or 'us-east-1'
    project = tf.get('project_name', '')
    ok, identity, error = aws(region, 'sts', 'get-caller-identity')
    if not ok:
        problem(f'AWS credentials do not work ({error}). Run aws configure, or aws sso login, and retry.')
        return

    # The chat model can be an inference profile such as global.anthropic...; availability uses the base id.
    chat_model = re.sub(r'^(global|us|eu|apac)\.', '', tf.get('chat_model_id', 'amazon.nova-pro-v1:0'))
    for model in sorted({'amazon.titan-embed-text-v2:0', 'amazon.nova-lite-v1:0', chat_model}):
        ok, status, error = aws(region, 'bedrock', 'get-foundation-model-availability', '--model-id', model)
        if not ok:
            problem(f'Cannot check Bedrock model {model} in {region} ({error}).')
        elif status.get('regionAvailability') != 'AVAILABLE':
            problem(f'Bedrock model {model} is not offered in {region}. Use us-east-1.')
        elif status.get('authorizationStatus') != 'AUTHORIZED':
            problem(f'Bedrock model {model} is not enabled for account {identity["Account"]}. Request access in the Bedrock console under Model access.')

    domain = tf.get('domain_name', '').rstrip('.')
    if domain:
        ok, zones, error = aws(region, 'route53', 'list-hosted-zones-by-name', '--dns-name', domain, '--max-items', '1')
        if not ok or not any(zone['Name'] == domain + '.' for zone in zones.get('HostedZones', [])):
            problem(f'domain_name "{domain}" has no Route 53 hosted zone in this account. Leave domain_name empty to use the CloudFront address.')

    if deployed_from_this_folder() or not re.fullmatch(r'[a-z][a-z0-9-]{2,20}', project):
        return  # A rerun updates the existing stack, so the name checks below do not apply.

    prefix = tf.get('cognito_domain_prefix', '')
    ok, found, _ = aws(region, 'cognito-idp', 'describe-user-pool-domain', '--domain', prefix)
    if ok and found.get('DomainDescription', {}).get('UserPoolId'):
        problem(f'cognito_domain_prefix "{prefix}" is already taken in {region}. Choose another value in terraform.tfvars.')

    ok, _, _ = aws(region, 'lambda', 'get-function', '--function-name', f'{project}-chat')
    existing = ok
    for name in (f'{project}-langfuse', f'{project}-evaluation-users'):
        ok, found, _ = aws(region, 'secretsmanager', 'describe-secret', '--secret-id', name)
        if ok and found.get('DeletedDate'):
            problem(f'Secret {name} from an earlier teardown is still reserved. Release it with:\n'
                    f'    aws secretsmanager delete-secret --secret-id {name} --force-delete-without-recovery --region {region}')
        elif ok:
            existing = True
    if existing:
        problem(f'A stack named "{project}" already exists in this AWS account, but this folder has no Terraform state for it. '
                'Run setup from the folder that created it, or choose a different project_name.')


def main():
    print('Checking tools, configuration and AWS account ...')
    check_tools()
    settings = check_files()
    if settings:
        check_aws(settings[0])
    if problems:
        print(f'\nFix these before running setup ({len(problems)} found):')
        for number, message in enumerate(problems, 1):
            print(f'  {number}. {message}')
        sys.exit(1)
    print('All setup checks passed.')


if __name__ == '__main__':
    main()
