import http.server
import json
from pathlib import Path
import subprocess
import threading
import time
import unittest

import container_trial


class HealthProbeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(container_trial, 'probe_health'), 'wall-bounded health probe missing')
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path == '/slow':
                    self.send_response(200); self.send_header('Content-Length', '200'); self.end_headers()
                    try:
                        for _ in range(200):
                            self.wfile.write(b' '); self.wfile.flush(); time.sleep(.05)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                payload = b' ' * 65537 if self.path == '/large' else json.dumps(
                    {'ready': True, 'active_sessions': 0} if self.path == '/app'
                    else {'ready': True, 'busy': False}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers()
                self.wfile.write(payload)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def test_normal_idle_endpoints_pass(self):
        container_trial.probe_health([self.url+'/app',self.url+'/model'])

    def test_trickled_body_hits_total_deadline(self):
        started=time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            container_trial.probe_health([self.url+'/slow',self.url+'/model'],timeout=.25)
        self.assertLess(time.monotonic()-started,1.5)

    def test_oversized_body_rejected(self):
        with self.assertRaises(subprocess.CalledProcessError):
            container_trial.probe_health([self.url+'/large',self.url+'/model'])


if __name__ == '__main__':
    unittest.main()
