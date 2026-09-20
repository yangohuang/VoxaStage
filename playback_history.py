"""History based on acknowledged PCM phrase boundaries, never generated words.

Call ``delivered`` only for PCM actually sent to the browser; ``mark`` attaches
an entire phrase/block to its final sample. A browser acknowledgement may arrive
before the marker. Neither a partial phrase nor an unacknowledged generated
suffix enters model history. Callers serialize these synchronous operations on
their event loop and use a fresh generation for each turn.
"""
from copy import deepcopy
from uuid import uuid4

UNHEARD_MARKER = '[上轮回复已中断；后续内容未确认播放，请勿假定用户已经听到。]'
MAX_TEXT = 32768
MAX_TURNS = 40
MINICPM_PRIOR_TURNS = 6  # Worker accepts 13 messages: six pairs plus active user.
MAX_AUDIO_BYTES = 1_920_000  # 60 seconds of 16 kHz mono PCM16 input.
MAX_CONTEXT_TEXT = 12000


def _normalized(text):
    # TTS phrase aggregation may add or remove whitespace between model deltas.
    return ''.join(text.split())


class PlaybackHistory:
    def __init__(self, backend='cascade', history=None, transcript=None):
        if backend not in ('cascade', 'minicpm'):
            raise ValueError('Unknown playback history backend')
        self.backend = backend
        self._key = 'content' if backend == 'cascade' else 'text'
        history = deepcopy(history or [])
        self._turns = ([history[i:i + 2] for i in range(0, len(history), 2)]
                       if backend == 'cascade' else history)
        if any(len(turn) != 2 or turn[0].get('role') != 'user'
               or turn[1].get('role') != 'assistant' for turn in self._turns):
            raise ValueError('History must contain complete user/assistant turns')
        if backend == 'minicpm':
            # Browser image counters restart on reconnect; persisted attachments
            # need a separate namespace in the newly resumed model context.
            restored_prefix = 'restored-' + uuid4().hex[:16]
            image_index = 0
            for user, _ in self._turns:
                for image in user.get('images', []):
                    image_index += 1
                    image['id'] = f'{restored_prefix}-{image_index}'
        self._transcript = deepcopy(transcript or [])
        if len(self._transcript) != 2 * len(self._turns):
            self._transcript = []
            for user, assistant in self._turns:
                value = assistant.get(self._key, '')
                heard = value.split(UNHEARD_MARKER, 1)[0].strip()
                self._transcript.extend([
                    self._user_transcript(user),
                    {'role': 'assistant', 'text': value, 'heard_text': heard,
                     'status': 'interrupted' if UNHEARD_MARKER in value else 'heard'}])
        self._pending = None
        self._last_generation = None
        self._trim()

    def _user_transcript(self, user):
        value = user.get(self._key)
        if not value and 'audio' in user:
            value = '[用户语音输入]'
        return {'role': 'user', 'text': (value or '')[:MAX_TEXT], 'status': 'input'}

    def begin(self, user, generation):
        """Start a turn and return flat model context including its user input."""
        if self._last_generation == generation:
            raise ValueError('Each turn requires a fresh generation')
        self.interrupt()
        if isinstance(user, str):
            user = {'role': 'user', self._key: user}
        user = deepcopy(user)
        if not isinstance(user, dict) or user.get('role') != 'user':
            raise ValueError('Expected a user message')
        if self._key in user:
            if not isinstance(user[self._key], str):
                raise ValueError('Expected user text')
            user[self._key] = user[self._key][:MAX_TEXT]
        if self.backend == 'minicpm' and self._audio_size(user) > MAX_AUDIO_BYTES:
            raise ValueError('User audio exceeds the conversation audio budget')
        self._last_generation = generation
        self._pending = {'user': user, 'generation': generation, 'generated': '',
                         'markers': [], 'clips': {}, 'finished': False, 'overflow': False}
        self._trim()
        return self.context()

    def _active(self, generation):
        return self._pending is not None and self._pending['generation'] == generation

    def text(self, delta, generation):
        if not self._active(generation) or not isinstance(delta, str) or not delta:
            return False
        pending = self._pending
        combined = pending['generated'] + delta
        pending['overflow'] |= len(combined) > MAX_TEXT
        pending['generated'] = combined[:MAX_TEXT]
        return True

    def delivered(self, clip_id, total_samples, generation):
        """Register the cumulative PCM sample bound actually sent for a clip."""
        if (not self._active(generation) or not isinstance(clip_id, str) or not clip_id
                or type(total_samples) is not int or total_samples <= 0):
            return False
        clips = self._pending['clips']
        if clip_id not in clips:
            if len(clips) >= 4096:
                self._pending['overflow'] = True
                return False
            clips[clip_id] = {'delivered': 0, 'played': 0}
        if total_samples <= clips[clip_id]['delivered']:
            return False
        clips[clip_id]['delivered'] = total_samples
        return True

    def mark(self, clip_id, end_sample, text, generation):
        """Associate a complete text phrase with its final queued PCM sample."""
        if (not self._active(generation) or not isinstance(clip_id, str) or not clip_id
                or type(end_sample) is not int or end_sample <= 0
                or not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT):
            return False
        pending = self._pending
        if clip_id not in pending['clips']:
            if len(pending['clips']) >= 4096:
                pending['overflow'] = True
                return False
            pending['clips'][clip_id] = {'delivered': 0, 'played': 0}
        marker = (clip_id, end_sample, text)
        if marker in pending['markers']:
            return False
        if len(pending['markers']) >= 4096:
            pending['overflow'] = True
            return False
        # Arrival order is the authoritative phrase order; malformed ordering is
        # conservatively rejected rather than inferring missing text or timing.
        if pending['markers']:
            previous_clip, previous_end, _ = pending['markers'][-1]
            order = list(pending['clips'])
            if (order.index(clip_id) < order.index(previous_clip)
                    or (clip_id == previous_clip and end_sample <= previous_end)):
                return False
        pending['markers'].append(marker)
        self._complete_if_heard()
        return True

    def acknowledge(self, clip_id, played_samples, generation):
        """Accept only monotonic browser progress inside the server-sent bound."""
        if (not self._active(generation) or not isinstance(clip_id, str) or not clip_id
                or type(played_samples) is not int):
            return False
        clip = self._pending['clips'].get(clip_id)
        if clip is None or not clip['played'] < played_samples <= clip['delivered']:
            return False
        clip['played'] = played_samples
        self._complete_if_heard()
        return True

    def _heard(self):
        pending = self._pending
        heard = ''
        clips = pending['clips']
        for clip_id, end_sample, phrase in pending['markers']:
            # Later clip acknowledgements cannot bridge an earlier audio gap.
            earlier_heard = True
            for earlier_id, clip in clips.items():
                if earlier_id == clip_id:
                    break
                if clip['played'] < clip['delivered']:
                    earlier_heard = False
                    break
            candidate = heard + phrase
            if (not earlier_heard or clips[clip_id]['played'] < end_sample
                    or not _normalized(pending['generated']).startswith(_normalized(candidate))):
                break
            heard = candidate
        return heard.strip()

    def finish(self, generation):
        """Mark response/TTS production done; playback may still be pending."""
        if not self._active(generation):
            return False
        self._pending['finished'] = True
        self._complete_if_heard()
        return True

    def _complete_if_heard(self):
        pending = self._pending
        if (pending['finished'] and not pending['overflow'] and pending['markers']
                and _normalized(self._heard()) == _normalized(pending['generated'])
                and _normalized(''.join(m[2] for m in pending['markers'])) == _normalized(pending['generated'])
                and all(pending['clips'][clip_id]['played'] >= end
                        for clip_id, end, _ in pending['markers'])
                and all(c['played'] == c['delivered'] for c in pending['clips'].values())):
            self._commit(interrupted=False)

    def _assistant(self, interrupted):
        heard = self._heard()
        value = heard
        if interrupted:
            # Reserve marker space even if a model produced the per-message cap.
            text_limit = 4000 if self.backend == 'minicpm' else MAX_TEXT
            heard = heard[:text_limit - len(UNHEARD_MARKER) - 1].rstrip()
            value = (heard + '\n' if heard else '') + UNHEARD_MARKER
        return {'role': 'assistant', self._key: value}

    def _pending_transcript(self, status):
        pending = self._pending
        return [self._user_transcript(pending['user']),
                {'role': 'assistant', 'text': pending['generated'],
                 'generated_text': pending['generated'], 'heard_text': self._heard(),
                 'status': status}]

    def _commit(self, interrupted):
        self._turns.append([deepcopy(self._pending['user']), self._assistant(interrupted)])
        self._transcript.extend(self._pending_transcript('interrupted' if interrupted else 'heard'))
        self._pending = None
        self._trim()

    def interrupt(self):
        if self._pending is None:
            return False
        self._commit(interrupted=True)
        return True

    def context(self):
        messages = [message for turn in self._turns for message in turn]
        if self._pending:
            messages.append(self._pending['user'])
        return deepcopy(messages)

    def snapshot(self):
        """Persist a conservative copy; taking a snapshot never ends a live turn."""
        turns = deepcopy(self._turns)
        if self._pending:
            turns.append([deepcopy(self._pending['user']), self._assistant(interrupted=True)])
        history = [m for turn in turns for m in turn] if self.backend == 'cascade' else turns
        return {'history': history}

    def transcript(self):
        messages = deepcopy(self._transcript)
        if self._pending:
            messages.extend(self._pending_transcript('generated' if self._pending['generated'] else 'pending'))
        return messages

    @staticmethod
    def _audio_size(user):
        audio = user.get('audio', '')
        padding = len(audio) - len(audio.rstrip('='))
        return len(audio) * 3 // 4 - padding

    def _trim(self):
        pending = [self._pending['user']] if self._pending else []
        def users():
            return [turn[0] for turn in self._turns] + pending
        def text_size():
            return sum(len(m.get(self._key, '')) for turn in self._turns for m in turn) + sum(len(m.get(self._key, '')) for m in pending)
        while self._turns and (len(self._turns) + bool(pending) > MAX_TURNS
                or (self.backend == 'minicpm' and len(self._turns) > MINICPM_PRIOR_TURNS)
                or (len(self._turns) + bool(pending) > 1 and text_size() > MAX_CONTEXT_TEXT)
                or (self.backend == 'minicpm' and sum(self._audio_size(m) for m in users()) > MAX_AUDIO_BYTES)):
            self._turns.pop(0)
            del self._transcript[:2]
        if self.backend == 'minicpm':
            count = sum(len(user.get('images', [])) for user in users())
            for user in users():
                if count <= 2:
                    break
                images = user.get('images', [])
                remove = min(len(images), count - 2)
                if remove:
                    user['images'] = images[remove:]
                    if not user['images']:
                        del user['images']
                    user['images_omitted'] = True
                    count -= remove
