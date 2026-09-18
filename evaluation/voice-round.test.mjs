import test from 'node:test';import assert from 'node:assert/strict';import vm from 'node:vm';import fs from 'node:fs';
const code=fs.readFileSync(new URL('./voice-round.js',import.meta.url),'utf8');
function setup(){let now=0;const sent=[];class Socket{static OPEN=1;constructor(){this.readyState=1;this.bufferedAmount=0;}addEventListener(){}send(buffer){sent.push(new Uint8Array(buffer));}}const ctx={WebSocket:Socket,performance:{now:()=>now},setTimeout(fn,ms){now+=ms;queueMicrotask(fn);},atob:s=>Buffer.from(s,'base64').toString('binary'),Uint8Array,AbortController};ctx.window=ctx;vm.runInNewContext(code,ctx);new ctx.WebSocket();return {ctx,sent};}
test('PCM packet boundaries are exact with a short final packet',async()=>{const {ctx,sent}=setup();const bytes=Buffer.alloc(1300,7);const row=await ctx.voxaVoice.send(bytes.toString('base64'),'sample');assert.equal(row.samples,650);assert.deepEqual(row.packets.map(x=>x.startSample).join(','),'0,320,640');assert.deepEqual(sent.map(x=>x.length),[640,640,20]);assert.equal(row.status,'passed');});
test('odd PCM and send overflow fail without sending',async()=>{const {ctx,sent}=setup();await assert.rejects(ctx.voxaVoice.send('AA==','odd'),/Invalid/);ctx.voxaVoice.socket.bufferedAmount=128001;await assert.rejects(ctx.voxaVoice.send('AAA=','full'),/buffer/);assert.equal(sent.length,0);assert.equal(ctx.voxaVoice.inputs[0].status,'failed');});
test('aborted injection is terminal before send',async()=>{const {ctx,sent}=setup();const abort=new AbortController();abort.abort();await assert.rejects(ctx.voxaVoice.send('AAA=','cancel',abort.signal),/Canceled/);assert.equal(sent.length,0);});

test('a socket closing during pacing fails before calling send', async () => {
 const {ctx,sent}=setup();
 ctx.setTimeout=callback=>{
  ctx.voxaVoice.socket.readyState=3;
  queueMicrotask(callback);
 };
 await assert.rejects(ctx.voxaVoice.send('AAA=','closed-during-wait'),/Socket unavailable/);
 assert.equal(sent.length,0);
 assert.equal(ctx.voxaVoice.inputs[0].status,'failed');
});

function roundSetup() {
 const env=setup(), {ctx}=env;
 const status={connected:true,microphone:false,scheduledSources:0,pendingFrames:0,idle:true};
 const app={
  generation:1,renderedFrames:0,playing:false,disconnectCalls:0,
  get metrics(){return {...status,playing:this.playing,generation:this.generation};},
  disconnect(){this.disconnectCalls++;status.connected=false;status.scheduledSources=0;this.playing=false;},
 };
 const probe={round:0,sources:[],events:[],begin(){this.round++;}};
 ctx.avatarDemo=app;ctx.voxaProbe=probe;
 const first=()=>{
  app.renderedFrames+=5;app.playing=true;
  status.scheduledSources=1;status.idle=false;
  probe.sources.push({round:probe.round,generation:1,createdMs:ctx.performance.now(),stoppedMs:null,endedMs:null});
  return {status:'passed',label:'first'};
 };
 const reset=({stop=true,reason='speech_started'}={})=>{
  const atMs=ctx.performance.now();
  ctx.voxaVoice.resets.push({generation:2,reason,atMs});
  if(stop)probe.sources[0].stoppedMs=atMs;
  else probe.sources[0].endedMs=atMs;
  app.generation=2;app.renderedFrames+=5;app.playing=true;
  status.scheduledSources=1;
 };
 return {...env,app,probe,status,first,reset};
}

test('second-input failure preserves its original cause and disconnects promptly', async () => {
 const {ctx,app,first}=roundSetup();
 ctx.voxaVoice.send=async(_,label)=>{
  if(label==='first')return first();
  throw new Error('Send buffer full');
 };
 const began=ctx.performance.now();
 await assert.rejects(ctx.voxaVoice.run('first','second','send-error'),/Send buffer full/);
 assert.equal(ctx.voxaVoice.last.status,'failed');
 assert.equal(ctx.voxaVoice.last.error,'Send buffer full');
 assert.ok(ctx.voxaVoice.last.finishedMs-began<100,'must not wait for the reset deadline');
 assert.equal(app.disconnectCalls,1);
});

