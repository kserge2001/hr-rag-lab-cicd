"""Wait for automatic LiteLLM initialization; do not print keys.

The gateway instance writes a new application key to Secrets Manager once LiteLLM
is running. When setup replaces the instance, the secret still holds the old
instance's key, so this waits for a key written after the current instance launched.
"""
import json, subprocess, time
import boto3

outputs = json.loads(subprocess.check_output(['terraform', '-chdir=infra/terraform', 'output', '-json'], text=True))
secret = outputs['gateway_app_key_secret_arn']['value']
instance_id = outputs['gateway_instance_id']['value']
client = boto3.client('secretsmanager')

instance = boto3.client('ec2').describe_instances(InstanceIds=[instance_id])['Reservations'][0]['Instances'][0]
launched = instance['LaunchTime']

for attempt in range(180):
    try:
        value = client.get_secret_value(SecretId=secret)
        key = json.loads(value['SecretString'])['key']
        written_after_launch = value['CreatedDate'] > launched
        if key.startswith('sk-') and written_after_launch:
            print('LiteLLM scoped application key initialized')
            break
    except client.exceptions.ResourceNotFoundException:
        pass
    time.sleep(10)
else:
    raise SystemExit('Gateway initialization failed; inspect the instance bootstrap logs without sharing secret files')
