/* Install before connecting. Injects controlled16k PCM at the browser transport boundary. */
(() => {
 if(window.voxaVoice)throw new Error('Voice probe already installed');
 const NativeSocket=window.WebSocket;
 const state=window.voxaVoice={socket:null,inputs:[],resets:[],last:null};
 window.WebSocket=class extends NativeSocket{
  constructor(...args){super(...args);state.socket=this;this.addEventListener('message',event=>{
   let data;try{data=JSON.parse(event.data)}catch{return}
   if(data.type==='reset')state.resets.push({generation:data.generation,reason:data.reason,atMs:performance.now()});
  });}
 };
 const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
 state.send=async(base64,label,signal)=>{
  if(typeof base64!=='string'||base64.length>1280000)throw new Error('Input too large');
  const raw=atob(base64);
  if(!raw.length||raw.length%2||raw.length>960000)throw new Error('Invalid PCM16 input');
  const pcm=Uint8Array.from(raw,c=>c.charCodeAt(0));
  const row={label,sampleRate:16000,samples:pcm.length/2,packets:[],startedMs:performance.now(),status:'running'};
  state.inputs.push(row);
  try{
   for(let offset=0;offset<pcm.length;offset+=640){
    if(signal?.aborted)throw new Error('Canceled');
    const socket=state.socket;
    if(!socket||socket.readyState!==NativeSocket.OPEN)throw new Error('Socket unavailable');
    if(socket.bufferedAmount>128000)throw new Error('Send buffer full');
    await sleep(Math.max(0,row.startedMs+offset/32-performance.now()));
    if(signal?.aborted)throw new Error('Canceled');
    if(socket!==state.socket||socket.readyState!==NativeSocket.OPEN)throw new Error('Socket unavailable');
    if(socket.bufferedAmount>128000)throw new Error('Send buffer full');
    const packet=pcm.slice(offset,offset+640);
    socket.send(packet.buffer);row.packets.push({startSample:offset/2,samples:packet.length/2,atMs:performance.now()});
   }
   row.status='passed';return row;
  }catch(error){row.status='failed';throw error;}
  finally{row.finishedMs=performance.now();}
 };
 state.run=async(first,second,label,{resetReason='speech_started'}={})=>{
  if(!['speech_started','interrupted'].includes(resetReason))throw new Error('Invalid reset reason');
  if(state.running)throw new Error('Round already running');
  const app=window.avatarDemo,probe=window.voxaProbe;
  if(!app.metrics.connected||app.metrics.microphone)throw new Error('Connect with physical microphone off');
  const controller=new AbortController(),row={label,status:'running',steps:[],scope:'controlled PCM transport injection; not microphone capture or physical sound'};
  state.last=row;state.running=row;let inflight=null,sendError=null;
  const wait=async(predicate,name,seconds=90)=>{
   const deadline=performance.now()+seconds*1000;
   while(performance.now()<deadline){
    if(sendError)throw sendError;
    if(!app.metrics.connected)throw new Error('Session disconnected');
    if(predicate()){row.steps.push({name,atMs:performance.now()});return;}
    await sleep(20);
   }
   throw new Error('Timeout: '+name);
  };
  try{
   probe.begin(label);row.round=probe.round;
   const before=app.renderedFrames;
   row.firstInput=await state.send(first,'first',controller.signal);
   await wait(()=>app.playing&&app.renderedFrames>=before+5&&app.metrics.scheduledSources>0,'first playback');
   row.beforeInterrupt=app.metrics;row.firstGeneration=app.generation;
   row.secondRequestedMs=performance.now();
   const secondSend=state.send(second,'second',controller.signal);
   // Catch immediately so a send failure cannot become an unhandled rejection.
   const secondDone=inflight=secondSend.then(value=>({value}),error=>{sendError=error;return {error};});
   await wait(()=>state.resets.some(x=>x.atMs>=row.secondRequestedMs&&x.generation>row.firstGeneration&&x.reason===resetReason),'speech-triggered reset',10);
   row.reset=state.resets.find(x=>x.atMs>=row.secondRequestedMs&&x.generation>row.firstGeneration&&x.reason===resetReason);
   if(probe.events.some(x=>x.round===row.round&&x.atMs>=row.secondRequestedMs&&x.atMs<=row.reset.atMs&&['sent_interrupt','sent_text'].includes(x.type)))throw new Error('Manual control contaminated voice interruption');
   row.afterReset=app.metrics;
   const old=probe.sources.filter(x=>x.round===row.round&&x.generation===row.firstGeneration&&x.createdMs<=row.reset.atMs);
   row.oldSourcesStopped=old.every(x=>x.stoppedMs!==null||x.endedMs!==null);
   const canceled=old.filter(x=>x.stoppedMs!==null&&x.stoppedMs>=row.secondRequestedMs);
   row.stoppedSourceCount=canceled.length;
   row.resetToLastStopMs=canceled.length?Math.max(...canceled.map(x=>x.stoppedMs))-row.reset.atMs:null;
   if(!old.length||!row.oldSourcesStopped||!canceled.length)throw new Error('No active sources canceled by voice reset');
   await wait(()=>app.generation>row.firstGeneration&&app.playing&&app.metrics.scheduledSources>0,'second playback');
   row.recovered=app.metrics;
   const sent=await secondDone;if(sent.error)throw sent.error;row.secondInput=sent.value;
   await wait(()=>probe.events.some(x=>x.round===row.round&&x.type==='clip_end'&&x.generation===app.generation)&&!app.playing&&app.metrics.scheduledSources===0&&app.metrics.pendingFrames===0&&app.metrics.idle,'recovery drain and idle');
   row.final=app.metrics;row.status='passed';return row;
  }catch(error){row.error=error.message;row.status='failed';throw error;}
  finally{controller.abort();app.disconnect();if(inflight)await inflight;row.finishedMs=performance.now();state.running=null;}
 };
 return 'installed';
})()
