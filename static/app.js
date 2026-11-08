// InterviewCoach browser client — voice session + talking avatar + scoring UI.
const $ = (id) => document.getElementById(id);
const WIRE_RATE = 24000;
const AGENT = window.AGENT || { id: '', name: 'InterviewCoach' };
const ROLE_SLUG = window.ROLE_SLUG || '';
const ROLE_NAME = window.ROLE_NAME || '';
const TOTAL_QUESTIONS = window.TOTAL_QUESTIONS || 5;
const SESSION_ID = 's_' + Math.random().toString(36).slice(2) + Date.now().toString(36);

const CAPTURE_WORKLET = `
  class CaptureProcessor extends AudioWorkletProcessor {
    constructor() {
      super();
      this._ratio = sampleRate / ${WIRE_RATE};
      this._pos = 0;
      this._prev = 0;
      this._src = null;
      this._out = null;
    }
    _toPcm(samples, len) {
      const pcm = new Int16Array(len);
      for (let i = 0; i < len; i++) {
        const s = Math.max(-1, Math.min(1, samples[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      return pcm;
    }
    process(inputs) {
      const ch = inputs[0]?.[0];
      if (!ch) return true;
      if (this._ratio === 1) {
        const pcm = this._toPcm(ch, ch.length);
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
        return true;
      }
      const n = ch.length;
      if (!this._src || this._src.length < n + 1) {
        this._src = new Float32Array(n + 1);
        this._out = new Float32Array(Math.ceil((n + 1) / this._ratio) + 2);
      }
      const src = this._src;
      const out = this._out;
      src[0] = this._prev;
      src.set(ch, 1);
      let outLen = 0;
      let pos = this._pos;
      while (pos < n) {
        const i = Math.floor(pos);
        const frac = pos - i;
        out[outLen++] = src[i] + (src[i + 1] - src[i]) * frac;
        pos += this._ratio;
      }
      this._pos = pos - n;
      this._prev = ch[n - 1];
      if (outLen) {
        const pcm = this._toPcm(out, outLen);
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
      }
      return true;
    }
  }
  registerProcessor('capture', CaptureProcessor);
`;

const PLAYBACK_WORKLET = `
  class PlaybackProcessor extends AudioWorkletProcessor {
    constructor() {
      super();
      this._ring = new Float32Array(sampleRate * 30);
      this._writePos = 0;
      this._readPos = 0;
      this._available = 0;
      this._step = ${WIRE_RATE} / sampleRate;
      this._rsPos = 0;
      this._rsPrev = 0;
      this._drained = false;
      this.port.onmessage = (e) => {
        if (e.data === 'stop') {
          this._writePos = this._readPos = this._available = 0;
          this._rsPos = this._rsPrev = 0;
          return;
        }
        const int16 = new Int16Array(e.data);
        if (!int16.length) return;
        if (this._drained) {
          this._rsPrev = 0;
          this._rsPos = 0;
          this._drained = false;
        }
        if (this._step === 1) {
          for (let i = 0; i < int16.length; i++) this._push(int16[i] / 32768);
          return;
        }
        const n = int16.length;
        let pos = this._rsPos;
        while (pos < n) {
          const i = Math.floor(pos);
          const frac = pos - i;
          const a = i === 0 ? this._rsPrev : int16[i - 1] / 32768;
          const b = int16[i] / 32768;
          this._push(a + (b - a) * frac);
          pos += this._step;
        }
        this._rsPos = pos - n;
        this._rsPrev = int16[n - 1] / 32768;
      };
    }
    _push(v) {
      if (this._available < this._ring.length) {
        this._ring[this._writePos] = v;
        this._writePos = (this._writePos + 1) % this._ring.length;
        this._available++;
      }
    }
    process(inputs, outputs) {
      const output = outputs[0];
      const out = output[0];
      const cap = this._ring.length;
      for (let i = 0; i < out.length; i++) {
        if (this._available > 0) {
          out[i] = this._ring[this._readPos];
          this._readPos = (this._readPos + 1) % cap;
          this._available--;
        } else {
          out[i] = 0;
          this._drained = true;
        }
      }
      for (let ch = 1; ch < output.length; ch++) output[ch].set(out);
      return true;
    }
  }
  registerProcessor('playback', PlaybackProcessor);
`;

const blobUrl = (code) => URL.createObjectURL(new Blob([code], { type: 'application/javascript' }));

