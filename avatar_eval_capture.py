"""Bounded local adapter outputs and numerical adjacent-frame diagnostics.

These diagnostics are not perceptual quality, lip-sync, or identity scores.
Compare only runs with the same character, provider, and output configuration.
Only one decoded previous frame is retained; original source PCM is not saved
again because MediaMeasurement checks every output sample against that input.
"""

import base64
import binascii
import hashlib
import io
import json
from pathlib import Path
import re
import wave

import numpy as np
from PIL import Image

from avatar_eval_metrics import MediaMeasurement


class ClipCapture:
    """Record one normally completed clip in a new local directory.

    Call ``add`` for every adapter event, then ``finish`` only after the stream
    returns normally. On upstream failure/cancellation, call ``fail`` with an
    exception class name. Validation failures automatically persist failed
    status and close the WAV. A clip_end alone never marks capture successful.
    """

    def __init__(self, output_dir: Path, expected_pcm: bytes, max_frames=1801):
        self._measurement = MediaMeasurement(expected_pcm)
        if len(expected_pcm) > 30 * 24000 * 2:
            raise ValueError('capture PCM exceeds 30 seconds')
        if type(max_frames) is not int or not 1 <= max_frames <= 1801:
            raise ValueError('max_frames must be between 1 and 1801')
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.max_frames = max_frames
        self._status = 'running'
        self._meta = None
        self._wav = None
        self._pcm_sha256 = hashlib.sha256()
        self._index = []
        self._previous = None
        self._previous_raw = None
        self._delta_sum = 0.0
        self._delta_max = None
        self._pairs = 0
        self._repeated = 0
        self._manifest = dict(status='running', n_frames=0, pcm_exact=False,
                              perceptual_quality_measured=False,
                              lip_sync_measured=False, identity_measured=False,
                              comparison_scope='same character, provider and output configuration')
        self._write_json('manifest.json', self._manifest)
        try:
            (self.output_dir / 'frames').mkdir()
            self._wav = wave.open(str(self.output_dir / 'output.wav'), 'wb')
            self._wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        except Exception as exc:
            self.fail(type(exc).__name__)
            raise

    def _write_json(self, name, value):
        destination = self.output_dir / name
        temporary = destination.with_suffix(destination.suffix + '.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
        temporary.replace(destination)

    def _require_running(self):
        if self._status != 'running':
            raise ValueError('capture is already finalized or failed')

    def _metadata(self, event):
        fps = event.get('fps')
        if type(fps) not in (int, float) or not 1 <= fps <= 60:
            raise ValueError('capture fps must be between 1 and 60')
        kind = event.get('kind', '3d')
        result = dict(kind=kind, fps=fps, sample_rate=24000)
        topology = None
        if kind == '2d':
            if (event.get('codec') != 'jpeg'
                    or any(type(event.get(k)) is not int or not 1 <= event[k] <= 2048
                           for k in ('width', 'height'))):
                raise ValueError('invalid capture JPEG metadata')
            result.update({key: event[key] for key in ('codec', 'width', 'height')})
        elif kind == '3d':
            count, faces = event.get('vertex_count'), event.get('faces')
            if (type(count) is not int or not 3 <= count <= 20000
                    or not isinstance(faces, list) or not 1 <= len(faces) <= 40000
                    or any(not isinstance(face, list) or len(face) != 3
                           or any(type(v) is not int or not 0 <= v < count for v in face)
                           for face in faces)):
                raise ValueError('invalid capture topology')
            topology = np.asarray(faces, dtype='<i4')
            result.update(vertex_count=count, topology='topology.npy')
        else:
            raise ValueError('unsupported capture kind')
        return result, topology

    @staticmethod
    def _decode(encoded, limit):
        if not isinstance(encoded, str) or len(encoded) > limit:
            raise ValueError('visual payload exceeds capture limit')
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError('visual payload must be valid base64') from exc

    def _visual(self, event):
        if self._meta is None:
            raise ValueError('capture media requires metadata')
        if len(self._index) >= self.max_frames:
            raise ValueError('capture frame limit exceeded')
        if self._meta['kind'] == '2d':
            raw = self._decode(event.get('image'), 1_400_000)
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    if (image.format != 'JPEG'
                            or image.size != (self._meta['width'], self._meta['height'])):
                        raise ValueError('JPEG dimensions or format differ from metadata')
                    pixels = np.asarray(image.convert('RGB'))
            except Exception as exc:
                raise ValueError('invalid capture JPEG') from exc
            return raw, pixels, 'jpg'
        size = self._meta['vertex_count'] * 3 * 4
        raw = self._decode(event.get('vertices'), 4 * ((size + 2) // 3))
        if len(raw) != size:
            raise ValueError('invalid capture vertices size')
        vertices = np.frombuffer(raw, dtype='<f4').reshape(-1, 3)
        if not np.isfinite(vertices).all():
            raise ValueError('capture vertices must be finite')
        return raw, vertices, 'npy'

    def add(self, event, elapsed_s):
        try:
            self._require_running()
            if not isinstance(event, dict):
                raise ValueError('adapter event must be an object')
            kind = event.get('type')
            if kind == 'avatar_meta':
                metadata, topology = self._metadata(event)
                self._measurement.add(event, elapsed_s)
                self._meta = metadata
                self._write_json('metadata.json', metadata)
                if topology is not None:
                    np.save(self.output_dir / 'topology.npy', topology, allow_pickle=False)
            elif kind == 'media':
                raw, values, suffix = self._visual(event)
                self._measurement.add(event, elapsed_s)
                # MediaMeasurement has already bounded and validated these bytes.
                pcm = base64.b64decode(event['audio'], validate=True)
                name = f'frames/{len(self._index):06d}.{suffix}'
                if suffix == 'jpg':
                    (self.output_dir / name).write_bytes(raw)
                else:
                    np.save(self.output_dir / name, values, allow_pickle=False)
                self._wav.writeframesraw(pcm)
                self._pcm_sha256.update(pcm)
                self._index.append(dict(frame_index=event['frame_index'], pts=event['pts'],
                                        start_sample=event['start_sample'], n_samples=len(pcm) // 2,
                                        artifact=name, payload_sha256=hashlib.sha256(raw).hexdigest()))
                self._observe(raw, values)
            else:
                self._measurement.add(event, elapsed_s)
        except Exception as exc:
            self.fail(type(exc).__name__)
            raise

    def _observe(self, raw, values):
        if self._previous is not None:
            difference = values.astype(np.float64) - self._previous
            if self._meta['kind'] == '2d':
                delta = float(np.abs(difference).mean() / 255)
                self._repeated += raw == self._previous_raw
            else:
                # RMS of per-vertex Euclidean displacement, in native mesh units.
                delta = float(np.sqrt(np.square(difference).sum(axis=1).mean()))
            self._pairs += 1
            self._delta_sum += delta
            self._delta_max = max(self._delta_max or 0.0, delta)
        self._previous = values
        self._previous_raw = raw

    def _continuity(self):
        if self._meta is None:
            return {}
        name = ('adjacent_pixel_mae_0_1' if self._meta['kind'] == '2d'
                else 'adjacent_vertex_rms_displacement')
        result = {name: dict(pairs=self._pairs,
                             mean=self._delta_sum / self._pairs if self._pairs else None,
                             max=self._delta_max)}
        if self._meta['kind'] == '2d':
            result['exact_repeated_frame_fraction'] = (self._repeated / self._pairs
                                                        if self._pairs else None)
        return result

    def _close_wav(self):
        if self._wav is not None:
            wav, self._wav = self._wav, None
            wav.close()

    def finish(self, elapsed_s):
        try:
            self._require_running()
            metrics = self._measurement.finish(elapsed_s)
            self._close_wav()
            self._write_json('index.json', self._index)
            self._manifest.update(status='passed', n_frames=len(self._index), pcm_exact=True,
                                  output_pcm_sha256=self._pcm_sha256.hexdigest(),
                                  continuity=self._continuity(), metrics=metrics)
            self._write_json('manifest.json', self._manifest)
            self._status = 'passed'
            self._previous = self._previous_raw = None
            return dict(self._manifest)
        except Exception as exc:
            self.fail(type(exc).__name__)
            raise

    def fail(self, error_type):
        """Persist failure without logging backend messages or URLs; safe to repeat."""
        error_type = (error_type if isinstance(error_type, str)
                      and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', error_type) else 'Error')
        self._status = 'failed'
        self._manifest.update(status='failed', n_frames=len(self._index), pcm_exact=False,
                              error_type=error_type, continuity=self._continuity())
        self._previous = self._previous_raw = None
        # Write failure even if closing an interrupted WAV raises an I/O error.
        try:
            self._close_wav()
        finally:
            self._write_json('manifest.json', self._manifest)
            self._write_json('index.json', self._index)
