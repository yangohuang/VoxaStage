import asyncio
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import wave

import httpx

from asr_capture import load_inputs, load_variants, run


def fixture(directory):
    root = Path(directory)
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x01\x00' * 1600)
    audio = stream.getvalue()
    (root / 'audio.wav').write_bytes(audio)
    manifest = dict(version=1, cases=[dict(id='mixed', audio='audio.wav',
        sha256=hashlib.sha256(audio).hexdigest(), reference='你好 agent',
        reference_kind='synthesis_prompt', transcript_reviewed=False, source='synthetic')])
    path = root / 'cases.json'
    path.write_text(json.dumps(manifest))
    config = root / 'variants.json'
    config.write_text(json.dumps(dict(version=1, variants=[dict(id='plain', context=''), dict(id='terms', context='agent, Pipecat')])) )
    args = SimpleNamespace(manifest=path, variants=config, output=root/'out', url='http://127.0.0.1:18315',
                           expected_model='test-model', model_revision=None, rounds=2, timeout=1)
    return args, audio, manifest


def health():
    return httpx.Response(200, json=dict(ready=True, busy=False, model='test-model', revision='test-revision',device='cpu',threads=2))


def result(text='你好 agent'):
    return httpx.Response(200, json=dict(text=text, audio_s=.1, elapsed_s=.01, model='test-model'))


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoints_preserve_full_report_when_cancelled_before_next_post(self):
        with tempfile.TemporaryDirectory() as d:
            args, _, _ = fixture(d)
            checkpoints = []
            second_ready = asyncio.Event()
            ready_count = 0
            async def handler(request):
                return health() if request.method == 'GET' else result()
            async def observe(event, trial):
                nonlocal ready_count
                if event == 'request_ready':
                    ready_count += 1
                    if ready_count == 2:
                        second_ready.set()
                        await asyncio.Event().wait()
            task = asyncio.create_task(run(args, transport=httpx.MockTransport(handler), observer=observe,
                                           checkpoint=lambda report:checkpoints.append(json.loads(json.dumps(report)))))
            await asyncio.wait_for(second_ready.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            last = checkpoints[-1]
            self.assertEqual(last['status'], 'aborted')
            self.assertEqual([t['status'] for t in last['trials']], ['ok', 'aborted'])
            self.assertIn('cases', last); self.assertIn('variants', last)
            self.assertIn('source_sha256', last); self.assertIn('health_snapshot_sha256', last)

    async def test_observer_denial_prevents_audio_post_and_retains_failed_trial(self):
        with tempfile.TemporaryDirectory() as d:
            args, _, _ = fixture(d)
            requests = []
            async def handler(request):
                requests.append(request.method)
                return health() if request.method == 'GET' else result()
            async def observe(event, trial):
                if event == 'request_ready':
                    raise RuntimeError('host_denied')
            report = await run(args, transport=httpx.MockTransport(handler), observer=observe)
            self.assertEqual(requests, ['GET'])
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['trials'][0]['status'], 'aborted')

    async def test_observer_receives_ordered_actual_trials_and_can_stop_on_error(self):
        with tempfile.TemporaryDirectory() as d:
            args, _, _ = fixture(d)
            events = []
            posts = []
            async def handler(request):
                if request.method == 'GET': return health()
                posts.append(request.content)
                return httpx.Response(500, json={'error': 'injected'})
            async def observe(event, trial):
                events.append((event, dict(trial)))
                if event == 'complete' and trial['status'] != 'ok':
                    raise RuntimeError('stop_failed_batch')
            report = await run(args, transport=httpx.MockTransport(handler), observer=observe)
            self.assertEqual(len(posts), 1)
            self.assertEqual([e[0] for e in events], ['request_ready', 'complete'])
            self.assertEqual(events[-1][1]['http_status'], 500)
            self.assertEqual(report['trials'][0]['status'], 'error')
            self.assertEqual(report['status'], 'failed')

    def test_header_whitespace_rejected_before_http(self):
        for context in (' agent', 'agent ', ' ', '\tagent'):
            raw=json.dumps(dict(version=1,variants=[dict(id='invalid',context=context)])).encode()
            with self.subTest(context=context), self.assertRaises(ValueError):load_variants(raw)

    def test_manifest_hash_format_and_path_validation(self):
        with tempfile.TemporaryDirectory() as d:
            args, audio, manifest = fixture(d)
            cases = load_inputs(args.manifest, args.manifest.read_bytes())
            self.assertEqual(cases[0]['audio'], audio)
            for patch in ({'sha256':'0'*64}, {'audio':'../escape.wav'}, {'source':'unknown'},
                          {'reference_kind':'human_transcript'}, {'transcript_reviewed':'false'}):
                changed = json.loads(json.dumps(manifest))
                changed['cases'][0].update(patch)
                with self.subTest(patch=patch), self.assertRaises(ValueError):
                    load_inputs(args.manifest, json.dumps(changed).encode())
            (Path(d)/'audio.wav').write_bytes(audio[:-3])
            manifest['cases'][0]['sha256']=hashlib.sha256(audio[:-3]).hexdigest()
            with self.assertRaises(ValueError):
                load_inputs(args.manifest, json.dumps(manifest).encode())

    def test_real_audio_requires_transcript_and_sources_are_separate(self):
        with tempfile.TemporaryDirectory() as d:
            args, _, manifest=fixture(d)
            manifest['cases'][0]['source']='human'
            with self.assertRaises(ValueError):
                load_inputs(args.manifest,json.dumps(manifest).encode())
            manifest['cases'][0].update(reference_kind='human_transcript',transcript_reviewed=True)
            cases=load_inputs(args.manifest,json.dumps(manifest).encode())
            self.assertEqual(cases[0]['public']['source'],'human')
            manifest['cases'].append(dict(manifest['cases'][0],id='other',source='synthetic'))
            with self.assertRaises(ValueError):
                load_inputs(args.manifest,json.dumps(manifest).encode())

    async def test_frozen_request_bytes_rotated_order_and_scoring(self):
        with tempfile.TemporaryDirectory() as d:
            args, audio, _ = fixture(d)
            contexts=[]
            async def handler(request):
                if request.url.path=='/health':
                    (Path(d)/'audio.wav').write_bytes(b'changed after validation')
                    return health()
                self.assertEqual(request.content, audio)
                self.assertNotIn('reference', str(request.headers))
                contexts.append(request.headers['x-asr-context'])
                return result()
            report=await run(args, transport=httpx.MockTransport(handler))
            self.assertEqual(report['status'],'passed')
            self.assertEqual(report['execution'],'injected_transport')
            self.assertEqual(contexts,['','agent, Pipecat','','agent, Pipecat','agent, Pipecat',''])
            self.assertEqual(len(report['trials']),6)
            self.assertEqual(len(json.loads((args.output/'scoring-input.json').read_text())),4)
            self.assertTrue(all(t['audio_sha256']==hashlib.sha256(audio).hexdigest() for t in report['trials']))
            self.assertIsNone(report['declared_model_revision'])
            self.assertEqual(report['reported_model_revision'],'test-revision')
            self.assertEqual(report['endpoint'],'http://127.0.0.1:18315')
            self.assertEqual(report['reported_execution_metadata']['threads'],2)
            self.assertIsNone(report['reported_execution_metadata']['dtype'])
            self.assertFalse(report['model_configuration_complete'])

    async def test_http_failure_keeps_formal_denominator_and_empty_output(self):
        with tempfile.TemporaryDirectory() as d:
            args, _, _=fixture(d)
            calls=0
            async def handler(request):
                nonlocal calls
                if request.url.path=='/health':return health()
                calls+=1
                if calls==3:return httpx.Response(503,text='private diagnostic body')
                return result('')
            report=await run(args,transport=httpx.MockTransport(handler))
            self.assertEqual(report['status'],'failed')
            rows=json.loads((args.output/'scoring-input.json').read_text())
            self.assertEqual(len(rows),4)
            self.assertEqual(sum(r['status']=='error' for r in rows),1)
            self.assertNotIn('private diagnostic body',(args.output/'results.json').read_text())
            self.assertEqual(report['scoring']['variants']['plain']['failed'],1)
            self.assertEqual(report['scoring']['variants']['terms']['reference_groups']['synthesis_prompt']['rate'],1)

    async def test_health_mismatch_prevents_inference(self):
        with tempfile.TemporaryDirectory() as d:
            args, _, _=fixture(d)
            calls=[]
            async def handler(request):
                calls.append(request.url.path)
                return httpx.Response(200,json=dict(ready=True,busy=False,model='wrong-model'))
            report=await run(args,transport=httpx.MockTransport(handler))
            self.assertEqual(report['status'],'failed')
            self.assertEqual(calls,['/health'])
            self.assertFalse((args.output/'scoring-input.json').exists())

    async def test_timeout_and_malformed_reply_are_failures(self):
        for mode in ('timeout','model','duration','size','nan'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                args, _, _=fixture(d)
                args.rounds=1;args.timeout=.02
                async def handler(request):
                    if request.url.path=='/health':return health()
                    if mode=='timeout':await asyncio.sleep(1)
                    if mode=='size':return httpx.Response(200,content=b'x'*70000)
                    if mode=='nan':return httpx.Response(200,content=b'{"text":"ok","audio_s":0.1,"elapsed_s":NaN,"model":"test-model"}')
                    return httpx.Response(200,json=dict(text='ok',audio_s=2 if mode=='duration' else .1,elapsed_s=.01,model='wrong' if mode=='model' else 'test-model'))
                report=await run(args,transport=httpx.MockTransport(handler))
                self.assertEqual(report['status'],'failed')
                self.assertTrue(all(t['status']=='error' for t in report['trials']))

    async def test_cancellation_persists_aborted_state(self):
        with tempfile.TemporaryDirectory() as d:
            args, _, _=fixture(d)
            async def handler(request):
                if request.url.path=='/health':return health()
                raise asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):
                await run(args,transport=httpx.MockTransport(handler))
            report=json.loads((args.output/'results.json').read_text())
            self.assertEqual(report['status'],'aborted')
            self.assertEqual(report['trials'][0]['status'],'aborted')

    async def test_reusing_output_and_invalid_context_do_not_send_requests(self):
        with tempfile.TemporaryDirectory() as d:
            args, _, _=fixture(d)
            async def handler(request):self.fail('unexpected request')
            args.variants.write_text(json.dumps(dict(version=1,variants=[dict(id='bad',context='bad\r\nheader')])) )
            report=await run(args,transport=httpx.MockTransport(handler))
            self.assertEqual(report['status'],'failed')
            original=(args.output/'results.json').read_bytes()
            with self.assertRaises(FileExistsError):await run(args,transport=httpx.MockTransport(handler))
            self.assertEqual((args.output/'results.json').read_bytes(),original)


if __name__=='__main__':unittest.main()
