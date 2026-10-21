"""Check ingestion completed and a real authenticated synthetic API request succeeds."""
import json,os,subprocess,sys,time
from pathlib import Path
import boto3,requests
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'ci'))
from auth import token_for
outputs=json.loads(subprocess.check_output(['terraform','-chdir=infra/terraform','output','-json'],cwd=root));get=lambda key:outputs[key]['value']
os.environ.update(HR_TEST_USERS_SECRET_ARN=get('evaluation_users_secret_arn'),HR_COGNITO_POOL_ID=get('cognito_user_pool_id'),HR_COGNITO_CLIENT_ID=get('cognito_client_id'))
# No sensitive question or answer is printed. This is a readiness check, not the full evaluation gate.
origin=get('app_url'); health=requests.get(origin+'/api/health',timeout=35);health.raise_for_status()
# setup-lab.sh passes --skip-ingestion-wait when no document changed: the index is already built,
# and the ingestion logs that prove it may have expired after 14 days.
wait_for_ingestion='--skip-ingestion-wait' not in sys.argv
if wait_for_ingestion:
    logs=boto3.client('logs',region_name=os.getenv('AWS_REGION','us-east-1'))
    function=get('function_names')['ingest']; deadline=time.monotonic()+900
    manifest=boto3.client('s3',region_name=os.getenv('AWS_REGION','us-east-1')).head_object(Bucket=get('documents_bucket'),Key='manifest.json')
    start_ms=int(manifest['LastModified'].timestamp()*1000)
    while time.monotonic()<deadline:
        events=logs.filter_log_events(logGroupName='/aws/lambda/'+function,startTime=start_ms).get('events',[])
        if any('chunks indexed' in e['message'] for e in events):break
        if any('[ERROR]' in e['message'] for e in events):raise SystemExit('Ingestion error; inspect restricted CloudWatch logs')
        time.sleep(10)
    else:raise SystemExit('Ingestion readiness timed out')
email=json.loads((root/'lambda/data/evaluation_questions.json').read_text())[0]['email']
headers={'Authorization':'Bearer '+token_for(email)}
# The first search after ingestion wakes the scaled-to-zero collection and can pass the 29-second API limit, so retry while it warms.
for attempt in range(3):
    try:
        response=requests.post(origin+'/api/chat',headers=headers,json={'question':'What is the annual leave policy?'},timeout=35)
        if response.status_code<500:break
    except requests.exceptions.Timeout:pass
    if attempt<2:print('Chat API still warming up; retrying in 20 seconds');time.sleep(20)
else:raise SystemExit('Chat API did not become ready; inspect restricted CloudWatch logs')
response.raise_for_status()
# Record the test conversation so setup can delete it; signed-in users should start with an empty history.
conversation=response.json().get('conversation_id')
if conversation is not None:
    record=root/'build/test-conversations.json'
    saved=json.loads(record.read_text()) if record.exists() else []
    record.write_text(json.dumps(sorted(set(saved+[conversation]))))
if not response.json().get('answer'):raise SystemExit('Readiness answer missing')
print(('Ingestion complete; ' if wait_for_ingestion else '')+'authenticated synthetic API readiness passed')
