import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'llm-server/deploy'))
from container_guard import OccupancyGuard

class ASRTrialTests(unittest.TestCase):
    def setUp(self):
        p=Path(__file__).with_name('container_trial.py');self.assertTrue(p.exists(),'ASR host adapter missing')
        spec=importlib.util.spec_from_file_location('asr_host_trial',p);self.mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.mod)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.model=self.root/'model';self.model.mkdir();self.payload=self.root/'payload';self.payload.mkdir()
        old=json.loads((ROOT/'docs/benchmarks/2026-10-10-asr17-validation.json').read_text())
        files={}
        for name in old['model_files_before']:
            (self.model/name).write_bytes(name.encode());files[name]=hashlib.sha256(name.encode()).hexdigest()
        self.provenance={'model_revision':old['model_revision'],'model_files_before':files}
        self.manifest={'model_name':'Qwen3-ASR-1.7B','model_revision':old['model_revision'],'timeouts_s':{'startup':2,'request':2,'recovery':2,'overall':10},'requests':[],'payload_sha256':{}}
        code=['asr-server/deploy/container_trial.py','asr-server/deploy/protocol_client.py',
              'evaluation/asr_capture.py','evaluation/asr_scoring.py','llm-server/deploy/container_trial.py',
              'llm-server/deploy/container_guard.py','llm-server/deploy/protocol_client.py']
        for name in code:
            p=self.payload/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((ROOT/name).read_bytes())
        (self.payload/'inputs').mkdir()
        (self.payload/'inputs/cases.json').write_text(json.dumps({'cases':[{'audio':'clip.wav'}]}))
        (self.payload/'inputs/variants.json').write_text('{}')
        (self.payload/'inputs/clip.wav').write_bytes(b'fixture')
        self.manifest['payload_sha256']={str(p.relative_to(self.payload)):hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in self.payload.rglob('*') if p.is_file()}
        (self.payload/'manifest.json').write_text(json.dumps(self.manifest))
        self.out=self.root/'out';self.out.mkdir()
        self.c=self.mod.ASRController('sha256:'+'a'*64,self.model,self.manifest,self.provenance,self.out,['http://127.0.0.1:1']*3,self.payload)
        self.c.guard=OccupancyGuard(lambda:None,lambda:None)

    def test_incomplete_payload_cannot_pass_even_with_self_consistent_manifest(self):
        self.manifest['payload_sha256'].clear()
        (self.payload/'manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(RuntimeError,'payload_required'):self.c.verify_models()

    def test_different_executed_source_cannot_claim_controller_source_hash(self):
        name='asr-server/deploy/protocol_client.py'
        (self.payload/name).write_text('print("different implementation")')
        self.manifest['payload_sha256'][name]=hashlib.sha256((self.payload/name).read_bytes()).hexdigest()
        (self.payload/'manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(RuntimeError,'payload_source_binding'):self.c.verify_models()

    def test_worker_startup_is_asr_and_payload_is_readonly(self):
        args=self.c.create_args();self.assertIn('--engine',args);self.assertIn('qwen',args)
        self.assertNotIn('--quantization',args);self.assertIn('Qwen3-ASR-1.7B',args)
        self.assertIn('type=bind,src='+str(self.payload)+',dst=/trial,readonly',args)

    def test_three_idle_services_required(self):
        good=[{'ready':True,'active_sessions':0},{'ready':True,'busy':False},{'ready':True,'busy':False}]
        self.mod.validate_services(good)
        for change in ({'ready':True,'busy':True},{'ready':True},{'ready':False,'busy':False}):
            with self.assertRaises(Exception):self.mod.validate_services(good[:2]+[change])
        with self.assertRaises(ValueError):self.mod.validate_services(good[:2])

    def test_model_changes_rejected_and_hash_records_retained(self):
        self.c.verify_models();self.assertEqual(len(self.c.state['model_hash_checks'][0]),12)
        (self.model/'config.json').write_text('changed')
        with self.assertRaisesRegex(RuntimeError,'model_hash'):self.c.verify_models()

    def test_payload_path_escape_and_hash_mismatch_rejected(self):
        self.c.manifest['payload_sha256']={'../escape':'0'*64}
        with self.assertRaisesRegex(RuntimeError,'payload_path'):self.c.verify_models()
        self.c.manifest['payload_sha256']={'a.txt':'0'*64};(self.payload/'a.txt').write_text('wrong')
        with self.assertRaisesRegex(RuntimeError,'payload_hash'):self.c.verify_models()

    def test_busy_asr_aborts_real_lifecycle_before_create(self):
        self.out.rmdir();self.c.gpu_admit=lambda:None
        self.c.probe=lambda:self.mod.validate_services([{'ready':True,'active_sessions':0},{'ready':True,'busy':False},{'ready':True,'busy':True}])
        calls=[];self.c.invoke=lambda args:calls.append(args)
        result=self.c.run();self.assertEqual(result['status'],'failed');self.assertFalse(calls)

    def test_probe_uses_whole_wall_timeout_for_all_three_urls(self):
        with patch.object(self.mod.subprocess,'run') as run:
            run.return_value.stdout=json.dumps([{'ready':True,'active_sessions':0},{'ready':True,'busy':False},{'ready':True,'busy':False}])
            self.mod.probe_services(['http://127.0.0.1:1']*3)
            self.assertEqual(run.call_args.kwargs['timeout'],5)
        with self.assertRaises(ValueError):self.mod.probe_services(['https://example.com']*3)

if __name__=='__main__':unittest.main()
