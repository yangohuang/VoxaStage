import {HeadRenderer} from './renderer.mjs';
import {frameAt,makeAnnotation,loadClip,createSelectionQueue} from './review-core.mjs';

const $=id=>document.getElementById(id),audio=$('audio'),canvas=$('portrait'),context=canvas.getContext('2d');
const categories={zh:'中文',en:'英文',mixed:'中英混合',silence:'静音',pause:'句中停顿',other:'其他'};
let head=null,current=null,annotations=[],catalog=[];
const selections=createSelectionQueue();
const controls=['previous','play','next','frame','mark-start','mark-end','save'];
const enable=value=>controls.forEach(id=>{$(id).disabled=!value;});
const status=text=>{$('status').textContent=text;};
const resetForm=()=>{$('severity').value='';$('confirmed').checked=false;$('notes').value='';$('save-status').textContent='';};
function clearCurrent(){
  audio.pause();audio.removeAttribute('src');audio.load();
  if(current){URL.revokeObjectURL(current.url);current.resources.dispose();current=null;}
  head?.clear();context.clearRect(0,0,canvas.width,canvas.height);
}
function draw(index,reason='seek',force=false){
  if(!current||(!force&&index===current.index))return;
  const {detail,resources,metrics}=current;
  if(detail.kind==='2d'){
    const ratio=Math.min(devicePixelRatio||1,2);
    canvas.width=Math.max(1,Math.round(canvas.clientWidth*ratio));canvas.height=Math.max(1,Math.round(canvas.clientHeight*ratio));
    const image=resources.images[index],scale=Math.min(canvas.width/image.width,canvas.height/image.height);
    context.clearRect(0,0,canvas.width,canvas.height);
    context.drawImage(image,(canvas.width-image.width*scale)/2,(canvas.height-image.height*scale)/2,image.width*scale,image.height*scale);
  }else{head.render(resources.vertices[index]);}
  current.index=index;metrics.displayed.add(index);if(reason==='playback')metrics.played.add(index);
  if(metrics.events.length<2000)metrics.events.push({index,reason,audioTime:audio.currentTime,atMs:performance.now()});
  $('frame').value=index;$('position').textContent=`${index+1} / ${detail.frames.length} · ${detail.frames[index].pts.toFixed(3)}s`;
}
function seek(index){if(!current?.ready)return;audio.pause();const i=Math.max(0,Math.min(current.detail.frames.length-1,index));audio.currentTime=current.detail.frames[i].pts;draw(i);}
function metadata(signal){
  return new Promise((resolve,reject)=>{
    const cleanup=()=>{clearTimeout(timer);audio.removeEventListener('loadedmetadata',loaded);audio.removeEventListener('error',failed);signal.removeEventListener('abort',canceled);};
    const loaded=()=>{cleanup();resolve();},failed=()=>{cleanup();reject(new Error('音频文件无法播放'));},canceled=()=>{cleanup();reject(new Error('样片加载已取消'));};
    const timer=setTimeout(failed,10000);audio.addEventListener('loadedmetadata',loaded,{once:true});audio.addEventListener('error',failed,{once:true});signal.addEventListener('abort',canceled,{once:true});
    if(signal.aborted)canceled();else if(audio.readyState>=1)loaded();
  });
}
async function select(id){
  enable(false);clearCurrent();resetForm();$('error').textContent='';status('校验并预载样片…');$('clip').value=id;
  return selections.run(async(signal,isCurrent)=>{
  let resources=null;
  try{
    const response=await fetch('/api/clips/'+encodeURIComponent(id),{signal});if(!response.ok)throw new Error('无法读取样片清单');
    const detail=await response.json();resources=await loadClip(detail,{signal});
    if(!isCurrent()){resources.dispose();return;}
    if(detail.kind==='3d'&&!head)head=new HeadRenderer($('head'));
    current={detail,resources,url:URL.createObjectURL(resources.audioBlob),index:-1,ready:false,
      metrics:{displayed:new Set(),played:new Set(),events:[],completedPlaybackCount:0}};
    $('head').hidden=detail.kind!=='3d';canvas.hidden=detail.kind!=='2d';
    if(detail.kind==='3d')head.setMeta({clip_id:detail.id,faces:detail.faces});
    $('kind').textContent=`${detail.kind.toUpperCase()} · ${categories[detail.category]||'其他'} · ${detail.duration.toFixed(2)}s`;
    $('frame').max=detail.frames.length-1;$('start').value='0';$('end').value=String(detail.duration);
    $('dimension').querySelector('[value="appearance"]').disabled=detail.kind==='3d';
    if(detail.kind==='3d'&&$('dimension').value==='appearance')$('dimension').value='continuity';
    audio.src=current.url;audio.load();draw(0,'preview');await metadata(signal);
    if(!isCurrent())return;
    if(Math.abs(audio.duration-detail.duration)>.01)throw new Error('音频时长与捕获记录不符');
    current.ready=true;enable(true);status('可以播放 · 尚未提交质量判断');
  }catch(error){
    if(isCurrent()){clearCurrent();resources?.dispose();$('error').textContent=error.message;status('样片未就绪');}
    else resources?.dispose();
  }
  });
}
async function refreshAnnotations(){
  const response=await fetch('/api/annotations');if(!response.ok)throw new Error('无法读取复核记录');
  annotations=(await response.json()).annotations;$('records').replaceChildren();$('empty').hidden=annotations.length>0;
  for(const row of annotations){
    const tr=document.createElement('tr');
    for(const value of [`${row.clipId} / ${row.reviewerKind==='human'?'人工':'自动'} · ${row.reviewer}`,`${row.start.toFixed(3)}–${row.end.toFixed(3)}`,`${row.dimension} / ${row.severity}`,row.notes]){
      const td=document.createElement('td');td.textContent=value;tr.append(td);
    }
    $('records').append(tr);
  }
}
$('clip').onchange=()=>select($('clip').value);
$('play').onclick=async()=>{try{if(audio.paused)await audio.play();else audio.pause();}catch{$('error').textContent='播放未启动，请再次点击播放';}};
audio.onplay=()=>{$('play').textContent='暂停';};audio.onpause=()=>{$('play').textContent='播放';};
audio.onseeked=()=>{if(current?.ready)draw(frameAt(audio.currentTime,current.detail.frames),'seek');};
audio.onended=()=>{if(current){current.metrics.completedPlaybackCount++;draw(current.detail.frames.length-1,'ended');status('播放结束 · 请按实际观察记录，未评估项保持为空');}};
$('previous').onclick=()=>seek(current.index-1);$('next').onclick=()=>seek(current.index+1);$('frame').oninput=()=>seek(Number($('frame').value));
$('mark-start').onclick=()=>{$('start').value=audio.currentTime.toFixed(3);};
$('mark-end').onclick=()=>{$('end').value=Math.min(current.detail.duration,Number(audio.currentTime.toFixed(3)));};
$('save').onclick=async()=>{
  $('save').disabled=true;$('save-status').textContent='';
  try{
    const row=makeAnnotation(current.detail,{reviewer:$('reviewer').value,reviewerKind:$('reviewer-kind').value,
      dimension:$('dimension').value,start:$('start').value,end:$('end').value,severity:$('severity').value,
      notes:$('notes').value,reviewed:$('confirmed').checked});
    const response=await fetch('/api/annotations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(row)});
    if(!response.ok)throw new Error('记录未保存，请检查范围及样片版本');
    await refreshAnnotations();$('save-status').textContent='已保存到本地复核目录';$('confirmed').checked=false;
  }catch(error){$('save-status').textContent=error.message;}
  finally{$('save').disabled=!current?.ready;}
};
$('export').onclick=()=>{
  const url=URL.createObjectURL(new Blob([JSON.stringify({version:1,annotations},null,2)],{type:'application/json'}));
  const anchor=document.createElement('a');anchor.href=url;anchor.download='quality-annotations.json';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
function tick(){if(current?.ready&&!audio.paused)draw(frameAt(audio.currentTime,current.detail.frames),'playback');requestAnimationFrame(tick);}requestAnimationFrame(tick);
new ResizeObserver(()=>{if(current)draw(current.index,'resize',true);}).observe($('portrait'));
window.addEventListener('pagehide',()=>{selections.cancel();clearCurrent();});
window.review={select,seek,get catalog(){return catalog;},get ready(){return Boolean(current?.ready);},
  get state(){return current?{id:current.detail.id,kind:current.detail.kind,fingerprint:current.detail.fingerprint,
    duration:current.detail.duration,frameCount:current.detail.frames.length,frame:current.index,
    displayedFrames:current.metrics.displayed.size,playedFrames:current.metrics.played.size,
    completedPlaybackCount:current.metrics.completedPlaybackCount,events:current.metrics.events}:null;}};
try{
  const response=await fetch('/api/clips');if(!response.ok)throw new Error('无法读取本地样片');
  catalog=(await response.json()).clips;
  for(const item of catalog)$('clip').add(new Option(`${item.id} · ${categories[item.category]||'其他'} · ${item.kind.toUpperCase()}`,item.id));
  if(!catalog.length)throw new Error('没有可复核的成功捕获');
  $('clip').disabled=false;await refreshAnnotations();await select(catalog[0].id);
}catch(error){$('error').textContent=error.message;status('复核页未就绪');}
