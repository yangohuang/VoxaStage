/* Observation-only probe injected before connecting; never sends audio or playback acknowledgements. */
(() => {
  if (window.voxaProbe) throw new Error('Probe already installed');
  const state = window.voxaProbe = {version:1,events:[],sources:[],rounds:[],round:0,
    packetCount:0,activePackets:0,lastActivePacketMs:null,firstPacketMs:null,
    scope:'browser WebSocket packet and AudioContext scheduling clocks; not physical sound',
    speechRmsThreshold:0.004, limit:12000, truncated:false};
  const record=(type,data={})=>{
    if(state.events.length>=state.limit){state.truncated=true;return;}
    state.events.push({type,atMs:performance.now(),round:state.round,...data});
  };
  state.begin=label=>{
    state.round++;state.lastActivePacketMs=null;state.firstPacketMs=null;
    state.packetCount=0;state.activePackets=0;
    record('round_begin',{label});
    return state.round;
  };
  const NativeSocket=window.WebSocket;
  window.WebSocket=class extends NativeSocket {
    constructor(...args){
      super(...args);
      this.addEventListener('message',event=>{
        if(typeof event.data!=='string')return;
        let data;try{data=JSON.parse(event.data)}catch{return}
        const type=data.type;
        if(type==='media'){
          record('media',{generation:data.generation,clipId:data.clip_id,
            frameIndex:data.frame_index,startSample:data.start_sample});
        } else if(['ready','reset','avatar_meta','clip_end','error','transcript','assistant_text','status'].includes(type)){
          record(type,{generation:data.generation,clipId:data.clip_id,
            state:data.state,text:type==='transcript'&&typeof data.text==='string'?data.text.slice(0,4000):undefined});
        }
      });
    }
    send(data){
      if(data instanceof ArrayBuffer){
        state.packetCount++;
        if(state.firstPacketMs===null)state.firstPacketMs=performance.now();
        const view=new DataView(data);let energy=0;
        for(let i=0;i+1<view.byteLength;i+=2){const s=view.getInt16(i,true)/32768;energy+=s*s;}
        const rms=Math.sqrt(energy/(view.byteLength/2));
        if(rms>state.speechRmsThreshold){state.lastActivePacketMs=performance.now();state.activePackets++;}
      }else if(typeof data==='string'){
        let message;try{message=JSON.parse(data)}catch{}
        if(message)record('sent_'+message.type,{generation:message.generation,state:message.state,
          lastActivePacketMs:state.lastActivePacketMs});
      }
      return super.send(data);
    }
  };
  const start=AudioBufferSourceNode.prototype.start,stop=AudioBufferSourceNode.prototype.stop;
  AudioBufferSourceNode.prototype.start=function(when=0,...args){
    const now=performance.now(),context=this.context;
    const row={round:state.round,generation:window.avatarDemo?.generation,
      createdMs:now,scheduledMs:now+Math.max(0,when-context.currentTime)*1000,
      audioStart:Math.max(when,context.currentTime),duration:this.buffer?.duration||0,
      contextRate:context.sampleRate,stoppedMs:null,endedMs:null};
    if(state.sources.length<state.limit)state.sources.push(row);else state.truncated=true;
    this.__voxaProbeRow=row;
    this.addEventListener('ended',()=>{row.endedMs=performance.now();});
    return start.call(this,when,...args);
  };
  AudioBufferSourceNode.prototype.stop=function(...args){
    if(this.__voxaProbeRow)this.__voxaProbeRow.stoppedMs=performance.now();
    return stop.apply(this,args);
  };
  state.snapshot=()=>({version:state.version,scope:state.scope,speechRmsThreshold:state.speechRmsThreshold,
    truncated:state.truncated,events:state.events,sources:state.sources,rounds:state.rounds});
  state.finish=label=>{
    const round=state.round,events=state.events.filter(x=>x.round===round);
    const sources=state.sources.filter(x=>x.round===round);
    const generation=window.avatarDemo?.generation;
    const first=events.find(x=>x.type==='media'&&x.generation===generation);
    const scheduled=sources.filter(x=>x.generation===generation&&(x.stoppedMs===null||x.stoppedMs>=x.scheduledMs));
    const earliest=scheduled.length?Math.min(...scheduled.map(x=>x.scheduledMs)):null;
    const result={round,label,lastActivePacketMs:state.lastActivePacketMs,
      firstPacketMs:state.firstPacketMs,packetCount:state.packetCount,activePackets:state.activePackets,
      firstMediaMs:first?.atMs??null,firstScheduledStartMs:earliest,
      endOfActivePacketToMediaMs:first&&state.lastActivePacketMs!==null?first.atMs-state.lastActivePacketMs:null,
      endOfActivePacketToScheduledStartMs:earliest!==null&&state.lastActivePacketMs!==null?earliest-state.lastActivePacketMs:null,
      firstMediaToScheduledStartMs:earliest!==null&&first?earliest-first.atMs:null,
      metrics:window.avatarDemo?.metrics};
    state.rounds.push(result);return result;
  };
  return {installed:true,scope:state.scope};
})()
