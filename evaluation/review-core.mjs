const DIMENSIONS = new Set(['timing','phonetics','continuity','appearance','pose']);

export function createSelectionQueue() {
  let pending=Promise.resolve(),controller=null,version=0;
  return {
    async run(task) {
      const mine=++version,previous=pending;controller?.abort();
      const own=new AbortController();controller=own;let release;
      pending=new Promise(resolve=>{release=resolve;});
      try {
        await previous;
        if(mine!==version)return;
        return await task(own.signal,()=>mine===version);
      } finally {release();}
    },
    cancel(){version++;controller?.abort();},
  };
}

export function frameAt(time, frames) {
  if (!Number.isFinite(time) || !frames.length) throw new Error('无效播放位置');
  let lo=0, hi=frames.length;
  while(lo<hi) { const mid=(lo+hi)>>1; if(frames[mid].pts<=time)lo=mid+1;else hi=mid; }
  return Math.max(0,lo-1);
}

export function makeAnnotation(clip, fields) {
  const number=value=>value!=='' && value!==null && value!==undefined && String(value).trim()!=='' ? Number(value) : NaN;
  const start=number(fields.start),end=number(fields.end),severity=number(fields.severity);
  const reviewer=String(fields.reviewer??'').trim(),notes=String(fields.notes??'').trim();
  if(fields.reviewed!==true)throw new Error('请确认已复核选定时间段');
  if(!reviewer || reviewer.length>80)throw new Error('请填写80字以内的复核者代号');
  if(!['human','automated'].includes(fields.reviewerKind))throw new Error('请选择复核来源');
  if(!DIMENSIONS.has(fields.dimension) || (clip.kind==='3d'&&fields.dimension==='appearance'))throw new Error('此样片不适用该评估维度');
  if(!Number.isFinite(start)||!Number.isFinite(end)||start<0||start>=end||end>clip.duration)throw new Error('请填写样片内有效的起止时间');
  if(!Number.isInteger(severity)||severity<0||severity>3)throw new Error('请选择严重程度，未评估不能填写为0');
  if(notes.length>2000)throw new Error('备注不能超过2000字');
  return {clipId:clip.id,fingerprint:clip.fingerprint,reviewer,reviewerKind:fields.reviewerKind,
    dimension:fields.dimension,start,end,severity,notes,reviewed:true};
}

export async function loadClip(detail, {fetcher=fetch, decodeImage=globalThis.createImageBitmap, signal}={}) {
  const frames=detail.frames;
  const bytesPerFrame=detail.kind==='2d'?detail.width*detail.height*4:detail.vertexCount*12;
  if(!['2d','3d'].includes(detail.kind)||!Array.isArray(frames)||!frames.length||frames.length>1801
    ||!Number.isFinite(bytesPerFrame)||bytesPerFrame<=0||bytesPerFrame*frames.length>256*1024*1024
    ||!Number.isFinite(detail.duration)||detail.duration<=0||detail.duration>30)throw new Error('样片超出预载范围');
  const controller=new AbortController(),images=[],vertices=[];
  let disposed=false,received=0,next=0,failure=null,audioBlob;
  const dispose=()=>{if(disposed)return;disposed=true;for(const image of images)image?.close();images.length=0;vertices.length=0;};
  const abort=()=>{controller.abort();dispose();};signal?.addEventListener('abort',abort,{once:true});
  if(signal?.aborted)abort();
  const check=()=>{if(controller.signal.aborted)throw new Error('样片加载已取消');};
  const read=async(url,limit)=>{
    check();const response=await fetcher(url,{signal:controller.signal});
    if(!response.ok)throw new Error('样片文件读取或校验失败');
    const bytes=await response.arrayBuffer();received+=bytes.byteLength;
    if(bytes.byteLength>limit||received>128*1024*1024)throw new Error('样片文件超出大小限制');
    check();return bytes;
  };
  const guard=task=>task.catch(error=>{failure??=error;abort();throw error;});
  const worker=async()=>{
    while(next<frames.length){
      check();const index=next++,bytes=await read(frames[index].url,detail.kind==='2d'?1400000:bytesPerFrame);
      if(detail.kind==='2d'){
        const image=await decodeImage(new Blob([bytes],{type:'image/jpeg'}));
        if(controller.signal.aborted||image.width!==detail.width||image.height!==detail.height){
          image.close();check();throw new Error('画面尺寸与捕获记录不符');
        }
        images[index]=image;
      }else{
        if(bytes.byteLength!==bytesPerFrame)throw new Error('网格长度与捕获记录不符');
        const view=new DataView(bytes),values=new Float32Array(detail.vertexCount*3);
        for(let i=0;i<values.length;i++){
          values[i]=view.getFloat32(i*4,true);if(!Number.isFinite(values[i]))throw new Error('网格包含无效坐标');
        }
        vertices[index]=values;
      }
    }
  };
  try{
    const audio=guard((async()=>{audioBlob=new Blob([await read(detail.audioUrl,1600000)],{type:'audio/wav'});})());
    await Promise.allSettled([audio,...Array.from({length:Math.min(4,frames.length)},()=>guard(worker()))]);
    if(failure)throw failure;check();
    return {audioBlob,images,vertices,dispose};
  }catch(error){dispose();throw error;}
  finally{signal?.removeEventListener('abort',abort);}
}
