// Panier — service worker
//
// Stratégie de mise à jour :
//  - la coquille (HTML/manifest) est servie en RÉSEAU D'ABORD, cache en secours.
//    => dès que tu redéploies, le téléphone récupère la nouvelle version au lancement suivant,
//       sans que tu aies besoin de changer quoi que ce soit ici.
//  - les images et assets OCR sont servis en CACHE D'ABORD (ils ne changent jamais).
//  - hors-ligne, tout retombe sur le cache : l'app reste utilisable.

const VERSION = 'panier-v1.3.0';
const SHELL_CACHE = 'panier-shell-' + VERSION;
const STATIC_CACHE = 'panier-static-v1';
const OCR_CACHE = 'panier-ocr-v1';

// Servis en réseau d'abord : c'est là que vit ton code.
const SHELL = ['./', './index.html', './manifest.webmanifest'];

// Servis en cache d'abord : binaires stables.
const STATIC = [
  './icon-192.png', './icon-512.png', './icon-maskable-512.png',
  './apple-touch-icon.png', './favicon-64.png'
];

const OCR_HOSTS = ['cdn.jsdelivr.net', 'unpkg.com', 'tessdata.projectnaptha.com'];
const NET_TIMEOUT = 4000;

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const shell = await caches.open(SHELL_CACHE);
    await shell.addAll(SHELL).catch(() => {});
    const stat = await caches.open(STATIC_CACHE);
    await Promise.all(STATIC.map((u) => stat.add(u).catch(() => {})));
    // On n'active pas de force : la page ouverte garde sa version jusqu'au rechargement.
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter((k) => k.startsWith('panier-shell-') && k !== SHELL_CACHE)
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
    // `cache: 'no-store'` évite le cache HTTP du navigateur (GitHub Pages met max-age=600).
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
  if (res && (res.ok || res.type === 'opaque')) cache.put(req, res.clone()).catch(() => {});
  return res;
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // Assets OCR (Tesseract) : cache durable, pour que l'OCR marche hors-ligne.
  if (OCR_HOSTS.includes(url.hostname)) {
    event.respondWith(cacheFirst(req, OCR_CACHE).catch(() => fetch(req)));
    return;
  }

  // Hors origine (relais d'import, API Jow, images de recettes) : réseau direct, jamais de cache.
  if (url.origin !== self.location.origin) return;

  // Icônes et binaires : cache d'abord.
  if (/\.(png|jpg|jpeg|svg|ico|woff2?)$/i.test(url.pathname)) {
    event.respondWith(cacheFirst(req, STATIC_CACHE).catch(() => fetch(req)));
    return;
  }

  // Navigation et coquille : réseau d'abord => mise à jour automatique.
  event.respondWith(networkFirst(req, SHELL_CACHE));
});
