"""Persistence, limits, adapter validation and filesystem safety."""
import base64
import concurrent.futures
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('conversation_store')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.module.ConversationStore(self.root)

    def create(self, **kwargs):
        return self.store.create(backend='cascade', profile_id='kanghui', **kwargs)

    def test_reopen_and_new_process_recover_saved_history(self):
        first = self.create(title='中文会话')
        history = {'history': [{'role': 'user', 'content': 'Remember blue'},
                               {'role': 'assistant', 'content': 'Blue.'}]}
        transcript = [{'role': 'assistant', 'text': 'Blue.', 'status': 'heard',
                       'generated_text': 'Blue.', 'heard_text': 'Blue.'}]
        saved = self.store.save(first['id'], snapshot=history, transcript=transcript,
                                expected_revision=first['revision'])
        self.assertEqual(saved['revision'], first['revision'] + 1)
        self.assertEqual(saved['created_at'], first['created_at'])
        self.assertEqual(saved, self.module.ConversationStore(self.root).load(first['id']))
        code = ('import json,sys;from conversation_store import ConversationStore;'
                'print(json.dumps(ConversationStore(sys.argv[1]).load(sys.argv[2])))')
        output = subprocess.check_output([sys.executable, '-c', code, str(self.root), first['id']])
        self.assertEqual(json.loads(output), saved)
        summary = self.store.list()[0]
        self.assertEqual(summary['title'], '中文会话')
        self.assertNotIn('snapshot', summary)
        self.assertNotIn('transcript', summary)
        self.assertTrue(self.store.delete(first['id']))
        self.assertFalse(self.store.delete(first['id']))
        self.assertEqual(self.store.list(), [])
        with self.assertRaises(self.module.ConversationNotFound):
            self.store.load(first['id'])

    def test_minicpm_audio_image_and_interrupted_turn_roundtrip(self):
        first = self.store.create(backend='minicpm', profile_id='flashhead')
        user = {'role': 'user', 'audio': base64.b64encode(b'\x00\x00' * 20).decode(),
                'images': [{'id': 'image-1', 'source': 'camera', 'captured_at_ms': 100.5,
                            'data': base64.b64encode(b'image bytes').decode()}]}
        snapshot = {'history': [[user, {'role': 'assistant', 'text': '[Interrupted]'}],
                                [{'role': 'user', 'text': 'Next', 'images_omitted': True},
                                 {'role': 'assistant', 'text': 'OK'}]]}
        self.store.save(first['id'], snapshot=snapshot)
        self.assertEqual(self.store.load(first['id'])['snapshot'], snapshot)

    def test_invalid_ids_metadata_and_unbounded_or_unknown_shapes_rejected(self):
        first = self.create()
        for bad in ('../x', '', 'a' * 31, 'A' * 32, "' OR 1=1 --", None):
            with self.subTest(id=bad), self.assertRaises(self.module.InvalidConversation):
                self.store.load(bad)
            with self.assertRaises(self.module.InvalidConversation):
                self.store.delete(bad)
        for metadata in ({'backend': 'unknown', 'profile_id': 'valid'},
                         {'backend': 'cascade', 'profile_id': '../escape'},
                         {'backend': 'cascade', 'profile_id': 'valid', 'title': 'x' * 201}):
            with self.assertRaises(self.module.InvalidConversation):
                self.store.create(**metadata)
        nested = []
        nested.append(nested)
        for snapshot in ({'history': nested}, {'history': [], 'url': 'http://evil'},
                         {'history': [{'role': 'tool', 'content': 'x'}]},
                         {'history': [{'role': 'user', 'content': {'nested': ['x']}}]},
                         {'history': [{'role': 'user', 'content': 'x', 'extra': 'x'}]},
                         {'history': [{'role': 'user', 'content': 'x'}] * 81}):
            with self.subTest(snapshot_type=type(snapshot)), self.assertRaises(self.module.InvalidConversation):
                self.store.save(first['id'], snapshot=snapshot)
        with self.assertRaises(self.module.InvalidConversation):
            self.store.save(first['id'], snapshot={'history': []},
                            transcript=[{'role': 'assistant', 'text': 'x', 'status': 'invented'}])
        self.assertEqual(self.store.load(first['id'])['revision'], 1)

    def test_minicpm_rejects_bad_base64_and_bad_image_metadata(self):
        first = self.store.create(backend='minicpm', profile_id='flashhead')
        for user in ({'role': 'user', 'audio': '!invalid'},
                     {'role': 'user', 'audio': 'AA=='},
                     {'role': 'user', 'text': 'x', 'images_omitted': 'true'},
                     {'role': 'user', 'text': 'x', 'images': [{'data': 'AAAA'}]}):
            with self.assertRaises(self.module.InvalidConversation):
                self.store.save(first['id'], snapshot={'history': [[user, {'role': 'assistant', 'text': 'x'}]]})

    def test_invalid_image_timestamp_has_stable_validation_error(self):
        first = self.store.create(backend='minicpm', profile_id='flashhead')
        for stamp in (float('nan'), float('inf'), 10 ** 1000, True, -1):
            user = {'role': 'user', 'text': 'x', 'images': [
                {'id': 'image-1', 'source': 'upload', 'captured_at_ms': stamp, 'data': 'AAAA'}]}
            with self.assertRaises(self.module.InvalidConversation):
                self.store.save(first['id'], snapshot={'history': [
                    [user, {'role': 'assistant', 'text': 'x'}]]})

    def test_orphan_write_recovery_and_failed_replace_preserve_committed_file(self):
        first = self.create()
        orphan = self.root / 'conversations' / ('.' + 'b' * 32 + '.tmp')
        orphan.write_text('partial content after a crashed writer')
        reopened = self.module.ConversationStore(self.root)
        self.assertFalse(orphan.exists())
        self.assertEqual(reopened.load(first['id']), first)
        with patch('conversation_store.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                reopened.save(first['id'], snapshot={'history': []}, title='Changed')
        self.assertEqual(reopened.load(first['id']), first)

    def test_independent_processes_observe_one_shared_count_limit(self):
        root = self.root / 'processes'
        store = self.module.ConversationStore(root, max_sessions=2)
        code = ("import sys;from conversation_store import ConversationStore,ConversationLimit;"
                "s=ConversationStore(sys.argv[1],max_sessions=2)\n"
                "try: print(s.create(backend='cascade',profile_id='x')['id'])\n"
                "except ConversationLimit: print('full')")
        processes = [subprocess.Popen([sys.executable, '-c', code, str(root)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for _ in range(5)]
        output = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr.decode())
            output.append(stdout.decode().strip())
        self.assertEqual(output.count('full'), 3)
        self.assertEqual(len(store.list()), 2)

    def test_session_total_and_count_limits_leave_previous_state(self):
        store = self.module.ConversationStore(self.root / 'limited', max_sessions=1,
                                              max_session_bytes=1024, max_total_bytes=1024)
        first = store.create(backend='cascade', profile_id='x')
        with self.assertRaises(self.module.ConversationLimit):
            store.create(backend='cascade', profile_id='x')
        with self.assertRaises(self.module.ConversationLimit):
            store.save(first['id'], snapshot={'history': [{'role': 'user', 'content': 'x' * 1024}]})
        self.assertEqual(store.load(first['id']), first)
        store.delete(first['id'])
        self.assertEqual(len(store.list()), 0)
        store = self.module.ConversationStore(self.root / 'total', max_total_bytes=700)
        first = store.create(backend='cascade', profile_id='x')
        with self.assertRaises(self.module.ConversationLimit):
            store.save(first['id'], snapshot={'history': [{'role': 'user', 'content': 'x' * 600}]})
        self.assertEqual(store.load(first['id']), first)

    def test_failed_write_preserves_existing_session(self):
        first = self.create()
        with patch('conversation_store.os.fsync', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.store.save(first['id'], snapshot={'history': []}, title='Changed')
        self.assertEqual(self.store.load(first['id']), first)
        self.assertFalse(list((self.root / 'conversations').glob('*.tmp')))

    def test_stale_revision_and_parallel_creation_are_consistent(self):
        first = self.create()
        self.store.save(first['id'], snapshot={'history': []}, expected_revision=first['revision'])
        with self.assertRaises(self.module.ConversationConflict):
            self.store.save(first['id'], snapshot={'history': []}, expected_revision=first['revision'])
        store = self.module.ConversationStore(self.root / 'parallel', max_sessions=3)
        def create_one(_):
            try:
                return store.create(backend='cascade', profile_id='x')['id']
            except self.module.ConversationLimit:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            created = [item for item in pool.map(create_one, range(12)) if item]
        self.assertEqual(len(set(created)), 3)
        self.assertEqual(len(store.list()), 3)

    def test_symlink_paths_and_tampered_records_rejected(self):
        target = self.root / 'outside'
        target.mkdir()
        link = self.root / 'linked'
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(self.module.InvalidConversation):
            self.module.ConversationStore(link)
        first = self.create()
        path = self.root / 'conversations' / (first['id'] + '.json')
        path.unlink()
        secret = self.root / 'secret'
        secret.write_text('private')
        path.symlink_to(secret)
        with self.assertRaises(self.module.InvalidConversation):
            self.store.load(first['id'])
        self.assertEqual(secret.read_text(), 'private')
        path.unlink()
        malformed = dict(first, snapshot={'history': [], 'unknown': 'tampered'})
        path.write_text(json.dumps(malformed))
        with self.assertRaises(self.module.InvalidConversation):
            self.store.load(first['id'])
        self.assertTrue(self.store.delete(first['id']))


if __name__ == '__main__':
    unittest.main()
