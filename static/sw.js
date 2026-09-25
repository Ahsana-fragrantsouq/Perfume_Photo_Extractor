// Bump VERSION on every deploy that changes static files.
const VERSION = 'v1';
const STATIC_CACHE = `fs-static-${VERSION}`;
const OFFLINE_URL = '/static/offline.html';
const DEBUG = true; // set false to silence logs

const log = (...args) => DEBUG && console.log(`[SW ${VERSION}]`, ...args);
const warn = (...args) => DEBUG && console.warn(`[SW ${VERSION}]`, ...args);

const PRECACHE = [
  OFFLINE_URL,
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

log('script loaded');

self.addEventListener('install', (event) => {
  log('install: start, precaching', PRECACHE);
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((c) => c.addAll(PRECACHE))
      .then(() => log('install: precache OK →', STATIC_CACHE))
      .then(() => self.skipWaiting())
      .then(() => log('install: skipWaiting done'))
      .catch((err) => warn('install: precache FAILED', err))
  );
});

self.addEventListener('activate', (event) => {
  log('activate: start');
  event.waitUntil(
    caches.keys()
      .then((keys) => {
        const stale = keys.filter((k) => k !== STATIC_CACHE);
        log('activate: caches found', keys, '| deleting', stale);
        return Promise.all(stale.map((k) => caches.delete(k)));
      })
      .then(() => self.clients.claim())
      .then(() => log('activate: clients claimed, SW now controlling pages'))
      .catch((err) => warn('activate: FAILED', err))
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Never touch uploads/API POSTs or other origins (Cloudinary, Airtable, etc.)
  if (req.method !== 'GET' || url.origin !== self.location.origin) {
    log('fetch: bypass', req.method, url.href);
    return;
  }

  // Pages: always network (fresh data), offline page as fallback
  if (req.mode === 'navigate') {
    log('fetch: navigate (network-first)', url.pathname);
    event.respondWith(
      fetch(req)
        .then((res) => { log('fetch: navigate OK', res.status, url.pathname); return res; })
        .catch((err) => {
          warn('fetch: navigate FAILED, serving offline page', url.pathname, err);
          return caches.match(OFFLINE_URL);
        })
    );
    return;
  }

  // Static assets: serve cache, refresh in background
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.open(STATIC_CACHE).then(async (cache) => {
        const cached = await cache.match(req);
        log('fetch: static', url.pathname, cached ? '(cache HIT)' : '(cache MISS)');
        const network = fetch(req)
          .then((res) => {
            if (res.ok) {
              cache.put(req, res.clone());
              log('fetch: static refreshed', url.pathname, res.status);
            } else {
              warn('fetch: static bad status', url.pathname, res.status);
            }
            return res;
          })
          .catch((err) => {
            warn('fetch: static network FAILED', url.pathname, err);
            return cached;
          });
        return cached || network;
      })
    );
    return;
  }

  // Everything else (JSON endpoints etc.) goes straight to network
  log('fetch: passthrough', url.pathname);
});
