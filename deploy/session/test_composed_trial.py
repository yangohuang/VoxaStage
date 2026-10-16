import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


class TrialTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('composed_trial.py')
        self.assertTrue(path.exists(), 'three-container controller missing')
        spec = importlib.util.spec_from_file_location('composed_trial', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = []
        self.created = {}
        self.fail_create = None
        self.fail_stop = None
        self.busy = False
        self.config = dict(images={name:'sha256:'+str(i)*64 for i,name in enumerate(('llm','asr','app'),1)},
                           models={'llm':str(self.root),'asr':str(self.root)},
                           ports={'llm':23711,'asr':23713,'app':23714},
                           daily_health=['http://127.0.0.1:25001/health','http://127.0.0.1:25002/health','http://127.0.0.1:25003/health'],
                           tts_url='http://127.0.0.1:25004',avatar_url='ws://127.0.0.1:25005/v1/stream',
                           male_speaker='fixture-male',minimum_free_mib=16384)
        self.manifest = {'cases':[{'case':'one','kind':'text','text':'你好','timeout_s':2}],
                         'payload_sha256':{},'models':{}}

    def invoke(self, cmd):
        self.calls.append(cmd)
        if cmd[0]=='nvidia-smi':return '18000\n'
        if cmd[1]=='create':
            name=cmd[cmd.index('--name')+1];role=name.split('-')[-1]
            cid=str(len(self.created)+1)*64
            self.created[cid]={'Id':cid,'Name':name,'Image':self.config['images'][role],
                'Config':{'Labels':{'org.voxastage.validation-run':cmd[cmd.index('--label')+1].split('=',1)[1]}},
                'State':{'Running':False,'ExitCode':0}}
            if self.fail_create==role:raise subprocess.TimeoutExpired(cmd,1)
            return cid
        if cmd[1:3]==['container','inspect']:
            rows=[row for row in self.created.values() if cmd[-1] in (row['Id'],row['Name'])]
            if not rows:raise subprocess.CalledProcessError(1,cmd,stderr='No such container')
            return json.dumps(rows)
        if cmd[1]=='stop' and cmd[-1]==self.fail_stop:raise RuntimeError('stop_failed')
        if cmd[1]=='rm':self.created.pop(cmd[-1])
        if cmd[1]=='logs':return 'fixture worker log'
        return ''

    def trial(self):
        t=self.module.ComposedTrial(self.config,self.manifest,self.root,self.root/'out',invoke=self.invoke)
        def probe():
            if self.busy:raise RuntimeError('daily_busy')
        t.probe=probe
        t.verify_all=lambda:None
        t.check_ports=lambda:None
        t.external_check=lambda:None
        t.wait_ready=lambda role:None
        t.protocol=lambda:None
        return t

    def test_success_starts_three_and_removes_in_reverse_order(self):
        state=self.trial().run()
        self.assertEqual(state['status'],'passed',state)
        self.assertEqual(len([c for c in self.calls if c[1]=='start']),3)
        self.assertEqual([c[-1] for c in self.calls if c[1]=='rm'],['3'*64,'2'*64,'1'*64])
        self.assertTrue(all(row['removed'] for row in state['units'].values()))

    def test_second_create_timeout_cleans_discovered_container_and_first(self):
        self.fail_create='asr'
        state=self.trial().run()
        self.assertEqual(state['status'],'failed')
        self.assertEqual([c[-1] for c in self.calls if c[1]=='start'],['1'*64])
        self.assertEqual([c[-1] for c in self.calls if c[1]=='rm'],['2'*64,'1'*64])

    def test_busy_after_create_cannot_start_that_container(self):
        t=self.trial();invoke=t.invoke
        def race(cmd):
            value=invoke(cmd)
            if cmd[1]=='create':self.busy=True
            return value
        t.invoke=race
        state=t.run()
        self.assertEqual(state['status'],'failed')
        self.assertFalse(any(c[1]=='start' for c in self.calls))
        self.assertEqual(len([c for c in self.calls if c[1]=='rm']),1)

    def test_cleanup_failure_does_not_skip_other_units(self):
        self.fail_stop='3'*64
        state=self.trial().run()
        self.assertEqual(state['status'],'failed')
        self.assertEqual([c[-1] for c in self.calls if c[1]=='rm'],['2'*64,'1'*64])
        self.assertTrue(state['cleanup_errors'])

    def test_log_read_failure_does_not_prevent_removing_stopped_container(self):
        t=self.trial();invoke=t.invoke
        def fail_log(cmd):
            if cmd[1]=='logs':raise RuntimeError('log_read_failed')
            return invoke(cmd)
        t.invoke=fail_log
        state=t.run()
        self.assertEqual(state['status'],'failed')
        self.assertTrue(all(row['removed'] for row in state['units'].values()))

    def test_application_has_no_gpu_and_only_loopback_listener(self):
        t=self.trial();t.run()
        creations=[c for c in self.calls if c[1]=='create']
        app=creations[-1]
        self.assertNotIn('--gpus',app)
        self.assertEqual(app[app.index('--host')+1],'127.0.0.1')
        for cmd in creations:
            self.assertIn('--read-only',cmd)
            self.assertEqual(cmd[cmd.index('--network')+1],'host')
        self.assertIn('/opt/app/runtime:rw,nosuid,size=64m,uid=65532,gid=65532',app)

    def test_insufficient_memory_prevents_creation(self):
        t=self.trial();invoke=t.invoke
        t.invoke=lambda cmd:'8000\n' if cmd[0]=='nvidia-smi' else invoke(cmd)
        state=t.run()
        self.assertEqual(state['status'],'failed')
        self.assertFalse(any(c[1]=='create' for c in self.calls))

    def test_gpu_occupancy_during_create_prevents_model_start(self):
        t=self.trial();invoke=t.invoke
        def changed(cmd):
            if cmd[0]=='nvidia-smi' and self.created:return '4000\n'
            return invoke(cmd)
        t.invoke=changed
        state=t.run()
        self.assertEqual(state['status'],'failed')
        self.assertFalse(any(c[1]=='start' for c in self.calls))

    def test_asr_start_rechecks_remaining_memory_after_llm_load(self):
        t=self.trial();invoke=t.invoke
        def changed(cmd):
            if cmd[0]=='nvidia-smi' and len(self.created)==2:return '4000\n'
            return invoke(cmd)
        t.invoke=changed
        state=t.run()
        self.assertEqual(state['status'],'failed')
        self.assertEqual([c[-1] for c in self.calls if c[1]=='start'],['1'*64])

    def test_payload_hash_mismatch_before_any_start(self):
        t=self.trial()
        (self.root/'bad.py').write_text('changed')
        t.frozen_manifest['payload_sha256']={'bad.py':'0'*64}
        with self.assertRaisesRegex(RuntimeError,'payload_hash'):
            t.verify_payload()

    def test_host_network_configuration_rejects_external_and_colliding_ports(self):
        for field,value in [('tts_url','http://example.com'),('ports',{'llm':23711,'asr':23711,'app':23714})]:
            with self.subTest(field=field):
                config=dict(self.config);config[field]=value
                with self.assertRaises(ValueError):
                    self.module.ComposedTrial(config,self.manifest,self.root,self.root/'out',invoke=self.invoke)

    def test_listener_must_belong_to_container_pid_one_and_bind_loopback(self):
        row='0: 0100007F:5CA2 00000000:0000 0A 0 0 0 65532 0 12345'
        self.assertTrue(self.module.listener_owned([row],{'socket:[12345]'},23714))
        self.assertFalse(self.module.listener_owned([row],{'socket:[99999]'},23714))
        self.assertFalse(self.module.listener_owned([row.replace('0100007F','00000000')],{'socket:[12345]'},23714))

    def test_keyboard_interrupt_still_cleans_every_created_unit(self):
        t=self.trial()
        def interrupted():raise KeyboardInterrupt('fixture')
        t.protocol=interrupted
        state=t.run()
        self.assertEqual(state['status'],'failed')
        self.assertEqual(state['error_type'],'KeyboardInterrupt')
        self.assertTrue(all(row['removed'] for row in state['units'].values()))

    def test_changed_ownership_refuses_mutation_but_cleans_other_units(self):
        t=self.trial()
        def change_owner():
            self.created['3'*64]['Config']['Labels']['org.voxastage.validation-run']='someone-else'
        t.protocol=change_owner
        state=t.run()
        self.assertEqual(state['status'],'failed')
        self.assertNotIn('3'*64,[c[-1] for c in self.calls if c[1] in ('stop','rm')])
        self.assertEqual([c[-1] for c in self.calls if c[1]=='rm'],['2'*64,'1'*64])

    def verified_payload(self):
        t=self.trial();t.output.mkdir()
        for name in t.sources:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes((self.module.ROOT/name).read_bytes())
        (self.root/'deploy/session/cases.json').write_text(json.dumps({'cases':self.manifest['cases']}))
        self.manifest['payload_sha256']={str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.root.rglob('*') if p.is_file()}
        for role,count in [('llm',10),('asr',12)]:
            directory=self.root/role;directory.mkdir();self.config['models'][role]=str(directory)
            files={}
            for index in range(count):
                path=directory/f'{index}.bin';path.write_bytes(bytes([index]))
                files[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
            self.manifest['models'][role]={'revision':'fixture-revision','files':files}
        (self.root/'manifest.json').write_text(json.dumps(self.manifest))
        return t

    def test_all_model_files_are_verified_and_later_mutation_is_rejected(self):
        t=self.verified_payload()
        self.module.ComposedTrial.verify_all(t)
        self.assertEqual(len(t.state['model_hash_checks'][0]['asr']),12)
        (self.root/'asr/11.bin').write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError,'model_hash_mismatch'):
            self.module.ComposedTrial.verify_all(t)

    def test_model_symlink_cannot_escape_read_only_mount(self):
        t=self.verified_payload()
        path=self.root/'llm/0.bin';path.unlink();path.symlink_to(self.root/'asr/0.bin')
        with self.assertRaisesRegex(RuntimeError,'unsafe_manifest_path'):
            self.module.ComposedTrial.verify_all(t)

    def test_payload_source_must_match_running_controller(self):
        t=self.verified_payload()
        name='deploy/session/composed_trial.py'
        (self.root/name).write_text('# altered controller')
        self.manifest['payload_sha256'][name]=hashlib.sha256((self.root/name).read_bytes()).hexdigest()
        with self.assertRaisesRegex(RuntimeError,'payload_running_source_mismatch'):
            t.verify_payload()


if __name__=='__main__':unittest.main()
