/**
 * Daxigua voice host: keeps the same-origin call iframe alive while the user
 * returns to chat or opens the small in-app browser.  All cross-frame messages
 * are origin- and source-checked.
 */
(function voiceHostBootstrap(global) {
  'use strict';

  const ORIGIN = global.location.origin;
  const bridge = global.DaxiguaVoiceChat;
  const state = {
    active: false,
    callId: '',
    sessionId: '',
    overlay: null,
    frame: null,
    ready: false,
    minimized: false,
    browserOpen: false,
    privateMode: false,
    sleepMode: false,
    audioClockMs: 0,
    readyTimer: 0,
    heartbeatTimer: 0,
    wakeLock: null,
    resumePromise: null,
    hiddenAt: 0,
    turnQueue: Promise.resolve(),
    previousHistoryState: null,
    fullPageHandoff: false,
  };

  function escapeHTML(value) {
    const node = document.createElement('div');
    node.textContent = String(value || '');
    return node.innerHTML;
  }

  async function fetchWithTimeout(url, options = {}, timeoutMs = 15000) {
    const controller = new AbortController();
    const timer = global.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        cache: 'no-store',
        ...options,
        signal: controller.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const problem = new Error(data.detail || data.error || `HTTP ${response.status}`);
        problem.status = response.status;
        throw problem;
      }
      return data;
    } catch (error) {
      if (error?.name === 'AbortError') throw new Error('语音线路请求超时');
      throw error;
    } finally {
      global.clearTimeout(timer);
    }
  }

  function withTimeout(promise, timeoutMs, message) {
    let timer = 0;
    return Promise.race([
      Promise.resolve(promise),
      new Promise((_, reject) => {
        timer = global.setTimeout(() => reject(new Error(message)), timeoutMs);
      }),
    ]).finally(() => global.clearTimeout(timer));
  }

  async function requestWakeLock() {
    if (!navigator.wakeLock || document.visibilityState !== 'visible') return false;
    if (state.wakeLock && !state.wakeLock.released) return true;
    try {
      const sentinel = await navigator.wakeLock.request('screen');
      state.wakeLock = sentinel;
      sentinel.addEventListener('release', () => {
        if (state.wakeLock === sentinel) state.wakeLock = null;
      }, { once: true });
      return true;
    } catch (_) {
      state.wakeLock = null;
      return false;
    }
  }

  async function releaseWakeLock() {
    const sentinel = state.wakeLock;
    state.wakeLock = null;
    if (!sentinel || sentinel.released) return;
    try { await sentinel.release(); } catch (_) {}
  }

  function postToFrame(type, payload = {}) {
    if (!state.frame?.contentWindow) return;
    state.frame.contentWindow.postMessage({ type, ...payload }, ORIGIN);
  }

  function callNative(name, ...args) {
    try {
      const method = global.DaxiguaVoice?.[name];
      if (typeof method === 'function') return method.apply(global.DaxiguaVoice, args);
    } catch (error) {
      console.warn(`[VoiceHost] native ${name} failed`, error);
    }
    return undefined;
  }

  function buildOverlay(companionName) {
    const overlay = document.createElement('section');
    overlay.className = 'voice-host-overlay';
    overlay.setAttribute('aria-label', `和${companionName}通话`);
    overlay.innerHTML = `
      <div class="voice-host-frame-shell">
        <iframe class="voice-host-frame" title="陪伴通话" allow="microphone; autoplay" referrerpolicy="same-origin"></iframe>
        <div class="voice-host-fallback hidden" role="alert">
          <strong>通话页没有在 5 秒内准备好</strong>
          <span>可以改用全屏通话；当前通话编号会继续沿用。</span>
          <button type="button" data-voice-host-action="fullscreen">打开全屏通话</button>
          <button type="button" data-voice-host-action="retry">重新载入</button>
        </div>
      </div>
      <button class="voice-host-bubble hidden" type="button" aria-label="返回通话">
        <span>${escapeHTML((companionName || '伴侣').slice(-1))}</span>
        <i></i><small>通话中</small>
      </button>
      <aside class="voice-host-browser hidden" aria-label="通话中的站内浏览">
        <header>
          <button type="button" data-browser-action="back" aria-label="后退">‹</button>
          <input type="url" value="${escapeHTML(ORIGIN + '/')}" aria-label="网页地址">
          <button type="button" data-browser-action="go">前往</button>
          <button type="button" data-browser-action="close" aria-label="关闭浏览">×</button>
        </header>
        <iframe title="站内浏览层" sandbox="allow-forms allow-scripts allow-same-origin allow-popups"></iframe>
      </aside>`;
    document.body.appendChild(overlay);
    state.overlay = overlay;
    state.frame = overlay.querySelector('.voice-host-frame');
    wireOverlay();
    return overlay;
  }

  function wireOverlay() {
    const overlay = state.overlay;
    overlay.addEventListener('click', (event) => {
      const action = event.target.closest('[data-voice-host-action]')?.dataset.voiceHostAction;
      if (action === 'fullscreen') openFullPage();
      if (action === 'retry') loadFrame();
    });
    overlay.querySelector('.voice-host-bubble')?.addEventListener('click', restore);
    wireBubbleDrag(overlay.querySelector('.voice-host-bubble'));
    const browser = overlay.querySelector('.voice-host-browser');
    browser?.addEventListener('click', (event) => {
      const action = event.target.closest('[data-browser-action]')?.dataset.browserAction;
      if (action === 'close') closeBrowser();
      if (action === 'back') {
        try { browser.querySelector('iframe').contentWindow.history.back(); } catch (_) {}
      }
      if (action === 'go') navigateBrowser();
    });
    browser?.querySelector('input')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        navigateBrowser();
      }
    });
  }

  function wireBubbleDrag(bubble) {
    if (!bubble) return;
    let drag = null;
    bubble.addEventListener('pointerdown', (event) => {
      drag = {
        id: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        left: bubble.offsetLeft,
        top: bubble.offsetTop,
        moved: false,
      };
      bubble.setPointerCapture?.(event.pointerId);
    });
    bubble.addEventListener('pointermove', (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 5) drag.moved = true;
      const maxLeft = Math.max(8, global.innerWidth - bubble.offsetWidth - 8);
      const maxTop = Math.max(8, global.innerHeight - bubble.offsetHeight - 8);
      bubble.style.left = `${Math.max(8, Math.min(maxLeft, drag.left + dx))}px`;
      bubble.style.top = `${Math.max(8, Math.min(maxTop, drag.top + dy))}px`;
      bubble.style.right = 'auto';
      bubble.style.bottom = 'auto';
    });
    bubble.addEventListener('pointerup', (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      if (drag.moved) event.preventDefault();
      drag = null;
    });
  }

  function frameUrl(standalone = false) {
    const params = new URLSearchParams({
      call_id: state.callId,
      session_id: state.sessionId,
      embedded: standalone ? '0' : '1',
    });
    return `/voice/call?${params.toString()}`;
  }

  function loadFrame() {
    if (!state.frame) return;
    state.ready = false;
    state.overlay?.querySelector('.voice-host-fallback')?.classList.add('hidden');
    state.frame.src = frameUrl(false);
    global.clearTimeout(state.readyTimer);
    state.readyTimer = global.setTimeout(() => {
      if (!state.ready && state.active) {
        state.overlay?.querySelector('.voice-host-fallback')?.classList.remove('hidden');
      }
    }, 5000);
  }

  function openFullPage() {
    if (!state.active) return;
    state.fullPageHandoff = true;
    global.location.assign(frameUrl(true));
  }

  async function waitForChatIdle(timeoutMs = 150000) {
    const started = Date.now();
    while (bridge?.isBusy?.()) {
      if (Date.now() - started >= timeoutMs) throw new Error('文字聊天仍在处理上一轮');
      await new Promise((resolve) => global.setTimeout(resolve, 250));
    }
  }

  async function handleUserTurn(message) {
    if (!state.active || !bridge?.sendTranscript) return;
    postToFrame('voice:turn-state', { turn_id: message.turn_id, state: 'thinking' });
    try {
      await waitForChatIdle();
      const result = await withTimeout(
        bridge.sendTranscript({
          text: message.text,
          durationMs: message.duration_ms,
          transcriber: message.transcriber,
          acoustic: message.acoustic,
          mood: message.mood,
          privateMode: state.privateMode,
          sleepMode: state.sleepMode,
        }),
        180000,
        '模型回复等待超时',
      );
      let reply = String(result?.text || result?.messageEl?._rawText || '').trim();
      const hangup = /\[call:hangup\]/i.test(reply);
      const important = /^\s*\[important\]/i.test(reply);
      reply = reply
        .replace(/\[call:hangup\]/ig, '')
        .replace(/^\s*\[important\]\s*/i, '')
        .trim();
      postToFrame('voice:assistant-reply', {
        turn_id: message.turn_id,
        text: reply,
        important,
        hangup,
      });
    } catch (error) {
      postToFrame('voice:turn-error', {
        turn_id: message.turn_id,
        error: error.message || '语音回合失败',
      });
    }
  }

  function onFrameMessage(event) {
    if (!state.active || event.origin !== ORIGIN || event.source !== state.frame?.contentWindow) return;
    const message = event.data;
    if (!message || typeof message !== 'object') return;
    switch (message.type) {
      case 'voice:ready':
        state.ready = true;
        global.clearTimeout(state.readyTimer);
        state.overlay?.querySelector('.voice-host-fallback')?.classList.add('hidden');
        postToFrame('voice:host-ready', {
          private_mode: state.privateMode,
          sleep_mode: state.sleepMode,
        });
        break;
      case 'voice:user-turn':
        state.turnQueue = state.turnQueue
          .then(() => handleUserTurn(message))
          .catch((error) => console.error('[VoiceHost] queue', error));
        break;
      case 'voice:minimize':
        minimize();
        break;
      case 'voice:restore':
        restore();
        break;
      case 'voice:end':
        end(message.reason || 'iframe');
        break;
      case 'voice:route':
        setRoute(message.route);
        break;
      case 'voice:mode':
        updateMode(message);
        break;
      case 'voice:browse':
        openBrowser(message.url);
        break;
      case 'voice:clock':
        state.audioClockMs = Math.max(state.audioClockMs, Number(message.audio_clock_ms || 0));
        break;
      case 'voice:mic-failed':
        state.overlay?.querySelector('.voice-host-fallback')?.classList.remove('hidden');
        break;
      default:
        break;
    }
  }

  async function updateMode(message) {
    if ('private_mode' in message) state.privateMode = message.private_mode === true;
    if ('sleep_mode' in message) state.sleepMode = message.sleep_mode === true;
    try {
      await fetchWithTimeout(`/api/voice/calls/${encodeURIComponent(state.callId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          private_mode: state.privateMode,
          sleep_mode: state.sleepMode,
          audio_clock_ms: Math.round(state.audioClockMs),
        }),
      });
    } catch (error) {
      postToFrame('voice:mode-error', { error: error.message });
    }
  }

  function setRoute(route) {
    const clean = route === 'earpiece' ? 'earpiece' : 'speaker';
    callNative('setAudioRoute', clean);
    fetchWithTimeout(`/api/voice/calls/${encodeURIComponent(state.callId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ route: clean }),
    }).catch(() => {});
  }

  function minimize() {
    if (!state.active || !state.overlay) return;
    state.minimized = true;
    state.overlay.classList.add('is-minimized');
    state.overlay.querySelector('.voice-host-bubble')?.classList.remove('hidden');
    postToFrame('voice:visibility', { minimized: true });
  }

  function restore() {
    if (!state.active || !state.overlay) return;
    state.minimized = false;
    closeBrowser();
    state.overlay.classList.remove('is-minimized');
    state.overlay.querySelector('.voice-host-bubble')?.classList.add('hidden');
    postToFrame('voice:visibility', { minimized: false });
  }

  function normalizeBrowserUrl(value) {
    try {
      const parsed = new URL(String(value || '/'), ORIGIN);
      // The Android JavascriptInterface is visible to frames in a WebView.
      // Keep this convenience browser strictly same-origin so untrusted pages
      // never share a renderer with the native voice bridge.
      if (parsed.origin !== ORIGIN) return ORIGIN + '/';
      return parsed.href;
    } catch (_) {
      return ORIGIN + '/';
    }
  }

  function openBrowser(url = '/') {
    if (!state.active || !state.overlay) return;
    minimize();
    state.browserOpen = true;
    const browser = state.overlay.querySelector('.voice-host-browser');
    const target = normalizeBrowserUrl(url);
    browser.classList.remove('hidden');
    browser.querySelector('input').value = target;
    browser.querySelector('iframe').src = target;
  }

  function navigateBrowser() {
    const browser = state.overlay?.querySelector('.voice-host-browser');
    if (!browser) return;
    const target = normalizeBrowserUrl(browser.querySelector('input').value);
    browser.querySelector('input').value = target;
    browser.querySelector('iframe').src = target;
  }

  function closeBrowser() {
    state.browserOpen = false;
    const browser = state.overlay?.querySelector('.voice-host-browser');
    browser?.classList.add('hidden');
    if (browser?.querySelector('iframe')) browser.querySelector('iframe').src = 'about:blank';
  }

  function startHeartbeat() {
    global.clearInterval(state.heartbeatTimer);
    const beat = () => {
      if (!state.active) return;
      fetchWithTimeout(`/api/voice/calls/${encodeURIComponent(state.callId)}/heartbeat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ audio_clock_ms: Math.round(state.audioClockMs) }),
      }).catch(() => {});
      callNative('voiceHeartbeat', state.callId);
    };
    beat();
    state.heartbeatTimer = global.setInterval(beat, 60000);
  }

  function checkCallAfterResume() {
    if (!state.active || !state.callId) return Promise.resolve();
    if (state.resumePromise) return state.resumePromise;
    state.resumePromise = (async () => {
      let shouldReplace = false;
      try {
        const call = await fetchWithTimeout(
          `/api/voice/calls/${encodeURIComponent(state.callId)}`,
          {},
          12000,
        );
        shouldReplace = call.status !== 'active';
        if (!shouldReplace) {
          postToFrame('voice:host-resumed', { hidden_ms: Math.max(0, Date.now() - state.hiddenAt) });
          startHeartbeat();
          bridge?.setStatus?.('语音线路已恢复');
          return;
        }
      } catch (error) {
        // A network outage is not evidence that the call ended. Only a real
        // 404 is replaced; other failures keep the current iframe and retry on
        // the next visibility/online event.
        if (error?.status !== 404) {
          bridge?.setStatus?.('正在等待语音线路恢复…');
          return;
        }
        shouldReplace = true;
      }
      if (!shouldReplace || !state.active) return;
      const oldCallId = state.callId;
      const call = await fetchWithTimeout('/api/voice/calls', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: state.sessionId,
          private_mode: state.privateMode,
          sleep_mode: state.sleepMode,
        }),
      });
      state.callId = call.id;
      state.audioClockMs = Number(call.audio_clock_ms || 0);
      state.turnQueue = Promise.resolve();
      callNative('stopVoiceKeepAlive', oldCallId);
      callNative('startVoiceKeepAlive', state.callId);
      loadFrame();
      startHeartbeat();
      bridge?.setStatus?.('iOS 返回前台后已重建语音线路');
    })().catch((error) => {
      bridge?.setStatus?.(`语音线路恢复失败：${error.message}`);
    }).finally(() => {
      state.resumePromise = null;
    });
    return state.resumePromise;
  }

  async function open() {
    if (state.active) {
      restore();
      return;
    }
    if (!bridge) {
      alert('语音通话桥没有加载，请刷新页面后重试。');
      return;
    }
    requestWakeLock();
    try {
      const voice = await bridge.voiceState();
      if (!voice?.settings?.enabled) throw new Error('请先到“系统 → 语音”开启语音通道');
      state.sessionId = bridge.currentSessionId();
      const call = await fetchWithTimeout('/api/voice/calls', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: state.sessionId,
          private_mode: false,
          sleep_mode: false,
        }),
      });
      state.callId = call.id;
      state.privateMode = false;
      state.sleepMode = false;
      state.audioClockMs = Number(call.audio_clock_ms || 0);
      state.active = true;
      state.fullPageHandoff = false;
      buildOverlay(bridge.companionName());
      state.previousHistoryState = history.state;
      history.pushState({ ...(history.state || {}), daxiguaVoiceCall: true }, '', location.href);
      global.addEventListener('message', onFrameMessage);
      loadFrame();
      startHeartbeat();
      callNative('startVoiceKeepAlive', state.callId);
      bridge.setStatus('语音线路已接通');
    } catch (error) {
      releaseWakeLock();
      alert(`陪伴通话没有启动：${error.message}`);
    }
  }

  async function end(reason = 'user') {
    if (!state.active) return;
    const callId = state.callId;
    state.active = false;
    global.clearTimeout(state.readyTimer);
    global.clearInterval(state.heartbeatTimer);
    postToFrame('voice:ended', { reason });
    callNative('stopVoiceKeepAlive', callId);
    global.removeEventListener('message', onFrameMessage);
    state.overlay?.remove();
    state.overlay = null;
    state.frame = null;
    state.ready = false;
    state.minimized = false;
    state.browserOpen = false;
    state.callId = '';
    state.resumePromise = null;
    releaseWakeLock();
    if (history.state?.daxiguaVoiceCall) {
      history.replaceState(state.previousHistoryState || {}, '', location.href);
    }
    state.previousHistoryState = null;
    bridge?.setStatus?.('');
    try {
      await fetchWithTimeout(`/api/voice/calls/${encodeURIComponent(callId)}`, {
        method: 'DELETE',
        keepalive: true,
      }, 8000);
    } catch (_) {}
  }

  function handleBack() {
    if (!state.active) return false;
    if (state.browserOpen) {
      closeBrowser();
      return true;
    }
    if (!state.minimized) {
      minimize();
      return true;
    }
    restore();
    return true;
  }

  global.addEventListener('popstate', () => {
    if (!state.active) return;
    handleBack();
    history.pushState({ ...(history.state || {}), daxiguaVoiceCall: true }, '', location.href);
  });

  global.addEventListener('pagehide', () => {
    if (!state.active || !state.callId || state.fullPageHandoff) return;
    try {
      navigator.sendBeacon(
        `/api/voice/calls/${encodeURIComponent(state.callId)}/end`,
        new Blob([], { type: 'text/plain' }),
      );
    } catch (_) {}
  });

  document.addEventListener('visibilitychange', () => {
    if (!state.active) return;
    if (document.visibilityState === 'hidden') {
      state.hiddenAt = Date.now();
      postToFrame('voice:host-suspended', {});
      bridge?.setStatus?.('iOS 切到后台时可能暂停麦克风；返回后会自动检查');
      return;
    }
    requestWakeLock();
    checkCallAfterResume();
  });
  global.addEventListener('pageshow', () => {
    if (!state.active) return;
    requestWakeLock();
    checkCallAfterResume();
  });
  global.addEventListener('online', () => {
    if (state.active) checkCallAfterResume();
  });

  global.DaxiguaVoiceHost = Object.freeze({
    open,
    end,
    minimize,
    restore,
    handleBack,
    isActive: () => state.active,
  });
})(window);
