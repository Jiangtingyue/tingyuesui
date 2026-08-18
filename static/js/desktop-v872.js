
(() => {
  'use strict';

  const $=(s,r=document)=>r.querySelector(s);
  const $$=(s,r=document)=>Array.from(r.querySelectorAll(s));
  const state={timer:0,musicArtworkKey:''};

  function sessionId(){
    return new URLSearchParams(location.search).get('session')||'';
  }
  function withSession(path){
    const sid=sessionId();
    if(!sid)return path;
    return `${path}${path.includes('?')?'&':'?'}session_id=${encodeURIComponent(sid)}`;
  }
  let browserSessionRepair=null;
  async function repairBrowserSession(){
    if(!['127.0.0.1','localhost','::1'].includes(location.hostname))return false;
    if(!browserSessionRepair){
      browserSessionRepair=fetch(`/?_jty_browser_boot=${Date.now()}`,{cache:'no-store',credentials:'same-origin'})
        .then(r=>r.ok).catch(()=>false).finally(()=>{browserSessionRepair=null});
    }
    return browserSessionRepair;
  }
  async function api(path){
    const opt={cache:'no-store',credentials:'same-origin'};
    let r=await fetch(path,opt),d=await r.json().catch(()=>({}));
    if(r.status===401&&d?.code==='PAIRING_REQUIRED'&&await repairBrowserSession()){
      r=await fetch(path,opt);d=await r.json().catch(()=>({}));
    }
    if(!r.ok)throw new Error(d.detail||d.error||r.status);
    return d;
  }
  function compact(v,n=150){
    const s=String(v||'').replace(/\s+/g,' ').trim();
    return s.length>n?`${s.slice(0,n).trim()}…`:s;
  }
  function openExisting(kind){
    if(window.DwellLife?.open){
      window.DwellLife.open(kind);
      return true;
    }
    return false;
  }
  const MUSIC_FALLBACK={
    a:[155,166,176], b:[202,180,168], c:[174,184,188]
  };
  function clampByte(n){return Math.max(0,Math.min(255,Math.round(Number(n)||0)))}
  function rgbCss(rgb,alpha=1){return `rgba(${clampByte(rgb[0])},${clampByte(rgb[1])},${clampByte(rgb[2])},${alpha})`}
  function luminance(rgb){return (.2126*rgb[0]+.7152*rgb[1]+.0722*rgb[2])/255}
  function saturation(rgb){
    const max=Math.max(...rgb),min=Math.min(...rgb);
    return max?((max-min)/max):0;
  }
  function distance(a,b){return Math.hypot(a[0]-b[0],a[1]-b[1],a[2]-b[2])}
  function darken(rgb,f=.28){return rgb.map(v=>clampByte(v*f))}
  function soften(rgb,white=.10){return rgb.map(v=>clampByte(v*(1-white)+255*white))}
  function paletteFromImage(img){
    const canvas=document.createElement('canvas');
    canvas.width=36;canvas.height=36;
    const ctx=canvas.getContext('2d',{willReadFrequently:true});
    if(!ctx)return MUSIC_FALLBACK;
    try{ctx.drawImage(img,0,0,36,36)}catch(_){return MUSIC_FALLBACK}
    let data;
    try{data=ctx.getImageData(0,0,36,36).data}catch(_){return MUSIC_FALLBACK}
    const buckets=new Map();
    for(let i=0;i<data.length;i+=4){
      if(data[i+3]<210)continue;
      const rgb=[data[i],data[i+1],data[i+2]];
      const lum=luminance(rgb),sat=saturation(rgb);
      if(lum<.06||lum>.94)continue;
      const q=rgb.map(v=>Math.round(v/28)*28);
      const key=q.join(',');
      const prev=buckets.get(key)||{rgb:q,count:0,score:0};
      prev.count+=1;
      // Saturated colours matter more, but muted album covers still retain their palette.
      prev.score+=.55+sat*1.55+(lum>.16&&lum<.84?.18:0);
      buckets.set(key,prev);
    }
    const ranked=[...buckets.values()].sort((x,y)=>(y.score*y.count)-(x.score*x.count));
    const chosen=[];
    for(const item of ranked){
      if(chosen.every(c=>distance(c,item.rgb)>72))chosen.push(item.rgb);
      if(chosen.length===3)break;
    }
    if(!chosen.length)return MUSIC_FALLBACK;
    while(chosen.length<3){
      const base=chosen[chosen.length-1]||chosen[0];
      chosen.push(soften(base,chosen.length===1?.24:.12));
    }
    return {a:chosen[0],b:chosen[1],c:chosen[2]};
  }
  function applyMusicPalette(shell,palette){
    if(!shell)return;
    const {a,b,c}=palette||MUSIC_FALLBACK;
    shell.style.setProperty('--music-a',rgbCss(soften(a,.05),.62));
    shell.style.setProperty('--music-a-soft',rgbCss(soften(a,.18),.26));
    shell.style.setProperty('--music-b',rgbCss(soften(b,.04),.52));
    shell.style.setProperty('--music-b-soft',rgbCss(soften(b,.20),.24));
    shell.style.setProperty('--music-c',rgbCss(soften(c,.06),.58));
    shell.style.setProperty('--music-c-soft',rgbCss(soften(c,.18),.24));
    shell.style.setProperty('--music-dark-a',rgbCss(darken(a,.24),.88));
    shell.style.setProperty('--music-dark-b',rgbCss(darken(b,.18),.82));
    shell.style.setProperty('--music-edge',rgbCss(soften(a,.56),.52));
  }
  function resetMusicArtwork(){
    const shell=$('.v872-music');
    const cover=$('#v872-music-cover');
    const wrap=cover?.closest('.v872-cover');
    state.musicArtworkKey='';
    applyMusicPalette(shell,MUSIC_FALLBACK);
    if(cover){cover.removeAttribute('src');cover.hidden=true}
    if(wrap)wrap.classList.remove('has-artwork');
  }
  function applyMusicArtwork(music){
    const shell=$('.v872-music');
    const cover=$('#v872-music-cover');
    const wrap=cover?.closest('.v872-cover');
    const musicUrl=String(music?.url||'').trim();
    if(!shell||!cover||!wrap||!musicUrl){resetMusicArtwork();return}
    if(state.musicArtworkKey===musicUrl&&cover.getAttribute('src'))return;
    state.musicArtworkKey=musicUrl;
    applyMusicPalette(shell,MUSIC_FALLBACK);
    wrap.classList.remove('has-artwork');
    cover.hidden=false;
    cover.onload=()=>{
      if(state.musicArtworkKey!==musicUrl)return;
      wrap.classList.add('has-artwork');
      applyMusicPalette(shell,paletteFromImage(cover));
    };
    cover.onerror=()=>{
      if(state.musicArtworkKey!==musicUrl)return;
      cover.hidden=true;
      wrap.classList.remove('has-artwork');
      applyMusicPalette(shell,MUSIC_FALLBACK);
    };
    cover.src=`/api/dwell/music-artwork?url=${encodeURIComponent(musicUrl)}`;
  }
  function monthName(d){return new Intl.DateTimeFormat('en-US',{month:'long'}).format(d)}
  function weekday(d){return new Intl.DateTimeFormat('en-US',{weekday:'short'}).format(d)}

  function calendarMarkup(){
    const now=new Date();
    const dow=now.getDay();
    const monday=new Date(now);
    monday.setHours(12,0,0,0);
    monday.setDate(now.getDate()-((dow+6)%7));
    const days=[];
    for(let i=0;i<7;i++){
      const d=new Date(monday);d.setDate(monday.getDate()+i);
      days.push(`<div class="v872-day ${d.toDateString()===now.toDateString()?'today':''}"><span>${weekday(d)}</span><b>${d.getDate()}</b></div>`);
    }
    return `<section class="v872-widget v872-calendar v872-glass-target" data-v872-open="calendar" role="button" tabindex="0" aria-label="本周日历，只读展示">
      <span class="v872-calendar-tint" aria-hidden="true"></span>
      <div class="v872-calendar-top"><div class="v872-calendar-segment" aria-hidden="true"><span class="on">Weekly</span><span>Monthly</span></div><span class="v872-calendar-gear" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 8.2a3.8 3.8 0 1 0 0 7.6 3.8 3.8 0 0 0 0-7.6Zm8.3 4.7v-1.8l-2.2-.7a6.8 6.8 0 0 0-.7-1.6l1.1-2-1.3-1.3-2 1.1a6.8 6.8 0 0 0-1.6-.7L12.9 3h-1.8l-.7 2.2a6.8 6.8 0 0 0-1.6.7l-2-1.1-1.3 1.3 1.1 2a6.8 6.8 0 0 0-.7 1.6l-2.2.7v1.8l2.2.7c.2.6.4 1.1.7 1.6l-1.1 2 1.3 1.3 2-1.1c.5.3 1 .5 1.6.7l.7 2.2h1.8l.7-2.2c.6-.2 1.1-.4 1.6-.7l2 1.1 1.3-1.3-1.1-2c.3-.5.5-1 .7-1.6l2.2-.7Z" fill="currentColor"/></svg></span></div>
      <div class="v872-calendar-date"><strong>${monthName(now)}</strong><b>${now.getDate()}</b></div>
      <div class="v872-calendar-arc"><div class="v872-week">${days.join('')}</div></div>
      <div class="v872-calendar-foot"><span>✎ &nbsp; ${new Intl.DateTimeFormat('zh-CN',{month:'long',day:'numeric'}).format(now)} · 今天</span><strong>＋ New Event</strong></div>
    </section>`;
  }

  function lifeMarkup(){
    const companion=document.documentElement.dataset.companionName||'他';
    return `<div class="v872-home-life">
      <section class="v872-home-memory" aria-label="记忆星图">
        <button class="v872-home-memory-stage v872-glass-target" id="v872-open-memory-atlas" type="button" aria-label="打开完整记忆星图">
          <img src="/static/images/memory-star-atlas-v897.svg" alt="" draggable="false" decoding="async">
          <span class="v872-home-memory-nodes" id="v872-home-memory-nodes" aria-hidden="true"></span>
          <span class="v872-home-memory-glint" aria-hidden="true"></span>
        </button>
        <div class="v872-home-memory-meta">
          <div><small>MEMORY COSMOS</small><strong>记忆星图</strong></div>
          <p id="v872-home-memory-detail">真实记忆正在落回各自的轨道。</p>
          <span id="v872-home-memory-count">读取中</span>
        </div>
      </section>
      <header class="v872-home-life-title v872-glass-target">
        <div><small>HOME · TODAY</small><h2>今天就在这里</h2><p>记忆在上面缓慢运行；日历、日记、待办、共读和音乐按它们真正的形状留在下面。</p></div>
        <span class="v872-home-life-date" id="v872-home-life-date">TODAY</span>
      </header>

      <section class="v872-widget v872-music v872-glass-target" aria-label="一起听音乐">
        <button type="button" data-v872-music="prev" aria-label="上一首">◀</button>
        <button type="button" data-v872-music="play" aria-label="播放或暂停">Ⅱ</button>
        <button type="button" data-v872-music="next" aria-label="下一首">▶</button>
        <div class="v872-now">
          <div class="v872-cover" aria-hidden="true"><img id="v872-music-cover" alt="" decoding="async"><span>♪</span></div>
          <div class="v872-now-copy"><strong id="v872-music-title">还没有音乐卡片</strong><span id="v872-music-artist">把一首歌留在这里</span><div class="v872-track"><i></i></div></div>
          <span class="v872-wave" aria-hidden="true">▥</span>
        </div>
        <button type="button" data-v872-music="route" aria-label="播放设备">◉</button>
        <button type="button" data-v872-music="settings" aria-label="音乐设置">☷</button>
        <button type="button" data-v872-music="volume" aria-label="音量">◕</button>
      </section>

      <div class="v872-home-life-canvas">
        ${calendarMarkup()}

        <button class="v872-widget v872-todo-board" id="v872-open-todos" type="button" data-v872-open="todos">
          <span class="v872-todo-pin" aria-hidden="true"></span>
          <header><div><small>TO-DO · TOGETHER</small><h3>今天要做的事</h3></div><strong id="v872-todo-open">0</strong></header>
          <div class="v872-todo-rule"></div>
          <ul id="v872-todo-preview" class="v872-todo-preview"><li class="empty">今天还没有挂上待办。</li></ul>
          <footer><span>你的 <b id="v872-todo-hers">0</b></span><span>${compact(companion,18)}的 <b id="v872-todo-mine">0</b></span><em>打开双人清单 ↗</em></footer>
        </button>

        <article class="v872-widget v872-journal">
          <div class="v872-journal-head"><span id="v872-journal-date">AUG 07</span><span id="v872-journal-year">2025</span></div>
          <h3># A Note to Myself Today</h3>
          <div class="v872-journal-line"><small>今天留下</small><p id="v872-journal-primary">今天还没有留下新的文字。</p></div>
          <div class="v872-journal-line"><small>一起记得</small><p id="v872-journal-secondary">把今天发生的小事留在这里。</p></div>
          <button class="v872-journal-open" id="v872-open-diary" type="button">一起写日记 ↗</button>
        </article>

        <section class="v872-widget v872-reading">
          <div class="v872-reading-stack">
            <div class="v872-reading-sheet s1"><p>读过的句子会一页页留在后面。</p></div>
            <div class="v872-reading-sheet s2"><p>一起读书，不需要再经过第二层目录。</p></div>
            <div class="v872-reading-sheet s3"><p id="v872-reading-echo">书架还空着。</p></div>
            <button class="v872-reading-front" id="v872-open-reading" type="button">
              <div class="v872-reading-copy"><span class="eyebrow">READING TOGETHER</span><h3 id="v872-book-title">书架还空着</h3><p id="v872-book-author">放上第一本一起读的书</p></div>
              <div class="v872-reading-art" aria-hidden="true"><span>${monthName(new Date()).slice(0,3)}</span><strong>${new Date().getDate()}</strong><i></i></div>
              <span class="v872-reading-progress" aria-hidden="true"><i id="v872-book-progress"></i></span>
            </button>
          </div>
        </section>
      </div>

      <section class="v897-life-specials" aria-labelledby="v897-life-specials-title">
        <header class="v897-life-specials-head">
          <div><small>LITTLE THINGS · AT HOME</small><h2 id="v897-life-specials-title">生活的小抽屉</h2></div>
          <p>四件小物各有自己的材质和尺寸，打开后回到清楚、安静的生活页面。</p>
        </header>
        <div class="v872-life-actions" aria-label="生活快捷入口">
          <button class="v897-life-card v897-whisper-card" type="button" data-v872-open="whispers" aria-label="打开悄悄话">
            <span class="v897-card-head"><small>PRIVATE NOTE</small><em id="v872-whispers">0 条</em></span>
            <span class="v897-envelope" aria-hidden="true"><i class="v897-envelope-flap"></i><i class="v897-envelope-letter"></i><b>月</b></span>
            <span class="v897-card-copy"><strong>悄悄话</strong><small>只放进当前聊天窗口的信封</small></span>
          </button>
          <button class="v897-life-card v897-body-card" type="button" data-v872-open="health" aria-label="打开身体与状态">
            <span class="v897-card-head"><small>BODY LOG</small><em>24H</em></span>
            <span class="v897-body-chart" aria-hidden="true"><i class="head"></i><i class="body"></i><span><b></b><b></b><b></b><b></b><b></b></span></span>
            <span class="v897-card-copy"><strong>身体</strong><small>生活状态与屏幕时间的体检夹</small></span>
          </button>
          <button class="v897-life-card v897-heart-card" type="button" data-v872-open="heartbeat" aria-label="打开心跳与主动共处">
            <span class="v897-card-head"><small>CO-PRESENCE</small><em>LIVE</em></span>
            <span class="v897-heart-monitor" aria-hidden="true"><i></i><svg viewBox="0 0 240 72" preserveAspectRatio="none"><polyline points="0,39 45,39 58,34 70,39 91,39 103,15 119,60 133,31 147,39 240,39"/></svg><b>♥</b></span>
            <span class="v897-card-copy"><strong>心跳</strong><small id="dwell-heartbeat-note">主动共处正在使用现有链路</small></span>
          </button>
          <button class="v897-life-card v897-daily-card" type="button" data-v872-open="daily" aria-label="打开今日小报">
            <span class="v897-card-head"><small>JTYHOME DAILY</small><em>今日</em></span>
            <span class="v897-newspaper" aria-hidden="true"><strong>家 里 小 报</strong><i></i><i></i><i></i><i></i><b>今天发生的，都排在这一页</b></span>
            <span class="v897-card-copy"><strong>今日小报</strong><small>日程、清单、日记与章节</small></span>
          </button>
          <div class="v872-memory-bridge-status" title="信件、日记和待办会进入长期记忆检索">
            <span>记忆桥</span><small id="v872-memory-bridge">同步中…</small>
          </div>
        </div>
      </section>
    </div>`;
  }

  function stableMemoryHash(value){
    let h=5381;
    for(const ch of String(value??''))h=((h*33)+ch.charCodeAt(0))>>>0;
    return h>>>0;
  }
  async function refreshHomeMemoryAtlas(){
    const layer=$('#v872-home-memory-nodes');
    const count=$('#v872-home-memory-count');
    const detail=$('#v872-home-memory-detail');
    if(!layer)return;
    let rows=[];
    try{rows=await api('/api/memory/emotion-map')}catch(_){rows=[]}
    rows=Array.isArray(rows)?rows:[];
    layer.innerHTML='';
    const centerX=49.3,centerY=48.1,aspect=1672/941;
    const sorted=[...rows].sort((a,b)=>Number(b.importance||b.decay_score||0)-Number(a.importance||a.decay_score||0));
    const visible=sorted.slice(0,18);
    if(count)count.textContent=rows.length>visible.length?`${rows.length} 条 · ${visible.length} 颗星`:`${rows.length} 条记忆`;
    const placed=[];
    visible.forEach((v,i)=>{
      const val=Math.max(-1,Math.min(1,Number(v.valence||0)));
      const aro=Math.max(0,Math.min(1,Number(v.arousal||0)));
      const imp=Math.max(0,Math.min(1,Number(v.importance||v.decay_score||.3)));
      const seed=stableMemoryHash(`${v.id||i}|${v.domain||''}|${String(v.content||'').slice(0,80)}`);
      const band=Math.max(0,Math.min(4,Math.round((1-imp)*4)));
      let radius=[7.8,12.8,17.9,23.0,28.1][band]+(((seed>>>7)%1000)/1000-.5)*5.2;
      let angle=((seed%100000)/100000)*Math.PI*2+val*.18+aro*.11;
      let left=centerX,top=centerY;
      for(let attempt=0;attempt<14;attempt++){
        left=Math.max(3,Math.min(97,centerX+Math.cos(angle)*radius));
        top=Math.max(3,Math.min(97,centerY+Math.sin(angle)*radius*aspect));
        if(!placed.some(q=>Math.hypot((left-q[0])*.70,top-q[1])<2.55))break;
        angle+=2.399963229728653;
        radius+=((attempt%3)-1)*.6;
      }
      placed.push([left,top]);
      const star=document.createElement('i');
      star.className='v872-home-memory-node';
      if(!v.resolved&&imp>.72)star.classList.add('hot');
      star.style.left=`${left}%`;star.style.top=`${top}%`;
      const size=1.25+imp*1.65;star.style.width=`${size.toFixed(2)}px`;star.style.height=`${size.toFixed(2)}px`;
      star.style.opacity=`${(.45+imp*.45).toFixed(2)}`;
      star.dataset.content=compact(v.content||'一条记忆',70);
      layer.appendChild(star);
    });
    if(detail){
      const first=sorted[0];
      detail.textContent=first?`最近最亮的一颗：${compact(first.content||'一条记忆',58)}`:'还没有记忆节点，底图先安静待着。';
    }
  }
  function openMemoryAtlas(){
    $('#btn-memory')?.click();
    setTimeout(()=>window.JTYUI83?.memory?.activate?.('map'),80);
  }

  function mountLife(){
    const host=$('#v872-home-life-host');
    if(!host||host.querySelector('.v872-home-life'))return;
    host.innerHTML=lifeMarkup();
    const board=host.firstElementChild;
    $('#v872-open-memory-atlas',board)?.addEventListener('click',openMemoryAtlas);
    refreshHomeMemoryAtlas();
    $('#v872-open-diary',board)?.addEventListener('click',()=>openExisting('diary'));
    $('#v872-open-reading',board)?.addEventListener('click',()=>openExisting('reading'));
    $$('[data-v872-open]',board).forEach(b=>{
      const open=()=>openExisting(b.dataset.v872Open);
      b.addEventListener('click',open);
      if(b.getAttribute('role')==='button')b.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open()}});
    });
    $$('[data-v872-music]',board).forEach(b=>b.addEventListener('click',()=>openExisting('music')));
    window.dispatchEvent(new CustomEvent('daxigua:glass-targets-change'));
  }

  function renderTodoPreview(data){
    const host=$('#v872-todo-preview');
    if(!host)return;
    const hers=(data?.hers||[]).filter(x=>!x.done).map(x=>({...x,side:'hers'}));
    const mine=(data?.mine||[]).filter(x=>!x.done).map(x=>({...x,side:'mine'}));
    const items=[...hers,...mine].sort((a,b)=>{
      if(Boolean(a.at)!==Boolean(b.at))return a.at?-1:1;
      if(a.at&&b.at&&a.at!==b.at)return a.at.localeCompare(b.at);
      return Number(a.made||0)-Number(b.made||0);
    }).slice(0,4);
    host.replaceChildren();
    if(!items.length){
      const li=document.createElement('li');li.className='empty';li.textContent='今天还没有挂上待办。';host.appendChild(li);return;
    }
    for(const item of items){
      const li=document.createElement('li');li.dataset.side=item.side;
      const dot=document.createElement('i');
      const text=document.createElement('span');text.textContent=compact(item.text,52);
      const time=document.createElement('small');time.textContent=item.at|| (item.side==='hers'?'你':'TA');
      li.append(dot,text,time);host.appendChild(li);
    }
  }

  async function refreshLife(){
    mountLife();
    try{
      const [sum,todos,bridge]=await Promise.all([
        api(withSession('/api/dwell/summary')),
        api('/api/dwell/todos').catch(()=>({hers:[],mine:[]})),
        api('/api/memory/life-bridge-status').catch(()=>null),
      ]);
      const p=sum.previews||{};
      const diary=p.recent_diary||null;
      const whisper=p.recent_whisper||null;
      const recent=Number(whisper?.at||0)>Number(diary?.at||0)?whisper:diary;
      const secondary=recent===diary?whisper:diary;
      const book=p.current_book||null;
      const music=p.current_music||null;
      const hersOpen=Number(sum.todos?.hers_open||0),mineOpen=Number(sum.todos?.mine_open||0);
      const open=hersOpen+mineOpen;
      const now=new Date();

      $('#v872-home-life-date')&&($('#v872-home-life-date').textContent=new Intl.DateTimeFormat('zh-CN',{month:'long',day:'numeric',weekday:'short'}).format(now));
      $('#v872-journal-date')&&($('#v872-journal-date').textContent=new Intl.DateTimeFormat('en-US',{month:'short',day:'2-digit'}).format(now).toUpperCase());
      $('#v872-journal-year')&&($('#v872-journal-year').textContent=String(now.getFullYear()));
      $('#v872-journal-primary')&&($('#v872-journal-primary').textContent=compact(recent?.text)||'今天还没有留下新的文字。');
      $('#v872-journal-secondary')&&($('#v872-journal-secondary').textContent=compact(secondary?.text,100)||(recent?'把这句话继续写下去。':'把今天发生的小事留在这里。'));

      const progress=Math.max(0,Math.min(100,Number(book?.progress||0)));
      $('#v872-book-title')&&($('#v872-book-title').textContent=book?.title||'书架还空着');
      $('#v872-book-author')&&($('#v872-book-author').textContent=book?`${book.author||'没有写作者'} · ${progress}%`:'放上第一本一起读的书');
      $('#v872-reading-echo')&&($('#v872-reading-echo').textContent=book?`《${book.title||'这本书'}》已经读到 ${progress}% 。`:'还没有一起读的书。');
      $('#v872-book-progress')&&($('#v872-book-progress').style.width=`${progress}%`);

      $('#v872-music-title')&&($('#v872-music-title').textContent=music?.title||'还没有音乐卡片');
      $('#v872-music-artist')&&($('#v872-music-artist').textContent=music?.artist||'把一首歌留在这里');
      applyMusicArtwork(music);

      $('#v872-todo-open')&&($('#v872-todo-open').textContent=String(open));
      $('#v872-todo-hers')&&($('#v872-todo-hers').textContent=String(hersOpen));
      $('#v872-todo-mine')&&($('#v872-todo-mine').textContent=String(mineOpen));
      renderTodoPreview(todos);
      $('#v872-whispers')&&($('#v872-whispers').textContent=`${Number(sum.whisper_count||0)} 条`);
      if(bridge&&$('#v872-memory-bridge')){
        const parts=[`信 ${bridge.letters||0}`,`日记 ${bridge.diary||0}`,`待办 ${bridge.todos||0}`];
        $('#v872-memory-bridge').textContent=parts.join(' · ');
      }
      refreshHomeMemoryAtlas().catch(()=>{});
    }catch(_){ }
  }

  async function mountHomeArtifacts(){
    const specs=[
      {id:'v872-artifact-fbi', selector:'#filesScene .clipboard', kind:'fbi'},
      {id:'v872-artifact-certificate', selector:'#certBook', kind:'book'},
      {id:'v872-artifact-passport', selector:'#passportBook', kind:'book'}
    ];
    if(specs.every(x=>document.getElementById(x.id)?.shadowRoot))return;
    let sourceText='';
    try{
      const res=await fetch('/static/keepsakes/birthday-bunny.html',{cache:'force-cache',credentials:'same-origin'});
      if(!res.ok)throw new Error(`birthday archive ${res.status}`);
      sourceText=await res.text();
    }catch(err){
      specs.forEach(x=>{
        const host=document.getElementById(x.id);
        if(host&&!host.shadowRoot)host.textContent='原件暂时没有载入';
      });
      return;
    }
    const sourceDoc=new DOMParser().parseFromString(sourceText,'text/html');
    const sourceCss=Array.from(sourceDoc.querySelectorAll('style')).map(x=>x.textContent||'').join('\n');
    const sourceFont=sourceDoc.querySelector('link[href*="fonts.googleapis.com"]')?.getAttribute('href')||'';
    const hostVars=`
      :host{
        --gold:#c9a227;--gold-dark:#9f7c17;--navy:#0a2647;--navy-deep:#071525;
        --paper:#fffdf8;--cream:#fbf6ec;--ink:#271912;--muted:#6d6258;
        --soft-shadow:0 24px 60px rgba(0,0,0,.28);--wax:#8e1730;--rose:#b86a73;
        --paper-grain:rgba(82,54,24,.035);display:block;width:100%;height:100%;overflow:visible;
        font-size:16px;color:#271912;
      }
      *,*::before,*::after{box-sizing:border-box}
      .v872-extracted-root{
        width:var(--artifact-source-width,520px);
        transform:scale(var(--artifact-scale,1));
        transform-origin:top left;
        position:relative;
      }
      .v872-extracted-root>.clipboard,
      .v872-extracted-root>.mini-book{margin:0!important}
      .v872-extracted-root>.mini-book .book-toggle,
      .v872-extracted-root>.mini-book .book-hint{display:none!important}
    `;
    specs.forEach(spec=>{
      const host=document.getElementById(spec.id);
      if(!host||host.shadowRoot)return;
      const source=sourceDoc.querySelector(spec.selector);
      if(!source){host.textContent='原件结构没有找到';return}
      const shadow=host.attachShadow({mode:'open'});
      const style=document.createElement('style');
      style.textContent=`${sourceFont?`@import url("${sourceFont}");`:''}\n${hostVars}\n${sourceCss}`;
      const root=document.createElement('div');
      root.className='v872-extracted-root';
      root.appendChild(source.cloneNode(true));
      shadow.append(style,root);

      if(spec.kind==='book'){
        const book=shadow.querySelector('.mini-book');
        shadow.querySelectorAll('[data-book]').forEach(el=>el.addEventListener('click',e=>{
          e.preventDefault();
          book?.classList.toggle('open');
        }));
      }
      if(spec.kind==='fbi'){
        const checks=Array.from(shadow.querySelectorAll('.fbi-check input[type="checkbox"]'));
        const fill=shadow.getElementById('fbiProgressFill');
        const txt=shadow.getElementById('fbiProgressText');
        const pct=shadow.getElementById('fbiProgressPercent');
        const update=()=>{
          const done=checks.filter(x=>x.checked).length,total=checks.length||1;
          const percent=Math.round(done/total*100);
          if(fill)fill.style.width=`${percent}%`;
          if(txt)txt.textContent=`${done} boxes verified`;
          if(pct)pct.textContent=`${percent}%`;
        };
        checks.forEach(x=>x.addEventListener('change',update));
        update();
      }
    });
  }

  function bindObjects(){
    $('#v872-open-letter')?.addEventListener('click',()=>$('#v872-letter-dialog')?.showModal?.());
    $('#v872-open-phone')?.addEventListener('click',()=>$('#v872-phone-dialog')?.showModal?.());
    $$('[data-v872-close]').forEach(b=>b.addEventListener('click',()=>document.getElementById(b.dataset.v872Close)?.close?.()));
    $$('.v872-world-dialog').forEach(d=>d.addEventListener('click',e=>{if(e.target===d)d.close?.()}));
  }

  function boot(){
    mountLife();
    mountHomeArtifacts();
    refreshLife();
    bindObjects();
    window.addEventListener('focus',refreshLife);
    document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshLife()});
    clearInterval(state.timer);
    state.timer=setInterval(()=>{if(!document.hidden&&$('#view-home')?.classList.contains('active'))refreshLife()},30000);
  }

  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>setTimeout(boot,90));
  else setTimeout(boot,90);
})();
