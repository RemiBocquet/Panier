"""
Panier — moissonneur de recettes Marmiton, Jow, CuisineAZ et 750g.

Produit deux choses à partir des mêmes fiches :

  • une base SQLite (--state), que tools/recipe-server.py sert à l'application
    pour la recherche par nom et l'import au clic ;
  • des fichiers JSON (--out) au format « recettes partagées » de Panier, que
    Réglages → « Importer des recettes partagées » sait relire :

    {"_kind":"panier-recipes","_v":1,"_date":"...","recipes":[{name,image,servings,
      cook,prep,source,sourceType,ingredients:[{qty,unit,name}],steps:[...]}, ...]}

C'est volontairement CE format-là et pas la sauvegarde complète : l'import de recettes
partagées AJOUTE sans rien remplacer, et dédoublonne par nom. Une sauvegarde, elle,
écraserait la bibliothèque existante.


CE QUE LE SCRIPT S'INTERDIT
---------------------------
robots.txt est lu à chaque démarrage, sur chaque domaine, et TOUTE URL est soumise à
can_fetch() avant d'être demandée. Rien n'est codé en dur : si un site durcit son
fichier demain, le script se restreint tout seul (et le dit dans son journal).

État constaté au 26/08/2026, à titre indicatif seulement :

  marmiton.org   Fiches /recettes/recette_*.aspx autorisées, sitemap publié dans
                 robots.txt. 43 828 fiches.
  jow.fr         Fiches /recipes/* autorisées, 3 590 fiches au sitemap. En revanche
                 « Disallow: /api/* » : l'API interne de Jow est HORS LIMITES ici.
                 Tout est lu dans le JSON-LD de la page publique, qui contient la
                 recette complète. (L'application, elle, interroge api.jow.fr — un
                 autre domaine, sans robots.txt — pour UNE recette demandée par
                 l'utilisateur. Un moissonnage massif est un usage tout autre.)
  cuisineaz.com  Sitemap publié dans robots.txt. 93 946 fiches, toutes autorisées :
                 les Disallow /recettes/recette-0*…9* visent une ancienne forme
                 d'URL qui n'apparaît plus au sitemap.
  750g.com       AUCUN sitemap publié. D'où le mode « crawl » : on parcourt l'arbre
                 des rubriques et leur pagination. La couverture vaut donc ce que
                 cet arbre expose — environ 80 000 fiches revendiquées par le site.
                 /recherche/ et /recipe/ sont interdits : on n'y touche pas.

Aucun des quatre ne déclare de Crawl-delay. Le script s'en impose donc un lui-même
(--delay, 2 s par défaut), une seule requête à la fois PAR DOMAINE.

À savoir tout de même : robots.txt règle la question du robot, pas celle des CGU.
Ces sites interdisent contractuellement la reproduction massive de leur catalogue.
Pour une bibliothèque personnelle qui reste sur ton Pi et chez tes amis, c'est un
usage privé ordinaire ; ça ne le resterait pas si le résultat était republié.


PARALLÈLE ET DURÉES
-------------------
--parallel ouvre un fil par site. La cadence restant comptée par DOMAINE, chaque
site reçoit exactement la même charge qu'en séquentiel : seul le temps total est
divisé. À --delay 1.5 (soit ~1,6 s en moyenne, gigue comprise) :

    cuisineaz   93 946 fiches           ~42 h
    750g        ~85 000 fiches + pages  ~38 h
    marmiton    43 828 fiches           ~20 h
    jow          3 590 fiches            ~2 h

Les deux nouveaux sites tiennent donc dans deux jours s'ils tournent ensemble.


USAGE
-----
    # Les deux nouveaux sites, en parallèle, sur deux jours
    python3 tools/scrape-recipes.py --site cuisineaz --site 750g \n            --parallel --delay 1.5 --chunk 2000

    python3 tools/scrape-recipes.py --stats-only          # où ça en est
    python3 tools/scrape-recipes.py --site 750g --limit 20   # essai

Le travail reprend là où il s'est arrêté (état dans le SQLite) : on peut couper,
rebooter le Pi, relancer. Le parcours des rubriques de 750g reprend lui aussi, grâce
à la table `pages`. Sur Raspberry Pi, pour laisser tourner :

    tmux new -s panier
    python3 tools/scrape-recipes.py --site cuisineaz --site 750g --parallel --delay 1.5
    # Ctrl+B puis D pour détacher

Puis, la moisson finie, pour que l'application voie les nouvelles recettes :

    python3 tools/recipe-server.py --db panier-scrape.sqlite --build-index

Python 3.7+, bibliothèque standard uniquement — rien à installer sur le Pi.
"""

import argparse
import gzip
import io
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import urljoin, urlparse

# Le journal contient des accents et quelques symboles. Une console qui n'est pas en
# UTF-8 (Windows par défaut) ferait planter le script sur un simple print au bout de
# six heures de moisson : on remplace le caractère plutôt que d'abandonner le travail.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

APP_KIND = "panier-recipes"
DEFAULT_UA = (
    "PanierBot/1.0 (+https://panier.remibocquet.fr ; "
    "moissonneur personnel de recettes)"
)

