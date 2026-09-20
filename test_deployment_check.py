"""Deployment diagnostics run without model packages or model inference."""
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'runtime').mkdir()

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('deployment_check'),
                             'The standard-library deployment diagnostics module is missing')
        import deployment_check
        return deployment_check

    def write(self, name, value):
        (self.root / name).write_text(value)

    def test_environment_precedence_literal_and_no_process_mutation(self):
        self.write('runtime/backend-env.json', json.dumps({'PIPECAT_X': 'runtime', 'PIPECAT_Y': 'runtime'}))
        self.write('.env', '# comment\nexport PIPECAT_X="file"\nPIPECAT_Y=file\nLITERAL=\'$(touch /tmp/never) ${HOME}\\n\'\n')
        environ = {'PIPECAT_X': 'process'}
        result = self.module().build_environment(self.root, environ=environ)
        self.assertEqual(result['PIPECAT_X'], 'process')
        self.assertEqual(result['PIPECAT_Y'], 'file')
        self.assertEqual(result['LITERAL'], '$(touch /tmp/never) ${HOME}\\n')
        self.assertEqual(environ, {'PIPECAT_X': 'process'})

    def test_local_service_bases_are_normalized_for_both_check_and_launch(self):
        self.write('runtime/backend-env.json', json.dumps({'PIPECAT_ASR_URL': 'http://asr/base/'}))
        self.write('.env', 'PIPECAT_LLM_URL=http://llm/base///')
        env = {'PIPECAT_TTS_URL': 'http://tts/base/', 'OTHER_URL': 'http://other/'}
        result = self.module().build_environment(self.root, environ=env)
        for kind in ('ASR', 'LLM', 'TTS'):
            self.assertEqual(result['PIPECAT_' + kind + '_URL'], 'http://' + kind.lower() + '/base')
        self.assertEqual(result['OTHER_URL'], 'http://other/')
        self.assertEqual(env['PIPECAT_TTS_URL'], 'http://tts/base/')

    def test_invalid_turn_settings_block_before_probing_without_leaking_input(self):
        for key in ('PIPECAT_VAD_STOP_SECS', 'PIPECAT_TURN_WAIT_SECS'):
            for value in ('secret', 'nan', 'inf', '0', '2.1'):
                with self.subTest(key=key, value=value):
                    result = self.inspect({key: value})
                    self.assertFalse(result['ready'])
                    self.assertEqual(self.checks(result)['configuration']['status'], 'fail')
                    self.assertEqual(self.requests, [])
                    self.assertNotIn('secret', json.dumps(result))

    def test_missing_explicit_env_is_error_but_default_is_optional(self):
        module = self.module()
        self.assertEqual(module.build_environment(self.root, environ={}), {})
        with self.assertRaises(ValueError):
            module.build_environment(self.root, 'missing.env', environ={})

    def test_bad_or_oversized_environment_has_redacted_errors(self):
        module = self.module()
        for name, values in {
            'runtime/backend-env.json': ['{secret', '[]', '{"LD_PRELOAD":"secret"}', '{"PIPECAT_X":5}', ' ' * 65537],
            '.env': ['secret', '1BAD=secret', 'KEY="secret', 'X=' + 's' * 65537],
        }.items():
            for value in values:
                self.write(name, value)
                with self.subTest(name=name, value=value[:30]), self.assertRaises(ValueError) as error:
                    module.build_environment(self.root, environ={})
                self.assertNotIn('secret', str(error.exception))
            (self.root / name).unlink()

    def ready_request(self, url, *, method='GET', timeout=3):
        self.requests.append((url, method, timeout))
        if url.endswith('/api/tts/list'):
            return {'speaker_ids': ['pipecat_male', 'female11']}
        return {'ready': True, 'status': 'ready', 'capabilities': {'image_input': True}}

    def inspect(self, env=None, **kwargs):
        self.requests = []
        kwargs.setdefault('request_json', self.ready_request)
        kwargs.setdefault('tcp_check', lambda host, port, *, timeout: True)
        return self.module().inspect_deployment(self.root, env or {}, **kwargs)

    def checks(self, result):
        return {check['name']: check for check in result['checks']}

    def test_cascade_checks_only_selected_dependencies_and_missing_idle_warns(self):
        result = self.inspect({'PIPECAT_MINICPM_URL': 'http://unused.invalid'})
        self.assertTrue(result['ready'])
        self.assertEqual(result['backend'], 'cascade')
        self.assertEqual(result['profile'], 'streamingtalker')
        self.assertEqual(result['provider'], 'streamingtalker')
        self.assertEqual(len(self.requests), 4)
        self.assertFalse(any('unused' in url for url, _, _ in self.requests))
        self.assertIn(('http://127.0.0.1:19006/api/tts/list', 'POST', 3), self.requests)
        self.assertEqual(self.checks(result)['idle']['status'], 'warn')
        for item in result['checks']:
            self.assertEqual(set(item), {'name', 'status', 'required', 'message', 'help'})

    def test_profile_voice_and_endpoint_override_are_used(self):
        self.write('runtime/avatar-profiles.json', json.dumps({'version': 1, 'default_profile': 'anchor', 'profiles': [
            {'id': 'anchor', 'label': 'Anchor', 'provider': 'streamingtalker', 'voice': 'female',
             'idle_url': '/avatar/idle/anchor.mp4', 'url': 'wss://avatar.private/prefix/v1/stream'}]}))
        result = self.inspect({'PIPECAT_FEMALE_SPEAKER_ID': 'missing'})
        self.assertFalse(result['ready'])
        self.assertEqual(self.checks(result)['tts']['status'], 'fail')
        self.assertIn(('https://avatar.private/prefix/health', 'GET', 3), self.requests)

    def test_api_is_configuration_only_and_does_not_probe_external_endpoints(self):
        env = {}
        for kind in ('ASR', 'LLM'):
            env.update({f'PIPECAT_{kind}_MODE': 'api', f'PIPECAT_{kind}_API_BASE': 'https://secret.example/v1',
                        f'PIPECAT_{kind}_API_MODEL': 'secret-model', f'PIPECAT_{kind}_API_KEY': 'secret-key'})
        result = self.inspect(env)
        self.assertTrue(result['ready'])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.checks(result)['asr']['status'], 'warn')
        self.assertNotIn('secret', json.dumps(result))
        del env['PIPECAT_ASR_API_KEY']
        result = self.inspect(env)
        self.assertFalse(result['ready'])
        self.assertEqual(self.checks(result)['asr']['status'], 'fail')

    def test_minicpm_is_independent_and_vision_must_be_explicit(self):
        env = {'PIPECAT_MINICPM_URL': 'http://omni.private/base/', 'PIPECAT_ASR_MODE': 'invalid'}
        result = self.inspect(env, backend='minicpm', require_vision=True)
        self.assertTrue(result['ready'])
        self.assertEqual(len(self.requests), 2)
        self.assertIn(('http://omni.private/base/health', 'GET', 3), self.requests)
        result = self.inspect(env, backend='minicpm', require_vision=True,
                              request_json=lambda *a, **k: {'ready': True, 'status': 'ready'})
        self.assertFalse(result['ready'])
        self.assertEqual(self.checks(result)['vision']['status'], 'fail')
        self.assertFalse(self.inspect(require_vision=True)['ready'])

    def test_dinet_only_checks_tcp_and_warns_model_unverified(self):
        env = {'PIPECAT_DINET_URL': 'ws://dinet.private:19003/api/ws/live_video/person?token=secret'}
        calls = []
        def tcp(host, port, *, timeout):
            calls.append((host, port, timeout))
            return True
        result = self.inspect(env, profile='dinet', tcp_check=tcp)
        self.assertTrue(result['ready'])
        self.assertEqual(calls, [('dinet.private', 19003, 3)])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.checks(result)['avatar-model']['status'], 'warn')
        self.assertNotIn('secret', json.dumps(result))
        self.assertFalse(self.inspect(env, profile='dinet', tcp_check=lambda *a, **k: False)['ready'])

    def test_health_failures_and_exceptions_never_disclose_service_data(self):
        for response in ({'ready': 'yes', 'status': 'starting'}, ['secret'], {'error': 'secret'}, None):
            result = self.inspect(request_json=lambda *a, **k: response)
            self.assertFalse(result['ready'])
            self.assertNotIn('secret', json.dumps(result))
        def fail(*args, **kwargs):
            raise RuntimeError('https://secret.example?key=secret')
        result = self.inspect(request_json=fail)
        self.assertFalse(result['ready'])
        self.assertNotIn('secret', json.dumps(result))

    def test_flashhead_uses_http_health_and_idle_is_relative_to_root(self):
        (self.root / 'runtime/idle').mkdir()
        (self.root / 'runtime/idle/flashhead.mp4').write_bytes(b'idle')
        env = {'PIPECAT_FLASHHEAD_URL': 'ws://flash.private/base/v1/stream',
               'PIPECAT_AVATAR_PROFILE': 'dinet'}
        result = self.inspect(env, profile='flashhead')
        self.assertTrue(result['ready'])
        self.assertEqual(result['profile'], 'flashhead')
        self.assertEqual(self.checks(result)['idle']['status'], 'pass')
        self.assertIn(('http://flash.private/base/health', 'GET', 3), self.requests)
        result = self.inspect(env, profile='flashhead', request_json=lambda *a, **k: {'status': 'ready'})
        self.assertEqual(self.checks(result)['avatar']['status'], 'fail')

    def test_selection_errors_are_redacted(self):
        for kwargs in ({'backend': 'https://secret'}, {'profile': 'secret'}, {'timeout': 0}):
            result = self.inspect(**kwargs)
            self.assertFalse(result['ready'])
            self.assertNotIn('secret', json.dumps(result))

    def test_deployment_import_uses_only_standard_library(self):
        self.module()
        result = subprocess.run([sys.executable, '-S', '-c',
            'import deployment_check; import sys; assert not any(x in sys.modules for x in '
            '("numpy", "httpx", "torch", "avatar_backend", "dinet_backend", "avatar_video_backend"))'],
            cwd=Path(__file__).parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class HTTPProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def do_GET(self):
                cls.calls.append(self.path)
                status, payload = 200, b'{"ready": true}'
                if self.path == '/header-drip':
                    try:
                        for chunk in (b'HTTP/1.1 200 OK\r\n', b'Content-Type: application/json\r\n',
                                      b'Content-Length: 15\r\n', b'Connection: close\r\n\r\n', payload):
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            time.sleep(.1)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    self.close_connection = True
                    return
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/redirect-target')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if self.path == '/error':
                    status, payload = 500, b'secret error'
                elif self.path == '/html':
                    payload = b'<html>secret</html>'
                elif self.path == '/array':
                    payload = b'[]'
                elif self.path in ('/large', '/unbounded'):
                    payload = b' ' * (256 * 1024 + 1)
                elif self.path == '/slow':
                    time.sleep(.2)
                self.send_response(status)
                if self.path != '/unbounded':
                    self.send_header('Content-Length', str(len(payload) + (10 if self.path == '/truncated' else 0)))
                self.send_header('Connection', 'close')
                self.end_headers()
                try:
                    if self.path == '/drip':
                        for chunk in (b'{', b'\"ready\":', b'true}'):
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            time.sleep(.15)
                    else:
                        self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_real_http_probe_rejects_redirect_errors_nonobjects_and_unbounded_bodies(self):
        from deployment_check import _request_json
        self.assertEqual(_request_json(self.base + '/ok'), {'ready': True})
        for path in ('/redirect', '/error', '/html', '/array', '/large', '/unbounded', '/truncated'):
            with self.subTest(path=path), self.assertRaises(Exception):
                _request_json(self.base + path)
        self.assertNotIn('/redirect-target', self.calls)

    def test_slow_drip_body_cannot_extend_the_probe_deadline(self):
        from deployment_check import _request_json
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            _request_json(self.base + '/drip', timeout=.2)
        self.assertLess(time.monotonic() - started, .28)

    def test_slow_response_headers_obey_total_deadline_and_leave_no_timer(self):
        from deployment_check import _request_json
        existing = {thread.ident for thread in threading.enumerate() if isinstance(thread, threading.Timer)}
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            _request_json(self.base + '/header-drip', timeout=.2)
        self.assertLess(time.monotonic() - started, .3)
        self.assertEqual({thread.ident for thread in threading.enumerate() if isinstance(thread, threading.Timer)}, existing)

    def test_probe_has_bounded_timeout(self):
        from deployment_check import _request_json
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            _request_json(self.base + '/slow', timeout=.05)
        self.assertLess(time.monotonic() - started, .5)


if __name__ == '__main__':
    unittest.main()