let ws, captureCtx, playbackCtx, playback, mic, callStart, timer;
let lastEvent = null;
let pendingTools = [];
// Interview progress state
let currentQ = 0;
let scores = [];

function getCandidate() {
  const nameEl = $('candidate-name');
  const emailEl = $('candidate-email');
  let name = (nameEl && nameEl.value.trim()) || localStorage.getItem('ic_name') || '';
  let email = (emailEl && emailEl.value.trim()) || localStorage.getItem('ic_email') || '';
  if (nameEl && nameEl.value.trim()) localStorage.setItem('ic_name', name);
  if (emailEl && emailEl.value.trim()) localStorage.setItem('ic_email', email);
  return { candidate_name: name, candidate_email: email };
}
// Restore saved candidate
try {
  const n = localStorage.getItem('ic_name'); const e = localStorage.getItem('ic_email');
  if (n && $('candidate-name')) $('candidate-name').value = n;
  if (e && $('candidate-email')) $('candidate-email').value = e;
} catch (_) {}

// Client-side tools follow the documented flow: accumulate the result, then
// send tool.result once reply.done is the latest event. A short fallback timer
// guarantees a result is never stranded if reply.done is missed.
let toolFlushTimer = null;

function flushTools() {
  if (toolFlushTimer) { clearTimeout(toolFlushTimer); toolFlushTimer = null; }
  if (!pendingTools.length) return;
  if (!ws || ws.readyState !== 1) return; // keep queued until the socket is up
  const queued = pendingTools;
  pendingTools = [];
  for (const tool of queued) {
    try {
      ws.send(JSON.stringify({ type: 'tool.result', call_id: tool.call_id, result: JSON.stringify(tool.result) }));
      logEvent('up', 'tool.result', `${tool.name} → ${JSON.stringify(tool.result).slice(0, 90)}`);
    } catch (_) {}
  }
}

function queueToolResult(callId, name, result) {
  pendingTools.push({ call_id: callId, name, result });
  if (lastEvent === 'reply.done') {
    flushTools();
    return;
  }
  // Safety net: if reply.done never arrives the agent would sit silent until
  // the tool timeout fires, so release the result after a short grace period.
  if (toolFlushTimer) clearTimeout(toolFlushTimer);
  toolFlushTimer = setTimeout(() => { toolFlushTimer = null; flushTools(); }, 3000);
}

async function runClientTool(name, args) {
  const cand = getCandidate();
  const body = { tool: name, arguments: { ...args, session_id: SESSION_ID, ...cand } };
  const res = await fetch('/tool', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `tools server returned ${res.status}`);
  }
  const result = await res.json();
  onToolResult(name, args, result);
  return result;
}

function onToolResult(name, args, result) {
  if (name === 'get_question' && result.question) {
    currentQ = result.question_number || currentQ + 1;
    setQuestion(result.question_number || currentQ, result.question, result.total_questions || TOTAL_QUESTIONS);
  }
  if (name === 'evaluate_answer' && typeof result.score === 'number') {
    scores.push(result.score);
    addScoreRow(scores.length, result.score, result.feedback || '');
  }
  if (name === 'get_session_summary') {
    showResult(result);
  }
}

function setQuestion(num, text, total) {
  currentQ = num;
  const qEl = $('question-current');
  if (qEl) qEl.textContent = `Q${num}: ${text}`;
  const cEl = $('question-count');
  if (cEl) cEl.textContent = `${Math.min(num, total)} / ${total}`;
  const fill = $('progress-fill');
  if (fill) fill.style.width = Math.min(100, (num / total) * 100) + '%';
  const avg = $('avg-score');
  if (avg) avg.textContent = scores.length ? `avg ${avgScore()} / 10` : `avg —`;
}

function avgScore() {
  if (!scores.length) return '—';
  return (scores.reduce((a, b) => a + b, 0) / scores.length).toFixed(1);
}

