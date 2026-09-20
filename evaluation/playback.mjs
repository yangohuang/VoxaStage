import {AvatarPlayer} from './assets/audio.mjs';
import {HeadRenderer,PortraitRenderer} from './assets/renderer.mjs';
const $=id=>document.getElementById(id),head=new HeadRenderer($('head')),portrait=new PortraitRenderer($('portrait'));
let active=null,generation=0,pendingFrame=null;
const player=new AvatarPlayer({
 onFrame(data,meta){if(meta.kind==='2d')portrait.render(data);else{head.setMeta(meta);head.render(data);}
  active?.rendered.push({frame:pendingFrame,atMs:performance.now(),audioTime:player.context.currentTime});},
 async decodeImage(bytes){
  const owner=active,entry={frame:owner.decodes.length,startedMs:performance.now()};owner.decodes.push(entry);
  const image=await createImageBitmap(new Blob([bytes],{type:'image/jpeg'}));entry.finishedMs=performance.now();return image;
 },
 onState(state){active?.states.push({state,atMs:performance.now()});},
 onError(){if(active)active.error='PlayerError';}
});
const originalPush=player.timeline.push.bind(player.timeline);
player.timeline.push=packet=>{active?.timelineReady.push({frame:packet.frame_index,atMs:performance.now()});return originalPush(packet);};
const advance=player.timeline.advance.bind(player.timeline);
player.timeline.advance=now=>{const result=advance(now);if(result.frame)pendingFrame=result.frame.frame_index;return result;};
const config=await(await fetch('/config')).json();
for(const provider of config.providers)$('provider').add(new Option(provider,provider));
for(const item of config.cases)$('sample').add(new Option(item.id+' · '+item.audio_duration_s.toFixed(2)+'秒',item.id));
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let instrumented=false;
async function run(provider=$('provider').value,caseId=$('sample').value){
 if(active)throw new Error('Already running');
 const row={provider,caseId,generation:++generation,status:'running',received:[],timelineReady:[],decodes:[],rendered:[],sources:[],states:[],startedMs:performance.now(),
  input:config.cases.find(x=>x.id===caseId),provenance:config.source_sha256};
 $('provider').value=provider;$('sample').value=caseId;
 active=row;window.lab.last=row;$('run').disabled=true;$('status').textContent='播放中';
 const before=player.metrics;let ws=null;
 try {
 await player.unlock();
 if(!instrumented){
  const create=player.context.createBufferSource.bind(player.context);
  player.context.createBufferSource=()=>{
   const source=create(),start=source.start.bind(source);
   source.start=(when,...args)=>{active?.sources.push({audioStart:when,duration:source.buffer.duration,atMs:performance.now()});return start(when,...args);};
   return source;
  };instrumented=true;
 }
 player.reset(generation);
 ws=new WebSocket(`${location.origin.replace('http','ws')}/ws?provider=${encodeURIComponent(provider)}&case=${encodeURIComponent(caseId)}`);
 row.stop=()=>{row.error='Canceled';ws.close();player.stop();};
 ws.onmessage=event=>{
  try{
   const data=JSON.parse(event.data);
   if(data.type==='stream_end'){row.streamEnd=data;if(data.status!=='passed')row.error=data.error_type||'StreamError';return;}
   row.received.push({type:data.type,frame:data.frame_index,atMs:performance.now(),adapterElapsedMs:data.adapter_elapsed_ms});
   if(data.type==='avatar_meta'){$('head').hidden=data.kind==='2d';$('portrait').hidden=data.kind!=='2d';}
   player.handle({...data,generation:row.generation,clip_id:caseId});
  }catch{row.error='ProtocolError';}
 };
 ws.onerror=()=>{row.error='SocketError';};
 ws.onclose=()=>{if(!row.streamEnd&&!row.error)row.error='PrematureClose';};
  const deadline=performance.now()+90000;
  while(performance.now()<deadline){
   if(row.error)throw new Error(row.error);
   if(row.streamEnd&&player.timeline.clips.length===0&&player.sources.size===0&&player.decodeQueue.length===0)break;
   await sleep(20);
  }
  if(!row.streamEnd||player.timeline.clips.length||player.sources.size)throw new Error('Timeout');
  row.status='passed';row.finishedMs=performance.now();row.metrics=player.metrics;
  for(const key of ['renderedFrames','scheduledSamples','droppedStale'])row.metrics[key]-=before[key];
  row.gaps=row.sources.slice(1).map((source,i)=>({left:i,right:i+1,ms:(source.audioStart-row.sources[i].audioStart-row.sources[i].duration)*1000})).filter(x=>x.ms>1);
  return row;
 }catch(error){row.status='failed';row.error=error.message;throw error;}
 finally{
  row.finishedMs??=performance.now();ws?.close();player.stop();delete row.stop;active=null;$('run').disabled=false;
  $('status').textContent=row.status;$('result').textContent=JSON.stringify({status:row.status,frames:row.rendered.length,gaps:row.gaps,error:row.error},null,2);
 }
}
window.lab={run,stop:()=>active?.stop(),player,config,last:null};
$('run').onclick=()=>run().catch(()=>{});$('stop').onclick=()=>window.lab.stop();
window.addEventListener('pagehide',()=>window.lab.stop());
$('run').disabled=false;$('status').textContent='可以播放';
