"""Real tiny capture fixtures exercise review trust boundaries without models."""
import base64
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer
import numpy as np
from PIL import Image
from avatar_eval_capture import ClipCapture


def fixture(root, kind='2d', case='zh_private', provider='secret-provider'):
    path = root / provider / case
    pcm = b'\0\0' * 1920
    capture = ClipCapture(path, pcm)
    meta = dict(type='avatar_meta', kind=kind, fps=25, sample_rate=24000)
    if kind == '2d':
        meta.update(codec='jpeg', width=8, height=6)
        image = io.BytesIO()
        Image.new('RGB', (8, 6), 'red').save(image, format='JPEG')
        visual = dict(image=base64.b64encode(image.getvalue()).decode())
    else:
        meta.update(vertex_count=3, faces=[[0, 1, 2]])
        visual = dict(vertices=base64.b64encode(np.zeros((3, 3), '<f4').tobytes()).decode())
    capture.add(meta, 0)
    for i in range(2):
        capture.add(dict(type='media', frame_index=i, pts=i / 25,
                         start_sample=i * 960, audio=base64.b64encode(pcm[i*1920:(i+1)*1920]).decode(),
                         **visual), .01 + i*.04)
    capture.add(dict(type='clip_end', total_samples=1920), .09)
    capture.finish(.1)
    return path


def edit_json(path, mutate):
    value = json.loads(path.read_text())
    mutate(value)
    path.write_text(json.dumps(value))


class ReviewLoadTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('avatar_review'), 'review service must exist')
        self.review = importlib.import_module('avatar_review')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'captures'
        self.out = Path(self.tmp.name) / 'reviews'
        self.path = fixture(self.root)

    def app(self):
        return self.review.make_app(self.root, self.out)

    def test_valid_small_capture_and_stable_key(self):
        self.app()
        key = json.loads((self.out / 'key.json').read_text())
        self.assertIn('clip-001', key)
        self.assertEqual(key['clip-001']['provider'], 'secret-provider')
        self.app()
        self.assertEqual(key, json.loads((self.out / 'key.json').read_text()))

    def test_conflicting_key_rejected(self):
        self.app()
        fixture(self.root, case='en_extra')
        with self.assertRaises(ValueError): self.app()

    def test_invalid_pcm_hash_rejected(self):
        edit_json(self.path / 'manifest.json', lambda m: m.update(output_pcm_sha256='0'*64))
        with self.assertRaises(ValueError): self.app()

    def test_index_path_and_clock_rejected(self):
        for change in ({'artifact': '../../outside.jpg'}, {'frame_index': 8}, {'pts': .123},
                       {'n_samples': 1000}, {'payload_sha256': 'bad'}):
            with self.subTest(change=change):
                index = (self.path / 'index.json').read_text()
                edit_json(self.path / 'index.json', lambda m: m[0].update(change))
                with self.assertRaises(ValueError): self.app()
                (self.path / 'index.json').write_text(index)

    def test_symlink_frame_rejected(self):
        frame = self.path / 'frames/000000.jpg'
        outside = Path(self.tmp.name) / 'outside.jpg'
        outside.write_bytes(frame.read_bytes())
        frame.unlink()
        frame.symlink_to(outside)
        with self.assertRaises(ValueError): self.app()

    def test_invalid_topology_and_huge_npy_header_rejected(self):
        path = fixture(self.root, '3d', 'en_mesh')
        np.save(path / 'topology.npy', np.array([[0, 1, 3]], '<i4'))
        with self.assertRaises(ValueError): self.app()
        np.save(path / 'topology.npy', np.array([[0, 1, 2]], '<i4'))
        with (path / 'frames/000000.npy').open('wb') as stream:
            np.lib.format.write_array_header_1_0(stream, dict(descr='<f4', fortran_order=False, shape=(10**10, 3)))
        with self.assertRaises(ValueError): self.app()

    def test_decoded_memory_limit(self):
        edit_json(self.path / 'metadata.json', lambda m: m.update(width=2048, height=2048))
        edit_json(self.path / 'manifest.json', lambda m: m.update(n_frames=30))
        index = json.loads((self.path / 'index.json').read_text())
        frames = []
        for i in range(30):
            frames.append(dict(index[0], frame_index=i, start_sample=i*960, pts=i/25, artifact=f'frames/{i:06d}.jpg'))
        (self.path / 'index.json').write_text(json.dumps(frames))
        with self.assertRaises(ValueError): self.app()


class ReviewHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(importlib.util.find_spec('avatar_review'), 'review service must exist')
        self.review = importlib.import_module('avatar_review')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'captures'
        self.out = Path(self.tmp.name) / 'reviews'
        self.p2 = fixture(self.root)
        self.p3 = fixture(self.root, '3d', 'en_private', 'hidden-mesh')
        self.client = TestClient(TestServer(self.review.make_app(self.root, self.out)))
        self.addAsyncCleanup(self.client.close)
        await self.client.start_server()
        self.headers = {'Host': '127.0.0.1:18426', 'Origin': 'http://127.0.0.1:18426'}
        self.clips = (await (await self.get('/api/clips')).json())['clips']

    async def get(self, path, **kwargs):
        return await self.client.get(path, headers=kwargs.pop('headers', self.headers), **kwargs)

    async def post(self, body, **kwargs):
        return await self.client.post('/api/annotations', json=body,
                                      headers=kwargs.pop('headers', self.headers), **kwargs)

    def annotation(self, kind='2d'):
        clip = next(c for c in self.clips if c['kind'] == kind)
        return dict(clipId=clip['id'], fingerprint=clip['fingerprint'], reviewer='rater-1',
                    reviewerKind='human', dimension='timing', start=0, end=.08,
                    severity=2, notes='visible delay', reviewed=True)

    async def test_anonymous_api_and_media(self):
        self.assertEqual(len(self.clips), 2)
        self.assertEqual({c['category'] for c in self.clips}, {'zh', 'en'})
        for clip in self.clips:
            self.assertIsInstance(clip['frames'], int)
            detail = await (await self.get('/api/clips/' + clip['id'])).json()
            self.assertEqual(detail['sampleRate'], 24000)
            for secret in ('secret-provider', 'private', 'hidden-mesh', str(self.root), 'artifact', 'roles'):
                self.assertNotIn(secret, json.dumps(detail))
            self.assertEqual(len(detail['frames']), 2)
            self.assertEqual((await self.get(detail['audioUrl'])).status, 200)
            response = await self.get(detail['frames'][0]['url'])
            self.assertEqual(response.status, 200)
            raw = await response.read()
            if clip['kind'] == '3d':
                self.assertEqual(len(raw), 36)
                self.assertEqual(detail['faces'], [[0, 1, 2]])
            else:
                self.assertEqual(Image.open(io.BytesIO(raw)).size, (8, 6))

    async def test_host_origin_and_unknown_paths(self):
        for host in ('localhost:18426', 'evil.test', '127.0.0.1:9999'):
            self.assertEqual((await self.get('/api/clips', headers={'Host': host})).status, 403)
        for origin in (None, 'http://evil.test', 'http://127.0.0.1:9999'):
            headers = {'Host': self.headers['Host']}
            if origin: headers['Origin'] = origin
            self.assertEqual((await self.post(self.annotation(), headers=headers)).status, 403)
        self.assertEqual((await self.get('/api/clips/../../key.json')).status, 404)
        self.assertEqual((await self.get('/key.json')).status, 404)

    async def test_annotation_validation_and_persistence(self):
        body = self.annotation()
        response = await self.post(body)
        self.assertEqual(response.status, 201)
        saved = await response.json()
        self.assertEqual(saved['reviewerKind'], 'human')
        self.assertIn('id', saved)
        self.assertIn('createdAt', saved)
        files = list(self.out.glob('annotation-*.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text()), saved)
        self.assertEqual((await (await self.get('/api/annotations')).json())['annotations'], [saved])
        restarted = TestClient(TestServer(self.review.make_app(self.root, self.out)))
        self.addAsyncCleanup(restarted.close)
        await restarted.start_server()
        response = await restarted.get('/api/annotations', headers=self.headers)
        self.assertEqual((await response.json())['annotations'], [saved])
        changes = [{'reviewed': False}, {'fingerprint': '0'*64}, {'start': -1}, {'end': .09},
                   {'start': .08}, {'severity': True}, {'severity': 4}, {'reviewer': ''},
                   {'notes': 'x'*4097}, {'dimension': 'bogus'}, {'reviewerKind': 'robot'}, {'start': float('nan')}]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual((await self.post(dict(body, **change))).status, 400)
        automated = dict(body, reviewerKind='automated')
        self.assertEqual((await (await self.post(automated)).json())['reviewerKind'], 'automated')
        self.assertEqual((await self.post(dict(self.annotation('3d'), dimension='appearance'))).status, 400)

    async def test_changed_media_rejected_without_paths(self):
        for clip in self.clips:
            detail = await (await self.get('/api/clips/' + clip['id'])).json()
            path = self.p2 if clip['kind'] == '2d' else self.p3
            target = path / ('frames/000000.jpg' if clip['kind'] == '2d' else 'frames/000000.npy')
            if clip['kind'] == '2d': target.write_bytes(target.read_bytes() + b'changed')
            else: np.save(target, np.ones((3, 3), '<f4'))
            response = await self.get(detail['frames'][0]['url'])
            self.assertEqual(response.status, 409)
            self.assertNotIn(str(path), await response.text())
            wav = path / 'output.wav'
            wav.write_bytes(wav.read_bytes() + b'changed')
            self.assertEqual((await self.get(detail['audioUrl'])).status, 409)

    async def test_unicode_annotation_survives_restart(self):
        body = dict(self.annotation(), notes='😀' * 3000)
        response = await self.client.post(
            '/api/annotations', data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
            headers=dict(self.headers, **{'Content-Type': 'application/json'}))
        self.assertEqual(response.status, 201)
        saved = await response.json()
        self.assertEqual(saved['notes'], body['notes'])
        restarted = TestClient(TestServer(self.review.make_app(self.root, self.out)))
        self.addAsyncCleanup(restarted.close)
        await restarted.start_server()
        response = await restarted.get('/api/annotations', headers=self.headers)
        self.assertEqual((await response.json())['annotations'], [saved])

    async def test_oversized_json_and_unknown_annotation_fields(self):
        response = await self.client.post('/api/annotations', data='x'*17000,
                                         headers=dict(self.headers, **{'Content-Type': 'application/json'}))
        self.assertEqual(response.status, 413)
        self.assertEqual((await self.post(dict(self.annotation(), provider='injected'))).status, 400)

    async def test_runtime_symlink_cannot_escape_root(self):
        clip = next(c for c in self.clips if c['kind'] == '2d')
        detail = await (await self.get('/api/clips/' + clip['id'])).json()
        frame = self.p2 / 'frames/000000.jpg'
        outside = Path(self.tmp.name) / 'outside.jpg'
        outside.write_bytes(frame.read_bytes())
        frame.unlink()
        frame.symlink_to(outside)
        self.assertEqual((await self.get(detail['frames'][0]['url'])).status, 409)

    async def test_changed_topology_rejected(self):
        clip = next(c for c in self.clips if c['kind'] == '3d')
        np.save(self.p3 / 'topology.npy', np.array([[2, 1, 0]], '<i4'))
        self.assertEqual((await self.get('/api/clips/' + clip['id'])).status, 409)


if __name__ == '__main__':
    unittest.main()
