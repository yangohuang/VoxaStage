"""Stateful bounded-context adaptation of the published, noncausal checkpoint.

HuBERT re-encodes received windows with explicit lookahead. Committed audio
features and generated motion are retained across packets; neither is reset.
Windowed normalization/context changes the offline model's outputs.
"""
import copy
from dataclasses import dataclass
import time
import numpy as np
import torch

from serving.incremental_clock import AudioClock,StreamConfig


@dataclass(frozen=True)
class Frame:
    index: int
    pts_seconds: float
    vertices: np.ndarray
    available_audio_samples: int


class IncrementalSession:
    @torch.inference_mode()
    def __init__(self,engine,config):
        self.engine=engine;self.config=config;self.clock=AudioClock(config)
        self.model=engine.model;self.net=self.model.diff_ar_network
        self.device=torch.device(engine.device)
        self.generator=torch.Generator(device=self.device).manual_seed(config.seed)
        self.scheduler=copy.deepcopy(self.net.scheduler)
        self.scheduler.set_timesteps(config.steps)
        self.timesteps=self.scheduler.timesteps.to(self.device)
        self.obj_embedding=self.net.obj_vector(torch.argmax(engine.identity,dim=1)).unsqueeze(1)
        neutral=torch.zeros((1,2,engine.template.size),device=self.device)
        quant,_,_=self.model.autoencoder.encode(neutral)
        self.latents=quant.permute(0,2,1).contiguous().reshape(1,-1,self.net.vq_dim)[:,:1].clone()
        self.audio_history=None
        self.frames=0;self.batches=0;self.closed=False;self.processing_seconds=0.0
        self.history_evictions=0;self.max_latent_history_frames=1;self.max_audio_window_samples=0
        self.period=int(engine.cfg.MODEL.DIFF_AR_DEC.period)
        w=config.history_frames
        self.temporal_mask=self.net.temporal_bias_mask[:,:w,:w].to(self.device)
        self.alignment_mask=self.net.align_bias_mask[:w,:w].to(self.device)
        stride=1;receptive=1
        for kernel,step in zip(self.model.audio_encoder.config.conv_kernel,self.model.audio_encoder.config.conv_stride):
            receptive+=(kernel-1)*stride;stride*=step
        if stride!=320:raise ValueError('Audio clock requires HuBERT stride of 320 samples')
        self.audio_stride=stride;self.receptive_field=receptive

    @torch.inference_mode()
    def push(self,audio):
        if self.closed:raise RuntimeError('Session is closed')
        start=time.perf_counter()
        frames=self._process(self.clock.push(audio))
        self.processing_seconds+=time.perf_counter()-start
        return frames

    @torch.inference_mode()
    def finish(self):
        if self.closed:raise RuntimeError('Session is closed')
        start=time.perf_counter();frames=self._process(self.clock.finish())
        self.processing_seconds+=time.perf_counter()-start
        return frames

    def _encode_audio(self,batch):
        self.max_audio_window_samples=max(self.max_audio_window_samples,len(batch.audio))
        normalized=self.engine.feature_extractor(batch.audio,sampling_rate=16000,return_tensors='pt').input_values
        raw=self.model.audio_encoder(normalized.to(self.device)).last_hidden_state
        # Resample on a fixed global clock, not an independently stretched chunk.
        indices=torch.arange(batch.frame_start,batch.frame_stop,device=self.device,dtype=torch.float64)
        centers=(indices+.5)*(self.config.sample_rate/self.config.fps)-batch.start_sample
        positions=((centers-(self.receptive_field-1)/2)/self.audio_stride).clamp(0,raw.shape[1]-1)
        lo=positions.floor().long();hi=(lo+1).clamp(max=raw.shape[1]-1)
        fraction=(positions-lo).to(raw.dtype)[None,:,None]
        features=raw[:,lo]*(1-fraction)+raw[:,hi]*fraction
        return self.net.audio_feature_map(features)

    def _process(self,batches):
        result=[];w=self.config.history_frames
        for batch in batches:
            features=self._encode_audio(batch);self.batches+=1
            for j in range(features.shape[1]):
                assert batch.frame_start+j==self.frames
                feature=features[:,j:j+1]
                self.audio_history=feature if self.audio_history is None else torch.cat((self.audio_history,feature),dim=1)[:,-w:]
                length=self.latents.shape[1]
                assert length==self.audio_history.shape[1]
                start_position=self.frames+1-length
                phase=start_position%self.period
                target=self.net.vq_latent_map(self.latents)+self.obj_embedding
                target=self.net.PPE.dropout(target+self.net.PPE.pe[:,phase:phase+length])
                condition=self.net.transformer_decoder(target,self.audio_history,
                    tgt_mask=self.temporal_mask[:,:length,:length],memory_mask=self.alignment_mask[:length,:length])[:,-1:]
                # The diffusion head and DDIM step are pointwise. Only the new
                # motion token is sampled; old tokens are never regenerated.
                noise=torch.randn((1,1,self.net.vq_dim),generator=self.generator,device=self.device)
                latent=noise*self.scheduler.init_noise_sigma
                for t in self.timesteps:
                    future=self.net.vq_latent_map(latent)
                    conditioned=condition+self.net.time_emb(t[None]).unsqueeze(1)
                    prediction=self.net.latent_vq_map(self.net.denoiser(torch.cat((future,conditioned),dim=-1)))
                    latent=self.scheduler.step(prediction,t,latent,eta=0.0).prev_sample
                appended=torch.cat((self.latents,latent),dim=1)
                if appended.shape[1]>w:self.history_evictions+=1
                self.latents=appended[:,-w:].contiguous()
                self.max_latent_history_frames=max(self.max_latent_history_frames,self.latents.shape[1])
                decoded=self.model.autoencoder.decoder(self.latents.transpose(1,2))[:,-1]
                vertices=(decoded+self.engine.template_tensor).reshape(-1,3).cpu().numpy().copy()
                if not np.isfinite(vertices).all():raise RuntimeError('Non-finite generated vertices')
                result.append(Frame(self.frames,self.frames/self.config.fps,vertices,batch.end_sample))
                self.frames+=1
        return result

    def stats(self):
        return {'initialization_count':1,'frames':self.frames,'received_samples':self.clock.received_samples,
            'latent_history_frames':0 if self.latents is None else self.latents.shape[1],
            'audio_feature_history_frames':0 if self.audio_history is None else self.audio_history.shape[1],
            'buffered_audio_samples':len(self.clock.buffer),'peak_buffer_samples':self.clock.peak_buffer_samples,
            'max_audio_window_samples':self.max_audio_window_samples,'max_latent_history_frames':self.max_latent_history_frames,
            'history_evictions':self.history_evictions,'inference_batches':self.batches,
            'processing_seconds':self.processing_seconds,'closed':self.closed}

    def close(self):
        self.clock.close();self.latents=None;self.audio_history=None;self.closed=True
