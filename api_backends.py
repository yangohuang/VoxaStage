"""Explicit opt-in compatibility APIs. Credentials remain server-side."""
import json
import os
from urllib.parse import urlsplit


def configured_api(kind, env=None):
    if kind not in ('asr','llm'):
        raise ValueError('Unknown API component')
    env=os.environ if env is None else env
    prefix='PIPECAT_'+kind.upper()
    mode=env.get(prefix+'_MODE','local')
    if mode=='local':return None
    if mode!='api':raise ValueError('Expected local or api mode')
    base=env.get(prefix+'_API_BASE','').rstrip('/')
    model=env.get(prefix+'_API_MODEL','')
    key=env.get(prefix+'_API_KEY','')
    parsed=urlsplit(base)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or not model or not key:
        raise ValueError(f'{prefix}: API base, model and key must be configured')
    _=parsed.port
    token_field=env.get('PIPECAT_LLM_TOKEN_FIELD','max_tokens')
    if token_field not in ('max_tokens','max_completion_tokens'):
        raise ValueError('Unsupported completion token field')
    return CompatibilityAPI(base,model,key,token_field)


class CompatibilityAPI:
    def __init__(self,base,model,key,token_field):
        self.base,self.model,self.key,self.token_field=base,model,key,token_field

    async def transcribe(self,client,wav):
        if len(wav)>2_000_000:raise ValueError('Audio request too large')
        r=await client.post(self.base+'/audio/transcriptions',
            headers={'Authorization':'Bearer '+self.key},
            files={'file':('speech.wav',wav,'audio/wav')},
            data={'model':self.model,'response_format':'json'},timeout=60)
        r.raise_for_status()
        text=r.json().get('text')
        if not isinstance(text,str) or len(text)>4000:raise ValueError('Invalid transcription response')
        return text

    async def generate(self,client,messages):
        payload={'model':self.model,'messages':messages,'stream':True,self.token_field:256}
        async with client.stream('POST',self.base+'/chat/completions',json=payload,
                headers={'Authorization':'Bearer '+self.key},timeout=90) as response:
            response.raise_for_status()
            text_count=0
            async for line in response.aiter_lines():
                if len(line)>100000:raise ValueError('API event too large')
                if not line.startswith('data:'):continue
                value=line[5:].strip()
                if value=='[DONE]':return
                event=json.loads(value)
                if event.get('error'):raise RuntimeError('Configured API returned an error')
                for choice in event.get('choices',[]):
                    content=choice.get('delta',{}).get('content')
                    if content is None:continue
                    if not isinstance(content,str):raise ValueError('Invalid API text')
                    text_count+=len(content)
                    if text_count>4000:raise ValueError('API response too long')
                    yield content
            raise ValueError('API stream ended without completion marker')
