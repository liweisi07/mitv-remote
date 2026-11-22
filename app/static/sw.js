/* 小米电视遥控 — Service Worker */
// 缓存名必须跟版本号走（mitvremote-v<版本>）：升级后旧缓存的 JS 会
// 让页面上的按钮成摆设。发版改版本号时务必同步改这里。
var CACHE_NAME = 'mitvremote-v1.0.5';
// 只预缓存公开静态资源；/ 与 /settings 要登录，不预缓存，页面一律走网络。
var APP_SHELL = [
  '/static/style.css',
  '/static/remote.js',
  '/static/settings.js',
  '/manifest.json',
  '/icons/icon-192.png',
  '/icons/icon-512.png'
];

self.addEventListener('install', function (event) {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(APP_SHELL);
    }).catch(function () { /* 缓存失败不阻塞安装 */ })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    Promise.all([
      clients.claim(),
      caches.keys().then(function (keys) {
        return Promise.all(keys.map(function (key) {
          if (key !== CACHE_NAME) return caches.delete(key);
          return undefined;
        }));
      }),
    ]).catch(function () {})
  );
});

self.addEventListener('fetch', function (event) {
  var url;
  try { url = new URL(event.request.url); } catch (e) { return; }
  if (event.request.method !== 'GET') return;
  if (url.origin !== self.location.origin) return;

  // /api/* 一律走网络，绝不缓存
  if (url.pathname.indexOf('/api/') === 0) {
    event.respondWith(fetch(event.request));
    return;
  }

  var isStatic = url.pathname.indexOf('/static/') === 0 ||
    url.pathname.indexOf('/icons/') === 0 ||
    url.pathname === '/manifest.json';

  if (isStatic) {
    event.respondWith(
      caches.match(event.request).then(function (cached) {
        if (cached) return cached;
        return fetch(event.request).then(function (res) {
          if (res && res.ok) {
            var copy = res.clone();
            caches.open(CACHE_NAME).then(function (cache) { cache.put(event.request, copy); }).catch(function () {});
          }
          return res;
        });
      }).catch(function () { return fetch(event.request); })
    );
    return;
  }

  // 页面（/、/settings、/login）：只走网络，不缓存
  if (url.pathname === '/' || url.pathname === '/settings' ||
      url.pathname === '/login') {
    event.respondWith(fetch(event.request));
  }
});