function addScoreRow(num, score, feedback) {
  const list = $('scores-list');
  if (!list) return;
  const cls = score >= 7 ? 'good' : score >= 5 ? 'mid' : 'low';
  const row = document.createElement('div');
  row.className = 'score-row ' + cls;
  const n = document.createElement('div');
  n.className = 'score-num';
  n.textContent = score;
  const t = document.createElement('div');
  const b = document.createElement('b');
  b.textContent = `Answer ${num} — ${score}/10`;
  const s = document.createElement('div');
  s.style.cssText = 'color:var(--muted);font-size:12.5px;margin-top:2px';
  s.textContent = feedback;
  t.append(b, s);
  row.append(n, t);
  list.append(row);
  const avg = $('avg-score');
  if (avg) avg.textContent = `avg ${avgScore()} / 10`;
  const fill = $('progress-fill');
  if (fill) fill.style.width = Math.min(100, (num / TOTAL_QUESTIONS) * 100) + '%';
  const cEl = $('question-count');
  if (cEl) cEl.textContent = `${Math.min(num, TOTAL_QUESTIONS)} / ${TOTAL_QUESTIONS}`;
}

function showResult(result) {
  const modal = $('result-modal');
  if (!modal) return;
  const scoreEl = $('result-score');
  const verdictEl = $('result-verdict');
  const summaryEl = $('result-summary');
  const tipsEl = $('result-tips');
  const nextBtn = $('result-next');
  const titleEl = $('result-title');
  const avg = result.average_score ?? avgScore();
  if (scoreEl) scoreEl.textContent = avg;
  const passed = !!result.passed;
  if (verdictEl) {
    verdictEl.textContent = passed ? '✓ PASSED — next round unlocked' : 'Not passed this time';
    verdictEl.className = 'verdict ' + (passed ? 'pass' : 'fail');
  }
  if (titleEl) titleEl.textContent = passed ? 'You passed! 🎉' : 'Interview complete';
  if (summaryEl) summaryEl.textContent = result.summary || `You answered ${result.questions_answered || scores.length} questions for ${ROLE_NAME}. Average ${avg}/10.`;
  if (tipsEl) {
    tipsEl.innerHTML = '';
    (result.top_tips || []).forEach((t) => {
      const li = document.createElement('li');
      li.textContent = t;
      tipsEl.append(li);
    });
    if (!result.top_tips || !result.top_tips.length) {
      const li = document.createElement('li');
      li.textContent = 'Keep answers structured with STAR: Situation, Task, Action, Result.';
      tipsEl.append(li);
    }
  }
  // Auto-generated pass link takes priority; fall back to a custom admin
  // URL or the roles page. Relative paths are resolved against this host so
  // the link works on localhost and any deployment without configuration.
  function passUrl() {
    const path = result.onboarding_path || '';
    if (path) return path.startsWith('http') ? path : location.origin + path;
    const url = result.onboarding_url || '';
    if (!url) return '';
    return url.startsWith('http') || url.startsWith('/') ? (url.startsWith('/') ? location.origin + url : url) : url;
  }
  if (nextBtn) {
    const url = passed ? passUrl() : '';
    if (passed && url) {
      nextBtn.href = url;
      nextBtn.textContent = result.onboarding_path ? 'Open my next-round pass →' : 'Continue to next round →';
      nextBtn.style.display = 'flex';
    } else if (passed) {
      nextBtn.href = '/';
      nextBtn.textContent = 'Back to roles →';
      nextBtn.style.display = 'flex';
    } else {
      nextBtn.style.display = 'none';
    }
  }
  if (summaryEl && passed && result.onboarding_path) {
    summaryEl.textContent += ' Your personal next-round pass link was auto-generated — tap the button below to open it.';
  }
  modal.hidden = false;
}

async function listMics() {
  try {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    const devices = await navigator.mediaDevices.enumerateDevices();
    const inputs = devices.filter((d) => d.kind === 'audioinput' && d.deviceId !== 'default' && d.deviceId !== 'communications');
    const select = $('mic');
    if (!select) return;
    const chosen = select.value;
    select.replaceChildren();
    const auto = document.createElement('option');
    auto.value = '';
    auto.textContent = 'Default microphone';
    select.append(auto);
    inputs.forEach((device, i) => {
      const option = document.createElement('option');
      option.value = device.deviceId;
      option.textContent = device.label || `Microphone ${i + 1}`;
      select.append(option);
    });
    if (chosen && inputs.some((d) => d.deviceId === chosen)) select.value = chosen;
  } catch (_) {}
}
listMics();
try { navigator.mediaDevices?.addEventListener?.('devicechange', listMics); } catch (_) {}

const btnEl = $('btn');
if (btnEl) btnEl.onclick = () => (ws?.readyState <= 1 ? stop() : start());

