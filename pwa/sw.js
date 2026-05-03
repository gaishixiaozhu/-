// 蜻蜓龙虾助手 - Service Worker v3
const CACHE_NAME = 'qingting-lobster-v3';
const ASSETS = [
  '/chat/',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png'
];

self.addEventListener('install', (e) => {
  // 立即激活，不等待旧SW
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(ASSETS).catch(() => {});
    })
  );
});

self.addEventListener('activate', (e) => {
  // 清除所有旧版本缓存
  e.waitUntil(
    caches.keys().then(keys => {
      return Promise.all(keys.map(k => caches.delete(k)));
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  // 不拦截API请求
  if (e.request.url.includes('/api/')) return;
  
  // 网络优先策略：先发网络请求，失败才用缓存
  e.respondWith(
    fetch(e.request).then(response => {
      // 网络成功 → 更新缓存 + 返回
      if (response && response.status === 200) {
        const clone = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(e.request, clone));
      }
      return response;
    }).catch(() => {
      // 网络失败 → 使用缓存（离线降级）
      return caches.match(e.request);
    })
  );
});
