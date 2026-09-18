import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np
from PIL import Image

from avatar_eval_capture import ClipCapture


def jpeg(color=0, size=(4, 4)):
    output = io.BytesIO()
    Image.new('RGB', size, (color, color, color)).save(output, format='JPEG')
    return output.getvalue()


def metadata(kind='2d', **changes):
    result = dict(type='avatar_meta', fps=25, sample_rate=24000)
    if kind == '2d':
        result.update(kind=kind, codec='jpeg', width=4, height=4)
    else:
        result.update(vertex_count=3, faces=[[0, 1, 2]])
    result.update(changes)
    return result


def media(pcm, visual, index=0, start=0, kind='2d', **changes):
    event = dict(type='media', frame_index=index, start_sample=start, pts=start / 24000,
                 audio=base64.b64encode(pcm).decode())
    event['image' if kind == '2d' else 'vertices'] = base64.b64encode(visual).decode()
    event.update(changes)
    return event


class ClipCaptureTests(unittest.TestCase):
    pcm = b'\x01\x00\x02\x00\x03\x00'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'capture'

    def capture(self, **kwargs):
        capture = ClipCapture(self.output, self.pcm, **kwargs)
        manifest = self.output / 'manifest.json'
        self.addCleanup(lambda: capture.fail('TestCleanup')
                        if json.loads(manifest.read_text())['status'] == 'running' else None)
        return capture

    def read_json(self, name):
        return json.loads((self.output / name).read_text())

    def complete(self, capture):
        capture.add(dict(type='clip_end', total_samples=3), .4)
        self.assertEqual(self.read_json('manifest.json')['status'], 'running')
        return capture.finish(.5)

    def test_2d_preserves_original_payload_pcm_and_index(self):
        capture = self.capture()
        capture.add(metadata(url='http://private', token='secret'), 0)
        raw = jpeg()
        capture.add(media(self.pcm[:4], raw), .1)
        capture.add(media(self.pcm[4:], raw, index=1, start=2), .2)
        report = self.complete(capture)
        self.assertEqual(report, self.read_json('manifest.json'))
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['n_frames'], 2)
        self.assertTrue(report['pcm_exact'])
        self.assertEqual(report['output_pcm_sha256'], hashlib.sha256(self.pcm).hexdigest())
        self.assertEqual((self.output / 'frames/000000.jpg').read_bytes(), raw)
        with wave.open(str(self.output / 'output.wav'), 'rb') as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (24000, 1, 2))
            self.assertEqual(wav.readframes(3), self.pcm)
        index = self.read_json('index.json')
        self.assertEqual(index[1]['start_sample'], 2)
        self.assertEqual(index[1]['n_samples'], 1)
        self.assertEqual(index[1]['pts'], 2 / 24000)
        self.assertEqual(index[0]['payload_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertNotIn('secret', (self.output / 'metadata.json').read_text())
        self.assertNotIn('http', (self.output / 'metadata.json').read_text())
        self.assertEqual(report['continuity']['adjacent_pixel_mae_0_1'], dict(pairs=1, mean=0.0, max=0.0))
        self.assertEqual(report['continuity']['exact_repeated_frame_fraction'], 1.0)
        self.assertFalse(report['perceptual_quality_measured'])
        self.assertFalse(report['lip_sync_measured'])

    def test_changed_pixels_have_normalized_mean_absolute_delta(self):
        capture = self.capture()
        capture.add(metadata(), 0)
        capture.add(media(self.pcm[:2], jpeg(0)), .1)
        capture.add(media(self.pcm[2:], jpeg(255), index=1, start=1), .2)
        report = self.complete(capture)
        self.assertEqual(report['continuity']['adjacent_pixel_mae_0_1'], dict(pairs=1, mean=1.0, max=1.0))
        self.assertEqual(report['continuity']['exact_repeated_frame_fraction'], 0.0)

    def test_mesh_preserves_float32_topology_and_displacement(self):
        capture = self.capture()
        capture.add(metadata('3d'), 0)
        first = np.zeros((3, 3), dtype='<f4')
        second = first.copy()
        second[:, 0] = 2
        capture.add(media(self.pcm[:2], first.tobytes(), kind='3d'), .1)
        capture.add(media(self.pcm[2:], second.tobytes(), index=1, start=1, kind='3d'), .2)
        report = self.complete(capture)
        np.testing.assert_array_equal(np.load(self.output / 'frames/000001.npy'), second)
        np.testing.assert_array_equal(np.load(self.output / 'topology.npy'), [[0, 1, 2]])
        self.assertEqual(np.load(self.output / 'frames/000001.npy').dtype, np.dtype('<f4'))
        self.assertEqual(report['continuity']['adjacent_vertex_rms_displacement'], dict(pairs=1, mean=2.0, max=2.0))

    def test_single_frame_has_no_adjacent_observations(self):
        capture = self.capture()
        capture.add(metadata(), 0)
        capture.add(media(self.pcm, jpeg()), .1)
        report = self.complete(capture)
        self.assertEqual(report['continuity']['adjacent_pixel_mae_0_1'], dict(pairs=0, mean=None, max=None))
        self.assertIsNone(report['continuity']['exact_repeated_frame_fraction'])

    def test_existing_directory_is_never_reused(self):
        self.output.mkdir()
        sentinel = self.output / 'manifest.json'
        sentinel.write_text('{"status":"passed"}')
        with self.assertRaises(FileExistsError):
            ClipCapture(self.output, self.pcm)
        self.assertEqual(sentinel.read_text(), '{"status":"passed"}')

    def test_input_and_frame_configuration_bounds(self):
        for pcm in (b'', b'x', b'\x00\x00' * (30 * 24000 + 1)):
            with self.subTest(pcm_size=len(pcm)), self.assertRaises(ValueError):
                ClipCapture(self.output, pcm)
        for limit in (0, -1, True, 1802, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                ClipCapture(self.output, self.pcm, max_frames=limit)
        self.assertFalse(self.output.exists())

    def test_bad_visuals_fail_without_writing_partial_frame(self):
        invalid = [media(self.pcm, b'not jpeg'), media(self.pcm, jpeg()[:100]),
                   media(self.pcm, jpeg(size=(5, 4))),
                   media(self.pcm, jpeg(), image='a' * 1_400_001),
                   media(self.pcm, jpeg(), image='%%%')]
        for index, event in enumerate(invalid):
            with self.subTest(index=index):
                self.output = Path(self.temp.name) / str(index)
                capture = self.capture()
                capture.add(metadata(), 0)
                with self.assertRaises(ValueError):
                    capture.add(event, .1)
                self.assertEqual(self.read_json('manifest.json')['status'], 'failed')
                self.assertEqual(list((self.output / 'frames').iterdir()), [])

    def test_invalid_mesh_and_metadata_are_rejected(self):
        invalid_meta = [metadata(width=2049), metadata(fps=True), metadata(fps=61),
                        metadata('3d', vertex_count=20001), metadata('3d', faces=[[0, 1, 3]]),
                        metadata('3d', faces=[[0, True, 2]]), metadata('3d', faces=[[0, 1]]),
                        dict(metadata(), kind='bad')]
        for index, event in enumerate(invalid_meta):
            with self.subTest(index=index):
                self.output = Path(self.temp.name) / f'meta-{index}'
                capture = self.capture()
                with self.assertRaises(ValueError):
                    capture.add(event, 0)
                self.assertEqual(self.read_json('manifest.json')['status'], 'failed')
        for index, raw in enumerate((b'123', np.full((3, 3), np.nan, '<f4').tobytes(), b'\0' * 40)):
            with self.subTest(index=index):
                self.output = Path(self.temp.name) / f'mesh-{index}'
                capture = self.capture()
                capture.add(metadata('3d'), 0)
                with self.assertRaises(ValueError):
                    capture.add(media(self.pcm, raw, kind='3d'), .1)
                self.assertEqual(list((self.output / 'frames').iterdir()), [])

    def test_frame_limit_fails_and_keeps_verified_prefix(self):
        capture = self.capture(max_frames=1)
        capture.add(metadata(), 0)
        capture.add(media(self.pcm[:2], jpeg()), .1)
        with self.assertRaises(ValueError):
            capture.add(media(self.pcm[2:], jpeg(), index=1, start=1), .2)
        self.assertEqual(self.read_json('manifest.json')['status'], 'failed')
        self.assertEqual(len(self.read_json('index.json')), 1)

    def test_no_end_and_invalid_pcm_never_pass(self):
        capture = self.capture()
        capture.add(metadata(), 0)
        capture.add(media(self.pcm, jpeg()), .1)
        with self.assertRaises(ValueError):
            capture.finish(.2)
        self.assertEqual(self.read_json('manifest.json')['status'], 'failed')
        with self.assertRaises(ValueError):
            capture.add(dict(type='clip_end', total_samples=3), .3)
        self.output = Path(self.temp.name) / 'bad-pcm'
        capture = self.capture()
        capture.add(metadata(), 0)
        with self.assertRaises(ValueError):
            capture.add(media(b'\x00\x00', jpeg()), .1)
        self.assertEqual(list((self.output / 'frames').iterdir()), [])

    def test_explicit_failure_sanitizes_error_and_closes_wav(self):
        capture = self.capture()
        capture.add(metadata(), 0)
        capture.add(media(self.pcm[:2], jpeg()), .1)
        capture.fail('http://private?secret=token')
        report = self.read_json('manifest.json')
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['error_type'], 'Error')
        self.assertNotIn('http', json.dumps(report))
        with wave.open(str(self.output / 'output.wav'), 'rb') as wav:
            self.assertEqual(wav.readframes(3), self.pcm[:2])

    def test_truncated_pcm_and_events_after_clip_end_are_failed(self):
        capture = self.capture()
        capture.add(metadata(), 0)
        capture.add(media(self.pcm[:2], jpeg()), .1)
        with self.assertRaises(ValueError):
            capture.add(dict(type='clip_end', total_samples=3), .2)
        self.assertEqual(self.read_json('manifest.json')['status'], 'failed')
        self.output = Path(self.temp.name) / 'after-end'
        capture = self.capture()
        capture.add(metadata(), 0)
        capture.add(media(self.pcm, jpeg()), .1)
        capture.add(dict(type='clip_end', total_samples=3), .2)
        with self.assertRaises(ValueError):
            capture.add(media(self.pcm, jpeg()), .3)
        self.assertEqual(self.read_json('manifest.json')['status'], 'failed')
        self.assertEqual(len(list((self.output / 'frames').iterdir())), 1)

    def test_large_finite_mesh_values_keep_diagnostics_finite(self):
        capture = self.capture()
        capture.add(metadata('3d'), 0)
        first = np.full((3, 3), np.finfo(np.float32).max, dtype='<f4')
        second = -first
        capture.add(media(self.pcm[:2], first.tobytes(), kind='3d'), .1)
        capture.add(media(self.pcm[2:], second.tobytes(), index=1, start=1, kind='3d'), .2)
        report = self.complete(capture)
        self.assertTrue(np.isfinite(report['continuity']['adjacent_vertex_rms_displacement']['mean']))

    def test_metadata_and_frame_timing_errors_persist_failed_status(self):
        for index, event in enumerate((metadata(), media(self.pcm, jpeg(), pts=1),
                                       media(self.pcm, jpeg(), frame_index=2))):
            self.output = Path(self.temp.name) / f'event-{index}'
            capture = self.capture()
            capture.add(metadata(), 0)
            with self.assertRaises(ValueError):
                capture.add(event, .1)
            self.assertEqual(self.read_json('manifest.json')['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
