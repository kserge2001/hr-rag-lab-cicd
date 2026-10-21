"""The packaged prompt is authoritative; Langfuse stores its release version."""
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).parent

def load_prompt():
    return (ROOT / 'prompts/hr_system.txt').read_text(encoding='utf-8')

def release_identity():
    path = ROOT / 'release.json'
    release = json.loads(path.read_text()) if path.exists() else {}
    actual = hashlib.sha256(load_prompt().encode()).hexdigest()
    if release and release['prompt_sha256'] != actual:
        raise RuntimeError('Packaged prompt does not match release manifest')
    # x-hr-release is the one release number, such as v1.0.3, from the VERSION file.
    if release.get('version'):
        release_name = 'v' + release['version']
    else:
        release_name = 'development'
    return {'x-hr-release': release_name,
            'x-hr-lambda-version':os.getenv('AWS_LAMBDA_FUNCTION_VERSION','$LATEST'), 'x-hr-commit': release.get('commit', 'development'),
            'x-hr-prompt-sha256': actual,
            'x-hr-prompt-version': str(release.get('prompt_version', 'unpublished'))}
