"""Set up the two kinds of sign-in account; keep passwords in Secrets Manager, not terminal output.

  Demo employees     people sign in to the app as them (Amara, Liam, Priya, Dana)
  Test accounts      the pipeline and automated tests sign in as them (qa.*@northwind.example)

Each kind has its own secret and its own password, so the CI role, which can read only the
test-account secret, never holds the password people use. Safe to rerun: stored passwords are kept.
"""
import json,os,secrets,subprocess
from pathlib import Path
import boto3
root=Path(__file__).resolve().parents[1]
outputs=json.loads(subprocess.check_output(['terraform','-chdir=infra/terraform','output','-json'],cwd=root)); get=lambda name:outputs[name]['value']
region=os.getenv('AWS_REGION','us-east-1'); sm=boto3.client('secretsmanager',region_name=region); cp=boto3.client('cognito-idp',region_name=region)
pool=get('cognito_user_pool_id')

def set_up(emails, secret_arn):
    try: stored=json.loads(sm.get_secret_value(SecretId=secret_arn)['SecretString'])
    except sm.exceptions.ResourceNotFoundException: stored={}
    # One random password per kind of account, reused on reruns so it stays the same.
    password=next(iter(stored.values()),None) or 'Aa1!'+secrets.token_urlsafe(24)
    for email in emails:
        try:cp.admin_get_user(UserPoolId=pool,Username=email)
        except cp.exceptions.UserNotFoundException:cp.admin_create_user(UserPoolId=pool,Username=email,MessageAction='SUPPRESS',UserAttributes=[{'Name':'email','Value':email},{'Name':'email_verified','Value':'true'}])
        # Reapply the retained password after a user pool is recreated.
        cp.admin_set_user_password(UserPoolId=pool,Username=email,Password=password,Permanent=True)
    # Write only these accounts, so an older secret that also held demo passwords is cleaned up.
    sm.put_secret_value(SecretId=secret_arn,SecretString=json.dumps({email:password for email in emails}))

test_accounts=sorted({case['email'] for case in json.loads((root/'lambda/data/evaluation_questions.json').read_text())})
set_up(test_accounts,get('evaluation_users_secret_arn'))
set_up(sorted(get('demo_user_emails')),get('demo_users_secret_arn'))
print(f'{len(test_accounts)} test accounts and {len(get("demo_user_emails"))} demo employees ready; passwords retained securely')