# Les quatre sources. « pattern » dit ce qu'est une fiche recette ; tout le reste
# (dossiers, articles, pages de rubrique) est ignoré.
#
# Deux modes d'inventaire, selon ce que le site publie :
#
#   discovery="sitemap"  Le site publie un sitemap : on le lit, on a la liste
#                        exhaustive, une poignée de requêtes suffit.
#   discovery="crawl"    Aucun sitemap (cas de 750g) : il faut parcourir l'arbre
#                        des rubriques et leur pagination pour trouver les fiches.
#                        Plus lent, et la couverture vaut ce que l'arbre expose.
SITES = {
    "marmiton": {
        "discovery": "sitemap",
        "sitemap": "https://www.marmiton.org/wsitemap_recipes_index.xml",
        "pattern": re.compile(
            r"^https://www\.marmiton\.org/recettes/recette_[^/]+\.aspx$",
            re.IGNORECASE,
        ),
        "source_type": "marmiton",
    },
    "jow": {
        "discovery": "sitemap",
        "sitemap": "https://jow.fr/sitemap-index.xml",
        "pattern": re.compile(
            r"^https://jow\.fr/recipes/[^/]+$", re.IGNORECASE
        ),
        "source_type": "jow",
    },
    "cuisineaz": {
        "discovery": "sitemap",
        "sitemap": "https://www.cuisineaz.com/xml/sitemap.xml",
        "pattern": re.compile(
            r"^https://www\.cuisineaz\.com/recettes/[^/]+-\d+\.aspx$",
            re.IGNORECASE,
        ),
        "source_type": "cuisineaz",
    },
    "750g": {
        "discovery": "crawl",
        # La page « toutes les rubriques » : 14 familles, qui se ramifient ensuite.
        "seeds": ["https://www.750g.com/home_rubrique_-_recettes.htm"],
        # Une rubrique, avec ou sans pagination. Les liens sont relatifs dans la
        # page, on les résout avant de les confronter à ce motif.
        "category": re.compile(
            r"^https://www\.750g\.com/recettes-[a-z0-9-]+(?:/[a-z0-9-]+)*/"
            r"(?:\?page=\d+)?$",
            re.IGNORECASE,
        ),
        "pattern": re.compile(
            r"^https://www\.750g\.com/[a-z0-9-]+-r\d+\.htm$", re.IGNORECASE
        ),
        "source_type": "750g",
    },
}


# --------------------------------------------------------------------------
# Bornes du format — les mêmes que sanitizeRecipe() dans index.html.
#
# L'import de recettes partagées ne repasse PAS par sanitizeRecipe : il fait
# confiance au fichier. C'est donc ici qu'on garantit qu'aucune fiche aberrante
# (une étape de 40 000 caractères, 3 000 ingrédients) n'entre dans l'application.
# --------------------------------------------------------------------------
TXT_MAX, STEP_MAX, ING_MAX, URL_MAX = 200, 1000, 120, 2000
MAX_INGREDIENTS, MAX_STEPS = 200, 60

# Caractères de contrôle : retirés du texte importé, et tolérés dans un JSON-LD
# mal formé (certaines pages en laissent traîner au milieu d'une chaîne).
CTRL_RE = re.compile("[\u0000-\u001f\u007f\u2028\u2029]+")


def clean_text(v, maximum=TXT_MAX):
    if v is None:
        return ""
    s = CTRL_RE.sub(" ", str(v))
    return re.sub(r"\s+", " ", s).strip()[:maximum]


def clean_int(v, lo, hi):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = v
    else:
        m = re.match(r"^-?\d+", clean_text(v, 40))
        if not m:
            return None
        n = int(m.group(0))
    try:
        r = int(round(n))
    except (TypeError, ValueError, OverflowError):
        return None
    return None if (r < lo or r > hi) else r


def safe_url(v):
    t = clean_text(v, URL_MAX)
    if not t:
        return ""
    try:
        u = urlparse(t)
    except ValueError:
        return ""
    return t if u.scheme in ("http", "https") and u.netloc else ""


# --------------------------------------------------------------------------
# Analyse d'un ingrédient — port fidèle de parseIngredient() dans index.html.
#
# Pourquoi le porter plutôt que laisser l'application le faire : l'import de
# recettes partagées attend des champs {qty, unit, name} DÉJÀ séparés, il ne
# relit pas de texte libre. Sans cette étape, « 200 g de farine » entrerait
# comme un nom d'ingrédient entier, sans quantité — donc inutilisable pour les
# courses et le stock, qui sont tout l'intérêt de l'application.
#
# La table d'unités est recopiée d'index.html : si elle y change, la changer ici.
# --------------------------------------------------------------------------
UNITS = [
    ("kg", "kg"),
    ("kilogramme", "kg"),
    ("kilogrammes", "kg"),
    ("kilo", "kg"),
    ("kilos", "kg"),
    ("g", "g"),
    ("gr", "g"),
    ("gramme", "g"),
    ("grammes", "g"),
    ("mg", "mg"),
    ("l", "l"),
    ("litre", "l"),
    ("litres", "l"),
    ("dl", "dl"),
    ("cl", "cl"),
    ("ml", "ml"),
    ("millilitre", "ml"),
    ("millilitres", "ml"),
    ("càs", "c. à soupe"),
    ("cas", "c. à soupe"),
    ("cuillère à soupe", "c. à soupe"),
    ("cuillères à soupe", "c. à soupe"),
    ("cuillere à soupe", "c. à soupe"),
    ("cuilleres à soupe", "c. à soupe"),
    ("c. à soupe", "c. à soupe"),
    ("c à soupe", "c. à soupe"),
    # 750g abrège en « 2 c. à s. de sucre glace » : sans ces alias, le « c. à s. »
    # resterait collé au nom de l'ingrédient.
    ("c. à s.", "c. à soupe"),
    ("c.à.s", "c. à soupe"),
    ("c. à c.", "c. à café"),
    ("c.à.c", "c. à café"),
    ("cs", "c. à soupe"),
    ("càc", "c. à café"),
    ("cac", "c. à café"),
    ("cuillère à café", "c. à café"),
    ("cuillères à café", "c. à café"),
    ("cuillere à café", "c. à café"),
    ("cuilleres à café", "c. à café"),
    ("c. à café", "c. à café"),
    ("c à café", "c. à café"),
    ("cc", "c. à café"),
    ("pincée", "pincée"),
    ("pincées", "pincée"),
    ("gousse", "gousse"),
    ("gousses", "gousse"),
    ("tranche", "tranche"),
    ("tranches", "tranche"),
    ("sachet", "sachet"),
    ("sachets", "sachet"),
    ("boîte", "boîte"),
    ("boîtes", "boîte"),
    ("boite", "boîte"),
    ("boites", "boîte"),
    ("botte", "botte"),
    ("bottes", "botte"),
    ("brin", "brin"),
    ("brins", "brin"),
    ("branche", "brin"),
    ("branches", "brin"),
    ("feuille", "feuille"),
    ("feuilles", "feuille"),
    ("verre", "verre"),
    ("verres", "verre"),
    ("tasse", "tasse"),
    ("tasses", "tasse"),
    ("pot", "pot"),
    ("pots", "pot"),
    ("bouquet", "bouquet"),
    ("bouquets", "bouquet"),
    ("morceau", "morceau"),
    ("morceaux", "morceau"),
    ("boule", "boule"),
    ("boules", "boule"),
    ("cup", "tasse"),
    ("cups", "tasse"),
    ("tbsp", "c. à soupe"),
    ("tablespoon", "c. à soupe"),
    ("tablespoons", "c. à soupe"),
    ("tsp", "c. à café"),
    ("teaspoon", "c. à café"),
    ("teaspoons", "c. à café"),
    ("slice", "tranche"),
    ("slices", "tranche"),
    ("clove", "gousse"),
    ("cloves", "gousse"),
    ("pinch", "pincée"),
    ("pinches", "pincée"),
    ("oz", "oz"),
    ("lb", "lb"),
    ("lbs", "lb"),
    ("pound", "lb"),
    ("pounds", "lb"),
    ("ounce", "oz"),
    ("ounces", "oz"),
    ("can", "boîte"),
    ("cans", "boîte"),
    ("tin", "boîte"),
    ("tins", "boîte"),
    ("jar", "bocal"),
    ("jars", "bocal"),
    ("bunch", "bouquet"),
    ("bunches", "bouquet"),
    ("sprig", "brin"),
    ("sprigs", "brin"),
    ("handful", "poignée"),
    ("handfuls", "poignée"),
    ("packet", "paquet"),
    ("packets", "paquet"),
    ("pack", "paquet"),
    ("packs", "paquet"),
    ("bag", "sachet"),
    ("bags", "sachet"),
    ("stick", "bâton"),
    ("sticks", "bâton"),
    ("leaf", "feuille"),
    ("leaves", "feuille"),
    ("glass", "verre"),
    ("glasses", "verre"),
    ("drop", "goutte"),
    ("drops", "goutte"),
    ("knob", "morceau"),
    ("piece", "morceau"),
    ("pieces", "morceau"),
    # Unités que CuisineAZ met dans son champ dédié et qui, faute d'être
    # reconnues ici, restaient collées au nom : « cube(s) Bouillon de volaille »,
    # « rouleau Pâte(s) feuilletée(s) », « zeste Citron(s) », « noix Beurre »,
    # « gou. Ail ». Chacune vaut des centaines de fiches.
    ("cube", "cube"),
    ("cubes", "cube"),
    ("rouleau", "rouleau"),
    ("rouleaux", "rouleau"),
    ("zeste", "zeste"),
    ("zestes", "zeste"),
    ("noix", "noix"),
    ("gou.", "gousse"),
]

