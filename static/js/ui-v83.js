(() => {
'use strict';
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let browserSessionRepair=null;
async function repairBrowserSession(){
  if(!['127.0.0.1','localhost','::1'].includes(location.hostname))return false;
  if(!browserSessionRepair){
    browserSessionRepair=fetch(`/?_jty_browser_boot=${Date.now()}`,{cache:'no-store',credentials:'same-origin'})
      .then(r=>r.ok).catch(()=>false).finally(()=>{browserSessionRepair=null});
  }
  return browserSessionRepair;
}
async function api(url,opt={}){
  const o={cache:'no-store',credentials:'same-origin',...opt};
  if(o.body&&typeof o.body!=='string'&&!(o.body instanceof FormData)){o.headers={...(o.headers||{}),'Content-Type':'application/json'};o.body=JSON.stringify(o.body)}
  let r=await fetch(url,o),d=await r.json().catch(()=>({}));
  if(r.status===401&&d?.code==='PAIRING_REQUIRED'&&await repairBrowserSession()){
    r=await fetch(url,o);d=await r.json().catch(()=>({}));
  }
  if(!r.ok)throw new Error(d.detail||d.error||r.status);return d
}
const sid=()=>new URLSearchParams(location.search).get('session')||'';
function intro(k,t,p,a=''){return `<div class="u83-intro"><div><small>${esc(k)}</small><h2>${esc(t)}</h2><p>${esc(p)}</p></div>${a?`<div class="u83-toolbar">${a}</div>`:''}</div>`}
function deck(view,name,tabs,def){if(!view||view.querySelector(`.u83-tabs[data-space="${name}"]`))return null;const c=view.querySelector('.page-scroll,.console-content')||view,h=c.querySelector(':scope > .system-hero,:scope > .inner-hero,:scope > .console-hero,:scope > .memory-hero');const n=document.createElement('nav');n.className='u83-tabs';n.dataset.space=name;n.innerHTML=tabs.map(x=>`<button type="button" data-u83-tab="${x[0]}">${esc(x[1])}</button>`).join('');const d=document.createElement('div');d.className='u83-deck';tabs.forEach(x=>{const p=document.createElement('section');p.className='u83-panel';p.dataset.u83Panel=x[0];d.appendChild(p)});(h?h.insertAdjacentElement('afterend',n):c.prepend(n));n.insertAdjacentElement('afterend',d);const activate=(key,persist=true)=>{if(!tabs.some(x=>x[0]===key))key=def;$$('button',n).forEach(b=>b.classList.toggle('active',b.dataset.u83Tab===key));$$('.u83-panel',d).forEach(p=>p.classList.toggle('active',p.dataset.u83Panel===key));if(persist)try{localStorage.setItem(`jtyhome:u83:${name}`,key)}catch(_){}window.dispatchEvent(new CustomEvent('jty:u83',{detail:{space:name,panel:key}}))};n.addEventListener('click',e=>{const b=e.target.closest('[data-u83-tab]');if(b)activate(b.dataset.u83Tab)});let saved='';try{saved=localStorage.getItem(`jtyhome:u83:${name}`)||''}catch(_){}activate(saved||def,false);return{nav:n,panel:k=>d.querySelector(`[data-u83-panel="${k}"]`),activate}}
const move=(el,to)=>{if(el&&to)to.appendChild(el)};

function sfxPanel(){
  const e=document.createElement('section');
  e.id='u83-sfx-panel';
  e.className='u83-card wide';
  e.innerHTML=`
    <div class="u83-head">
      <div>
        <small>LOCAL SFX · USER LIBRARY</small>
        <h3>消息环境音</h3>
        <p>安装包不再内置任何音效。你导入的音频只保存在当前浏览器的 IndexedDB，不上传服务器，也不进入模型上下文或缓存。</p>
      </div>
      <span id="u83-sfx-status" class="u83-badge">素材库为空</span>
    </div>
    <div class="u83-sfx-master">
      <div>
        <strong>自动环境音</strong>
        <small>开启后，回复命中对应场景时只从你自己的本地素材库中随机播放。</small>
      </div>
      <button id="u83-sfx-master" class="u83-switch" type="button" aria-pressed="false"></button>
    </div>
    <div class="u83-controls">
      <label>强度<select id="u83-sfx-intensity"><option value="subtle">轻微</option><option value="natural">自然</option><option value="obvious">明显</option></select></label>
      <label>最多叠层<select id="u83-sfx-density"><option value="1">1 层</option><option value="2">2 层</option><option value="3">3 层</option></select></label>
      <label>音量<select id="u83-sfx-volume"><option value="0.35">35%</option><option value="0.55">55%</option><option value="0.75">75%</option></select></label>
    </div>
    <div class="u83-checks">
      <label><input data-sfx-cat="water" type="checkbox">水声 / 淋浴</label>
      <label><input data-sfx-cat="contact" type="checkbox">节奏接触</label>
      <label><input data-sfx-cat="wet" type="checkbox">湿润接触</label>
      <label><input data-sfx-cat="bed" type="checkbox">床垫 / 床架</label>
      <label><input data-sfx-cat="thump" type="checkbox">墙 / 家具</label>
      <label><input data-sfx-cat="fabric" type="checkbox">布料 / 动作</label>
    </div>
    <div class="u83-sfx-importer">
      <label>导入到分类
        <select id="u83-sfx-import-category">
          <option value="water">水声 / 淋浴</option>
          <option value="contact">节奏接触</option>
          <option value="wet">湿润接触</option>
          <option value="bed">床垫 / 床架</option>
          <option value="thump">墙 / 家具</option>
          <option value="fabric">布料 / 动作</option>
        </select>
      </label>
      <input id="u83-sfx-files" type="file" accept="audio/*" multiple hidden>
      <button id="u83-sfx-import" class="u83-btn primary" type="button">导入音频</button>
      <span id="u83-sfx-count" class="u83-sfx-count">0 段</span>
      <button id="u83-sfx-stop" class="u83-btn" type="button">停止全部</button>
      <button id="u83-sfx-clear" class="u83-btn danger" type="button">清空素材库</button>
    </div>
    <div id="u83-sfx-library" class="u83-list u83-sfx-library"><div class="u83-empty">正在读取本地音效库…</div></div>`;
  return e;
}
function pushPanel(){const e=document.createElement('section');e.className='u83-card wide';e.innerHTML=`<div class="u83-head"><div><small>WEB PUSH</small><h3>系统通知</h3><p>开启、测试、关闭都直接放出来。</p></div><span id="u83-push-state" class="u83-badge">读取中</span></div><div class="u83-toolbar"><button id="u83-push-enable" class="u83-btn primary">开启通知</button><button id="u83-push-test" class="u83-btn">测试通知</button><button id="u83-push-disable" class="u83-btn danger">关闭本机订阅</button><button id="u83-push-refresh" class="u83-btn">刷新</button></div><div id="u83-push-detail" class="u83-list" style="margin-top:10px"></div>`;bindPush(e);return e}
function bindPush(root){async function load(){const d=await api('/api/push/status').catch(e=>({error:e.message}));$('#u83-push-state',root).textContent=d.error?'读取失败':(d.ready?'推送可用':(d.configured?'等待订阅':'未配置'));$('#u83-push-detail',root).innerHTML=`<div class="u83-row"><div><strong>Web Push</strong><small>${esc(d.error||d.reason||`当前活跃订阅 ${Number(d.active_subscriptions||0)} 个`)}</small></div><em>${d.ready?'可发送':(d.configured?'未订阅':'未就绪')}</em></div>`}$('#u83-push-enable',root).onclick=()=>$('#btn-enable-push')?.click();$('#u83-push-test',root).onclick=async()=>{const d=await api('/api/push/test',{method:'POST',body:{}});alert(d.sent?'测试推送已发送':'当前没有可发送的订阅')};$('#u83-push-disable',root).onclick=async()=>{const reg=await navigator.serviceWorker?.ready,sub=await reg?.pushManager?.getSubscription();if(!sub){alert('当前浏览器没有订阅');return}await api('/api/push/unsubscribe',{method:'POST',body:{endpoint:sub.endpoint}});await sub.unsubscribe();await load()};$('#u83-push-refresh',root).onclick=load;window.addEventListener('jty:u83',e=>{if(e.detail.space==='system'&&e.detail.panel==='settings')load().catch(()=>{})})}

function memoryLab(){return `<div class="v872-memory-wrap">
<section class="u83-card wide v872-cosmos-card">
  <div class="u83-head"><div><small>MEMORY ATLAS · REAL DATA</small><h3>记忆星图</h3><p>三层距离圈只负责建立空间尺度；彩色星点来自真实记忆，不再使用密集放射线。点一颗星，右侧直接看原始情绪、激活与权重。</p></div><span id="u83-map-count" class="u83-badge">读取中</span></div>
  <div class="v872-cosmos-shell">
    <div id="u83-map" class="v872-cosmos-stage">
      <img class="v872-cosmos-art" src="/static/images/memory-star-atlas-v897.svg" alt="" draggable="false" decoding="async">
      <div id="u83-map-nodes" class="v872-cosmos-data" aria-label="记忆星图数据层"></div>
    </div>
    <aside id="u83-map-detail" class="v872-inspector">
      <small>SELECTED MEMORY</small><h4>点一颗记忆</h4><p>详情会固定出现在这里，不需要再往页面下面找。</p>
      <div class="v872-metrics"><span>情绪<b>—</b></span><span>激活<b>—</b></span><span>权重<b>—</b></span><span>状态<b>—</b></span></div>
      <div class="v872-inspector-note">节点大小表示重要度；颜色只区分温暖、平静与低落倾向。原始 valence × arousal 数值仍完整保留。</div>
    </aside>
  </div>
</section>
<div class="v872-memory-below">
  <section class="u83-card"><div class="u83-head"><div><small>SIMILAR</small><h3>相似记忆</h3><p>实际在讲同一件事的聚类。</p></div></div><div id="u83-clusters" class="u83-list"></div></section>
  <section class="u83-card"><div class="u83-head"><div><small>SURFACING</small><h3>正在浮现</h3><p>当前权重最高的未解决记忆。</p></div><button id="u83-decay" class="u83-btn">运行衰减</button></div><div id="u83-surface" class="u83-list"></div></section>
</div>
<section class="u83-card wide"><div class="u83-head"><div><small>ORGANIZE</small><h3>搜索、置顶与合并</h3><p>搜索和置顶完全本地；合并预览只有你点按钮时才调用辅助通道。</p></div></div><div class="u83-toolbar"><input id="u83-memory-q" type="search" placeholder="语义搜索记忆…" style="flex:1;min-width:180px;border:1px solid rgba(60,110,114,.14);border-radius:11px;padding:8px 10px"><button id="u83-memory-search" class="u83-btn">搜索</button><button id="u83-memory-merge" class="u83-btn">合并所选</button></div><div id="u83-memory-results" class="u83-list" style="margin-top:10px"></div><div id="u83-memory-merge-preview" class="u83-empty" style="margin-top:10px">先勾选至少两条，再点“合并所选”。预览不会落库。</div></section>
</div>`}
function bindMemory(root){async function load(){const [m,c,s]=await Promise.all([api('/api/memory/emotion-map').catch(()=>[]),api('/api/memory/clusters?threshold=.78').catch(()=>[]),api('/api/memory/surface?limit=8').catch(()=>[])]);renderMap(m);$('#u83-clusters').innerHTML=c.length?c.slice(0,10).map((g,i)=>`<div class="u83-cluster"><header><strong>聚类 ${i+1}</strong><span>${(g.members||[]).length} 条</span></header><ul>${(g.members||[]).map(v=>`<li>#${Number(v.id||0)} ${esc(v.content||'')}</li>`).join('')}</ul></div>`).join(''):'<div class="u83-empty">暂时没有可聚类的记忆。</div>';renderSurface(s)}$('#u83-memory-refresh',root.closest('.u83-panel')).onclick=load;$('#u83-decay',root).onclick=async()=>{await api('/api/memory/decay-cycle',{method:'POST',body:{}});await load()};const search=async()=>{const q=$('#u83-memory-q',root).value.trim();if(!q)return;const rows=await api('/api/memory/search-v2',{method:'POST',body:{query:q}}).catch(()=>[]);const host=$('#u83-memory-results',root);host.innerHTML=rows.length?rows.map(v=>`<div class="u83-row"><div><label style="display:flex;gap:8px;align-items:flex-start"><input type="checkbox" data-memory-pick="${Number(v.id||0)}"><span><strong>#${Number(v.id||0)} ${esc(v.content||'')}</strong><small>相似 ${Number(v.similarity||0).toFixed(2)} · 权重 ${Number(v.decay_score||0).toFixed(2)}</small></span></label></div><button class="u83-btn" data-memory-pin="${Number(v.id||0)}">置顶</button></div>`).join(''):'<div class="u83-empty">没有搜索结果。</div>';$$('[data-memory-pin]',host).forEach(b=>b.onclick=async()=>{await api('/api/memory/pin',{method:'POST',body:{id:Number(b.dataset.memoryPin),pinned:true}});b.textContent='已置顶';b.disabled=true})};$('#u83-memory-search',root).onclick=search;$('#u83-memory-q',root).addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();search()}});$('#u83-memory-merge',root).onclick=async()=>{const ids=$$('[data-memory-pick]:checked',root).map(x=>Number(x.dataset.memoryPick));if(ids.length<2){alert('至少勾选两条');return}const out=$('#u83-memory-merge-preview',root);out.textContent='正在生成合并预览…';const d=await api('/api/memory/merge/preview',{method:'POST',body:{ids}}).catch(e=>({error:e.message}));if(d.error){out.textContent=d.error;return}out.innerHTML=`<strong>合并预览</strong><p style="white-space:pre-wrap">${esc(d.preview||'')}</p><button id="u83-memory-merge-commit" class="u83-btn primary">确认合并并归档原条</button>`;$('#u83-memory-merge-commit',out).onclick=async()=>{await api('/api/memory/merge/commit',{method:'POST',body:{content:d.preview,source_ids:ids,inherit_pin:Boolean(d.inherit_pin)}});out.textContent='合并完成；原条已归档保留安全网。';await load()}};window.addEventListener('jty:u83',e=>{if(e.detail.space==='memory'&&e.detail.panel==='map')load().catch(()=>{})})}
function renderMap(items){
  const stage=$('#u83-map'),layer=$('#u83-map-nodes',stage);if(!stage||!layer)return;
  layer.replaceChildren();
  const rows=Array.isArray(items)?items:[];
  const inspector=$('#u83-map-detail');
  const hash=t=>Array.from(String(t||'')).reduce((a,c)=>((a*33)+c.charCodeAt(0))>>>0,5381);
  const centerX=49.3,centerY=48.1,aspect=1672/941;
  // Keep the complete API dataset behind the panel, but cap the raster overlay.
  // Dense white buttons were visually reading as eggs rather than stars.
  const sorted=[...rows].sort((a,b)=>Number(b.importance||b.decay_score||0)-Number(a.importance||a.decay_score||0));
  const MAX_VISIBLE=24;
  const visible=sorted.slice(0,MAX_VISIBLE);
  $('#u83-map-count').textContent=rows.length>visible.length?`${rows.length} 条 · ${visible.length} 颗星`:`${rows.length} 条`;
  const placed=[];
  const locate=(v,i,imp)=>{
    const seed=hash(`${v.id||i}|${v.domain||''}|${String(v.content||'').slice(0,80)}`);
    const val=Math.max(-1,Math.min(1,Number(v.valence||0)));
    const aro=Math.max(0,Math.min(1,Number(v.arousal||0)));
    const band=Math.max(0,Math.min(4,Math.round((1-imp)*4)));
    const baseR=[7.8,12.8,17.9,23.0,28.1][band];
    let radius=baseR+(((seed>>>7)%1000)/1000-.5)*5.2;
    let angle=((seed%100000)/100000)*Math.PI*2+val*.18+aro*.11;
    let left=centerX,top=centerY;
    for(let attempt=0;attempt<16;attempt++){
      left=Math.max(3,Math.min(97,centerX+Math.cos(angle)*radius));
      top=Math.max(3,Math.min(97,centerY+Math.sin(angle)*radius*aspect));
      const crowded=placed.some(q=>Math.hypot((left-q[0])*.70,top-q[1])<2.35);
      if(!crowded)break;
      angle+=2.399963229728653; // golden-angle retry; deterministic and non-stringy
      radius+=((attempt%3)-1)*.55;
    }
    placed.push([left,top]);
    return [left,top,val,aro];
  };
  visible.forEach((v,i)=>{
    const imp=Math.max(0,Math.min(1,Number(v.importance||v.decay_score||.3)));
    const [left,top,val,aro]=locate(v,i,imp);
    const b=document.createElement('button');
    b.type='button';b.className='v872-memory-node';
    if(!v.resolved&&imp>.72)b.classList.add('hot');
    b.style.left=`${left}%`;b.style.top=`${top}%`;
    b.dataset.tone=val>.2?'warm':(val<-.2?'cool':'calm');
    b.style.setProperty('--star-size',`${(3.2+imp*3.8).toFixed(2)}px`);
    b.style.setProperty('--star-alpha',`${(.68+imp*.30).toFixed(2)}`);
    const content=String(v.content||'').trim();
    b.title=content.slice(0,140);
    b.setAttribute('aria-label',`记忆 ${Number(v.id||0)}：${content.slice(0,80)}`);
    b.onclick=()=>{
      $$('.v872-memory-node.active',layer).forEach(x=>x.classList.remove('active'));
      b.classList.add('active');
      if(!inspector)return;
      inspector.innerHTML=`<small>SELECTED MEMORY · #${Number(v.id||0)}</small><h4>${esc(v.domain||'未分类')}</h4><p>${esc(content)}</p><div class="v872-metrics"><span>情绪<b>${val.toFixed(2)}</b></span><span>激活<b>${aro.toFixed(2)}</b></span><span>权重<b>${Number(v.decay_score||imp).toFixed(2)}</b></span><span>状态<b>${v.resolved?'已解决':'仍在浮现'}</b></span></div><div class="v872-inspector-note">清晰底图只保留三层距离圈；彩色星点来自 /api/memory/emotion-map。地图最多显示 24 颗代表星，完整记忆仍保留在搜索、聚类与浮现数据中。</div>`;
    };
    layer.appendChild(b);
  });
}
function renderSurface(items){const h=$('#u83-surface');h.innerHTML=items.length?items.map(v=>`<div class="u83-row"><div><strong>${esc(v.content||v.summary||'记忆')}</strong><small>#${Number(v.id||0)} · 权重 ${Number(v.decay_score||0).toFixed(2)}</small></div><button class="u83-btn" data-resolve="${Number(v.id||0)}">解决</button></div>`).join(''):'<div class="u83-empty">现在没有主动浮现的记忆。</div>';$$('[data-resolve]',h).forEach(b=>b.onclick=async()=>{await api('/api/memory/resolve',{method:'POST',body:{memory_id:Number(b.dataset.resolve)}});renderSurface(await api('/api/memory/surface?limit=8').catch(()=>[]))})}
function buildMemory(){const v=$('#view-memory'),x=deck(v,'memory',[['history','历史'],['memory','记忆'],['style','风格'],['map','星图']],'memory');if(!x)return;const c=v.querySelector('.page-scroll'),h=x.panel('history');h.innerHTML=intro('RAW HISTORY','历史与原话','恢复旧窗口、全文搜索和收藏直接在这里。');move(c.querySelector('.conversation-import-panel'),h);move(c.querySelector('.local-history-panel'),h);const m=x.panel('memory');m.innerHTML=intro('MEMORY','当前记忆','随口事实与原话搜索放在同一页。');move(c.querySelector('.natural-memory-panel'),m);move(c.querySelector('.memory-search-panel'),m);move(c.querySelector('.memory-principles'),m);const s=x.panel('style');s.innerHTML=intro('VOICE PROFILE','聊天风格','只学习你亲自选择的表达样本。');move(c.querySelector('.style-profile-panel'),s);const p=x.panel('map');p.innerHTML=intro('MEMORY ATLAS','记忆星图','情绪坐标、相似聚类和主动浮现终于有真正窗口。','<button id="u83-memory-refresh" class="u83-btn">刷新</button>')+memoryLab();bindMemory(p);window.JTYUI83.memory=x}

function innerTrail(){return `<div class="u83-grid"><section class="u83-card"><div class="u83-head"><div><small>STATE CHANGES</small><h3>完整变化</h3><p>显示真正跨级的状态变化。</p></div></div><div id="u83-inner-changes" class="u83-list"></div></section><section class="u83-card"><div class="u83-head"><div><small>PRIVATE OS</small><h3>内心 OS 历史</h3><p>主观余波，不进入事实记忆。</p></div></div><div id="u83-inner-os" class="u83-list"></div></section></div>`}
function bindInnerTrail(root){async function load(){const q=sid()?`&session_id=${encodeURIComponent(sid())}`:'';const [a,b]=await Promise.all([api(`/api/inner-state/changes?limit=40${q}`).catch(()=>({items:[]})),api(`/api/inner-state/monologues?limit=30${q}`).catch(()=>({items:[]}))]);$('#u83-inner-changes').innerHTML=(a.items||[]).length?a.items.map(v=>`<div class="u83-row"><div><strong>${esc(v.domain_label||v.domain)} · ${esc(v.label||v.dimension)}</strong><small>${esc(v.description||v.reason||v.source||'状态变化')}</small></div><em>${Number(v.before||0)}→${Number(v.after||0)}</em></div>`).join(''):'<div class="u83-empty">还没有跨级变化。</div>';$('#u83-inner-os').innerHTML=(b.items||[]).length?b.items.map(v=>`<div class="u83-row"><div><strong>${esc(v.text||v.content||v.monologue||'一段内心余波')}</strong><small>${esc(v.dominant_emotion||v.reason||'')}</small></div><em>${esc(String(v.created_at||'').slice(0,16))}</em></div>`).join(''):'<div class="u83-empty">还没有 OS 历史。</div>'}$('#u83-inner-refresh',root.closest('.u83-panel')).onclick=load;window.addEventListener('jty:u83',e=>{if(e.detail.space==='inner'&&e.detail.panel==='trail')load().catch(()=>{})})}
function morningUI(){return `<div class="u83-grid"><section class="u83-card wide"><div class="u83-head"><div><small>CURRENT EVENT</small><h3>晨间状态与设置</h3><p>时间窗和主动表达直接在这里管理。</p></div><span id="u83-morning-state" class="u83-badge">读取中</span></div><div class="u83-form"><label>模式<select id="u83-morning-mode"><option value="off">关闭</option><option value="subtle">轻微</option><option value="natural">自然</option><option value="vivid">鲜明</option></select></label><label>开始小时<input id="u83-morning-start" type="number" min="0" max="23"></label><label>结束小时<input id="u83-morning-end" type="number" min="0" max="23"></label><label>持续分钟<input id="u83-morning-duration" type="number" min="30" max="180"></label><label>时区<input id="u83-morning-timezone" type="text"></label><label>主动表达<select id="u83-morning-proactive"><option value="true">开启</option><option value="false">关闭</option></select></label></div><div class="u83-toolbar" style="margin-top:10px"><button id="u83-morning-save" class="u83-btn primary">保存</button><button id="u83-morning-test" class="u83-btn">测试当前窗口</button><button id="u83-morning-reset" class="u83-btn">重置事件</button><button id="u83-morning-refresh" class="u83-btn">刷新</button></div><div id="u83-morning-metrics" class="u83-metrics" style="margin-top:10px"></div></section><section class="u83-card wide"><div class="u83-head"><div><small>HISTORY</small><h3>晨间历史</h3><p>过去事件和主动表达结果。</p></div></div><div id="u83-morning-history" class="u83-list"></div></section></div>`}
function bindMorning(root){const q=id=>$(id,root);async function load(){const [d,h]=await Promise.all([api('/api/morning/state'),api('/api/morning/history?limit=30')]),s=d.settings||{},ev=d.active||d.active_event||d.event||d.meta?.active_event||{},m=ev.metrics||d.metrics||{};q('#u83-morning-mode').value=s.mode||'natural';q('#u83-morning-start').value=s.window_start??6;q('#u83-morning-end').value=s.window_end??9;q('#u83-morning-duration').value=s.duration_minutes??90;q('#u83-morning-timezone').value=s.timezone||'Asia/Shanghai';q('#u83-morning-proactive').value=String(s.proactive_enabled!==false);q('#u83-morning-state').textContent=ev.event_id?'事件进行中':'当前平稳';q('#u83-morning-metrics').innerHTML=[['硬度',m.hardness],['敏感',m.sensitivity],['欲望',m.desire],['张力',m.physical_tension]].map(([k,v])=>`<div class="u83-metric"><span>${k}</span><strong>${v==null?'—':Math.round(Number(v)*10)+'/10'}</strong></div>`).join('');q('#u83-morning-history').innerHTML=(h.items||[]).length?h.items.map(v=>`<div class="u83-row"><div><strong>${esc(String(v.event_date||v.local_date||v.created_at||'晨间事件').slice(0,16))}</strong><small>硬度 ${Number(v.levels?.hardness||0)}/10 · 主动 ${esc(v.proactive_outcome||v.proactive_state||'—')}</small></div><em>${esc(v.mode||'')}</em></div>`).join(''):'<div class="u83-empty">还没有晨间历史。</div>'}q('#u83-morning-save').onclick=async()=>{await api('/api/morning/settings',{method:'PATCH',body:{mode:q('#u83-morning-mode').value,window_start:Number(q('#u83-morning-start').value),window_end:Number(q('#u83-morning-end').value),duration_minutes:Number(q('#u83-morning-duration').value),timezone:q('#u83-morning-timezone').value.trim(),proactive_enabled:q('#u83-morning-proactive').value==='true'}});await load()};q('#u83-morning-test').onclick=async()=>{if(!sid()){alert('请先进入一个已保存窗口');return}await api('/api/morning/test-trigger',{method:'POST',body:{session_id:sid()}});await load()};q('#u83-morning-reset').onclick=async()=>{await api('/api/morning/reset',{method:'POST',body:{keep_settings:true}});await load()};q('#u83-morning-refresh').onclick=load;window.addEventListener('jty:u83',e=>{if(e.detail.space==='inner'&&e.detail.panel==='morning')load().catch(()=>{})})}
function buildInner(){const x=deck($('#view-inner'),'inner',[['now','此刻'],['trail','轨迹'],['morning','晨间'],['archive','档案']],'now');if(!x)return;const n=x.panel('now');n.innerHTML=intro('CANONICAL STATE','此刻','只看当前会话真正使用的内在状态。');move($('#inner-state-section'),n);const t=x.panel('trail');t.innerHTML=intro('INNER TRAIL','内心轨迹','完整变化记录与私人 OS 历史。','<button id="u83-inner-refresh" class="u83-btn">刷新</button>')+innerTrail();bindInnerTrail(t);const m=x.panel('morning');m.innerHTML=intro('MORNING RESPONSE','晨间系统','当前状态、时间窗、历史和主动表达结果放到独立页面。')+morningUI();bindMorning(m);const a=x.panel('archive');a.innerHTML=intro('ARCHIVE','高级与兼容档案','性格脊柱、旧情绪引擎和旧念头池保留，但不挤在“此刻”。');move($('#inner-advanced-source'),a);window.JTYUI83.inner=x}

function toolsUI(){return `<div class="u83-grid"><section class="u83-card"><div class="u83-head"><div><small>READ ONLY</small><h3>本机只读工具</h3><p>不执行 shell，不写文件或数据库。</p></div></div><div id="u83-ro-tools" class="u83-list"></div></section><section class="u83-card"><div class="u83-head"><div><small>MCP</small><h3>MCP 工具</h3><p>只展示注册能力，打开页面不会执行。</p></div></div><div id="u83-mcp-tools" class="u83-list"></div></section></div>`}
function bindTools(root){async function load(){const [a,b]=await Promise.all([api('/api/tools/read-only').catch(()=>({tools:[]})),api('/api/mcp/tools').catch(()=>[])]),arr=Array.isArray(b)?b:(b.tools||[]);$('#u83-ro-tools').innerHTML=(a.tools||[]).length?a.tools.map(v=>`<div class="u83-tool"><strong>${esc(v.name||'tool')}</strong><p>${esc(v.description||'')}</p></div>`).join(''):'<div class="u83-empty">当前没有只读工具。</div>';$('#u83-mcp-tools').innerHTML=arr.length?arr.map(v=>`<div class="u83-tool"><strong>${esc(v.name||v.tool_name||'MCP tool')}</strong><p>${esc(v.description||v.summary||'已注册')}</p></div>`).join(''):'<div class="u83-empty">当前没有 MCP 工具。</div>'}$('#u83-tools-refresh',root.closest('.u83-panel')).onclick=load;window.addEventListener('jty:u83',e=>{if(e.detail.space==='system'&&e.detail.panel==='tools')load().catch(()=>{})})}
function buildSystem(){const v=$('#view-system'),x=deck(v,'system',[['usage','用量'],['settings','设置'],['tools','工具'],['data','数据']],'usage');if(!x)return;$('#console-nav')?.remove();const cards=v.querySelector('.console-cards');if(cards)x.nav.insertAdjacentElement('beforebegin',cards);const u=x.panel('usage');u.innerHTML=intro('API LEDGER','钱花在哪里','主聊天、缓存、proactive 与后台调用分开看。');move($('#cost-section'),u);move($('#session-cost-section'),u);const s=x.panel('settings');s.innerHTML=intro('LOCAL SETTINGS','显示、模型与共处','界面、关系保护、模型房间和推送共处都留在工作室，不再占一个独立生活页。');move($('#appearance-section'),s);move($('#relational-honesty-section'),s);move($('#living-section'),s);move($('#model-hub-section'),s);s.appendChild(pushPanel());const t=x.panel('tools');t.innerHTML=intro('LOCAL TOOLS','声音与工具台','听海、语音、表情包、环境音与只读工具都在这里。','<button id="u83-tools-refresh" class="u83-btn">刷新</button>')+toolsUI();move($('#ocean-listen-section'),t);move($('#voice-section'),t);move($('#media-section'),t);t.appendChild(sfxPanel());bindTools(t);const data=x.panel('data');data.innerHTML=intro('LOCAL DATA','本机数据与资料','资料工作室、备份、恢复和自动运行集中在这一页。');move($('#file-workspace-section'),data);move($('#local-data-section'),data);window.JTYUI83.system=x}

function organize(){buildMemory();buildInner();buildSystem();$('#u83-header-sfx')?.remove();const k=$('#view-system .console-kicker');if(k)k.textContent='SYSTEM ROOM · V8.9.7'}
window.JTYUI83={organize,activate:(space,panel)=>window.JTYUI83[space]?.activate?.(panel),memory:null,inner:null,system:null};
})();
