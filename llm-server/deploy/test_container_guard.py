import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest


class GuardTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('container_guard.py')
        self.assertTrue(path.is_file(), 'continuous occupancy guard is not implemented')
        spec = importlib.util.spec_from_file_location('container_guard', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_health_requires_explicit_ready_idle_fields(self):
        validate = self.module.validate_idle_health
        validate({'ready': True, 'active_sessions': 0}, {'ready': True, 'busy': False})
        for app, model in [
            ({'ready': True}, {'ready': True, 'busy': False}),
            ({'ready': True, 'active_sessions': False}, {'ready': True, 'busy': False}),
            ({'ready': True, 'active_sessions': 1}, {'ready': True, 'busy': False}),
            ({'ready': False, 'active_sessions': 0}, {'ready': True, 'busy': False}),
            ({'ready': True, 'active_sessions': 0}, {'ready': True}),
            ({'ready': True, 'active_sessions': 0}, {'ready': True, 'busy': True}),
            ({'ready': True, 'active_sessions': 0}, {'ready': False, 'busy': False}),
        ]:
            with self.subTest(app=app, model=model):
                with self.assertRaises(self.module.OccupancyViolation):
                    validate(app, model)

    def test_busy_initial_admission_fails_before_work(self):
        aborted = []
        def probe():
            raise RuntimeError('application_busy')
        guard = self.module.OccupancyGuard(probe, lambda: aborted.append(True))
        with self.assertRaisesRegex(self.module.OccupancyViolation, 'application_busy'):
            guard.start()
        self.assertEqual(aborted, [True])
        guard.stop()

    def test_busy_during_child_work_aborts_once_and_stays_failed(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        busy = threading.Event()
        aborted = threading.Event()
        calls = []
        def probe():
            if busy.is_set():
                raise RuntimeError('model_busy')
        def abort():
            calls.append(True)
            child.terminate()
            aborted.set()
        guard = self.module.OccupancyGuard(probe, abort, interval=.01)
        try:
            guard.start()
            busy.set()
            self.assertTrue(aborted.wait(2), 'ongoing child was not interrupted')
            child.wait(timeout=2)
            busy.clear()
            with self.assertRaisesRegex(self.module.OccupancyViolation, 'model_busy'):
                guard.check_now()
            self.assertEqual(calls, [True])
        finally:
            guard.stop()
            if child.poll() is None:
                child.kill()
            child.wait()

    def test_post_hash_readmission_does_not_wait_for_periodic_poll(self):
        busy = False
        aborted = []
        def probe():
            if busy:
                raise RuntimeError('occupied_after_hash')
        guard = self.module.OccupancyGuard(probe, lambda: aborted.append(True), interval=10)
        try:
            guard.start()
            busy = True
            with self.assertRaisesRegex(self.module.OccupancyViolation, 'occupied_after_hash'):
                guard.check_now()
            self.assertEqual(aborted, [True])
        finally:
            guard.stop()

    def test_health_transport_failure_is_not_treated_as_idle(self):
        failed = threading.Event()
        aborted = threading.Event()
        def probe():
            if failed.is_set():
                raise TimeoutError('health_timeout')
        guard = self.module.OccupancyGuard(probe, aborted.set, interval=.01)
        try:
            guard.start()
            failed.set()
            self.assertTrue(aborted.wait(2))
            with self.assertRaisesRegex(self.module.OccupancyViolation, 'health_timeout'):
                guard.raise_if_failed()
        finally:
            guard.stop()

    def test_abort_failure_is_preserved_and_does_not_allow_resume(self):
        def probe():
            raise RuntimeError('busy')
        def abort():
            raise RuntimeError('cleanup_failed')
        guard = self.module.OccupancyGuard(probe, abort)
        with self.assertRaisesRegex(self.module.OccupancyViolation, 'busy'):
            guard.start()
        self.assertIn('cleanup_failed', guard.abort_error)
        with self.assertRaises(self.module.OccupancyViolation):
            guard.check_now()
        guard.stop()

    def test_normal_stop_joins_monitor_without_aborting(self):
        aborted = []
        guard = self.module.OccupancyGuard(lambda: None, lambda: aborted.append(True), interval=.01)
        guard.start()
        guard.check_now()
        guard.stop()
        self.assertFalse(guard.running)
        self.assertEqual(aborted, [])

    def owned_fixture(self, alter=None):
        self.assertTrue(hasattr(self.module, 'OwnedContainer'), 'identity-scoped cleanup is missing')
        cid, image, run_id = 'a' * 64, 'sha256:' + 'b' * 64, 'trial-001'
        metadata = {'Id': cid, 'Image': image,
                    'Config': {'Labels': {'org.voxastage.validation-run': run_id}}}
        if alter:
            alter(metadata)
        commands = []
        def invoke(command):
            commands.append(command)
            if command[1:3] == ['container', 'inspect']:
                return json.dumps([metadata])
            return cid + '\n'
        return self.module.OwnedContainer(cid, image, run_id, invoke), commands

    def test_cleanup_rejects_foreign_id_image_or_label(self):
        for alter in [
            lambda d: d.update(Id='c' * 64),
            lambda d: d.update(Image='sha256:' + 'd' * 64),
            lambda d: d['Config']['Labels'].update({'org.voxastage.validation-run': 'other'}),
            lambda d: d['Config']['Labels'].clear(),
        ]:
            with self.subTest(alter=alter):
                owned, commands = self.owned_fixture(alter)
                with self.assertRaisesRegex(RuntimeError, 'container_identity_mismatch'):
                    owned.stop()
                self.assertFalse(any(c[1] in ('stop', 'rm') for c in commands))

    def test_ownership_requires_full_ids_and_nonempty_run_label(self):
        for cid, image, label in [
            ('a' * 64, 'sha256:' + 'b' * 64, None),
            ('a' * 64, 'sha256:' + 'b' * 64, ''),
            ('a' * 64, 'sha256:' + 'b' * 64, '  '),
            ('short-id', 'sha256:' + 'b' * 64, 'trial'),
            ('a' * 64, 'mutable-tag', 'trial'),
        ]:
            with self.subTest(cid=cid, image=image, label=label):
                commands = []
                with self.assertRaisesRegex(ValueError, 'invalid_container_ownership'):
                    self.module.OwnedContainer(cid, image, label, commands.append)
                self.assertEqual(commands, [])

    def test_concurrent_abort_and_final_cleanup_stop_only_once(self):
        owned, commands = self.owned_fixture()
        workers = [threading.Thread(target=owned.stop) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sum(c[1] == 'stop' for c in commands), 1)
        owned.remove()
        self.assertEqual(sum(c[1] == 'rm' for c in commands), 1)
        self.assertNotIn('--force', [arg for c in commands for arg in c])

    def test_remove_requires_successful_stop_and_rechecks_identity(self):
        owned, commands = self.owned_fixture()
        with self.assertRaisesRegex(RuntimeError, 'container_not_stopped'):
            owned.remove()
        self.assertEqual(commands, [])
        owned.stop()
        owned.remove()
        self.assertEqual(sum(c[1:3] == ['container', 'inspect'] for c in commands), 2)

    def test_failed_stop_is_not_recorded_as_success(self):
        owned, commands = self.owned_fixture()
        invoke = owned.invoke
        def fail_stop(command):
            if command[1] == 'stop':
                raise RuntimeError('docker_stop_failed')
            return invoke(command)
        owned.invoke = fail_stop
        with self.assertRaisesRegex(RuntimeError, 'docker_stop_failed'):
            owned.stop()
        with self.assertRaisesRegex(RuntimeError, 'container_not_stopped'):
            owned.remove()
        self.assertFalse(any(c[1] == 'rm' for c in commands))


if __name__ == '__main__':
    unittest.main()
