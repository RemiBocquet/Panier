#!/usr/bin/env python3
"""
Panier — traduction du catalogue vers l'anglais.

Sert à deux choses : c'est la bibliothèque qu'importe recipe-server.py pour
répondre à `?lang=en`, et l'outil en ligne de commande qui entretient le
lexique. Python 3.7+, bibliothèque standard uniquement.

POURQUOI DEUX MÉCANISMES ET NON UN SEUL
---------------------------------------
Une recette contient deux natures de texte, et les traiter pareil serait une
faute.

  Le titre et les étapes sont de la prose. On les lit, on ne les compare
  jamais entre elles. Une traduction automatique y est parfaite, et son
  irrégularité — « faites revenir » rendu tantôt par « fry », tantôt par
  « brown » — n'a aucune conséquence.

  Les ingrédients et les unités, eux, sont des CLÉS. Le stock, les besoins des
  repas et la liste de courses fusionnent par nom : c'est tout le mécanisme de
  la validation de la semaine. Or une traduction automatique n'est pas
  déterministe. « blanc de poulet » sort tantôt « chicken breast », tantôt
  « white of chicken », et l'utilisateur se retrouve avec deux lignes distinctes
  dans ses courses pour le même produit. La fusion casse silencieusement, et
  c'est le genre de bogue qu'on ne comprend qu'après trois semaines.

D'où la règle tenue par ce fichier : les ingrédients passent par un LEXIQUE, un
dictionnaire figé fr → en. Le moteur de traduction ne sert qu'à REMPLIR ce
lexique quand un terme inconnu se présente — jamais à traduire à la volée. Une
fois écrit, un terme ne bouge plus : deux recettes contenant « blanc de poulet »
donneront toujours la même chaîne anglaise, aujourd'hui et dans six mois.

Le cache de recettes (recipe_tr) ne remplace PAS le lexique. Il évite de
repayer la traduction d'une même fiche ; le lexique, lui, garantit la cohérence
ENTRE fiches différentes. Seul le second protège la liste de courses.

OÙ VIVENT LES DONNÉES
---------------------
Tout va dans `<db>.tr`, à côté du catalogue — jamais dedans. Le catalogue est
ouvert en lecture seule par le service (le moissonneur peut tourner en même
temps), et un cache est de toute façon jetable : supprimer `<db>.tr` ne perd
rien qu'on ne puisse reconstruire, alors qu'une moisson représente des jours.

LE MOTEUR
---------
DeepL en premier pour la qualité, LibreTranslate en secours. Le secours n'est
pas décoratif : c'est lui qui fait que l'épuisement du quota DeepL ne casse
rien. Et si les deux manquent, la fiche part en français plutôt qu'en erreur —
une recette lisible dans la mauvaise langue vaut mieux qu'un écran d'échec.

    export PANIER_DEEPL_KEY=…            # ou <db>.deepl, en 0600
    export PANIER_LIBRETRANSLATE_URL=http://127.0.0.1:5000

EN LIGNE DE COMMANDE
--------------------
    python3 tools/translate.py --db … --seed        # pose le lexique de base
    python3 tools/translate.py --db … --extract     # ce qui manque, par fréquence
    python3 tools/translate.py --db … --fill 500    # remplit les 500 plus fréquents
    python3 tools/translate.py --db … --export t.tsv  # relecture à la main
    python3 tools/translate.py --db … --import t.tsv
    python3 tools/translate.py --db … --stats
"""

import argparse
import collections
import json
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request

TARGET_DEFAULT = "EN-GB"

# DeepL accepte 50 textes par appel. On reste en dessous : la limite réelle est
# aussi une taille de requête, et un ingrédient occasionnel très long ne doit
# pas faire basculer tout le lot au-delà.
BATCH = 40

# Au-delà, ce n'est plus un nom d'ingrédient mais une phrase mal découpée par le
# moissonneur. On la traduit quand même, mais on refuse de l'écrire au lexique :
# elle n'a aucune chance de se représenter à l'identique, et n'y ferait que du
# volume mort.
LEXICON_MAX = 60


def strip_accents(s):
    return "".join(
        c for c in unicodedata.normalize("NFD", str(s))
        if unicodedata.category(c) != "Mn"
    )


# La ligature ne se décompose pas en NFD : strip_accents() ne la voit pas, il
# faut la remplacer explicitement. Ça n'a rien d'un détail — « Œuf(s) » est
# l'ingrédient le plus fréquent de tout le catalogue.
LIGATURES = {"œ": "oe", "Œ": "oe", "æ": "ae", "Æ": "ae"}

# « Œuf(s) », « Poireau(x) », « Pomme(s) de terre » : CuisineAZ marque le pluriel
# ainsi. Le moissonneur enlève déjà ce suffixe des UNITÉS (« 8 tranche(s) »),
# mais pas des noms d'ingrédients, où il crée une deuxième graphie pour chaque
# terme.
PLURAL_MARK = re.compile(r"\((?:s|x|es)\)", re.IGNORECASE)

# Pluriel ordinaire : « échalotes » et « échalote » sont le même produit, et le
# catalogue emploie les deux au gré des sites.
#
# La règle n'a pas à être linguistiquement juste, seulement IDENTIQUE des deux
# côtés : « ananas » devient « anana » et « gros » devient « gro », sans que ça
# gêne, puisque le lexique est replié pareil. Le seul vrai risque serait de
# réunir deux ingrédients distincts, ce qui ne se produit pas en cuisine.
#
# La lookbehind impose au moins trois caractères devant, donc quatre en tout :
# sans elle « jus » deviendrait « ju » et « pas » « pa ».
PLURAL_WORD = re.compile(r"(?<=\w{3})[sx]\b")


def norm(s):
    """Clé du lexique.

    Volontairement PLUS agressive que le norm() de recipe-server.py, et il ne
    faut surtout pas les réunir : celui-là indexe des titres pour une recherche
    plein texte, celui-ci fabrique une clé de dictionnaire. Ce qui se passe ici
    ne doit jamais toucher l'index de recherche.

    Quatre replis, tous mesurés sur le catalogue réel et tous coûteux à
    négliger :

      accents et casse   « Oignon », « oignon », « OIGNONS »
      apostrophes        le catalogue écrit « huile d’olive » (U+2019), le
                         lexique « huile d'olive ». Sans ça, les deux graphies
                         de l'huile d'olive ratent — plus de 30 000 occurrences.
      ligatures          « Œuf(s) », « jaune d’œuf »
      marques de pluriel « Œuf(s) » et « Œufs » sont le même ingrédient
      pluriel ordinaire  « échalotes » et « échalote » aussi
      parenthèses        « Crème fraîche (épaisse) » = « crème fraîche épaisse »
      ponctuation        « Sel, poivre » et « Sel poivre »

    Conséquence assumée : le singulier et le pluriel d'un ingrédient n'ont plus
    qu'UNE entrée de lexique. C'est voulu — un nom d'ingrédient est une clé de
    fusion pour la liste de courses, et deux formes valaient deux lignes.
    """
    s = str(s or "")
    for a in ("’", "‘", "‛", "`", "´"):
        s = s.replace(a, "'")
    for lig, rep in LIGATURES.items():
        s = s.replace(lig, rep)
    s = strip_accents(s).lower()
    # D'abord la marque de pluriel entre parenthèses, ENSUITE les parenthèses
    # restantes : « Œuf(s) » perd son suffixe, « (épaisse) » garde son contenu.
    s = PLURAL_MARK.sub("", s)
    s = re.sub(r"[()\[\]]", " ", s)
    s = re.sub(r"[,;:.]+", " ", s)
    s = PLURAL_WORD.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------
