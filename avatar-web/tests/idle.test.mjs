import assert from 'node:assert/strict';
import { IdlePlayer } from '../idle.mjs';

const video={hidden:true,paused:true,src:'',play(){this.paused=false;return Promise.resolve();},pause(){this.paused=true;},load(){},removeAttribute(){this.src='';}};
const idle=new IdlePlayer(video);
idle.select('/avatar/idle/dinet.mp4');await idle.show();
assert.equal(video.hidden,false);assert.equal(video.muted,true);assert.equal(video.loop,true);
idle.hide();assert.equal(video.hidden,true);assert.equal(video.paused,true);
await idle.show();assert.equal(video.paused,false);
idle.select('/avatar/idle/flashhead.mp4');await idle.show();assert.match(video.src,/flashhead/);
idle.select('https://other/track.mp4');assert.equal(video.hidden,true);
idle.select('/avatar/idle/dinet.mp4');video.play=()=>Promise.reject(new Error('missing'));
await idle.show();assert.equal(video.hidden,true);
let release;video.play=()=>new Promise(r=>{release=r;});
const pending=idle.show();idle.hide();release();await pending;assert.equal(video.hidden,true);
console.log('Idle loop, talk/idle transitions, provider switch, missing asset and stale play passed');