// Live mic level meter: proves the browser is actually capturing audio.
// If the bar never moves while you talk, the problem is mic/device/permission
// (not the AI). Warns once if the mic stays dead silent after the call starts.
let micRaf = null;
let micPeak = 0;
let micWarned = false;

function startMicMeter(source) {
  stopMicMeter();
  micWarned = false;
  try {
    const analyser = captureCtx.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    const t0 = Date.now();
    const loop = () => {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      if (rms > micPeak) micPeak = rms;
      const bar = $('mic-level');
      if (bar) bar.style.width = Math.min(100, rms * 400) + '%';
      const warn = $('mic-warn');
      if (micPeak >= 0.02) {
        if (warn) warn.hidden = true;
      } else if (!micWarned && Date.now() - t0 > 6000) {
        micWarned = true;
        if (warn) warn.hidden = false;
        logEvent('sys', 'mic.silent', 'no mic signal 6s after start — check device/permission');
      }
      micRaf = requestAnimationFrame(loop);
    };
    loop();
  } catch (_) {}
}

function stopMicMeter() {
  if (micRaf) cancelAnimationFrame(micRaf);
  micRaf = null;
  micPeak = 0;
  const bar = $('mic-level');
  if (bar) bar.style.width = '0%';
  const warn = $('mic-warn');
  if (warn) warn.hidden = true;
}

async function addWorklet(ctx, code, name) {
  const url = blobUrl(code);
  try {
    await ctx.audioWorklet.addModule(url);
  } finally {
    URL.revokeObjectURL(url);
  }
  return new AudioWorkletNode(ctx, name);
}

