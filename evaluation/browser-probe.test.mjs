import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
const code=fs.readFileSync(new URL('./browser-probe.js',import.meta.url),'utf8');
function setup(){
 let now=0;
 class Socket {static OPEN=1;constructor(){this.handlers={};}addEventListener(k,fn){this.handlers[k]=fn;}send(){}receive(data){this.handlers.message({data:JSON.stringify(data)});}}
 class Source {constructor(){this.context={currentTime:1,sampleRate:24000};this.buffer={duration:.04};}addEventListener(){}start(){}stop(){}}
 const ctx={performance:{now:()=>now},WebSocket:Socket,AudioBufferSourceNode:Source,ArrayBuffer,DataView,console,
  avatarDemo:{generation:1,metrics:{playing:false}}};ctx.window=ctx;
 vm.runInNewContext(code,ctx);
 return {ctx,setTime:v=>now=v};
}
test('packet activity and scheduled audio share browser time, not server time',()=>{
 const {ctx,setTime}=setup();ctx.voxaProbe.begin('sample');
 const ws=new ctx.WebSocket();
 const pcm=new Int16Array([1000,1000]).buffer;
 setTime(200);ws.send(pcm);
 setTime(250);ws.receive({type:'media',generation:1,clip_id:'c',frame_index:0,start_sample:0});
 setTime(280);new ctx.AudioBufferSourceNode().start(1.12);
 const row=ctx.voxaProbe.finish('sample');
 assert.equal(row.endOfActivePacketToMediaMs,50);
 assert.ok(Math.abs(row.endOfActivePacketToScheduledStartMs-200)<1e-8);
 assert.ok(Math.abs(row.firstMediaToScheduledStartMs-150)<1e-8);
});
test('silence and canceled-before-start audio produce missing measurements',()=>{
 const {ctx,setTime}=setup();ctx.voxaProbe.begin('silence');
 const ws=new ctx.WebSocket();ws.send(new ArrayBuffer(640));
 const source=new ctx.AudioBufferSourceNode();setTime(100);source.start(2);setTime(200);source.stop();
 const row=ctx.voxaProbe.finish('silence');
 assert.equal(row.lastActivePacketMs,null);assert.equal(row.firstScheduledStartMs,null);
 assert.equal(row.endOfActivePacketToScheduledStartMs,null);
});
test('event overflow is explicit and does not grow without bound',()=>{
 const {ctx}=setup();ctx.voxaProbe.limit=2;ctx.voxaProbe.begin('x');
 const ws=new ctx.WebSocket();for(let i=0;i<10;i++)ws.receive({type:'media'});
 assert.equal(ctx.voxaProbe.events.length,2);assert.equal(ctx.voxaProbe.truncated,true);
});
test('backend error text is excluded from evidence',()=>{
 const {ctx}=setup();const ws=new ctx.WebSocket();
 ws.receive({type:'error',message:'ws://private?token=secret'});
 assert.equal(JSON.stringify(ctx.voxaProbe.snapshot()).includes('secret'),false);
});