# Les unités : une table, jamais de traduction automatique
#
# scrape-recipes.py ramène toutes les graphies rencontrées à une trentaine de
# valeurs canoniques (UNITS, en haut de ce fichier-là). L'ensemble étant fermé
# et connu, l'écrire à la main coûte dix minutes et rend le résultat exact et
# stable pour toujours. Y envoyer un moteur de traduction serait payer pour
# obtenir moins bien.
#
# Le système reste métrique : la cuisine britannique pèse en grammes, et
# convertir en cups introduirait des arrondis dans des quantités qui servent
# ensuite à calculer un manque de stock.
# --------------------------------------------------------------------------
UNITS_EN = {
    "kg": "kg",
    "g": "g",
    "mg": "mg",
    "l": "l",
    "dl": "dl",
    "cl": "cl",
    "ml": "ml",
    "oz": "oz",
    "lb": "lb",
    "c. à soupe": "tbsp",
    "c. à café": "tsp",
    "pincée": "pinch",
    "gousse": "clove",
    "tranche": "slice",
    "sachet": "sachet",
    "paquet": "packet",
    "boîte": "tin",
    "bocal": "jar",
    "botte": "bunch",
    "bouquet": "bouquet",
    "brin": "sprig",
    "feuille": "leaf",
    "verre": "glass",
    "tasse": "cup",
    "pot": "pot",
    "morceau": "piece",
    "boule": "ball",
    "poignée": "handful",
    "bâton": "stick",
    "goutte": "drop",
}

