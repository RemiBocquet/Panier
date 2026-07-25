# Panier — repas & courses

PWA installable sur Android. Base de données **locale au téléphone** (IndexedDB), fonctionne hors-ligne.
Trois listes tenues à jour : **repas à faire**, **stock** et **liste de courses**. Ajouter une recette
aux repas la met en attente de validation ; c'est la validation de la semaine qui compare les besoins
au stock et pousse le manque réel dans les courses (voir [Stock et validation de la semaine](#stock-et-validation-de-la-semaine)).

## Installation

1. Sur Android, ouvrir l'URL dans Chrome → menu ⋮ → **Installer l'application**.
2. L'icône apparaît sur l'écran d'accueil, l'app s'ouvre en plein écran.

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

Désactivé par défaut. Une fois activé, les recettes, repas et courses sont partagés entre tes
appareils — et uniquement les tiens.

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

| Durée | Exemples |
|---|---|
| 3 j | viande, poisson, volaille, charcuterie fraîche, champignons, salade, herbes fraîches, crème |
| 4 j | lardons, jambon, mozzarella, tofu |
| 5 j | lait, courgette, brocoli, fruits rouges |
| 7 j | tomate, poivron, aubergine, pain, pâte à tarte |
| 10 j | yaourt, fromage blanc, brie, camembert |
| 14 j | carotte, chou, agrumes, pomme, courge |
| 21 j | œufs, beurre, fromages à pâte dure |
| 30 j | pommes de terre, oignon, ail |
| — | farine, riz, pâtes, conserves, épices, huile… (aucune contrainte) |

Les valeurs sont volontairement prudentes : mieux vaut être alerté trop tôt que trop tard.
Ce sont des ordres de grandeur pour une conservation correcte, **pas une garantie sanitaire** :
la date sur l'emballage prime toujours.

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
