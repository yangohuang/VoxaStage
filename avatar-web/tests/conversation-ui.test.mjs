import test from 'node:test';
import assert from 'node:assert/strict';

class Element {
  constructor(id=''){this.id=id;this.children=[];this.listeners={};this.value='';this.textContent='';this.hidden=false;
    this.dataset={};this.classList={toggle(){},remove(){}};}
  addEventListener(name,handler){this.listeners[name]=handler;}
  async fire(name){await this.listeners[name]?.({preventDefault(){}});await settle();}
  append(...items){for(const item of items){item.parentNode=this;this.children.push(item);}}
  add(item){this.append(item);}
  replaceChildren(...items){this.children=[];this.append(...items);}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(item=>item!==this);}
  setAttribute(name,value){this[name]=value;}
  removeAttribute(name){delete this[name];}
  get firstElementChild(){return this.children[0];}
  getContext(){return null;}
  pause(){this.paused=true;}
  async play(){this.paused=false;}
  load(){}
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const cid='a'.repeat(32);
const profile={id:'kanghui256',label:'康辉 256',provider:'dinet',voice:'male',kind:'2d',configured:true,idle_url:'/avatar/idle/kanghui256.mp4'};
const second={...profile,id:'female',label:'女主播',provider:'flashhead',voice:'female',idle_url:'/avatar/idle/flashhead.mp4'};
const record={id:cid,backend:'cascade',profile_id:profile.id,title:'记住我的名字',transcript:[
  {role:'user',text:'我叫小林',status:'input'},
  {role:'assistant',text:'你好，小林。',status:'interrupted',heard_text:'你好，'}]};

async function fixture(t){
  const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);};
  const requests=[],sockets=[];
  const saved={};
  const replace=(name,value)=>{saved[name]=Object.getOwnPropertyDescriptor(globalThis,name);Object.defineProperty(globalThis,name,{value,writable:true,configurable:true});};
  replace('document',{getElementById:get,createElement:tag=>new Element(tag),querySelector:()=>get('audio-symbol')});
  replace('window',{addEventListener(){}});
  replace('location',{protocol:'http:',host:'localhost'});
  replace('Option',class extends Element{constructor(text,value){super();this.textContent=text;this.value=value;}});
  replace('setInterval',()=>0);
  replace('AudioContext',class{constructor(){this.state='running';}async resume(){}});
  replace('WebSocket',class{
    static OPEN=1;
    constructor(url){this.url=url;this.readyState=1;this.sent=[];sockets.push(this);}
    send(value){this.sent.push(JSON.parse(value));}
    close(){this.readyState=3;}
    receive(message){this.onmessage({data:JSON.stringify(message)});}
  });
  replace('fetch',async(url,options={})=>{
    requests.push({url,...options});let data;
    if(url==='/avatar/providers')data={providers:[{...profile,id:'dinet'},{...second,id:'flashhead'}],
      default_provider:'dinet',profiles:[profile,second],default_profile:profile.id,
      dialogue_backends:[{id:'cascade',label:'级联',configured:true},{id:'minicpm',label:'日常',configured:true}],default_backend:'cascade'};
    else if(url==='/avatar/conversations')data=options.method==='POST'?record:{conversations:[record]};
    else if(url===`/avatar/conversations/${cid}`)data=options.method==='DELETE'?{deleted:true}:record;
    else throw new Error(`Unexpected URL: ${url}`);
    return {ok:true,json:async()=>data};
  });
  t.after(async()=>{window.avatarDemo.disconnect();await settle();await settle();for(const[name,descriptor]of Object.entries(saved)){if(descriptor)Object.defineProperty(globalThis,name,descriptor);else delete globalThis[name];}});
  await import(`../app.mjs?case=${Math.random()}`);await settle();await settle();
  return {get,requests,sockets,api:window.avatarDemo};
}

function ready(ws,extra={}){ws.receive({type:'ready',generation:0,input_sample_rate:16000,kind:'2d',provider:'dinet',
  session_id:'transport',conversation_id:cid,profile_id:profile.id,tools:true,transcript:record.transcript,...extra});}

