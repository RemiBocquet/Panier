# vendor/ — bibliothèques tierces, servies par Panier

Ces fichiers ne sont pas écrits par nous : ce sont des bibliothèques externes, **copiées
ici volontairement** plutôt que chargées depuis un CDN.

## Pourquoi

Charger une bibliothèque depuis `cdn.jsdelivr.net` revient à faire confiance à un
hébergeur tiers, à chaque ouverture de l'application, pour toute l'éternité. Si ce serveur
est compromis, ou si le paquet publié est remplacé, le code envoyé s'exécute avec **tous
les droits de Panier** : il peut lire la base IndexedDB et le code de synchro. C'est le
constat PAN-3 de l'audit du 21 août 2026.

Ici, les fichiers viennent du même serveur que l'application, sous le même contrôle
qu'elle. Effets de bord bienvenus : l'OCR fonctionne hors-ligne dès le premier usage, et
une politique de contenu (CSP) pourra se limiter à `script-src 'self'`.

## Contenu

| Fichier | Version | Taille | Licence |
|---|---|---|---|
| `qrcode.js` | qrcode-generator 1.4.4 | 56 Ko | MIT — Kazuhiko Arase |
| `tesseract/tesseract.min.js` | tesseract.js 5.1.1 | 66 Ko | Apache-2.0 |
| `tesseract/worker.min.js` | tesseract.js 5.1.1 | 124 Ko | Apache-2.0 |
| `tesseract/core/tesseract-core-simd-lstm.wasm.js` | tesseract.js-core 5.1.1 | 3,9 Mo | Apache-2.0 |
| `tesseract/core/tesseract-core-lstm.wasm.js` | tesseract.js-core 5.1.1 | 3,9 Mo | Apache-2.0 |
| `tesseract/lang/fra.traineddata.gz` | tessdata 4.0.0_best_int | 707 Ko | Apache-2.0 |

Les deux variantes du cœur sont nécessaires : tesseract.js choisit `-simd-` si le
navigateur gère SIMD (tous les navigateurs récents), et l'autre sinon.

Les variantes **sans** `-lstm` ne sont pas là, et c'est voulu : `index.html` appelle
`createWorker('fra', 1, …)` où `1` = `OEM.LSTM_ONLY`. Si un jour ce paramètre change, il
faudra rapatrier aussi `tesseract-core.wasm.js` et `tesseract-core-simd.wasm.js`, et le
dictionnaire passera de `4.0.0_best_int` (707 Ko) à `4.0.0` (6,3 Mo).

## Le piège à connaître

`index.html` passe **explicitement** `workerPath`, `corePath` et `langPath` à
`createWorker()`, en URL **absolues** (`TESS_BASE`).

Ce n'est pas une coquetterie :

- si l'une de ces options manque, tesseract.js retombe **sans le dire** sur ses adresses
  jsdelivr par défaut — l'auto-hébergement ne servirait plus à rien, sans aucun message
  d'erreur pour le signaler ;
- si l'une est **relative**, elle est résolue à l'intérieur du Web Worker, dont l'URL de
  base est un `blob:` — elle ne pointerait nulle part.

## Mettre à jour

```sh
B=https://cdn.jsdelivr.net/npm
curl -f -o vendor/qrcode.js                      "$B/qrcode-generator@1.4.4/qrcode.js"
curl -f -o vendor/tesseract/tesseract.min.js     "$B/tesseract.js@5.1.1/dist/tesseract.min.js"
curl -f -o vendor/tesseract/worker.min.js        "$B/tesseract.js@5.1.1/dist/worker.min.js"
curl -f -o vendor/tesseract/core/tesseract-core-simd-lstm.wasm.js \
                                                 "$B/tesseract.js-core@5.1.1/tesseract-core-simd-lstm.wasm.js"
curl -f -o vendor/tesseract/core/tesseract-core-lstm.wasm.js \
                                                 "$B/tesseract.js-core@5.1.1/tesseract-core-lstm.wasm.js"
curl -f -L -o vendor/tesseract/lang/fra.traineddata.gz \
                                                 "$B/@tesseract.js-data/fra@1.0.0/4.0.0_best_int/fra.traineddata.gz"
```

Après toute modification de ce dossier, **incrémenter `VENDOR_CACHE` dans `sw.js`**
(`panier-vendor-v1` → `v2`), sinon les appareils déjà installés continueront à servir
l'ancienne copie depuis leur cache.

## Côté serveur

Un seul point d'attention : `fra.traineddata.gz` doit être servi **tel quel**, sans
en-tête `Content-Encoding: gzip`. C'est le comportement normal de nginx pour une requête
visant directement un fichier `.gz` — c'est `gzip_static` qui ajouterait cet en-tête, et
seulement pour une requête visant le fichier *sans* l'extension. Rien à configurer donc,
mais si l'OCR échouait avec une erreur de décompression, chercher là.