# Ces suites commencent par un mot d'unité mais n'en sont pas : les découper
# détruirait l'ingrédient. « 1 bouquet garni » n'est pas un bouquet de garni, et
# « noix de coco » n'est pas une noix de coco au sens d'une quantité de coco.
#
# La liste est courte et le restera : elle ne recense que les cas où le mot qui
# suit ne tient pas debout tout seul. « bouquet de persil » n'y est pas — le
# persil, lui, est un ingrédient, et « 1 bouquet · persil » est exactement ce
# qu'on veut dans les courses.
PROTECTED_COMPOUNDS = (
    "bouquet garni",
    "noix de coco",
    "noix de muscade",
    "noix de cajou",
    "noix de pecan",
    "noix de saint-jacques",
    "noix de saint jacques",
    "noix de veau",
    "noix de jambon",
    "feuille de brick",
    "feuilles de brick",
)

# Le plus long d'abord : sans quoi « c » de « cl » gagnerait contre « cuillère à café ».
UNIT_MAP = sorted(UNITS, key=lambda kv: -len(kv[0]))

WORD_NUM = {
    "un": 1,
    "une": 1,
    "deux": 2,
    "trois": 3,
    "quatre": 4,
    "cinq": 5,
    "six": 6,
    "sept": 7,
    "huit": 8,
    "neuf": 9,
    "dix": 10,
    "demi": 0.5,
    "demie": 0.5,
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "half": 0.5,
    "dozen": 12,
}
FRAC = {
    "½": 0.5,
    "⅓": 1 / 3,
    "⅔": 2 / 3,
    "¼": 0.25,
    "¾": 0.75,
    "⅕": 0.2,
    "⅛": 0.125,
}

# Un nombre écrit en toutes lettres exige une frontière derrière lui : sans quoi le
# « a » de « a pinch of salt » avalerait aussi le « a » d'« abricot ».
QTY_RE = re.compile(
    r"^((?:\d+\s+)?\d+\s*/\s*\d+"
    r"|[½⅓⅔¼¾⅕⅛]"
    r"|\d+[.,]?\d*"
    r"|(?:un(?:e)?|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|demie?|an?|one|two|three"
    r"|four|five|seven|eight|nine|ten|half|dozen)(?![a-zà-ÿ]))\s*",
    re.IGNORECASE,
)
ARTICLE_RE = re.compile(
    r"^(de\s+la\s+|de\s+l['’]\s*|d['’]\s*|de\s+|du\s+|des\s+|of\s+the\s+|of\s+)",
    re.IGNORECASE,
)


def strip_accents(s):
    return "".join(
        c
        for c in unicodedata.normalize("NFD", str(s))
        if unicodedata.category(c) != "Mn"
    )


# Un nom d'ingrédient qui n'est QUE le mot d'une unité n'est pas un ingrédient :
# c'est un champ resté vide côté site. « Gramme(s) » apparaît ainsi des centaines
# de fois. Défini ici et non près de UNITS : strip_accents() n'existe qu'au-dessus.
#
# « noix » en est retiré, et c'est indispensable : le mot est aussi un ingrédient
# à part entière. Entre supprimer les cerneaux de noix de toutes les recettes et
# laisser passer quelques champs vides, le choix n'est pas douteux.
UNIT_WORDS = {strip_accents(k.lower()).rstrip(".") for k, _ in UNITS} - {"noix"}


def bare_unit_word(name):
    """Vrai si le nom se réduit au mot d'une unité — « Gramme(s) », « cube(s) »."""
    flat = strip_accents(str(name or "").lower()).strip()
    flat = re.sub(r"\((?:s|x|es)\)", "", flat).strip().rstrip(".")
    return flat in UNIT_WORDS


