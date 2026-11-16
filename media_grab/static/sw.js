const CACHE_NAME = 'media-grab-shell-v7';
const SHELL_URLS = ['/', '/manifest.json', '/icon.svg'];
const SW_OFFLINE_HTML = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="background:#0b0e14;color:#eef2f8;font-family:sans-serif;padding:24px;line-height:1.6"><h3>本地 Termux 服务被系统暂停</h3><p style="color:#9aa7bd">正在自动检测并恢复连接；若 3 秒后仍未跳转，请点下方按钮唤起 Termux 后切回本页（建议在系统设置将 Termux 省电策略设为「无限制」）：</p><p><a href="intent://#Intent;package=com.termux;end" style="display:inline-block;padding:12px 18px;background:#2563eb;color:#fff;border-radius:10px;text-decoration:none">⚡ 一键唤醒 Termux 服务</a> <button onclick="location.replace(\'/\')" style="padding:12px 16px;background:#1e2636;color:#eef2f8;border:1px solid #334155;border-radius:10px;margin-left:8px">立即刷新</button></p><script>function p(){fetch("/api/config",{cache:"no-store"}).then(function(r){if(r.ok)location.replace("/")}).catch(function(){})}p();setInterval(p,1200);document.addEventListener("visibilitychange",function(){if(document.visibilityState==="visible")p()})</script></body>';

function fetchWithTimeout(reqOrUrl, ms) {
  const ctrl = new AbortController();
  const tid = setTimeout(() => ctrl.abort(), ms);
  const target = typeof reqOrUrl === 'string' ? reqOrUrl : reqOrUrl.url;
  return fetch(target, { signal: ctrl.signal, cache: 'no-store', credentials: 'same-origin' })
    .finally(() => clearTimeout(tid));
}

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => Promise.all(
        SHELL_URLS.map((u) => fetch(u, { cache: 'no-store', credentials: 'same-origin' })
          .then((res) => (res && res.ok ? cache.put(u, res) : null))
          .catch(() => null))
      ))
      .catch(() => {})
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
      .catch(() => {})
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;

  if (req.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html') {
    event.respondWith(
      caches.match('/').catch(() => null).then((cached) => {
        const timeoutMs = cached ? 900 : 3000;
        return fetchWithTimeout('/', timeoutMs)
          .then((res) => {
            if (res && res.ok) {
              const copy = res.clone();
              caches.open(CACHE_NAME).then((c) => c.put('/', copy)).catch(() => {});
              return res;
            }
            return cached || res;
          })
          .catch(() => cached || new Response(
            SW_OFFLINE_HTML,
            { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' } }
          ));
      })
    );
    return;
  }

  if (url.pathname === '/manifest.json' || url.pathname === '/icon.svg') {
    event.respondWith(
      fetchWithTimeout(url.pathname, 900)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put(url.pathname, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => caches.match(url.pathname).catch(() => null))
    );
  }
});
