"""Print the app address, the demo employees and the gateway UI sign-in, and save them to demo-sign-in.txt.

    python scripts/show-demo-users.py

setup-lab.sh runs this at the end, so the details are always in demo-sign-in.txt. Rerun this to refresh it.
The pipeline's test accounts are not listed: they belong to automated tests, not people.
The file is git-ignored. It holds the gateway master key, so keep it on this computer.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
SAVED = ROOT / 'demo-sign-in.txt'
color = sys.stdout.isatty() and not os.getenv('NO_COLOR')
GREEN, BOLD, RESET = ('\033[32m', '\033[1m', '\033[0m') if color else ('', '', '')

outputs = json.loads(subprocess.check_output(['terraform', '-chdir=infra/terraform', 'output', '-json'], cwd=ROOT))
region = os.getenv('AWS_REGION', 'us-east-1')
secrets = boto3.client('secretsmanager', region_name=region)
passwords = json.loads(secrets.get_secret_value(SecretId=outputs['demo_users_secret_arn']['value'])['SecretString'])
master_key = json.loads(secrets.get_secret_value(
    SecretId=outputs['gateway_settings_secret_arn']['value'])['SecretString'])['master_key']

# Name and role come from the demo data the database was seeded with.
seed = (ROOT / 'lambda/db/03_seed_data.sql').read_text(encoding='utf-8')
people = {email: (name, role) for email, name, role in
          re.findall(r"\('NW-\d+',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)'", seed)}

rows = [('Email', 'Name', 'Role')] + [(email, *people.get(email, ('', ''))) for email in sorted(passwords)]
widths = [max(len(row[i]) for row in rows) for i in range(3)]
table = ['    ' + '  '.join(value.ljust(width) for value, width in zip(row, widths)).rstrip() for row in rows]
url, password = outputs['app_url']['value'], next(iter(passwords.values()))


def sign_in(bold='', reset=''):
    return '\n'.join([
        f'  Open the app:  {bold}{url}{reset}',
        '  Click Sign in, then use any of these email addresses.',
        '', *table, '',
        f'  Password (the same for every account):  {bold}{password}{reset}',
        '',
        '  Each role sees different data, so try more than one.',
        '',
        '  LiteLLM gateway admin UI',
        '    Open the tunnel:  python scripts/open-gateway-ui.py',
        '    Then open:        http://localhost:4000/ui',
        '    Username:         admin',
        f'    Password:         {bold}{master_key}{reset}',
        '    The password is the gateway master key. It can change every model limit and budget.',
    ])


SAVED.write_text('HR assistant: demo sign-in\n\n' + sign_in() + '\n', encoding='utf-8', newline='\n')
print(f'\n{GREEN}{BOLD}Setup complete.{RESET}\n')
print(sign_in(BOLD, RESET))
print(f'\n  Also saved in {SAVED.name} in the project folder.\n')
