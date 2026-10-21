"""CloudWatch subscription -> Langfuse OTLP/HTTP, outside the private application VPC."""
import base64,gzip,json,os,urllib.request
import boto3

def handler(event,context):
    batch=json.loads(gzip.decompress(base64.b64decode(event['awslogs']['data'])))
    if batch.get('messageType')=='CONTROL_MESSAGE': return {'exported':0}
    spans=[]
    scores=[]   # user feedback, printed by telemetry.record_score()
    for item in batch.get('logEvents',[]):
        try: record=json.loads(item['message'])
        except (ValueError,TypeError): continue
        if record.get('hr_otel') is True and isinstance(record.get('span'),dict): spans.append(record['span'])
        if record.get('hr_score') is True and isinstance(record.get('score'),dict): scores.append(record['score'])
    if not spans and not scores:return {'exported':0}
    secret=json.loads(boto3.client('secretsmanager').get_secret_value(SecretId=os.environ['LANGFUSE_SECRET_ARN'])['SecretString'])
    endpoint=os.environ['LANGFUSE_BASE_URL'].rstrip('/')
    if endpoint not in ('https://cloud.langfuse.com','https://us.cloud.langfuse.com','https://jp.cloud.langfuse.com'):raise ValueError('Unapproved endpoint')
    auth=base64.b64encode((secret['public_key']+':'+secret['secret_key']).encode()).decode()
    if scores:
        send_scores(endpoint, auth, scores)
    if not spans:
        return {'exported':0,'scores':len(scores)}
    payload={'resourceSpans':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':'hr-rag-lab'}}]},'scopeSpans':[{'scope':{'name':'hr-lab-cloudwatch-export'},'spans':spans}]}]}
    req=urllib.request.Request(endpoint+'/api/public/otel/v1/traces',data=json.dumps(payload).encode(),headers={'Authorization':'Basic '+auth,'Content-Type':'application/json','x-langfuse-ingestion-version':'4'})
    try:
        with urllib.request.urlopen(req,timeout=15) as response:
            result=json.loads(response.read() or b'{}')
            if result.get('partialSuccess',{}).get('rejectedSpans',0):raise RuntimeError('Partial trace rejection')
    except Exception as error:
        # No response bodies, credentials, or HR content in error logs.
        raise RuntimeError('Trace export failed: '+type(error).__name__) from None
    return {'exported':len(spans),'scores':len(scores)}


def send_scores(endpoint, auth, scores):
    """Send each feedback score to Langfuse. A score with an existing ID replaces the old one."""
    for score in scores:
        allowed = {key: score[key] for key in ('id', 'traceId', 'name', 'value', 'dataType', 'comment', 'environment') if key in score}
        request = urllib.request.Request(endpoint + '/api/public/scores', data=json.dumps(allowed).encode(),
                                         headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=15):
                pass
        except Exception as error:
            # No response bodies or feedback text in error logs.
            raise RuntimeError('Score export failed: ' + type(error).__name__) from None
