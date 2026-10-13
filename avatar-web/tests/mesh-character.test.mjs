import test from 'node:test';
import assert from 'node:assert/strict';
import { MeshCharacter } from '../mesh-character.mjs';
const profile=id=>({mesh_url:`/avatar/mesh/${id}.glb`,retarget_url:`/avatar/mesh/${id}.json`});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const renderer=()=>({disposed:false,mounted:false,mount(){this.mounted=true;},cleared:0,dispose(){this.disposed=true;},clear(){this.cleared++;},render(){}});
test('late mesh load cannot replace current character and gets disposed',async()=>{
  const a=deferred(),b=deferred(),ra=renderer(),rb=renderer();
  const m=new MeshCharacter({load:p=>p.mesh_url.includes('/a.')?a.promise:b.promise});
  const first=m.select(profile('a'));assert.equal(m.pending,true);assert.equal(m.ready,false);
  const second=m.select(profile('b'));b.resolve(rb);await second;
  a.resolve(ra);await first;assert.equal(m.renderer,rb);assert.equal(ra.disposed,true);assert.equal(ra.mounted,false);assert.equal(rb.mounted,true);
  m.clear();assert.equal(rb.cleared,1);
  await m.select({});assert.equal(rb.disposed,true);assert.equal(m.active,false);
});
test('failure stays blocked but reselect retries; same ready profile reuses renderer',async()=>{
  let calls=0;const r=renderer();
  const m=new MeshCharacter({load:async()=>{if(++calls===1)throw Error('missing');return r;}});
  await m.select(profile('a'));assert.equal(m.ready,false);assert.equal(m.pending,false);assert.ok(m.error);
  await m.select(profile('a'));assert.equal(m.ready,true);assert.equal(m.error,'');
  await m.select(profile('a'));assert.equal(calls,2);
});
test('leaving a loading character ignores its later failure',async()=>{
  const d=deferred();const m=new MeshCharacter({load:()=>d.promise});const p=m.select(profile('a'));
  await m.select({});d.reject(Error('old'));await p;assert.equal(m.error,'');assert.equal(m.active,false);
});
