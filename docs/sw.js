/* 理财工作台 Service Worker —— 鸿蒙浏览器 / ArkWeb 同源离线缓存
 * 策略：HTML 文档 network-first（保证 14:00 重新生成后拿到最新），
 *       静态资源 cache-first（离线可用）；离线时文档回退到缓存首页。
 * 四个模块页面已内联进 index.html，不再预缓存 pages/ 子页面。
 * 缓存版本由 scripts/build_workbench.py 按数据日期自动刷新。
 */
const CACHE = 'fwb-7d011987f0';

const ASSETS = [
  './',
  './index.html',
  './manifest.webmanifest',
  './data/workbench-summary.json',
  './assets/icon.svg',
  './assets/icon-192.png',
  './assets/icon-512.png',
  './assets/icon-maskable-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      // 逐条缓存：单文件发布时缺失的可选资源不影响其余资源入缓存
      .then((c) => Promise.all(ASSETS.map((u) => c.add(u).catch(() => undefined))))
      .then(() => self.skipWaiting())
      .catch((e) => { console.warn('[SW] 预缓存部分失败', e); return self.skipWaiting(); })
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // 只处理同源资源

  const isDoc = req.mode === 'navigate' ||
                url.pathname.endsWith('.html') ||
                (req.headers.get('accept') || '').includes('text/html');

  if (isDoc) {
    event.respondWith(
      fetch(req).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      }).catch(() => caches.match(req).then((hit) => hit || caches.match('./index.html')))
    );
    return;
  }

  event.respondWith(
    caches.match(req).then((hit) => {
      if (hit) return hit;
      return fetch(req).then((res) => {
        if (res && res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      });
    })
  );
});
