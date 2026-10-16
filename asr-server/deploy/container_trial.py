"""ASR-specific adapter for the reviewed container lifecycle and event supervisor."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'llm-server/deploy'))
from container_trial import Controller, HEALTH_READER, install_signal_handlers
from container_guard import validate_idle_health


def validate_services(states):
    if len(states) != 3:
        raise ValueError('three_daily_health_states_required')
    validate_idle_health(states[0], states[1])
    validate_idle_health(states[0], states[2])


def probe_services(urls):
    if len(urls) != 3 or any(urlsplit(url).scheme != 'http' or urlsplit(url).hostname not in
                            ('127.0.0.1', 'localhost', '::1') for url in urls):
        raise ValueError('three_loopback_http_urls_required')
    result = subprocess.run([sys.executable, '-I', '-c', HEALTH_READER, json.dumps(urls)],
                            capture_output=True, text=True, check=True, timeout=5)
    validate_services(json.loads(result.stdout))


class ASRController(Controller):
    def __init__(self, image, model, manifest, provenance, output, health_urls, payload, **kwargs):
        super().__init__(image, model, manifest, provenance, output, health_urls, **kwargs)
        self.payload = Path(payload).resolve()
        self.name = 'voxastage-asr-trial-' + self.run_id
        self.state['name'] = self.name
        self.state['asr_source_sha256'] = {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [Path(__file__), Path(__file__).with_name('protocol_client.py'),
                         ROOT/'evaluation/asr_capture.py', ROOT/'evaluation/asr_scoring.py']}

    def probe(self):
        started = time.monotonic()
        try:
            probe_services(self.health_urls)
        finally:
            self.state['health_probes'] += 1
            self.state['max_health_probe_s'] = max(self.state['max_health_probe_s'], time.monotonic()-started)

    def verify_payload(self):
        for name, expected in self.manifest['payload_sha256'].items():
            relative = Path(name)
            path = self.payload/relative
            if relative.is_absolute() or '..' in relative.parts or path.is_symlink():
                raise RuntimeError('invalid_payload_path')
            if not path.resolve().is_relative_to(self.payload):
                raise RuntimeError('invalid_payload_path')
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError('payload_hash_mismatch:' + name)
        code = {'llm-server/deploy/'+name: digest for name, digest in self.state['source_sha256'].items()}
        code.update(self.state['asr_source_sha256'])
        required = set(code) | {'inputs/cases.json', 'inputs/variants.json'}
        declared = self.manifest['payload_sha256']
        if not required.issubset(declared):
            raise RuntimeError('payload_required_files_missing')
        if any(declared[name] != expected for name, expected in code.items()):
            raise RuntimeError('payload_source_binding_mismatch')
        cases = json.loads((self.payload/'inputs/cases.json').read_text())['cases']
        for case in cases:
            relative = Path(case['audio'])
            if relative.is_absolute() or '..' in relative.parts:
                raise RuntimeError('invalid_payload_path')
            if 'inputs/'+str(relative) not in declared:
                raise RuntimeError('payload_required_audio_missing')
        if json.loads((self.payload/'manifest.json').read_text()) != self.manifest:
            raise RuntimeError('payload_manifest_mismatch')

    def verify_models(self):
        if (self.manifest['model_name'] != 'Qwen3-ASR-1.7B'
                or self.provenance['model_revision'] != self.manifest['model_revision']):
            raise RuntimeError('asr_model_revision_mismatch')
        files = self.provenance['model_files_before']
        if len(files) != 12:
            raise RuntimeError('asr_model_manifest_incomplete')
        verified = {}
        for name, expected in files.items():
            if Path(name).name != name:
                raise RuntimeError('invalid_model_filename')
            digest = hashlib.sha256()
            with (self.model/name).open('rb') as source:
                for block in iter(lambda:source.read(4*1024*1024), b''):
                    self.guard.raise_if_failed()
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise RuntimeError('model_hash_mismatch:' + name)
            verified[name] = digest.hexdigest()
        self.verify_payload()
        self.state.setdefault('model_hash_checks', []).append(verified)
        self.save()

    def create_args(self):
        args = super().create_args()
        index = args.index(self.image)
        return args[:index] + ['--mount', 'type=bind,src='+str(self.payload)+',dst=/trial,readonly',
                              self.image, '--engine', 'qwen', '--model', '/model',
                              '--model-name', self.manifest['model_name'], '--port', '18315']

    def exec_args(self):
        self.verify_payload()
        return ['docker', 'exec', '-i', self.owned.container_id, '/opt/worker/bin/python', '-I', '-u',
                '/trial/asr-server/deploy/protocol_client.py', '/trial/manifest.json']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['image', 'model', 'manifest', 'provenance', 'output', 'payload',
                 'application-health', 'model-health', 'asr-health']:
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args()
    install_signal_handlers()
    result = ASRController(args.image, args.model, json.loads(Path(args.manifest).read_text()),
                           json.loads(Path(args.provenance).read_text()), args.output,
                           [args.application_health, args.model_health, args.asr_health], args.payload).run()
    print(json.dumps(result), flush=True)
    if result['status'] != 'passed': raise SystemExit(1)

if __name__ == '__main__': main()
