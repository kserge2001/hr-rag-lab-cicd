"""Validate, publish a candidate prompt, and package its exact release identity."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from string import Formatter
import zipfile

from versioning import read_version, tag_name

ROOT = Path(__file__).resolve().parents[1]

def digest(value):
    return hashlib.sha256(value).hexdigest()

def validate_prompt(text):
    fields = {field for _, field, _, _ in Formatter().parse(text) if field}
    if fields != {'full_name', 'job_title', 'department', 'location'}:
        raise ValueError(f'Unexpected prompt variables: {sorted(fields)}')
    if not text.strip() or len(text) > 30000:
        raise ValueError('Empty or oversized prompt')
    # Structural checks are not proof of resistance to prompt injection.
    text.format(full_name='Synthetic User', job_title='Employee', department='Demo', location='Austin, TX')

def generated_entry(name):
    # A fixed timestamp keeps function.zip identical for identical sources, so Terraform redeploys only real changes.
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16  # readable by the Lambda runtime user
    return info

def package(publish=False, output_dir=None):
    text = (ROOT / 'lambda/app/prompts/hr_system.txt').read_text(encoding='utf-8')
    validate_prompt(text)
    # The one release number, from the VERSION file, stamped on everything below.
    release_version = read_version()
    version = None
    if publish:
        from langfuse import get_client
        lf = get_client()
        # The label, such as v1.0.3, shows in Langfuse which release this prompt version belongs to.
        prompt = lf.create_prompt(name='hr-policy-system', prompt=text, type='text',
            labels=['candidate', tag_name(release_version)],
            config={'release': tag_name(release_version), 'commit': os.environ['GITHUB_SHA'],
                    'prompt_sha256': digest(text.encode())})
        version = prompt.version
        if lf.get_prompt('hr-policy-system', version=version).prompt != text:
            raise ValueError('Published prompt read-back mismatch')
    release = {'version': release_version, 'commit': os.getenv('GITHUB_SHA', 'development'),
        'prompt_sha256': digest(text.encode()), 'prompt_version': version,
        'dataset_sha256': digest((ROOT / 'lambda/data/evaluation_questions.json').read_bytes())}
    target = Path(output_dir) if output_dir is not None else ROOT / 'build'
    target.mkdir(exist_ok=True)
    with zipfile.ZipFile(target / 'function.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for source, prefix in [('lambda/app', 'app'), ('lambda/db', 'db')]:
            for path in sorted((ROOT / source).rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo'):
                    inside = (Path(prefix) / path.relative_to(ROOT / source)).as_posix()
                    if inside == 'app/prompts/hr_system.txt':
                        archive.writestr(generated_entry(inside), text.encode('utf-8'))
                    else:
                        archive.write(path, inside)
        archive.write(ROOT / 'lambda/data/evaluation_questions.json', 'data/evaluation_questions.json')
        archive.write(ROOT / 'lambda/data/documents/manifest.json', 'data/policy_manifest.json')
        archive.writestr(generated_entry('app/release.json'), json.dumps(release))
    release['artifact_sha256'] = digest((target / 'function.zip').read_bytes())
    (target / 'release.json').write_text(json.dumps(release, indent=2))
    return release

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    print(json.dumps(package(args.publish)))
