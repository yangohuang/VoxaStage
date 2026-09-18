import test from 'node:test';
import assert from 'node:assert/strict';
import {frameAt, makeAnnotation, loadClip, createSelectionQueue} from './review-core.mjs';

const clip = () => ({id:'clip-001',kind:'2d',category:'zh',duration:.1,fps:25,
  fingerprint:'a'.repeat(64),width:2,height:2,sampleRate:24000,audioUrl:'/audio',
  frames:[{index:0,pts:0,url:'/0'},{index:1,pts:.04,url:'/1'},{index:2,pts:.08,url:'/2'}]});
test('frame selection follows source PTS through seeks and the partial final frame', () => {
  const frames=clip().frames;
  assert.equal(frameAt(0,frames),0);assert.equal(frameAt(.039,frames),0);
  assert.equal(frameAt(.04,frames),1);assert.equal(frameAt(.1,frames),2);
  assert.equal(frameAt(999,frames),2);assert.throws(()=>frameAt(NaN,frames));
});

test('annotations require intentional evaluation and keep reviewer origin and bounded range', () => {
  const fields={reviewer:' local-reviewer ',reviewerKind:'automated',dimension:'continuity',start:'0',end:'.1',severity:'2',notes:' visible jump ',reviewed:true};
  const row=makeAnnotation(clip(),fields);
  assert.equal(row.reviewer,'local-reviewer');assert.equal(row.reviewerKind,'automated');
  assert.equal(row.start,0);assert.equal(row.end,.1);assert.equal(row.severity,2);
  assert.equal(row.fingerprint,'a'.repeat(64));assert.equal(row.notes,'visible jump');
  for(const update of [{reviewed:false},{severity:''},{start:''},{start:-1},{end:1},{start:.1},{reviewer:''},{severity:1.5}]) {
    assert.throws(()=>makeAnnotation(clip(),{...fields,...update}));
  }
  assert.throws(()=>makeAnnotation({...clip(),kind:'3d'},{...fields,dimension:'appearance'}));
});

function response(bytes) {return {ok:true,arrayBuffer:async()=>bytes};}
test('preloaded bitmaps remain usable for backward seeks and close only on disposal',async()=>{
  let closed=0;
  const loaded=await loadClip(clip(),{fetcher:async()=>response(new ArrayBuffer(8)),decodeImage:async()=>({width:2,height:2,close(){closed++;}})});
  assert.equal(loaded.images.length,3);assert.equal(closed,0);assert.ok(loaded.audioBlob instanceof Blob);
  loaded.dispose();loaded.dispose();assert.equal(closed,3);
});

test('a frame read failure waits for in-flight decoding and releases all completed bitmaps',async()=>{
  let release,closed=0;
  const decoding=new Promise(resolve=>{release=resolve;});
  const pending=loadClip(clip(),{fetcher:async url=>url==='/1'?{ok:false}:response(new ArrayBuffer(8)),
    decodeImage:async()=>{await decoding;return {width:2,height:2,close(){closed++;}};}});
  let settled=false;pending.catch(()=>{settled=true;});await new Promise(r=>setTimeout(r,0));
  assert.equal(settled,false);release();await assert.rejects(pending);assert.ok(closed>0);
});

test('canceling during decode closes late results before the loader settles',async()=>{
  const controller=new AbortController();let release,closed=0;
  const gate=new Promise(resolve=>{release=resolve;});
  const pending=loadClip(clip(),{signal:controller.signal,fetcher:async()=>response(new ArrayBuffer(8)),
    decodeImage:async()=>{await gate;return {width:2,height:2,close(){closed++;}};}});
  await new Promise(r=>setTimeout(r,0));controller.abort();release();await assert.rejects(pending);assert.equal(closed,3);
});

test('3D replay preserves little-endian vertex coordinates and rejects nonfinite geometry',async()=>{
  const detail={...clip(),kind:'3d',vertexCount:3,frames:clip().frames.slice(0,1)};
  const bytes=new ArrayBuffer(36),view=new DataView(bytes);for(let i=0;i<9;i++)view.setFloat32(i*4,i/8,true);
  const opts={fetcher:async url=>response(url==='/audio'?new ArrayBuffer(8):bytes)};
  const loaded=await loadClip(detail,opts);assert.equal(loaded.vertices[0][7],.875);loaded.dispose();
  view.setFloat32(4,NaN,true);await assert.rejects(loadClip(detail,opts));
});

test('decoded-size bounds are enforced before requesting any media',async()=>{
  let fetched=0;
  await assert.rejects(loadClip({...clip(),width:2048,height:2048,frames:Array.from({length:100},(_,i)=>({index:i,pts:i/25,url:'/frame'}))},
    {fetcher:async()=>{fetched++;return response(new ArrayBuffer(8));}}));
  assert.equal(fetched,0);
});

test('cancel releases already decoded bitmaps while waiting for late decoders to settle',async()=>{
  const controller=new AbortController();let release,decoded=0,closed=0;
  const gate=new Promise(resolve=>{release=resolve;});
  const pending=loadClip(clip(),{signal:controller.signal,fetcher:async()=>response(new ArrayBuffer(8)),
    decodeImage:async()=>{if(decoded++>0)await gate;return {width:2,height:2,close(){closed++;}};}});
  pending.catch(()=>{});await new Promise(r=>setTimeout(r,0));assert.equal(closed,0);
  controller.abort();assert.equal(closed,1,'ready bitmap is freed immediately');
  release();await assert.rejects(pending);assert.equal(closed,3);
});

test('rapid selections wait for old cleanup and skip superseded queued clips',async()=>{
  const queue=createSelectionQueue(),started=[];let release,active=0,maximum=0,firstSignal;
  const gate=new Promise(resolve=>{release=resolve;});
  const first=queue.run(async signal=>{firstSignal=signal;started.push('first');active++;maximum=Math.max(maximum,active);await gate;active--;});
  await new Promise(r=>setTimeout(r,0));
  const second=queue.run(async()=>{started.push('second');});
  const third=queue.run(async()=>{started.push('third');active++;maximum=Math.max(maximum,active);active--;});
  assert.equal(firstSignal.aborted,true);assert.deepEqual(started,['first']);
  release();await Promise.all([first,second,third]);assert.deepEqual(started,['first','third']);assert.equal(maximum,1);
});