def num_tok(tok):
    tok = tok.strip()
    if tok in FRAC:
        return FRAC[tok]
    mm = re.match(r"^(\d+)\s+(\d+)\s*/\s*(\d+)$", tok)
    if mm:
        return int(mm.group(1)) + int(mm.group(2)) / int(mm.group(3))
    if re.match(r"^\d+\s*/\s*\d+$", tok):
        a, b = tok.split("/")
        return int(a.strip()) / int(b.strip())
    t = tok.replace(",", ".")
    if re.match(r"^\d*\.?\d+$", t):
        return float(t)
    return WORD_NUM.get(strip_accents(tok.lower()))


def parse_ingredient(raw):
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s:
        return None
    original, qty, unit = s, None, None

    m = QTY_RE.match(s)
    if m:
        q = num_tok(m.group(1))
        if q is not None:
            qty = q
            s = s[m.end() :].strip()
    # « 1 ½ citron » : la fraction collée derrière un entier s'y ajoute.
    if qty is not None:
        m2 = re.match(r"^([½⅓⅔¼¾⅕⅛])\s*", s)
        if m2:
            qty += FRAC[m2.group(1)]
            s = s[m2.end() :].strip()

    low = s.lower()
    flat = strip_accents(low)
    # Une suite protégée n'est pas découpée : « bouquet garni » commence par un
    # mot d'unité sans en être une, et le découper laissait « garni » seul dans
    # la liste de courses — plus de 2 000 fiches concernées.
    protected = any(flat.startswith(c) for c in PROTECTED_COMPOUNDS)

    if not protected:
        for k, v in UNIT_MAP:
            # Le « (s) » fait partie de l'unité, pas du nom. CuisineAZ écrit « 8
            # tranche(s) Pain de mie » : sans cette tolérance l'ingrédient s'appellerait
            # « tranche(s) Pain de mie » et arriverait sans unité dans les courses.
            m_unit = re.match(re.escape(k) + r"(?:\(s\))?(?=\s|$|\.)", low, re.IGNORECASE)
            if not m_unit:
                continue
            rest = re.sub(r"^\.\s*", "", s[m_unit.end() :]).strip()
            # Une unité qui avale TOUT le texte n'est pas une unité : c'est le nom
            # lui-même. Sans ce garde-fou, « Noix » devenait une quantité sans
            # ingrédient, et « Gramme(s) » un ingrédient nommé « Gramme(s) ».
            if not ARTICLE_RE.sub("", rest).strip():
                break
            unit = v
            s = rest
            break

    s = ARTICLE_RE.sub("", s).strip()
    name = re.sub(r"^[-–,\s]+|[-–,\s]+$", "", re.sub(r"\s+", " ", s))
    if not name:
        name = original
    # Un nom qui n'est que le mot d'une unité vient d'un champ resté vide côté
    # site : ce n'est pas un ingrédient, et il n'a rien à faire dans les courses.
    if bare_unit_word(name):
        return None
    if isinstance(qty, float):
        qty = int(qty) if qty.is_integer() else round(qty, 4)
    return {"qty": qty, "unit": unit, "name": clean_text(name, ING_MAX)}


# CuisineAZ range ses intertitres au milieu des ingrédients : « POUR VIANDES ROUGES »,
# « Préparation », « Garniture ». Sans quantité ni unité, ils passeraient pour des
# ingrédients et atterriraient tels quels dans la liste de courses.
#
# Le filtre reste étroit à dessein : « sel » et « poivre » n'ont eux non plus ni
# quantité ni unité, et doivent survivre. On ne retient donc que trois signaux sûrs —
# commencer par « pour », être un mot de section connu, ou être tout en capitales.
SECTION_WORDS = {
    "preparation", "garniture", "decoration", "dressage", "montage", "finition",
    "ustensiles", "materiel", "ingredients", "sauce", "marinade", "accompagnement",
}


