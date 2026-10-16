"""Bounded PCM transport and single-owner lifecycle for a synchronous TTS model."""
import asyncio
import json
import queue
import re
import threading
import wave
from pathlib import Path

import numpy as np
import soxr


class BusyError(RuntimeError):
    pass


def load_voices(manifest):
    rows = json.loads(Path(manifest).read_text())
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise ValueError('Provide one to eight local voices')
    voices = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'id', 'wav_local_path'}:
            raise ValueError('Invalid voice configuration')
        name, location = row['id'], row['wav_local_path']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) or name in voices:
            raise ValueError('Invalid or duplicate voice ID')
        if not isinstance(location, str) or not Path(location).is_absolute():
            raise ValueError('Reference WAV must use an absolute administrator path')
        path = Path(location).resolve(strict=True)
        try:
            with wave.open(str(path), 'rb') as stream:
                if (stream.getnchannels() != 1 or stream.getsampwidth() != 2
                        or not 8000 <= stream.getframerate() <= 48000
                        or not 1 <= stream.getnframes() / stream.getframerate() <= 15):
                    raise ValueError('Reference must be mono PCM16, 8–48 kHz, 1–15 seconds')
                if len(stream.readframes(stream.getnframes())) != 2 * stream.getnframes():
                    raise ValueError('Truncated reference WAV')
        except (wave.Error, EOFError) as exc:
            raise ValueError('Invalid reference WAV') from exc
        voices[name] = path
    return voices


class PCMStream:
    """Upstream tensors contain PCM amplitude units, not normalized [-1, 1]."""
    def __init__(self):
        self.resampler = soxr.ResampleStream(22050, 24000, 1, dtype='float32', quality='HQ')
        self.samples = 0

    @staticmethod
    def packets(values):
        pcm = np.clip(np.rint(values), -32768, 32767).astype('<i2').tobytes()
        for offset in range(0, len(pcm), 1920):
            yield pcm[offset:offset + 1920]

    def feed(self, waveform):
        values = np.asarray(waveform, dtype=np.float32)
        if values.ndim == 2 and values.shape[0] == 1:
            values = values[0]
        if values.ndim != 1 or not values.size or not np.isfinite(values).all():
            raise ValueError('Expected finite nonempty mono audio')
        self.samples += values.size
        if self.samples > 22050 * 300:
            raise ValueError('Synthesis exceeds five-minute output limit')
        yield from self.packets(self.resampler.resample_chunk(values))

    def finish(self):
        if not self.samples:
            raise ValueError('Model returned no audio')
        yield from self.packets(self.resampler.resample_chunk(np.empty(0, dtype=np.float32), last=True))


class Job:
    def __init__(self, engine, voice, text):
        self.engine, self.voice, self.text = engine, voice, text
        self.stop = threading.Event()
        self.done = threading.Event()
        self.queue = queue.Queue(maxsize=4)
        self.failed = False
        self.thread = threading.Thread(target=self.run, name='tts-inference', daemon=False)

    def cancel(self):
        self.stop.set()

    def put(self, packet):
        while not self.stop.is_set():
            try:
                self.queue.put(packet, timeout=.05)
                return True
            except queue.Full:
                pass
        return False

    def run(self):
        source = None
        try:
            source = iter(self.engine.generate(self.voice, self.text))
            pcm = PCMStream()
            while not self.stop.is_set():
                try:
                    waveform = next(source)
                except StopIteration:
                    break
                if self.stop.is_set():
                    return
                for packet in pcm.feed(waveform):
                    if not self.put(packet):
                        return
            if not self.stop.is_set():
                for packet in pcm.finish():
                    if not self.put(packet):
                        return
        except Exception:
            self.failed = True
        finally:
            try:
                if source is not None and hasattr(source, 'close'):
                    source.close()
            except Exception:
                self.failed = True
            finally:
                # Release only after generator cleanup, never when a client disconnects.
                self.engine.lock.release()
                self.done.set()

    def get(self):
        while not self.stop.is_set():
            try:
                return self.queue.get(timeout=.05)
            except queue.Empty:
                if self.done.is_set():
                    if self.failed:
                        raise RuntimeError('Synthesis failed')
                    return None
        return None

    async def receive(self):
        packet = await asyncio.to_thread(self.get)
        return None if self.stop.is_set() else packet


class Engine:
    def __init__(self, generate, voices):
        self.generate = generate
        self.voices = dict(voices)
        self.lock = threading.Lock()
        self.jobs = []
        self.closed = False

    @property
    def busy(self):
        return self.lock.locked()

    def start(self, voice, text):
        if self.closed:
            raise RuntimeError('Worker is shutting down')
        if voice not in self.voices:
            raise ValueError('Unknown voice')
        if not self.lock.acquire(blocking=False):
            raise BusyError('Synthesis is busy')
        try:
            self.jobs = [job for job in self.jobs if job.thread.is_alive()]
            job = Job(self, self.voices[voice], text)
            self.jobs.append(job)
            job.thread.start()
            return job
        except BaseException:
            self.lock.release()
            raise

    async def wait_idle(self):
        for job in list(self.jobs):
            await asyncio.to_thread(job.thread.join)

    async def shutdown(self):
        self.closed = True
        for job in self.jobs:
            job.cancel()
        await self.wait_idle()
