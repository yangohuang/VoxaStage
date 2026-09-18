"""Small torch components test state semantics; real checkpoint validation is a separate GPU check."""
from types import SimpleNamespace as NS
import numpy as np
import torch
from torch import nn
import pytest

from serving.incremental import IncrementalSession,StreamConfig


class AudioEncoder(nn.Module):
    def __init__(self):
        super().__init__();self.conv=nn.Conv1d(1,8,400,stride=320)
        self.config=NS(conv_kernel=[400],conv_stride=[320])
    def forward(self,audio):return NS(last_hidden_state=self.conv(audio[:,None]).transpose(1,2))


class Conditions(nn.Module):
    def forward(self,x,audio,**kwargs):
        return x+x.cumsum(1)/torch.arange(1,x.shape[1]+1)[None,:,None]+audio


class Scheduler:
    init_noise_sigma=1
    def set_timesteps(self,n):self.timesteps=torch.arange(n-1,-1,-1)
    def step(self,pred,t,x,**kwargs):return NS(prev_sample=(pred+x)*.5)


class Decoder(nn.Module):
    def __init__(self):
        super().__init__();self.linear=nn.Linear(8,9);self.lengths=[]
    def forward(self,x):
        self.lengths.append(x.shape[-1]);y=x.transpose(1,2)
        return self.linear(y+.2*y.mean(1,keepdim=True))


class Autoencoder(nn.Module):
    def __init__(self):super().__init__();self.decoder=Decoder();self.encode_calls=0
    def encode(self,x):
        self.encode_calls+=1
        return torch.zeros((1,8,2)),None,None


def engine():
    torch.manual_seed(17)
    net=nn.Module();net.vq_dim=8;net.obj_vector=nn.Embedding(8,8)
    net.vq_latent_map=nn.Linear(8,8);net.audio_feature_map=nn.Linear(8,8)
    net.PPE=nn.Module();net.PPE.register_buffer('pe',torch.sin((torch.arange(120)%30)[:,None]*torch.ones(1,8))[None]);net.PPE.dropout=nn.Identity()
    net.temporal_bias_mask=torch.zeros((2,100,100));net.align_bias_mask=~torch.eye(100,dtype=torch.bool)
    net.transformer_decoder=Conditions();net.time_emb=nn.Embedding(50,8)
    net.denoiser=nn.Linear(16,8);net.latent_vq_map=nn.Linear(8,8);net.scheduler=Scheduler()
    model=nn.Module();model.diff_ar_network=net;model.audio_encoder=AudioEncoder();model.autoencoder=Autoencoder();model.eval()
    cfg=NS(MODEL=NS(VAE=NS(face_quan_num=1,zquant_dim=8),DIFF_AR_DEC=NS(period=30),eta=0))
    return NS(model=model,cfg=cfg,device='cpu',template=np.zeros((3,3),np.float32),template_tensor=torch.zeros(9),
              identity=torch.eye(8)[:1],feature_extractor=lambda a,**kw:NS(input_values=torch.tensor(a)[None]))


def render(e,audio,packet):
    s=IncrementalSession(e,StreamConfig(steps=2,history_frames=12));frames=[]
    for i in range(0,len(audio),packet):frames+=s.push(audio[i:i+packet])
    frames+=s.finish()
    return s,frames


def test_state_and_random_generator_survive_audio_packets():
    e=engine();rng=torch.get_rng_state().clone();s=IncrementalSession(e,StreamConfig(steps=2))
    first=s.push(np.zeros(6400,np.float32));previous=s.latents.clone()
    second=s.push(np.ones(3200,np.float32)*.1)
    assert len(first)==len(second)==6 and second[0].index==6
    torch.testing.assert_close(s.latents[:,:previous.shape[1]],previous,rtol=0,atol=0)
    assert e.model.autoencoder.encode_calls==1 and s.stats()['initialization_count']==1
    assert torch.equal(torch.get_rng_state(),rng)


def test_packet_partition_invariance_and_bounded_histories():
    e=engine();a=np.random.default_rng(9).normal(0,.1,48017).astype(np.float32)
    x,xf=render(e,a,1600);y,yf=render(e,a,731)
    assert len(xf)==len(yf)==91
    np.testing.assert_array_equal(np.stack([f.vertices for f in xf]),np.stack([f.vertices for f in yf]))
    assert [f.index for f in xf]==list(range(91))
    assert [f.pts_seconds for f in xf]==[i/30 for i in range(91)]
    assert x.stats()['latent_history_frames']<=12 and x.stats()['audio_feature_history_frames']<=12
    assert max(e.model.autoencoder.decoder.lengths)<=12


def test_returned_frames_remain_immutable_when_later_audio_arrives():
    e=engine();a=IncrementalSession(e,StreamConfig(steps=2));b=IncrementalSession(e,StreamConfig(steps=2))
    prefix=np.sin(np.arange(16000)*.04).astype(np.float32)
    af=a.push(prefix);bf=b.push(prefix)
    a.push(np.zeros(16000,np.float32));b.push(np.ones(16000,np.float32)*.8)
    np.testing.assert_array_equal(np.stack([f.vertices for f in af]),np.stack([f.vertices for f in bf]))
    assert len(af)==24


def test_finish_close_and_input_validation():
    s=IncrementalSession(engine(),StreamConfig(steps=2))
    with pytest.raises(ValueError):s.push(np.array([np.nan]))
    assert s.stats()['received_samples']==0
    s.push(np.zeros(1600,np.float32));assert len(s.finish())==3
    with pytest.raises(RuntimeError):s.push(np.zeros(1600,np.float32))
    s.close();assert s.stats()['buffered_audio_samples']==0


def test_teacher_forcing_alignment_and_position_phase_across_eviction():
    e=engine();net=e.model.diff_ar_network;records=[]
    class Recorder(nn.Module):
        def forward(self,x,audio,**kw):
            records.append((x.clone(),audio.clone(),kw))
            return audio
    class ConditionOnly(nn.Module):
        def forward(self,x):return x[...,8:]
    class IdentityStep(Scheduler):
        def step(self,pred,t,x,**kw):return NS(prev_sample=pred)
    net.transformer_decoder=Recorder();net.denoiser=ConditionOnly()
    net.vq_latent_map=nn.Identity();net.latent_vq_map=nn.Identity()
    net.obj_vector.weight.data.zero_();net.time_emb.weight.data.zero_()
    net.PPE.pe.copy_((torch.arange(120)%30)[None,:,None].expand(1,120,8))
    net.temporal_bias_mask=torch.triu(torch.full((2,100,100),float('-inf')),diagonal=1)
    net.scheduler=IdentityStep()
    session=IncrementalSession(e,StreamConfig(steps=1));session.latents=torch.full_like(session.latents,-1)
    session._encode_audio=lambda batch:torch.arange(batch.frame_start,batch.frame_stop,dtype=torch.float32)[None,:,None].expand(1,-1,8)
    for _ in range(40):session.push(np.zeros(1600,np.float32))
    session.finish()
    for i in [0,29,30,59,60,61,89,90,119]:
        x,audio,masks=records[i];length=min(i+1,60)
        positions=torch.arange(i+1-length,i+1)
        motion=torch.arange(i-length,i)
        torch.testing.assert_close(audio[0,:,0],positions.float())
        torch.testing.assert_close(x[0,:,0],motion.float()+positions%30)
        assert torch.equal(masks['memory_mask'],~torch.eye(length,dtype=torch.bool))
        assert torch.isneginf(masks['tgt_mask'][0][torch.triu(torch.ones(length,length,dtype=torch.bool),diagonal=1)]).all()