async function start() {
  const btn = $('btn');
  const micSel = $('mic');
  if (btn) { btn.disabled = true; }
  if (micSel) micSel.disabled = true;
  setStatus('connecting');
  getCandidate();

  try {
    const res = await fetch('/token');
    if (!res.ok) {
      setStatus('error', 'could not mint a token, check the API key');
      reset();
      return;
    }
    const { token } = await res.json();

    captureCtx = new AudioContext({ sampleRate: WIRE_RATE });
    playbackCtx = new AudioContext({ sampleRate: WIRE_RATE });
    await Promise.all([captureCtx.resume(), playbackCtx.resume()]);

    playback = await addWorklet(playbackCtx, PLAYBACK_WORKLET, 'playback');
    playback.connect(playbackCtx.destination);

    const deviceId = micSel ? micSel.value : '';
    mic = await navigator.mediaDevices.getUserMedia({
      audio: {
        ...(deviceId ? { deviceId } : {}),
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: false,
        autoGainControl: false,
      },
    });
    listMics();
    warmVideos(); // user gesture here unlocks muted playback; warms decoders early
    const capture = await addWorklet(captureCtx, CAPTURE_WORKLET, 'capture');
    const micSource = captureCtx.createMediaStreamSource(mic);
    micSource.connect(capture);
    startMicMeter(micSource);

    const url = new URL('wss://agents.assemblyai.com/v1/ws');
    url.searchParams.set('token', token);
    ws = new WebSocket(url);
    let ready = false;

    capture.port.onmessage = ({ data }) => {
      if (!ready || ws.readyState !== 1) return;
      const bytes = new Uint8Array(data);
      let binary = '';
      for (let i = 0; i < bytes.length; i += 0x8000) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      }
      ws.send(JSON.stringify({ type: 'input.audio', audio: btoa(binary) }));
      logEvent('up', 'input.audio');
    };

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'session.update', session: { agent_id: AGENT.id } }));
      logEvent('up', 'session.update', AGENT.id);
    };

    ws.onmessage = ({ data }) => {
      const msg = JSON.parse(data);
      switch (msg.type) {
        case 'session.ready':
          ready = true;
          try { sessionStorage.removeItem('ic_cfg_reload'); } catch (_) {}
          // Seed the role as conversation context. input.text is not a valid
          // message type; conversation.message is the documented way to inject
          // context without the candidate having to say it out loud.
          if (ROLE_SLUG) {
            const intro = `I am interviewing for the ${ROLE_NAME || ROLE_SLUG} role. The role slug is ${ROLE_SLUG}.`;
            try {
              ws.send(JSON.stringify({ type: 'conversation.message', role: 'user', content: intro }));
              logEvent('up', 'conversation.message', intro);
            } catch (_) {}
          }
          callStart = Date.now();
          timer = setInterval(tick, 1000);
          tick();
          setStatus('listening');
          setAvatar('listening');
          if (btn) { btn.disabled = false; btn.textContent = 'End call'; btn.classList.add('live'); }
          logEvent('down', msg.type, msg.session_id);
          break;

        case 'input.speech.started':
          playback?.port.postMessage('stop');
          setStatus('listening');
          setAvatar('listening');
          stopAgentMeter();
          lastEvent = msg.type;
          logEvent('down', msg.type);
          break;

        case 'reply.started':
          setStatus('speaking');
          setAvatar('speaking');
          startAgentMeter();
          lastEvent = msg.type;
          logEvent('down', msg.type);
          break;

        case 'reply.audio': {
          const raw = atob(msg.data);
          const bytes = new Uint8Array(raw.length);
          for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
          playback?.port.postMessage(bytes.buffer, [bytes.buffer]);
          logEvent('down', msg.type);
          break;
        }

        case 'reply.done':
          setStatus('listening');
          setAvatar('listening');
          stopAgentMeter();
          lastEvent = msg.type;
          if (msg.status === 'interrupted') {
            playback?.port.postMessage('stop');
            stopAgentMeter();
            pendingTools = [];
          } else {
            flushTools();
          }
          logEvent('down', msg.type, msg.status);
          break;

        case 'transcript.user.delta':
          partial('you', msg.text);
          logEvent('down', msg.type, msg.text);
          break;

        case 'transcript.agent.delta':
          logEvent('down', msg.type, msg.delta);
          if (msg.reply_id && msg.reply_id === printedReply) break;
          if (msg.reply_id !== liveReply) {
            liveReply = msg.reply_id;
            dropPartial('agent');
          }
          partial('agent', appendDelta(partialText.agent || '', msg.delta));
          break;

        case 'transcript.user':
          addLine('you', msg.text);
          logEvent('down', msg.type, msg.text);
          break;

        case 'transcript.agent':
          printedReply = msg.reply_id ?? printedReply;
          addLine('agent', msg.text);
          logEvent('down', msg.type, msg.text);
          break;

        case 'tool.call': {
          // Tool traffic stays in the debug log — candidates should only ever
          // see the conversation, never raw function calls.
          const args = JSON.stringify(msg.arguments ?? {});
          logEvent('down', msg.type, `${msg.name} ${args}`);
          runClientTool(msg.name, msg.arguments ?? {})
            .then((result) => queueToolResult(msg.call_id, msg.name, result))
            .catch((err) => queueToolResult(msg.call_id, msg.name, { error: err.message }));
          break;
        }

        case 'session.ended':
          logEvent('down', msg.type);
          ws.close();
          break;

        case 'session.error':
          // Stale tab holding an agent ID from before a server restart/key
          // change: fetch a fresh page (with the live agent ID) once, then
          // show the error if it still fails.
          if ((msg.code === 'agent_not_found' || /not found/i.test(msg.message || '')) &&
              !sessionStorage.getItem('ic_cfg_reload')) {
            sessionStorage.setItem('ic_cfg_reload', '1');
            setStatus('connecting', 'refreshing interview setup…');
            logEvent('down', msg.type, 'stale agent id — reloading page for fresh config');
            try { ws.close(); } catch (_) {}
            setTimeout(() => location.reload(), 1200);
          } else {
            setStatus('error', msg.message);
            logEvent('down', msg.type, `${msg.code}: ${msg.message}`);
          }
          break;

        default:
          logEvent('down', msg.type);
      }
    };

    ws.onclose = () => { setStatus('idle'); reset(); };
    ws.onerror = () => { setStatus('error', 'connection failed'); reset(); };
  } catch (error) {
    setStatus('error', error.message);
    reset();
  }
}

function stop() {
  if (ws?.readyState === 1) {
    try { ws.send(JSON.stringify({ type: 'session.end' })); } catch (_) {}
    logEvent('up', 'session.end');
    const socket = ws;
    setTimeout(() => { if (socket.readyState === 1) socket.close(); }, 3000);
  } else {
    ws?.close();
  }
  playback?.port.postMessage('stop');
  mic?.getTracks().forEach((track) => track.stop());
  try { captureCtx?.close(); } catch (_) {}
  try { playbackCtx?.close(); } catch (_) {}
  captureCtx = playbackCtx = playback = mic = null;
  restVideos();
  stopMicMeter();
  reset();
  setStatus('idle');
  setAvatar('idle');
}