# --------------------------------------------------------------------------
# Le lexique de départ
#
# Environ trois cents entrées écrites à la main : le noyau qu'on retrouve dans
# la quasi-totalité des recettes françaises. La distribution des ingrédients est
# très inégale — quelques centaines de termes couvrent l'essentiel des
# occurrences, et la longue traîne est faite de variantes qu'on ne verra qu'une
# fois. Ce noyau-là mérite d'être juste ; le reste peut venir de la machine.
#
# Ces clés sont écrites accentuées pour rester lisibles ; elles passent par
# norm() au chargement.
# --------------------------------------------------------------------------
SEED = {
    # Base
    "sel": "salt",
    "sel fin": "fine salt",
    "gros sel": "coarse salt",
    "fleur de sel": "fleur de sel",
    "poivre": "pepper",
    "poivre noir": "black pepper",
    "poivre blanc": "white pepper",
    "sel et poivre": "salt and pepper",
    # « Sel, poivre » et « Sel poivre » : deuxième et cinquième ingrédients les
    # plus fréquents du catalogue. La ponctuation étant repliée, une seule
    # entrée couvre les deux graphies.
    "sel poivre": "salt and pepper",
    "sel ou sel fin": "salt",
    "sel et poivre du moulin": "salt and freshly ground pepper",
    "sel poivre du moulin": "salt and freshly ground pepper",
    "poivre du moulin": "freshly ground pepper",
    "sucre": "sugar",
    "sucre en poudre": "caster sugar",
    "sucre semoule": "caster sugar",
    "sucre glace": "icing sugar",
    "sucre roux": "brown sugar",
    "sucre vanillé": "vanilla sugar",
    "cassonade": "soft brown sugar",
    "farine": "flour",
    "farine de blé": "plain flour",
    "farine t45": "plain flour",
    "farine t55": "plain flour",
    "farine de maïs": "cornmeal",
    "beurre": "butter",
    "beurre doux": "unsalted butter",
    "beurre demi-sel": "salted butter",
    "beurre salé": "salted butter",
    "beurre mou": "softened butter",
    "beurre fondu": "melted butter",
    "margarine": "margarine",
    "huile": "oil",
    "huile d'olive": "olive oil",
    "huile de tournesol": "sunflower oil",
    "huile de colza": "rapeseed oil",
    "huile d'arachide": "groundnut oil",
    "huile de sésame": "sesame oil",
    "vinaigre": "vinegar",
    "vinaigre balsamique": "balsamic vinegar",
    "vinaigre de vin": "wine vinegar",
    "vinaigre de cidre": "cider vinegar",
    "eau": "water",
    "glaçons": "ice cubes",
    "levure": "yeast",
    "levure chimique": "baking powder",
    "levure de boulanger": "baker's yeast",
    "bicarbonate de soude": "bicarbonate of soda",
    "maïzena": "cornflour",
    "fécule de maïs": "cornflour",
    "fécule de pomme de terre": "potato starch",
    "chapelure": "breadcrumbs",
    "gélatine": "gelatine",
    "agar-agar": "agar-agar",
    "miel": "honey",
    "sirop d'érable": "maple syrup",
    "confiture": "jam",
    "moutarde": "mustard",
    "moutarde de dijon": "Dijon mustard",
    "mayonnaise": "mayonnaise",
    "ketchup": "ketchup",
    "sauce soja": "soy sauce",
    "sauce tomate": "tomato sauce",
    "concentré de tomate": "tomato purée",
    "coulis de tomate": "tomato passata",
    "tomates pelées": "peeled tomatoes",
    # Œufs et laitages
    "oeufs": "eggs",
    "jaunes d'oeufs": "egg yolks",
    "blancs d'oeufs": "egg whites",
    "lait": "milk",
    "lait entier": "whole milk",
    "lait demi-écrémé": "semi-skimmed milk",
    "lait de coco": "coconut milk",
    "crème de coco": "coconut cream",
    "crème fraîche": "crème fraîche",
    "crème fraîche épaisse": "thick crème fraîche",
    "crème liquide": "single cream",
    "crème entière": "double cream",
    "crème épaisse": "thick cream",
    "yaourt": "yoghurt",
    "yaourt nature": "plain yoghurt",
    "fromage": "cheese",
    "fromage râpé": "grated cheese",
    "fromage blanc": "fromage blanc",
    "gruyère": "Gruyère",
    "gruyère râpé": "grated Gruyère",
    "emmental râpé": "grated Emmental",
    "parmesan râpé": "grated Parmesan",
    "emmental": "Emmental",
    "comté": "Comté",
    "parmesan": "Parmesan",
    "mozzarella": "mozzarella",
    "chèvre": "goat's cheese",
    "fromage de chèvre": "goat's cheese",
    "roquefort": "Roquefort",
    "bleu": "blue cheese",
    "feta": "feta",
    "ricotta": "ricotta",
    "mascarpone": "mascarpone",
    # Légumes
    "oignons": "onions",
    "oignon rouge": "red onion",
    "oignon jaune": "yellow onion",
    "oignon blanc": "white onion",
    "échalote": "shallot",
    "ail": "garlic",
    "gousses d'ail": "garlic cloves",
    "carottes": "carrots",
    "pommes de terre": "potatoes",
    "patate douce": "sweet potato",
    "tomates": "tomatoes",
    "tomates cerises": "cherry tomatoes",
    "courgettes": "courgettes",
    "aubergine": "aubergine",
    "poivron": "bell pepper",
    "poivron rouge": "red pepper",
    "poivron vert": "green pepper",
    "poivron jaune": "yellow pepper",
    "champignons": "mushrooms",
    "champignons de paris": "button mushrooms",
    "poireaux": "leeks",
    "céleri": "celery",
    "céleri-rave": "celeriac",
    "navet": "turnip",
    "chou": "cabbage",
    "chou-fleur": "cauliflower",
    "chou rouge": "red cabbage",
    "chou de bruxelles": "Brussels sprout",
    "brocoli": "broccoli",
    "épinards": "spinach",
    "haricots verts": "green beans",
    "haricots blancs": "haricot beans",
    "haricots rouges": "kidney beans",
    "petits pois": "peas",
    "lentilles": "lentils",
    "pois chiches": "chickpeas",
    "fèves": "broad beans",
    "maïs": "sweetcorn",
    "salade": "lettuce",
    "laitue": "lettuce",
    "roquette": "rocket",
    "mâche": "lamb's lettuce",
    "endive": "chicory",
    "courge": "squash",
    "potiron": "pumpkin",
    "butternut": "butternut squash",
    "betterave": "beetroot",
    "radis": "radish",
    "concombre": "cucumber",
    "avocat": "avocado",
    "fenouil": "fennel",
    "artichaut": "artichoke",
    "asperges": "asparagus",
    "olives": "olives",
    "olives noires": "black olives",
    "olives vertes": "green olives",
    "cornichons": "gherkins",
    "câpres": "capers",
    # Herbes et épices
    "persil": "parsley",
    "persil plat": "flat-leaf parsley",
    "ciboulette": "chives",
    "basilic": "basil",
    "thym": "thyme",
    "laurier": "bay leaf",
    "feuille de laurier": "bay leaf",
    "romarin": "rosemary",
    "menthe": "mint",
    "coriandre": "coriander",
    "estragon": "tarragon",
    "aneth": "dill",
    "origan": "oregano",
    "sauge": "sage",
    "herbes de provence": "herbes de Provence",
    "bouquet garni": "bouquet garni",
    "curry": "curry powder",
    "curcuma": "turmeric",
    "cumin": "cumin",
    "paprika": "paprika",
    "piment": "chilli",
    "piment d'espelette": "Espelette pepper",
    "piment de cayenne": "cayenne pepper",
    "cannelle": "cinnamon",
    "muscade": "nutmeg",
    "noix de muscade": "nutmeg",
    "gingembre": "ginger",
    "safran": "saffron",
    "clou de girofle": "clove",
    "anis étoilé": "star anise",
    "vanille": "vanilla",
    "gousse de vanille": "vanilla pod",
    "extrait de vanille": "vanilla extract",
    "cardamome": "cardamom",
    "graines de sésame": "sesame seeds",
    "quatre-épices": "mixed spice",
    "ras el hanout": "ras el hanout",
    # Viandes et poissons
    "poulet": "chicken",
    "blancs de poulet": "chicken breasts",
    "filet de poulet": "chicken breast",
    "cuisse de poulet": "chicken thigh",
    "escalope de poulet": "chicken escalope",
    "boeuf": "beef",
    "steak": "steak",
    "steak haché": "beef mince",
    "viande hachée": "minced meat",
    "boeuf haché": "beef mince",
    "porc": "pork",
    "filet mignon": "pork tenderloin",
    "échine de porc": "pork shoulder",
    "lardons": "lardons",
    "lard": "bacon",
    "bacon": "bacon",
    "jambon": "ham",
    "jambon blanc": "cooked ham",
    "jambon cru": "cured ham",
    "saucisses": "sausages",
    "saucisson": "saucisson",
    "chorizo": "chorizo",
    "merguez": "merguez",
    "veau": "veal",
    "agneau": "lamb",
    "gigot d'agneau": "leg of lamb",
    "canard": "duck",
    "magret de canard": "duck breast",
    "dinde": "turkey",
    "lapin": "rabbit",
    "poisson": "fish",
    "saumon": "salmon",
    "pavé de saumon": "salmon fillet",
    "saumon fumé": "smoked salmon",
    "cabillaud": "cod",
    "colin": "hake",
    "thon": "tuna",
    "truite": "trout",
    "crevettes": "prawns",
    "moules": "mussels",
    "noix de saint-jacques": "scallops",
    "calamars": "squid",
    "crabe": "crab",
    "anchois": "anchovies",
    "sardines": "sardines",
    "surimi": "surimi",
    # Féculents et boulangerie
    "riz": "rice",
    "riz basmati": "basmati rice",
    "pâtes": "pasta",
    "spaghetti": "spaghetti",
    "tagliatelles": "tagliatelle",
    "penne": "penne",
    "lasagnes": "lasagne sheets",
    "macaroni": "macaroni",
    "semoule": "couscous",
    "couscous": "couscous",
    "boulgour": "bulgur wheat",
    "quinoa": "quinoa",
    "pain": "bread",
    "pain de mie": "sliced white bread",
    "baguette": "baguette",
    "biscuits": "biscuits",
    "pâte brisée": "shortcrust pastry",
    "pâte feuilletée": "puff pastry",
    "pâte sablée": "sweet shortcrust pastry",
    "pâte à pizza": "pizza dough",
    # Sucré
    "chocolat": "chocolate",
    "chocolat noir": "dark chocolate",
    "chocolat au lait": "milk chocolate",
    "chocolat blanc": "white chocolate",
    "cacao": "cocoa",
    "cacao en poudre": "cocoa powder",
    "noix": "walnuts",
    "noisettes": "hazelnuts",
    "amandes": "almonds",
    "amandes effilées": "flaked almonds",
    "poudre d'amandes": "ground almonds",
    "pignons de pin": "pine nuts",
    "pistaches": "pistachios",
    "raisins secs": "raisins",
    "noix de coco": "coconut",
    "noix de coco râpée": "desiccated coconut",
    # Bouillons et alcools
    "bouillon": "stock",
    "bouillon de volaille": "chicken stock",
    "bouillon de boeuf": "beef stock",
    "bouillon de légumes": "vegetable stock",
    "cube de bouillon": "stock cube",
    "fond de veau": "veal stock",
    "vin blanc": "white wine",
    "vin blanc sec": "dry white wine",
    "vin rouge": "red wine",
    "bière": "beer",
    "cidre": "cider",
    "rhum": "rum",
    "cognac": "cognac",
    # Fruits
    "pommes": "apples",
    "poire": "pear",
    "banane": "banana",
    "fraises": "strawberries",
    "framboises": "raspberries",
    "myrtilles": "blueberries",
    "cerises": "cherries",
    "abricots": "apricots",
    "pêche": "peach",
    "prunes": "plums",
    "raisin": "grapes",
    "orange": "orange",
    "citron": "lemon",
    "citron vert": "lime",
    "jus de citron": "lemon juice",
    "zeste de citron": "lemon zest",
    "pamplemousse": "grapefruit",
    "ananas": "pineapple",
    "mangue": "mango",
    "kiwi": "kiwi",
    "melon": "melon",
    "pastèque": "watermelon",
    "figues": "figs",
    "dattes": "dates",
    "pruneaux": "prunes",
    "rhubarbe": "rhubarb",
    "fruits rouges": "red berries",
    # --------------------------------------------------------------------
    # Deuxième passe, tirée d'un --extract sur le catalogue réel (154 789
    # fiches) : les plus fréquents des manquants une fois la normalisation
    # corrigée. Tous vérifiés à la main.
    # --------------------------------------------------------------------
    "oeufs entiers": "whole eggs",
    "jaunes d'oeufs battus": "beaten egg yolks",
    "amandes en poudre": "ground almonds",
    "cerneaux de noix": "walnut halves",
    "café": "coffee",
    "cannelle en poudre": "ground cinnamon",
    "cumin en poudre": "ground cumin",
    "gingembre en poudre": "ground ginger",
    "crème chantilly": "whipped cream",
    "crème": "cream",
    "crème fleurette": "whipping cream",
    "crème fraîche liquide": "single cream",
    "crème liquide entière": "double cream",
    "lait concentré sucré": "condensed milk",
    "fromage frais": "soft cheese",
    "chèvre frais": "fresh goat's cheese",
    "lardons fumés": "smoked lardons",
    "citron jaune": "lemon",
    "jus d'orange": "orange juice",
    "persil haché": "chopped parsley",
    "menthe fraîche": "fresh mint",
    "cerfeuil": "chervil",
    "beurre tendre": "softened butter",
    "miel liquide": "runny honey",
    "huile de friture": "frying oil",
    "blancs de poireaux": "leek whites",
    "levure de boulangerie": "baker's yeast",
    "pain de campagne": "country bread",
    "feuilles de brick": "brick pastry sheets",
    "brick": "brick pastry",
    # --------------------------------------------------------------------
    # Troisième passe (156 889 fiches, couverture 69 %). Ceux-ci sont écrits
    # à la main plutôt que laissés à --fill parce qu'une traduction
    # automatique s'y trompe de façon prévisible : « beurre pommade » devient
    # « pomade butter », « fumet » devient « aroma », « chocolat pâtissier »
    # devient « pastry chocolate ». Ce sont des termes de métier, pas des
    # mots ordinaires.
    # --------------------------------------------------------------------
    "beurre pommade": "softened butter",
    "beurre ramolli": "softened butter",
    "beurre non salé": "unsalted butter",
    "noix de beurre": "knob of butter",
    "filet d'huile d'olive": "drizzle of olive oil",
    "huile végétale": "vegetable oil",
    "sucre poudre": "caster sugar",
    "chocolat pâtissier": "cooking chocolate",
    "pépites de chocolat": "chocolate chips",
    "potimarron": "red kuri squash",
    "fumet de poisson": "fish stock",
    "bouillon de poulet": "chicken stock",
    "bûche de chèvre": "goat's cheese log",
    "fromage de chèvre frais": "fresh goat's cheese",
    "fines herbes": "mixed herbs",
    "eau de fleur d'oranger": "orange blossom water",
    "fleur d'oranger": "orange blossom water",
    "vinaigre de xérès": "sherry vinegar",
    "moutarde à l'ancienne": "wholegrain mustard",
    "spéculoos": "speculoos biscuits",
    "nutella": "Nutella",
    "foie gras": "foie gras",
    "eau tiède": "lukewarm water",
    "coriandre fraîche": "fresh coriander",
    "persil frais": "fresh parsley",
    "persil ciselé": "chopped parsley",
    "gingembre frais": "fresh ginger",
    "vanille liquide": "vanilla extract",
    "vanille en poudre": "vanilla powder",
    "curry en poudre": "curry powder",
    "lait écrémé": "skimmed milk",
    "tomates séchées": "sun-dried tomatoes",
    "tomate concassée": "chopped tomatoes",
    "asperges vertes": "green asparagus",
    "oignons émincés": "sliced onions",
    "crevettes roses": "pink prawns",
    "haricots rosés": "pinto beans",
    "flocons d'avoine": "rolled oats",
    "jus de citron vert": "lime juice",
    "zeste de citron vert": "lime zest",
    "épices": "spices",
    "liqueur": "liqueur",
}


