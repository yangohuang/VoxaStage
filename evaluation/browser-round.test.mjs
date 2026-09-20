import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
const code=fs.readFileSync(new URL('./browser-round.js',import.meta.url),'utf8');
test('a microphone-start timeout disconnects the test session',async()=>{
 let now=0,disconnected=false,clicked=false;
 const app={metrics:{connected:true,microphone:false},renderedFrames:0,disconnect(){disconnected=true;}};
 const ctx={avatarDemo:app,voxaProbe:{begin(){}},performance:{now:()=>now},
  setTimeout(fn,ms){now+=ms;queueMicrotask(fn)},document:{getElementById(id){
   if(id==='notice')return {hidden:true};return {click(){clicked=true;}};
  }}};ctx.window=ctx;
 vm.runInNewContext(code,ctx);
 await assert.rejects(ctx.runVoxaBrowserRound('timeout'),/Timeout/);
 assert.equal(clicked,true);assert.equal(disconnected,true);
});
test('temporary queue drain before clip end is not recovery completion',()=>{
 const ctx={};ctx.window=ctx;vm.runInNewContext(code,ctx);
 const metrics={generation:3,playing:false,scheduledSources:0,pendingFrames:0,idle:true};
 assert.equal(ctx.voxaRecoveryComplete(metrics,[],3),false);
 assert.equal(ctx.voxaRecoveryComplete(metrics,[{type:'clip_end',generation:2}],3),false);
 assert.equal(ctx.voxaRecoveryComplete(metrics,[{type:'clip_end',generation:3}],3),true);
 assert.equal(ctx.voxaRecoveryComplete({...metrics,idle:false},[{type:'clip_end',generation:3}],3),false);
});
