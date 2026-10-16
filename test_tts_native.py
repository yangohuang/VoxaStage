import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

import tts_native as native


class NativeTests(unittest.TestCase):
    def test_model_hash_manifest_rejects_tampering_and_incomplete_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); rows = []
            for name in native.REQUIRED_MODEL_FILES:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
                data = name.encode(); path.write_bytes(data)
                rows.append({'local_name': name, 'bytes': len(data),
                             'upstream_sha256': hashlib.sha256(data).hexdigest()})
            manifest = {'groups': [{'files': rows}]}
            native.verify_model_files(root, manifest)
            with self.assertRaisesRegex(ValueError, 'coverage'):
                native.verify_model_files(root, {'groups': [{'files': rows[:-1]}]})
            path = root / native.REQUIRED_MODEL_FILES[0]
            data = path.read_bytes(); path.write_bytes(b'x' * len(data))
            with self.assertRaisesRegex(ValueError, 'checksum'):
                native.verify_model_files(root, manifest)

    def test_local_bundle_requires_every_auxiliary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in native.REQUIRED_MODEL_FILES:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'fixture')
            (root / 'config.yaml').write_text('version: 2.5\ns2mel:\n  preprocess_params:\n    sr: 22050\n')
            self.assertEqual(native.validate_bundle(root), root)
            (root / 'hf_cache/campplus_cn_common.bin').unlink()
            with self.assertRaisesRegex(ValueError, 'Incomplete local model bundle'):
                native.validate_bundle(root)

    def test_wrong_version_or_output_rate_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in native.REQUIRED_MODEL_FILES:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'fixture')
            for config in ['version: 2\ns2mel: {preprocess_params: {sr: 22050}}',
                           'version: 2.5\ns2mel: {preprocess_params: {sr: 24000}}']:
                (root / 'config.yaml').write_text(config)
                with self.assertRaises(ValueError): native.validate_bundle(root)

    def test_adapter_uses_upstream_segment_stream_and_closes_generator(self):
        calls = []; closed = []
        class Model:
            def infer(self, **kwargs):
                calls.append(kwargs)
                try:
                    yield [1, 2, 3]
                    yield [4, 5, 6]
                finally: closed.append(True)
        adapter = native.NativeGenerator(Model())
        stream = adapter(Path('/male.wav'), 'hello，介绍 agent')
        self.assertEqual(next(stream), [1, 2, 3]); stream.close()
        self.assertEqual(closed, [True])
        self.assertEqual(calls[0]['spk_audio_prompt'], '/male.wav')
        self.assertEqual(calls[0]['text'], 'hello，介绍 agent')
        self.assertIs(calls[0]['stream_return'], True)
        self.assertEqual(calls[0]['max_text_tokens_per_segment'], 80)
        self.assertIsNone(calls[0]['output_path'])
        self.assertEqual(calls[0]['num_beams'], 1)

    def test_source_requires_pinned_commit_and_clean_tracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            def run(*args, **kwargs):
                command = args[0]
                from subprocess import CompletedProcess
                if '--show-toplevel' in command: return CompletedProcess(command, 0, str(root) + '\n')
                if 'rev-parse' in command: return CompletedProcess(command, 0, 'wrong-revision\n')
                self.fail('must reject before importing model')
            with patch('tts_native.subprocess.run', side_effect=run):
                with self.assertRaisesRegex(ValueError, 'revision'):
                    native.verify_source(root)


if __name__ == '__main__': unittest.main()
