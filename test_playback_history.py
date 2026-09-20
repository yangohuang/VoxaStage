import base64
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest

from playback_history import PlaybackHistory, UNHEARD_MARKER


class PlaybackHistoryTests(unittest.TestCase):
    def ledger(self, backend='cascade'):
        ledger = PlaybackHistory(backend)
        ledger.begin({'role': 'user', 'content' if backend == 'cascade' else 'text': 'question'}, 1)
        return ledger

    def test_partial_clip_commits_only_completed_phrase(self):
        ledger = self.ledger()
        ledger.text('First. Second.', 1)
        ledger.delivered('a', 200, 1)
        ledger.mark('a', 100, 'First. ', 1)
        ledger.mark('a', 200, 'Second.', 1)
        ledger.acknowledge('a', 150, 1)
        ledger.interrupt()
        self.assertEqual(ledger.context()[-1]['content'], 'First.\n' + UNHEARD_MARKER)
        self.assertEqual(ledger.transcript()[-1]['heard_text'], 'First.')
        self.assertNotIn('Second.', ledger.context()[-1]['content'])

    def test_ack_before_marker_promotes_when_marker_arrives(self):
        ledger = self.ledger()
        ledger.text('Hello', 1)
        ledger.delivered('a', 100, 1)
        ledger.acknowledge('a', 100, 1)
        ledger.finish(1)
        ledger.mark('a', 100, 'Hello', 1)
        self.assertEqual(ledger.context()[-1], {'role': 'assistant', 'content': 'Hello'})
        self.assertEqual(ledger.transcript()[-1]['status'], 'heard')

    def test_invalid_stale_and_repeated_acks_do_not_change_history(self):
        ledger = self.ledger()
        ledger.text('Hello', 1)
        ledger.delivered('a', 100, 1)
        ledger.mark('a', 100, 'Hello', 1)
        before = ledger.snapshot()
        for clip, count, gen in [('unknown', 100, 1), ('a', 101, 1), ('a', -1, 1), ('a', 100, 0), ('a', True, 1)]:
            self.assertFalse(ledger.acknowledge(clip, count, gen))
            self.assertEqual(ledger.snapshot(), before)
        self.assertTrue(ledger.acknowledge('a', 100, 1))
        self.assertFalse(ledger.acknowledge('a', 100, 1))
        ledger.interrupt()
        ledger.begin('next', 2)
        self.assertFalse(ledger.acknowledge('a', 100, 1))
        self.assertFalse(ledger.text('stale', 1))
        self.assertFalse(ledger.finish(1))

    def test_future_clip_cannot_skip_unheard_earlier_audio(self):
        ledger = self.ledger()
        ledger.text('First.Second.', 1)
        ledger.delivered('a', 100, 1)
        ledger.delivered('b', 100, 1)
        ledger.mark('a', 100, 'First.', 1)
        ledger.mark('b', 100, 'Second.', 1)
        ledger.acknowledge('b', 100, 1)
        self.assertEqual(ledger.transcript()[-1]['heard_text'], '')
        ledger.acknowledge('a', 100, 1)
        self.assertEqual(ledger.transcript()[-1]['heard_text'], 'First.Second.')

    def test_missing_earlier_marker_cannot_promote_later_text(self):
        ledger = self.ledger()
        ledger.text('First.Second.', 1)
        ledger.delivered('a', 100, 1)
        ledger.delivered('b', 100, 1)
        ledger.acknowledge('a', 100, 1)
        ledger.acknowledge('b', 100, 1)
        ledger.mark('b', 100, 'Second.', 1)
        self.assertEqual(ledger.transcript()[-1]['heard_text'], '')

    def test_finish_needs_all_audio_and_all_generated_text(self):
        ledger = self.ledger()
        ledger.text('Heard. Unheard.', 1)
        ledger.delivered('a', 100, 1)
        ledger.mark('a', 100, 'Heard.', 1)
        ledger.acknowledge('a', 100, 1)
        ledger.finish(1)
        self.assertEqual(ledger.transcript()[-1]['status'], 'generated')
        self.assertIn(UNHEARD_MARKER, ledger.snapshot()['history'][-1]['content'])
        self.assertNotIn('Unheard.', ledger.snapshot()['history'][-1]['content'])

    def test_snapshot_is_conservative_without_ending_live_turn(self):
        ledger = self.ledger()
        ledger.text('Hello', 1)
        self.assertEqual(ledger.snapshot()['history'][-1]['content'], UNHEARD_MARKER)
        ledger.delivered('a', 100, 1)
        ledger.mark('a', 100, 'Hello', 1)
        ledger.acknowledge('a', 100, 1)
        self.assertEqual(ledger.transcript()[-1]['status'], 'generated')
        ledger.finish(1)
        self.assertEqual(ledger.snapshot()['history'][-1]['content'], 'Hello')

    def test_finish_waits_for_audio_beyond_last_text_marker(self):
        ledger = self.ledger()
        ledger.text('Hello', 1)
        ledger.delivered('a', 200, 1)
        ledger.mark('a', 100, 'Hello', 1)
        ledger.acknowledge('a', 100, 1)
        ledger.finish(1)
        self.assertEqual(ledger.transcript()[-1]['status'], 'generated')
        ledger.acknowledge('a', 200, 1)
        self.assertEqual(ledger.transcript()[-1]['status'], 'heard')

    def test_queued_marker_can_precede_actual_pcm_delivery(self):
        ledger = self.ledger()
        ledger.text('Hello', 1)
        self.assertTrue(ledger.mark('a', 100, 'Hello', 1))
        self.assertFalse(ledger.acknowledge('a', 100, 1))
        ledger.finish(1)
        ledger.delivered('a', 50, 1)
        self.assertFalse(ledger.acknowledge('a', 100, 1))
        ledger.acknowledge('a', 50, 1)
        self.assertEqual(ledger.transcript()[-1]['heard_text'], '')
        ledger.delivered('a', 100, 1)
        ledger.acknowledge('a', 100, 1)
        self.assertEqual(ledger.transcript()[-1]['status'], 'heard')

    def test_finish_cannot_skip_a_queued_mismatching_marker(self):
        ledger = self.ledger()
        ledger.text('Hello', 1)
        ledger.mark('a', 100, 'Hello', 1)
        ledger.mark('a', 200, 'Extra', 1)
        ledger.delivered('a', 100, 1)
        ledger.acknowledge('a', 100, 1)
        ledger.finish(1)
        self.assertEqual(ledger.transcript()[-1]['status'], 'generated')
        ledger.delivered('a', 200, 1)
        ledger.acknowledge('a', 200, 1)
        self.assertEqual(ledger.transcript()[-1]['status'], 'generated')

    def test_invalid_clip_identifiers_are_ignored(self):
        ledger = self.ledger()
        for clip in ([], {}, None, 1):
            self.assertFalse(ledger.mark(clip, 100, 'Hello', 1))
            self.assertFalse(ledger.acknowledge(clip, 100, 1))

    def test_restore_both_backends(self):
        for backend in ('cascade', 'minicpm'):
            with self.subTest(backend=backend):
                ledger = self.ledger(backend)
                ledger.text('hidden', 1)
                ledger.interrupt()
                restored = PlaybackHistory(backend, ledger.snapshot()['history'], ledger.transcript())
                self.assertEqual(restored.snapshot(), ledger.snapshot())
                self.assertEqual(restored.transcript(), ledger.transcript())
                self.assertNotIn('hidden', str(restored.context()))
                self.assertEqual(len(restored.begin('next', 2)), 3)

    def test_audio_user_and_images_preserved_with_raw_audio_label(self):
        ledger = PlaybackHistory('minicpm')
        audio = base64.b64encode(b'\0\0' * 160).decode()
        image = {'id': 'image-1', 'source': 'upload', 'captured_at_ms': 0, 'data': 'AA=='}
        for generation in range(3):
            user = {'role': 'user', 'audio': audio, 'images': [dict(image)]}
            ledger.begin(user, generation)
            ledger.interrupt()
        turns = ledger.snapshot()['history']
        self.assertEqual(turns[-1][0]['audio'], audio)
        self.assertEqual(sum(len(t[0].get('images', [])) for t in turns), 2)
        self.assertTrue(turns[0][0]['images_omitted'])
        self.assertIn('语音', ledger.transcript()[-2]['text'])

    def test_audio_budget_discards_oldest_turn(self):
        ledger = PlaybackHistory('minicpm')
        audio = base64.b64encode(b'\0\0' * (16000 * 25)).decode()
        for generation in range(3):
            ledger.begin({'role': 'user', 'audio': audio}, generation)
            ledger.interrupt()
        self.assertEqual(len(ledger.snapshot()['history']), 2)

    def test_minicpm_eighth_turn_stays_within_worker_message_limit(self):
        ledger = PlaybackHistory('minicpm')
        for generation in range(10):
            context = ledger.begin('问题' + str(generation), generation)
            self.assertLessEqual(len(context), 13)
            self.assertEqual(context[-1]['role'], 'user')
            if generation >= 6:
                self.assertEqual(len(context), 13)
                self.assertEqual(context[0]['text'], '问题' + str(generation - 6))
            ledger.interrupt()
        self.assertEqual(len(ledger.snapshot()['history']), 6)
        self.assertEqual(len(ledger.transcript()), 12)

    def test_restored_minicpm_context_keeps_six_most_recent_turns(self):
        turns = [[{'role': 'user', 'text': str(i)},
                  {'role': 'assistant', 'text': '答案'}] for i in range(10)]
        ledger = PlaybackHistory('minicpm', turns)
        self.assertEqual(len(ledger.begin('继续', 1)), 13)
        self.assertEqual(ledger.context()[0]['text'], '4')

    @staticmethod
    def worker_parser():
        worker_dir = Path(__file__).parent / 'omni-server'
        spec = importlib.util.spec_from_file_location('playback_test_worker', worker_dir / 'worker.py')
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(worker_dir))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(worker_dir))
        return module.parse_messages

    def test_restored_image_ids_do_not_collide_with_new_browser_session(self):
        from PIL import Image
        stream = io.BytesIO()
        Image.new('RGB', (1, 1)).save(stream, format='PNG')
        image = {'id': 'image-1', 'source': 'camera', 'captured_at_ms': 42,
                 'data': base64.b64encode(stream.getvalue()).decode()}
        history = [[{'role': 'user', 'text': '之前', 'images': [image]},
                    {'role': 'assistant', 'text': '确认'}]]
        ledger = PlaybackHistory('minicpm', history)
        context = ledger.begin({'role': 'user', 'text': '现在', 'images': [dict(image)]}, 1)
        parsed = self.worker_parser()({'messages': context}, vision_enabled=True)
        self.assertEqual(len(parsed), 3)
        restored = context[0]['images'][0]
        self.assertRegex(restored['id'], r'^restored-[a-f0-9]+-[0-9]+$')
        self.assertLessEqual(len(restored['id']), 64)
        self.assertEqual({k: v for k, v in restored.items() if k != 'id'},
                         {k: v for k, v in image.items() if k != 'id'})
        self.assertEqual(image['id'], 'image-1')
        self.assertEqual(context[-1]['images'][0]['id'], 'image-1')
        other = PlaybackHistory('minicpm', history)
        self.assertNotEqual(restored['id'], other.context()[0]['images'][0]['id'])
        from conversation_store import ConversationStore
        with tempfile.TemporaryDirectory() as directory:
            store = ConversationStore(directory)
            record = store.create(backend='minicpm', profile_id='flashhead')
            saved = store.save(record['id'], snapshot=ledger.snapshot(),
                               transcript=ledger.transcript())
            self.assertEqual(store.load(record['id']), saved)
            self.assertEqual(saved['snapshot']['history'][0][0]['images'][0]['id'],
                             restored['id'])

    def test_interrupted_long_minicpm_reply_fits_actual_worker_text_limit(self):
        ledger = self.ledger('minicpm')
        reply = '答' * 3990
        ledger.text(reply, 1)
        ledger.delivered('clip', 100, 1)
        ledger.mark('clip', 100, reply, 1)
        ledger.acknowledge('clip', 100, 1)
        snapshot_text = ledger.snapshot()['history'][-1][1]['text']
        ledger.interrupt()
        context = ledger.begin('继续', 2)
        self.assertEqual(len(self.worker_parser()({'messages': context})), 3)
        self.assertLessEqual(len(snapshot_text), 4000)
        self.assertEqual(context[1]['text'], snapshot_text)
        self.assertTrue(snapshot_text.endswith(UNHEARD_MARKER))
        self.assertTrue(reply.startswith(snapshot_text.split('\n')[0]))

    def test_turn_limit_and_defensive_copies(self):
        ledger = PlaybackHistory()
        for generation in range(42):
            ledger.begin('question', generation)
            ledger.interrupt()
        snapshot = ledger.snapshot()
        self.assertEqual(len(snapshot['history']), 80)
        snapshot['history'][0]['content'] = 'modified'
        self.assertNotEqual(ledger.context()[0]['content'], 'modified')


if __name__ == '__main__':
    unittest.main()
