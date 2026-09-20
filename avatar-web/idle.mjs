// Local silent idle clips never own conversation audio or model sessions.
export class IdlePlayer {
  constructor(video,onVisibility=()=>{}) {
    this.video=video;this.onVisibility=onVisibility;this.wanted=false;this.epoch=0;this.url='';
    video.muted=true;video.loop=true;video.playsInline=true;
    video.onerror=()=>{video.hidden=true;this.onVisibility(false);};
  }
  select(url) {
    if(url===this.url)return;
    this.epoch++;this.video.pause();this.video.hidden=true;this.onVisibility(false);
    this.url=/^\/avatar\/idle\/[a-z0-9][a-z0-9_-]{0,63}\.mp4$/.test(url)?url:'';
    if(this.url)this.video.src=this.url;else this.video.removeAttribute('src');
    this.video.load();
    if(this.wanted)void this.show();
  }
  async show() {
    this.wanted=true;
    if(!this.url)return;
    const epoch=++this.epoch;
    try {
      await this.video.play();
      if(epoch!==this.epoch||!this.wanted)return;
      this.video.hidden=false;this.onVisibility(true);
    } catch {
      if(epoch===this.epoch){this.video.hidden=true;this.onVisibility(false);}
    }
  }
  hide() {this.wanted=false;this.epoch++;this.video.pause();this.video.hidden=true;this.onVisibility(false);}
}
