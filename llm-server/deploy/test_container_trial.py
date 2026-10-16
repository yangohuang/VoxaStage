import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class TrialTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('container_trial.py')
        self.assertTrue(path.exists(), 'complete host controller missing')
        spec = importlib.util.spec_from_file_location('container_trial', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / 'trial'
        self.calls = []
        self.busy = False
        self.cid = 'a' * 64
        self.image = 'sha256:' + 'b' * 64
        self.created = False
        self.create_timeout = False
        self.stop_fail = False
        self.manifest = {'timeouts_s': {'startup': 2, 'request': 2, 'recovery': 2},
                         'requests': [{'case': 'one'}]}
        self.script = '''import json,sys
print(json.dumps({'event':'request_ready','case':'one'}),flush=True)
assert json.loads(sys.stdin.readline())=={'continue':'one'}
print(json.dumps({'event':'chunk','case':'one','text':'kept'}),flush=True)
print(json.dumps({'event':'complete','case':'one'}),flush=True)
print(json.dumps({'event':'finished','requests':1}),flush=True)
'''

    def invoke(self, command):
        self.calls.append(command)
        if command[1] == 'create':
            self.created = True
            self.label = command[command.index('--label') + 1].split('=', 1)[1]
            if self.create_timeout:
                raise subprocess.TimeoutExpired(command, 1)
            return self.cid + '\n'
        if command[1:3] == ['container', 'inspect']:
            if not self.created:
                raise subprocess.CalledProcessError(1, command, stderr='No such container')
            return json.dumps([{'Id': self.cid, 'Image': self.image,
                               'Config': {'Labels': {'org.voxastage.validation-run': self.label}}}])
        if command[1] == 'stop' and self.stop_fail:
            raise RuntimeError('injected_stop_failure')
        if command[1] == 'logs':
            return 'preserved worker log\n'
        return ''

    def controller(self):
        c = self.module.Controller(self.image, Path(self.temp.name), self.manifest,
                                   {}, self.out, [], invoke=self.invoke)
        c.verify_models = lambda: None
        c.gpu_admit = lambda: None
        def probe():
            if self.busy:
                raise RuntimeError('daily_service_busy')
        c.probe = probe
        c.exec_args = lambda: [sys.executable, '-u', '-c', self.script]
        return c

    def test_overall_deadline_bounds_individually_timely_phases(self):
        controller = self.controller()
        controller.manifest['timeouts_s']['overall'] = .05
        self.script = self.script.replace("print(json.dumps({'event':'complete'", "import time; time.sleep(.15)\nprint(json.dumps({'event':'complete'")
        result = controller.run()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_type'], 'TimeoutError')
        self.assertTrue(result['removed'])

    def test_creation_hook_controls_actual_docker_startup(self):
        controller = self.controller()
        original = controller.create_args
        def create():
            args = original()
            args[-1] = 'test-mode'
            return args
        controller.create_args = create
        result = controller.run()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(next(c for c in self.calls if c[1] == 'create')[-1], 'test-mode')

    def test_success_persists_output_and_removes_owned_container(self):
        result = self.controller().run()
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['removed'])
        self.assertIn('kept', (self.out/'events.jsonl').read_text())
        self.assertEqual(sum(c[1] == 'stop' for c in self.calls), 1)
        self.assertEqual(sum(c[1] == 'rm' for c in self.calls), 1)

    def test_numeric_user_has_name_and_writable_cache_home(self):
        self.controller().run()
        creation = next(c for c in self.calls if c[1] == 'create')
        environment = [creation[i+1] for i, item in enumerate(creation) if item == '--env']
        self.assertIn('USER=voxastage', environment)
        self.assertIn('HOME=/tmp', environment)

    def test_busy_after_hash_never_starts_container(self):
        c = self.controller()
        c.verify_models = lambda: setattr(self, 'busy', True)
        result = c.run()
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(any(x[1] in ('create', 'start') for x in self.calls))

    def test_create_timeout_discovers_and_cleans_exact_owned_id(self):
        self.create_timeout = True
        result = self.controller().run()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['removed'])
        self.assertFalse(any(x[1] == 'start' for x in self.calls))
        self.assertEqual([x[-1] for x in self.calls if x[1] in ('stop','rm')], [self.cid,self.cid])

    def test_incomplete_client_never_counts_as_pass(self):
        self.script = "print('{\"event\":\"chunk\",\"text\":\"partial\"}',flush=True)"
        result = self.controller().run()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['removed'])
        self.assertIn('partial', (self.out/'events.jsonl').read_text())

    def test_cleanup_failure_preserved_without_removal(self):
        self.stop_fail = True
        result = self.controller().run()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('injected_stop_failure', json.dumps(result))
        self.assertFalse(any(x[1] == 'rm' for x in self.calls))

    def test_occupancy_during_generation_stops_and_keeps_partial(self):
        self.script = "import time; print('{\"event\":\"chunk\",\"text\":\"partial\"}',flush=True); time.sleep(20)"
        c = self.controller()
        def probe():
            path = self.out/'events.jsonl'
            if path.exists() and path.stat().st_size:
                raise RuntimeError('daily_service_busy')
        c.probe = probe
        result = c.run()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['removed'])
        self.assertIn('partial', (self.out/'events.jsonl').read_text())

    def test_late_monitor_failure_cannot_pass(self):
        c = self.controller()
        cleanup = c.cleanup
        def inject():
            self.busy = True
            try:
                c.guard.check_now()
            except RuntimeError:
                pass
            cleanup()
        c.cleanup = inject
        result = c.run()
        self.assertEqual(result['status'], 'failed')

    def test_abort_after_final_admission_cannot_start_late(self):
        c = self.controller()
        admissions = []
        def gpu_admit():
            admissions.append(True)
            if len(admissions) == 3:
                self.busy = True
                try:
                    c.guard.check_now()
                except RuntimeError:
                    pass
        c.gpu_admit = gpu_admit
        result = c.run()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['removed'])
        self.assertFalse(any(command[1] == 'start' for command in self.calls))

    def test_term_and_hup_cleanup_in_real_process(self):
        self.assertTrue(hasattr(self.module, 'install_signal_handlers'), 'signal integration missing')
        template = '''
import json,os,signal,sys
from test_container_trial import TrialTests
import container_trial
t=TrialTests();t.setUp()
try:
 container_trial.install_signal_handlers()
 c=t.controller()
 if {during_cleanup!r}:
  invoke=c.invoke
  def injected(command):
   if command[1]=='stop':os.kill(os.getpid(),{sig})
   return invoke(command)
  c.invoke=injected
 else:
  def interrupted():
   os.kill(os.getpid(),{sig})
  c.protocol=interrupted
 result=c.run()
 assert result['removed'],result
 assert result['status']=={status!r},result
 print(json.dumps(result))
finally:t.doCleanups()
'''
        import signal
        for sig in (signal.SIGTERM, signal.SIGHUP):
            for during_cleanup in (False, True):
                with self.subTest(sig=sig, during_cleanup=during_cleanup):
                    script = template.format(sig=int(sig), during_cleanup=during_cleanup,
                                             status='passed' if during_cleanup else 'failed')
                    result = subprocess.run([sys.executable, '-u', '-c', script],
                                            cwd=Path(__file__).parent, capture_output=True,
                                            text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == '__main__':
    unittest.main()
