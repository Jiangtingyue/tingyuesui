/** JTYHome v8.9.8 · embedded full-stack Hydrangea interactive-water scene. */
(function () {
  'use strict';
  if (!window.React || !window.ReactDOM) return;

  const root = document.getElementById('hydrangea-water-hero-root');
  if (!root) return;

  const React = window.React;
  const e = React.createElement;
  const html = document.documentElement;
  const body = document.body;
  const originalTitle = document.title;
  const IMAGE_URL = '/static/hydrangea-water-hero/flower-sea.png';
  const FALLBACK_PROFILE = Object.assign({
    name: 'balanced',
    renderer: 'webgl',
    target_fps: 60,
    pointer_interval_ms: 10,
    trail_spacing_px: 6,
    tap_radius: 4.8,
    tap_strength: 0.72,
    ambient_fx: true,
  }, window.JTYHydrangeaVideoRefraction?.DEFAULT_CONFIG || {});

  let profile = Object.assign({}, FALLBACK_PROFILE);
  let backendPayload = null;
  let backendReady = false;
  let mediaReady = false;
  let mediaMissing = false;
  let serverDisabled = false;
  let lastActive = null;
  let profileRequest = null;
  let profileResizeTimer = 0;
  let refraction = null;
  let heroFx = null;

  function requestExit() {
    html.dataset.flowerSea = 'false';
    window.dispatchEvent(new CustomEvent('daxigua:flower-sea-exit'));
    syncHero(true);
  }

  function markMediaMissing() {
    mediaMissing = true;
    mediaReady = false;
    root.dataset.mediaMissing = 'true';
    requestExit();
  }

  function markMediaReady() {
    mediaReady = true;
    mediaMissing = false;
    delete root.dataset.mediaMissing;
    syncHero(true);
  }

  function Hero() {
    return e('main', { className: 'waterHero', 'aria-label': '花海互动水波场景' },
      e('img', {
        className: 'waterSource',
        src: IMAGE_URL,
        alt: '',
        'aria-hidden': 'true',
        onLoad: markMediaReady,
        onError: markMediaMissing,
      }),
      e('canvas', { className: 'glCanvas', 'aria-label': '可用鼠标或手指互动的水面' }),
      e('canvas', { className: 'heroFx', 'aria-hidden': 'true' }),
      e('div', { className: 'waterFallbackRipples', 'aria-hidden': 'true' }),
      e('div', { className: 'waterModeBadge', role: 'status', 'aria-live': 'polite' },
        e('span', { className: 'waterModeName' }, '花海 · 水波'),
        e('small', { className: 'waterModeState' }, '互动水面准备中')
      ),
      e('div', { className: 'waterInteractionHint', 'aria-hidden': 'true' }, '移动鼠标或轻触水面'),
      e('button', {
        className: 'waterExit',
        type: 'button',
        onClick: requestExit,
        'aria-label': '退出水波模式',
      }, '返回')
    );
  }

  window.ReactDOM.render(e(Hero), root);

  const sourceImage = root.querySelector('.waterSource');
  const glCanvas = root.querySelector('.glCanvas');
  const heroFxCanvas = root.querySelector('.heroFx');
  const fallbackLayer = root.querySelector('.waterFallbackRipples');
  const modeState = root.querySelector('.waterModeState');
  const fallbackPointers = new Map();
  let fallbackLastRippleAt = 0;

  function qualityLabel(name) {
    if (name === 'high') return '精细';
    if (name === 'eco') return '节能';
    return '均衡';
  }

  function updateBadge(renderer, detail) {
    if (!modeState) return;
    if (renderer === 'image') {
      modeState.textContent = backendReady ? '兼容水纹 · 本机后端已就绪' : '兼容水纹 · 本地档';
      return;
    }
    const fps = Number(detail?.target_fps || profile.target_fps || 60);
    const suffix = backendReady ? '本机后端已就绪' : '本地档';
    modeState.textContent = `${qualityLabel(profile.name)} · WebGL ${fps} FPS · ${suffix}`;
  }

  function setRenderer(renderer, detail) {
    const next = renderer === 'webgl' ? 'webgl' : 'image';
    root.dataset.renderer = next;
    updateBadge(next, detail || {});
    window.dispatchEvent(new CustomEvent('daxigua:water-renderer-change', {
      detail: Object.assign({ renderer: next, quality: profile.name }, detail || {}),
    }));
  }

  function onRendererFailure(error) {
    setRenderer('image', { reason: String(error?.message || 'webgl-unavailable') });
  }

  refraction = window.JTYHydrangeaVideoRefraction?.initVideoRefraction?.({
    canvas: glCanvas,
    image: sourceImage,
    config: profile,
    isActive: () => root.dataset.active === 'true' && root.dataset.renderer === 'webgl',
    onFailure: onRendererFailure,
    onRendererChange: setRenderer,
  }) || null;

  heroFx = window.JTYHydrangeaHeroFx?.initHeroFx?.({
    canvas: heroFxCanvas,
    gradAt: (x, y) => refraction?.gradAt?.(x, y) || { x: 0, y: 0 },
    isActive: () => root.dataset.active === 'true',
    ambientEnabled: profile.ambient_fx !== false,
  }) || null;

  function clientCapabilities() {
    const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    const pointer = window.matchMedia?.('(pointer: coarse)').matches
      ? 'coarse'
      : (window.matchMedia?.('(pointer: fine)').matches ? 'fine' : 'none');
    return {
      viewport_width: Math.max(1, window.innerWidth || 1),
      viewport_height: Math.max(1, window.innerHeight || 1),
      device_pixel_ratio: Math.min(4, Math.max(1, window.devicePixelRatio || 1)),
      hardware_concurrency: Number(navigator.hardwareConcurrency || 4),
      device_memory: Number(navigator.deviceMemory || 4),
      pointer: pointer,
      reduced_motion: Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches),
      save_data: Boolean(connection?.saveData),
      webgl: Boolean(window.WebGLRenderingContext),
    };
  }

  function applyBackendPayload(payload) {
    if (!payload || typeof payload !== 'object') throw new Error('水波后端响应无效');
    backendPayload = payload;
    serverDisabled = payload.enabled === false;
    backendReady = payload.ok === true;
    root.dataset.backendReady = String(backendReady);
    root.dataset.integration = String(payload.integration || 'embedded-same-origin');
    if (payload.profile && typeof payload.profile === 'object') {
      profile = Object.assign({}, profile, payload.profile);
      root.dataset.quality = String(profile.name || 'balanced');
      refraction?.applyConfig?.(profile);
      heroFx?.applyConfig?.(profile);
    }
    if (serverDisabled) {
      requestExit();
      return payload;
    }
    updateBadge(root.dataset.renderer || 'image', refraction?.getStatus?.() || {});
    if (root.dataset.active === 'true') syncHero(true);
    return payload;
  }

  async function loadBackendProfile(force) {
    if (profileRequest) return profileRequest;
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timeout = controller ? window.setTimeout(() => controller.abort(), 2600) : 0;
    profileRequest = fetch('/api/water-scene/bootstrap', {
      method: 'POST',
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(clientCapabilities()),
      signal: controller?.signal,
    })
      .then(async (response) => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || payload.error || `HTTP ${response.status}`);
        return applyBackendPayload(payload);
      })
      .catch(() => {
        backendReady = false;
        root.dataset.backendReady = 'false';
        updateBadge(root.dataset.renderer || 'image', refraction?.getStatus?.() || {});
        return null;
      })
      .finally(() => {
        if (timeout) window.clearTimeout(timeout);
        profileRequest = null;
      });
    return profileRequest;
  }

  function scheduleProfileReload() {
    if (profileResizeTimer) window.clearTimeout(profileResizeTimer);
    profileResizeTimer = window.setTimeout(() => {
      profileResizeTimer = 0;
      loadBackendProfile(true);
    }, 360);
  }

  function shouldShowHero() {
    const activeView = html.dataset.activeView || 'home';
    return html.dataset.flowerSea === 'true'
      && activeView === 'home'
      && html.dataset.uiMode !== 'reader'
      && mediaReady
      && !mediaMissing
      && !serverDisabled;
  }

  function syncHero(force) {
    const requested = html.dataset.flowerSea === 'true';
    let active = shouldShowHero();
    if (requested && !mediaReady && !mediaMissing) active = false;
    if (!force && active === lastActive) return;
    lastActive = active;

    root.dataset.active = active ? 'true' : 'false';
    if (active) {
      const preferImage = profile.renderer === 'image' || !refraction;
      if (preferImage) {
        refraction?.stop?.();
        setRenderer('image', { reason: 'negotiated-fallback' });
      } else {
        root.dataset.renderer = 'webgl';
        const started = refraction.start?.();
        if (started === false) setRenderer('image', { reason: 'webgl-start-failed' });
      }
      if (profile.ambient_fx !== false) heroFx?.start?.();
    } else {
      refraction?.stop?.();
      heroFx?.stop?.();
      fallbackPointers.clear();
      fallbackLayer?.replaceChildren();
    }
    body.classList.toggle('flower-sea-active', active);
    document.title = active ? '大西瓜 · 花海水波' : originalTitle;
  }

  function addFallbackRipple(x, y, intensity) {
    if (!fallbackLayer || root.dataset.renderer !== 'image' || root.dataset.active !== 'true') return;
    const bounds = root.getBoundingClientRect();
    const localX = x - bounds.left;
    const localY = y - bounds.top;
    if (localX < 0 || localY < 0 || localX > bounds.width || localY > bounds.height) return;
    const ring = document.createElement('i');
    const strength = Math.max(0.55, Math.min(1.4, Number(intensity || 1)));
    ring.className = 'waterFallbackRipple';
    ring.style.left = `${localX}px`;
    ring.style.top = `${localY}px`;
    ring.style.setProperty('--water-ring-scale', (strength * 5.5).toFixed(2));
    ring.addEventListener('animationend', () => ring.remove(), { once: true });
    fallbackLayer.appendChild(ring);
    while (fallbackLayer.childElementCount > 14) fallbackLayer.firstElementChild?.remove();
  }

  function fallbackPointerDown(event) {
    if (event.target.closest?.('.waterExit')) return;
    if (root.dataset.renderer !== 'image' || root.dataset.active !== 'true') return;
    const point = { x: event.clientX, y: event.clientY, time: performance.now() };
    fallbackPointers.set(event.pointerId, point);
    addFallbackRipple(point.x, point.y, 1.2);
  }

  function fallbackPointerMove(event) {
    if (root.dataset.renderer !== 'image' || root.dataset.active !== 'true') return;
    const isHover = (event.pointerType || 'mouse') === 'mouse';
    const previous = fallbackPointers.get(event.pointerId);
    if (!previous && !isHover) return;
    const now = performance.now();
    const point = { x: event.clientX, y: event.clientY, time: now };
    if (!previous) {
      fallbackPointers.set(event.pointerId, point);
      return;
    }
    const distance = Math.hypot(point.x - previous.x, point.y - previous.y);
    const interval = Math.max(18, Number(profile.pointer_interval_ms || 10) * 1.8);
    const spacing = Math.max(12, Number(profile.trail_spacing_px || 6) * 2.2);
    if (now - fallbackLastRippleAt >= interval && distance >= spacing) {
      fallbackLastRippleAt = now;
      addFallbackRipple(point.x, point.y, Math.min(1.2, 0.72 + distance / 90));
      fallbackPointers.set(event.pointerId, point);
    }
  }

  function fallbackPointerEnd(event) {
    fallbackPointers.delete(event.pointerId);
  }

  root.addEventListener('pointerdown', fallbackPointerDown, { passive: true });
  root.addEventListener('pointermove', fallbackPointerMove, { passive: true });
  root.addEventListener('pointerup', fallbackPointerEnd, { passive: true });
  root.addEventListener('pointercancel', fallbackPointerEnd, { passive: true });
  root.addEventListener('pointerleave', fallbackPointerEnd, { passive: true });

  const observer = new MutationObserver(() => syncHero(false));
  observer.observe(html, {
    attributes: true,
    attributeFilter: ['data-flower-sea', 'data-active-view', 'data-ui-mode'],
  });
  window.addEventListener('daxigua:flower-sea-change', () => syncHero(true));
  window.addEventListener('resize', scheduleProfileReload, { passive: true });
  window.addEventListener('pageshow', () => syncHero(true));
  window.addEventListener('pagehide', () => {
    refraction?.stop?.();
    heroFx?.stop?.();
    lastActive = null;
  });

  root.dataset.renderer = 'image';
  root.dataset.backendReady = 'false';
  root.dataset.quality = profile.name;
  loadBackendProfile(false);
  syncHero(true);

  window.JTYWaterScene = Object.freeze({
    exit: requestExit,
    reloadProfile: () => loadBackendProfile(true),
    addRipple: (x, y, strength) => {
      if (root.dataset.renderer === 'webgl') {
        refraction?.drop?.(x, y, profile.tap_radius, Number(strength || profile.tap_strength));
      } else {
        addFallbackRipple(x, y, strength);
      }
    },
    getStatus: () => ({
      active: root.dataset.active === 'true',
      backend_ready: backendReady,
      renderer: root.dataset.renderer || 'image',
      quality: profile.name,
      profile: Object.assign({}, profile),
      backend: backendPayload ? {
        ok: backendPayload.ok,
        version: backendPayload.version,
        integration: backendPayload.integration,
      } : null,
      refraction: refraction?.getStatus?.() || null,
    }),
  });
})();
