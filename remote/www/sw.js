/* sw.js - service worker
 *
 * Its whole job is to make the app open when the rig is off. Without it the phone
 * asks a powered-down machine for index.html and shows a browser error page - at the
 * exact moment you wanted to press "power on".
 *
 * Shell: cache-first, because it changes only when the agent is redeployed and a
 * stage is no place to discover that a page load is waiting on a timeout.
 * API: never cached. A stale health report is worse than no health report; it would
 * claim the audio device is fine while you stand in silence.
 */

const CACHE = 'riomhdhos-v1';
const SHELL = ['/', '/index.html', '/app.js', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      // addAll is all-or-nothing; one missing icon would leave the app uninstallable,
      // so each entry is added independently and a failure is tolerated.
      .then(c => Promise.all(SHELL.map(u => c.add(u).catch(() => {}))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  if (url.pathname.startsWith('/api/')) return;           // always live, never cached
  if (e.request.method !== 'GET') return;
  if (url.origin !== location.origin) return;             // the smart plug is not ours to cache

  e.respondWith(
    caches.match(e.request).then(hit => {
      if (hit) {
        // Refresh in the background so a redeploy lands next launch, without ever
        // making this launch wait on the network.
        fetch(e.request).then(r => {
          if (r && r.ok) caches.open(CACHE).then(c => c.put(e.request, r.clone()));
        }).catch(() => {});
        return hit;
      }
      return fetch(e.request).then(r => {
        if (r && r.ok) {
          const copy = r.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return r;
      }).catch(() => caches.match('/index.html'));
    })
  );
});
