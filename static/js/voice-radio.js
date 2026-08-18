/** Local voice archive and user-curated radio programs. */
(function voiceRadioBootstrap(global) {
  'use strict';
  const panel = document.getElementById('voice-radio-panel');
  if (!panel) return;

  const archiveList = document.getElementById('voice-archive-list');
  const programList = document.getElementById('voice-program-list');
  const titleInput = document.getElementById('voice-program-title');
  const categoryInput = document.getElementById('voice-program-category');
  const refreshButton = document.getElementById('btn-voice-archive-refresh');
  const createButton = document.getElementById('btn-voice-program-create');
  let loaded = false;
  let activeAudio = null;

  function escapeHTML(value) {
    const node = document.createElement('div');
    node.textContent = String(value || '');
    return node.innerHTML;
  }

  async function fetchJSON(url, options = {}, timeoutMs = 20000) {
    const controller = new AbortController();
    const timer = global.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, { cache: 'no-store', ...options, signal: controller.signal });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
      return data;
    } catch (error) {
      if (error?.name === 'AbortError') throw new Error('电台读取超时');
      throw error;
    } finally {
      global.clearTimeout(timer);
    }
  }

  function archiveLabel(item) {
    const role = item.role === 'assistant' ? 'AI' : '你';
    const duration = Number(item.duration_ms || 0) > 0
      ? `${(Number(item.duration_ms) / 1000).toFixed(1)} 秒` : '时长未知';
    return `${role} · ${duration}`;
  }

  function renderArchive(items) {
    const playable = items.filter((item) => item.audio_url);
    if (!items.length) {
      archiveList.innerHTML = '<p>还没有通话存档。私密与陪睡模式不会保存原音频。</p>';
      return;
    }
    archiveList.innerHTML = items.map((item) => {
      const mood = item.mood?.emotion ? ` · ${item.mood.emotion}` : '';
      const privacy = item.private_mode || item.sleep_mode
        ? '<small>本轮只保留文字，没有原音频</small>' : '';
      const player = item.audio_url
        ? `<audio controls preload="none" src="${escapeHTML(item.audio_url)}"></audio>` : '';
      return `<article class="voice-archive-item">
        <div class="voice-archive-head">
          <input type="checkbox" value="${escapeHTML(item.id)}" ${item.audio_url ? '' : 'disabled'} aria-label="选择片段">
          <strong>${escapeHTML(archiveLabel(item))}${escapeHTML(mood)}</strong>
          <small>${escapeHTML(String(item.created_at || '').replace('T', ' ').slice(0, 16))}</small>
        </div>
        <p>${escapeHTML(item.transcript || '（无字幕）')}</p>${privacy}${player}
      </article>`;
    }).join('');
    if (!playable.length) archiveList.insertAdjacentHTML('afterbegin', '<p>这些通话都处于私密或陪睡模式，因此没有可做成节目的原音频。</p>');
    archiveList.querySelectorAll('audio').forEach((audio) => {
      audio.addEventListener('play', () => {
        if (activeAudio && activeAudio !== audio) activeAudio.pause();
        activeAudio = audio;
      });
    });
  }

  function renderPrograms(items) {
    if (!items.length) {
      programList.innerHTML = '<p>还没有节目。勾选上方片段后就能制作。</p>';
      return;
    }
    programList.innerHTML = items.map((program) => `<article class="voice-program-item" data-program-id="${escapeHTML(program.id)}">
      <strong>${escapeHTML(program.title)}</strong>
      <small>${escapeHTML(program.category || '未分类')} · ${Number(program.item_count || 0)} 段</small>
      <button class="btn-soft" type="button" data-program-play="${escapeHTML(program.id)}">连续播放</button>
      <div class="voice-program-playlist hidden"></div>
    </article>`).join('');
  }

  async function load() {
    refreshButton.disabled = true;
    try {
      const [archive, programs] = await Promise.all([
        fetchJSON('/api/voice/archive?limit=200'),
        fetchJSON('/api/voice/programs?limit=100'),
      ]);
      renderArchive(Array.isArray(archive.items) ? archive.items : []);
      renderPrograms(Array.isArray(programs.items) ? programs.items : []);
      loaded = true;
    } catch (error) {
      archiveList.innerHTML = `<p>读取失败：${escapeHTML(error.message)}</p>`;
    } finally {
      refreshButton.disabled = false;
    }
  }

  async function createProgram() {
    const segmentIds = [...archiveList.querySelectorAll('input[type="checkbox"]:checked')]
      .map((input) => input.value);
    if (!titleInput.value.trim()) {
      alert('先给节目取一个名字。');
      return;
    }
    if (!segmentIds.length) {
      alert('至少勾选一段带原音频的通话。');
      return;
    }
    createButton.disabled = true;
    try {
      await fetchJSON('/api/voice/programs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: titleInput.value.trim(),
          category: categoryInput.value.trim(),
          segment_ids: segmentIds,
        }),
      });
      titleInput.value = '';
      await load();
    } catch (error) {
      alert(`节目没有创建：${error.message}`);
    } finally {
      createButton.disabled = false;
    }
  }

  async function playProgram(programId, article) {
    const box = article.querySelector('.voice-program-playlist');
    try {
      const program = await fetchJSON(`/api/voice/programs/${encodeURIComponent(programId)}`);
      const items = (program.items || []).filter((item) => item.audio_url);
      box.classList.remove('hidden');
      box.innerHTML = items.map((item, index) => `<button type="button" data-radio-index="${index}">${escapeHTML(archiveLabel(item))} · ${escapeHTML(item.transcript || '无字幕')}</button>`).join('');
      if (!items.length) return;
      let index = 0;
      const playNext = () => {
        if (index >= items.length) return;
        activeAudio?.pause?.();
        const audio = new Audio(items[index].audio_url);
        activeAudio = audio;
        audio.addEventListener('ended', () => { index += 1; playNext(); }, { once: true });
        audio.play().catch(() => {});
      };
      box.onclick = (event) => {
        const button = event.target.closest('[data-radio-index]');
        if (!button) return;
        index = Number(button.dataset.radioIndex || 0);
        playNext();
      };
      playNext();
    } catch (error) {
      box.classList.remove('hidden');
      box.textContent = `节目读取失败：${error.message}`;
    }
  }

  panel.addEventListener('toggle', () => {
    if (panel.open && !loaded) load();
  });
  refreshButton.addEventListener('click', load);
  createButton.addEventListener('click', createProgram);
  programList.addEventListener('click', (event) => {
    const button = event.target.closest('[data-program-play]');
    if (button) playProgram(button.dataset.programPlay, button.closest('.voice-program-item'));
  });
})(window);
