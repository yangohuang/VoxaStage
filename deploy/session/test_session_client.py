import asyncio
import base64
from collections import deque
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import wave

import httpx

spec=importlib.util.spec_from_file_location('session_client',Path(__file__).with_name('session_client.py'))
client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)


class Socket:
    def __init__(self, mode='normal', prior=None):
        self.mode=mode;self.events=deque();self.sent=[];self.generation=0;self.finished=False;self.turns=0
        self.prior=prior or [];self.last_text='';self.binary_started=False
        self.events.append(dict(type='ready',generation=0,provider='streamingtalker',dialogue_backend='cascade',
                                input_sample_rate=16000,conversation_id='fixture-conversation',transcript=self.prior))
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def recv(self):
        while not self.events:await asyncio.sleep(.005)
        event=self.events.popleft()
        if event['type']=='clip_end' and event['clip_id']=='2':self.finished=True
        return json.dumps(event)
    def reply(self,text):
        self.generation+=1;self.turns+=1;g=self.generation;self.last_text=text;self.finished=False
        self.events.extend([dict(type='reset',generation=g,reason='text'),dict(type='transcript',text=text),
                            dict(type='assistant_text',generation=g,text='你好。')])
        for clip in ['1','2']:
            self.events.append(dict(type='avatar_meta',generation=g,clip_id=clip,vertex_count=3,fps=30,sample_rate=24000,faces=[[0,1,2]]))
            self.events.append(dict(type='media',generation=g,clip_id=clip,start_sample=0,frame_index=0,pts=0,
                                    audio=base64.b64encode(bytes(1600)).decode(),vertices=base64.b64encode(struct.pack('<9f',*([0.]*9))).decode()))
            if self.mode=='error':self.events.append(dict(type='error',message='fixture'));return
            if self.mode=='timeout':return
            self.events.append(dict(type='clip_end',generation=g,clip_id=clip,total_samples=800))
    async def send(self,data):
        self.sent.append(data)
        if isinstance(data,bytes):
            if not self.binary_started:self.binary_started=True;self.reply('语音 agent')
            return
        event=json.loads(data)
        if event['type']=='text':self.reply(event['text'])
        elif event['type']=='interrupt':
            self.events.clear();old=self.generation;self.generation+=1
            self.events.append(dict(type='reset',generation=self.generation,reason='interrupted'))
            if self.mode=='late':self.events.append(dict(type='media',generation=old,clip_id='1'))
    def history(self):
        if self.mode=='stale_history':
            return [{'role':'user','text':'old'},{'role':'assistant','text':'你好。','status':'heard'}]
        rows=list(self.prior)
        if self.turns>1:rows.extend([{'role':'user','text':'old'},{'role':'assistant','text':'old','status':'interrupted'}])
        return rows+[{'role':'user','text':self.last_text},{'role':'assistant','text':'你好。','status':'heard' if self.finished else 'generated'}]


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self,mode='normal',**changes):
        sock=Socket(mode);events=[]
        async def handler(request):return httpx.Response(200,json={'transcript':sock.history()})
        case=dict(case='fixture',kind='text',text='你好',timeout_s=2);case.update(changes)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            try:
                result=await client.exercise_case(case,url='http://127.0.0.1:1',input_root='.',http=http,
                                                  connect=lambda *a,**kw:sock,emit=events.append)
            except BaseException as error:return sock,events,error
        return sock,events,result

    async def test_waits_for_all_clips_and_saved_history(self):
        sock,events,result=await self.exercise()
        self.assertIsInstance(result,dict);self.assertEqual(result['status'],'passed')
        self.assertEqual(len(result['evidence']['clips']),2)
        self.assertTrue(all(row['ended'] for row in result['evidence']['clips'].values()))
        self.assertEqual(result['transcript'][-1]['status'],'heard')
        self.assertIn('synthetic',result['playback'])

    async def test_playback_spans_all_clips_in_the_turn(self):
        sock,events,result=await self.exercise()
        playback=[json.loads(data) for data in sock.sent if isinstance(data,str)
                  and json.loads(data)['type']=='playback']
        self.assertEqual([row['state'] for row in playback],
                         ['started','progress','progress','ended'])
        self.assertEqual({row['generation'] for row in playback},{result['completed_generation']})

    async def test_interrupted_generation_never_sends_ended_after_reset(self):
        sock,events,result=await self.exercise(interrupt=True,recovery_text='恢复')
        playback=[json.loads(data) for data in sock.sent if isinstance(data,str)
                  and json.loads(data)['type']=='playback']
        self.assertEqual([row['generation'] for row in playback if row['state']=='started'],
                         [result['interrupted_generation'],result['completed_generation']])
        self.assertEqual([row['generation'] for row in playback if row['state']=='ended'],
                         [result['completed_generation']])

    async def test_server_error_preserves_first_media_in_failed_checkpoint(self):
        sock,events,error=await self.exercise('error')
        self.assertIsInstance(error,RuntimeError)
        self.assertEqual(events[-1]['report']['status'],'failed')
        self.assertEqual(events[-1]['report']['evidence']['clips']['1:1']['samples'],800)

    async def test_timeout_preserves_partial_progress(self):
        sock,events,error=await self.exercise('timeout',timeout_s=.08)
        self.assertIsInstance(error,TimeoutError)
        self.assertEqual(events[-1]['report']['status'],'failed')
        self.assertEqual(events[-1]['report']['evidence']['clips']['1:1']['frames'],1)

    async def test_interrupt_then_recovery_is_distinct_generation(self):
        sock,events,result=await self.exercise(interrupt=True,recovery_text='恢复')
        self.assertIsInstance(result,dict)
        self.assertGreater(result['completed_generation'],result['interrupted_generation'])
        self.assertTrue(any(row.get('status')=='interrupted' for row in result['transcript']))

    async def test_old_media_after_reset_fails(self):
        sock,events,error=await self.exercise('late',interrupt=True,recovery_text='恢复')
        self.assertIsInstance(error,ValueError)
        self.assertEqual(events[-1]['report']['status'],'failed')
        self.assertEqual(events[-1]['report']['failure_reason'],'stale_or_invalid_generation')
        rejected=next(row for row in events if row['event']=='rejected_event')
        self.assertEqual(rejected['data']['generation'],1)
        self.assertNotIn('audio',rejected['data']);self.assertNotIn('vertices',rejected['data'])

    async def test_stale_heard_history_cannot_complete_current_turn(self):
        sock,events,error=await self.exercise('stale_history',timeout_s=.08)
        self.assertIsInstance(error,TimeoutError)
        self.assertEqual(events[-1]['report']['status'],'failed')

    async def test_restore_selects_interrupted_case_instead_of_first_case(self):
        events=[];sockets=[];rows=[]
        def connect(*args,**kwargs):
            sock=Socket(prior=rows[1]['transcript'] if len(sockets)==2 else None)
            sockets.append(sock)
            return sock
        def emit(event):
            events.append(event)
            if event['event']=='checkpoint' and event['report']['status']=='passed':
                rows.append(event['report'])
        async def handler(request):return httpx.Response(200,json={'transcript':sockets[-1].history()})
        async def admit(case):pass
        cases=[dict(case='first',kind='text',text='你好',timeout_s=1),
               dict(case='interrupt',kind='text',text='长回答',interrupt=True,recovery_text='恢复',timeout_s=1),
               dict(case='restore',kind='text',text='继续',restore_case='interrupt',timeout_s=1)]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result=await client.run_batch({'cases':cases},url='http://127.0.0.1:1',input_root='.',
                http=http,connect=connect,emit=emit,admit=admit)
        self.assertTrue(result[2]['restored_history_matches'])
        self.assertEqual(result[2]['transcript'][:4],result[1]['transcript'])

    async def test_denied_admission_never_connects(self):
        events=[];connections=[]
        async def admit(case):raise TimeoutError('denied')
        with self.assertRaises(TimeoutError):
            await client.run_batch({'cases':[{'case':'one'}]},url='http://127.0.0.1:1',input_root='.',http=None,
                                   connect=lambda *a,**kw:connections.append(a),emit=events.append,admit=admit)
        self.assertEqual(connections,[]);self.assertEqual(events[-1]['event'],'failed')

    async def test_restore_checks_saved_transcript_before_new_turn(self):
        prior=[{'role':'user','text':'old'},{'role':'assistant','text':'旧回答','status':'heard'}]
        sock=Socket(prior=prior)
        async def handler(request):return httpx.Response(200,json={'transcript':sock.history()})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result=await client.exercise_case(dict(case='restore',kind='text',text='继续',timeout_s=2),
                url='http://127.0.0.1:1',input_root='.',http=http,connect=lambda *a,**kw:sock,emit=lambda row:None,
                restored={'conversation_id':'fixture-conversation','transcript':prior})
        self.assertTrue(result['restored_history_matches']);self.assertEqual(result['transcript'][:2],prior)

    async def test_wav_hash_mismatch_sends_no_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fixture.wav';path.write_bytes(b'not-the-frozen-file')
            sock=Socket();events=[]
            with self.assertRaisesRegex(ValueError,'input_hash_mismatch'):
                await client.exercise_case(dict(case='audio',kind='wav',file=path.name,sha256='0'*64,timeout_s=1),
                    url='http://127.0.0.1:1',input_root=directory,http=None,connect=lambda *a,**kw:sock,emit=events.append)
            self.assertEqual(sock.sent,[]);self.assertEqual(events[-1]['report']['status'],'failed')

if __name__=='__main__':unittest.main()
