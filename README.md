# Panier — repas & courses

PWA installable sur Android. Base de données **locale au téléphone** (IndexedDB), fonctionne hors-ligne.
Deux listes tenues à jour : **repas à faire** et **liste de courses**. Ajouter une recette aux repas
pousse automatiquement tous ses ingrédients dans les courses.

## Installation (2 minutes)

1. Servir le dossier en **HTTPS** (obligatoire pour le service worker) :
   - Cloudflare Pages / Netlify / GitHub Pages : glisser-déposer le dossier ;
   - ou en local pour tester : `npx serve .` puis ouvrir `http://localhost:3000` (localhost est accepté).
2. Sur Android, ouvrir l'URL dans Chrome → menu ⋮ → **Installer l'application**.
3. L'icône apparaît sur l'écran d'accueil, l'app s'ouvre en plein écran.

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
- Jow : l'`recipeId` de l'URL (`app.jow.com/…?recipeId=…`) sert à interroger `api.jow.fr`,
  qui renvoie les quantités par couvert — multipliées par le nombre de couverts.

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
