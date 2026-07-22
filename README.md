# Panier — repas & courses

PWA installable sur Android. Base de données **locale au téléphone** (IndexedDB), fonctionne hors-ligne.
Deux listes tenues à jour : **repas à faire** et **liste de courses**. Ajouter une recette aux repas
pousse automatiquement tous ses ingrédients dans les courses.

## Installation (2 minutes)

1. Sur Android, ouvrir l'URL dans Chrome → menu ⋮ → **Installer l'application**.
2. L'icône apparaît sur l'écran d'accueil, l'app s'ouvre en plein écran.

## Le relais d'import (optionnel mais recommandé)

Le navigateur ne peut pas lire marmiton.org directement (blocage CORS). Un petit Cloudflare Worker
gratuit sert de relais.

1. dash.cloudflare.com → Workers & Pages → Create Worker
2. Coller le contenu de `cors-relay.js`, déployer
3. Copier l'URL du Worker (ex. `https://panier-relay.xxx.workers.dev`)
4. Dans l'app : ⚙️ Réglages → **Relais d'import** → coller l'URL → Enregistrer

Jow fonctionne souvent **sans** relais (son API renvoie les bons en-têtes CORS). Marmiton en a besoin.

## Les trois façons d'ajouter une recette

**Depuis un lien** — Recettes → Importer → Depuis un lien.
- Marmiton, CuisineAZ, 750g : lecture du JSON-LD `schema.org/Recipe` de la page.
- Jow : l'`recipeId` de l'URL est extrait, puis plusieurs endpoints sont tentés. Si aucun ne
  répond, l'app lit le titre de la page partagée et bascule automatiquement sur la recherche
  Jow, qui elle est fiable.

**Rechercher sur Jow** — Recettes → Importer → Rechercher sur Jow.
- Recherche par nom via `POST api.jow.fr/public/recipe/quicksearch`, le seul endpoint public
  fiable. La réponse contient les recettes complètes.
- Les quantités Jow sont exprimées **par couvert** (`constituents[].ingredient.quantityPerCover`)
  et multipliées par `roundedCoversCount`. L'unité est résolue en croisant `unit.id` avec
  `naturalUnit` ou `alternativeUnits` de l'ingrédient — c'est ainsi que Jow choisit d'afficher
  « 20 cl » plutôt que « 200 ml ».
- Cette recherche passe par une requête POST : si tu utilises le relais, prends bien la
  **version à jour** de `cors-relay.js`, qui relaie POST (l'ancienne ne gérait que GET).

**Depuis une photo (OCR)** — Recettes → Importer → Depuis une photo.
- Capture d'écran de n'importe quel site/appli, ou photo d'une page de livre.
- Le texte est lu **localement sur le téléphone** (Tesseract.js en WebAssembly). Rien n'est envoyé
  à un serveur. Le moteur (~3 Mo) est téléchargé au premier usage puis mis en cache : ensuite l'OCR
  marche hors-ligne.
- Le parseur repère la section « Ingrédients », ignore les étapes, pubs et temps de cuisson, et gère
  la mise en page Jow où le nom et la quantité sont sur deux lignes séparées.
- Les quantités reconnues sont **à vérifier** dans le formulaire avant enregistrement.

**Saisie manuelle** — champ « Coller une liste » : une ligne = un ingrédient, analysée automatiquement.

## Partage direct

L'app est déclarée comme cible de partage. Depuis Chrome ou l'appli Jow : **Partager → Panier**,
l'écran d'import s'ouvre avec le lien pré-rempli.

## Fonctionnement des quantités

Les ingrédients sont agrégés par nom + famille d'unité :
- 200 g de farine + 0,3 kg de farine → **500 g de farine** (une seule ligne)
- masses (mg/g/kg) et volumes (ml/cl/dl/l) sont convertis dans une unité de base puis réaffichés
  lisiblement (500 g, 1,5 kg, 250 ml…)
- les unités non convertibles (gousse, pincée, sachet…) restent séparées, ce qui est le comportement
  voulu : 2 gousses d'ail et 1 c. à café d'ail en poudre ne se confondent pas.

Chaque article garde la trace des recettes qui l'ont demandé (affiché sous le nom). Retirer un repas
retire exactement sa contribution, sans toucher aux quantités venant des autres recettes ni aux
articles ajoutés à la main.

Changer le nombre de personnes au moment d'ajouter un repas met les quantités à l'échelle.

## Les rayons

La liste est triée par rayon de supermarché (fruits & légumes, viandes & poissons, crémerie,
boulangerie, épicerie salée/sucrée, boissons, surgelés, entretien) via un dictionnaire de mots-clés
français dans `index.html` (constante `RAYONS`) — facile à compléter.

## Mettre à jour l'application

**Tu n'as jamais besoin de désinstaller / réinstaller.** Il suffit de pousser les fichiers modifiés
sur GitHub ; le téléphone récupère la nouvelle version tout seul.

Comment ça marche : le service worker sert `index.html` en **réseau d'abord** (cache uniquement en
secours hors-ligne), et il est enregistré avec `updateViaCache:'none'` pour que le navigateur ne
garde pas un vieux `sw.js`. Concrètement :

