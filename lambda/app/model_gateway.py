"""Bedrock-shaped requests through LiteLLM. No direct-model fallback.

LiteLLM runs on its own instance. It does the routing, load balancing, fallback,
caching, cost tracking and rate limiting itself. They are switched on in
infra/terraform/gateway-bootstrap.sh.tftpl. This file is the application's side:
it sends each request to LiteLLM, asks for the features it wants, and reads back
what LiteLLM did. Search for "LiteLLM feature" to find each one.
"""
import json
import os
from contextlib import contextmanager
from functools import cache
from urllib.parse import urlparse
import boto3
import requests

class GatewayPolicyDenied(RuntimeError):
    """Authorization, budget or rate-limit denial; never retry on another model."""

class GatewayUnavailable(RuntimeError):
    """Network/provider failure after the gateway's own fallback was tried."""

# Details of the chat request being answered right now. A Lambda answers one
# request at a time, so one shared dictionary is enough.
current_request = {'user': None, 'use_cache': False}

@contextmanager
def gateway_context(user=None, use_cache=False):
    """Inside this block, model calls carry the user's ID and may use cached answers."""
    current_request['user'] = user
    current_request['use_cache'] = use_cache
    try:
        yield
    finally:
        # Forget them, so the next request starts clean.
        current_request['user'] = None
        current_request['use_cache'] = False

@cache
def gateway_key():
    arn = os.environ['LLM_GATEWAY_KEY_SECRET_ARN']
    value = boto3.client('secretsmanager').get_secret_value(SecretId=arn)
    key = json.loads(value['SecretString'])['key']
    if not key or not key.startswith('sk-'):
        raise ValueError('Gateway key is not initialized')
    return key

class GatewayClient:
    def __init__(self, timeout=20, purpose='hr-chat'):
        self.purpose = purpose
        self.base = os.environ['LLM_GATEWAY_URL'].rstrip('/')
        parsed = urlparse(self.base)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username:
            raise ValueError('Invalid gateway endpoint')
        self.timeout = timeout

    def converse(self, modelId, **request):
        allowed = {os.environ['CHAT_MODEL'], os.environ['INLINE_JUDGE_MODEL']}
        fallback = os.getenv('FALLBACK_CHAT_MODEL')
        if fallback: allowed.add(fallback)
        if modelId not in allowed:
            raise GatewayPolicyDenied('Model is not approved for this application')
        config = request.get('inferenceConfig', {})
        if not 1 <= config.get('maxTokens', 1024) <= 2048:
            raise GatewayPolicyDenied('Output token limit exceeded')
        messages = []
        if request.get('system'):
            messages.append({'role':'system','content':'\n'.join(x['text'] for x in request['system'])})
        for message in request.get('messages', []):
            text=[];calls=[];results=[]
            for block in message.get('content', []):
                if 'text' in block: text.append(block['text'])
                elif 'toolUse' in block:
                    tool=block['toolUse']
                    calls.append({'id':tool['toolUseId'],'type':'function','function':{'name':tool['name'],'arguments':json.dumps(tool['input'])}})
                elif 'toolResult' in block:
                    tool=block['toolResult']
                    results.append({'role':'tool','tool_call_id':tool['toolUseId'],'content':json.dumps(tool['content'])})
                else: raise GatewayPolicyDenied('Unsupported content block')
            if text or calls:
                entry={'role':message['role'],'content':'\n'.join(text)}
                if calls: entry['tool_calls']=calls
                messages.append(entry)
            messages.extend(results)
        payload={'model':modelId,'messages':messages,'max_tokens':config.get('maxTokens',1024),'temperature':config.get('temperature',0)}
        tools=request.get('toolConfig',{}).get('tools',[])
        if tools:
            payload['tools']=[{'type':'function','function':{'name':t['toolSpec']['name'],'description':t['toolSpec'].get('description',''),'parameters':t['toolSpec']['inputSchema']['json']}} for t in tools]
        choice=request.get('toolConfig',{}).get('toolChoice',{})
        if 'any' in choice: payload['tool_choice']='required'
        elif 'tool' in choice: payload['tool_choice']={'type':'function','function':{'name':choice['tool']['name']}}
        # LiteLLM feature 8, tags and users: the purpose tag (hr-chat, inline-judge
        # or ci-judge) and the opaque user ID are stored with the request, so the
        # UI can show logs and spend per purpose and per user.
        payload['metadata'] = {'tags': [self.purpose]}
        if current_request['user']:
            payload['user'] = current_request['user']
        # LiteLLM feature 4, caching: ask for a cached answer only when allowed.
        # Pipeline test accounts never are, so repeated evaluations still measure
        # how much answers vary.
        if current_request['use_cache']:
            payload['cache'] = {'use-cache': True}
        # LiteLLM feature 1, routing: every model call goes to this one address.
        # The key proves which application is asking. LiteLLM checks the key's
        # allowed models, budget and rate limits before it calls Bedrock.
        try:
            response = requests.post(
                self.base + '/chat/completions',
                headers={'Authorization': 'Bearer ' + gateway_key()},
                json=payload, timeout=(2, self.timeout))
        except requests.RequestException as error:
            raise GatewayUnavailable('Gateway request did not complete') from error
        # LiteLLM features 5 and 6, budget and rate limits: when this key has spent
        # its budget or sent too many requests, LiteLLM refuses with 429 and never
        # calls the model. Refusals are not retried on another model.
        if response.status_code in (400, 401, 403, 429):
            # Do not expose proxy error bodies: they can contain keys or input text.
            raise GatewayPolicyDenied('Gateway rejected the request or its allowance')
        # LiteLLM feature 3, fallback: a 5xx arrives only after LiteLLM has
        # already tried the fallback model.
        if response.status_code >= 500:
            raise GatewayUnavailable('Gateway or model service unavailable')
        if response.status_code != 200:
            raise GatewayPolicyDenied('Unexpected gateway response')
        value = response.json()
        choices=value.get('choices',[])
        if not choices: raise GatewayUnavailable('Incomplete gateway model response')
        message=choices[0]['message'];content=[]
        if message.get('content'): content.append({'text':message['content']})
        for call in message.get('tool_calls',[]):
            content.append({'toolUse':{'toolUseId':call['id'],'name':call['function']['name'],'input':json.loads(call['function']['arguments'])}})
        reason={'stop':'end_turn','tool_calls':'tool_use','length':'max_tokens'}.get(choices[0].get('finish_reason'),'unknown')
        usage=value.get('usage',{})
        # What LiteLLM did with this request, read from its reply.
        # LiteLLM feature 3, fallback: the model that answered. After a fallback
        # it differs from the model we asked for, and the header counts the attempts.
        model_used = value.get('model') or modelId
        model_used = model_used.replace('bedrock/', '')
        fallbacks_tried = response.headers.get('x-litellm-attempted-fallbacks', '0')
        # LiteLLM feature 2, load balancing: which deployment, and so which region, answered.
        deployment = response.headers.get('x-litellm-model-id', '')
        # LiteLLM feature 5, cost tracking: the price LiteLLM calculated for this
        # request, in US dollars. On a cache hit this header still shows the
        # original price, but LiteLLM records 0 spend against the key.
        cost = response.headers.get('x-litellm-response-cost', '')
        gateway = {
            'model': model_used,
            'deployment': deployment,
            'fallback': fallbacks_tried not in ('', '0', 'None'),
            'cost_usd': cost,
        }
        return {'output':{'message':{'role':'assistant','content':content}},'stopReason':reason,
                'usage':{'inputTokens':usage.get('prompt_tokens',0),'outputTokens':usage.get('completion_tokens',0)},
                'gateway': gateway}
