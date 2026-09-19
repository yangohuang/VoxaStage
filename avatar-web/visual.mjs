/** Explicit still-image attachment state, separate from microphone and playback. */
export class VisualState {
  constructor(){ this.reset(null,false); }
  reset(sessionId,enabled){this.sessionId=sessionId;this.enabled=!!enabled;this.sequence=0;this.pending=null;this.inflight=null;}
  get busy(){return this.inflight!==null;}
  request(operation,image){
    if(!this.enabled||!this.sessionId||this.busy)throw new Error('请等待画面确认后继续。');
    const sequence=++this.sequence;
    if(sequence>10000)throw new Error('画面提交次数已达上限，请重新连接。');
    const message={type:'visual',session_id:this.sessionId,sequence,operation};
    if(image)message.image={...image,id:`image-${sequence}`};
    this.inflight=message;return message;
  }
  stage(image){return this.request('set',image);}
  clear(){return this.request('clear');}
  ack(message){
    if(!this.inflight||message.sequence!==this.inflight.sequence)return false;
    if(message.state!==(this.inflight.operation==='set'?'pending':'cleared'))return false;
    this.pending=this.inflight.image||null;this.inflight=null;return true;
  }
  reject(message){if(message.sequence!==this.inflight?.sequence)return false;this.inflight=null;return true;}
  bound(message){
    if(!this.pending||!message.images?.some(i=>i.id===this.pending.id))return false;
    this.pending=null;return !this.busy;
  }
}

export class VisualInput {
  constructor({get,send,notice,changed=()=>{},fatal=notice}){
    this.get=get;this.send=send;this.notice=notice;this.changed=changed;this.fatal=fatal;
    this.state=new VisualState();this.epoch=0;this.stream=null;this.processing=false;this.timer=null;this.started=0;
    get('visual-file').addEventListener('change',()=>void this.file());
    get('visual-camera').addEventListener('click',()=>void this.camera());
    get('visual-snapshot').addEventListener('click',()=>void this.snapshot());
    get('visual-remove').addEventListener('click',()=>{try{this.submit(this.state.clear());}catch(e){notice(e.message);}});
    this.refresh();
  }
  get busy(){return this.processing||this.state.busy;}
  stopCamera(){this.stream?.getTracks().forEach(t=>t.stop());this.stream=null;this.get('visual-video').srcObject=null;}
  reset(sessionId=null,enabled=false){
    this.epoch++;clearTimeout(this.timer);this.stopCamera();this.processing=false;
    this.state.reset(sessionId,enabled);this.started=performance.now();this.get('visual-file').value='';this.refresh();
  }
  refresh(){
    const get=this.get;get('visual-panel').hidden=!this.state.enabled;
    get('visual-file').disabled=this.busy;get('visual-camera').disabled=this.busy;
    get('visual-camera').textContent=this.stream?'关闭摄像头':'打开摄像头';
    get('visual-snapshot').hidden=!this.stream;get('visual-snapshot').disabled=this.busy;
    get('visual-video').hidden=!this.stream;
    get('visual-remove').disabled=this.busy||!this.state.pending;
    const image=this.state.inflight?.image||this.state.pending;
    get('visual-preview').hidden=!image;
    if(image)get('visual-preview').src=`data:image/jpeg;base64,${image.data}`;
    else get('visual-preview').removeAttribute('src');
    get('visual-status').textContent=this.busy?'正在确认画面，请稍候…':this.state.pending?'画面已确认，将用于下一条文字或下一轮语音。':'可提交一张画面，再输入或说出问题；保留最近两张画面供比较。';
    this.changed();
  }
  submit(message){
    this.send(message);clearTimeout(this.timer);
    this.timer=setTimeout(()=>this.fatal('画面确认超时，请重新连接后提交。'),5000);this.refresh();
  }
  handle(message){
    let accepted=false;
    if(message.type==='visual_ack')accepted=this.state.ack(message);
    else if(message.type==='visual_error'){accepted=this.state.reject(message);if(accepted)this.notice(message.message);}
    else if(message.type==='visual_bound')this.state.bound(message);
    if(accepted)clearTimeout(this.timer);this.refresh();
  }
  async file(){
    const file=this.get('visual-file').files?.[0];if(!file||this.busy||!this.state.enabled)return;
    const epoch=this.epoch,stamp=performance.now()-this.started;
    this.processing=true;this.refresh();let bitmap;
    try{
      if(!['image/png','image/jpeg'].includes(file.type)||file.size>8*1024*1024)throw new Error('请选择8MB以内的JPEG或PNG图片。');
      bitmap=await createImageBitmap(file);
      await this.encode(bitmap,'upload',stamp,epoch);
    }catch(e){if(epoch===this.epoch)this.notice(e.message);}
    finally{bitmap?.close();if(epoch===this.epoch){this.processing=false;this.get('visual-file').value='';this.refresh();}}
  }
  async camera(){
    if(this.stream){this.stopCamera();this.refresh();return;}
    if(this.busy||!this.state.enabled)return;
    const epoch=this.epoch;this.processing=true;this.refresh();let stream;
    try{
      stream=await navigator.mediaDevices.getUserMedia({video:{width:{ideal:640},height:{ideal:480}},audio:false});
      if(epoch!==this.epoch){stream.getTracks().forEach(t=>t.stop());return;}
      this.stream=stream;const video=this.get('visual-video');video.srcObject=stream;await video.play();
    }catch(e){stream?.getTracks().forEach(t=>t.stop());if(epoch===this.epoch){this.stopCamera();this.notice(`摄像头未开启：${e.message}`);}}
    finally{if(epoch===this.epoch){this.processing=false;this.refresh();}}
  }
  async snapshot(){
    if(!this.stream||this.busy)return;
    const epoch=this.epoch,stamp=performance.now()-this.started;this.processing=true;this.refresh();
    try{await this.encode(this.get('visual-video'),'camera',stamp,epoch);}
    catch(e){if(epoch===this.epoch)this.notice(e.message);}
    finally{if(epoch===this.epoch){this.processing=false;this.refresh();}}
  }
  async encode(source,kind,stamp,epoch){
    if(epoch!==this.epoch||!this.state.enabled)return;
    const width=source.videoWidth||source.width,height=source.videoHeight||source.height;
    if(!width||!height)throw new Error('画面尚未准备好，请重试。');
    const scale=Math.min(1,512/Math.max(width,height)),canvas=document.createElement('canvas');
    canvas.width=Math.max(1,Math.round(width*scale));canvas.height=Math.max(1,Math.round(height*scale));
    canvas.getContext('2d').drawImage(source,0,0,canvas.width,canvas.height);
    const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.82));
    if(!blob||blob.size>262144)throw new Error('画面压缩后仍过大，请选择更简单的图片。');
    const bytes=new Uint8Array(await blob.arrayBuffer());let binary='';for(const b of bytes)binary+=String.fromCharCode(b);
    if(epoch!==this.epoch||!this.state.enabled)return;
    this.submit(this.state.stage({source:kind,captured_at_ms:Math.round(stamp),data:btoa(binary)}));
  }
}