- **fermer puis rouvrir l'app** suffit dans la quasi-totalité des cas ;
- l'app revérifie aussi à chaque retour au premier plan, et toutes les heures si elle reste ouverte ;
- si c'est le service worker lui-même qui a changé, un bandeau « Nouvelle version — appuyer pour
  recharger » apparaît : un appui et c'est fait ;
- Réglages → **Rechercher une mise à jour** force la vérification et affiche la version installée.

Tu n'as plus à incrémenter `VERSION` dans `sw.js` pour publier une modification de l'app : ce
numéro ne sert plus qu'à purger les anciens caches quand tu modifies la stratégie de cache
elle-même.

Si jamais l'app semble figée sur une vieille version (rare, typiquement après une coupure réseau
au mauvais moment) : Chrome → ⋮ → Paramètres du site → Panier → Effacer les données. Tes recettes
et listes sont dans IndexedDB, qui n'est pas touché par le vidage du cache — mais fais un export
JSON avant, par prudence.

## Synchroniser deux téléphones (optionnel)

Désactivé par défaut. Une fois activé, les recettes, repas et courses sont partagés entre tes
appareils — et uniquement les tiens.

### Mise en place

1. **Créer la base.** Cloudflare → Workers & Pages → D1 → Create database (nom libre).
   Dans l'onglet Console, colle :

   ```sql
   CREATE TABLE IF NOT EXISTS items (
     room       TEXT    NOT NULL,
     id         TEXT    NOT NULL,
     store      TEXT    NOT NULL,
     updated_at INTEGER NOT NULL,
     deleted    INTEGER NOT NULL DEFAULT 0,
     payload    TEXT,
     PRIMARY KEY (room, id)
   );
   CREATE INDEX IF NOT EXISTS idx_room_updated ON items(room, updated_at);
   ```

2. **Lier la base au Worker.** Settings → Bindings → Add → D1 database,
   nom de variable exactement **`DB`**.

3. **Redéployer** `cors-relay.js` (il contient désormais les endpoints de synchro).

4. **Sur le téléphone 1.** Réglages → Synchro entre appareils : colle l'URL du Worker,
   appuie sur « Générer un code sûr », puis Enregistrer.

5. **Sur le téléphone 2.** Même URL, et **exactement le même code**. Enregistrer.

### Confidentialité

Le code de partage ne quitte jamais l'appareil. Il sert à dériver, par PBKDF2 (150 000
itérations) et avec deux sels différents, deux valeurs indépendantes :

- un **identifiant de salon** (32 caractères hexadécimaux) envoyé au serveur ;
- une **clé AES-GCM** qui reste locale et chiffre chaque enregistrement avant l'envoi.

Le serveur ne stocke donc que des identifiants opaques, des horodatages et du chiffré. Il ne
peut pas lire tes recettes, et personne ne peut accéder à ton salon sans le code. Corollaire :
**si tu perds le code, les données déjà envoyées sont irrécupérables** — d'où l'intérêt de
l'export JSON.

### Fusion des modifications

La résolution se fait **enregistrement par enregistrement** (pas en bloc) : si un téléphone
ajoute du pain pendant que l'autre ajoute du lait, les deux articles subsistent. En cas de
modification du même article sur les deux appareils, la plus récente l'emporte.

Les suppressions laissent une « pierre tombale » horodatée, sans quoi un article effacé sur un
téléphone réapparaîtrait depuis l'autre. Ces marqueurs sont purgés après 90 jours.

La synchro se déclenche au lancement, au retour au premier plan, toutes les 5 minutes, et
4 secondes après une modification (groupées, pour ne pas marteler le serveur). Hors-ligne, tout
continue de fonctionner : les changements partent à la reconnexion.

## Sauvegarde

Réglages → Exporter : un fichier JSON avec tout (recettes, repas, courses, réglages).
Réglages → Importer : restaure. Pratique pour changer de téléphone, puisque rien n'est dans le cloud.

## Fichiers

```
index.html              toute l'application (UI + logique + OCR)
manifest.webmanifest    métadonnées PWA, raccourcis, cible de partage
sw.js                   service worker : coquille hors-ligne + cache des assets OCR
icon-192.png            icônes de l'application
icon-512.png
icon-maskable-512.png
apple-touch-icon.png
favicon-64.png
gen_icons.py            régénère les icônes (pip install pillow)
cors-relay.js           relais CORS Cloudflare — à coller dans un Worker, PAS servi par le site
.nojekyll               désactive Jekyll sur GitHub Pages
```

Structure volontairement **plate** : aucun sous-dossier, pour pouvoir tout téléverser d'un coup
depuis un téléphone sur github.com.

Aucune dépendance à installer : tout est en vanilla JS. Seul Tesseract.js est chargé depuis un CDN,
et seulement si tu utilises l'import par photo.
