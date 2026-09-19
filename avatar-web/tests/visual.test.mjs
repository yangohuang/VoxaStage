import test from 'node:test';
import assert from 'node:assert/strict';
import { VisualState } from '../visual.mjs';

test('attachment acknowledgements and bindings cannot clear newer submissions',()=>{
 const s=new VisualState();s.reset('a',true);
 const first=s.stage({data:'abc',source:'upload',captured_at_ms:10});
 assert.equal(first.image.id,'image-1');assert.equal(s.busy,true);
 assert.throws(()=>s.stage({}));
 assert.equal(s.ack({sequence:9,state:'pending'}),false);
 assert.equal(s.ack({sequence:1,state:'pending'}),true);
 const second=s.stage({data:'def',source:'upload',captured_at_ms:20});
 assert.equal(second.sequence,2);
 assert.equal(s.bound({sequence:1,images:[{id:'image-1'}]}),false);
 s.ack({sequence:2,state:'pending'});
 assert.equal(s.bound({sequence:2,images:[{id:'image-2'}]}),true);
 assert.equal(s.pending,null);
});
test('disabled and disconnected sessions reject image submission',()=>{
 const s=new VisualState();assert.throws(()=>s.stage({}));s.reset('a',true);s.stage({data:'x'});
 s.reset(null,false);assert.equal(s.pending,null);assert.equal(s.busy,false);assert.throws(()=>s.stage({}));
});
test('rejected submission retains previous accepted image until clear acknowledged',()=>{
 const s=new VisualState();s.reset('a',true);s.stage({data:'old'});s.ack({sequence:1,state:'pending'});
 s.stage({data:'new'});s.reject({sequence:2});assert.equal(s.pending.data,'old');
 const clear=s.clear();assert.equal(clear.operation,'clear');assert.equal(s.pending.data,'old');
 s.ack({sequence:3,state:'cleared'});assert.equal(s.pending,null);
});

import { VisualInput } from '../visual.mjs';
function nodes(){const m=new Map();return id=>{if(!m.has(id))m.set(id,{hidden:false,value:'',files:[],addEventListener(){},removeAttribute(){},play:async()=>{}});return m.get(id);};}
test('camera granted after disconnect is stopped without attaching to next session',async()=>{
 let resolve,stopped=0;const original=Object.getOwnPropertyDescriptor(globalThis,'navigator');
 Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:()=>new Promise(r=>resolve=r)}}});
 const get=nodes(),v=new VisualInput({get,send(){},notice(){}});v.reset('a',true);
 try{const pending=v.camera();v.reset('b',true);resolve({getTracks:()=>[{stop(){stopped++;}}]});await pending;assert.equal(stopped,1);assert.equal(v.stream,null);assert.equal(get('visual-video').srcObject,null);}
 finally{v.reset();if(original)Object.defineProperty(globalThis,'navigator',original);else delete globalThis.navigator;}
});
test('file decoded after disconnect cannot submit or allocate a new preview',async()=>{
 let resolve,closed=0,sends=0,allocations=0;const original=globalThis.createImageBitmap,originalDocument=globalThis.document;
 globalThis.document={createElement(){allocations++;throw new Error('Stale preview allocation');}};
 globalThis.createImageBitmap=()=>new Promise(r=>resolve=r);
 const get=nodes(),v=new VisualInput({get,send(){sends++;},notice(message){throw new Error(message);}});v.reset('a',true);
 get('visual-file').files=[{type:'image/png',size:100}];
 try{const pending=v.file();v.reset();resolve({width:20,height:20,close(){closed++;}});await pending;assert.equal(allocations,0);assert.equal(sends,0);assert.equal(closed,1);assert.equal(v.state.pending,null);}
 finally{v.reset();globalThis.createImageBitmap=original;globalThis.document=originalDocument;}
});
