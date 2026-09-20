import asyncio
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
from types import SimpleNamespace

from benchmark_avatar import load_cases, run_benchmark, render_markdown


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        wav = self.root / 'voice.wav'
        with wave.open(str(wav), 'wb') as out:
            out.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            out.writeframes(b'\x01\x00' * 960)
        self.data = {'version': 1, 'cases': [{'id': 'mixed-01', 'wav': 'voice.wav',
            'sha256': hashlib.sha256(wav.read_bytes()).hexdigest(), 'category': 'mixed',
            'source': 'synthetic', 'transcript': '语音 agent', 'transcript_reviewed': False}]}
        self.manifest = self.root / 'cases.json'
        self.write()

    def write(self):
        self.manifest.write_text(json.dumps(self.data))

    def test_loads_manifest_relative_to_its_directory(self):
        case, = load_cases(self.manifest)
        self.assertEqual(case.id, 'mixed-01')
        self.assertEqual(case.pcm, b'\x01\x00' * 960)
        self.assertEqual(case.input_sha256, self.data['cases'][0]['sha256'])
        self.assertFalse(case.transcript_reviewed)

    def test_rejects_input_changed_after_manifest_was_created(self):
        self.data['cases'][0]['sha256'] = '0' * 64
        self.write()
        with self.assertRaises(ValueError): load_cases(self.manifest)

    def test_rejects_duplicate_case_and_implicit_review(self):
        self.data['cases'].append(dict(self.data['cases'][0]))
        self.write()
        with self.assertRaises(ValueError): load_cases(self.manifest)
        self.data['cases'].pop()
        self.data['cases'][0]['transcript_reviewed'] = 'false'
        self.write()
        with self.assertRaises(ValueError): load_cases(self.manifest)

    def test_rejects_truncated_wav(self):
        wav = self.root/'voice.wav'
        wav.write_bytes(wav.read_bytes()[:-10])
        self.data['cases'][0]['sha256'] = hashlib.sha256(wav.read_bytes()).hexdigest()
        self.write()
        with self.assertRaises(ValueError): load_cases(self.manifest)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    setUp = ManifestTests.setUp
    write = ManifestTests.write
    def args(self):
        return SimpleNamespace(manifest=self.manifest, output=self.root/'report',
            provider='flashhead', mode='burst', rounds=2, warmup=1, timeout=1.,
            interrupt=True, deployment_label='test-fixture', config=None)

    async def test_serial_phases_cleanup_and_report_scope(self):
        closed=[]
        class Backend:
            async def stream(self, source):
                try:
                    pcm=b''.join([part async for part in source])
                    yield {'type':'avatar_meta','sample_rate':24000,'fps':25}
                    yield {'type':'media','frame_index':0,'start_sample':0,'pts':0.,
                           'audio':base64.b64encode(pcm).decode()}
                    yield {'type':'clip_end','total_samples':len(pcm)//2}
                finally: closed.append(True)
        report=await run_benchmark(self.args(), backend_factory=Backend)
        self.assertEqual(report['status'],'passed')
        self.assertEqual([r['phase'] for r in report['trials']],
                         ['warmup','measured','measured','interrupt','recovery'])
        self.assertEqual(len(closed),5)
        self.assertEqual(report['summary'][0]['attempted'],2)
        self.assertFalse(report['quality']['lip_sync']['measured'])
        self.assertEqual(json.loads((self.root/'report/results.json').read_text())['status'],'passed')
        self.assertIn('接收', render_markdown(report))

    async def test_failed_run_replaces_old_success_and_keeps_denominator(self):
        args=self.args(); args.warmup=0; args.interrupt=False
        args.output.mkdir()
        (args.output/'results.json').write_text('{"status":"passed"}')
        class Broken:
            async def stream(self, source):
                yield {'type':'avatar_meta','sample_rate':24000,'fps':25}
                raise RuntimeError('private ws://secret-user:secret@private-host')
        report=await run_benchmark(args,backend_factory=Broken)
        self.assertEqual(report['status'],'failed')
        self.assertEqual(report['summary'][0]['failed'],2)
        saved=(args.output/'results.json').read_text()
        self.assertNotIn('secret',saved)
        self.assertNotIn('private-host',saved)

    async def test_timeout_closes_generator(self):
        args=self.args(); args.rounds=1; args.warmup=0; args.interrupt=False; args.timeout=.01
        closed=[]
        class Slow:
            async def stream(self, source):
                try:
                    yield {'type':'avatar_meta','sample_rate':24000,'fps':25}
                    await asyncio.sleep(5)
                finally: closed.append(True)
        report=await run_benchmark(args,backend_factory=Slow)
        self.assertEqual(report['status'],'failed')
        self.assertEqual(report['trials'][0]['error_type'],'TimeoutError')
        self.assertEqual(closed,[True])

    async def test_interrupt_cleanup_outlives_operation_deadline(self):
        args=self.args(); args.timeout=.01
        closed=[]
        class Backend:
            async def stream(self, source):
                try:
                    pcm=b''.join([chunk async for chunk in source])
                    yield {'type':'avatar_meta','sample_rate':24000,'fps':25}
                    yield {'type':'media','frame_index':0,'start_sample':0,'pts':0.,
                           'audio':base64.b64encode(pcm).decode()}
                finally:
                    await asyncio.sleep(.03)
                    closed.append(True)
        from benchmark_avatar import trial
        row=await trial(load_cases(self.manifest)[0],args,Backend,'interrupt',0)
        self.assertEqual(closed,[True])
        self.assertEqual(row['status'],'passed')

    async def test_external_cancel_waits_for_transport_cleanup(self):
        args=self.args()
        entered=asyncio.Event(); release=asyncio.Event(); closed=[]
        class Backend:
            async def stream(self, source):
                try:
                    pcm=b''.join([chunk async for chunk in source])
                    yield {'type':'avatar_meta','sample_rate':24000,'fps':25}
                    yield {'type':'media','frame_index':0,'start_sample':0,'pts':0.,
                           'audio':base64.b64encode(pcm).decode()}
                finally:
                    entered.set()
                    await release.wait()
                    closed.append(True)
        from benchmark_avatar import trial
        task=asyncio.create_task(trial(load_cases(self.manifest)[0],args,Backend,'interrupt',0))
        await asyncio.wait_for(entered.wait(),1)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(closed,[True])

    async def test_manifest_hash_uses_the_executed_snapshot(self):
        args=self.args(); args.rounds=1; args.warmup=0; args.interrupt=False
        expected=hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        def mutate_manifest():
            self.manifest.write_text('{}')
            return {}
        class Empty:
            async def stream(self, source):
                yield {'type':'avatar_meta','sample_rate':24000,'fps':25}
        with patch('benchmark_avatar.provenance', side_effect=mutate_manifest):
            report=await run_benchmark(args,backend_factory=Empty)
        self.assertEqual(report['manifest_sha256'], expected)

    async def test_manifest_error_also_invalidates_prior_success(self):
        args=self.args(); args.output.mkdir()
        (args.output/'results.json').write_text('{"status":"passed"}')
        self.manifest.write_text('{}')
        with self.assertRaises(ValueError): await run_benchmark(args)
        self.assertEqual(json.loads((args.output/'results.json').read_text())['status'],'failed')


if __name__ == '__main__': unittest.main()
