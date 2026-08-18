(() => {
  'use strict';

  const KEY = 'jtyhome:sfx:v3';
  const DB_NAME = 'jtyhome-local-sfx';
  const DB_VERSION = 1;
  const STORE = 'clips';
  const DEF = {
    enabled: false,
    intensity: 'natural',
    density: 2,
    volume: .55,
    categories: {
      water: true,
      contact: true,
      wet: true,
      bed: true,
      thump: true,
      fabric: true,
    },
  };
  const CATEGORY_LABELS = {
    water: '水声 / 淋浴',
    contact: '节奏接触',
    wet: '湿润接触',
    bed: '床垫 / 床架',
    thump: '墙 / 家具',
    fabric: '布料 / 动作',
  };

  let cfg = loadConfig();
  let ctx = null;
  let dbPromise = null;
  const buffers = new Map();
  const live = new Set();

  function loadConfig() {
    try {
      const raw = JSON.parse(localStorage.getItem(KEY) || '{}');
      return {
        ...DEF,
        ...raw,
        categories: { ...DEF.categories, ...(raw.categories || {}) },
      };
    } catch (_) {
      return { ...DEF, categories: { ...DEF.categories } };
    }
  }

  function saveConfig() {
    try { localStorage.setItem(KEY, JSON.stringify(cfg)); } catch (_) {}
    syncControls();
  }

  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE)) {
          const store = db.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true });
          store.createIndex('category', 'category', { unique: false });
          store.createIndex('createdAt', 'createdAt', { unique: false });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('无法打开本地音效库'));
    });
    return dbPromise;
  }

  async function tx(mode, run) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE, mode);
      const store = transaction.objectStore(STORE);
      let result;
      try { result = run(store, transaction); } catch (error) { reject(error); return; }
      transaction.oncomplete = () => resolve(result);
      transaction.onerror = () => reject(transaction.error || new Error('本地音效库操作失败'));
      transaction.onabort = () => reject(transaction.error || new Error('本地音效库操作已取消'));
    });
  }

  async function listClips() {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE, 'readonly');
      const request = transaction.objectStore(STORE).getAll();
      request.onsuccess = () => resolve((request.result || []).sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0)));
      request.onerror = () => reject(request.error || new Error('读取本地音效库失败'));
    });
  }

  async function addFiles(files, category) {
    const audioFiles = Array.from(files || []).filter((file) => file && String(file.type || '').startsWith('audio/'));
    if (!audioFiles.length) return 0;
    const now = Date.now();
    await tx('readwrite', (store) => {
      audioFiles.forEach((file, index) => store.add({
        name: file.name || `音效-${index + 1}`,
        type: file.type || 'audio/*',
        size: Number(file.size || 0),
        category,
        createdAt: now + index,
        blob: file,
      }));
    });
    return audioFiles.length;
  }

  async function deleteClip(id) {
    const numericId = Number(id);
    if (!Number.isFinite(numericId)) return;
    buffers.delete(numericId);
    await tx('readwrite', (store) => store.delete(numericId));
  }

  async function clearLibrary() {
    buffers.clear();
    await tx('readwrite', (store) => store.clear());
  }

  async function audio() {
    if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === 'suspended') await ctx.resume();
    return ctx;
  }

  async function picks(category) {
    const clips = await listClips();
    return clips.filter((item) => item.category === category);
  }

  function one(items) {
    return items[Math.floor(Math.random() * items.length)];
  }

  async function bufferFor(item) {
    if (!item?.id || !item.blob) return null;
    if (buffers.has(item.id)) return buffers.get(item.id);
    const context = await audio();
    const decoded = await context.decodeAudioData(await item.blob.arrayBuffer());
    buffers.set(item.id, decoded);
    return decoded;
  }

  async function play(item, scale = 1) {
    if (!item) return;
    const context = await audio();
    const decoded = await bufferFor(item);
    if (!decoded) return;
    const source = context.createBufferSource();
    const gain = context.createGain();
    source.buffer = decoded;
    source.playbackRate.value = .96 + Math.random() * .08;
    const intensity = { subtle: .72, natural: 1, obvious: 1.18 }[cfg.intensity] || 1;
    gain.gain.value = Math.min(1, cfg.volume * scale * intensity * (.88 + Math.random() * .2));
    source.connect(gain).connect(context.destination);
    live.add(source);
    source.onended = () => live.delete(source);
    source.start();
  }

  function stop() {
    live.forEach((source) => { try { source.stop(); } catch (_) {} });
    live.clear();
  }

  function cues(text) {
    const value = String(text || '').toLowerCase();
    const result = [];
    const add = (category) => {
      if (cfg.categories[category] !== false && !result.includes(category)) result.push(category);
    };
    if (/淋浴|洗澡|浴室|水声|水花|水里|浴缸|shower|bath|splash/.test(value)) add('water');
    if (/床|床垫|床架|床单|床头|吱|mattress|bedframe|creak/.test(value)) add('bed');
    if (/墙|家具|柜|桌|门板|咚|撞|抵在|靠在|thump|wall|furniture/.test(value)) add('thump');
    if (/衣服|布料|床单|摩擦|fabric|cloth|rustle/.test(value)) add('fabric');
    if (/水润|湿润|湿漉|水迹|湿热|wet|moist|slick/.test(value)) add('wet');
    if (/节奏|拍击|拍打|撞击|一下又一下|啪啪|啪、|啪。|slap|impact|rhythm/.test(value)) add('contact');
    return result;
  }

  async function playCues(categories) {
    if (!cfg.enabled || !categories.length) return;
    const maxLayers = Math.max(1, Math.min(3, Number(cfg.density) || 2));
    for (const [index, category] of categories.slice(0, maxLayers).entries()) {
      const clips = await picks(category);
      if (!clips.length) continue;
      window.setTimeout(() => play(one(clips), index ? .72 : 1).catch(() => {}), index * 120 + Math.random() * 80);
    }
  }

  function decorate(element, text) {
    if (!element || element.dataset.role !== 'assistant') return;
    const categories = cues(text);
    const host = element.querySelector('.message-actions');
    let button = host?.querySelector('[data-message-action="sfx"]');
    if (!categories.length) {
      button?.remove();
      return;
    }
    if (!button && host) {
      button = document.createElement('button');
      button.type = 'button';
      button.dataset.messageAction = 'sfx';
      button.className = 'u83-sfx-msg';
      button.textContent = '环境音';
      button.title = `使用你导入的本地音效：${categories.map((x) => CATEGORY_LABELS[x] || x).join(' / ')}`;
      button.onclick = () => {
        cfg.enabled = true;
        saveConfig();
        audio().then(() => playCues(categories));
      };
      host.prepend(button);
    }
  }

  async function final(element, text) {
    decorate(element, text);
    if (cfg.enabled) await playCues(cues(text));
  }

  async function preview(category) {
    cfg.enabled = true;
    saveConfig();
    await audio();
    const clips = await picks(category);
    if (!clips.length) return false;
    await play(one(clips));
    return true;
  }

  function formatBytes(bytes) {
    const size = Number(bytes || 0);
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function safeText(value) {
    return String(value || '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  }

  async function renderLibrary() {
    const host = document.querySelector('#u83-sfx-library');
    const count = document.querySelector('#u83-sfx-count');
    const status = document.querySelector('#u83-sfx-status');
    if (!host && !count && !status) return;
    try {
      const clips = await listClips();
      if (count) count.textContent = `${clips.length} 段`;
      if (status) status.textContent = clips.length
        ? (cfg.enabled ? `自动匹配已开启 · ${clips.length} 段` : `已导入 ${clips.length} 段 · 当前关闭`)
        : '素材库为空 · 请先导入';
      if (!host) return;
      if (!clips.length) {
        host.innerHTML = '<div class="u83-empty">这里不再内置任何音效。选择分类后，从你的设备导入音频文件即可。</div>';
        return;
      }
      host.innerHTML = clips.map((item) => `
        <div class="u83-row u83-sfx-row">
          <div>
            <strong>${safeText(item.name)}</strong>
            <small>${safeText(CATEGORY_LABELS[item.category] || item.category)} · ${formatBytes(item.size)}</small>
          </div>
          <div class="u83-sfx-row-actions">
            <button class="u83-btn" type="button" data-sfx-preview-id="${Number(item.id)}">试听</button>
            <button class="u83-btn danger" type="button" data-sfx-delete-id="${Number(item.id)}">删除</button>
          </div>
        </div>`).join('');
      host.querySelectorAll('[data-sfx-preview-id]').forEach((button) => {
        button.onclick = async () => {
          const clip = clips.find((item) => Number(item.id) === Number(button.dataset.sfxPreviewId));
          if (!clip) return;
          cfg.enabled = true;
          saveConfig();
          await play(clip).catch(() => {});
        };
      });
      host.querySelectorAll('[data-sfx-delete-id]').forEach((button) => {
        button.onclick = async () => {
          await deleteClip(button.dataset.sfxDeleteId);
          await renderLibrary();
        };
      });
    } catch (error) {
      if (host) host.innerHTML = `<div class="u83-empty">本地音效库读取失败：${safeText(error.message)}</div>`;
      if (status) status.textContent = '本地音效库不可用';
    }
  }

  function syncControls() {
    const master = document.querySelector('#u83-sfx-master');
    if (master) master.setAttribute('aria-pressed', String(cfg.enabled));
    const intensity = document.querySelector('#u83-sfx-intensity');
    if (intensity) intensity.value = cfg.intensity;
    const density = document.querySelector('#u83-sfx-density');
    if (density) density.value = String(cfg.density);
    const volume = document.querySelector('#u83-sfx-volume');
    if (volume) volume.value = String(cfg.volume);
    document.querySelectorAll('[data-sfx-cat]').forEach((checkbox) => {
      checkbox.checked = cfg.categories[checkbox.dataset.sfxCat] !== false;
    });
    renderLibrary().catch(() => {});
  }

  function bind() {
    const root = document.querySelector('#u83-sfx-panel');
    if (!root || root.dataset.bound === 'true') {
      syncControls();
      return;
    }
    root.dataset.bound = 'true';
    syncControls();

    document.querySelector('#u83-sfx-master')?.addEventListener('click', async () => {
      cfg.enabled = !cfg.enabled;
      saveConfig();
      if (cfg.enabled) await audio();
    });
    document.querySelector('#u83-sfx-intensity')?.addEventListener('change', (event) => {
      cfg.intensity = event.target.value;
      saveConfig();
    });
    document.querySelector('#u83-sfx-density')?.addEventListener('change', (event) => {
      cfg.density = Number(event.target.value) || 2;
      saveConfig();
    });
    document.querySelector('#u83-sfx-volume')?.addEventListener('change', (event) => {
      cfg.volume = Number(event.target.value) || .55;
      saveConfig();
    });
    document.querySelectorAll('[data-sfx-cat]').forEach((checkbox) => checkbox.addEventListener('change', () => {
      cfg.categories[checkbox.dataset.sfxCat] = checkbox.checked;
      saveConfig();
    }));

    const fileInput = document.querySelector('#u83-sfx-files');
    document.querySelector('#u83-sfx-import')?.addEventListener('click', () => fileInput?.click());
    fileInput?.addEventListener('change', async () => {
      const category = document.querySelector('#u83-sfx-import-category')?.value || 'water';
      const count = await addFiles(fileInput.files, category).catch(() => 0);
      fileInput.value = '';
      if (count) await renderLibrary();
    });
    document.querySelector('#u83-sfx-clear')?.addEventListener('click', async () => {
      await clearLibrary();
      stop();
      await renderLibrary();
    });
    document.querySelector('#u83-sfx-stop')?.addEventListener('click', stop);
  }

  window.JTYSFX = {
    bind,
    decorate,
    final,
    stop,
    renderLibrary,
    clearLibrary,
  };
})();
