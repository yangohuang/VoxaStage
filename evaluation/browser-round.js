/* Run after a real Connect click has unlocked AudioContext. Reuses product controls. */
window.voxaRecoveryComplete = (metrics, events, generation) =>
  events.some(x=>x.type==='clip_end'&&x.generation===generation)
  && !metrics.playing && metrics.scheduledSources===0 && metrics.pendingFrames===0 && metrics.idle;
window.runVoxaBrowserRound = async function(label) {
  const app=window.avatarDemo,probe=window.voxaProbe;
  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const wait=async(predicate,name,seconds=90)=>{
    const deadline=performance.now()+seconds*1000;
    while(performance.now()<deadline){
      const notice=document.getElementById('notice');
      if(!notice.hidden)throw new Error('Page reported a service error');
      if(predicate())return;
      await sleep(20);
    }
    throw new Error('Timeout: '+name);
  };
  if(!app.metrics.connected||app.metrics.microphone)throw new Error('Expected connected, microphone off');
  try {
  probe.begin(label);
  const before=app.renderedFrames;
  document.getElementById('microphone').click();
  await wait(()=>app.metrics.microphone,'microphone start');
  await wait(()=>app.playing&&app.renderedFrames>=before+8&&app.metrics.scheduledSources>0,'actual scheduled playback');
  document.getElementById('microphone').click();
  await wait(()=>!app.metrics.microphone,'microphone stop');
  const first=probe.finish(label);
  if(first.lastActivePacketMs===null||first.endOfActivePacketToScheduledStartMs===null||first.endOfActivePacketToScheduledStartMs<0)
    throw new Error('No valid input/end-to-schedule observation');
  const generation=app.generation;
  const interrupt={requestedMs:performance.now(),before:app.metrics};
  document.getElementById('interrupt').click();
  interrupt.stoppedMs=performance.now();interrupt.after=app.metrics;
  if(interrupt.after.scheduledSources!==0)throw new Error('Scheduled sources survived manual interrupt');
  await wait(()=>app.generation>generation,'server reset',10);
  interrupt.serverResetMs=performance.now();
  const frames=app.renderedFrames;
  await sleep(250);
  interrupt.framesStable=app.renderedFrames===frames;
  if(!interrupt.framesStable)throw new Error('Old render frames survived interrupt');
  const recoveredBefore=app.renderedFrames;
  const recoveryStart=performance.now();
  await app.sendText('请只说你好。');
  await wait(()=>app.renderedFrames>recoveredBefore&&app.playing,'recovery playback');
  const recoveryFirstObservedMs=performance.now();
  await wait(()=>window.voxaRecoveryComplete(app.metrics,probe.events,app.generation),'recovery clip end, drain and idle');
  await sleep(200);
  const result={label,passed:true,first,interrupt,recovery:{requestedMs:recoveryStart,
    firstPlaybackObservedMs:recoveryFirstObservedMs,metrics:app.metrics},
    transcript:[...document.querySelectorAll('.message.user p')].map(x=>x.textContent).slice(-2),
    idleAfterPlayback:app.metrics.idle};
  app.disconnect();
  await sleep(300);
  result.disconnected=app.metrics;
  result.idleAfterDisconnect=app.metrics.idle;
  if(app.metrics.microphone||app.metrics.scheduledSources||!result.idleAfterPlayback||!result.idleAfterDisconnect)
    throw new Error('Cleanup or idle transition failed');
  return result;
  } finally { app.disconnect(); }
};
'installed';
