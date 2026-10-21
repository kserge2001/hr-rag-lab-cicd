"""Delete every conversation in this deployment, so every account starts with an empty chat history.

    python scripts/clear-chat-history.py          asks first
    python scripts/clear-chat-history.py --yes    no question

Use it after evaluation runs or demos. The request and lookup logs used for monitoring are kept.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]

if '--yes' not in sys.argv:
    answer = input('Delete the chat history of every account in this deployment? [y/N] ')
    if answer.strip().lower() not in ('y', 'yes'):
        sys.exit('Nothing was deleted.')

outputs = json.loads(subprocess.check_output(['terraform', '-chdir=infra/terraform', 'output', '-json'], cwd=ROOT))
function = outputs['function_names']['value']['setup-db']
response = boto3.client('lambda', region_name=os.getenv('AWS_REGION', 'us-east-1')).invoke(
    FunctionName=function, Payload=json.dumps({'delete_all_conversations': True}))
result = json.loads(response['Payload'].read())
if 'FunctionError' in response or 'errorMessage' in result:
    sys.exit(f'Deleting chat history failed: {result.get("errorMessage", result)}')
print(f'Deleted {result["deleted_conversations"]} conversations.')