test('profile selection syncs driver/idle and the socket binds the selected saved conversation',async t=>{
  const {get,api,requests,sockets}=await fixture(t);
  assert.equal(get('profile').value,profile.id);
  assert.equal(get('idle-video').src,profile.idle_url);
  get('conversation-select').value=cid;await get('conversation-resume').fire('click');
  assert.equal(get('messages').children.length,2);
  assert.match(get('messages').children[1].children[0].textContent,/打断/);
  await api.connect();
  const url=new URL(sockets[0].url);
  assert.equal(url.searchParams.get('profile'),profile.id);
  assert.equal(url.searchParams.get('conversation'),cid);
  ready(sockets[0]);
  assert.equal(get('profile').disabled,true);
  assert.equal(get('conversation-delete').disabled,true);
  assert.equal(get('messages').children.length,2);
  assert.ok(requests.some(request=>request.url===`/avatar/conversations/${cid}`));
  api.disconnect();
  assert.equal(get('profile').disabled,false);
  get('profile').value=second.id;await get('profile').fire('change');
  assert.equal(get('provider').value,'flashhead');
  await api.connect();
  assert.equal(new URL(sockets[1].url).searchParams.has('conversation'),false);
});

test('new and delete use bounded conversation API and preserve model/profile choices',async t=>{
  const {get,requests}=await fixture(t);
  await get('conversation-new').fire('click');
  const created=requests.find(request=>request.method==='POST');
  assert.ok(created);
  assert.deepEqual(JSON.parse(created.body),{backend:'cascade',profile_id:profile.id});
  get('conversation-select').value=cid;await get('conversation-delete').fire('click');
  assert.ok(requests.some(request=>request.method==='DELETE'&&request.url.endsWith(cid)));
  assert.equal(get('messages').children.length,0);
});

test('task cards and streamed text reject stale generations and freeze after local interruption',async t=>{
  const {get,api,sockets}=await fixture(t);await api.connect();const ws=sockets[0];ready(ws,{transcript:[]});
  ws.receive({type:'reset',generation:1});
  ws.receive({type:'assistant_text',generation:0,text:'过期内容'});
  ws.receive({type:'agent_task',generation:0,task_id:'old',state:'running',label:'旧任务'});
  assert.equal(get('messages').children.length,0);
  assert.equal(get('task-list').children.length,0);
  ws.receive({type:'assistant_text',generation:1,text:'流式'});
  ws.receive({type:'assistant_text',generation:1,text:'回复'});
  assert.equal(get('messages').children[0].children[1].textContent,'流式回复');
  ws.receive({type:'agent_task',generation:1,task_id:'current',state:'running',label:'计算'});
  assert.match(get('task-list').children[0].textContent,/计算.*进行中/);
  ws.receive({type:'agent_task',generation:1,task_id:'current',state:'completed',label:'计算',result:{value:42}});
  assert.match(get('task-list').children[0].textContent,/已完成/);
  ws.receive({type:'agent_task',generation:1,task_id:'slow',state:'running',label:'搜索'});
  api.interrupt();
  assert.match(get('task-list').children[1].textContent,/已取消/);
  ws.receive({type:'assistant_text',generation:1,text:'打断后漏出'});
  ws.receive({type:'agent_task',generation:1,task_id:'slow',state:'completed',label:'搜索'});
  assert.equal(get('messages').children[0].children[1].textContent,'流式回复');
  assert.match(get('task-list').children[1].textContent,/已取消/);
  ws.receive({type:'reset',generation:2});
  ws.receive({type:'agent_task',generation:2,task_id:'current',state:'running',label:'新计算'});
  ws.receive({type:'agent_task',generation:2,task_id:'current',state:'failed',label:'新计算',error:'服务暂不可用'});
  assert.match(get('task-list').children[2].textContent,/新计算.*失败.*服务暂不可用/);
});

test('changing legacy driver synchronizes profile and blocks editing while connected',async t=>{
  const {get,api,sockets}=await fixture(t);
  get('provider').value='flashhead';await get('provider').fire('change');
  assert.equal(get('profile').value,second.id);
  assert.equal(get('idle-video').src,second.idle_url);
  await api.connect();
  ready(sockets[0],{provider:'flashhead',profile_id:second.id,tools:false,transcript:[]});
  assert.match(get('task-capability').textContent,/日常对话/);
  const count=sockets.length;
  await get('conversation-new').fire('click');
  assert.equal(sockets.length,count);
  assert.equal(get('dialogue-backend').disabled,true);
});

test('conversation errors remain visible without changing the selected profile',async t=>{
  const {get}=await fixture(t);
  globalThis.fetch=async()=>({ok:false,json:async()=>({detail:'本地会话已删除'})});
  get('conversation-select').value=cid;await get('conversation-resume').fire('click');
  assert.equal(get('notice').textContent,'本地会话已删除');
  assert.equal(get('profile').value,profile.id);
  assert.equal(get('connect').disabled,false);
});
