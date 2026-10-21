"""Promote only an evaluated immutable version inside this isolated staging lab."""
import base64,hashlib,json,os
from pathlib import Path
import boto3
root=Path(__file__).resolve().parents[1];release=json.loads((root/'build/release.json').read_text());report=json.loads((root/'build/evaluation.json').read_text())
if report['release']!=release or not report['records'] or len(report['records'])!=report['expected'] or any(not r['passed'] for r in report['records']):raise SystemExit('Missing or failing exact-release evaluation')
version=str(release.get('lambda_version',''))
if not version.isdigit():raise SystemExit('Published numeric version required')
client=boto3.client('lambda');function=os.environ['HR_STAGING_FUNCTION'];config=client.get_function_configuration(FunctionName=function,Qualifier=version)
if config.get('Environment',{}).get('Variables',{}).get('LAB_ENVIRONMENT')!='staging':raise SystemExit('Only isolated staging is authorized')
expected=base64.b64encode(bytes.fromhex(release['artifact_sha256'])).decode()
if config['CodeSha256']!=expected:raise SystemExit('Published artifact differs from evaluated artifact')
try:
    alias=client.get_alias(FunctionName=function,Name='approved')
    (root/'build/previous-approved.json').write_text(json.dumps({'function':function,'version':alias['FunctionVersion']}))
    # The alias description records the version it replaced, so ci/rollback_lab.py can restore it.
    client.update_alias(FunctionName=function,Name='approved',FunctionVersion=version,RevisionId=alias['RevisionId'],
                        Description=f"Lab approved release pointer; release v{release['version']}; previous approved version: {alias['FunctionVersion']}")
except client.exceptions.ResourceNotFoundException:
    client.create_alias(FunctionName=function,Name='approved',FunctionVersion=version,Description=f"Lab approved release pointer; release v{release['version']}; previous approved version: none")
print(f"Release v{release['version']} (Lambda version {version}) promoted to the lab approved alias; production is separate")