def is_section_heading(ing):
    if ing["qty"] is not None or ing["unit"] is not None:
        return False
    name = ing["name"].strip()
    if not name:
        return True
    if re.match(r"^pour", name, re.IGNORECASE):
        return True
    if strip_accents(name.lower()) in SECTION_WORDS:
        return True
    letters = [c for c in name if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


def norm_name(t):
    """Clé de dédoublonnage — la même que normIng() dans index.html."""
    s = strip_accents(str(t or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"['’`-]", " ", s)).strip()


# --------------------------------------------------------------------------
# JSON-LD → fiche Panier.
#
# Marmiton et Jow publient tous les deux un schema.org/Recipe complet dans leur
# page. Un seul analyseur suffit donc pour les deux, et c'est le même que celui
# de l'application (extractRecipeFromHTML) : ce qui entre par ce script et ce qui
# entre par le bouton « Importer » donnent la même fiche.
# --------------------------------------------------------------------------
LD_RE = re.compile(
    r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def first_str(v):
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return first_str(v[0]) if v else None
    if isinstance(v, dict):
        return v.get("url") or v.get("name") or v.get("@id")
    return None


def iso8601_to_min(d):
    if not isinstance(d, str):
        return None
    m = re.search(r"PT(?:(\d+)H)?(?:(\d+)M)?", d)
    if not m:
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def yield_to_num(v):
    # Marmiton : « 2 personnes ». Jow : ['4', 'portions'].
    s = first_str(v)
    if not s:
        return None
    m = re.search(r"\d+", str(s))
    return int(m.group(0)) if m else None


def instr_to_steps(v):
    out = []

    def walk(x):
        if not x:
            return
        if isinstance(x, str):
            for t in x.splitlines():
                t = t.strip()
                if t:
                    out.append(t)
            return
        if isinstance(x, list):
            for i in x:
                walk(i)
            return
        if isinstance(x, dict):
            if x.get("@type") == "HowToSection" and (
                x.get("itemListElement") or x.get("steps")
            ):
                walk(x.get("itemListElement") or x.get("steps"))
                return
            t = str(x.get("text") or x.get("name") or "").strip()
            if t:
                out.append(t)

    walk(v)
    return [re.sub(r"\s+", " ", s).strip() for s in out if s.strip()]


def find_recipe_node(data):
    """Marmiton range sa recette dans un @graph aux côtés de l'organisation et du
    fil d'Ariane ; Jow la met à la racine. On descend jusqu'à trouver le nœud."""

    def is_recipe(o):
        if not isinstance(o, dict):
            return False
        t = o.get("@type")
        return t == "Recipe" or (isinstance(t, list) and "Recipe" in t)

    stack = [data]
    while stack:
        o = stack.pop(0)
        if is_recipe(o):
            return o
        if isinstance(o, list):
            stack = list(o) + stack
        elif isinstance(o, dict):
            if "@graph" in o:
                stack = [o["@graph"]] + stack
            stack += [
                v
                for k, v in o.items()
                if k != "@graph" and isinstance(v, (dict, list))
            ]
    return None


# Marmiton suffixe ses titres pour le référencement. « Gaufres citron et pavot :
# la meilleure recette » fait une entrée de bibliothèque bien plus lisible sans.
TITLE_NOISE = re.compile(
    r"\s*[:|]\s*(la meilleure recette|recette de cuisine|facile et rapide"
    r"|découvrez les recettes.*|recette.*marmiton)\s*$",
    re.IGNORECASE,
)


def html_to_recipe(html, url, source_type):
    node = None
    for m in LD_RE.finditer(html):
        blob = m.group(1)
        try:
            data = json.loads(blob)
        except ValueError:
            try:
                data = json.loads(CTRL_RE.sub(" ", blob))
            except ValueError:
                continue
        node = find_recipe_node(data)
        if node:
            break
    if not node:
        return None

    name = TITLE_NOISE.sub("", clean_text(first_str(node.get("name")) or ""))
    name = re.sub(r"\s*[|—–-]\s*(Jow|Marmiton)\s*$", "", name).strip()

    raw = node.get("recipeIngredient") or node.get("ingredients") or []
    if isinstance(raw, str):
        raw = [raw]
    ingredients = []
    for x in raw[:MAX_INGREDIENTS]:
        ing = parse_ingredient(x if isinstance(x, str) else first_str(x))
        if ing and ing["name"] and not is_section_heading(ing):
            ingredients.append(ing)

    steps = [
        clean_text(s, STEP_MAX)
        for s in instr_to_steps(node.get("recipeInstructions"))
    ]
    steps = [s for s in steps if s][:MAX_STEPS]

    # Une fiche sans aucun ingrédient n'a pas d'intérêt ici : elle ne peut alimenter
    # ni les courses ni le stock. On la compte « vide » plutôt que de l'exporter.
    if not name or not ingredients:
        return None

    return {
        "name": name,
        "image": safe_url(first_str(node.get("image")) or ""),
        "servings": clean_int(yield_to_num(node.get("recipeYield")), 1, 200),
        "cook": clean_int(iso8601_to_min(node.get("cookTime")), 0, 6000),
        "prep": clean_int(iso8601_to_min(node.get("prepTime")), 0, 6000),
        "source": safe_url(url),
        "sourceType": source_type,
        "ingredients": ingredients,
        "steps": steps,
    }


# --------------------------------------------------------------------------
# Récupération réseau : une requête à la fois, cadencée, identifiée, et jamais
# sans l'accord de robots.txt.
# --------------------------------------------------------------------------
class Fetcher:
    def __init__(self, user_agent, delay, timeout, verbose=True, prefix=""):
        self.ua = user_agent
        # Le jeton d'agent (avant le premier '/') est ce que robots.txt compare.
        self.token = user_agent.split("/")[0] or "PanierBot"
        self.delay = delay
        self.timeout = timeout
        self.verbose = verbose
        # En mode parallèle, préfixer chaque ligne dit de quel site elle vient.
        self.prefix = prefix
        self._last = {}  # host -> horodatage de la dernière requête
        self._robots = {}  # host -> (RobotFileParser, cadence effective)

    def log(self, *a):
        if not self.verbose:
            return
        msg = " ".join(str(x) for x in a)
        # Un message qui se désigne déjà (« [750g] 12/97 … ») n'a pas besoin du
        # préfixe : sans ce test, les journaux parallèles doubleraient l'étiquette.
        print(msg if msg.lstrip().startswith("[") else self.prefix + msg, flush=True)

    def _wait(self, host):
        gap = self._robots.get(host, (None, self.delay))[1]
        last = self._last.get(host)
        if last is not None:
            # Un peu de hasard : deux exécutions ne frappent pas en cadence identique.
            due = last + gap * random.uniform(0.9, 1.25)
            now = time.monotonic()
            if now < due:
                time.sleep(due - now)
        self._last[host] = time.monotonic()

    def _raw(self, url, tries=4):
        """GET brut, sans contrôle robots. Réservé à robots.txt lui-même."""
        host = urlparse(url).hostname
        backoff = 5
        for attempt in range(1, tries + 1):
            self._wait(host)
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.ua,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "fr-FR,fr;q=0.9",
                    "Accept-Encoding": "gzip",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read()
                    # gzip arrive de deux façons : en-tête d'encodage, ou fichier .gz.
                    if (
                        r.headers.get("Content-Encoding") == "gzip"
                        or body[:2] == b"\x1f\x8b"
                    ):
                        body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
                    # L'URL finale sert à repérer les redirections : au-delà de sa
                    # dernière page, 750g répond 301 vers la rubrique, et urllib suit
                    # le renvoi en silence. Sans cette comparaison, la pagination
                    # retomberait sur la page 1 et tournerait en rond sans fin.
                    final = getattr(r, "url", None) or r.geturl()
                    return (
                        getattr(r, "status", r.getcode()),
                        body.decode("utf-8", "replace"),
                        final,
                    )
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < tries:
                    # Retry-After fait foi : c'est le serveur qui dit quand revenir.
                    wait = backoff
                    ra = e.headers.get("Retry-After") if e.headers else None
                    if ra and ra.strip().isdigit():
                        wait = min(int(ra.strip()), 600)
                    self.log("    %s → %d, pause %ds" % (url, e.code, wait))
                    time.sleep(wait)
                    backoff *= 3
                    continue
                if 500 <= e.code < 600 and attempt < tries:
                    time.sleep(backoff)
                    backoff *= 3
                    continue
                return e.code, "", url
            except Exception as e:  # réseau coupé, DNS, TLS, timeout…
                if attempt < tries:
                    self.log(
                        "    %s → %s, nouvelle tentative dans %ds"
                        % (url, e, backoff)
                    )
                    time.sleep(backoff)
                    backoff *= 3
                    continue
                return 0, "", url
        return 0, "", url

    def robots(self, host):
        """Charge et met en cache le robots.txt d'un domaine."""
        if host in self._robots:
            return self._robots[host]
        # Provisoire, pour que _wait() ait une cadence pendant qu'on lit robots.txt.
        self._robots[host] = (None, self.delay)
        rp = urllib.robotparser.RobotFileParser()
        status, body, _ = self._raw("https://%s/robots.txt" % host)
        if status == 200 and body:
            rp.parse(body.splitlines())
            self.log(
                "  robots.txt de %s : lu (%d lignes)"
                % (host, len(body.splitlines()))
            )
        elif status in (401, 403):
            # Un robots.txt protégé vaut interdiction totale (RFC 9309).
            rp.disallow_all = True
            self.log(
                "  robots.txt de %s : %d → tout le domaine est refusé"
                % (host, status)
            )
        elif status == 0:
            # Réseau indisponible : on refuse plutôt que de supposer l'autorisation.
            rp.disallow_all = True
            self.log(
                "  robots.txt de %s : injoignable → domaine refusé pour cette exécution"
                % host
            )
        else:
            # Absent (404) : rien n'est interdit. On garde quand même notre cadence.
            rp.allow_all = True
            self.log(
                "  robots.txt de %s : absent (%d) → aucune restriction déclarée"
                % (host, status)
            )
        declared = None
        try:
            declared = rp.crawl_delay(self.token)
        except Exception:
            pass
        gap = max(self.delay, float(declared)) if declared else self.delay
        if declared:
            self.log(
                "  Crawl-delay déclaré par %s : %ss (cadence retenue : %.1fs)"
                % (host, declared, gap)
            )
        self._robots[host] = (rp, gap)
        return self._robots[host]

    def allowed(self, url):
        rp, _ = self.robots(urlparse(url).hostname)
        try:
            return rp.can_fetch(self.token, url)
        except Exception:
            return False

    def get(self, url):
        """Renvoie (status, texte, url_finale).

        status = -1 si robots.txt interdit l'URL : l'appelant distingue ainsi un
        refus de notre part d'une erreur du serveur.
        """
        if not self.allowed(url):
            return -1, "", url
        return self._raw(url)


def collect_sitemap_urls(fetcher, sitemap_url, pattern, seen=None, depth=0):
    """Descend récursivement un sitemapindex et renvoie les URL qui sont des recettes."""
    if seen is None:
        seen = set()
    if sitemap_url in seen or depth > 4:
        return []
    seen.add(sitemap_url)

    status, body, _ = fetcher.get(sitemap_url)
    if status == -1:
        fetcher.log("  ⊘ robots.txt interdit %s" % sitemap_url)
        return []
    if status != 200 or not body:
        fetcher.log("  ✗ sitemap illisible (%s) %s" % (status, sitemap_url))
        return []

    locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", body)
    if "<sitemapindex" in body:
        origin = urlparse(sitemap_url).hostname
        out = []
        for child in locs:
            # On ne suit pas un sitemap hébergé ailleurs : ce serait crawler un
            # domaine dont on n'a ni lu ni accepté le robots.txt.
            if urlparse(child).hostname != origin:
                fetcher.log("  ⊘ sitemap hors domaine ignoré : %s" % child)
                continue
            out += collect_sitemap_urls(
                fetcher, child, pattern, seen, depth + 1
            )
        return out

    return [u for u in locs if pattern.match(u)]


# --------------------------------------------------------------------------
# État — un SQLite, pour pouvoir couper et reprendre.
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS urls(
  url     TEXT PRIMARY KEY,
  site    TEXT NOT NULL,
  status  TEXT NOT NULL DEFAULT 'todo',   -- todo | ok | vide | refuse | echec
  http    INTEGER,
  tries   INTEGER NOT NULL DEFAULT 0,
  updated INTEGER,
  recipe  TEXT
);
CREATE INDEX IF NOT EXISTS urls_status ON urls(site, status);

-- Frontière du parcours par rubriques (750g). Sans cette table, une coupure au
-- milieu de l'inventaire obligerait à refaire les deux heures de parcours.
CREATE TABLE IF NOT EXISTS pages(
  url  TEXT PRIMARY KEY,
  site TEXT NOT NULL,
  done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS pages_todo ON pages(site, done);
"""


def open_state(path):
    db = sqlite3.connect(path, timeout=30)
    # WAL : deux sites moissonnés en parallèle écrivent dans la même base. Sans lui,
    # chaque écriture verrouillerait le fichier entier et l'autre fil attendrait.
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.commit()
    return db


def discover(db, fetcher, site, max_pages=None):
    """Remplit la table urls avec les fiches du site, par sitemap ou par parcours."""
    if SITES[site].get("discovery") == "crawl":
        return discover_by_crawl(db, fetcher, site, max_pages)
    cfg = SITES[site]
    fetcher.log("\n[%s] inventaire des fiches…" % site)
    urls = collect_sitemap_urls(fetcher, cfg["sitemap"], cfg["pattern"])
    before = db.execute(
        "SELECT COUNT(*) FROM urls WHERE site=?", (site,)
    ).fetchone()[0]
    db.executemany(
        "INSERT OR IGNORE INTO urls(url, site) VALUES(?, ?)",
        [(u, site) for u in urls],
    )
    db.commit()
    after = db.execute(
        "SELECT COUNT(*) FROM urls WHERE site=?", (site,)
    ).fetchone()[0]
    fetcher.log(
        "[%s] %d URL au sitemap, %d nouvelles (total connu : %d)"
        % (site, len(urls), after - before, after)
    )


HREF_RE = re.compile(r'href="([^"#]+)"', re.IGNORECASE)


def discover_by_crawl(db, fetcher, site, max_pages=None):
    """Inventaire de 750g, qui ne publie aucun sitemap.

    On parcourt l'arbre des rubriques en largeur. Chaque page visitée livre trois
    choses : des sous-rubriques (à visiter à leur tour), des fiches recette (à
    stocker), et implicitement une page suivante.

    La pagination ne s'arrête pas où le site le laisse croire : la page 1 de
    /recettes-desserts/cookies/ annonce 33 pages, or la page 34 existe et contient
    17 fiches inédites. On avance donc tant qu'une page rapporte des fiches, et on
    s'arrête sur la vraie fin — un 301 vers la rubrique, que l'on repère parce que
    l'URL finale ne correspond plus à celle demandée.
    """
    cfg = SITES[site]
    fetcher.log("\n[%s] parcours des rubriques (pas de sitemap publié)…" % site)

    db.executemany(
        "INSERT OR IGNORE INTO pages(url, site) VALUES(?, ?)",
        [(u, site) for u in cfg["seeds"]],
    )
    db.commit()

    visited = 0
    while True:
        row = db.execute(
            "SELECT url FROM pages WHERE site=? AND done=0 LIMIT 1", (site,)
        ).fetchone()
        if not row:
            break
        if max_pages and visited >= max_pages:
            fetcher.log("[%s] plafond de %d pages atteint, inventaire suspendu "
                        "(relancer pour le poursuivre)" % (site, max_pages))
            break
        page = row[0]
        status, html, final = fetcher.get(page)
        visited += 1

        # Redirection : on est retombé ailleurs, donc au-delà de la dernière page.
        redirected = final and final.rstrip("/") != page.rstrip("/")
        if status != 200 or redirected:
            db.execute("UPDATE pages SET done=1 WHERE url=?", (page,))
            db.commit()
            continue

        recipes, cats = set(), set()
        for href in HREF_RE.findall(html):
            u = urljoin(page, href.strip())
            u = u.split("#")[0]
            if cfg["pattern"].match(u):
                recipes.add(u)
            elif cfg["category"].match(u):
                cats.add(u)

        if recipes:
            db.executemany(
                "INSERT OR IGNORE INTO urls(url, site) VALUES(?, ?)",
                [(u, site) for u in recipes],
            )
            # Cette page a rapporté des fiches : la suivante vaut donc d'être tentée.
            cats.add(next_page_url(page))
        db.executemany(
            "INSERT OR IGNORE INTO pages(url, site) VALUES(?, ?)",
            [(u, site) for u in cats],
        )
        db.execute("UPDATE pages SET done=1 WHERE url=?", (page,))
        db.commit()

        if visited % 20 == 0:
            known = db.execute(
                "SELECT COUNT(*) FROM urls WHERE site=?", (site,)
            ).fetchone()[0]
            left = db.execute(
                "SELECT COUNT(*) FROM pages WHERE site=? AND done=0", (site,)
            ).fetchone()[0]
            fetcher.log(
                "[%s] %d pages parcourues, %d fiches connues, %d pages en attente"
                % (site, visited, known, left)
            )

    total = db.execute(
        "SELECT COUNT(*) FROM urls WHERE site=?", (site,)
    ).fetchone()[0]
    fetcher.log(
        "[%s] parcours terminé : %d pages visitées, %d fiches trouvées"
        % (site, visited, total)
    )


def next_page_url(page):
    """/rubrique/ → /rubrique/?page=2 ; /rubrique/?page=7 → /rubrique/?page=8."""
    m = re.match(r"^(.*\?page=)(\d+)$", page)
    if m:
        return m.group(1) + str(int(m.group(2)) + 1)
    return page.split("?")[0] + "?page=2"


def human_duration(seconds):
    seconds = int(max(0, seconds))
    h, m = divmod(seconds // 60, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return "%dj %dh" % (d, h)
    return "%dh%02d" % (h, m) if h else "%d min" % m


def harvest(db, fetcher, site, limit=None, retry_failed=False):
    cfg = SITES[site]
    states = ["todo", "echec"] if retry_failed else ["todo"]
    rows = db.execute(
        "SELECT url FROM urls WHERE site=? AND status IN (%s) ORDER BY url"
        % ",".join("?" * len(states)),
        [site] + states,
    ).fetchall()
    todo = [r[0] for r in rows]
    if limit:
        todo = todo[:limit]
    if not todo:
        fetcher.log("[%s] rien à récupérer." % site)
        return

    total = len(todo)
    # L'hôte vient d'une fiche à récupérer, et non du sitemap : 750g n'en a pas.
    gap = fetcher.robots(urlparse(todo[0]).hostname)[1]
    fetcher.log(
        "[%s] %d fiches à récupérer — environ %s à cette cadence."
        % (site, total, human_duration(total * gap))
    )

    ok = vide = refuse = echec = 0
    started = time.monotonic()
    for i, url in enumerate(todo, 1):
        status, html, _ = fetcher.get(url)
        now = int(time.time())
        if status == -1:
            db.execute(
                "UPDATE urls SET status='refuse', http=-1, updated=? WHERE url=?",
                (now, url),
            )
            refuse += 1
        elif status == 200:
            rec = html_to_recipe(html, url, cfg["source_type"])
            if rec:
                db.execute(
                    "UPDATE urls SET status='ok', http=200, recipe=?, updated=? WHERE url=?",
                    (json.dumps(rec, ensure_ascii=False), now, url),
                )
                ok += 1
            else:
                db.execute(
                    "UPDATE urls SET status='vide', http=200, updated=? WHERE url=?",
                    (now, url),
                )
                vide += 1
        else:
            db.execute(
                "UPDATE urls SET status='echec', http=?, tries=tries+1, updated=? WHERE url=?",
                (status, now, url),
            )
            echec += 1

        if i % 25 == 0 or i == total:
            db.commit()
            elapsed = time.monotonic() - started
            fetcher.log(
                "[%s] %d/%d — %d recettes, %d vides, %d échecs, %d refusées — reste ~%s"
                % (
                    site,
                    i,
                    total,
                    ok,
                    vide,
                    echec,
                    refuse,
                    human_duration((elapsed / i) * (total - i)),
                )
            )
    db.commit()


def write_chunk(out_dir, site, part, batch):
    path = os.path.join(out_dir, "panier-recettes-%s-%04d.json" % (site, part))
    payload = {
        "_kind": APP_KIND,
        "_v": 1,
        "_date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "recipes": batch,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("  → %s (%d recettes)" % (os.path.basename(path), len(batch)))
    return 1


def export(db, out_dir, chunk, sites):
    """Écrit les recettes en fichiers panier-recipes, découpés.

    Découpés parce que l'import de l'application écrit dans IndexedDB recette par
    recette puis redessine la liste : 45 000 d'un coup ferait ramer, voire figerait
    l'onglet. Par paquets de quelques centaines, l'import reste rapide et on peut
    s'arrêter en cours de route.
    """
    os.makedirs(out_dir, exist_ok=True)
    written = files = doublons = 0
    for site in sites:
        rows = db.execute(
            "SELECT recipe FROM urls WHERE site=? AND status='ok' ORDER BY url",
            (site,),
        ).fetchall()
        seen, batch, part = set(), [], 1
        for (blob,) in rows:
            try:
                rec = json.loads(blob)
            except ValueError:
                continue
            # L'application dédoublonne aussi par nom à l'import ; le faire ici évite
            # d'écrire des fichiers pleins de recettes qui seraient ignorées.
            key = norm_name(rec.get("name"))
            if not key or key in seen:
                doublons += 1
                continue
            seen.add(key)
            batch.append(rec)
            if len(batch) >= chunk:
                files += write_chunk(out_dir, site, part, batch)
                written += len(batch)
                batch, part = [], part + 1
        if batch:
            files += write_chunk(out_dir, site, part, batch)
            written += len(batch)
    print(
        "\n%d recettes écrites dans %d fichiers (%s), %d doublons de nom écartés."
        % (written, files, out_dir, doublons)
    )
    print(
        "Dans Panier : Réglages → « Importer des recettes partagées », un fichier à la fois."
    )


def stats(db):
    print("\nÉtat :")
    for site, status, n in db.execute(
        "SELECT site, status, COUNT(*) FROM urls GROUP BY site, status ORDER BY site, status"
    ):
        print("  %-10s %-8s %6d" % (site, status, n))


def main():
    p = argparse.ArgumentParser(
        description="Moissonne les recettes de Marmiton, Jow, CuisineAZ et 750g "
        "au format Panier, "
        "en respectant le robots.txt de chaque site.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exemple : python3 %s --site cuisineaz --site 750g --parallel --delay 1.5"
        % os.path.basename(sys.argv[0]),
    )
    p.add_argument(
        "--site", choices=sorted(SITES) + ["all"], action="append",
        dest="sites", help="répétable : --site cuisineaz --site 750g. "
        "Par défaut, tous les sites.",
    )
    p.add_argument(
        "--parallel",
        action="store_true",
        help="un fil par site, en parallèle. La cadence reste comptée PAR DOMAINE, "
        "donc chaque site reçoit exactement la même charge qu'en séquentiel — "
        "seul le temps total est divisé. Indispensable pour tenir CuisineAZ et "
        "750g en deux jours.",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="secondes minimum entre deux requêtes sur un même domaine "
        "(défaut 2 ; un Crawl-delay plus long déclaré par le site l’emporte)",
    )
    p.add_argument(
        "--out", default="export", help="dossier des fichiers JSON produits"
    )
    p.add_argument(
        "--state",
        default="panier-scrape.sqlite",
        help="base d’état, pour reprendre après une coupure",
    )
    p.add_argument(
        "--chunk", type=int, default=400, help="recettes par fichier exporté"
    )
    p.add_argument("--limit", type=int, help="ne traiter que N fiches (essai)")
    p.add_argument(
        "--discover-limit",
        type=int,
        help="plafonne le nombre de pages de rubrique parcourues (750g). "
        "Sert aux essais, et de garde-fou si l'arbre s'avérait plus vaste que prévu.",
    )
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--user-agent", default=DEFAULT_UA)
    p.add_argument(
        "--retry-failed",
        action="store_true",
        help="réessayer les fiches en échec réseau",
    )
    p.add_argument(
        "--export-only",
        action="store_true",
        help="ne rien récupérer, réécrire les fichiers depuis l’état",
    )
    p.add_argument("--stats-only", action="store_true")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    chosen = args.sites or ["all"]
    sites = list(SITES) if "all" in chosen else list(dict.fromkeys(chosen))
    db = open_state(args.state)

    if args.stats_only:
        stats(db)
        return

    if not args.export_only:
        print("Agent déclaré : %s" % args.user_agent)
        print(
            "Cadence : 1 requête / %.1fs et par domaine%s."
            % (args.delay, ", fils parallèles" if args.parallel else ", séquentiel")
        )
        if args.parallel:
            run_parallel(args, sites)
        else:
            f = Fetcher(
                args.user_agent, args.delay, args.timeout, verbose=not args.quiet
            )
            for site in sites:
                discover(db, f, site, max_pages=args.discover_limit)
                harvest(
                    db, f, site, limit=args.limit, retry_failed=args.retry_failed
                )

    stats(db)
    export(db, args.out, args.chunk, sites)
    db.close()


def run_parallel(args, sites):
    """Un fil par site, chacun avec son Fetcher et sa connexion SQLite.

    Les sites étant sur des domaines distincts, leurs cadences ne se gênent pas :
    chaque domaine continue de recevoir une requête toutes les --delay secondes,
    exactement comme en séquentiel. C'est le temps total qui est divisé, pas la
    politesse qui est réduite.

    Chaque fil ouvre sa propre connexion : un objet sqlite3.Connection ne se
    partage pas entre fils, et le mode WAL permet aux écritures de se croiser.
    """
    threads = []
    for site in sites:
        t = threading.Thread(
            target=_site_worker, args=(args, site), name=site, daemon=False
        )
        t.start()
        threads.append(t)
        # Léger décalage au démarrage : les journaux des fils restent lisibles.
        time.sleep(1)
    for t in threads:
        t.join()


def _site_worker(args, site):
    db = open_state(args.state)
    try:
        f = Fetcher(
            args.user_agent,
            args.delay,
            args.timeout,
            verbose=not args.quiet,
            prefix="[%s] " % site,
        )
        discover(db, f, site, max_pages=args.discover_limit)
        harvest(db, f, site, limit=args.limit, retry_failed=args.retry_failed)
    except Exception as e:
        print("[%s] fil interrompu : %s" % (site, e), flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nInterrompu. L’état est enregistré : relancer la même commande reprend "
            "où on s’est arrêté."
        )
        sys.exit(130)
