'use strict';

// はるみバス Service Worker。
//  - アプリシェル(HTML/JS/CSS/manifest/icon): キャッシュ優先(オフライン起動)
//  - timetable.json: ネットワーク優先(最新を取りつつ、圏外では前回分を表示)
// キャッシュ名の版を上げると古いキャッシュを一掃する。
const CACHE = 'harumi-bus-v1';
const SHELL = [
  './',
  './index.html',
  './app.js',
  './style.css',
  './manifest.webmanifest',
  './icon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
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

  // データはネットワーク優先(取得できたらキャッシュ更新、失敗時はキャッシュ)
  if (url.pathname.endsWith('timetable.json')) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // ナビゲーションはオフライン時に index.html へフォールバック
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match('./index.html'))
    );
    return;
  }

  // それ以外(シェル)はキャッシュ優先
  event.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
      return res;
    }))
  );
});
