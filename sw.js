// Panier — service worker
const VERSION = 'panier-v3.1.0';
const SHELL_CACHE = 'panier-shell-' + VERSION;
const STATIC_CACHE = 'panier-static-v1';
// PAN-3 : les bibliotheques sont desormais servies par l'application elle-meme.
// Elles sont immuables (versions figees dans vendor/), donc cache-first, et separees
// du cache applicatif pour ne pas etre re-telechargees a chaque version de Panier.
// N'incrementer ce numero QUE si le contenu de vendor/ change.
const VENDOR_CACHE = 'panier-vendor-v1';

const SHELL = ['./', './index.html', './manifest.webmanifest', './manifest-en.webmanifest'];
const STATIC = [
  './icon-192.png', './icon-512.png', './icon-maskable-512.png',
  './apple-touch-icon.png', './favicon-64.png'
];

const NET_TIMEOUT = 4000;

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const shell = await caches.open(SHELL_CACHE);
    await shell.addAll(SHELL).catch(() => {});
    const stat = await caches.open(STATIC_CACHE);
    await Promise.all(STATIC.map((u) => stat.add(u).catch(() => {})));
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter((k) => (k.startsWith('panier-shell-') && k !== SHELL_CACHE)
                      || (k.startsWith('panier-vendor-') && k !== VENDOR_CACHE)
                      || k.startsWith('panier-cdn-'))   // PAN-3 : plus de tiers a mettre en cache
          .map((k) => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

// Permet à la page de demander l'activation immédiate de la nouvelle version.
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
  if (event.data === 'VERSION' && event.source) {
    event.source.postMessage({ type: 'VERSION', version: VERSION });
  }
});

function timeout(ms) {
  return new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms));
}

async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    // évite le cache HTTP du navigateur (GitHub Pages met max-age=600).
    const fresh = await Promise.race([
      fetch(req, { cache: 'no-store' }),
      timeout(NET_TIMEOUT)
    ]);
    if (fresh && fresh.ok) {
      cache.put(req, fresh.clone()).catch(() => {});
      return fresh;
    }
    throw new Error('bad response');
  } catch (e) {
    const hit = (await cache.match(req)) || (await cache.match('./index.html')) || (await caches.match(req));
    if (hit) return hit;
    throw e;
  }
}

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  // Tout est same-origin depuis PAN-3 : une reponse opaque serait anormale, on l'ecarte.
  if (res && res.ok) cache.put(req, res.clone()).catch(() => {});
  return res;
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  if (url.origin !== self.location.origin) return;

  // Bibliotheques versionnees (vendor/) : cache d'abord, pour qu'elles fonctionnent
  // hors-ligne apres un premier chargement et ne repassent plus par le reseau.
  if (url.pathname.includes('/vendor/')) {
    event.respondWith(cacheFirst(req, VENDOR_CACHE).catch(() => fetch(req)));
    return;
  }

  // Icônes et binaires : cache d'abord.
  if (/\.(png|jpg|jpeg|svg|ico|woff2?)$/i.test(url.pathname)) {
    event.respondWith(cacheFirst(req, STATIC_CACHE).catch(() => fetch(req)));
    return;
  }
  event.respondWith(networkFirst(req, SHELL_CACHE));
});
