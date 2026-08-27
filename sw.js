// Panier — service worker
const VERSION = 'panier-v3.2.0';
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

    // Purge unique des reponses d'API qu'une version anterieure du worker a pu
    // enregistrer. Tant que /api/ tombait dans networkFirst, une reponse 200 du
    // serveur web — sa page d'accueil, faute de relais configure — etait mise en
    // cache comme s'il s'agissait d'un resultat de recherche. Le nom du cache
    // applicatif ne changeant pas a chaque correctif, cette entree survivrait au
    // remplacement du worker : on l'enleve nommement.
    const shell = await caches.open(SHELL_CACHE);
    for (const req of await shell.keys()) {
      if (new URL(req.url).pathname.startsWith('/api/')) await shell.delete(req);
    }

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

  // Catalogue de recettes : jamais intercepte.
  //
  // Deux raisons, et la premiere est un bug vecu. networkFirst se rabat sur
  // './index.html' quand le reseau echoue : une recherche dont le service ne
  // repond pas recevait donc la page HTML de l'application, que l'appli tentait
  // de lire comme du JSON (« Unexpected token '<' »). Un repli de navigation n'a
  // aucun sens pour un appel d'API : mieux vaut laisser l'erreur reelle passer.
  //
  // Ensuite, les resultats n'ont rien a faire en cache : ils dependent de la
  // requete, et le catalogue evolue a chaque moisson.
  if (url.pathname.startsWith('/api/')) return;

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