test('failure cleanup waits for the aborted in-flight sender to settle', async () => {
 const {ctx,app,status,first}=roundSetup();
 let releaseSender,aborted=false,settled=false;
 ctx.voxaVoice.send=(_,label,signal)=>{
  if(label==='first')return Promise.resolve(first());
  status.connected=false;
  return new Promise((_,reject)=>{
   releaseSender=()=>reject(new Error('Canceled'));
   signal.addEventListener('abort',()=>{aborted=true;});
  });
 };
 const outcome=ctx.voxaVoice.run('first','second','disconnect-during-send')
  .then(value=>({value}),error=>({error}))
  .then(result=>{settled=true;return result;});
 try {
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(aborted,true);
  assert.equal(settled,false,'run must not return a snapshot while the sender is still changing it');
 } finally {
  releaseSender();
  const result=await outcome;
  assert.match(result.error?.message??'',/Session disconnected/);
 }
 assert.equal(app.disconnectCalls,1);
 assert.equal(ctx.voxaVoice.last.status,'failed');
});

test('success requires voice reset, a canceled old source, new playback, and completed drain', async () => {
 const {ctx,app,probe,status,first,reset}=roundSetup();
 ctx.voxaVoice.send=async(_,label)=>{
  if(label==='first')return first();
  reset();
  return {status:'passed',label:'second'};
 };
 const sleep=ctx.setTimeout;
 ctx.setTimeout=(callback,ms)=>{
  // Keep the recovered source active until the runner actually waits for drain.
  if(app.generation===2){
   probe.events.push({round:probe.round,type:'clip_end',generation:2});
   app.playing=false;status.scheduledSources=0;status.pendingFrames=0;status.idle=true;
  }
  sleep(callback,ms);
 };
 const row=await ctx.voxaVoice.run('first','second','voice-success');
 assert.equal(row.status,'passed');
 assert.equal(row.reset.reason,'speech_started');
 assert.equal(row.oldSourcesStopped,true);
 assert.equal(row.stoppedSourceCount,1);
 assert.equal(row.recovered.generation,2);
 assert.equal(row.recovered.playing,true);
 assert.equal(row.final.scheduledSources,0);
 assert.equal(row.final.idle,true);
 assert.equal(row.steps.map(step=>step.name).join(','),
  'first playback,speech-triggered reset,second playback,recovery drain and idle');
 assert.equal(row.firstInput.status,'passed');
 assert.equal(row.secondInput.status,'passed');
 assert.equal(app.disconnectCalls,1);
});

test('natural source completion is not evidence of voice cancellation', async () => {
 const {ctx,app,first,reset}=roundSetup();
 ctx.voxaVoice.send=async(_,label)=>{
  if(label==='first')return first();
  reset({stop:false});
  return {status:'passed'};
 };
 await assert.rejects(ctx.voxaVoice.run('first','second','natural-end'),/No active sources canceled/);
 assert.equal(ctx.voxaVoice.last.status,'failed');
 assert.equal(ctx.voxaVoice.last.stoppedSourceCount,0);
 assert.equal(app.disconnectCalls,1);
});

function cascadeRound({sentControl=null}={}) {
 const env=roundSetup(),{ctx,app,probe,status,first,reset}=env;
 ctx.voxaVoice.send=async(_,label)=>{
  if(label==='first')return first();
  await new Promise(resolve=>ctx.setTimeout(resolve,1));
  if(sentControl)probe.events.push({round:probe.round,type:sentControl,atMs:ctx.performance.now()});
  await new Promise(resolve=>ctx.setTimeout(resolve,1));
  reset({reason:'interrupted'});
  return {status:'passed',label:'second'};
 };
 const sleep=ctx.setTimeout;
 ctx.setTimeout=(callback,ms)=>{
  // Only drain after recovery was observed; an unrecognized reset must time out.
  if(ctx.voxaVoice.last?.recovered){
   probe.events.push({round:probe.round,type:'clip_end',generation:2});
   app.playing=false;status.scheduledSources=0;status.pendingFrames=0;status.idle=true;
  }
  sleep(callback,ms);
 };
 return env;
}

test('cascade accepts an explicitly selected interrupted reset from voice input', async () => {
 const {ctx,app}=cascadeRound();
 const row=await ctx.voxaVoice.run('first','second','cascade-voice',{resetReason:'interrupted'});
 assert.equal(row.status,'passed');
 assert.equal(row.reset.reason,'interrupted');
 assert.equal(row.stoppedSourceCount,1);
 assert.equal(row.final.idle,true);
 assert.equal(app.disconnectCalls,1);
});

for(const control of ['sent_interrupt','sent_text']) {
 test(`cascade rejects ${control} between second input and reset`, async () => {
  const {ctx,app}=cascadeRound({sentControl:control});
  const began=ctx.performance.now();
  await assert.rejects(ctx.voxaVoice.run('first','second','cascade-control',{resetReason:'interrupted'}));
  const row=ctx.voxaVoice.last;
  assert.equal(row.status,'failed');
  assert.doesNotMatch(row.error,/Timeout/,'reject control contamination rather than missing the cascade reset');
  assert.ok(row.finishedMs-began<100,'reject the conflicting control as soon as reset is observed');
  assert.equal(app.disconnectCalls,1);
 });
}