def _en_variants(phrase):
    """Le terme anglais et ses formes de nombre voisines.

    Le pendant, côté anglais, du repli de pluriel de norm(). Seul le dernier
    mot varie : « red onion » / « red onions », jamais « reds onion ».
    """
    words = phrase.split()
    if not words:
        return [phrase]
    last, head = words[-1], words[:-1]
    alts = [last, last + "s", last + "es"]
    if last.endswith("es") and len(last) > 4:
        alts.append(last[:-2])
    if last.endswith("s") and len(last) > 3:
        alts.append(last[:-1])
    out, seen = [], set()
    for a in alts:
        if a not in seen:
            seen.add(a)
            out.append(" ".join(head + [a]))
    return out


class TranslationUnavailable(Exception):
    """Aucun moteur n'a pu répondre. L'appelant sert le français."""


# --------------------------------------------------------------------------
# Les moteurs
# --------------------------------------------------------------------------
def _post_json(url, payload, headers, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Translator:
    """DeepL d'abord, LibreTranslate en secours.

    Le secours n'est pas de la coquetterie : le palier gratuit de DeepL est
    mensuel, et le jour où il tombe, tout un pan de l'application s'arrêterait
    sans lui. Un LibreTranslate sur le Pi coûte un conteneur et rend le service
    indépendant de l'extérieur — moins bon, mais toujours là.

    L'ordre est délibérément figé. Alterner selon la charge ferait varier la
    traduction d'un même terme d'un appel à l'autre, ce qui est exactement ce
    que le lexique existe pour empêcher.
    """

    def __init__(
        self,
        deepl_key=None,
        libre_url=None,
        libre_key=None,
        target=TARGET_DEFAULT,
        timeout=20,
    ):
        self.deepl_key = (deepl_key or "").strip() or None
        self.libre_url = (libre_url or "").strip().rstrip("/") or None
        self.libre_key = (libre_key or "").strip() or None
        self.target = target
        self.timeout = timeout
        # Une panne franche de DeepL (clé refusée, quota épuisé) ne doit pas être
        # repayée d'un aller-retour réseau à chaque recette. On la retient un
        # moment, puis on retente : un quota se renouvelle.
        self._deepl_muted_until = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, db_path=None, target=TARGET_DEFAULT):
        """Clés lues dans l'environnement, ou dans un fichier à côté de la base.

        Le fichier suit exactement la convention du secret de signature de
        recipe-server.py : rangé en 0600 près du SQLite, hors du dépôt. Une clé
        DeepL est un moyen de paiement — elle n'a rien à faire dans git, et pas
        davantage dans une ligne de commande que `ps` expose.
        """
        key = os.environ.get("PANIER_DEEPL_KEY", "")
        if not key and db_path:
            path = db_path + ".deepl"
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    key = f.read().strip()
        return cls(
            deepl_key=key,
            libre_url=os.environ.get("PANIER_LIBRETRANSLATE_URL", ""),
            libre_key=os.environ.get("PANIER_LIBRETRANSLATE_KEY", ""),
            target=target,
        )

    def available(self):
        return bool(self.deepl_key or self.libre_url)

    def describe(self):
        parts = []
        if self.deepl_key:
            parts.append("DeepL")
        if self.libre_url:
            parts.append("LibreTranslate (%s)" % self.libre_url)
        return " puis ".join(parts) if parts else "aucun"

    # -- DeepL ------------------------------------------------------------
    def _deepl_host(self):
        # Les clés du palier gratuit se terminent par « :fx » et ne répondent
        # QUE sur api-free. Le deviner évite un réglage de plus à se tromper.
        return (
            "https://api-free.deepl.com"
            if self.deepl_key.endswith(":fx")
            else "https://api.deepl.com"
        )

    def _deepl(self, texts, context=None):
        payload = {
            "text": texts,
            "source_lang": "FR",
            "target_lang": self.target,
        }
        if context:
            # `context` oriente la traduction sans être traduit lui-même. C'est
            # ce qui fait la différence entre « blanc de poulet » → « chicken
            # breast » et → « white of chicken » : hors contexte, un nom
            # d'ingrédient isolé n'a pas de quoi lever l'ambiguïté.
            payload["context"] = context
        out = _post_json(
            self._deepl_host() + "/v2/translate",
            payload,
            {"Authorization": "DeepL-Auth-Key " + self.deepl_key},
            self.timeout,
        )
        return [t["text"] for t in out["translations"]]

    def usage(self):
        """Quota DeepL consommé, ou None si pas de clé."""
        if not self.deepl_key:
            return None
        req = urllib.request.Request(self._deepl_host() + "/v2/usage")
        req.add_header("Authorization", "DeepL-Auth-Key " + self.deepl_key)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # -- LibreTranslate ---------------------------------------------------
    def _libre(self, texts):
        payload = {
            "q": texts,
            "source": "fr",
            "target": "en",
            "format": "text",
        }
        if self.libre_key:
            payload["api_key"] = self.libre_key
        out = _post_json(
            self.libre_url + "/translate", payload, {}, self.timeout
        )
        res = out.get("translatedText")
        # L'API rend une liste pour une liste, une chaîne pour une chaîne. Les
        # instances anciennes ne gèrent pas les listes : on retombe alors sur
        # des appels un par un plutôt que de renoncer.
        if isinstance(res, list):
            return res
        if len(texts) == 1 and isinstance(res, str):
            return [res]
        return [
            _post_json(
                self.libre_url + "/translate",
                dict(payload, q=t),
                {},
                self.timeout,
            )["translatedText"]
            for t in texts
        ]

    # -- Façade -----------------------------------------------------------
    def translate(self, texts, context=None):
        """Traduit une liste de textes. Rend (résultats, nom du moteur).

        Les chaînes vides ne partent pas sur le réseau : les étapes d'une
        recette en contiennent régulièrement, et les envoyer coûterait des
        caractères facturés pour rien.
        """
        texts = list(texts)
        idx = [i for i, t in enumerate(texts) if (t or "").strip()]
        if not idx:
            return list(texts), "aucun"
        payload = [texts[i] for i in idx]

        out, engine = None, None
        with self._lock:
            deepl_ok = self.deepl_key and time.time() >= self._deepl_muted_until
        if deepl_ok:
            try:
                out, engine = self._chunked(self._deepl, payload, context), "deepl"
            except Exception as e:
                # 456 = quota épuisé, 403 = clé refusée : inutile d'insister
                # avant un moment. Le reste (réseau, 5xx) peut être passager,
                # mais la mise en sourdine courte évite de s'acharner.
                code = getattr(e, "code", None)
                with self._lock:
                    self._deepl_muted_until = time.time() + (
                        3600 if code in (403, 456) else 60
                    )
                sys.stderr.write("DeepL indisponible (%s), repli.\n" % e)

        if out is None and self.libre_url:
            try:
                out, engine = self._chunked(self._libre, payload), "libretranslate"
            except Exception as e:
                sys.stderr.write("LibreTranslate indisponible (%s).\n" % e)

        if out is None:
            raise TranslationUnavailable(self.describe())

        merged = list(texts)
        for i, t in zip(idx, out):
            merged[i] = t
        return merged, engine

    def _chunked(self, fn, texts, *extra):
        out = []
        for i in range(0, len(texts), BATCH):
            out.extend(fn(texts[i : i + BATCH], *extra))
        if len(out) != len(texts):
            raise ValueError("le moteur a rendu %d textes pour %d" % (len(out), len(texts)))
        return out


