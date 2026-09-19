import { AvatarPlayer } from './audio.mjs';
import { HeadRenderer, PortraitRenderer } from './renderer.mjs';
import { Microphone } from './microphone.mjs';
import { IdlePlayer } from './idle.mjs';
import { VisualInput } from './visual.mjs';

const $ = id => document.getElementById(id);
let socket = null, ready = false, connecting = false, assistantBubble = null, sessionId = null, micPending = false;
let renderer, portraitRenderer, providerKind = '3d';
let visual = null;
const providerOptions=new Map();
const idle=new IdlePlayer($('idle-video'),visible=>{
  if(visible)$('placeholder').hidden=true;
  else if(idle.wanted)$('placeholder').hidden=false;
});
function notice(message = '') { $('notice').textContent = message; $('notice').hidden = !message; }
function activity(title, detail) { $('activity').textContent = title; $('activity-detail').textContent = detail; }
function send(data) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(data)); }
function refresh() {
  const connected = socket?.readyState === WebSocket.OPEN;
  $('connect').disabled = connecting || !!socket;
  $('disconnect').disabled = !socket;
  $('interrupt').disabled = !ready;
  $('microphone').disabled = !ready || micPending || (!!visual?.busy && !mic.active);
  $('microphone').textContent = mic.active ? '关闭麦克风' : micPending ? '开启中…' : '开启麦克风';
  $('microphone').setAttribute('aria-pressed', String(mic.active));
  $('text-input').disabled = !ready; $('send').disabled = !ready || !!visual?.busy;
  $('provider').disabled = connecting || !!socket;
  $('dialogue-backend').disabled = connecting || !!socket;
  $('connection-label').textContent = ready ? '已连接' : connected || connecting ? '连接中…' : '未连接';
  $('connection-dot').classList.toggle('live', ready);
}
function append(role, text) {
  $('welcome')?.remove();
  const article = document.createElement('article'); article.className = `message ${role}`;
  const label = document.createElement('span'); label.className = 'speaker'; label.textContent = role === 'user' ? '你' : '数字人';
  const paragraph = document.createElement('p'); paragraph.textContent = text;
  article.append(label, paragraph); $('messages').append(article);
  while ($('messages').children.length > 100) $('messages').firstElementChild.remove();
  $('messages').scrollTop = $('messages').scrollHeight;
  return paragraph;
}
function clearStage(text = '等待下一句话') {
  renderer?.clear(); portraitRenderer?.clear(); $('placeholder').hidden = false; $('placeholder-text').textContent = text;
  document.querySelector('.audio-symbol').classList.remove('speaking');
  void idle.show();
}
const player = new AvatarPlayer({
  onFrame(vertices, meta) {
    if (!meta) return;
    idle.hide();
    if (meta.kind === '2d') { portraitRenderer?.render(vertices); }
    else { renderer?.setMeta(meta); renderer?.render(vertices); }
    $('placeholder').hidden = true;
  },
  onState(state) {
    send({ type: 'playback', generation: player.timeline.generation, state });
    document.querySelector('.audio-symbol').classList.toggle('speaking', state === 'started');
    if (state === 'started') { idle.hide(); activity('正在回应', '声音与表情同步播放；你可以随时打断。'); }
    else { void idle.show(); activity(mic.active ? '正在聆听' : '等待你的下一句话', mic.active ? '麦克风已开启，直接说话即可。' : '输入文字，或开启麦克风继续。'); }
  },
  onError: error => fail(error.message),
});
const mic = new Microphone(packet => {
  if (!ready || socket?.readyState !== WebSocket.OPEN) return;
  if (socket.bufferedAmount > 128000) { fail('连接发送缓冲已满，麦克风已停止。请重新连接。'); return; }
  socket.send(packet);
});
try { renderer = new HeadRenderer($('head')); }
catch (error) { notice(error.message); }
try { portraitRenderer = new PortraitRenderer($('portrait')); }
catch (error) { notice(error.message); }
visual = new VisualInput({get:$,send,notice,changed:refresh,fatal:fail});

