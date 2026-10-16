"""Bounded real-session evidence collection; playback acknowledgements are simulated."""
import argparse
import asyncio
import base64
import copy
import hashlib
import io
import json
import math
from pathlib import Path
import re
import struct
import sys
import time
import wave


def sha(data):
    return hashlib.sha256(data).hexdigest()


def failure_reason(error):
    value = str(error)
    return value if re.fullmatch('[a-z][a-z0-9_]{0,80}', value) else type(error).__name__


def rejected_summary(event):
    """Only bounded protocol coordinates; never raw PCM, mesh or server errors."""
    result = {}
    if isinstance(event, dict):
        for key in ('type', 'generation', 'clip_id', 'frame_index', 'start_sample', 'total_samples'):
            value = event.get(key)
            if type(value) is int and abs(value) < 10**15:
                result[key] = value
            elif isinstance(value, str) and len(value) <= 80 and re.fullmatch('[a-zA-Z0-9_:-]+', value):
                result[key] = value
    result['event_sha256'] = sha(json.dumps(event, sort_keys=True).encode())
    return result


class Evidence:
    """Validate transmitted PCM/mesh structure and keep bounded public metadata."""
    def __init__(self, ready, now):
        self.generation = ready['generation']
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError('invalid_generation')
        self.started = now
        self.clips = {}
        self.resets = []
        self.transcripts = []
        self.text = {}
        self.first_media = {}
        self.events = 0

    def accept(self, event, now):
        self.events += 1
        if self.events > 20000:
            raise ValueError('too_many_events')
        kind = event['type']
        public = dict(event, elapsed_s=now-self.started)
        if kind == 'error':
            raise RuntimeError('server_error')
        if kind == 'reset':
            generation = event['generation']
            if type(generation) is not int or generation <= self.generation:
                raise ValueError('invalid_generation_reset')
            self.generation = generation
            self.resets.append(copy.deepcopy(public))
        elif kind in ('avatar_meta', 'media', 'clip_end', 'assistant_text'):
            generation = event.get('generation')
            if type(generation) is not int or generation != self.generation:
                raise ValueError('stale_or_invalid_generation')
            if kind == 'assistant_text':
                value = self.text.setdefault(str(generation), '') + event['text']
                if len(value) > 20000:
                    raise ValueError('text_limit')
                self.text[str(generation)] = value
                return public
            clip = event['clip_id']
            if not isinstance(clip, str) or not clip.isascii() or not clip.isdigit() or len(clip)>12:
                raise ValueError('invalid_clip_id')
            key = f'{generation}:{clip}'
            if kind == 'avatar_meta':
                count, fps, faces = event['vertex_count'], event['fps'], event['faces']
                if (key in self.clips or type(count) is not int or not 3<=count<=20000
                        or not isinstance(fps,(int,float)) or not math.isfinite(fps) or not 1<=fps<=60
                        or event['sample_rate']!=24000 or not isinstance(faces,list) or not 1<=len(faces)<=40000
                        or any(not isinstance(face,list) or len(face)!=3
                               or any(type(i) is not int or not 0<=i<count for i in face) for face in faces)):
                    raise ValueError('invalid_avatar_metadata')
                self.clips[key]=dict(generation=generation, clip_id=clip, vertices=count, fps=fps, samples=0, frames=0, ended=False)
                public.pop('faces')
                public['faces_sha256']=sha(json.dumps(faces,separators=(',',':')).encode())
            else:
                row = self.clips.get(key)
                if row is None or row['ended']:
                    raise ValueError('unknown_or_ended_clip')
                if kind == 'media':
                    audio = base64.b64decode(event['audio'],validate=True)
                    vertices = base64.b64decode(event['vertices'],validate=True)
                    if (not audio or len(audio)%2 or len(vertices)!=row['vertices']*12
                            or any(not math.isfinite(v[0]) for v in struct.iter_unpack('<f',vertices))
                            or type(event['start_sample']) is not int or event['start_sample']!=row['samples']
                            or type(event['frame_index']) is not int or event['frame_index']!=row['frames']
                            or not math.isfinite(event['pts']) or abs(event['pts']-row['samples']/24000)>.00001):
                        raise ValueError('invalid_media_samples_or_mesh')
                    row['samples']+=len(audio)//2
                    row['frames']+=1
                    if row['samples']>720000:
                        raise ValueError('clip_sample_limit')
                    self.first_media.setdefault(str(generation),now-self.started)
                    public.pop('audio');public.pop('vertices')
                    public.update(audio_samples=len(audio)//2,audio_sha256=sha(audio),vertices_sha256=sha(vertices))
                else:
                    if type(event['total_samples']) is not int or event['total_samples']!=row['samples'] or not row['frames']:
                        raise ValueError('clip_end_sample_mismatch')
                    row['ended']=True
        elif kind == 'transcript':
            if not isinstance(event['text'],str) or len(event['text'])>4000:
                raise ValueError('invalid_transcript')
            self.transcripts.append(event['text'])
        return public

    def media_complete(self, generation):
        rows=[r for r in self.clips.values() if r['generation']==generation]
        return bool(rows) and all(r['ended'] for r in rows)

    def snapshot(self):
        return copy.deepcopy(dict(generation=self.generation, clips=self.clips, resets=self.resets,
                                  transcripts=self.transcripts, text=self.text,
                                  first_media_s=self.first_media, events=self.events))


async def exercise_case(case, *, url, input_root, http, connect, emit, restored=None):
    """One bounded socket lifetime; fixture injection never substitutes real evidence."""
    query = '?profile=streamingtalker&backend=cascade'
    if restored is not None:
        query += '&conversation=' + restored['conversation_id']
    ws_url = url.replace('http://', 'ws://', 1) + '/avatar/ws' + query
    report = dict(case=case['case'], status='running', input_kind=case['kind'],
                  playback='synthetic received-sample acknowledgements, not physical playback')
    evidence = None
    sender = None
    interrupted_generation = None
    playback_started = set()
    def checkpoint():
        if evidence is not None:
            report['evidence'] = evidence.snapshot()
        emit(dict(event='checkpoint', report=copy.deepcopy(report)))
    checkpoint()
    try:
        async with asyncio.timeout(case.get('timeout_s', 90)):
            async with connect(ws_url, proxy=None, max_size=2_000_000, open_timeout=5, close_timeout=2) as ws:
                ready = json.loads(await ws.recv())
                if (ready.get('type')!='ready' or ready.get('provider')!='streamingtalker'
                        or ready.get('dialogue_backend')!='cascade' or ready.get('input_sample_rate')!=16000):
                    raise ValueError('unexpected_session_ready')
                report['conversation_id'] = ready['conversation_id']
                baseline = ready['transcript']
                if not isinstance(baseline, list):
                    raise ValueError('invalid_initial_history')
                if restored is not None:
                    if ready['conversation_id']!=restored['conversation_id'] or ready['transcript']!=restored['transcript']:
                        raise ValueError('restored_history_mismatch')
                    report['restored_history_matches'] = True
                evidence = Evidence(ready, now=time.monotonic())
                emit(dict(event='session_ready', case=case['case'], ready=ready))
                async def send(message):
                    await ws.send(json.dumps(message, ensure_ascii=False))
                if case['kind']=='wav':
                    relative=Path(case['file'])
                    if relative.name!=str(relative):
                        raise ValueError('invalid_input_filename')
                    path=Path(input_root)/relative
                    if path.is_symlink() or path.stat().st_size>2_000_000:
                        raise ValueError('invalid_input_file')
                    data=path.read_bytes()
                    if sha(data)!=case['sha256']:
                        raise ValueError('input_hash_mismatch')
                    with wave.open(io.BytesIO(data)) as wav:
                        if (wav.getframerate(),wav.getnchannels(),wav.getsampwidth())!=(16000,1,2):
                            raise ValueError('invalid_wav_format')
                        pcm=wav.readframes(wav.getnframes())
                    report['input_sha256']=sha(data)
                    async def send_audio():
                        for offset in range(0,len(pcm),640):
                            await ws.send(pcm[offset:offset+640]);await asyncio.sleep(.02)
                        for _ in range(60):
                            await ws.send(bytes(640));await asyncio.sleep(.02)
                    sender=asyncio.create_task(send_audio())
                elif case['kind']=='text':
                    await send({'type':'text','text':case['text']})
                else:
                    raise ValueError('unknown_input_kind')
                checkpoint()
                while True:
                    if sender is not None and sender.done():
                        sender.result()
                    try:
                        message=await asyncio.wait_for(ws.recv(), .2)
                    except asyncio.TimeoutError:
                        message=None
                    if message is not None:
                        event=json.loads(message)
                        try:
                            public=evidence.accept(event,now=time.monotonic())
                        except Exception as error:
                            emit(dict(event='rejected_event',case=case['case'],
                                      reason=failure_reason(error),data=rejected_summary(event)))
                            raise
                        emit(dict(event='session_event',case=case['case'],data=public))
                        checkpoint()
                        if event['type']=='media':
                            generation=event['generation']
                            if generation not in playback_started:
                                await send(dict(type='playback',generation=generation,state='started'))
                                playback_started.add(generation)
                            # Acknowledge bytes received to exercise application history.
                            # This deliberately does not simulate a wall-clock speaker.
                            await send(dict(type='playback',generation=generation,state='progress',
                                            clip_id=event['clip_id'],played_samples=evidence.clips[f"{generation}:{event['clip_id']}"]['samples']))
                            if case.get('interrupt') and interrupted_generation is None:
                                interrupted_generation=generation
                                report['interrupted_generation']=generation
                                await send({'type':'interrupt'})
                                await send({'type':'text','text':case['recovery_text']})
                                emit(dict(event='interrupt_sent',case=case['case'],generation=generation))
                    current=evidence.generation
                    if not evidence.media_complete(current) or (sender is not None and not sender.done()):
                        continue
                    if case.get('interrupt') and (interrupted_generation is None or current<=interrupted_generation):
                        continue
                    response=await http.get(url+'/avatar/conversations/'+report['conversation_id'],timeout=2)
                    response.raise_for_status();record=response.json()
                    transcript=record.get('transcript',[])
                    if not transcript or transcript[-1].get('role')!='assistant' or transcript[-1].get('status')!='heard':
                        continue
                    if not evidence.text.get(str(current)) or not evidence.transcripts:
                        raise ValueError('reply_or_input_transcript_missing')
                    # A stale GET may return the preceding turn marked heard.
                    # Require this connection's exact prefix and new turn(s).
                    expected_rows = len(baseline) + (4 if case.get('interrupt') else 2)
                    normalized = lambda value: ''.join(value.split())
                    if (len(transcript) != expected_rows or transcript[:len(baseline)] != baseline
                            or transcript[-2].get('role') != 'user'
                            or transcript[-2].get('text') != evidence.transcripts[-1]
                            or normalized(transcript[-1].get('text', '')) != normalized(evidence.text[str(current)])):
                        report['history_pending_reason'] = 'current_turn_not_committed'
                        continue
                    if case.get('interrupt') and not any(row.get('role')=='assistant' and row.get('status')=='interrupted' for row in transcript):
                        raise ValueError('interruption_not_saved')
                    # clip_end only closes one TTS segment. Finish the simulated
                    # playback after production and every segment are committed.
                    await send(dict(type='playback',generation=current,state='ended'))
                    report.pop('history_pending_reason', None)
                    report.update(status='passed',transcript=transcript,completed_generation=current)
                    break
    except BaseException as error:
        report.update(status='failed',error_type=type(error).__name__,failure_reason=failure_reason(error))
        raise
    finally:
        if sender is not None:
            sender.cancel();await asyncio.gather(sender,return_exceptions=True)
        checkpoint()
    return report


async def run_batch(manifest, *, url, input_root, http, connect, emit, admit):
    results=[]
    try:
        for case in manifest['cases']:
            emit(dict(event='request_ready',case=case['case']))
            await admit(case['case'])
            restore=None
            if 'restore_case' in case:
                restore=next((row for row in results if row['case']==case['restore_case']),None)
                if restore is None:
                    raise ValueError('restore_case_not_completed')
            row=await exercise_case(case,url=url,input_root=input_root,http=http,connect=connect,
                                    emit=emit,restored=restore)
            results.append(row)
            emit(dict(event='complete',case=case['case']))
        emit(dict(event='capture',report={'status':'passed','cases':results}))
        emit(dict(event='finished',requests=len(results)))
        return results
    except BaseException as error:
        emit(dict(event='failed',error_type=type(error).__name__,completed_cases=len(results)))
        raise


def main():
    import httpx
    import websockets
    from urllib.parse import urlsplit
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--inputs',type=Path,required=True)
    parser.add_argument('--url',required=True)
    args=parser.parse_args()
    parsed=urlsplit(args.url)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.path:
        parser.error('application URL must be a loopback HTTP origin')
    # Reuse the existing bounded stdin admission reader; host controls each case.
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'llm-server/deploy'))
    from protocol_client import admit_stdin
    def emit(row):print(json.dumps(row,ensure_ascii=False,allow_nan=False),flush=True)
    async def run():
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=3) as http:
            await run_batch(json.loads(args.manifest.read_text()),url=args.url,input_root=args.inputs,
                            http=http,connect=websockets.connect,emit=emit,admit=admit_stdin)
    asyncio.run(run())


if __name__=='__main__':main()
