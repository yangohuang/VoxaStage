import numpy as np
import pytest

from serving.incremental_clock import AudioClock, StreamConfig


def collect(audio, sizes):
    clock=AudioClock(StreamConfig()); batches=[]; pos=0
    for size in sizes:
        if pos==len(audio):break
        chunk=audio[pos:pos+size];pos+=len(chunk);batches+=clock.push(chunk)
    assert pos==len(audio)
    batches+=clock.finish()
    return clock,batches


def test_output_starts_before_end_with_explicit_lookahead():
    clock=AudioClock(StreamConfig())
    assert clock.push(np.zeros(3200,np.float32))==[]
    batches=clock.push(np.zeros(3200,np.float32))
    assert len(batches)==1
    assert (batches[0].frame_start,batches[0].frame_stop)==(0,6)
    assert batches[0].end_sample==6400


def test_network_packet_boundaries_do_not_change_audio_windows():
    audio=np.random.default_rng(3).normal(0,.1,16000*5+173).astype(np.float32)
    _,a=collect(audio,[1600]*51)
    _,b=collect(audio,[731]*110)
    assert len(a)==len(b)
    for x,y in zip(a,b):
        assert (x.start_sample,x.end_sample,x.frame_start,x.frame_stop)==(y.start_sample,y.end_sample,y.frame_start,y.frame_stop)
        np.testing.assert_array_equal(x.audio,y.audio)


def test_ten_minutes_has_no_per_packet_frame_loss_and_bounded_audio():
    clock=AudioClock(StreamConfig()); count=0; peak=0
    for _ in range(6000):
        for b in clock.push(np.zeros(1600,np.float32)):
            assert b.frame_start==count and b.end_sample<=clock.received_samples
            count=b.frame_stop
        peak=max(peak,len(clock.buffer))
    for b in clock.finish():
        assert b.frame_start==count
        count=b.frame_stop
    assert count==18000 and clock.received_samples==9600000
    assert peak<=40000


def test_final_partial_frame_and_no_input_after_end():
    clock,b=collect(np.zeros(1777,np.float32),[1600,177])
    assert b[-1].frame_stop==4
    with pytest.raises(RuntimeError):clock.push(np.zeros(1600,np.float32))
    with pytest.raises(RuntimeError):clock.finish()


@pytest.mark.parametrize('value',[np.array([],np.float32),np.zeros(16001,np.float32),np.array([np.nan]),np.zeros((2,2))])
def test_invalid_input_does_not_advance_clock(value):
    clock=AudioClock(StreamConfig())
    with pytest.raises(ValueError):clock.push(value)
    assert clock.received_samples==0


@pytest.mark.parametrize('kwargs',[{'block_ms':1},{'history_frames':1},{'lookahead_ms':-1},{'sample_rate':8000},{'steps':0}])
def test_reject_invalid_config(kwargs):
    with pytest.raises(ValueError):StreamConfig(**kwargs)
