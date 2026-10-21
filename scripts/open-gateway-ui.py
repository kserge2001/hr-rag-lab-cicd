"""Open the LiteLLM admin UI on this computer through an SSM tunnel.

    python scripts/open-gateway-ui.py               serves the UI at http://localhost:4000/ui
    python scripts/open-gateway-ui.py --port 4100   uses another local port

The gateway has no public inbound rule. AWS Systems Manager forwards the instance's
port 4000 to this computer. Your AWS permissions decide who can open the tunnel.
The traffic stays inside the encrypted SSM session. Press Ctrl+C to close the tunnel.

Needs the AWS CLI and the Session Manager plugin:
https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_GUIDE = 'https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html'
color = sys.stdout.isatty() and not os.getenv('NO_COLOR')
BOLD, YELLOW, RESET = ('\033[1m', '\033[33m', '\033[0m') if color else ('', '', '')

parser = argparse.ArgumentParser(description='Open the LiteLLM admin UI through an SSM tunnel.')
parser.add_argument('--port', type=int, default=4000, help='local port for the UI (default 4000)')
local_port = parser.parse_args().port

if not shutil.which('session-manager-plugin'):
    sys.exit(f'The Session Manager plugin is not installed. Install it, open a new terminal, then rerun.\n  {PLUGIN_GUIDE}')

outputs = json.loads(subprocess.check_output(['terraform', '-chdir=infra/terraform', 'output', '-json'], cwd=ROOT))
region = os.getenv('AWS_REGION', 'us-east-1')
instance = outputs['gateway_instance_id']['value']
settings = json.loads(boto3.client('secretsmanager', region_name=region).get_secret_value(
    SecretId=outputs['gateway_settings_secret_arn']['value'])['SecretString'])

print(f'\n  Gateway UI:  {BOLD}http://localhost:{local_port}/ui{RESET}')
print('  Username:    admin')
print(f'  Password:    {settings["master_key"]}')
print(f'\n  {YELLOW}The password is the gateway master key. It can create keys and change every limit.')
print(f'  Keep it out of screenshots, tickets and chat.{RESET}')
print('\n  Opening the tunnel. Leave this terminal open and press Ctrl+C to close it.\n', flush=True)

try:
    subprocess.run(['aws', 'ssm', 'start-session', '--region', region, '--target', instance,
                    '--document-name', 'AWS-StartPortForwardingSession',
                    '--parameters', json.dumps({'portNumber': ['4000'], 'localPortNumber': [str(local_port)]})])
except KeyboardInterrupt:
    pass
print('\n  Tunnel closed.')