function reset() {
  clearInterval(timer);
  clearPartials();
  open.forEach((run) => paint(run, true));
  open.clear();
  const btn = $('btn');
  const micSel = $('mic');
  if (btn) { btn.disabled = false; btn.textContent = currentQ > 0 ? 'Restart interview' : 'Start interview'; btn.classList.remove('live'); }
  if (micSel) micSel.disabled = false;
}

function setStatus(state, detail) {
  const el = $('status');
  const txt = $('status-text');
  if (el) el.className = 'status ' + state;
  if (txt) txt.textContent = detail || state;
}

// Both interview videos are preloaded once the call starts so switching states
// is an instant opacity crossfade with zero play() latency.
function ensurePlaying(v) {
  if (!v) return;
  try { if (v.paused) { const p = v.play(); if (p) p.catch(() => {}); } } catch (_) {}
}

function pauseVideo(v) {
  try { if (v && !v.paused) v.pause(); } catch (_) {}
}

function restartVideo(v) {
  if (!v) return;
  try { v.currentTime = 0; } catch (_) {}
  ensurePlaying(v);
}

function warmVideos() {
  // Load both decoders up front, but only the active one plays.
  for (const id of ['vid-listening', 'vid-speaking']) {
    const v = $(id);
    if (!v) continue;
    try { v.load(); } catch (_) {}
  }
}

function restVideos() {
  for (const id of ['vid-listening', 'vid-speaking']) pauseVideo($(id));
  stopAgentMeter();
}

function setAvatar(state) {
  const avatar = $('avatar');
  const label = $('avatar-label');
  const wave = $('wave');
  const speaking = $('vid-speaking');
  const listening = $('vid-listening');
  // Logo at rest; listening video loops while the candidate talks;
  // speaking video restarts from frame 0 so the mouth moves with the audio.
  if (avatar) {
    avatar.classList.remove('show-logo', 'show-listening', 'show-speaking');
    if (state === 'speaking' && speaking) {
      avatar.classList.add('show-speaking');
      pauseVideo(listening);
      restartVideo(speaking);
    } else if (state === 'listening' && listening) {
      avatar.classList.add('show-listening');
      pauseVideo(speaking);
      restartVideo(listening);
    } else {
      avatar.classList.add('show-logo');
      pauseVideo(speaking);
      pauseVideo(listening);
    }
  }
  if (wave) wave.classList.toggle('live', state === 'speaking');
  if (!label) return;
  if (state === 'speaking') label.textContent = 'Talha is speaking…';
  else if (state === 'listening') label.textContent = 'Listening to you — speak now';
  else if (state === 'connecting') label.textContent = 'Connecting…';
  else if (state === 'idle') label.textContent = currentQ > 0 ? 'Call ended — review your transcript' : 'Ready when you are';
}

// Drive the waveform from Talha's real playback level so the avatar visibly
// reacts to his voice instead of running a fixed CSS animation.
let agentRaf = null;
let agentAnalyser = null;
let agentSink = null;

function startAgentMeter() {
  stopAgentMeter();
  if (!playbackCtx || !playback) return;
  try {
    agentAnalyser = playbackCtx.createAnalyser();
    agentAnalyser.fftSize = 256;
    // A muted sink keeps the analyser pulled by the graph without doubling
    // the audible output (playback already connects to the destination).
    agentSink = playbackCtx.createGain();
    agentSink.gain.value = 0;
    playback.connect(agentAnalyser);
    agentAnalyser.connect(agentSink);
    agentSink.connect(playbackCtx.destination);
    const buf = new Float32Array(agentAnalyser.fftSize);
    const bars = Array.from(document.querySelectorAll('#wave i'));
    if (!bars.length) return;
    const loop = () => {
      agentAnalyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      const level = Math.min(1, rms * 7);
      const t = performance.now() / 110;
      bars.forEach((bar, i) => {
        const wobble = 0.5 + 0.5 * Math.sin(t + i * 0.85);
        bar.style.height = (7 + level * 25 * wobble).toFixed(1) + 'px';
      });
      agentRaf = requestAnimationFrame(loop);
    };
    loop();
  } catch (_) {}
}