function fail(message) {
  player.stop(); clearStage('会话暂停，请重新连接');
  void mic.stop(); micPending = false; ready = false; connecting = false;
  visual.reset(); sessionId = null;
  socket?.close(); socket = null; refresh();
  activity('会话已停止', '重新连接后可以继续。'); notice(message);
}

async function connect() {
  if (socket || connecting) return;
  connecting = true; notice(); refresh();
  try {
    await player.unlock();
    if (!connecting) return;
    // A new socket is a new server session; generation numbering starts again.
    player.stop(); player.timeline.generation = -1;
    const provider = $('provider').value;
    const backend = $('dialogue-backend').value;
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/avatar/ws?provider=${encodeURIComponent(provider)}&backend=${encodeURIComponent(backend)}`);
    socket = ws; refresh();
    ws.onmessage = event => {
      if (socket !== ws) return;
      try {
        const message = JSON.parse(event.data);
        switch (message.type) {
          case 'ready':
            if (ready || message.input_sample_rate !== 16000 || !Number.isSafeInteger(message.generation) || !['2d', '3d'].includes(message.kind) || typeof message.provider !== 'string') throw new Error('服务握手格式不正确');
            player.reset(message.generation); sessionId = message.session_id; ready = true; connecting = false;
            visual.reset(sessionId,message.image_input === true);
            providerKind = message.kind; setStageKind(providerKind);
            clearStage(); refresh(); activity('已连接，开始对话吧', '输入文字，或点击开启麦克风。'); break;
          case 'reset':
            if (player.reset(message.generation)) { void idle.show(); assistantBubble = null; document.querySelector('.audio-symbol').classList.remove('speaking'); activity('正在准备回应', '正在组织回复…'); }
            break;
          case 'avatar_meta': case 'media': case 'clip_end': player.handle(message); break;
          case 'transcript':
            if (typeof message.text === 'string' && message.text) { append('user', message.text); assistantBubble = null; }
            break;
          case 'assistant_text':
            if (typeof message.text === 'string') {
              if (!assistantBubble) assistantBubble = append('assistant', '');
              assistantBubble.textContent = (assistantBubble.textContent + message.text).slice(-10000);
              $('messages').scrollTop = $('messages').scrollHeight;
            }
            break;
          case 'error': fail(message.message || '服务出现错误'); break;
          case 'visual_ack': case 'visual_error': case 'visual_bound':
            visual.handle(message);
            if(message.type === 'visual_bound' && message.images?.length) append('user',`[第 ${message.turn_id} 轮已附画面]`);
            break;
          case 'context_trimmed': notice(message.message); break;
          case 'status':
            if (message.state === 'thinking') activity('正在思考', '正在组织回复…');
            else if (message.state === 'listening') activity('正在聆听', '直接说话，或输入文字。');
            break;
        }
      } catch (error) { fail(error.message); }
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      socket = null; ready = false; connecting = false; player.stop(); void mic.stop(); micPending = false;
      visual.reset(); sessionId = null;
      clearStage('连接已断开'); refresh(); activity('已断开', '声音和麦克风均已停止。');
    };
    ws.onerror = () => { if (socket === ws) fail('无法连接本地数字人服务，请确认服务已启动。'); };
  } catch (error) { fail(error.message); }
}

function disconnect() {
  connecting = false; ready = false; socket?.close(); socket = null;
  player.stop(); void mic.stop(); micPending = false; sessionId = null;
  visual.reset();
  clearStage('连接后，第一句话会点亮这里'); refresh(); activity('已断开', '声音和麦克风均已停止。');
}
function interrupt() {
  if (!ready) return false;
  player.stop(); clearStage(); assistantBubble = null;
  send({ type: 'interrupt' }); activity('已打断', '等待服务确认，可继续说话或发送文字。');
  return true;
}
async function sendText(text) {
  text = String(text).trim();
  if (!text || !ready) return false;
  if (visual.busy) { notice('请等待画面确认后再发送问题。'); return false; }
  if (text.length > 500) { notice('每条消息最多 500 字。'); return false; }
  const activeSocket = socket;
  try { await player.unlock(); } catch (error) { fail(error.message); return false; }
  if (!ready || socket !== activeSocket) return false;
  if (visual.busy) { notice('请等待画面确认后再发送问题。'); return false; }
  player.stop(); clearStage(); assistantBubble = null; notice();
  send({ type: 'text', text });
  // Server transcript is authoritative for both typed and spoken messages.
  activity('正在准备回应', '消息已发送。'); $('text-input').value = '';
  return true;
}
$('connect').addEventListener('click', () => void connect());
$('disconnect').addEventListener('click', disconnect);
$('interrupt').addEventListener('click', interrupt);
$('microphone').addEventListener('click', async () => {
  if (mic.active) { await mic.stop(); refresh(); activity('麦克风已关闭', '仍可通过文字继续对话。'); return; }
  micPending = true; refresh(); notice();
  try { await mic.start(); if (ready && mic.active) activity('正在聆听', '麦克风已开启，直接说话即可。'); }
  catch (error) { notice(`麦克风未开启：${error.message}。你仍可使用文字。`); }
  finally { micPending = false; refresh(); }
});
$('text-form').addEventListener('submit', event => { event.preventDefault(); void sendText($('text-input').value); });
$('text-input').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); void sendText($('text-input').value); }
});
window.addEventListener('pagehide', disconnect);
function setStageKind(kind) {
  const portrait = kind === '2d';
  $('head').hidden = portrait; $('portrait').hidden = !portrait;
  $('stage-label').textContent = portrait ? '2D 肖像 · 实时语音驱动' : '3D 头部示例 · 非真人形象';
}
$('provider').addEventListener('change',()=>{
  const provider=providerOptions.get($('provider').value);
  if(provider){setStageKind(provider.kind);idle.select(provider.idle_url);clearStage();}
});
async function loadProviders() {
  try {
    const response = await fetch('/avatar/providers', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('无法加载驱动列表');
    const payload = await response.json();
    if (!payload || typeof payload.default_provider !== 'string' || !Array.isArray(payload.providers)) throw new Error('驱动列表格式不正确');
    const select = $('provider'); select.replaceChildren();
    for (const item of payload.providers) {
      if (!item || typeof item.id !== 'string' || typeof item.label !== 'string' || !['2d', '3d'].includes(item.kind) || typeof item.configured !== 'boolean') continue;
      const option = new Option(`${item.label}${item.voice ? (item.voice === 'male' ? ' · 男声' : ' · 女声') : ''}${item.configured ? '' : '（未配置）'}`, item.id);
      option.disabled = !item.configured; option.title = item.description || '';
      select.add(option);
      providerOptions.set(item.id,item);
    }
    select.value = payload.default_provider;
    if (!select.value) throw new Error('默认驱动不可用');
    const provider=providerOptions.get(select.value);setStageKind(provider.kind);idle.select(provider.idle_url);void idle.show();
    const backends = $('dialogue-backend'); backends.replaceChildren();
    for (const item of payload.dialogue_backends || []) {
      const option = new Option(`${item.label}${!item.configured ? '（未配置）' : item.available === false ? '（服务离线）' : ''}`, item.id);
      option.disabled = !item.configured || item.available === false; backends.add(option);
    }
    backends.value = payload.default_backend;
    if (!backends.value) throw new Error('默认对话后端不可用');
  } catch (error) { notice(`驱动列表不可用：${error.message}`); }
}
void loadProviders();
window.avatarDemo = Object.freeze({ connect, disconnect, sendText, interrupt,
  get metrics() { return { ...player.metrics, connected: ready, microphone: mic.active, sessionId, idle:!$('idle-video').hidden&&!$('idle-video').paused }; },
  get generation() { return player.timeline.generation; },
  get renderedFrames() { return player.renderedFrames; },
  get droppedStale() { return player.timeline.droppedStale; },
  get playing() { return player.timeline.playing; },
});
refresh();
