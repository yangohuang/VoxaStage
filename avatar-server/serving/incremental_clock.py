"""Bounded received-audio timeline; fixed inference windows ignore packet boundaries."""
from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True)
class StreamConfig:
    steps: int = 10
    seed: int = 42
    block_ms: int = 200
    lookahead_ms: int = 200
    audio_context_ms: int = 2000
    history_frames: int = 60
    sample_rate: int = 16000
    fps: int = 30

    def __post_init__(self):
        for name,value in vars(self).items():
            if isinstance(value,bool) or not isinstance(value,int):
                raise ValueError(f'{name} must be an integer')
        if self.sample_rate!=16000 or self.fps!=30:
            raise ValueError('Only 16 kHz mono input and 30 FPS output are supported')
        if not 1<=self.steps<=50 or not 0<=self.seed<2**31:
            raise ValueError('Invalid steps or seed')
        if not 100<=self.block_ms<=1000 or self.block_ms*self.fps%1000:
            raise ValueError('block_ms must represent an integral 3–30 frames')
        if not 0<=self.lookahead_ms<=1000 or not 100<=self.audio_context_ms<=5000:
            raise ValueError('Invalid audio context or lookahead')
        if not 2<=self.history_frames<=240:
            raise ValueError('history_frames must be between 2 and 240')

    @property
    def block_frames(self):return self.block_ms*self.fps//1000


@dataclass(frozen=True)
class AudioBatch:
    audio: np.ndarray
    start_sample: int
    end_sample: int
    frame_start: int
    frame_stop: int


class AudioClock:
    def __init__(self,config):
        self.config=config
        self.buffer=np.empty(0,np.float32)
        self.buffer_start=0
        self.received_samples=0
        self.next_frame=0
        self.finished=False
        self.peak_buffer_samples=0

    def _window_start(self,frame):
        # HuBERT convolution stride is 320 samples; retain this global alignment.
        c=self.config
        sample=frame*c.sample_rate//c.fps-c.audio_context_ms*c.sample_rate//1000
        return max(0,sample//320*320)

    def push(self,audio):
        if self.finished:raise RuntimeError('Session has ended')
        audio=np.asarray(audio,dtype=np.float32)
        if audio.ndim!=1 or not 0<len(audio)<=self.config.sample_rate or not np.isfinite(audio).all():
            raise ValueError('Expected 1–16000 finite mono samples per packet')
        self.buffer=np.concatenate((self.buffer,audio))
        self.received_samples+=len(audio)
        self.peak_buffer_samples=max(self.peak_buffer_samples,len(self.buffer))
        return self._produce(False)

    def finish(self):
        if self.finished:raise RuntimeError('Session has ended')
        if self.received_samples<1600:raise ValueError('At least 0.1 seconds of audio is required')
        self.finished=True
        return self._produce(True)

    def _produce(self,final):
        c=self.config; batches=[]
        total_frames=(self.received_samples*c.fps+c.sample_rate-1)//c.sample_rate
        while self.next_frame<total_frames:
            stop=self.next_frame+c.block_frames
            if final:stop=min(stop,total_frames)
            end=(stop*c.sample_rate+c.fps-1)//c.fps+c.lookahead_ms*c.sample_rate//1000
            if not final and end>self.received_samples:break
            end=min(end,self.received_samples)
            start=self._window_start(self.next_frame)
            assert self.buffer_start<=start<end<=self.received_samples
            offset=start-self.buffer_start
            batches.append(AudioBatch(self.buffer[offset:offset+end-start].copy(),start,end,self.next_frame,stop))
            self.next_frame=stop
        keep=self._window_start(self.next_frame)
        removed=keep-self.buffer_start
        if removed>0:
            self.buffer=self.buffer[removed:].copy()
            self.buffer_start=keep
        return batches

    def close(self):
        self.finished=True
        self.buffer=np.empty(0,np.float32)