function stopAgentMeter() {
  if (agentRaf) cancelAnimationFrame(agentRaf);
  agentRaf = null;
  try { if (agentAnalyser && playback) playback.disconnect(agentAnalyser); } catch (_) {}
  try { agentAnalyser?.disconnect(); } catch (_) {}
  try { agentSink?.disconnect(); } catch (_) {}
  agentAnalyser = null;
  agentSink = null;
  document.querySelectorAll('#wave i').forEach((bar) => { bar.style.height = ''; });
}

const COST_PER_SECOND = 4.5 / 3600;

function tick() {
  const el = $('elapsed');
  const cost = $('cost');
  if (!callStart) return;
  const seconds = Math.floor((Date.now() - callStart) / 1000);
  if (el) el.textContent = Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
  if (cost) cost.textContent = '$' + (seconds * COST_PER_SECOND).toFixed(3);
}

// --- transcript ---
const partialText = {};
const partialEl = {};
let liveReply = null;
let printedReply = null;

const ATTACHES_LEFT = /^[.,!?;:%°)\]}…'"’”]/;
const NO_SPACE_AFTER = /[([{$\-\/'"‘“]$/;

function appendDelta(text, delta) {
  if (!delta) return text;
  if (!text) return delta;
  if (/^\s/.test(delta) || /\s$/.test(text)) return text + delta;
  if (ATTACHES_LEFT.test(delta) || NO_SPACE_AFTER.test(text)) return text + delta;
  return text + ' ' + delta;
}

function dropPartial(who) {
  try { partialEl[who]?.remove(); } catch (_) {}
  delete partialEl[who];
  delete partialText[who];
}

function transcriptLine(who, text, cls) {
  const line = document.createElement('div');
  line.className = 'line ' + who + (cls ? ' ' + cls : '');
  const label = document.createElement('span');
  label.className = 'who';
  label.textContent = who === 'agent' ? 'Talha' : who === 'you' ? 'You' : '⚙';
  const body = document.createElement('span');
  body.className = 'said';
  body.textContent = text;
  line.append(label, body);
  return line;
}

function clearEmpty(el) {
  if (!el) return;
  const empty = el.querySelector('.empty');
  if (empty) empty.remove();
}

function scroll(el) {
  if (el) el.scrollTop = el.scrollHeight;
}

function partial(who, text) {
  const box = $('transcript');
  if (!box) return;
  clearEmpty(box);
  partialText[who] = text;
  if (partialEl[who]) {
    partialEl[who].querySelector('.said').textContent = text;
  } else {
    partialEl[who] = transcriptLine(who, text, 'partial');
    box.append(partialEl[who]);
  }
  scroll(box);
}

function addLine(who, text) {
  const box = $('transcript');
  if (!box) return;
  clearEmpty(box);
  dropPartial(who);
  box.append(transcriptLine(who, text));
  scroll(box);
}

function clearPartials() {
  for (const who of Object.keys(partialEl)) dropPartial(who);
  liveReply = printedReply = null;
}

// --- event log ---
const COALESCE = new Set([
  'input.audio',
  'reply.audio',
  'transcript.user.delta',
  'transcript.agent.delta',
]);
const open = new Map();

function eventRow(direction, type, detail) {
  const row = document.createElement('div');
  row.className = 'event ' + direction;
  row.textContent = `${direction === 'up' ? '↑' : '↓'} ${type}${detail ? ' — ' + String(detail).slice(0, 120) : ''}`;
  return row;
}

function paint(live, final) {
  const now = performance.now();
  if (!final && now - live.painted < 100) return;
  live.painted = now;
  if (live.count > 1) live.row.textContent += '';
}

function logEvent(direction, type, detail) {
  const log = $('events-body');
  if (!log) return;
  clearEmpty(log);
  const key = direction + ' ' + type;
  const live = open.get(key);
  if (live) {
    live.count += 1;
    if (detail) live.detail = detail;
    paint(live);
    return;
  }
  if (!COALESCE.has(type)) {
    open.forEach((run) => paint(run, true));
    open.clear();
  }
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const row = eventRow(direction, type, detail);
  log.append(row);
  while (log.children.length > 400) log.firstChild.remove();
  if (COALESCE.has(type)) open.set(key, { row, count: 1, detail, painted: 0 });
  if (atBottom) scroll(log);
}
