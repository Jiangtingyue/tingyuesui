/**
 * 大西瓜 · 环境型天气状态机
 *
 * 天气首先改变街景的光照、色温、云雾、玻璃湿润和路面反射；
 * 全视口湿窗读取同一组环境参数。这里不创建可见降水粒子层，
 * 不生成粒子，也不接触聊天、模型或任何用户内容。
 */
(function () {
  'use strict';

  // app.js exposes a storage facade that survives Safari storage failures.
  // Keep a defensive fallback so this module also remains safe in isolation.
  const localStorage = window.DaxiguaStorage || Object.freeze({
    getItem(key) {
      try { return window.localStorage.getItem(key); } catch (_) { return null; }
    },
    setItem(key, value) {
      try { window.localStorage.setItem(key, value); } catch (_) { /* optional preference */ }
    },
    removeItem(key) {
      try { window.localStorage.removeItem(key); } catch (_) { /* optional preference */ }
    },
  });

  const STORAGE_KEY = 'daxigua:v74-weather';
  const LEGACY_KEYS = ['daxigua:v73-weather', 'daxigua:v71-weather'];
  const LAST_ENVIRONMENT_KEY = 'daxigua:v74-last-weather';
  const SOUND_KEY = 'daxigua:v74-weather-sound';
  const BLUR_STORAGE_KEYS = Object.freeze({
    clear: 'daxigua:v791-weather-blur-clear',
    rain: 'daxigua:v791-weather-blur-rain',
  });
  const BLUR_DEFAULTS = Object.freeze({ clear: 18, rain: 24 });
  const STATES = Object.freeze([
    'clear',
    'rain',
  ]);
  const STATE_SET = new Set(STATES);
  const LABELS = Object.freeze({
    clear: '晴天',
    rain: '雨天',
  });

  // 运行时只保留晴天与雨天；水波由花海折射场景接管。
  const TRANSITIONS = Object.freeze({
    clear: Object.freeze(['rain']),
    rain: Object.freeze(['clear']),
  });

  // 晴天与雨天共享同一日间基底，天气参数只负责连续环境变化。
  const PRESETS = Object.freeze({
    clear: Object.freeze({
      exposure: 1.04,
      temperature: .12,
      contrast: 1.02,
      saturation: 1.04,
      cloudCover: .04,
      cloudDepth: .06,
      cloudSpeed: .06,
      fog: .02,
      lowFog: .01,
      glassWetness: 0,
      glassFlow: 0,
      condensation: 0,
      reflection: .18,
      precipitation: 0,
      wind: .04,
      lightning: 0,
      soundRain: 0,
      soundWind: .015,
      soundLow: 0,
    }),
    rain: Object.freeze({
      // v7.8.4 light rain: keep the street/hydrangea scene crisp. Rain is a
      // sparse decorative layer on top of the glass, never a fog/filter over
      // the source image. Glass may reflect the environment; drops do not.
      exposure: .96,
      temperature: -.05,
      contrast: 1.04,
      saturation: 1.02,
      cloudCover: 0,
      cloudDepth: 0,
      cloudSpeed: .08,
      fog: 0,
      lowFog: 0,
      glassWetness: .68,
      glassFlow: .16,
      condensation: 0,
      reflection: .20,
      precipitation: .30,
      wind: .08,
      lightning: 0,
      soundRain: .22,
      soundWind: .045,
      soundLow: .01,
    }),
  });

  const root = document.documentElement;
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const PARAMETER_NAMES = Object.keys(PRESETS.clear);
  let currentState = normalize(root.dataset.weather);
  let targetState = currentState;
  let currentParams = { ...PRESETS[currentState] };
  let pathQueue = [];
  let transition = null;
  let transitionFrame = 0;
  let transitionLastAppliedAt = 0;
  let phase = 'stable';
  let button = null;
  let clearButton = null;
  let select = null;
  let stateLabel = null;
  let phaseNote = null;
  let soundControl = null;
  let soundNote = null;
  let homeWeatherKicker = null;
  const blurRanges = { clear: null, rain: null };
  const blurOutputs = { clear: null, rain: null };
  let soundEnabled = localStorage.getItem(SOUND_KEY) === 'true';
  let lastEnvironment = normalize(
    localStorage.getItem(LAST_ENVIRONMENT_KEY) || 'rain'
  );
  if (lastEnvironment === 'clear') lastEnvironment = 'rain';

  function blurBucket(state = targetState) {
    return normalize(state) === 'clear' ? 'clear' : 'rain';
  }

  function normalizeBlurPercent(value, fallback = 1) {
    const parsed = Number.parseFloat(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.round(Math.max(1, Math.min(100, parsed)));
  }

  function readBlurPercent(bucket) {
    return normalizeBlurPercent(
      localStorage.getItem(BLUR_STORAGE_KEYS[bucket]),
      BLUR_DEFAULTS[bucket]
    );
  }

  const blurValues = {
    clear: readBlurPercent('clear'),
    rain: readBlurPercent('rain'),
  };

  function normalize(value) {
    return STATE_SET.has(value) ? value : 'clear';
  }

  function clamp(value, min = 0, max = 1) {
    return Math.max(min, Math.min(max, Number(value) || 0));
  }

  function mix(from, to, amount) {
    return from + (to - from) * amount;
  }

  function smoothstep(edge0, edge1, value) {
    const amount = clamp((value - edge0) / Math.max(.0001, edge1 - edge0));
    return amount * amount * (3 - 2 * amount);
  }

  function eased(amount) {
    const value = clamp(amount);
    // Smootherstep keeps both velocity and acceleration continuous at the
    // endpoints, so light, fog and wetness settle without a visible snap.
    return value * value * value * (value * (value * 6 - 15) + 10);
  }

  function interpolate(from, to, amount) {
    const result = {};
    for (const name of PARAMETER_NAMES) {
      result[name] = mix(from[name], to[name], amount);
    }
    return result;
  }

  function shortestPath(from, to) {
    if (from === to) return [from];
    const queue = [[from]];
    const visited = new Set([from]);
    while (queue.length) {
      const path = queue.shift();
      const tail = path[path.length - 1];
      for (const next of TRANSITIONS[tail]) {
        if (visited.has(next)) continue;
        const candidate = [...path, next];
        if (next === to) return candidate;
        visited.add(next);
        queue.push(candidate);
      }
    }
    return [from, to];
  }

  function setCssNumber(name, value) {
    root.style.setProperty(name, Number(value).toFixed(4));
  }

  function blurPixels(percent) {
    // 低段刻意拉长，1–35% 都是细微可读性调整；只有用户主动把
    // 滑杆推到高段时才会出现明显景深，避免再次擅自重度模糊。
    const ratio = (normalizeBlurPercent(percent) - 1) / 99;
    return Math.pow(ratio, 1.35) * 16;
  }

  function updateBlurControls() {
    for (const bucket of ['clear', 'rain']) {
      const percent = blurValues[bucket];
      const range = blurRanges[bucket];
      if (range && document.activeElement !== range) {
        range.value = String(percent);
      }
      if (blurOutputs[bucket]) blurOutputs[bucket].value = `${percent}%`;
    }
  }

  function applySceneBlur(state = targetState, emit = true) {
    const bucket = blurBucket(state);
    const percent = blurValues[bucket];
    const pixels = blurPixels(percent);
    root.dataset.weatherBlur = String(percent);
    root.dataset.weatherBlurMode = bucket;
    root.style.setProperty('--weather-scene-blur-px', `${pixels.toFixed(2)}px`);
    root.style.setProperty(
      '--weather-scene-blur-scale',
      (1.002 + pixels / 400).toFixed(4)
    );
    updateBlurControls();
    if (emit) {
      window.dispatchEvent(new CustomEvent('daxigua:weather-blur-change', {
        detail: { mode: bucket, percent, pixels },
      }));
    }
  }

  function setBlurPercent(value, options = {}) {
    const bucket = blurBucket(options.state || targetState);
    const percent = normalizeBlurPercent(value, blurValues[bucket]);
    blurValues[bucket] = percent;
    if (options.persist !== false) {
      localStorage.setItem(BLUR_STORAGE_KEYS[bucket], String(percent));
    }
    updateBlurControls();
    if (bucket === blurBucket(targetState)) applySceneBlur(targetState);
    return percent;
  }

  function mixedRgb(from, to, amount) {
    const channels = from.map((value, index) => (
      Math.round(mix(value, to[index], clamp(amount)))
    ));
    return `rgb(${channels.join(' ')})`;
  }

  function updateThemeColor() {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) return;
    if (currentParams.exposure < .58) meta.content = '#344a53';
    else if (currentParams.exposure < .8) meta.content = '#587680';
    else meta.content = '#6fa9aa';
  }

  function applyParams(params, emit = true) {
    currentParams = { ...params };
    const darkness = clamp(1 - params.exposure, 0, .72);
    const lift = clamp(params.exposure - 1, 0, .18);
    const textLift = clamp((.8 - params.exposure) / .33);
    setCssNumber('--weather-exposure', params.exposure);
    setCssNumber('--weather-darkness', darkness);
    setCssNumber('--weather-lift', lift);
    setCssNumber('--weather-cool', clamp(-params.temperature));
    setCssNumber('--weather-warm', clamp(params.temperature));
    setCssNumber('--weather-contrast', params.contrast);
    setCssNumber('--weather-contrast-loss', clamp(1 - params.contrast));
    setCssNumber('--weather-saturation', params.saturation);
    root.style.setProperty(
      '--weather-home-ink',
      mixedRgb([23, 63, 74], [229, 244, 242], textLift)
    );
    root.style.setProperty(
      '--weather-home-ink-secondary',
      mixedRgb([35, 78, 88], [202, 226, 223], textLift)
    );
    root.style.setProperty(
      '--weather-home-muted',
      mixedRgb([95, 126, 132], [170, 204, 201], textLift)
    );
    setCssNumber('--weather-cloud-cover', params.cloudCover);
    setCssNumber('--weather-cloud-depth', params.cloudDepth);
    setCssNumber('--weather-cloud-speed', params.cloudSpeed);
    root.style.setProperty(
      '--weather-cloud-duration',
      `${mix(118, 48, clamp(params.cloudSpeed)).toFixed(2)}s`
    );
    root.style.setProperty(
      '--weather-cloud-duration-far',
      `${mix(154, 67, clamp(params.cloudSpeed)).toFixed(2)}s`
    );
    setCssNumber('--weather-fog', params.fog);
    setCssNumber('--weather-low-fog', params.lowFog);
    setCssNumber('--weather-glass-wetness', params.glassWetness);
    setCssNumber('--weather-glass-flow', params.glassFlow);
    setCssNumber('--weather-condensation', params.condensation);
    setCssNumber('--weather-reflection', params.reflection);
    setCssNumber('--weather-precipitation', params.precipitation);
    // 背景与湿窗使用同一个连续权重，晴景不会在切换开始时被雨景硬替换。
    // 小雨也完整呈现紫阳花场景；雨滴密度仍由 precipitation 独立控制。
    const rainSceneMix = smoothstep(.06, .64, params.glassWetness)
      * smoothstep(.015, .18, params.precipitation);
    setCssNumber('--weather-rain-scene-mix', rainSceneMix);
    // 湿窗稍晚于场景换景出现，避免两个雨景层叠加后把中段吞成硬切。
    setCssNumber(
      '--weather-rain-canvas-mix',
      smoothstep(.45, .96, rainSceneMix)
    );
    setCssNumber('--weather-wind', params.wind);
    root.style.setProperty(
      '--weather-fog-duration',
      `${mix(42, 18, clamp(params.wind)).toFixed(2)}s`
    );
    setCssNumber('--weather-lightning', params.lightning);
    // data-weather 只描述当前已经走到的视觉状态；目标状态由控件单独显示。
    // 提前写入最终目标会让雨景专属 CSS 在第一帧就整套跳进来。
    root.dataset.weather = phase === 'stable' ? targetState : currentState;
    root.dataset.weatherState = currentState;
    root.dataset.weatherPhase = phase;
    updateThemeColor();
    soundscape.apply(params);
    updateControls();
    if (emit) {
      window.dispatchEvent(new CustomEvent('daxigua:weather-frame', {
        detail: {
          state: currentState,
          target: targetState,
          phase,
          params: { ...currentParams },
        },
      }));
    }
  }

  function transitionDuration(fromParams, toParams) {
    const visualDistance = (
      Math.abs(fromParams.exposure - toParams.exposure) * 1.2
      + Math.abs(fromParams.cloudCover - toParams.cloudCover)
      + Math.abs(fromParams.fog - toParams.fog)
      + Math.abs(fromParams.glassWetness - toParams.glassWetness)
    );
    return Math.round(940 + Math.min(1.2, visualDistance) * 620);
  }

  function finishAt(state, emit = true) {
    if (transitionFrame) cancelAnimationFrame(transitionFrame);
    transitionFrame = 0;
    transition = null;
    transitionLastAppliedAt = 0;
    pathQueue = [];
    currentState = state;
    targetState = state;
    phase = 'stable';
    applyParams(PRESETS[state], emit);
    if (emit) {
      window.dispatchEvent(new CustomEvent('daxigua:weather-change', {
        detail: { mode: state, state, params: { ...currentParams } },
      }));
    }
  }

  function startNextLeg(now = performance.now()) {
    if (!pathQueue.length) {
      phase = 'stable';
      currentState = targetState;
      applyParams(PRESETS[currentState]);
      window.dispatchEvent(new CustomEvent('daxigua:weather-change', {
        detail: {
          mode: currentState,
          state: currentState,
          params: { ...currentParams },
        },
      }));
      return;
    }
    const nextState = pathQueue.shift();
    transition = {
      fromParams: { ...currentParams },
      toState: nextState,
      toParams: PRESETS[nextState],
      startedAt: now,
      duration: transitionDuration(currentParams, PRESETS[nextState]),
    };
    phase = 'transitioning';
    transitionLastAppliedAt = 0;
    root.dataset.weatherPhase = phase;
    transitionFrame = requestAnimationFrame(tickTransition);
  }

  function preferredTransitionFps() {
    const area = window.innerWidth * window.innerHeight;
    if (window.innerWidth <= 768) return 30;
    if (area >= 5_000_000) return 30;
    if (area >= 3_000_000) return 36;
    return 45;
  }

  function tickTransition(now) {
    transitionFrame = 0;
    if (!transition) return;
    const progress = clamp(
      (now - transition.startedAt) / Math.max(1, transition.duration)
    );
    const frameMs = 1000 / preferredTransitionFps();
    const shouldApply = progress >= 1
      || !transitionLastAppliedAt
      || now - transitionLastAppliedAt >= frameMs;
    if (shouldApply) {
      transitionLastAppliedAt = now;
      applyParams(interpolate(
        transition.fromParams,
        transition.toParams,
        eased(progress)
      ));
    }
    if (progress >= 1) {
      currentState = transition.toState;
      transition = null;
      startNextLeg(now);
      return;
    }
    transitionFrame = requestAnimationFrame(tickTransition);
  }

  function setMode(next, options = {}) {
    const wanted = normalize(next);
    if (root.dataset.flowerSea === 'true') {
      window.dispatchEvent(new CustomEvent('daxigua:flower-sea-exit'));
    }
    const persist = options.persist !== false;
    const immediate = Boolean(options.immediate)
      || reduceMotion.matches
      || document.hidden;
    if (wanted !== 'clear') {
      lastEnvironment = wanted;
      localStorage.setItem(LAST_ENVIRONMENT_KEY, wanted);
    }
    if (persist) {
      localStorage.setItem(STORAGE_KEY, wanted);
      // 旧版键只保留它能识别的雨天；其他退役状态回退为 clear。
      for (const key of LEGACY_KEYS) {
        localStorage.setItem(
          key,
          wanted === 'rain' ? 'rain' : 'clear'
        );
      }
    }
    targetState = wanted;
    applySceneBlur(wanted);
    if (transitionFrame) cancelAnimationFrame(transitionFrame);
    transitionFrame = 0;
    transition = null;
    pathQueue = [];
    if (immediate || (wanted === currentState && phase === 'stable')) {
      finishAt(wanted);
      return;
    }
    pathQueue = wanted === currentState
      ? [wanted]
      : shortestPath(currentState, wanted).slice(1);
    startNextLeg();
  }

  function toggle() {
    soundscape.unlockIfEnabled();
    setMode(targetState === 'clear' ? 'rain' : 'clear');
  }

  class AmbientSoundscape {
    constructor() {
      this.context = null;
      this.master = null;
      this.rainGain = null;
      this.windGain = null;
      this.lowGain = null;
      this.ready = false;
    }

    makeNoiseBuffer(context, seconds, seed) {
      const length = Math.max(1, Math.floor(context.sampleRate * seconds));
      const buffer = context.createBuffer(1, length, context.sampleRate);
      const data = buffer.getChannelData(0);
      let value = seed >>> 0;
      let brown = 0;
      for (let index = 0; index < length; index += 1) {
        value = (value * 1664525 + 1013904223) >>> 0;
        const white = (value / 4294967295) * 2 - 1;
        brown = brown * .97 + white * .03;
        data[index] = white * .72 + brown * .8;
      }
      return buffer;
    }

    source(context, buffer) {
      const node = context.createBufferSource();
      node.buffer = buffer;
      node.loop = true;
      return node;
    }

    ensure() {
      if (this.ready) return true;
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!AudioContext) return false;
      const context = new AudioContext({ latencyHint: 'playback' });
      const rainSource = this.source(
        context,
        this.makeNoiseBuffer(context, 5, 0x73a41d)
      );
      const windSource = this.source(
        context,
        this.makeNoiseBuffer(context, 7, 0x14ce9b)
      );
      const rainHigh = context.createBiquadFilter();
      rainHigh.type = 'highpass';
      rainHigh.frequency.value = 1250;
      const rainLow = context.createBiquadFilter();
      rainLow.type = 'lowpass';
      rainLow.frequency.value = 6400;
      const windLow = context.createBiquadFilter();
      windLow.type = 'lowpass';
      windLow.frequency.value = 520;
      windLow.Q.value = .3;
      const lowFilter = context.createBiquadFilter();
      lowFilter.type = 'lowpass';
      lowFilter.frequency.value = 105;
      const rainGain = context.createGain();
      const windGain = context.createGain();
      const lowGain = context.createGain();
      const master = context.createGain();
      rainGain.gain.value = 0;
      windGain.gain.value = 0;
      lowGain.gain.value = 0;
      master.gain.value = 0;
      rainSource.connect(rainHigh).connect(rainLow).connect(rainGain);
      windSource.connect(windLow).connect(windGain);
      windSource.connect(lowFilter).connect(lowGain);
      rainGain.connect(master);
      windGain.connect(master);
      lowGain.connect(master);
      master.connect(context.destination);
      rainSource.start();
      windSource.start();
      this.context = context;
      this.master = master;
      this.rainGain = rainGain;
      this.windGain = windGain;
      this.lowGain = lowGain;
      this.ready = true;
      root.dataset.weatherSound = 'ready';
      return true;
    }

    async unlockIfEnabled() {
      if (!soundEnabled || !this.ensure()) return;
      try {
        await this.context.resume();
        this.apply(currentParams);
        updateControls();
      } catch (_error) {
        root.dataset.weatherSound = 'blocked';
        updateControls();
      }
    }

    apply(params) {
      if (!this.ready) return;
      const now = this.context.currentTime;
      const audible = soundEnabled && !document.hidden ? 1 : 0;
      this.master.gain.setTargetAtTime(audible * .34, now, .5);
      this.rainGain.gain.setTargetAtTime(params.soundRain * .42, now, .65);
      this.windGain.gain.setTargetAtTime(params.soundWind * .24, now, .9);
      this.lowGain.gain.setTargetAtTime(params.soundLow * .32, now, 1.3);
    }

    suspend() {
      if (!this.ready) return;
      this.master.gain.setTargetAtTime(0, this.context.currentTime, .12);
    }
  }

  const soundscape = new AmbientSoundscape();

  function setSoundEnabled(enabled, fromUser = false) {
    soundEnabled = Boolean(enabled);
    localStorage.setItem(SOUND_KEY, String(soundEnabled));
    root.dataset.weatherSound = soundEnabled ? 'armed' : 'off';
    if (fromUser && soundEnabled) soundscape.unlockIfEnabled();
    if (!soundEnabled) soundscape.suspend();
    soundscape.apply(currentParams);
    updateControls();
    window.dispatchEvent(new CustomEvent('daxigua:weather-sound', {
      detail: { enabled: soundEnabled },
    }));
  }

  function updateControls() {
    if (select && select.value !== targetState) select.value = targetState;
    const waterActive = root.dataset.flowerSea === 'true';
    if (button) {
      const active = !waterActive && targetState === 'rain';
      button.dataset.weather = targetState;
      button.setAttribute('aria-pressed', String(active));
      button.setAttribute('aria-checked', String(active));
      button.classList.toggle('active', active);
      button.title = active ? '当前为雨天' : '切换到雨天';
      button.setAttribute('aria-label', button.title);
    }
    if (clearButton) {
      const active = !waterActive && targetState === 'clear';
      clearButton.setAttribute('aria-pressed', String(active));
      clearButton.setAttribute('aria-checked', String(active));
      clearButton.classList.toggle('active', active);
      clearButton.title = active ? '当前为晴天' : '切换到晴天';
      clearButton.setAttribute('aria-label', clearButton.title);
    }
    if (stateLabel) stateLabel.textContent = LABELS[targetState];
    if (phaseNote) {
      phaseNote.textContent = phase === 'transitioning'
        ? `环境正在沿状态路径过渡至${LABELS[targetState]}`
        : '光照、色温、雾、云、反射与玻璃湿润使用同一状态参数。';
    }
    if (soundControl) soundControl.checked = soundEnabled;
    if (soundNote) {
      soundNote.textContent = soundEnabled
        ? (soundscape.ready ? '环境声已跟随天气。' : '环境声已开启；首次操作天气后开始播放。')
        : '环境声已静音。';
    }
    if (homeWeatherKicker) {
      const appVersion = document.documentElement.dataset.appVersion || '8.2';
      homeWeatherKicker.textContent = `大西瓜 ${appVersion} · ${LABELS[targetState]}环境回家系统`;
    }
    updateBlurControls();
  }

  function bindControls() {
    button = document.getElementById('btn-weather');
    clearButton = document.getElementById('btn-weather-clear');
    select = document.getElementById('weather-mode');
    stateLabel = document.getElementById('weather-state-label');
    phaseNote = document.getElementById('weather-support-note');
    soundControl = document.getElementById('weather-sound');
    soundNote = document.getElementById('weather-sound-note');
    homeWeatherKicker = document.getElementById('home-weather-kicker');
    blurRanges.clear = document.getElementById('weather-blur-clear-range');
    blurRanges.rain = document.getElementById('weather-blur-rain-range');
    blurOutputs.clear = document.getElementById('weather-blur-clear-value');
    blurOutputs.rain = document.getElementById('weather-blur-rain-value');
    button?.addEventListener('click', () => {
      soundscape.unlockIfEnabled();
      setMode('rain');
    });
    clearButton?.addEventListener('click', () => {
      soundscape.unlockIfEnabled();
      setMode('clear');
    });
    for (const bucket of ['clear', 'rain']) {
      blurRanges[bucket]?.addEventListener('input', () => {
        setBlurPercent(blurRanges[bucket].value, { state: bucket });
      });
    }
    select?.addEventListener('change', () => {
      soundscape.unlockIfEnabled();
      setMode(select.value);
    });
    soundControl?.addEventListener('change', () => {
      setSoundEnabled(soundControl.checked, true);
    });
    reduceMotion.addEventListener?.('change', () => {
      if (reduceMotion.matches && phase === 'transitioning') {
        finishAt(targetState);
      }
    });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) soundscape.suspend();
      else soundscape.apply(currentParams);
    });
    window.addEventListener('daxigua:flower-sea-change', updateControls);
    window.addEventListener('daxigua:flower-sea-exit', updateControls);
    updateControls();
  }

  function storedMode() {
    const direct = localStorage.getItem(STORAGE_KEY);
    if (STATE_SET.has(direct)) return direct;
    for (const key of LEGACY_KEYS) {
      const legacy = localStorage.getItem(key);
      if (STATE_SET.has(legacy)) return legacy;
    }
    return normalize(root.dataset.weather);
  }

  // 每次打开都从绿色晴天街景开始；雨天只由本次页面中的按钮触发。
  const initial = 'clear';
  currentState = initial;
  targetState = initial;
  phase = 'stable';
  applyParams(PRESETS[initial], false);
  applySceneBlur(initial, false);
  root.dataset.weatherSound = soundEnabled ? 'armed' : 'off';

  window.DaxiguaWeather = Object.freeze({
    STATES,
    LABELS,
    PRESETS,
    TRANSITIONS,
    get mode() { return targetState; },
    get state() { return currentState; },
    get target() { return targetState; },
    get phase() { return phase; },
    get params() { return { ...currentParams }; },
    get soundEnabled() { return soundEnabled; },
    get blurPercent() { return blurValues[blurBucket(targetState)]; },
    get blurPixels() { return blurPixels(blurValues[blurBucket(targetState)]); },
    get engine() {
      return root.dataset.weatherGlass === 'webgl'
        ? 'dom-environment+webgl-glass'
        : 'dom-environment+css-glass';
    },
    setMode,
    toggle,
    setSoundEnabled,
    setBlurPercent,
    shortestPath,
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bindControls, { once: true });
  } else bindControls();
})();
