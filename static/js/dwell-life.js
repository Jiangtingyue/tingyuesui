(() => {
  'use strict';

  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
  const esc = (v) => String(v ?? '').replace(/[&<>'"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const companion = document.documentElement.dataset.companionName || '他';
  const dialog = $('#dwell-life-dialog');
  const body = $('#dwell-life-dialog-body');
  const title = $('#dwell-life-dialog-title');
  const kicker = $('#dwell-life-dialog-kicker');
  const subtitle = $('#dwell-life-dialog-subtitle');
  const state = {
    todoSide: 'hers',
    loaded: false,
    calendarCursor: new Date(new Date().getFullYear(), new Date().getMonth(), 1),
    currentMusic: null,
  };
  const sheetKinds = Object.freeze({
    '日记': 'diary',
    '悄悄话': 'whispers',
    '双人清单': 'todos',
    '日历': 'calendar',
    '共读': 'reading',
    '音乐卡片': 'music',
    '身体与状态': 'health',
    '心跳': 'heartbeat',
    '今日小报': 'daily',
  });

  function currentSessionId() {
    return $('.session-item.active')?.dataset.id || '';
  }

  function withSession(url) {
    const sid = currentSessionId();
    const join = url.includes('?') ? '&' : '?';
    return sid ? `${url}${join}session_id=${encodeURIComponent(sid)}` : url;
  }

  async function api(url, options = {}) {
    const init = { ...options, headers: { ...(options.headers || {}) } };
    if (init.body && typeof init.body !== 'string') {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(init.body);
    }
    const response = await fetch(url, init);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.detail || data.error || `请求失败（${response.status}）`);
      error.dwellLife = true;
      throw error;
    }
    return data;
  }

  function showMutationError(error) {
    const message = error?.message || '操作失败，请稍后重试';
    let host = $('.dwell-inline-error', body);
    if (!host) {
      host = document.createElement('div');
      host.className = 'dwell-inline-error';
      body?.prepend(host);
    }
    host.textContent = message;
  }

  window.addEventListener('unhandledrejection', (event) => {
    if (!event.reason?.dwellLife) return;
    event.preventDefault();
    showMutationError(event.reason);
  });

  function openSheet(name, sub = '') {
    const kind = sheetKinds[name] || 'life';
    dialog.dataset.lifeKind = kind;
    body.dataset.lifeKind = kind;
    kicker.textContent = name.toUpperCase();
    title.textContent = name;
    subtitle.textContent = sub;
    body.innerHTML = '<div class="dwell-empty"><strong>正在打开</strong><span>从当前 jtyhome 读取本地数据…</span></div>';
    if (!dialog.open) dialog.showModal();
  }

  function closeSheet() { if (dialog?.open) dialog.close(); }

  function localYMD(date = new Date()) {
    return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
  }

  function formatTime(ts) {
    const d = new Date(Number(ts || 0) * 1000);
    if (Number.isNaN(d.getTime())) return '';
    return new Intl.DateTimeFormat('zh-CN', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' }).format(d);
  }

  function setToday() {
    const d = new Date();
    if ($('#dwell-today-month')) $('#dwell-today-month').textContent = new Intl.DateTimeFormat('en', { month:'short' }).format(d).toUpperCase();
    if ($('#dwell-today-day')) $('#dwell-today-day').textContent = String(d.getDate()).padStart(2, '0');
    if ($('#dwell-today-week')) $('#dwell-today-week').textContent = new Intl.DateTimeFormat('zh-CN', { weekday:'short' }).format(d);
  }

  function compactText(value, limit = 88) {
    const clean = String(value || '').replace(/\s+/g, ' ').trim();
    return clean.length > limit ? `${clean.slice(0, limit).trim()}…` : clean;
  }


  async function loadSummary() {
    setToday();
    try {
      const [sum, co] = await Promise.all([
        api(withSession('/api/dwell/summary')),
        api(withSession('/api/co-presence/state')).catch(() => ({})),
      ]);
      const previews = sum.previews || {};
      const diaryCount = Number(sum.diary_count || 0);
      const whisperCount = Number(sum.whisper_count || 0);
      const bookCount = Number(sum.book_count || 0);
      const musicCount = Number(sum.music_count || 0);
      const open = Number(sum.todos?.hers_open || 0) + Number(sum.todos?.mine_open || 0);
      const total = Number(sum.todos?.hers_total || 0) + Number(sum.todos?.mine_total || 0);
      const done = Math.max(0, total - open);
      if ($('#dwell-diary-count')) $('#dwell-diary-count').textContent = diaryCount ? `${diaryCount} 篇留在这里` : '还没有写';
      if ($('#dwell-whisper-count')) $('#dwell-whisper-count').textContent = whisperCount ? `${whisperCount} 句话在抽屉里` : '抽屉是空的';
      if ($('#dwell-book-count')) $('#dwell-book-count').textContent = bookCount ? `${bookCount} 本书在书架上` : '书架还空着';
      if ($('#dwell-music-count')) $('#dwell-music-count').textContent = musicCount ? `${musicCount} 张音乐卡片` : '还没有音乐卡片';
      if ($('#dwell-calendar-count')) $('#dwell-calendar-count').textContent = Number(sum.calendar_count || 0) ? `${sum.calendar_count} 条日程` : '没有日程';
      if ($('#dwell-todo-count')) $('#dwell-todo-count').textContent = open ? `${open} 件还没做` : (total ? '都做完了' : '0 件待办');
      const ring = $('#dwell-todo-ring');
      if (ring) ring.style.setProperty('--pct', total ? Math.round(done / total * 100) : 0);
      const enabled = co.settings?.enabled !== false && co.settings?.independent_initiative_enabled !== false;
      if ($('#dwell-heartbeat-note')) $('#dwell-heartbeat-note').textContent = enabled ? '主动共处正在使用现有链路' : '主动共处已关闭';

      state.currentMusic = previews.current_music || null;
      state.loaded = true;
    } catch (error) {
      console.warn('[dwell-life] summary:', error);
    }
  }

  async function showDiary() {
    openSheet('日记', '你的本子。写了就在这里，不会被当成聊天历史。');
    const data = await api('/api/dwell/diary');
    body.innerHTML = `
      <form class="dwell-form" id="dwell-diary-form">
        <input name="title" maxlength="100" placeholder="今天这页叫什么（可留空）">
        <textarea name="text" maxlength="8000" required placeholder="写点什么…"></textarea>
        <button type="submit">记上</button>
      </form>
      <div class="dwell-section-title"><strong>最近</strong><small>${data.items.length} 篇</small></div>
      <div class="dwell-diary-list" id="dwell-diary-list"></div>`;
    renderDiary(data.items);
    $('#dwell-diary-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const fd = new FormData(form);
      await api('/api/dwell/diary', { method:'POST', body:{ title:fd.get('title'), text:fd.get('text') } });
      form.reset();
      const fresh = await api('/api/dwell/diary');
      renderDiary(fresh.items);
      loadSummary();
    });
  }

  function renderDiary(items) {
    const host = $('#dwell-diary-list');
    host.innerHTML = items.length ? items.map((item) => `
      <article class="dwell-diary-entry">
        <header><div><h3>${esc(item.title || '没有标题的一页')}</h3></div><time>${esc(formatTime(item.at))}</time></header>
        <p>${esc(item.text)}</p>
        <div class="dwell-row-actions" style="justify-content:flex-end;margin-top:8px"><button type="button" data-diary-edit="${esc(item.id)}">编辑</button><button type="button" data-diary-del="${esc(item.id)}">删除</button></div>
      </article>`).join('') : '<div class="dwell-empty"><strong>本子还是新的</strong><span>第一句话写下去以后，这一页就开始长了。</span></div>';
    $$('[data-diary-edit]', host).forEach((btn) => btn.addEventListener('click', async () => {
      const item = items.find((value) => String(value.id) === String(btn.dataset.diaryEdit));
      if (!item) return;
      const nextTitle = prompt('标题', item.title || '');
      if (nextTitle === null) return;
      const nextText = prompt('日记内容', item.text || '');
      if (nextText === null) return;
      await api(`/api/dwell/diary/${encodeURIComponent(item.id)}`, {
        method:'PATCH', body:{title:nextTitle, text:nextText}
      });
      renderDiary((await api('/api/dwell/diary')).items); loadSummary();
    }));
    $$('[data-diary-del]', host).forEach((btn) => btn.addEventListener('click', async () => {
      if (!confirm('删除这篇日记？')) return;
      await api(`/api/dwell/diary/${encodeURIComponent(btn.dataset.diaryDel)}`, { method:'DELETE' });
      renderDiary((await api('/api/dwell/diary')).items); loadSummary();
    }));
  }

  async function showWhispers() {
    openSheet('悄悄话', `只交给当前聊天窗口里的${companion}；换到新窗口不会自动带过去。`);
    const sid = currentSessionId();
    if (!sid) {
      body.innerHTML = '<div class="dwell-empty"><strong>先进入一个聊天窗口</strong><span>悄悄话需要明确属于某个窗口，不能作为全局旧记忆存在。</span></div>';
      return;
    }
    const data = await api(withSession('/api/dwell/whispers'));
    body.innerHTML = `
      <form class="dwell-form" id="dwell-whisper-form">
        <div class="dwell-form-line"><input name="text" maxlength="2000" required placeholder="悄悄跟${esc(companion)}说点什么…"><button type="submit">放进去</button></div>
      </form>
      <p class="dwell-note">这里不是第二个聊天窗口。内容只成为当前窗口的安静背景，不会自动生成回复，也不会进入其他新窗口。</p>
      <div class="dwell-section-title"><strong>抽屉</strong><small>${data.items.length} 条</small></div>
      <div class="dwell-whisper-list" id="dwell-whisper-list"></div>`;
    renderWhispers(data.items);
    $('#dwell-whisper-form').addEventListener('submit', async (e) => {
      e.preventDefault(); const form = e.currentTarget; const fd = new FormData(form);
      await api('/api/dwell/whispers', { method:'POST', body:{ text:fd.get('text'), session_id:sid } });
      form.reset(); renderWhispers((await api(withSession('/api/dwell/whispers'))).items); loadSummary();
    });
  }

  function renderWhispers(items) {
    const host = $('#dwell-whisper-list');
    host.innerHTML = items.length ? items.map((item) => `
      <div class="dwell-whisper ${item.who === 'gu' ? 'gu' : 'her'}">
        ${esc(item.text)}<small>${item.who === 'gu' ? esc(companion) : '你'} · ${esc(formatTime(item.at))} <button class="dwell-icon-button" type="button" data-whisper-del="${esc(item.id)}">删掉</button></small>
      </div>`).join('') : '<div class="dwell-empty"><strong>还没有人开口</strong><span>你写下的，他会知道；但这里不会自动响起回音。</span></div>';
    $$('[data-whisper-del]', host).forEach((btn) => btn.addEventListener('click', async () => {
      if (!confirm('删掉这条悄悄话？')) return;
      await api(withSession(`/api/dwell/whispers/${encodeURIComponent(btn.dataset.whisperDel)}`), { method:'DELETE' });
      renderWhispers((await api(withSession('/api/dwell/whispers'))).items); loadSummary();
    }));
  }

  async function showTodos(side = state.todoSide) {
    state.todoSide = side;
    openSheet('双人清单', `${companion}的和你的，两栏看的是同一间屋子。`);
    const data = await api('/api/dwell/todos');
    renderTodoSheet(data);
  }

  function sortTodos(items) {
    const now = new Date(); const hm = `${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}`;
    return items.slice().sort((a,b) => {
      const la = a.at && !a.done && a.at < hm, lb = b.at && !b.done && b.at < hm;
      if (la !== lb) return la ? -1 : 1;
      if (!!a.at !== !!b.at) return a.at ? -1 : 1;
      if (a.at && b.at && a.at !== b.at) return a.at < b.at ? -1 : 1;
      return Number(a.made || 0) - Number(b.made || 0);
    });
  }

  function renderTodoSheet(data) {
    const items = sortTodos(data[state.todoSide] || []);
    body.innerHTML = `
      <div class="dwell-todo-tabs"><button type="button" data-todo-side="hers" class="${state.todoSide==='hers'?'active':''}">我的</button><button type="button" data-todo-side="mine" class="${state.todoSide==='mine'?'active':''}">${esc(companion)}的</button></div>
      <form class="dwell-form" id="dwell-todo-form"><div class="dwell-form-line"><input name="text" maxlength="500" required placeholder="记一件事…"><button type="submit">加上</button></div><input name="at" type="time" aria-label="可选时间"></form>
      <div class="dwell-group" id="dwell-todo-list">${items.length ? items.map(todoRow).join('') : '<div class="dwell-empty"><strong>这一栏是空的</strong><span>没有事情挂在这里。</span></div>'}</div>`;
    $$('[data-todo-side]', body).forEach(btn => btn.addEventListener('click', () => showTodos(btn.dataset.todoSide)));
    $('#dwell-todo-form').addEventListener('submit', async (e) => {
      e.preventDefault(); const fd = new FormData(e.currentTarget);
      await api('/api/dwell/todos', { method:'POST', body:{ action:'add', side:state.todoSide, text:fd.get('text'), at:fd.get('at') } });
      renderTodoSheet(await api('/api/dwell/todos')); loadSummary();
    });
    $$('[data-todo-toggle]', body).forEach(btn => btn.addEventListener('click', async () => {
      await api('/api/dwell/todos', { method:'POST', body:{ action:'toggle', side:state.todoSide, id:btn.dataset.todoToggle } });
      renderTodoSheet(await api('/api/dwell/todos')); loadSummary();
    }));
    $$('[data-todo-del]', body).forEach(btn => btn.addEventListener('click', async () => {
      if (!confirm('删除这条待办？')) return;
      await api('/api/dwell/todos', { method:'POST', body:{ action:'del', side:state.todoSide, id:btn.dataset.todoDel } });
      renderTodoSheet(await api('/api/dwell/todos')); loadSummary();
    }));
  }

  function todoRow(item) {
    return `<div class="dwell-row ${item.done?'done':''}">
      <button type="button" class="dwell-todo-check ${item.done?'done':''}" data-todo-toggle="${esc(item.id)}">${item.done?'✓':''}</button>
      <div class="dwell-row-main"><strong>${esc(item.text)}</strong><small>${item.at ? `时间 ${esc(item.at)}` : '没有挂时间'}</small></div>
      <div class="dwell-row-actions"><button type="button" data-todo-del="${esc(item.id)}">删除</button></div>
    </div>`;
  }

  async function showCalendar() {
    openSheet('日历', '日程和一点点心情，单独留在生活空间。');
    const data = await api('/api/dwell/calendar');
    renderCalendarSheet(data.items);
  }

  function calendarCells(items, cursor = state.calendarCursor) {
    const now = new Date(); const y=cursor.getFullYear(), m=cursor.getMonth();
    const days = new Date(y,m+1,0).getDate(); const start = new Date(y,m,1).getDay();
    const prefix = `${y}-${String(m+1).padStart(2,'0')}`;
    const set = new Set(items.filter(x => String(x.date||'').startsWith(prefix)).map(x => Number(String(x.date).slice(8,10))));
    let out=''; for(let i=0;i<start;i++) out += '<span></span>';
    for(let d=1;d<=days;d++) {
      const today = d===now.getDate() && y===now.getFullYear() && m===now.getMonth();
      out += `<span class="${today?'today ':''}${set.has(d)?'has-event':''}">${d}</span>`;
    }
    return out;
  }

  function renderCalendarSheet(items) {
    const cursor = state.calendarCursor;
    const y = cursor.getFullYear(), m = cursor.getMonth();
    const prefix = `${y}-${String(m+1).padStart(2,'0')}`;
    const monthItems = items.filter(x => String(x.date||'').startsWith(prefix));
    const defaultDate = (y===new Date().getFullYear() && m===new Date().getMonth())
      ? localYMD() : `${prefix}-01`;
    const monthLabel = new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'long'}).format(cursor);
    body.innerHTML = `
      <div class="dwell-calendar-nav"><button type="button" data-cal-prev>‹</button><strong>${esc(monthLabel)}</strong><button type="button" data-cal-next>›</button></div>
      <div class="dwell-cal-grid">${calendarCells(items, cursor)}</div>
      <form class="dwell-form" id="dwell-cal-form">
        <div class="dwell-form-line"><input name="title" maxlength="160" required placeholder="这天有什么"><button type="submit">记到日历</button></div>
        <input name="date" type="date" value="${defaultDate}" required><input name="mood" maxlength="24" placeholder="心情（可留空）"><textarea name="note" maxlength="1200" placeholder="补充（可留空）"></textarea>
      </form>
      <div class="dwell-group" id="dwell-cal-list">${monthItems.length ? monthItems.map(calRow).join('') : '<div class="dwell-empty"><strong>这个月很安静</strong><span>还没有写下任何日程。</span></div>'}</div>`;
    $('[data-cal-prev]', body)?.addEventListener('click', () => { state.calendarCursor = new Date(y,m-1,1); renderCalendarSheet(items); });
    $('[data-cal-next]', body)?.addEventListener('click', () => { state.calendarCursor = new Date(y,m+1,1); renderCalendarSheet(items); });
    $('#dwell-cal-form').addEventListener('submit', async (e) => {
      e.preventDefault(); const fd=new FormData(e.currentTarget);
      await api('/api/dwell/calendar',{method:'POST',body:{title:fd.get('title'),date:fd.get('date'),mood:fd.get('mood'),note:fd.get('note')}});
      renderCalendarSheet((await api('/api/dwell/calendar')).items); loadSummary();
    });
    $$('[data-cal-edit]', body).forEach(btn => btn.addEventListener('click', async () => {
      const item = items.find((value) => String(value.id) === String(btn.dataset.calEdit));
      if (!item) return;
      const nextDate = prompt('日期（YYYY-MM-DD）', item.date || '');
      if (nextDate === null) return;
      const nextTitle = prompt('日程标题', item.title || '');
      if (nextTitle === null) return;
      const nextMood = prompt('心情', item.mood || '');
      if (nextMood === null) return;
      const nextNote = prompt('补充', item.note || '');
      if (nextNote === null) return;
      await api(`/api/dwell/calendar/${encodeURIComponent(item.id)}`, {
        method:'PATCH', body:{date:nextDate,title:nextTitle,mood:nextMood,note:nextNote}
      });
      renderCalendarSheet((await api('/api/dwell/calendar')).items); loadSummary();
    }));
    $$('[data-cal-del]', body).forEach(btn => btn.addEventListener('click', async () => {
      if (!confirm('删除这条日程？')) return;
      await api(`/api/dwell/calendar/${encodeURIComponent(btn.dataset.calDel)}`, {method:'DELETE'});
      renderCalendarSheet((await api('/api/dwell/calendar')).items); loadSummary();
    }));
  }

  function calRow(item) {
    return `<div class="dwell-row"><div class="dwell-row-main"><strong>${esc(item.date)} · ${esc(item.title)}</strong><small>${esc([item.mood,item.note].filter(Boolean).join(' · ') || '没有补充')}</small></div><div class="dwell-row-actions"><button type="button" data-cal-edit="${esc(item.id)}">编辑</button><button type="button" data-cal-del="${esc(item.id)}">删除</button></div></div>`;
  }

  async function showReading() {
    openSheet('共读', '书名、进度和想留下的一句话。先把书架做成真的，再慢慢加划线。');
    const data = await api('/api/dwell/books');
    renderReadingSheet(data.items);
  }

  function renderReadingSheet(items) {
    body.innerHTML = `
      <div class="dwell-kindle-shell">
        <div class="dwell-kindle-bar" aria-hidden="true"><span>Aa</span><strong>共同书架</strong><span>☰</span></div>
        <div class="dwell-kindle-page">
          <form class="dwell-form" id="dwell-book-form">
            <div class="dwell-form-line"><input name="title" maxlength="180" required placeholder="书名"><button type="submit">放上书架</button></div>
            <input name="author" maxlength="120" placeholder="作者（可留空）"><input name="progress" type="number" min="0" max="100" value="0" placeholder="进度 %"><textarea name="note" maxlength="2000" placeholder="想留下的一句话（可留空）"></textarea>
          </form>
          <div class="dwell-group">${items.length ? items.map(bookRow).join('') : '<div class="dwell-empty"><strong>书架还空着</strong><span>放上第一本正在一起读的书。</span></div>'}</div>
        </div>
        <div class="dwell-kindle-home" aria-hidden="true"></div>
      </div>`;
    $('#dwell-book-form').addEventListener('submit', async (e) => {
      e.preventDefault(); const fd=new FormData(e.currentTarget);
      await api('/api/dwell/books',{method:'POST',body:{action:'add',title:fd.get('title'),author:fd.get('author'),progress:Number(fd.get('progress')||0),note:fd.get('note')}});
      renderReadingSheet((await api('/api/dwell/books')).items); loadSummary();
    });
    $$('[data-book-del]', body).forEach(btn => btn.addEventListener('click', async () => {
      if (!confirm('把这本书从书架删除？')) return;
      await api('/api/dwell/books',{method:'POST',body:{action:'del',id:btn.dataset.bookDel}});
      renderReadingSheet((await api('/api/dwell/books')).items); loadSummary();
    }));
    $$('[data-book-edit]', body).forEach(btn => btn.addEventListener('click', async () => {
      const item = items.find(x => String(x.id) === String(btn.dataset.bookEdit));
      if (!item) return;
      const nextTitle = prompt('书名', item.title || '');
      if (nextTitle === null) return;
      const nextAuthor = prompt('作者', item.author || '');
      if (nextAuthor === null) return;
      const nextNote = prompt('备注', item.note || '');
      if (nextNote === null) return;
      await api('/api/dwell/books',{method:'POST',body:{action:'update',id:item.id,title:nextTitle,author:nextAuthor,note:nextNote}});
      renderReadingSheet((await api('/api/dwell/books')).items); loadSummary();
    }));
    $$('[data-book-progress]', body).forEach(input => input.addEventListener('change', async () => {
      await api('/api/dwell/books',{method:'POST',body:{action:'update',id:input.dataset.bookProgress,progress:Number(input.value||0)}});
      renderReadingSheet((await api('/api/dwell/books')).items); loadSummary();
    }));
  }

  function bookRow(item) {
    const pct=Math.max(0,Math.min(100,Number(item.progress||0)));
    return `<div class="dwell-row"><div class="dwell-book-cover" aria-hidden="true"></div><div class="dwell-row-main"><strong>${esc(item.title)}</strong><small>${esc(item.author || '没有写作者')} · ${pct}%</small><div class="dwell-progress"><i style="width:${pct}%"></i></div>${item.note?`<small>${esc(item.note)}</small>`:''}</div><input data-book-progress="${esc(item.id)}" type="range" min="0" max="100" value="${pct}" aria-label="阅读进度"><div class="dwell-row-actions"><button type="button" data-book-edit="${esc(item.id)}">编辑</button><button type="button" data-book-del="${esc(item.id)}">删除</button></div></div>`;
  }

  async function showMusic() {
    openSheet('音乐卡片', '把链接收在家里。卡片只保存你输入的标题、歌手和链接，不向第三方抓取元数据。');
    const data = await api('/api/dwell/music');
    renderMusicSheet(data.items || []);
  }

  function musicProvider(url) {
    try {
      const host = new URL(url).hostname.replace(/^www\./, '');
      if (host.includes('spotify')) return 'SPOTIFY';
      if (host.includes('music.apple')) return 'APPLE MUSIC';
      if (host.includes('youtube') || host.includes('youtu.be')) return 'YOUTUBE';
      if (host.includes('music.163')) return 'NETEASE';
      if (host.includes('qq.com')) return 'QQ MUSIC';
      return host.toUpperCase();
    } catch (_) { return 'MUSIC'; }
  }


  function isMusicLink(url) {
    try {
      const parsed = new URL(url, window.location.href);
      if (!['http:', 'https:'].includes(parsed.protocol)) return false;
      const host = parsed.hostname.toLowerCase().replace(/^www\./, '');
      return [
        'music.163.com', 'y.qq.com', 'c.y.qq.com', 'i.y.qq.com',
        'open.spotify.com', 'music.apple.com', 'music.youtube.com',
        'youtube.com', 'youtu.be', 'soundcloud.com'
      ].some((domain) => host === domain || host.endsWith(`.${domain}`));
    } catch (_) { return false; }
  }

  function decorateMusicLinks(root = document) {
    const scope = root?.querySelectorAll ? root : document;
    scope.querySelectorAll('.message .bubble a[href]:not([data-dwell-music-card])').forEach((link) => {
      const url = String(link.href || '');
      if (!isMusicLink(url)) return;
      const original = String(link.textContent || '').trim();
      const label = (!original || /^https?:\/\//i.test(original) || original.length > 96)
        ? '打开这张音乐卡片'
        : original;
      link.dataset.dwellMusicCard = 'true';
      link.classList.add('dwell-chat-music-card');
      link.setAttribute('aria-label', `${musicProvider(url)}：${label}`);
      link.innerHTML = `
        <span class="dwell-chat-music-mark" aria-hidden="true">♪</span>
        <span class="dwell-chat-music-copy"><small>${esc(musicProvider(url))}</small><strong>${esc(label)}</strong></span>
        <span class="dwell-chat-music-open" aria-hidden="true">打开 ↗</span>`;
    });
  }

  function renderMusicSheet(items) {
    body.innerHTML = `
      <form class="dwell-form" id="dwell-music-form">
        <div class="dwell-form-line"><input name="url" type="url" maxlength="2048" required placeholder="粘贴歌曲 / 专辑链接"><button type="submit">做成卡片</button></div>
        <input name="title" maxlength="180" placeholder="歌名（可留空）">
        <input name="artist" maxlength="160" placeholder="歌手（可留空）">
        <textarea name="note" maxlength="1200" placeholder="想留的一句话（可留空）"></textarea>
      </form>
      <div class="dwell-section-title"><strong>音乐角落</strong><small>${items.length} 张</small></div>
      <div class="dwell-music-list">${items.length ? items.map(musicRow).join('') : '<div class="dwell-empty"><strong>还没有歌留在这里</strong><span>贴一条链接，它会变成可以直接点开的卡片。</span></div>'}</div>`;
    $('#dwell-music-form')?.addEventListener('submit', async (e) => {
      e.preventDefault(); const fd = new FormData(e.currentTarget);
      const next = await api('/api/dwell/music', {method:'POST', body:{action:'add',url:fd.get('url'),title:fd.get('title'),artist:fd.get('artist'),note:fd.get('note')}});
      renderMusicSheet(next.items || []); loadSummary();
    });
    $$('[data-music-edit]', body).forEach(btn => btn.addEventListener('click', async () => {
      const item = items.find((value) => String(value.id) === String(btn.dataset.musicEdit));
      if (!item) return;
      const nextUrl = prompt('歌曲 / 专辑链接', item.url || '');
      if (nextUrl === null) return;
      const nextTitle = prompt('歌名', item.title || '');
      if (nextTitle === null) return;
      const nextArtist = prompt('歌手', item.artist || '');
      if (nextArtist === null) return;
      const nextNote = prompt('想留的一句话', item.note || '');
      if (nextNote === null) return;
      const next = await api(`/api/dwell/music/${encodeURIComponent(item.id)}`, {
        method:'PATCH', body:{url:nextUrl,title:nextTitle,artist:nextArtist,note:nextNote}
      });
      renderMusicSheet(next.items || []); loadSummary();
    }));
    $$('[data-music-del]', body).forEach(btn => btn.addEventListener('click', async () => {
      if (!confirm('删除这张音乐卡片？')) return;
      const next = await api('/api/dwell/music', {method:'POST',body:{action:'del',id:btn.dataset.musicDel}});
      renderMusicSheet(next.items || []); loadSummary();
    }));
  }

  function musicRow(item) {
    return `<article class="dwell-music-card"><div class="dwell-music-mark">♪</div><div class="dwell-row-main"><span class="dwell-card-kicker">${esc(musicProvider(item.url))}</span><strong>${esc(item.title || '一张音乐卡片')}</strong><small>${esc(item.artist || '没有写歌手')}</small>${item.note?`<p>${esc(item.note)}</p>`:''}</div><div class="dwell-music-actions"><a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">打开 ↗</a><button type="button" data-music-edit="${esc(item.id)}">编辑</button><button type="button" data-music-del="${esc(item.id)}">删除</button></div></article>`;
  }

  async function showHeartbeat() {
    openSheet('心跳', '不新建第二套主动系统；这里直接控制我们已经存在的同窗口主动共处。');
    const data = await api(withSession('/api/co-presence/state'));
    const s = data.settings || {};
    body.innerHTML = `
      <div class="dwell-switch-row"><div><strong>允许独立主动联系</strong><p class="dwell-note">沿用现有同窗口链路和现有安静判断。</p></div><label><input id="dwell-heartbeat-toggle" type="checkbox" ${s.independent_initiative_enabled!==false?'checked':''}></label></div>
      <div class="dwell-section-title"><strong>现在</strong><small>${esc(data.status || '安静共处')}</small></div>
      <div class="dwell-stat-grid">
        <div class="dwell-stat"><small>总开关</small><strong>${s.enabled!==false?'开':'关'}</strong></div>
        <div class="dwell-stat"><small>自然续话</small><strong>${s.natural_continuation_enabled!==false?'开':'关'}</strong></div>
      </div>
      <p class="dwell-note">这个卡片只是把已有功能搬进生活空间；没有新增后台线程，也没有新增自动消息来源。</p>`;
    $('#dwell-heartbeat-toggle').addEventListener('change', async (e) => {
      await api('/api/co-presence/settings',{method:'PATCH',body:{independent_initiative_enabled:e.currentTarget.checked}});
      loadSummary();
    });
  }

  async function showHealth() {
    openSheet('身体与状态', '这里先把 jtyhome 已经存在的生活状态和屏幕时间放进同一套页面，不伪造手表数据。');
    const [living, screen] = await Promise.all([
      api('/api/living/state').catch(()=>({})),
      api('/api/screen/summary?hours=24').catch(()=>({})),
    ]);
    const activity = living.activity?.label || '安静待着';
    const phase = living.phase?.label || living.phase || '读取中';
    const screenText = screen.total_minutes != null ? `${Math.round(Number(screen.total_minutes)||0)} 分钟` : (screen.total_seconds != null ? `${Math.round(Number(screen.total_seconds||0)/60)} 分钟` : '暂无记录');
    body.innerHTML = `<div class="dwell-stat-grid"><div class="dwell-stat"><small>${esc(companion)}此刻</small><strong>${esc(activity)}</strong></div><div class="dwell-stat"><small>生活阶段</small><strong>${esc(phase)}</strong></div><div class="dwell-stat"><small>近 24h 屏幕</small><strong>${esc(screenText)}</strong></div><div class="dwell-stat"><small>数据来源</small><strong>现有系统</strong></div></div><p class="dwell-note">这里不会把没有接入的数据装成“手表实测”。以后如果要接手机健康快捷指令，可以单独加真实同步入口。</p>`;
  }

  async function showDaily() {
    openSheet('今日小报', '把今天家里已经存在的东西排成一页；打开不会额外调用模型。');
    const [sum, cal, diary, home] = await Promise.all([
      api(withSession('/api/dwell/summary')), api('/api/dwell/calendar'), api('/api/dwell/diary'), api('/api/home').catch(()=>({}))
    ]);
    const today = localYMD();
    const todayCal = cal.items.filter(x=>x.date===today);
    const recentDiary = diary.items[0];
    const chapter = home.relationship?.chapter?.title || home.chapter?.title || home.chapter || '正在接续';
    body.innerHTML = `<article class="dwell-paper"><header><span class="dwell-eyebrow">JTYHOME DAILY · ${esc(today)}</span><h3>今天，家里有什么</h3><small>${esc(companion)}和你</small></header><section><strong>今天的日程</strong>${todayCal.length?`<p>${todayCal.map(x=>esc(x.title)).join(' · ')}</p>`:'<p>今天没有写在日历上的事。</p>'}</section><section><strong>清单</strong><p>你的 ${Number(sum.todos?.hers_open||0)} 件还没做；${esc(companion)}的 ${Number(sum.todos?.mine_open||0)} 件还挂着。</p></section><section><strong>最近一页日记</strong><p>${recentDiary?esc(recentDiary.text).slice(0,260):'本子今天还是空的。'}</p></section><section><strong>我们这一章</strong><p>${esc(chapter)}</p></section></article>`;
  }

  const handlers = { diary:showDiary, whispers:showWhispers, todos:showTodos, calendar:showCalendar, reading:showReading, music:showMusic, health:showHealth, heartbeat:showHeartbeat, daily:showDaily };

  function bind() {
    $('#dwell-life-dialog-close')?.addEventListener('click', closeSheet);
    dialog?.addEventListener('click', (e) => { if (e.target === dialog) closeSheet(); });
    $$('[data-dwell-open]').forEach((btn) => btn.addEventListener('click', async () => {
      try { await handlers[btn.dataset.dwellOpen]?.(); } catch (error) { body.innerHTML=`<div class="dwell-empty"><strong>没打开</strong><span>${esc(error.message)}</span></div>`; }
    }));
    $$('[data-dwell-system]').forEach((btn) => btn.addEventListener('click', () => {
      const selector = btn.dataset.dwellSystem;
      const direct = {
        '#file-workspace-section': ['system', 'data'],
        '#model-hub-section': ['system', 'settings'],
        '#media-section': ['system', 'tools'],
        '#ocean-listen-section': ['system', 'tools'],
        '#voice-section': ['system', 'tools'],
        '#living-section': ['system', 'settings'],
        '#appearance-section': ['system', 'settings'],
      }[selector];
      if (direct && window.JTYUI83) {
        $('#btn-system')?.click();
        requestAnimationFrame(() => {
          window.JTYUI83?.system?.activate?.(direct[1]);
          requestAnimationFrame(() => $(selector)?.scrollIntoView({behavior:'auto',block:'start'}));
        });
        return;
      }
      $('#btn-system')?.click();
      requestAnimationFrame(() => $(selector)?.scrollIntoView({behavior:'auto',block:'start'}));
    }));
    const home = $('#view-home');
    if (home) new MutationObserver(() => { if (home.classList.contains('active')) loadSummary(); }).observe(home,{attributes:true,attributeFilter:['class']});

    // Dwell 的音乐卡片直接接到原聊天渲染层：模型或用户发出受支持的
    // 音乐链接后，不需要第二套消息系统，原链接就在现有气泡里变成卡片。
    const messages = $('#messages');
    if (messages) {
      decorateMusicLinks(messages);
      new MutationObserver((mutations) => {
        for (const mutation of mutations) {
          mutation.addedNodes.forEach((node) => {
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            if (node.matches?.('.message, .bubble, a[href]')) decorateMusicLinks(messages);
            else if (node.querySelector?.('a[href]')) decorateMusicLinks(messages);
          });
        }
      }).observe(messages, {childList:true, subtree:true});
    }

    // 在生活页停留时切换聊天窗口，悄悄话计数也跟着当前 session 更新。
    const sessionList = $('#session-list');
    if (sessionList) {
      let sessionRefreshTimer = null;
      new MutationObserver(() => {
        if (!home?.classList.contains('active')) return;
        clearTimeout(sessionRefreshTimer);
        sessionRefreshTimer = setTimeout(loadSummary, 80);
      }).observe(sessionList, {subtree:true, childList:true, attributes:true, attributeFilter:['class']});
    }
    loadSummary();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind, {once:true}); else bind();
  window.DwellLife = { loadSummary, decorateMusicLinks, open: async (kind) => { const fn = handlers[kind]; if (!fn) return false; try { await fn(); return true; } catch (error) { showMutationError(error); return false; } } };
})();
