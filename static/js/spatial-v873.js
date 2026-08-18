/**
 * JTYHome 8.7.3 — one-room spatial presentation.
 *
 * This file does not replace routes, data containers or business handlers.
 * It only mounts spatial affordances, moves the existing Home sections into a
 * reversible Life layer, assigns glass material roles and coordinates subtle
 * environment feedback.
 */
(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const root = document.documentElement;
  let emotionIntensity = 0;
  let messageObserver = null;
  let bubbleIntersection = null;

  const viewMotion = {
    home: 'home',
    chat: 'chat',
    memory: 'memory',
    inner: 'inner',
    us: 'us',
    system: 'system',
  };

  function requestGlassRefresh() {
    window.dispatchEvent(new CustomEvent('daxigua:glass-targets-change'));
  }

  function addFoyerTypography() {
    const wordmark = $('#view-home .home-wordmark');
    if (!wordmark || wordmark.querySelector('.jty-foyer-fashion')) return;
    const fashion = document.createElement('span');
    fashion.className = 'jty-foyer-fashion';
    fashion.setAttribute('aria-hidden', 'true');
    fashion.textContent = 'HOME';
    const ting = document.createElement('span');
    ting.className = 'jty-foyer-ting';
    ting.setAttribute('aria-hidden', 'true');
    ting.textContent = '亭亭';
    const chrome = document.createElement('span');
    chrome.className = 'jty-foyer-chrome';
    chrome.setAttribute('aria-hidden', 'true');
    chrome.textContent = 'Ting';
    const index = document.createElement('span');
    index.className = 'jty-foyer-index';
    index.setAttribute('aria-hidden', 'true');
    index.innerHTML = 'PRIVATE ROOM<br>WEATHER / MEMORY / LIFE';
    wordmark.append(fashion, ting, chrome, index);
  }

  function portalButton(space, zh, en) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'jty-space-portal jty-space-glass jty-3d-glass-target';
    button.dataset.space = space;
    button.dataset.glassThickness = '8';
    button.dataset.glassRx = '1.2';
    button.dataset.glassRy = space === 'chat' ? '-1.4' : space === 'studio' ? '1.2' : '0.7';
    button.dataset.glassRz = '0';
    button.innerHTML = `<strong>${zh}</strong><small>${en}</small>`;
    return button;
  }

  function routeViaExistingButton(view) {
    root.dataset.spaceMotion = viewMotion[view] || 'home';
    const selector = view === 'chat' ? '#btn-chat-home' : `#btn-${view}`;
    const button = $(selector) || $(`.btn-nav[data-view="${view}"]`);
    button?.click();
  }

  function mountFoyerPortals() {
    const hero = $('#view-home .home-hero');
    if (!hero || hero.querySelector('.jty-foyer-portals')) return;
    const portals = document.createElement('div');
    portals.className = 'jty-foyer-portals';
    portals.setAttribute('aria-label', '回家入口');
    const chat = portalButton('chat', '去见他', 'TURN RIGHT / TALK');
    chat.addEventListener('click', () => routeViaExistingButton('chat'));
    portals.append(chat);
    hero.appendChild(portals);
  }

  function mountLifeLayer() {
    // Life already lives directly below Welcome Home in the source DOM.
    // Do not re-parent it into an overlay: moving glass-bearing DOM was the
    // source of stale geometry and cross-view stacking.
    $('#v872-home-life-host')?.classList.add('jty-life-inline');
  }

  function closeLife() {
    delete root.dataset.homeSpace;
  }

  function mountRoomPlanes() {
    // 8.8: empty full-area glass backplates were visual underlays, not content.
    $$('.jty-chat-plane,.jty-studio-canopy').forEach((node) => node.remove());
  }

  function mountSpatialNavHandle() {
    if ($('.jty-room-nav-handle')) return;
    const handle = document.createElement('button');
    handle.type = 'button';
    handle.className = 'jty-room-nav-handle jty-jelly-control jty-jelly-target jty-3d-glass-target';
    handle.dataset.glassThickness = '7';
    handle.dataset.glassRx = '1';
    handle.dataset.glassRy = '-1';
    handle.setAttribute('aria-label', '打开空间导航');
    handle.setAttribute('aria-expanded', 'false');
    handle.innerHTML = '<span aria-hidden="true"></span><small>ROOM</small>';
    document.body.appendChild(handle);

    const setOpen = (open) => {
      root.dataset.roomNavOpen = open ? 'true' : 'false';
      handle.setAttribute('aria-expanded', String(open));
      handle.setAttribute('aria-label', open ? '收起空间导航' : '打开空间导航');
      requestGlassRefresh();
    };
    handle.addEventListener('click', () => setOpen(root.dataset.roomNavOpen !== 'true'));
    document.addEventListener('click', (event) => {
      if (window.innerWidth > 900 || root.dataset.roomNavOpen !== 'true') return;
      if (event.target.closest('.jty-room-nav-handle')) return;
      setOpen(false);
    });
  }

  function mountAdvancedRail() {
    if ($('.jty-tool-rail')) return;
    const rail = document.createElement('nav');
    rail.className = 'jty-tool-rail';
    rail.setAttribute('aria-label', '高级工具常驻入口');
    rail.innerHTML = `
      <button type="button" data-jty-tool="atlas" title="记忆星图" aria-label="记忆星图">星</button>
      <button type="button" data-jty-tool="mcp" title="MCP" aria-label="MCP">M</button>
      <button type="button" data-jty-tool="push" title="Web Push / 系统设置" aria-label="Web Push / 系统设置">P</button>`;
    document.body.appendChild(rail);

    rail.addEventListener('click', (event) => {
      const button = event.target.closest('[data-jty-tool]');
      if (!button) return;
      const tool = button.dataset.jtyTool;
      if (tool === 'atlas') {
        routeViaExistingButton('memory');
        setTimeout(() => window.JTYUI83?.memory?.activate?.('map'), 80);
        return;
      }
      routeViaExistingButton('system');
      setTimeout(() => {
        if (tool === 'mcp') {
          window.JTYUI83?.system?.activate?.('tools');
          setTimeout(() => $('#u83-mcp-tools')?.scrollIntoView({ block: 'center', behavior: 'smooth' }), 80);
        } else {
          window.JTYUI83?.system?.activate?.('settings');
          setTimeout(() => $('#u83-push-state')?.scrollIntoView({ block: 'center', behavior: 'smooth' }), 80);
        }
      }, 80);
    });
  }

  function setMotionFromTarget(target) {
    const view = target?.dataset?.view || target?.dataset?.goView;
    if (!view) return;
    root.dataset.spaceMotion = viewMotion[view] || (view === 'life' ? 'life' : 'home');
    if (view !== 'home') closeLife();
  }

  function bindSpatialNavigation() {
    document.addEventListener('click', (event) => {
      const target = event.target.closest('[data-view],[data-go-view]');
      if (target) setMotionFromTarget(target);
    }, true);

    const activeObserver = new MutationObserver(() => {
      const active = root.dataset.activeView || 'home';
      if (active !== 'home') closeLife();
      decorateViewGlass(active);
      requestGlassRefresh();
    });
    activeObserver.observe(root, { attributes: true, attributeFilter: ['data-active-view'] });
  }

  function decorateViewGlass() {
    ['chat', 'memory', 'inner', 'system', 'us'].forEach((view) => {
      $(`#view-${view}`)?.classList.add('jty-room-plane');
    });
    [
      '#view-inner .inner-hero',
      '#view-us .system-hero',
    ].forEach((selector) => $(selector)?.classList.add('jty-space-glass'));
    $('#view-chat .input-wrap')?.classList.add('jty-jelly-target');
  }

  function decorateDialogs() {
    $$('.dwell-sheet').forEach((node) => {
      // Life sheets are tactile paper/device surfaces. Keeping them out of the
      // GPU target list prevents refraction and transparency from reducing text contrast.
      node.classList.remove('jty-jelly-target', 'jty-3d-glass-target', 'liquid-webgl-target', 'liquid-gpu-target');
      node.dataset.glass = 'off';
    });
    $$('dialog.system-dialog:not(#dwell-life-dialog)').forEach((dialog) => {
      const shell = dialog.firstElementChild;
      if (shell) {
        shell.classList.add('jty-jelly-target', 'jty-3d-glass-target');
        shell.dataset.glassThickness ||= '11';
        shell.dataset.glassRx ||= '.8';
        shell.dataset.glassRy ||= '-.7';
      }
    });
    $$('.dialog-close,.dwell-icon-button').forEach((node) => node.classList.add('jty-jelly-control'));
  }

  function ensureBubbleObserver() {
    const messages = $('#messages');
    if (!messages || bubbleIntersection) return;
    bubbleIntersection = new IntersectionObserver((entries) => {
      let changed = false;
      entries.forEach((entry) => {
        const bubble = entry.target;
        if (entry.isIntersecting) {
          if (!bubble.classList.contains('jty-jelly-target')) {
            bubble.classList.add('jty-jelly-target');
            changed = true;
          }
        } else if (bubble.classList.contains('jty-jelly-target')) {
          bubble.classList.remove('jty-jelly-target');
          changed = true;
        }
      });
      if (changed) requestGlassRefresh();
    }, { root: messages, rootMargin: '100px 0px', threshold: 0 });
  }

  function observeBubble(bubble) {
    ensureBubbleObserver();
    if (!bubble || bubble.dataset.jtyGlassObserved === '1') return;
    bubble.dataset.jtyGlassObserved = '1';
    bubbleIntersection?.observe(bubble);
  }

  function decorateMessages() {
    $$('#messages .message .bubble').forEach(observeBubble);
  }

  function pulseNewestAssistant() {
    if (emotionIntensity < .75) return;
    const bubble = $$('#messages .message.assistant .bubble').at(-1);
    if (!bubble) return;
    bubble.classList.remove('jty-state-pulse');
    void bubble.offsetWidth;
    bubble.classList.add('jty-state-pulse');
    setTimeout(() => bubble.classList.remove('jty-state-pulse'), 2050);
  }

  function bindMessageObserver() {
    const messages = $('#messages');
    if (!messages || messageObserver) return;
    messageObserver = new MutationObserver((records) => {
      let newAssistant = false;
      records.forEach((record) => record.addedNodes.forEach((node) => {
        if (!(node instanceof Element)) return;
        const bubbles = node.matches?.('.message .bubble') ? [node] : $$('.message .bubble', node);
        bubbles.forEach(observeBubble);
        if (node.matches?.('.message.assistant') || node.querySelector?.('.message.assistant')) newAssistant = true;
      }));
      if (newAssistant) setTimeout(pulseNewestAssistant, 40);
    });
    messageObserver.observe(messages, { childList: true, subtree: true });
  }

  function parseLevel(text) {
    const m = String(text || '').match(/(10|[0-9])\s*\/\s*10/);
    return m ? Number(m[1]) : 0;
  }

  function updateEmotionalEnvironment() {
    const highlights = $$('#home-inner-highlights span');
    const labels = highlights.map((node) => node.textContent || '').join(' ');
    const levels = highlights.map((node) => parseLevel(node.textContent));
    const phase = $('#home-life-phase')?.textContent || '';
    const maxLevel = Math.max(0, ...levels);
    emotionIntensity = Math.max(0, Math.min(1, (maxLevel - 4) / 6));

    let edge = '210 236 232';
    let warm = 0;
    let cool = 0;
    if (/开心|温柔|依恋|安心|满足|亲近|爱|暖/.test(labels + phase)) {
      edge = '246 223 198'; warm = emotionIntensity;
    } else if (/难过|低落|孤独|疲惫|困/.test(labels + phase)) {
      edge = '191 216 231'; cool = emotionIntensity * .85;
    } else if (/焦虑|紧张|不安|警觉/.test(labels + phase)) {
      edge = '211 204 235'; cool = emotionIntensity * .45; warm = emotionIntensity * .12;
    } else if (/生气|愤怒|恼火|烦躁/.test(labels + phase)) {
      edge = '239 204 190'; warm = emotionIntensity * .58;
    }

    root.style.setProperty('--jty-emotion-edge', edge);
    root.style.setProperty('--jty-emotion-edge-alpha', String(.08 + emotionIntensity * .08));
    root.style.setProperty('--jty-emotion-warm', warm.toFixed(3));
    root.style.setProperty('--jty-emotion-cool', cool.toFixed(3));
    root.style.setProperty('--jty-emotion-exposure', (1 + warm * .012 - cool * .010).toFixed(3));
    root.style.setProperty('--jty-rain-tempo', (1 + emotionIntensity * .08).toFixed(3));
    root.dataset.jtyStateStrong = emotionIntensity >= .75 ? 'true' : 'false';

    window.dispatchEvent(new CustomEvent('daxigua:ambient-state-change', {
      detail: { intensity: emotionIntensity, edge, warm, cool },
    }));
  }

  function bindEmotionalEnvironment() {
    const targets = [$('#home-inner-highlights'), $('#home-life-phase')].filter(Boolean);
    if (!targets.length) return;
    const observer = new MutationObserver(updateEmotionalEnvironment);
    targets.forEach((node) => observer.observe(node, { childList: true, subtree: true, characterData: true }));
    updateEmotionalEnvironment();
  }

  function bindReaderBreathing() {
    window.addEventListener('daxigua:ui-mode-change', () => {
      requestAnimationFrame(() => requestGlassRefresh());
    });
  }

  function boot() {
    addFoyerTypography();
    mountFoyerPortals();
    mountLifeLayer();
    mountRoomPlanes();
    mountSpatialNavHandle();
    mountAdvancedRail();
    decorateViewGlass();
    decorateDialogs();
    decorateMessages();
    bindMessageObserver();
    bindSpatialNavigation();
    bindEmotionalEnvironment();
    bindReaderBreathing();
    requestGlassRefresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(boot, 180), { once: true });
  } else {
    setTimeout(boot, 180);
  }
})();
