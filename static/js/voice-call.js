/** Full half-duplex call client: rolling MediaRecorder, local VAD/acoustics,
 * Scribe+emotion turn upload, WebAudio timed playback, private/sleep modes. */
(function voiceCallBootstrap(global) {
  'use strict';

  const ORIGIN = global.location.origin;
  const params = new URLSearchParams(global.location.search);
  const companionName = document.documentElement.dataset.companionName || '伴侣';
  const state = {
    callId: params.get('call_id') || '',
    sessionId: params.get('session_id') || '',
    embedded: params.get('embedded') !== '0' && global.parent !== global,
    settings: null,
    call: null,
    stream: null,
    mediaSource: null,
    audioContext: null,
    analyser: null,
    recorder: null,
    recorderStoppingForPlayback: false,
    preRoll: [],
    utterance: null,
    finalizing: null,
    busy: false,
    playing: false,
    ended: false,
    inputMode: 'auto',
    route: 'speaker',
    privateMode: false,
    sleepMode: false,
    sampleTimer: 0,
    heartbeatTimer: 0,
    sampleClockMs: 0,
    audioClockMs: 0,
    nextSleepSnapshotMs: 30 * 60 * 1000,
    ambientFrames: [],
    vadStartFrames: 0,
    activeSource: null,
    pendingTurns: new Map(),
    turnSequence: 0,
    pageEnding: false,
    captureSuspended: false,
    captureEnded: false,
    wakeLock: null,
  };

  const ui = {
    stage: document.querySelector('.call-stage'),
    status: document.getElementById('call-status'),
    subtitles: document.getElementById('call-subtitles'),
    meter: document.getElementById('call-meter-fill'),
    acoustic: document.getElementById('call-acoustic'),
    inputMode: document.getElementById('call-input-mode'),
    privateMode: document.getElementById('call-private'),
    sleepMode: document.getElementById('call-sleep'),
    route: document.getElementById('call-route'),
    talk: document.getElementById('call-talk'),
    minimize: document.getElementById('call-minimize'),
    browse: document.getElementById('call-browse'),
    end: document.getElementById('call-end'),
    resume: document.getElementById('call-resume'),
    error: document.getElementById('call-error'),
  };

  function post(type, payload = {}) {
    if (!state.embedded) return;
    global.parent.postMessage({ type, ...payload }, ORIGIN);
  }

  function setStatus(text, mode = '') {
    ui.status.textContent = String(text || '');
    ui.stage.dataset.state = mode || 'idle';
  }

  function showError(text = '') {
    ui.error.textContent = String(text || '');
    ui.error.classList.toggle('hidden', !text);
  }

  async function fetchRaw(url, options = {}, timeoutMs = 100000) {
    const controller = new AbortController();
    const timer = global.setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(url, { cache: 'no-store', ...options, signal: controller.signal });
    } catch (error) {
      if (error?.name === 'AbortError') throw new Error('语音网络请求超时');
      throw error;
    } finally {
      global.clearTimeout(timer);
    }
  }

  async function fetchJSON(url, options = {}, timeoutMs = 100000) {
    const response = await fetchRaw(url, options, timeoutMs);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
    return data;
  }

  function readStreamChunk(reader, timeoutMs = 45000) {
    return new Promise((resolve, reject) => {
      const timer = global.setTimeout(
        () => reject(new Error(`模型回复流 ${Math.round(timeoutMs / 1000)} 秒没有新数据`)),
        timeoutMs,
      );
      reader.read().then(
        (chunk) => { global.clearTimeout(timer); resolve(chunk); },
        (error) => { global.clearTimeout(timer); reject(error); },
      );
    });
  }

  function callNative(name, ...args) {
    try {
      const method = global.DaxiguaVoice?.[name];
      if (typeof method === 'function') return method.apply(global.DaxiguaVoice, args);
    } catch (_) {}
    return undefined;
  }

  async function requestWakeLock() {
    if (state.embedded || !navigator.wakeLock || document.visibilityState !== 'visible') return;
    if (state.wakeLock && !state.wakeLock.released) return;
    try {
      const sentinel = await navigator.wakeLock.request('screen');
      state.wakeLock = sentinel;
      sentinel.addEventListener('release', () => {
        if (state.wakeLock === sentinel) state.wakeLock = null;
      }, { once: true });
    } catch (_) {
      state.wakeLock = null;
    }
  }

  async function releaseWakeLock() {
    const sentinel = state.wakeLock;
    state.wakeLock = null;
    if (!sentinel || sentinel.released) return;
    try { await sentinel.release(); } catch (_) {}
  }

  function recorderMimeType() {
    const choices = [
      'audio/webm;codecs=opus', 'audio/mp4;codecs=mp4a.40.2',
      'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus',
    ];
    return choices.find((type) => {
      try { return MediaRecorder.isTypeSupported(type); } catch (_) { return false; }
    }) || '';
  }

  function showCaptureRecovery(message = 'iOS 暂停了麦克风，点一下即可恢复。') {
    if (state.ended) return;
    state.captureSuspended = true;
    ui.resume.classList.remove('hidden');
    setStatus(message, 'paused');
  }

  function wireCaptureTrack(stream) {
    const track = stream.getAudioTracks()[0];
    if (!track) return;
    track.addEventListener('mute', () => {
      if (state.stream !== stream || state.ended) return;
      state.captureSuspended = true;
      if (document.visibilityState === 'visible') {
        showCaptureRecovery('麦克风现在被系统静音，点一下检查并恢复。');
      }
      logEvent('microphone_track_muted');
    });
    track.addEventListener('unmute', () => {
      if (state.stream !== stream || state.ended) return;
      state.captureSuspended = false;
      state.captureEnded = false;
      ui.resume.classList.add('hidden');
      logEvent('microphone_track_unmuted');
      resumeListening();
    });
    track.addEventListener('ended', () => {
      if (state.stream !== stream || state.ended) return;
      state.captureEnded = true;
      showCaptureRecovery('麦克风线路已经结束，点一下重新接通。');
      logEvent('microphone_track_ended');
    });
  }

  async function stopRecorderQuietly() {
    state.utterance = null;
    state.finalizing = null;
    state.preRoll = [];
    const recorder = state.recorder;
    if (!recorder || recorder.state !== 'recording') return;
    state.recorderStoppingForPlayback = true;
    await new Promise((resolve) => {
      const timer = global.setTimeout(resolve, 1200);
      recorder.addEventListener('stop', () => {
        global.clearTimeout(timer);
        resolve();
      }, { once: true });
      try { recorder.stop(); } catch (_) {
        global.clearTimeout(timer);
        resolve();
      }
    });
    if (state.recorder === recorder) state.recorder = null;
  }

  async function attachMicrophoneStream() {
    const nextStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });
    await stopRecorderQuietly();
    const AudioContext = global.AudioContext || global.webkitAudioContext;
    if (!AudioContext) {
      nextStream.getTracks().forEach((track) => track.stop());
      throw new Error('此浏览器没有 WebAudio');
    }
    if (!state.audioContext || state.audioContext.state === 'closed') {
      state.audioContext = new AudioContext({ latencyHint: 'interactive' });
      state.analyser = state.audioContext.createAnalyser();
      state.analyser.fftSize = 2048;
      state.analyser.smoothingTimeConstant = 0.1;
    }
    await state.audioContext.resume().catch(() => {});
    try { state.mediaSource?.disconnect?.(); } catch (_) {}
    state.stream?.getTracks?.().forEach((track) => track.stop());
    state.stream = nextStream;
    state.mediaSource = state.audioContext.createMediaStreamSource(nextStream);
    state.mediaSource.connect(state.analyser);
    state.captureSuspended = false;
    state.captureEnded = false;
    ui.resume.classList.add('hidden');
    wireCaptureTrack(nextStream);
  }

  async function recoverMicrophone() {
    if (state.ended || ui.resume.disabled) return;
    ui.resume.disabled = true;
    state.busy = true;
    setStatus('正在重新取得麦克风…', 'starting');
    try {
      await attachMicrophoneStream();
      showError('');
      await requestWakeLock();
      state.busy = false;
      resumeListening();
      await logEvent('microphone_recovered');
    } catch (error) {
      state.busy = false;
      showError(`麦克风仍未恢复：${error.message}`);
      showCaptureRecovery('请确认 Safari 的麦克风权限后再点一次。');
    } finally {
      ui.resume.disabled = false;
    }
  }

  function inspectCaptureAfterResume() {
    if (state.ended) return;
    const track = state.stream?.getAudioTracks?.()[0];
    if (!track || track.readyState === 'ended' || state.captureEnded) {
      showCaptureRecovery('iOS 返回前台后需要重新接通麦克风。');
      return;
    }
    if (track.muted || state.captureSuspended) {
      showCaptureRecovery('iOS 返回前台后麦克风仍是静音，点一下恢复。');
      return;
    }
    ui.resume.classList.add('hidden');
    state.captureSuspended = false;
    resumeListening();
  }

  function calculateRms(data) {
    let sum = 0;
    let peak = 0;
    for (let index = 0; index < data.length; index += 1) {
      const value = data[index];
      sum += value * value;
      peak = Math.max(peak, Math.abs(value));
    }
    return { rms: Math.sqrt(sum / Math.max(1, data.length)), peak };
  }

  function autocorrelationPitch(data, sampleRate) {
    const size = data.length;
    let rms = 0;
    for (let index = 0; index < size; index += 1) rms += data[index] * data[index];
    rms = Math.sqrt(rms / size);
    if (rms < 0.008) return 0;
    const minLag = Math.floor(sampleRate / 500);
    const maxLag = Math.min(Math.floor(sampleRate / 55), size - 2);
    let bestLag = 0;
    let bestCorrelation = 0;
    for (let lag = minLag; lag <= maxLag; lag += 1) {
      let correlation = 0;
      let normA = 0;
      let normB = 0;
      for (let index = 0; index < size - lag; index += 2) {
        const a = data[index];
        const b = data[index + lag];
        correlation += a * b;
        normA += a * a;
        normB += b * b;
      }
      const normalized = correlation / Math.sqrt(Math.max(1e-9, normA * normB));
      if (normalized > bestCorrelation) {
        bestCorrelation = normalized;
        bestLag = lag;
      }
    }
    return bestCorrelation >= 0.56 && bestLag ? sampleRate / bestLag : 0;
  }

  function summarizeFrames(frames, durationMs = 0) {
    const source = frames.length ? frames : [{ rms: 0, peak: 0, pitch: 0, voiced: false }];
    const pitches = source.map((item) => item.pitch).filter((value) => value > 0);
    const average = (values) => values.reduce((sum, value) => sum + value, 0) / Math.max(1, values.length);
    return {
      rms: Number(average(source.map((item) => item.rms)).toFixed(4)),
      peak: Number(Math.max(...source.map((item) => item.peak)).toFixed(4)),
      pitch_hz: Number(average(pitches).toFixed(1)) || 0,
      pitch_min_hz: pitches.length ? Number(Math.min(...pitches).toFixed(1)) : 0,
      pitch_max_hz: pitches.length ? Number(Math.max(...pitches).toFixed(1)) : 0,
      voiced_ratio: Number((source.filter((item) => item.voiced).length / source.length).toFixed(3)),
      frame_count: source.length,
      duration_ms: Math.max(0, Math.round(durationMs)),
    };
  }

  function updateMeter(frame) {
    ui.meter.style.width = `${Math.min(100, frame.rms * 850)}%`;
    ui.acoustic.textContent = `RMS ${frame.rms.toFixed(4)} · ${frame.pitch ? `${frame.pitch.toFixed(0)} Hz` : '等待稳定音高'}`;
  }

  function startUtterance(manual = false) {
    if (state.busy || state.playing || state.ended || state.utterance || !state.recorder) return;
    state.utterance = {
      chunks: state.preRoll.splice(0),
      frames: [],
      startedAt: performance.now(),
      startedClockMs: state.audioClockMs,
      silenceMs: 0,
      manual,
    };
    ui.talk.classList.toggle('is-recording', manual);
    setStatus('正在听你说…', 'recording');
  }

  function finalizeUtterance(reason = 'silence') {
    if (!state.utterance || state.finalizing || !state.recorder) return;
    const current = state.utterance;
    current.reason = reason;
    current.durationMs = performance.now() - current.startedAt;
    state.utterance = null;
    state.finalizing = current;
    state.busy = true;
    ui.talk.classList.remove('is-recording');
    setStatus('正在整理这句话…', 'transcribing');
    try {
      if (state.recorder.state === 'recording') state.recorder.stop();
    } catch (error) {
      state.finalizing = null;
      state.busy = false;
      showError(`录音结束失败：${error.message}`);
      resumeListening();
    }
  }

  function startRollingRecorder() {
    if (state.ended || state.playing || state.busy || !state.stream || state.recorder) return;
    const mimeType = recorderMimeType();
    const recorder = new MediaRecorder(state.stream, mimeType ? { mimeType } : undefined);
    state.recorder = recorder;
    state.recorderStoppingForPlayback = false;
    recorder.addEventListener('dataavailable', (event) => {
      if (!event.data?.size) return;
      if (state.utterance) state.utterance.chunks.push(event.data);
      else if (state.finalizing) state.finalizing.chunks.push(event.data);
      else {
        state.preRoll.push(event.data);
        if (state.preRoll.length > 4) state.preRoll.shift();
      }
    });
    recorder.addEventListener('stop', () => {
      if (state.recorder === recorder) state.recorder = null;
      if (state.recorderStoppingForPlayback) {
        state.recorderStoppingForPlayback = false;
        state.preRoll = [];
        return;
      }
      const utterance = state.finalizing;
      state.finalizing = null;
      if (!utterance) return;
      const blob = new Blob(utterance.chunks, { type: recorder.mimeType || mimeType || 'audio/webm' });
      processUtterance(blob, utterance).catch((error) => {
        showError(error.message || '语音处理失败');
        state.busy = false;
        resumeListening();
      });
    }, { once: true });
    recorder.addEventListener('error', () => {
      state.recorder = null;
      state.busy = false;
      showError('麦克风录音器发生错误，请重新进入通话。');
      logEvent('media_recorder_error');
    });
    recorder.start(250);
  }

  function stopRecorderForPlayback() {
    state.utterance = null;
    state.finalizing = null;
    state.preRoll = [];
    if (state.recorder?.state === 'recording') {
      state.recorderStoppingForPlayback = true;
      try { state.recorder.stop(); } catch (_) { state.recorder = null; }
    }
  }

  function sampleAudio() {
    if (state.ended || !state.analyser || !state.audioContext) return;
    const data = new Float32Array(state.analyser.fftSize);
    state.analyser.getFloatTimeDomainData(data);
    const energy = calculateRms(data);
    const pitch = autocorrelationPitch(data, state.audioContext.sampleRate);
    const frame = {
      rms: energy.rms,
      peak: energy.peak,
      pitch,
      voiced: pitch > 0 && energy.rms >= 0.008,
    };
    updateMeter(frame);
    if (!state.playing) {
      state.sampleClockMs += 120;
      state.ambientFrames.push(frame);
      if (state.ambientFrames.length > 15000) state.ambientFrames.shift();
    }
    if (state.sleepMode && state.sampleClockMs >= state.nextSleepSnapshotMs) {
      const snapshotFrames = state.ambientFrames.slice(-15000);
      const acoustic = summarizeFrames(snapshotFrames, 30 * 60 * 1000);
      fetchJSON(`/api/voice/calls/${encodeURIComponent(state.callId)}/sleep-snapshot`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sample_clock_ms: state.sampleClockMs, acoustic }),
      }, 15000).catch(() => logEvent('sleep_snapshot_failed'));
      state.nextSleepSnapshotMs += 30 * 60 * 1000;
    }
    if (state.playing || state.busy) return;
    if (state.utterance) {
      state.utterance.frames.push(frame);
      if (frame.rms < 0.008) state.utterance.silenceMs += 120;
      else state.utterance.silenceMs = 0;
      if (!state.utterance.manual) {
        const speechMs = performance.now() - state.utterance.startedAt;
        const silenceLimit = speechMs > 10000 ? 2800 : 1800;
        if (state.utterance.silenceMs >= silenceLimit && speechMs >= 500) {
          finalizeUtterance('vad-silence');
        }
      }
      return;
    }
    if (state.inputMode !== 'auto') return;
    if (frame.rms >= 0.012 && frame.voiced) state.vadStartFrames += 1;
    else state.vadStartFrames = Math.max(0, state.vadStartFrames - 1);
    if (state.vadStartFrames >= 2) {
      state.vadStartFrames = 0;
      startUtterance(false);
    } else {
      setStatus('线路在线 · 自动听你说话', 'listening');
    }
  }

  async function logEvent(event, detail = '') {
    if (!state.callId) return;
    try {
      await fetchJSON(`/api/voice/calls/${encodeURIComponent(state.callId)}/event`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event, detail }),
      }, 8000);
    } catch (_) {}
  }

  function appendSubtitle(role, text, options = {}) {
    ui.subtitles.querySelector('.call-empty')?.remove();
    const item = document.createElement('article');
    item.className = `call-subtitle ${role}`;
    const who = role === 'user' ? '你' : companionName;
    const line = document.createElement('div');
    line.className = 'line';
    if (Array.isArray(options.blocks) && options.blocks.length) {
      options.blocks.forEach((block, index) => {
        const span = document.createElement('span');
        span.className = 'script-block';
        span.dataset.block = String(index);
        span.textContent = block.text;
        line.appendChild(span);
      });
    } else {
      line.textContent = String(text || '');
    }
    const stamp = document.createElement('small');
    stamp.textContent = `${who} · ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
    item.append(stamp, line);
    if (options.observation) {
      const observation = document.createElement('div');
      observation.className = 'voice-observation';
      observation.textContent = options.observation;
      item.appendChild(observation);
    }
    ui.subtitles.appendChild(item);
    ui.subtitles.scrollTop = ui.subtitles.scrollHeight;
    return item;
  }

  async function translateSubtitle(item, text) {
    if (!state.settings?.settings?.translation_enabled || !text.trim()) return;
    const lines = text.split(/\n+/).map((line) => line.trim()).filter(Boolean);
    if (!lines.length) return;
    try {
      const result = await fetchJSON('/api/voice/translate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lines, target_language: 'zh' }),
      }, 45000);
      if (!result.translated) return;
      const translated = document.createElement('div');
      translated.className = 'translation';
      translated.textContent = result.lines.join('\n');
      item.appendChild(translated);
    } catch (error) {
      logEvent('translation_failed', error.message);
    }
  }

  function moodObservation(acoustic, mood) {
    const audio = `音量 ${Number(acoustic.rms || 0).toFixed(3)} · 音高 ${Number(acoustic.pitch_hz || 0).toFixed(0) || '—'}Hz`;
    if (!mood?.available) return audio;
    return `${audio} · ${mood.emotion || '情绪未知'} · ${mood.pace || '节奏未知'}`;
  }

  async function processUtterance(blob, utterance) {
    if (state.ended) return;
    if (!blob.size) throw new Error('这一段录音是空的');
    const acoustic = summarizeFrames(utterance.frames, utterance.durationMs);
    // Human/noise gating belongs upstream of the language model.
    if (acoustic.frame_count > 0 && acoustic.voiced_ratio < 0.03) {
      await logEvent('local_no_human_signal', JSON.stringify(acoustic));
      setStatus('只听到环境声，继续等你说话', 'listening');
      state.busy = false;
      resumeListening();
      return;
    }
    setStatus('正在转成文字并分析声音…', 'transcribing');
    const extension = blob.type.includes('mp4') ? 'm4a' : blob.type.includes('ogg') ? 'ogg' : 'webm';
    const form = new FormData();
    form.append('file', blob, `call-${Date.now()}.${extension}`);
    form.append('language_code', 'zh');
    form.append('call_id', state.callId);
    form.append('keyterms', JSON.stringify(state.settings?.settings?.keyterms || []));
    form.append('acoustic_json', JSON.stringify(acoustic));
    form.append('started_ms', String(Math.round(utterance.startedClockMs)));
    form.append('duration_ms', String(Math.round(utterance.durationMs)));
    form.append('private_mode', String(state.privateMode));
    form.append('sleep_mode', String(state.sleepMode));
    const response = await fetchJSON('/api/voice/transcribe', { method: 'POST', body: form }, 130000);
    if (!response.human_signal || !String(response.text || '').trim()) {
      await logEvent('server_no_human_signal', (response.audio_events || []).join(','));
      setStatus('没有检测到可发送的人声，继续听着', 'listening');
      state.busy = false;
      resumeListening();
      return;
    }
    const transcript = String(response.text).trim();
    const userItem = appendSubtitle('user', transcript, {
      observation: moodObservation(acoustic, response.mood || {}),
    });
    translateSubtitle(userItem, transcript);
    state.audioClockMs += Math.max(0, Number(utterance.durationMs || 0));
    post('voice:clock', { audio_clock_ms: state.audioClockMs });
    setStatus(`${companionName}正在想怎么回答…`, 'thinking');
    const turnId = `voice_${Date.now().toString(36)}_${++state.turnSequence}`;
    const reply = await requestAssistant({
      turn_id: turnId,
      text: transcript,
      duration_ms: Math.round(utterance.durationMs),
      acoustic,
      mood: response.mood || {},
      transcriber: response.provider || 'ElevenLabs',
    });
    if (state.ended) return;
    await handleAssistantReply(reply);
  }

  function requestAssistant(payload) {
    if (!state.embedded) return requestAssistantStandalone(payload);
    return new Promise((resolve, reject) => {
      const timer = global.setTimeout(() => {
        state.pendingTurns.delete(payload.turn_id);
        reject(new Error('模型回复等待超时'));
      }, 190000);
      state.pendingTurns.set(payload.turn_id, {
        resolve: (value) => { global.clearTimeout(timer); resolve(value); },
        reject: (error) => { global.clearTimeout(timer); reject(error); },
      });
      post('voice:user-turn', payload);
    });
  }

  async function requestAssistantStandalone(payload) {
    const clientId = global.crypto?.randomUUID?.() || `voice_${Date.now()}_${Math.random()}`;
    const controller = new AbortController();
    const overallTimer = global.setTimeout(() => controller.abort(), 190000);
    let reader = null;
    try {
      const response = await fetch('/api/chat', {
        cache: 'no-store',
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify({
          message: payload.text,
          session_id: state.sessionId,
          client_request_id: clientId,
          client_metadata: {
            voice_transcript: true,
            voice_duration_ms: payload.duration_ms,
            voice_transcriber: payload.transcriber,
            voice_acoustic: payload.acoustic,
            voice_mood: payload.mood,
            voice_private_mode: state.privateMode,
            voice_sleep_mode: state.sleepMode,
          },
        }),
      });
      if (!response.ok || !response.body) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || data.error || `HTTP ${response.status}`);
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let text = '';
      while (true) {
        // An open SSE connection must still make progress; otherwise a dead proxy
        // could leave the microphone paused forever.
        const chunk = await readStreamChunk(reader, 45000);
        const { done, value } = chunk;
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let boundary;
        while ((boundary = buffer.search(/\r?\n\r?\n/)) !== -1) {
          const block = buffer.slice(0, boundary);
          const separator = buffer.slice(boundary).match(/^\r?\n\r?\n/)[0];
          buffer = buffer.slice(boundary + separator.length);
          const line = block.split(/\r?\n/).find((item) => item.startsWith('data:'));
          if (!line) continue;
          try {
            const event = JSON.parse(line.slice(5).trim());
            if (event.type === 'text') text += event.text || '';
            if (event.type === 'error') throw new Error(event.error || '模型回复失败');
          } catch (error) {
            if (error instanceof SyntaxError) continue;
            throw error;
          }
        }
      }
      const hangup = /\[call:hangup\]/i.test(text);
      const important = /^\s*\[important\]/i.test(text);
      return {
        text: text.replace(/\[call:hangup\]/ig, '').replace(/^\s*\[important\]\s*/i, '').trim(),
        hangup,
        important,
      };
    } catch (error) {
      if (error?.name === 'AbortError') throw new Error('模型回复等待超时');
      throw error;
    } finally {
      global.clearTimeout(overallTimer);
      if (reader) {
        try { await reader.cancel(); } catch (_) {}
        try { reader.releaseLock(); } catch (_) {}
      }
    }
  }

  function plainSpokenText(text) {
    return String(text || '')
      .replace(/```[\s\S]*?```/g, '。这里省略了一段代码。')
      .replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
      .replace(/^#{1,6}\s+/gm, '')
      .replace(/[*_~>|`]/g, '')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  function audioBytes(base64Value) {
    const raw = atob(String(base64Value || ''));
    return Uint8Array.from(raw, (character) => character.charCodeAt(0)).buffer;
  }

  function buildScriptBlocks(text, alignment, durationSeconds) {
    const characters = Array.isArray(alignment?.characters) ? alignment.characters : [...text];
    const starts = Array.isArray(alignment?.character_start_times_seconds)
      ? alignment.character_start_times_seconds : [];
    const ends = Array.isArray(alignment?.character_end_times_seconds)
      ? alignment.character_end_times_seconds : [];
    const joined = characters.join('') || text;
    const boundaries = [];
    let start = 0;
    for (let index = 0; index < joined.length; index += 1) {
      const length = index - start + 1;
      const boundary = joined[index] === '\n'
        || (/[。！？.!?]/.test(joined[index]) && length >= 12)
        || length >= 72;
      if (boundary) {
        const value = joined.slice(start, index + 1);
        if (value.trim()) boundaries.push({ start, end: index + 1, text: value });
        start = index + 1;
      }
    }
    if (start < joined.length) {
      const value = joined.slice(start);
      if (value.trim()) boundaries.push({ start, end: joined.length, text: value });
    }
    const count = Math.max(1, joined.length);
    return boundaries.map((block) => ({
      ...block,
      startSeconds: Number(starts[block.start] ?? (block.start / count) * durationSeconds),
      endSeconds: Number(ends[block.end - 1] ?? (block.end / count) * durationSeconds),
    }));
  }

  async function playDecodedAudio(arrayBuffer, subtitleItem = null, blocks = []) {
    if (!state.audioContext) throw new Error('WebAudio 没有准备好');
    stopRecorderForPlayback();
    state.playing = true;
    state.busy = true;
    setStatus(`${companionName}正在说话…`, 'speaking');
    let decoded = null;
    let source = null;
    let animation = 0;
    try {
      await state.audioContext.resume().catch(() => {});
      decoded = await state.audioContext.decodeAudioData(arrayBuffer.slice(0));
      source = state.audioContext.createBufferSource();
      source.buffer = decoded;
      source.connect(state.audioContext.destination);
      state.activeSource = source;
      const beganAt = state.audioContext.currentTime;
      subtitleItem?.classList.add('is-speaking');
      const update = () => {
        const elapsed = state.audioContext.currentTime - beganAt;
        let activeIndex = -1;
        blocks.forEach((block, index) => {
          if (elapsed >= block.startSeconds && elapsed < block.endSeconds) activeIndex = index;
        });
        subtitleItem?.querySelectorAll('.script-block').forEach((node, index) => {
          node.classList.toggle('is-current', index === activeIndex);
        });
        if (state.playing && state.activeSource === source) animation = requestAnimationFrame(update);
      };
      if (blocks.length) animation = requestAnimationFrame(update);
      await new Promise((resolve, reject) => {
        let settled = false;
        const finish = (callback, value) => {
          if (settled) return;
          settled = true;
          callback(value);
        };
        source.onended = () => finish(resolve);
        try { source.start(); } catch (error) { finish(reject, error); }
      });
      state.audioClockMs += Math.round(decoded.duration * 1000);
      post('voice:clock', { audio_clock_ms: state.audioClockMs });
    } finally {
      cancelAnimationFrame(animation);
      subtitleItem?.classList.remove('is-speaking');
      subtitleItem?.querySelectorAll('.script-block').forEach((node) => node.classList.remove('is-current'));
      if (state.activeSource === source) state.activeSource = null;
      state.playing = false;
    }
  }

  async function playTimedReply(text, subtitleItem) {
    let preferredVoice = '';
    try { preferredVoice = localStorage.getItem('daxigua:voice-id') || ''; } catch (_) {}
    const result = await fetchJSON('/api/voice/synthesize-timed', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        voice_id: preferredVoice,
        call_id: state.callId,
        started_ms: Math.round(state.audioClockMs),
      }),
    }, 130000);
    const alignment = result.alignment || result.normalized_alignment || {};
    const spoken = String(result.spoken_text || text);
    const blocks = buildScriptBlocks(spoken, alignment, Number(result.duration_ms || 0) / 1000);
    const line = subtitleItem.querySelector('.line');
    line.textContent = '';
    blocks.forEach((block, index) => {
      const span = document.createElement('span');
      span.className = 'script-block';
      span.dataset.block = String(index);
      span.textContent = block.text;
      line.appendChild(span);
    });
    await playDecodedAudio(audioBytes(result.audio_base64), subtitleItem, blocks);
  }

  async function browserSpeak(text) {
    if (!global.speechSynthesis || !global.SpeechSynthesisUtterance) {
      throw new Error('系统朗读不可用');
    }
    stopRecorderForPlayback();
    state.playing = true;
    return new Promise((resolve, reject) => {
      const utterance = new SpeechSynthesisUtterance(plainSpokenText(text));
      const timer = global.setTimeout(() => {
        global.speechSynthesis.cancel();
        reject(new Error('系统朗读超时'));
      }, 60000);
      utterance.lang = 'zh-CN';
      utterance.onend = () => { global.clearTimeout(timer); resolve(); };
      utterance.onerror = (event) => { global.clearTimeout(timer); reject(new Error(event.error || '系统朗读失败')); };
      global.speechSynthesis.cancel();
      global.speechSynthesis.speak(utterance);
    }).finally(() => { state.playing = false; });
  }

  async function handleAssistantReply(reply) {
    const text = String(reply?.text || '').trim();
    if (!text) throw new Error('这一轮没有收到文字回复');
    const item = appendSubtitle('assistant', plainSpokenText(text));
    translateSubtitle(item, plainSpokenText(text));
    const shouldSpeak = state.settings?.settings?.call_auto_reply !== false
      && (!state.sleepMode || reply.important === true);
    if (shouldSpeak) {
      try {
        if (state.settings?.tts_ready) await playTimedReply(text, item);
        else await browserSpeak(text);
      } catch (error) {
        showError(`字幕已显示；声音没有播放：${error.message}`);
        await logEvent('assistant_playback_failed', error.message);
      }
    } else if (state.sleepMode) {
      setStatus('陪睡静音中 · 我还在听', 'listening');
    }
    if (reply.hangup) {
      await endCall('assistant');
      return;
    }
    state.busy = false;
    await new Promise((resolve) => global.setTimeout(resolve, 250));
    resumeListening();
  }

  function resumeListening() {
    if (state.ended || state.playing) return;
    state.busy = false;
    state.preRoll = [];
    startRollingRecorder();
    setStatus(
      state.inputMode === 'auto' ? '线路在线 · 自动听你说话' : '线路在线 · 按住说话',
      'listening',
    );
  }

  async function playGreeting() {
    try {
      const response = await fetchRaw('/api/voice/greeting', {}, 2500);
      if (response.status === 204) return false;
      if (!response.ok) return false;
      const buffer = await response.arrayBuffer();
      await playDecodedAudio(buffer);
      return true;
    } catch (_) {
      return false;
    }
  }

  async function updateModes() {
    state.privateMode = ui.privateMode.checked;
    state.sleepMode = ui.sleepMode.checked;
    post('voice:mode', {
      private_mode: state.privateMode,
      sleep_mode: state.sleepMode,
    });
    try {
      await fetchJSON(`/api/voice/calls/${encodeURIComponent(state.callId)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          private_mode: state.privateMode,
          sleep_mode: state.sleepMode,
          audio_clock_ms: Math.round(state.audioClockMs),
        }),
      }, 15000);
      showError('');
      if (state.sleepMode) setStatus('陪睡模式 · 只听真人声音，下行保持静音', 'listening');
    } catch (error) {
      showError(`模式没有保存：${error.message}`);
    }
  }

  function wireUI() {
    ui.inputMode.addEventListener('change', () => {
      state.inputMode = ui.inputMode.value === 'manual' ? 'manual' : 'auto';
      ui.talk.classList.toggle('hidden', state.inputMode !== 'manual');
      if (state.utterance) finalizeUtterance('mode-change');
      resumeListening();
    });
    ui.privateMode.addEventListener('change', updateModes);
    ui.sleepMode.addEventListener('change', updateModes);
    ui.route.addEventListener('click', () => {
      state.route = state.route === 'speaker' ? 'earpiece' : 'speaker';
      ui.route.querySelector('small').textContent = state.route === 'speaker' ? '扬声器' : '听筒';
      post('voice:route', { route: state.route });
      callNative('setAudioRoute', state.route);
    });
    ui.minimize.addEventListener('click', () => post('voice:minimize'));
    ui.browse.addEventListener('click', () => post('voice:browse', { url: '/' }));
    ui.end.addEventListener('click', () => endCall('user'));
    ui.resume.addEventListener('click', recoverMicrophone);
    ui.talk.addEventListener('contextmenu', (event) => event.preventDefault());
    ui.talk.addEventListener('pointerdown', (event) => {
      if (state.inputMode !== 'manual') return;
      event.preventDefault();
      ui.talk.setPointerCapture?.(event.pointerId);
      startUtterance(true);
    });
    ['pointerup', 'pointercancel', 'lostpointercapture'].forEach((name) => {
      ui.talk.addEventListener(name, (event) => {
        if (!state.utterance?.manual) return;
        event.preventDefault();
        finalizeUtterance(name);
      });
    });
    document.addEventListener('pointerdown', () => state.audioContext?.resume?.(), { once: true });
  }

  function onHostMessage(event) {
    if (!state.embedded || event.origin !== ORIGIN || event.source !== global.parent) return;
    const message = event.data;
    if (!message || typeof message !== 'object') return;
    if (message.type === 'voice:assistant-reply') {
      const pending = state.pendingTurns.get(message.turn_id);
      if (pending) {
        state.pendingTurns.delete(message.turn_id);
        pending.resolve({ text: message.text, important: message.important, hangup: message.hangup });
      }
    } else if (message.type === 'voice:turn-error') {
      const pending = state.pendingTurns.get(message.turn_id);
      if (pending) {
        state.pendingTurns.delete(message.turn_id);
        pending.reject(new Error(message.error || '模型回复失败'));
      }
    } else if (message.type === 'voice:ended') {
      stopLocalMedia();
    } else if (message.type === 'voice:host-suspended') {
      setStatus('iOS 后台可能暂停麦克风；回到页面后会检查线路', 'paused');
    } else if (message.type === 'voice:host-resumed') {
      inspectCaptureAfterResume();
    }
  }

  function startStandaloneHeartbeat() {
    if (state.embedded) return;
    const beat = () => {
      if (state.ended) return;
      fetchJSON(`/api/voice/calls/${encodeURIComponent(state.callId)}/heartbeat`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ audio_clock_ms: Math.round(state.audioClockMs) }),
      }, 12000).catch(() => {});
      callNative('voiceHeartbeat', state.callId);
    };
    beat();
    state.heartbeatTimer = global.setInterval(beat, 60000);
  }

  function stopLocalMedia() {
    state.ended = true;
    global.clearInterval(state.sampleTimer);
    global.clearInterval(state.heartbeatTimer);
    state.pendingTurns.forEach((pending) => pending.reject(new Error('通话已经结束')));
    state.pendingTurns.clear();
    try { state.activeSource?.stop(); } catch (_) {}
    global.speechSynthesis?.cancel?.();
    try { if (state.recorder?.state === 'recording') state.recorder.stop(); } catch (_) {}
    state.stream?.getTracks?.().forEach((track) => track.stop());
    try { state.mediaSource?.disconnect?.(); } catch (_) {}
    state.mediaSource = null;
    state.audioContext?.close?.().catch?.(() => {});
    releaseWakeLock();
  }

  async function endCall(reason = 'user') {
    if (state.pageEnding) return;
    state.pageEnding = true;
    stopLocalMedia();
    post('voice:end', { reason });
    callNative('stopVoiceKeepAlive', state.callId);
    if (!state.embedded) {
      try {
        await fetchJSON(`/api/voice/calls/${encodeURIComponent(state.callId)}`, {
          method: 'DELETE', keepalive: true,
        }, 8000);
      } catch (_) {}
      setStatus('通话已结束', 'ended');
    }
  }

  async function init() {
    wireUI();
    if (!state.embedded) {
      ui.minimize.classList.add('hidden');
      ui.browse.classList.add('hidden');
    }
    global.addEventListener('message', onHostMessage);
    if (!state.callId || !state.sessionId) {
      showError('缺少通话编号或聊天窗口编号。');
      post('voice:mic-failed', { error: 'missing-call-context' });
      return;
    }
    try {
      const [settings, call] = await Promise.all([
        fetchJSON('/api/voice/settings', {}, 15000),
        fetchJSON(`/api/voice/calls/${encodeURIComponent(state.callId)}`, {}, 15000),
      ]);
      state.settings = settings;
      state.call = call;
      state.privateMode = Boolean(call.private_mode);
      state.sleepMode = Boolean(call.sleep_mode);
      state.audioClockMs = Number(call.audio_clock_ms || 0);
      ui.privateMode.checked = state.privateMode;
      ui.sleepMode.checked = state.sleepMode;
      await attachMicrophoneStream();
      startRollingRecorder();
      state.sampleTimer = global.setInterval(sampleAudio, 120);
      startStandaloneHeartbeat();
      requestWakeLock();
      callNative('startVoiceKeepAlive', state.callId);
      post('voice:ready', { call_id: state.callId });
      const greeted = await playGreeting();
      state.playing = false;
      state.busy = false;
      if (!greeted && state.settings?.settings?.greeting_text) {
        try { await browserSpeak(state.settings.settings.greeting_text); } catch (_) {}
        state.playing = false;
      }
      await new Promise((resolve) => global.setTimeout(resolve, 250));
      resumeListening();
    } catch (error) {
      showError(`麦克风没有启动：${error.message}`);
      setStatus('语音输入不可用', 'error');
      post('voice:mic-failed', { error: error.message });
      logEvent('microphone_start_failed', error.message);
    }
  }

  global.addEventListener('pagehide', () => {
    if (state.pageEnding || state.embedded || !state.callId) return;
    try {
      navigator.sendBeacon(
        `/api/voice/calls/${encodeURIComponent(state.callId)}/end`,
        new Blob([], { type: 'text/plain' }),
      );
    } catch (_) {}
  });

  document.addEventListener('visibilitychange', () => {
    if (state.ended) return;
    if (document.visibilityState === 'hidden') {
      if (state.utterance && !state.utterance.manual) finalizeUtterance('page-hidden');
      setStatus('切到后台时 iOS 可能暂停麦克风', 'paused');
      return;
    }
    requestWakeLock();
    global.setTimeout(inspectCaptureAfterResume, 350);
  });
  global.addEventListener('pageshow', () => {
    if (state.ended) return;
    requestWakeLock();
    global.setTimeout(inspectCaptureAfterResume, 350);
  });

  init();
})(window);
