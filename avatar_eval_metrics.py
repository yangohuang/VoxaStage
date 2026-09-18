"""Bounded adapter stream measurements, not playback or model quality scores.

``wall_audio_ratio`` measures adapter wall time (including input/output pacing)
divided by input audio duration. It is not an isolated model inference RTF.
"""

import base64
import binascii
import math


def _finite_number(value, name, *, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a finite number')
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f'{name} must be a finite number') from exc
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise ValueError(f'{name} must be finite' + (' and nonnegative' if nonnegative else ''))
    return number


class MediaMeasurement:
    """Validate one clip and retain only its expected PCM and scalar counters.

    Arrival times are monotonic elapsed seconds from the beginning of the
    adapter invocation. Metadata and visuals are never retained.
    """

    def __init__(self, expected_pcm: bytes, sample_rate=24000):
        if not isinstance(expected_pcm, bytes) or not expected_pcm or len(expected_pcm) % 2:
            raise ValueError('expected_pcm must be nonempty PCM16 bytes')
        if type(sample_rate) is not int or sample_rate <= 0:
            raise ValueError('sample_rate must be a positive integer')
        _finite_number(sample_rate, 'sample_rate')
        self._expected_pcm = expected_pcm
        self.sample_rate = sample_rate
        self.input_samples = len(expected_pcm) // 2
        self.frames = 0
        self.received_samples = 0
        self._fps = None
        self._last_event_s = 0.0
        self._first_media_s = None
        self._last_media_s = None
        self._max_receive_gap_s = None
        self._max_pts_error_s = 0.0
        self._completed = False

    def _elapsed(self, elapsed_s):
        elapsed_s = _finite_number(elapsed_s, 'elapsed_s', nonnegative=True)
        if elapsed_s < self._last_event_s:
            raise ValueError('elapsed_s must be monotonic')
        return elapsed_s

    def add(self, event: dict, elapsed_s: float):
        elapsed_s = self._elapsed(elapsed_s)
        if not isinstance(event, dict):
            raise ValueError('adapter event must be an object')
        if self._completed:
            raise ValueError('event received after clip_end')
        kind = event.get('type')
        if kind == 'avatar_meta':
            if self._fps is not None:
                raise ValueError('repeated avatar_meta')
            rate = event.get('sample_rate')
            if type(rate) is not int or rate != self.sample_rate:
                raise ValueError('metadata sample_rate does not match input')
            fps = _finite_number(event.get('fps'), 'fps')
            if fps <= 0:
                raise ValueError('fps must be positive')
            self._fps = fps
        elif kind == 'media':
            self._add_media(event, elapsed_s)
        elif kind == 'clip_end':
            total = event.get('total_samples')
            if not self.frames:
                raise ValueError('clip_end received before media')
            if (type(total) is not int or total != self.input_samples
                    or self.received_samples != self.input_samples):
                raise ValueError('clip_end must account for every expected sample')
            self._completed = True
        else:
            raise ValueError(f'unknown adapter event type: {kind!r}')
        self._last_event_s = elapsed_s

    def _add_media(self, event, elapsed_s):
        if self._fps is None:
            raise ValueError('media received before avatar_meta')
        index, start = event.get('frame_index'), event.get('start_sample')
        if type(index) is not int or index != self.frames:
            raise ValueError('frame_index must be consecutive from zero')
        if type(start) is not int or start != self.received_samples:
            raise ValueError('start_sample must equal received sample count')
        pts = _finite_number(event.get('pts'), 'pts', nonnegative=True)
        error = abs(pts - start / self.sample_rate)
        if error > 1e-6:
            raise ValueError('pts does not match the PCM sample clock')
        encoded = event.get('audio')
        if not isinstance(encoded, str):
            raise ValueError('audio must be base64 PCM16')
        # Bound decoded allocation by the remaining expected bytes.
        remaining = len(self._expected_pcm) - start * 2
        if len(encoded) > 4 * ((remaining + 2) // 3):
            raise ValueError('audio exceeds expected PCM length')
        try:
            pcm = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError('audio must be valid base64 PCM16') from exc
        if not pcm or len(pcm) % 2:
            raise ValueError('audio must contain nonempty PCM16 samples')
        end = start * 2 + len(pcm)
        if end > len(self._expected_pcm) or pcm != self._expected_pcm[start * 2:end]:
            raise ValueError('audio does not preserve the expected PCM prefix')
        if self._first_media_s is None:
            self._first_media_s = elapsed_s
        if self._last_media_s is not None:
            gap = elapsed_s - self._last_media_s
            self._max_receive_gap_s = max(self._max_receive_gap_s or 0.0, gap)
        self._last_media_s = elapsed_s
        self._max_pts_error_s = max(self._max_pts_error_s, error)
        self.received_samples += len(pcm) // 2
        self.frames += 1

    def finish(self, elapsed_s, interrupted=False):
        elapsed_s = self._elapsed(elapsed_s)
        if not self.frames:
            raise ValueError('measurement requires at least one media event')
        if not interrupted and not self._completed:
            raise ValueError('normal completion requires clip_end')
        duration = self.input_samples / self.sample_rate
        ratio = _finite_number(elapsed_s / duration, 'wall_audio_ratio', nonnegative=True)
        return dict(frames=self.frames, received_samples=self.received_samples,
                    input_samples=self.input_samples, audio_duration_s=duration,
                    first_media_s=self._first_media_s, wall_s=elapsed_s,
                    wall_audio_ratio=ratio, max_receive_gap_s=self._max_receive_gap_s,
                    max_pts_error_s=self._max_pts_error_s,
                    pcm_exact=None if interrupted else True, prefix_exact=True,
                    completed=self._completed, playback_measured=False, lip_sync_measured=False)


def percentile(values, p):
    """Linearly interpolate finite observations; empty observations return None."""
    p = _finite_number(p, 'percentile')
    if not 0 <= p <= 100:
        raise ValueError('percentile must be between 0 and 100')
    values = sorted(_finite_number(value, 'observation') for value in values)
    if not values:
        return None
    position = (len(values) - 1) * p / 100
    low, high = math.floor(position), math.ceil(position)
    fraction = position - low
    # A weighted sum avoids overflow in the difference of opposite extremes.
    return values[low] * (1 - fraction) + values[high] * fraction


def summarize(rows):
    """Group measured attempts, retaining failures only in the denominators."""
    fields = ('provider', 'case_id', 'input_sha256', 'mode')
    metrics = ('first_media_s', 'wall_s', 'wall_audio_ratio')
    groups = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('summary rows must be objects')
        if row.get('phase') != 'measured':
            continue
        key = tuple(row.get(field) for field in fields)
        if any(not isinstance(value, str) or not value for value in key):
            raise ValueError('measured rows require nonempty grouping fields')
        if row['mode'] not in ('burst', 'realtime'):
            raise ValueError('invalid measured input mode')
        if row.get('status') not in ('passed', 'failed'):
            raise ValueError('invalid measured status')
        observations = {}
        if row['status'] == 'passed':
            if not isinstance(row.get('metrics'), dict):
                raise ValueError('passed measured rows require metrics')
            observations = {name: _finite_number(row['metrics'].get(name), name, nonnegative=True)
                            for name in metrics}
        if key not in groups:
            groups[key] = dict(attempted=0, passed=0, failed=0,
                               values={name: [] for name in metrics})
        group = groups[key]
        group['attempted'] += 1
        group[row['status']] += 1
        for name, value in observations.items():
            group['values'][name].append(value)
    result = []
    for key, group in sorted(groups.items()):
        stats = {name: dict(n=len(values), p50=percentile(values, 50), p95=percentile(values, 95))
                 for name, values in group['values'].items()}
        result.append(dict(zip(fields, key), attempted=group['attempted'],
                           passed=group['passed'], failed=group['failed'], metrics=stats))
    return result
