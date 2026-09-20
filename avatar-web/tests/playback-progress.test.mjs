import test from 'node:test';
import assert from 'node:assert/strict';
import { AvatarPlayer } from '../audio.mjs';

const meta = {type:'avatar_meta', generation:0, clip_id:'clip', sample_rate:24000,
  fps:30, vertex_count:3, faces:[[0,1,2]]};
const packet = index => ({type:'media', generation:0, clip_id:'clip', start_sample:index*800,
  frame_index:index, pts:index/30, audio:Buffer.alloc(1600).toString('base64'),
  vertices:Buffer.alloc(36).toString('base64')});

function fixture(t) {
  const progress=[];
  const player=new AvatarPlayer({onFrame(){},onState(){},onError(error){throw error;},
    onProgress: value=>progress.push(value)});
  t.after(()=>clearInterval(player.timer));
  player.context={state:'running',currentTime:0,destination:{},
    createBuffer(){return {copyToChannel(){}};},
    createBufferSource(){return {connect(){},disconnect(){},start(){},stop(){this.onended?.();}};}};
  player.reset(0);player.handle(meta);
  for(let i=0;i<4;i++)player.handle(packet(i));
  return {player,progress,sources:[...player.sources]};
}

test('acknowledges naturally finished packets in clip sample coordinates exactly once',t=>{
  const {player,progress,sources}=fixture(t);
  assert.deepEqual(progress,[]);
  sources[0].onended();sources[0].onended();sources[1].onended();
  assert.deepEqual(progress,[{clip_id:'clip',played_samples:800},{clip_id:'clip',played_samples:1600}]);
  player.stop();
  assert.equal(progress.length,2);
});

test('stop invalidates callbacks before stopping sources, including synchronous ended events',t=>{
  const {player,progress,sources}=fixture(t);
  player.stop();
  sources.forEach(source=>source.onended());
  assert.deepEqual(progress,[]);
});

test('late completion from a previous epoch cannot confirm the new generation',t=>{
  const {player,progress,sources}=fixture(t);
  player.reset(1);sources[0].onended();
  assert.deepEqual(progress,[]);
});
