/**
 * 大西瓜 Service Worker
 * 离线缓存 + Web Push 推送监听
 */
// Bump this cache name for every release that changes frontend assets.
const CACHE_NAME = 'jtyhome-v8.9.8-desktop-slim-cachefix-1';
// v6.5.2：运行时缓存（表情包、附件缩略图等动态资源）与预缓存分开，
// 并限制条数——旧版把所有成功的 GET 无限写进同一个缓存，用得越久越大。
const RUNTIME_CACHE = 'jtyhome-v8.9.8-runtime-v1-scroll-hotfix-desktop-layout-v3';
const RUNTIME_MAX_ENTRIES = 120;

const STATIC_ASSETS = [  '/static/css/desktop-bundle.css',
  '/static/hydrangea-water-hero/hero.css',
  '/static/fonts/misans/MiSans-Regular.min.css',
  '/static/fonts/misans/MiSans-Semibold.min.css',
  '/static/fonts/noto-serif-sc/NotoSerifSC-Medium.css',
  '/static/fonts/noto-serif-sc/NotoSerifSC-Medium.woff2',
  '/static/vendor/marked.umd.js',
  '/static/vendor/purify.min.js',
  '/static/vendor/react-16.0.0.production.min.js',
  '/static/vendor/react-dom-16.0.1.production.min.js',
  '/static/hydrangea-water-hero/water-refraction.runtime.js',
  '/static/hydrangea-water-hero/hero-fx.runtime.js',
  '/static/hydrangea-water-hero/hero.runtime.js',
  '/static/js/chat-reliability.js',
  '/static/js/app.js',
  '/static/js/dwell-life.js',
  '/static/js/weather-system.js',
  '/static/js/weather-rain-map.js',
  '/static/js/liquid-glass-webgpu.js',
  '/static/js/liquid-glass-webgl.js',
  '/static/js/ui-v83.js',
  '/static/js/desktop-v872.js',
  '/static/js/spatial-v873.js',
  '/static/js/voice-host.js',
  '/static/js/voice-call.js',
  '/static/js/voice-radio.js',
  '/manifest.json',
];

// v6.5.2：体积较大的资源单独“尽力缓存”，
// 任何一个失败都不会让整个 Service Worker 安装失败。
const OPTIONAL_ASSETS = [
  '/static/images/sunny-street-desktop.jpg',
  '/static/images/crystal-street-desktop.jpg',
  '/static/images/weather/drop-alpha.png',
  '/static/images/weather/drop-color.png',
  '/static/images/home-ornaments.png',
  '/static/images/home-wordmark-main-green.png',
  '/static/images/welcome-romantic-green.png',
  '/static/hydrangea-water-hero/flower-sea.png',
  '/static/images/memory-star-map-transparent.png',
  '/static/roleplay/johnson-phone.html',
  '/static/keepsakes/birthday-bunny.html',
  '/static/keepsakes/moon-mail.html',
  '/static/images/home-bunny.png',
];

// ── 安装：预缓存静态资源 ──
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      // cache: reload avoids Safari satisfying a new Service Worker install
      // from its normal HTTP cache after the backend bundle was upgraded.
      const freshRequest = (asset) => new Request(asset, { cache: 'reload' });
      await cache.addAll(STATIC_ASSETS.map(freshRequest));
      await Promise.allSettled(
        OPTIONAL_ASSETS.map((asset) => cache.add(freshRequest(asset)))
      );
    })
  );
  self.skipWaiting();
});

// ── 激活：清理旧缓存（保留当前静态与运行时两个缓存）──
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE_NAME && k !== RUNTIME_CACHE)
          .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// 运行时缓存裁剪：超过上限时删掉最旧的条目。
async function trimRuntimeCache() {
  const cache = await caches.open(RUNTIME_CACHE);
  let keys = await cache.keys();
  while (keys.length > RUNTIME_MAX_ENTRIES) {
    await cache.delete(keys[0]);
    keys = await cache.keys();
  }
}

// ── 请求拦截：Network First + Cache Fallback ──
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // v6.5.2：只接管本站的 GET 请求。跨域请求（如未来接入的外部服务）
  // 不缓存也不拦截，交给浏览器默认行为。
  if (url.origin !== self.location.origin) return;
  if (event.request.method !== 'GET') return;

  // API 请求不缓存
  if (url.pathname.startsWith('/api/')) return;

  // HTML navigation must hit the local backend. The root response establishes
  // the HttpOnly browser-session cookie; serving a cached shell to a fresh
  // Chrome profile can otherwise leave every private API at 401.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request, { cache: 'no-store' }).catch(() => new Response(
        '<!doctype html><meta charset="utf-8"><title>JTYHome</title><style>body{font-family:system-ui;padding:36px;background:#f5f5f3;color:#222}main{max-width:620px;margin:auto}</style><main><h2>JTYHome 本地服务还没有连接</h2><p>请确认后端正在运行，然后刷新这个页面。</p></main>',
        { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' } }
      ))
    );
    return;
  }

  // SSE 流不缓存
  if (event.request.headers.get('accept')?.includes('text/event-stream')) return;

  const isPrecached =
    STATIC_ASSETS.includes(url.pathname) || OPTIONAL_ASSETS.includes(url.pathname);

  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        if (resp.ok) {
          const clone = resp.clone();
          const targetCache = isPrecached ? CACHE_NAME : RUNTIME_CACHE;
          const cacheWrite = caches.open(targetCache).then(async (cache) => {
            await cache.put(event.request, clone);
            if (!isPrecached) await trimRuntimeCache();
          });
          event.waitUntil(cacheWrite);
        }
        return resp;
      })
      .catch(async () => {
          let hit = await caches.match(event.request);
          // HTML uses a release query string to defeat Safari's HTTP cache.
          // The install cache keeps canonical URLs, so offline lookup also
          // tries the same path without requiring an exact query match.
          if (!hit && isPrecached) {
            const staticCache = await caches.open(CACHE_NAME);
            hit = await staticCache.match(event.request, { ignoreSearch: true });
          }
          if (hit) return hit;
          return undefined;
        })
  );
});

// ── Web Push 推送 ──
self.addEventListener('push', (event) => {
  let data = { title: '大西瓜', body: '💕' };

  try {
    data = event.data.json();
  } catch (e) {
    data.body = event.data?.text() || '💕';
  }

  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: data.icon || '/static/icons/icon-192.png',
      badge: data.badge || '/static/icons/badge-72.png',
      tag: data.tag || 'default',
      data: data.data || {},
      vibrate: [100, 50, 100],
      actions: [{ action: 'open', title: '打开' }],
    })
  );
});

// ── 点击推送通知 ──
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  const sessionId = event.notification.data?.session_id;
  const url = sessionId ? `/?session=${sessionId}` : '/';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      // 如果已经有窗口打开，聚焦它
      for (const client of clients) {
        if (client.url.includes(self.location.origin)) {
          client.focus();
          if (sessionId) client.postMessage({ type: 'switch_session', session_id: sessionId });
          return;
        }
      }
      // 否则打开新窗口
      return self.clients.openWindow(url);
    })
  );
});