# --------------------------------------------------------------------------
# Le magasin : lexique + cache
# --------------------------------------------------------------------------
STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lexicon(
  fr     TEXT NOT NULL,          -- clé normalisée par norm()
  lang   TEXT NOT NULL,
  en     TEXT NOT NULL,
  source TEXT NOT NULL,          -- seed | manual | deepl | libretranslate
  ts     INTEGER NOT NULL,
  PRIMARY KEY(fr, lang)
);
-- Le sens inverse sert la recherche : l'utilisateur anglais tape « chicken »
-- dans un index français. Un index sur `en` suffit, la table est petite.
CREATE INDEX IF NOT EXISTS lexicon_en ON lexicon(lang, en);

CREATE TABLE IF NOT EXISTS recipe_tr(
  id     INTEGER NOT NULL,       -- = urls.rowid du catalogue
  lang   TEXT NOT NULL,
  json   TEXT NOT NULL,
  engine TEXT NOT NULL,
  ts     INTEGER NOT NULL,
  PRIMARY KEY(id, lang)
);

-- Les titres à part : une recherche en rend vingt, dont dix-neuf ne seront
-- jamais ouvertes. Les traduire entiers serait payer pour du texte que
-- personne ne lira.
CREATE TABLE IF NOT EXISTS title_tr(
  id     INTEGER NOT NULL,
  lang   TEXT NOT NULL,
  title  TEXT NOT NULL,
  ts     INTEGER NOT NULL,
  PRIMARY KEY(id, lang)
);
"""


class TrStore:
    """Lexique et caches, dans `<db>.tr`.

    Une connexion par fil, comme le Catalog : sqlite3 l'exige. En WAL, pour que
    les lectures des autres fils ne soient pas bloquées par l'écriture d'une
    traduction qui vient d'arriver.
    """

    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        db = self._db()
        db.executescript(STORE_SCHEMA)
        db.commit()
        # Deux requêtes simultanées sur la même recette inconnue paieraient deux
        # fois la même traduction. Ce dictionnaire de verrous fait que la
        # seconde attend et lit le cache que la première vient d'écrire.
        self._inflight = {}
        self._inflight_lock = threading.Lock()

    def _db(self):
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=30)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            self._local.db = db
        return db

    def lock_for(self, key):
        with self._inflight_lock:
            lk = self._inflight.get(key)
            if lk is None:
                lk = self._inflight[key] = threading.Lock()
            return lk

    # -- lexique ----------------------------------------------------------
    def lookup(self, names, lang="en"):
        """Rend {nom brut: traduction} pour ceux qui sont déjà connus."""
        names = [n for n in names if n]
        if not names:
            return {}
        # Une clé normalisée peut venir de PLUSIEURS noms bruts — « Sel » et
        # « sel », « Œuf(s) » et « oeufs ». Les ranger dans un dict clé → nom
        # perdrait tous les doublons sauf un, et ceux-là seraient déclarés
        # inconnus alors qu'ils sont au lexique : c'est ce qui faisait annoncer
        # 21 % de couverture au lieu de la vraie.
        keys = {}
        for n in names:
            keys.setdefault(norm(n), []).append(n)
        out = {}
        db = self._db()
        # Par paquets : SQLite plafonne le nombre de paramètres d'une requête.
        ks = list(keys)
        for i in range(0, len(ks), 400):
            chunk = ks[i : i + 400]
            qs = ",".join("?" * len(chunk))
            for fr, en in db.execute(
                "SELECT fr, en FROM lexicon WHERE lang=? AND fr IN (%s)" % qs,
                [lang] + chunk,
            ):
                for raw in keys[fr]:
                    out[raw] = en
        return out

    def put(self, rows, source, lang="en"):
        """rows : itérable de (fr, en). Les termes déjà connus ne bougent pas.

        INSERT OR IGNORE, et non REPLACE : une entrée relue et corrigée à la
        main ne doit jamais être réécrasée par la machine au prochain passage.
        Une correction se fait avec --import, qui lui écrase délibérément.
        """
        now = int(time.time())
        vals = [
            (norm(fr), lang, en.strip(), source, now)
            for fr, en in rows
            if fr and en and en.strip() and len(norm(fr)) <= LEXICON_MAX
        ]
        if not vals:
            return 0
        db = self._db()
        cur = db.executemany(
            "INSERT OR IGNORE INTO lexicon(fr, lang, en, source, ts) VALUES(?,?,?,?,?)",
            vals,
        )
        db.commit()
        return cur.rowcount

    def overwrite(self, rows, source, lang="en"):
        now = int(time.time())
        vals = [
            (norm(fr), lang, en.strip(), source, now)
            for fr, en in rows
            if fr and en and en.strip()
        ]
        db = self._db()
        db.executemany(
            "INSERT OR REPLACE INTO lexicon(fr, lang, en, source, ts) VALUES(?,?,?,?,?)",
            vals,
        )
        db.commit()
        return len(vals)

    def to_french(self, q, lang="en", maxgram=3):
        """Ramène une saisie anglaise vers le français de l'index.

        Rend (requête française, mots restés anglais).

        La correspondance est gloutonne sur les groupes de mots, du plus long au
        plus court, et c'est indispensable : mot à mot, « chicken breast »
        donnerait « poulet breast », et comme FTS5 exige TOUS les termes d'une
        requête, la recherche ne rendrait rien. En trigrammes d'abord, elle
        trouve « blanc de poulet » d'un coup.

        Les mots sans équivalent connu sont gardés tels quels : beaucoup de
        termes de cuisine passent la frontière sans changer (pizza, risotto,
        curry), et les jeter appauvrirait la recherche plus que la traduction ne
        l'enrichit. Ils sont signalés à part pour que l'appelant puisse retenter
        sans eux si la recherche ne donne rien.
        """
        db = self._db()
        words = [w for w in re.split(r"\W+", q) if w]
        out, unmapped, i = [], [], 0
        while i < len(words):
            for n in range(maxgram, 0, -1):
                if i + n > len(words):
                    continue
                phrase = " ".join(words[i : i + n])
                if len(phrase) < 2:
                    continue
                # Le lexique ne garde qu'une forme par ingrédient — « pommes »
                # → « apples ». Sans replier aussi le pluriel ANGLAIS, celui
                # qui tape « apple » ne trouverait rien. On essaie donc les
                # variantes du dernier mot, ce qui ne coûte qu'un IN de plus.
                alts = _en_variants(phrase.lower())
                row = db.execute(
                    "SELECT fr FROM lexicon WHERE lang=? AND lower(en) IN (%s) "
                    # La correspondance EXACTE avant une variante de nombre :
                    # « chicken breasts » vaut « blanc de poulet » et non
                    # « filet de poulet », qui n'est là que par sa forme
                    # singulière. Sans ce critère les deux sont à égalité de
                    # longueur et c'est SQLite qui tranche, donc personne.
                    "ORDER BY (lower(en) <> ?), length(fr) LIMIT 1"
                    % ",".join("?" * len(alts)),
                    [lang] + alts + [phrase.lower()],
                ).fetchone()
                if row:
                    out.append(row[0])
                    i += n
                    break
            else:
                out.append(words[i])
                unmapped.append(words[i])
                i += 1
        return " ".join(out), unmapped


    # -- caches -----------------------------------------------------------
    def recipe_get(self, rid, lang="en"):
        row = self._db().execute(
            "SELECT json FROM recipe_tr WHERE id=? AND lang=?", (rid, lang)
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def recipe_put(self, rid, rec, engine, lang="en"):
        db = self._db()
        db.execute(
            "INSERT OR REPLACE INTO recipe_tr(id, lang, json, engine, ts) VALUES(?,?,?,?,?)",
            (rid, lang, json.dumps(rec, ensure_ascii=False), engine, int(time.time())),
        )
        db.commit()

    def titles_get(self, ids, lang="en"):
        if not ids:
            return {}
        qs = ",".join("?" * len(ids))
        return {
            i: t
            for i, t in self._db().execute(
                "SELECT id, title FROM title_tr WHERE lang=? AND id IN (%s)" % qs,
                [lang] + list(ids),
            )
        }

    def titles_put(self, rows, lang="en"):
        now = int(time.time())
        db = self._db()
        db.executemany(
            "INSERT OR REPLACE INTO title_tr(id, lang, title, ts) VALUES(?,?,?,?)",
            [(i, lang, t, now) for i, t in rows],
        )
        db.commit()

    def stale(self, lang="en"):
        """Entrées dont la clé ne correspond plus à norm().

        Une règle de normalisation qui change périme les clés déjà écrites :
        elles ne seront plus jamais retrouvées, sans que rien ne le signale.
        Plutôt qu'un numéro de version à tenir à jour, on recalcule — c'est
        exact par construction, et la table est petite.
        """
        return [
            (fr, en, src)
            for fr, en, src in self._db().execute(
                "SELECT fr, en, source FROM lexicon WHERE lang=?", (lang,)
            )
            if fr != norm(fr)
        ]

    def renormalize(self, lang="en"):
        """Réécrit les clés périmées avec la règle courante.

        En cas de collision — deux anciennes clés qui n'en font plus qu'une —
        une relecture humaine l'emporte sur une sortie de machine : c'est la
        seule des deux qu'on ne saurait pas refabriquer.
        """
        rows = self.stale(lang)
        if not rows:
            return 0
        db = self._db()
        best = {}
        for fr, en, src in rows:
            k = norm(fr)
            if not k:
                continue
            if k not in best or (src == "manual" and best[k][1] != "manual"):
                best[k] = (en, src)
        now = int(time.time())
        db.executemany(
            "DELETE FROM lexicon WHERE lang=? AND fr=?",
            [(lang, fr) for fr, _, _ in rows],
        )
        db.executemany(
            "INSERT OR REPLACE INTO lexicon(fr, lang, en, source, ts) VALUES(?,?,?,?,?)",
            [(k, lang, en, src, now) for k, (en, src) in best.items()],
        )
        db.commit()
        return len(rows)

    def stats(self, lang="en"):
        db = self._db()
        by_source = dict(
            db.execute(
                "SELECT source, COUNT(*) FROM lexicon WHERE lang=? GROUP BY source",
                (lang,),
            )
        )
        return {
            "lexicon": sum(by_source.values()),
            "by_source": by_source,
            "recipes": db.execute(
                "SELECT COUNT(*) FROM recipe_tr WHERE lang=?", (lang,)
            ).fetchone()[0],
            "titles": db.execute(
                "SELECT COUNT(*) FROM title_tr WHERE lang=?", (lang,)
            ).fetchone()[0],
        }


def search_queries(store, q, lang="en"):
    """Les requêtes à essayer, dans l'ordre, pour une saisie anglaise.

    De la plus précise à la plus large, et chacune ne coûte que si la précédente
    n'a rien rendu :

      1. la saisie ramenée au français par groupes de mots — le cas normal ;
      2. la saisie brute — « ratatouille », « pizza » et tous les noms de plats
         qui n'ont jamais eu besoin d'être traduits ;
      3. la même, sans les mots restés anglais. FTS5 exige tous les termes :
         un « tikka » resté tel quel dans « chicken tikka » suffirait à tout
         faire échouer, alors que « poulet » seul rend ce qu'il faut ;
      4. la traduction mot à mot, sans les mots restés anglais. Celle-ci est
         volontairement plus large que la première : /search cherche dans les
         TITRES, et la correspondance par groupes rend la requête plus précise
         que la saisie ne l'était. « chicken breast » donne « blanc de poulet »,
         qui ne trouve plus « Poulet au curry » — alors que « chicken » seul le
         trouvait. Ce dernier essai rattrape exactement ce cas.
    """
    fr, unmapped = store.to_french(q, lang)
    tries = [fr]
    if q != fr:
        tries.append(q)

    def without(pair):
        """La traduction, moins les mots qui n'ont pas su l'être."""
        text, skip = pair
        left = " ".join(w for w in text.split() if w not in skip)
        return left if left and left != text else None

    for cand in (
        without((fr, unmapped)),
        without(store.to_french(q, lang, maxgram=1)),
    ):
        if cand:
            tries.append(cand)

    seen, out = set(), []
    for t in tries:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# --------------------------------------------------------------------------
# Traduire une fiche
# --------------------------------------------------------------------------
ING_CONTEXT = (
    "Liste d'ingrédients d'une recette de cuisine française. "
    "Traduire chaque ligne comme un produit alimentaire."
)
STEP_CONTEXT = "Étapes de préparation d'une recette de cuisine."


def translate_units(rec):
    """Les unités, par la table. Toujours, et sans jamais toucher au réseau."""
    for ing in rec.get("ingredients") or []:
        u = ing.get("unit")
        if u and u in UNITS_EN:
            ing["unit"] = UNITS_EN[u]


def translate_ingredients(rec, store, tr, lang="en"):
    """Noms d'ingrédients : lexique d'abord, moteur pour les seuls inconnus.

    C'est ici que se joue la cohérence de la liste de courses. Ce qui sort du
    moteur est immédiatement écrit au lexique, si bien qu'un terme n'est traduit
    qu'une fois dans la vie du catalogue : la fiche suivante qui le contient
    reprendra exactement la même chaîne.
    """
    ings = rec.get("ingredients") or []
    names = [(i.get("name") or "").strip() for i in ings]
    known = store.lookup(names, lang)

    # Dédoublonné par clé normalisée, pas par graphie : une fiche portant à la
    # fois « Sel » et « sel » ne doit payer qu'une traduction.
    seen, missing = set(), []
    for n in sorted(set(names)):
        k = norm(n)
        if not n or n in known or k in seen:
            continue
        seen.add(k)
        missing.append(n)
    engine = None
    if missing:
        got, engine = tr.translate(missing, context=ING_CONTEXT)
        store.put(zip(missing, got), engine, lang)
        # Redistribué par clé : seule la graphie représentante est partie au
        # moteur, mais la traduction vaut pour toutes ses variantes de la fiche.
        fresh = {norm(a): b for a, b in zip(missing, got)}
        for n in names:
            if n not in known and norm(n) in fresh:
                known[n] = fresh[norm(n)]

    for ing, n in zip(ings, names):
        if n in known:
            ing["name"] = known[n]
    return engine


def translate_recipe(rec, store, tr, lang="en"):
    """Rend la fiche traduite. Lève TranslationUnavailable si rien ne répond."""
    rec = json.loads(json.dumps(rec))  # copie : le cache du catalogue est partagé

    translate_units(rec)
    engine = translate_ingredients(rec, store, tr, lang)

    # Titre et étapes en un seul appel : c'est de la prose, elle n'a pas besoin
    # du lexique, et un aller-retour vaut mieux que deux.
    steps = list(rec.get("steps") or [])
    blob = [rec.get("name") or ""] + steps
    out, eng2 = tr.translate(blob, context=STEP_CONTEXT)
    rec["name"] = out[0]
    if steps:
        rec["steps"] = out[1:]

    rec["lang"] = lang
    return rec, (eng2 or engine or "cache")


def get_translated(rid, rec, store, tr, lang="en"):
    """Fiche traduite, du cache si possible. Ne lève jamais.

    Rend (fiche, langue réellement servie). Si la traduction échoue — quota
    épuisé, LibreTranslate arrêté, réseau coupé — on rend le français. Une
    recette lisible dans la mauvaise langue reste utilisable ; un écran d'erreur
    ne l'est pas, et l'utilisateur n'a rien fait de mal.
    """
    hit = store.recipe_get(rid, lang)
    if hit:
        return hit, lang

    with store.lock_for(("recipe", rid, lang)):
        # Relecture sous verrou : une requête concurrente vient peut-être de
        # faire le travail pendant qu'on attendait.
        hit = store.recipe_get(rid, lang)
        if hit:
            return hit, lang
        try:
            out, engine = translate_recipe(rec, store, tr, lang)
        except TranslationUnavailable:
            return rec, "fr"
        except Exception as e:
            sys.stderr.write("Traduction de la recette %s échouée : %s\n" % (rid, e))
            return rec, "fr"
        store.recipe_put(rid, out, engine, lang)
        if out.get("name"):
            store.titles_put([(rid, out["name"])], lang)
        return out, lang


def translate_titles(rows, store, tr, lang="en"):
    """rows : [(id, titre)]. Rend {id: titre traduit} — cache compris.

    Appelé par /search. Les titres déjà vus ne repartent pas sur le réseau, et
    comme ce sont les mêmes recettes qui remontent pour les mêmes recherches, le
    cache se remplit vite et le coût s'effondre.
    """
    ids = [r[0] for r in rows]
    known = store.titles_get(ids, lang)
    missing = [(i, t) for i, t in rows if i not in known and t]
    if missing:
        try:
            got, _ = tr.translate([t for _, t in missing], context=STEP_CONTEXT)
        except TranslationUnavailable:
            return known
        except Exception as e:
            sys.stderr.write("Traduction des titres échouée : %s\n" % e)
            return known
        pairs = list(zip([i for i, _ in missing], got))
        store.titles_put(pairs, lang)
        known.update(dict(pairs))
    return known


# --------------------------------------------------------------------------
# Outil en ligne de commande
# --------------------------------------------------------------------------
def store_path(db):
    return db + ".tr"


def scan_ingredients(db_path, verbose=True):
    """Compte les noms d'ingrédients du catalogue, du plus fréquent au moins.

    C'est le chiffre qui décide de tout le reste : il dit combien de termes il
    faut vraiment traduire pour couvrir l'essentiel des recettes.
    """
    db = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=30)
    counts = collections.Counter()
    n = 0
    for (blob,) in db.execute(
        "SELECT recipe FROM urls WHERE status='ok' AND recipe IS NOT NULL"
    ):
        try:
            rec = json.loads(blob) or {}
        except ValueError:
            continue
        n += 1
        for ing in rec.get("ingredients") or []:
            name = (ing.get("name") or "").strip()
            if name and len(name) <= LEXICON_MAX:
                counts[name] += 1
        if verbose and n % 20000 == 0:
            print("  %d fiches lues…" % n, flush=True)
    db.close()
    return counts, n


def cmd_extract(args, store):
    counts, n = scan_ingredients(args.db)
    total = sum(counts.values())
    known = store.lookup(list(counts), args.lang)
    covered = sum(c for name, c in counts.items() if name in known)

    print("\n%d fiches, %d occurrences d'ingrédients, %d noms distincts."
          % (n, total, len(counts)))
    if total:
        print("Couverture du lexique actuel : %.1f %% des occurrences "
              "(%d noms sur %d).\n" % (100.0 * covered / total, len(known), len(counts)))

    # La courbe cumulée, parce que c'est elle qui répond à « combien en
    # traduire » : sur ce genre de distribution, les premiers pour cent de
    # termes portent la grande majorité des occurrences.
    run = 0
    marks = [200, 500, 1000, 2000, 5000]
    print("Couverture atteignable en traduisant les N plus fréquents :")
    for i, (_, c) in enumerate(counts.most_common(), 1):
        run += c
        if i in marks:
            print("  %5d termes → %.1f %%" % (i, 100.0 * run / total))
    print()

    missing = [(name, c) for name, c in counts.most_common() if name not in known]
    print("Manquants, les %d plus fréquents :" % min(args.top, len(missing)))
    for name, c in missing[: args.top]:
        print("  %6d  %s" % (c, name))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            for name, c in missing:
                f.write("%d\t%s\n" % (c, name))
        print("\n%d manquants écrits dans %s" % (len(missing), args.out))


def cmd_fill(args, store, tr):
    if not tr.available():
        raise SystemExit(
            "Aucun moteur configuré. Pose PANIER_DEEPL_KEY (ou %s.deepl) "
            "et/ou PANIER_LIBRETRANSLATE_URL." % args.db
        )
    counts, _ = scan_ingredients(args.db)
    known = store.lookup(list(counts), args.lang)
    # Dédoublonné par clé normalisée : « Œuf(s) », « Oeufs » et « oeufs » sont
    # une seule entrée de lexique, les envoyer trois fois serait payer trois
    # fois la même traduction. On garde la graphie la plus fréquente comme
    # représentante — c'est celle qui a le plus de chances d'être bien écrite,
    # et le moteur traduit mieux « Œuf » que « oeuf(s) ».
    seen, missing = set(), []
    for n, _ in counts.most_common():
        k = norm(n)
        if n in known or k in seen:
            continue
        seen.add(k)
        missing.append(n)
    missing = missing[: args.fill]
    if not missing:
        print("Rien à remplir : tout le catalogue est déjà couvert.")
        return

    chars = sum(len(m) for m in missing)
    print("%d termes à traduire (%d caractères) via %s."
          % (len(missing), chars, tr.describe()))
    if not args.yes:
        rep = input("Continuer ? [o/N] ").strip().lower()
        if rep not in ("o", "oui", "y", "yes"):
            return

    done = 0
    for i in range(0, len(missing), BATCH):
        chunk = missing[i : i + BATCH]
        try:
            got, engine = tr.translate(chunk, context=ING_CONTEXT)
        except TranslationUnavailable as e:
            print("\nArrêt : plus aucun moteur disponible (%s)." % e)
            break
        done += store.put(zip(chunk, got), engine, args.lang)
        print("  %d/%d…" % (min(i + BATCH, len(missing)), len(missing)), flush=True)
    print("\n%d entrées ajoutées au lexique." % done)
    print("Relis-les avant de t'y fier :\n"
          "  python3 %s --db %s --export lexique.tsv"
          % (os.path.basename(sys.argv[0]), args.db))


def cmd_export(args, store):
    db = store._db()
    rows = db.execute(
        "SELECT fr, en, source FROM lexicon WHERE lang=? ORDER BY source, fr",
        (args.lang,),
    ).fetchall()
    with open(args.export, "w", encoding="utf-8") as f:
        f.write("# fr\ten\tsource — corrige la 2e colonne, puis --import\n")
        for fr, en, src in rows:
            f.write("%s\t%s\t%s\n" % (fr, en, src))
    print("%d entrées écrites dans %s" % (len(rows), args.export))


def cmd_import(args, store):
    rows = []
    with open(args.imp, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
                rows.append((parts[0], parts[1]))
    # « manual » et non la source d'origine : une entrée passée par une relecture
    # humaine ne doit plus jamais être considérée comme réécrivable.
    n = store.overwrite(rows, "manual", args.lang)
    print("%d entrées reprises depuis %s (marquées « manual »)." % (n, args.imp))


def main():
    p = argparse.ArgumentParser(
        description="Lexique et traduction du catalogue Panier.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("EN LIGNE DE COMMANDE")[-1],
    )
    p.add_argument(
        "--db",
        default=os.path.expanduser("~/panier-scrape/panier-scrape.sqlite"),
        help="catalogue produit par scrape-recipes.py",
    )
    p.add_argument("--lang", default="en")
    p.add_argument("--target", default=TARGET_DEFAULT,
                   help="langue cible DeepL (défaut EN-GB ; EN-US pour l'américain)")
    p.add_argument("--seed", action="store_true",
                   help="installe le lexique de base (unités + ~300 ingrédients)")
    p.add_argument("--extract", action="store_true",
                   help="mesure la couverture et liste les manquants par fréquence")
    p.add_argument("--top", type=int, default=60,
                   help="nombre de manquants affichés par --extract")
    p.add_argument("--out", help="écrit tous les manquants dans ce fichier TSV")
    p.add_argument("--fill", type=int, metavar="N",
                   help="traduit les N termes manquants les plus fréquents")
    p.add_argument("--yes", action="store_true", help="ne pas demander confirmation")
    p.add_argument("--export", metavar="TSV", help="sort le lexique pour relecture")
    p.add_argument("--import", dest="imp", metavar="TSV",
                   help="reprend un TSV relu (écrase, marque « manual »)")
    p.add_argument("--renormalize", action="store_true",
                   help="réécrit les clés du lexique périmées par un changement "
                        "de règle de normalisation")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--usage", action="store_true", help="quota DeepL consommé")
    p.add_argument("--test", metavar="TEXTE", help="essai des moteurs")
    args = p.parse_args()

    if not os.path.exists(args.db) and not (args.test or args.usage):
        raise SystemExit("Base introuvable : %s" % args.db)

    store = TrStore(store_path(args.db))
    tr = Translator.from_env(args.db, args.target)

    if args.renormalize:
        n = store.renormalize(args.lang)
        print("%d clés réécrites." % n if n else "Aucune clé périmée.")
    else:
        # Silencieux tant que tout va bien, mais impossible à manquer sinon :
        # des clés périmées ne se voient pas, elles font juste retomber la
        # couverture sans rien dire.
        n = len(store.stale(args.lang))
        if n:
            print("⚠ %d entrées du lexique ont une clé périmée : elles ne seront "
                  "jamais retrouvées.\n  python3 %s --db %s --renormalize\n"
                  % (n, os.path.basename(sys.argv[0]), args.db))

    if args.seed:
        n = store.put(SEED.items(), "seed", args.lang)
        u = store.put(
            ((k, v) for k, v in UNITS_EN.items() if norm(k) != norm(v)),
            "seed",
            args.lang,
        )
        print("Lexique de base : %d ingrédients + %d unités ajoutés." % (n, u))
        print("(les entrées déjà présentes n'ont pas été touchées)")
    if args.test:
        print("Moteurs : %s" % tr.describe())
        out, engine = tr.translate([args.test], context=ING_CONTEXT)
        print("%s → %s   [%s]" % (args.test, out[0], engine))
    if args.usage:
        u = tr.usage()
        if not u:
            print("Pas de clé DeepL configurée.")
        else:
            lim = u.get("character_limit") or 0
            used = u.get("character_count") or 0
            print("DeepL : %d / %d caractères%s"
                  % (used, lim, " (%.1f %%)" % (100.0 * used / lim) if lim else ""))
    if args.extract:
        cmd_extract(args, store)
    if args.fill:
        cmd_fill(args, store, tr)
    if args.export:
        cmd_export(args, store)
    if args.imp:
        cmd_import(args, store)
    if args.stats:
        st = store.stats(args.lang)
        print("Lexique   : %d entrées (%s)"
              % (st["lexicon"],
                 ", ".join("%s %d" % kv for kv in sorted(st["by_source"].items()))
                 or "vide"))
        print("Recettes  : %d en cache" % st["recipes"])
        print("Titres    : %d en cache" % st["titles"])
        print("Moteurs   : %s" % tr.describe())

    if not any([args.seed, args.extract, args.fill, args.export, args.imp,
                args.stats, args.usage, args.test, args.renormalize]):
        p.print_help()


if __name__ == "__main__":
    main()
