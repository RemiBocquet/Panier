# Panier — repas & courses

PWA installable sur Android. Base de données **locale au téléphone** (IndexedDB), fonctionne hors-ligne.
Deux listes tenues à jour : **repas à faire** et **liste de courses**. Ajouter une recette aux repas
pousse automatiquement tous ses ingrédients dans les courses.

## Installation (2 minutes)

1. Sur Android, ouvrir l'URL dans Chrome → menu ⋮ → **Installer l'application**.
2. L'icône apparaît sur l'écran d'accueil, l'app s'ouvre en plein écran.

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

## Synchroniser deux téléphones (optionnel)

Désactivé par défaut. Une fois activé, les recettes, repas et courses sont partagés entre tes
appareils — et uniquement les tiens.

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
