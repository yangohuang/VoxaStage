import test from 'node:test';
import assert from 'node:assert/strict';
import { MouthRetarget } from '../mouth-retarget.mjs';
const config = () => ({version:1,vertex_count:4,pairs:[[0,1],[2,3]],closed:.1,open:.5,
  upper:{node:'Beak Upper',axis:'x',radians:-.05236},lower:{node:'Beak Lower',axis:'x',radians:.43633}});
const frame = gap => new Float32Array([0,gap,0,0,0,0,1,gap,0,1,0,0]);
test('lip separation drives jaws with clamped rest and full-open limits',()=>{
  const m = new MouthRetarget(config());
  assert.equal(m.measure(frame(.05)),0);
  assert.ok(Math.abs(m.measure(frame(.3))-.5)<1e-6);
  assert.equal(m.measure(frame(2)),1);
  assert.equal(m.measure(frame(0)),0); // no prior clip or peak state leaks
});
test('rigid translation and rotation leave aperture unchanged',()=>{
  const m=new MouthRetarget(config()), a=frame(.3),b=Float32Array.from(a);
  for(let i=0;i<b.length;i+=3){b[i]=-a[i+1]+10;b[i+1]=a[i]+20;b[i+2]=a[i+2]-5;}
  assert.ok(Math.abs(m.measure(a)-m.measure(b))<1e-5);
});
test('rejects incompatible topology, nonfinite source and unsafe calibration',()=>{
  const m=new MouthRetarget(config());
  assert.throws(()=>m.measure(new Float32Array(6)));
  const a=frame(.3);a[0]=NaN;assert.throws(()=>m.measure(a));
  for(const changes of [{vertex_count:3},{pairs:[[0,4]]},{pairs:[]},{closed:NaN},{open:.1},
    {version:2},{lower:{node:'Beak Lower',axis:'y',radians:7}},{upper:{node:'',axis:'x',radians:0}}]){
    assert.throws(()=>new MouthRetarget({...config(),...changes}));
  }
});
