// Panier — service worker (offline-first app shell + cache OCR)
const VERSION = 'panier-v1.1.0';
const OCR_CACHE = 'panier-ocr-v1';

const SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-maskable-512.png',
  './icons/apple-touch-icon.png',
  './icons/favicon-64.png'
];

// Assets OCR (Tesseract.js + langue française) : gros fichiers, mis en cache
// durablement au premier usage pour que l'OCR marche ensuite hors-ligne.
const OCR_HOSTS = ['cdn.jsdelivr.net', 'unpkg.com', 'tessdata.projectnaptha.com'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== VERSION && k !== OCR_CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // Assets OCR : cache-first, persistant (permet l'OCR hors-ligne après 1er usage).
  if (OCR_HOSTS.includes(url.hostname)) {
    event.respondWith(
      caches.open(OCR_CACHE).then((cache) =>
        cache.match(req).then((hit) => {
          if (hit) return hit;
          return fetch(req).then((res) => {
            if (res && (res.ok || res.type === 'opaque')) {
              cache.put(req, res.clone()).catch(() => {});
            }
            return res;
          });
        })
      )
    );
    return;
  }

  // Tout le reste hors origine (relais d'import, API Jow, images) : réseau direct.
  if (url.origin !== self.location.origin) return;

  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(VERSION).then((cache) => cache.put(req, copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match('./index.html'));
    })
  );
});
