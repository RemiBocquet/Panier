# Panier — repas & courses

PWA installable sur Android. Base de données **locale au téléphone** (IndexedDB), fonctionne hors-ligne.
Trois listes tenues à jour : **repas à faire**, **stock** et **liste de courses**. Ajouter une recette
aux repas la met en attente de validation ; c'est la validation de la semaine qui compare les besoins
au stock et pousse le manque réel dans les courses (voir [Stock et validation de la semaine](#stock-et-validation-de-la-semaine)).

## Installation

1. Sur Android, ouvrir l'URL dans Chrome → menu ⋮ → **Installer l'application**.
2. L'icône apparaît sur l'écran d'accueil, l'app s'ouvre en plein écran.

Un tutoriel interactif s'affiche automatiquement au premier lancement : il encadre les vrais
boutons de l'appli (onglets, Importer, Réglages...) directement dans l'écran normal, et se
tape dessus pour avancer — pas de simple diaporama à lire passivement. Il reste accessible
ensuite via le bouton **?** de la barre du haut.

Après une mise à jour, une fenêtre **Nouveautés** résume ce qui a changé depuis ta dernière
visite (`CHANGELOG` dans `index.html`, juste au-dessus d'`APP_VERSION`). Elle ne s'affiche
jamais au tout premier lancement.

## Les trois façons d'ajouter une recette

**Depuis un lien** — Recettes → Importer → Depuis un lien.
- Marmiton, CuisineAZ, 750g : lecture du JSON-LD `schema.org/Recipe` de la page.
- Jow : **utilise le lien de la page web**, pas le lien de partage de l'appli.
  Ouvrez la recette sur `jow.com`, copiez l'URL de la forme `jow.com/recipes/<nom>-<id>`.
  Les liens `app.jow.com/...?recipeId=...` sont aussi tentés, mais Jow ne garantit pas leur
  résolution : si ça échoue, passez par le lien de page.

**Rechercher sur Jow** — Recettes → Importer → Rechercher sur Jow.

**Depuis une photo (OCR)** — Recettes → Importer → Depuis une photo.
- Capture d'écran de n'importe quel site/appli, ou photo d'une page de livre.
- Le texte est lu **localement sur le téléphone** (Tesseract.js en WebAssembly). Rien n'est envoyé
  à un serveur. Le moteur (~3 Mo) est téléchargé au premier usage puis mis en cache: ensuite l'OCR
  marche hors-ligne.
- Le parseur repère la section « Ingrédients », ignore les étapes, pubs et temps de cuisson, et gère
  la mise en page Jow où le nom et la quantité sont sur deux lignes séparées.
- Les quantités reconnues sont **à vérifier** dans le formulaire avant enregistrement.

**Saisie manuelle** — champ « Coller une liste » : une ligne = un ingrédient, analysée automatiquement.

### Image d'une recette

Le bouton **Chercher une image libre de droits** (formulaire de recette) interroge
[Wikimedia Commons](https://commons.wikimedia.org), qui héberge des photos sous licence Creative
Commons ou domaine public. Aucune clé API à configurer, recherche anonyme depuis le navigateur.
Certaines licences (CC-BY) demandent de créditer l'auteur — nom et licence affichés sous chaque
vignette — si la recette est republiée ailleurs.
Seule l'URL de l'image choisie est conservée, comme pour une image importée depuis une recette web.

### Partager une recette

Le bouton **Partager la recette** (fiche recette) envoie ingrédients et préparation en texte via
le partage natif du téléphone (SMS, WhatsApp, mail...) — pratique pour la transmettre à quelqu'un
qui n'a pas l'app. Sans partage natif disponible, le texte est copié dans le presse-papiers.

### Note personnelle

Chaque recette peut avoir une note libre (astuces, ajustements, avis...), affichée en encart sur
la fiche recette. Distincte du contenu de la recette : elle ne se modifie que depuis son propre
petit écran, pas dans le formulaire « Modifier ».

## Fonctionnement des quantités

Les ingrédients sont agrégés par nom + famille d'unité :
- 200 g de farine + 0,3 kg de farine → **500 g de farine** (une seule ligne)
- masses (mg/g/kg) et volumes (ml/cl/dl/l) sont convertis dans une unité de base puis réaffichés
  lisiblement (500 g, 1,5 kg, 250 ml…)
- les unités non convertibles (gousse, pincée, sachet…) restent séparées, ce qui est le comportement
  voulu : 2 gousses d'ail et 1 c. à café d'ail en poudre ne se confondent pas.

Chaque article garde la trace des recettes qui l'ont demandé (affiché sous le nom). Retirer un repas
retire exactement sa contribution, sans toucher aux quantités venant des autres recettes ni aux articles ajoutés à la main.

Changer le nombre de personnes au moment d'ajouter un repas met les quantités à l'échelle. Réglages →
Foyer permet de fixer un nombre de personnes par défaut, proposé (et modifiable) à chaque ajout.

## Les rayons

La liste est triée par rayon de supermarché (fruits & légumes, viandes & poissons, crémerie,
boulangerie, épicerie salée/sucrée, boissons, surgelés, entretien).

## Filtrer les recettes

L'onglet Recettes propose une barre de recherche et des filtres cumulables. Tout est déduit des
ingrédients déjà enregistrés : rien à ressaisir.

- **Recherche** — porte sur le nom et les ingrédients : « poulet » trouve « Poulet basquaise »
  comme toute recette contenant du poulet. Insensible à la casse et aux accents.
- **Favoris** — recettes marquées d'une étoile (bouton ☆ sur la fiche recette).
- **Avec mon stock** — ne garde que les recettes dont tous les ingrédients sont déjà en stock.
  Les basiques de placard non suivis individuellement (sel, farine, huile...) sont supposés
  toujours disponibles et ne bloquent jamais ce filtre.
- **Végétarien / Végan** — végan exclut en plus les produits laitiers, les œufs et le miel.
- **Sans porc / Sans lactose / Sans gluten**
- **≤ 5 ingrédients** — pour les soirs pressés.
- **Jamais cuisiné** — masque les recettes déjà présentes dans la liste des repas.
- **＋ Plus** — exclure un ingrédient précis (« sans champignon ») et filtrer par provenance
  (Marmiton, Jow, CuisineAZ, 750g, photo, saisie manuelle, autre site).

Les filtres se combinent, et le compteur indique « N recettes sur M » avec un bouton de
réinitialisation. Le sélecteur de recette (Planifier un repas) dispose aussi d'une recherche.

### Ce que la détection sait faire, et ne sait pas faire

La classification repose sur des mots-clés appliqués aux noms d'ingrédients. Les pièges courants
sont traités : « lait de coco », « lait d'amande », « steak de soja » ou « farine de sarrasin » ne
déclenchent pas à tort les filtres lait, viande ou gluten.

Pour les filtres d'exclusion, le choix a été fait de **pencher du côté prudent** : en cas
d'ambiguïté, l'ingrédient est considéré comme présent, quitte à masquer une recette à tort.
Exemple : « saucisse » est comptée comme du porc, faute de précision.

Cela reste une heuristique, **pas une garantie**. Pour une allergie sérieuse ou un régime strict,
**vérifiez la liste des ingrédients de la recette**.

## Stock et validation de la semaine

L'onglet **Stock** liste ce que vous avez déjà. Tant qu'un repas n'est pas validé, il ne génère
aucune course : c'est la validation qui compare les besoins au stock et n'achète que le manque.

### Le cycle

1. **Planifier** — ajoute des recettes aux repas. Elles s'affichent en « en attente de
   validation » sans échéance ni course.
2. **Renseigner le stock** — à la main (« 500 g de semoule ») ou par photo.
3. **Valider la semaine** — un écran récapitule ce qui est déjà en stock et ce qui est
   à acheter. À la confirmation, le stock est déduit et la liste de courses ne contient
   que le manque réel.

Exemple : deux recettes utilisant 250 g et 150 g de semoule, avec 500 g en stock → aucun rachat,
et le stock tombe à 100 g. Si tu annules ensuite un repas validé, les quantités prélevées
retournent au stock.

### Quantité connue ou inconnue

Un article de stock peut avoir une quantité chiffrée (500 g) ou aucune. Sans quantité, il est
considéré comme **disponible en quantité suffisante** — le cas normal du placard (sel, épices,
huile), et le seul résultat honnête d'une photo, qui ne sait pas peser. Le rapprochement se fait
sur le nom, insensible à la casse et au pluriel ; les quantités ne sont comparées que si les
unités sont compatibles.

## Synchroniser deux téléphones (optionnel)

Désactivé par défaut. Une fois activé, les recettes, repas, stock et courses sont partagés entre
tes appareils — et uniquement les tiens.

Réglages → Synchro → **Générer un code sûr**, puis **Partager par QR code** : scanné avec
l'appareil photo natif de l'autre téléphone, il propose d'ouvrir Panier et configure la synchro
tout seul, sans ressaisir le code à la main. Le QR encode juste une URL de l'app (le code n'est
jamais envoyé à un serveur), et l'app l'efface de la barre d'adresse dès qu'elle l'a lu.

## Priorité et péremption

Le compte à rebours d'un ingrédient démarrequand l'article est coché dans la liste de courses, c'est-à-dire au moment de l'achat, pas au moment où la recette est planifiée.

L'échéance d'un repas est celle de son ingrédient le plus fragile. Un plat au saumon passe
donc devant un plat de viande rouge, lui-même devant un dahl de lentilles qui n'a aucune
contrainte.

L'onglet Repas se réorganise en conséquence :

- section **En priorité** en tête, triée par urgence, puis **Quand tu veux** ;
- badge sur chaque carte : « À faire aujourd'hui », « À faire demain », « En retard de 2 jours »,
  ou une date explicite au-delà de trois jours ;
- liseré coloré (rouge en retard, orange sous 24 h, jaune sous 3 jours) ;
- la ligne « à cause de : saumon » indique **quel ingrédient** impose la date — l'information
  qui permet d'agir ;
- un bandeau d'alerte en haut dès qu'un repas est à faire aujourd'hui, demain, ou en retard.

Un repas dont les ingrédients ne sont pas encore achetés n'affiche aucune échéance : le compte
à rebours n'a pas commencé.

### Les durées de conservation

Les durées visent la date de péremption d'un produit **fermé**, à partir de son achat — pas sa
fragilité une fois entamé. C'est cohérent avec le départ du compte à rebours : au moment où
l'article est coché dans les courses, il vient d'être acheté, donc encore scellé. Un yaourt,
une mozzarella ou des raviolis sous emballage tiennent ainsi plusieurs semaines, même s'ils
deviennent fragiles en quelques jours une fois ouverts — à surveiller une fois le produit
entamé, ce que l'app ne peut pas savoir.

| Durée | Exemples |
|---|---|
| 3 j | viande, volaille et poisson crus non transformés (frais, jamais fumés/cuits) |
| 5 j | champignons, salade et légumes-feuilles en sachet, burrata |
| 7 j | herbes fraîches en sachet, **lait frais** (pasteurisé, rayon réfrigéré), tomate, poivron, pain |
| 14 j | carotte, chou, agrumes, pomme, courge |
| 21 j | yaourt, fromage blanc, brie, camembert, mozzarella, fromage frais, tofu, crème (en pot/brique scellé), charcuterie et fumé/curé (jambon, lardons, bacon, chorizo, saumon fumé), pâtes/pâtons scellés (raviolis, tortellini, pâte à tarte, tortilla) |
| 30 j | beurre, pommes de terre, oignon, ail |
| 90 j | feta |
| — | farine, riz, pâtes, conserves, épices, huile, fromages à pâte dure (comté, gruyère, parmesan, emmental, cheddar…), **lait** (UHT par défaut) (aucune contrainte) |

« Lait » seul est traité comme du lait longue conservation (UHT) : c'est le cas largement
majoritaire en France, et il se garde des mois tant qu'il n'est pas ouvert. Précise « lait
frais » si c'est effectivement du lait pasteurisé au rayon réfrigéré, plus fragile (7 j).

Les valeurs restent volontairement prudentes : mieux vaut être alerté trop tôt que trop tard.
Ce sont des ordres de grandeur pour une conservation correcte, **pas une garantie sanitaire** :
la date sur l'emballage prime toujours, et un produit entamé se garde toujours moins longtemps
qu'un produit fermé.

Ces valeurs sont modifiables une par une dans Réglages → **Modifier les durées de péremption**
(recherche par nom, remise à zéro individuelle ou globale). C'est un réglage propre à l'appareil,
non synchronisé entre tes deux téléphones.

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

## Signaler un bug ou proposer une idée

Réglages → **🐛 Signaler un bug / proposer une idée** : un message vers
`panier.repas.courses@gmail.com`, avec version de l'app, appareil et date joints
automatiquement.

L'envoi est **silencieux et automatique** via le relais (Cloudflare Worker, endpoint
`/feedback` de `cors-relay.js`), qui passe par [Resend](https://resend.com) — gratuit jusqu'à
3000 emails/mois, sans domaine à configurer pour cet usage. Si Resend n'est pas configuré
(clé absente) ou injoignable, l'app **retombe automatiquement** sur l'appli mail du téléphone
(`mailto:`) : le message n'est jamais perdu, juste moins automatique.

Mise en place du côté serveur (facultative, une seule fois) :
1. Crée un compte sur [resend.com](https://resend.com) avec l'adresse qui doit recevoir les
   messages (ex. `panier.repas.courses@gmail.com`).
2. Dashboard → API Keys → crée une clé.
3. Sur ton Worker Cloudflare : Settings → Variables → ajoute un **secret** nommé
   `RESEND_API_KEY` (ou `wrangler secret put RESEND_API_KEY`).
