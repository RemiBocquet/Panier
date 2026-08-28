#!/usr/bin/env python3
"""
Panier — service de recherche dans le catalogue moissonné.

Sert la base produite par scrape-recipes.py (~180 000 recettes) à l'application,
qui interroge /api/recipes/search puis /api/recipes/recipe pour importer au clic.

POURQUOI UN SERVICE PLUTÔT QU'UN GROS FICHIER
---------------------------------------------
Un index statique de 180 000 noms pèse quelques mégaoctets qu'il faudrait
télécharger sur chaque téléphone avant la première recherche. Ici la base ne
quitte jamais le Pi : une recherche envoie trois mots et reçoit vingt lignes,
soit quelques kilo-octets. Sur un catalogue de cette taille, la différence est
celle entre « instantané » et « attendre le chargement ».

CE QUE LE SERVICE EXPOSE
------------------------
  GET  <base>/search?q=courgette&limit=20[&site=marmiton][&lang=en]
       → {"results":[{id,token,name,site,image,servings,prep,cook,ingredients,steps}]}
  GET  <base>/recipe?id=12345&t=<token>[&lang=en]
       → la fiche complète, au format attendu par l'import de Panier
  GET  <base>/stats
       → {"total":…}

`lang=en` traduit à la volée, une seule fois par fiche : voir translate.py, qui
tient le lexique et les caches dans `<db>.tr`. Sans moteur configuré le
paramètre est simplement ignoré et le français est servi — la traduction est un
agrément, pas une dépendance du service.

Les fiches sont déjà stockées au format Panier par le moissonneur : ce service ne
fait que les retrouver, il ne retransforme rien.

Le `token` n'est pas décoratif : /recipe le REFUSE s'il manque. Une fiche ne
s'obtient donc qu'en étant passé par une recherche, jamais en énumérant les
numéros. Voir le commentaire « Restreindre l'accès au catalogue » plus bas.

INSTALLATION SUR LE PI
----------------------
    # 1. Construire l'index de recherche (à refaire après chaque moisson)
    python3 tools/recipe-server.py --db ~/panier-scrape/panier-scrape.sqlite \
                                   --build-index

    # 2. Lancer le service
    python3 tools/recipe-server.py --db ~/panier-scrape/panier-scrape.sqlite \
                                   --port 8765

    # 3. L'exposer derrière nginx, sur le domaine qui sert déjà l'application.
    #    --install-help sort l'unité systemd ET le bloc nginx tout prêts, quota
    #    compris. Ne pas omettre le X-Forwarded-For qu'il contient : sans lui le
    #    service voit tout venir de 127.0.0.1 et son quota devient collectif.

    python3 tools/recipe-server.py --db … --install-help

Python 3.7+, bibliothèque standard uniquement.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Voisin de ce fichier. L'import est tolérant à dessein : quelqu'un qui n'a pas
# besoin de l'anglais doit pouvoir lancer le service avec le seul recipe-server.py
# dans les mains, comme avant.
#
# OSError autant qu'ImportError, et ce n'est pas de la prudence gratuite : un
# translate.py présent mais illisible (déployé en root sous /var/www, par
# exemple) fait lever PermissionError — un OSError, qu'« except ImportError »
# laisse passer. Le service planterait au démarrage au lieu de faire ce pour
# quoi ce garde-fou existe : servir le français et se taire.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
#
# Le filet est volontairement LARGE — un except Exception nu, ce qu'on s'interdit
# partout ailleurs. La raison tient à ce que ce module est facultatif : quelle
# que soit la façon dont son chargement échoue, le service doit servir le
# français, jamais refuser de démarrer. Or les modes d'échec ne se ressemblent
# pas : ImportError s'il est absent, PermissionError s'il est illisible, et
# SyntaxError si une copie a été tronquée en route — et cette dernière n'est ni
# un ImportError ni un OSError, elle passait donc au travers.
try:
    import translate as tr_mod
except Exception as _e:  # pragma: no cover
    sys.stderr.write(
        "translate.py inutilisable (%s: %s) : le service démarre sans "
        "traduction.\n" % (type(_e).__name__, _e)
    )
    tr_mod = None

# Origines autorisées à appeler ce service depuis un navigateur. Même liste que
# cors-relay.js : l'application est servie par le Pi (donc même origine, où CORS
# ne s'applique pas), mais l'ancien hébergement GitHub Pages reste à couvrir.
ALLOWED_ORIGINS = [
    "https://panier.remibocquet.fr",
    "https://remibocquet.github.io",
]

MAX_LIMIT = 50
DEFAULT_LIMIT = 20


# --------------------------------------------------------------------------
# Restreindre l'accès au catalogue
#
# CE QUI N'EST PAS POSSIBLE, ET POURQUOI
# L'application est un fichier statique public : tout ce qu'elle sait, n'importe
# qui le lit dans le source de la page. Un jeton en dur n'est donc pas un secret.
# Et CORS n'engage que les navigateurs — curl ignore complètement ces en-têtes.
# Il n'existe aucun moyen de prouver « c'est bien l'application qui appelle ».
#
# CE QUI EST FAIT À LA PLACE — fermer les trois portes réelles :
#
#   1. Non-énumérable.  /recipe n'accepte plus un numéro de ligne, mais un jeton
#      signé que seule une recherche délivre. Sans ça, un compteur de 1 à 180 000
#      suffisait à aspirer toute la base en quelques heures. C'est la vraie
#      brèche, et c'est celle qui compte.
#   2. Cadencé.         Un quota par adresse IP. Une personne qui cherche fait
#      quelques requêtes par minute ; un aspirateur en veut des milliers. Le
#      quota ne gêne pas la première et rend le second interminable.
#   3. Même origine.    Les requêtes venant d'un autre site sont refusées, ce qui
#      empêche qu'une page tierce se serve du catalogue via ses visiteurs.
#
# Aucune de ces trois barrières n'est infranchissable prise isolément. Ensemble
# elles font qu'un usage normal passe et qu'une extraction massive ne passe pas,
# ce qui est l'objectif réel.
# --------------------------------------------------------------------------

# Durée de validité d'un jeton de recette. Deux fenêtres sont acceptées (celle en
# cours et la précédente), pour qu'un jeton obtenu à 10 h 59 marche encore à 11 h 01.
TOKEN_TTL = 3600


def load_secret(db_path):
    """Clé de signature, tirée au sort au premier lancement et conservée.

    Elle vit à côté de la base plutôt qu'en mémoire : sans ça, un redémarrage du
    service invaliderait tous les jetons des recherches en cours.
    """
    path = db_path + ".secret"
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read().strip()
            if len(data) >= 32:
                return data
    secret = base64.b64encode(os.urandom(48))
    # 0600 : lisible du seul compte qui fait tourner le service.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(secret)
    return secret


def sign_id(secret, rid, window=None):
    if window is None:
        window = int(time.time() // TOKEN_TTL)
    msg = ("%d:%d" % (rid, window)).encode("ascii")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:24]


def check_token(secret, rid, token):
    if not token:
        return False
    now = int(time.time() // TOKEN_TTL)
    # compare_digest : la comparaison ne doit pas fuir par son temps d'exécution.
    return any(
        hmac.compare_digest(sign_id(secret, rid, w), token)
        for w in (now, now - 1)
    )


class RateLimiter:
    """Seau à jetons par adresse IP.

    Volontairement en mémoire : au redémarrage tout le monde repart à zéro, ce
    qui est sans importance ici. Un dictionnaire qui ne serait jamais purgé
    finirait par grossir indéfiniment, d'où le nettoyage périodique.
    """

    def __init__(self, rate, burst):
        self.rate = float(rate)      # requêtes par seconde reconstituées
        self.burst = float(burst)    # pointe tolérée
        self._buckets = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def allow(self, key):
        now = time.monotonic()
        with self._lock:
            if now - self._last_sweep > 300:
                cutoff = now - 600
                self._buckets = {
                    k: v for k, v in self._buckets.items() if v[1] > cutoff
                }
                self._last_sweep = now
            tokens, seen = self._buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - seen) * self.rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True


def strip_accents(s):
    return "".join(
        c for c in unicodedata.normalize("NFD", str(s))
        if unicodedata.category(c) != "Mn"
    )


def norm(s):
    return re.sub(r"\s+", " ", strip_accents(str(s or "")).lower()).strip()


# --------------------------------------------------------------------------
# Index de recherche
#
# Le moissonneur stocke la recette en JSON dans urls.recipe. Chercher dedans à
# coups de LIKE sur 180 000 lignes marche, mais mal : pas de classement, et les
# accents comptent. On matérialise donc les noms dans une table dédiée, doublée
# d'un index FTS5 quand SQLite en dispose.
#
# FTS5 est présent dans la quasi-totalité des SQLite modernes, Raspberry Pi OS
# compris — mais pas dans tous, et un service qui refuse de démarrer pour ça
# serait pénible. D'où le repli LIKE, plus fruste mais suffisant.
# --------------------------------------------------------------------------
INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_idx(
  id   INTEGER PRIMARY KEY,   -- = urls.rowid
  name TEXT NOT NULL,
  norm TEXT NOT NULL,
  site TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS search_idx_norm ON search_idx(norm);
CREATE INDEX IF NOT EXISTS search_idx_site ON search_idx(site);
"""


def has_fts5(db):
    try:
        db.execute("CREATE VIRTUAL TABLE temp.__fts_probe USING fts5(x)")
        db.execute("DROP TABLE temp.__fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def build_index(path, verbose=True):
    db = sqlite3.connect(path, timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(INDEX_SCHEMA)

    def say(*a):
        if verbose:
            print(*a, flush=True)

    say("Lecture des fiches récupérées…")
    rows = db.execute(
        "SELECT rowid, site, recipe FROM urls WHERE status='ok' AND recipe IS NOT NULL"
    ).fetchall()

    entries = []
    for rid, site, blob in rows:
        try:
            name = (json.loads(blob) or {}).get("name") or ""
        except ValueError:
            continue
        name = name.strip()
        if name:
            entries.append((rid, name, norm(name), site))

    db.execute("DELETE FROM search_idx")
    db.executemany(
        "INSERT INTO search_idx(id, name, norm, site) VALUES(?,?,?,?)", entries
    )
    db.commit()
    say("  %d fiches indexées." % len(entries))

    if has_fts5(db):
        # remove_diacritics 2 : « creme » retrouve « crème », ce qui compte
        # beaucoup quand on cherche au pouce sur un téléphone.
        db.execute("DROP TABLE IF EXISTS search_fts")
        db.execute(
            "CREATE VIRTUAL TABLE search_fts USING fts5("
            "  name, content='search_idx', content_rowid='id',"
            "  tokenize=\"unicode61 remove_diacritics 2\")"
        )
        db.execute("INSERT INTO search_fts(search_fts) VALUES('rebuild')")
        db.commit()
        say("  Index FTS5 construit (recherche classée, insensible aux accents).")
    else:
        db.execute("DROP TABLE IF EXISTS search_fts")
        db.commit()
        say("  SQLite sans FTS5 : repli sur une recherche LIKE, sans classement.")

    db.close()


def fts_query(q):
    """Traduit une saisie libre en requête FTS5 sûre.

    On ne passe JAMAIS le texte de l'utilisateur tel quel : les opérateurs FTS5
    (guillemets, NEAR, *, parenthèses) provoqueraient des erreurs de syntaxe sur
    une saisie ordinaire comme « poulet (rapide) ». On ne garde donc que des
    mots, chacun transformé en préfixe : « poulet curry » trouve « poulet au
    curry » comme « currywurst au poulet ».
    """
    toks = [t for t in re.split(r"[^0-9a-zà-ÿA-ZÀ-Ÿ]+", q) if len(t) >= 2]
    return " ".join('"%s"*' % t.replace('"', "") for t in toks[:8])


class Catalog:
    """Accès en lecture à la base. Une connexion par fil : sqlite3 l'exige."""

    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        probe = self._db()
        self.fts = bool(
            probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_fts'"
            ).fetchone()
        )
        if not probe.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_idx'"
        ).fetchone():
            raise SystemExit(
                "Index absent. Lance d'abord :\n"
                "  python3 %s --db %s --build-index"
                % (os.path.basename(sys.argv[0]), path)
            )

    def _db(self):
        db = getattr(self._local, "db", None)
        if db is None:
            # Ouverture en lecture seule : le moissonneur peut tourner en même
            # temps, et ce service n'a aucune raison de pouvoir écrire.
            db = sqlite3.connect(
                "file:%s?mode=ro" % self.path, uri=True, timeout=15
            )
            self._local.db = db
        return db

    def search(self, q, limit, site=None):
        db = self._db()
        params = []
        if self.fts:
            match = fts_query(q)
            if not match:
                return []
            sql = (
                "SELECT i.id, i.name, i.site FROM search_fts f "
                "JOIN search_idx i ON i.id = f.rowid "
                "WHERE search_fts MATCH ? "
            )
            params.append(match)
            if site:
                sql += "AND i.site = ? "
                params.append(site)
            sql += "ORDER BY rank LIMIT ?"
        else:
            sql = "SELECT id, name, site FROM search_idx WHERE norm LIKE ? "
            params.append("%" + norm(q) + "%")
            if site:
                sql += "AND site = ? "
                params.append(site)
            # Sans classement FTS, le plus court d'abord : « Tarte aux pommes »
            # remonte avant « Tarte aux pommes façon grand-mère revisitée ».
            sql += "ORDER BY length(name) LIMIT ?"
        params.append(limit)

        try:
            rows = db.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []

        ids = [r[0] for r in rows]
        recipes = self._recipes(ids)
        out = []
        for rid, name, st in rows:
            rec = recipes.get(rid) or {}
            out.append(
                {
                    "id": rid,
                    "name": rec.get("name") or name,
                    "site": st,
                    "image": rec.get("image") or "",
                    "servings": rec.get("servings"),
                    "prep": rec.get("prep"),
                    "cook": rec.get("cook"),
                    "ingredients": len(rec.get("ingredients") or []),
                    "steps": len(rec.get("steps") or []),
                }
            )
        return out

    def _recipes(self, ids):
        if not ids:
            return {}
        db = self._db()
        qs = ",".join("?" * len(ids))
        out = {}
        for rid, blob in db.execute(
            "SELECT rowid, recipe FROM urls WHERE rowid IN (%s)" % qs, ids
        ):
            try:
                out[rid] = json.loads(blob)
            except (ValueError, TypeError):
                pass
        return out

    def recipe(self, rid):
        return self._recipes([rid]).get(rid)

    def stats(self):
        db = self._db()
        sites = {
            s: n
            for s, n in db.execute(
                "SELECT site, COUNT(*) FROM search_idx GROUP BY site"
            )
        }
        return {
            "total": sum(sites.values()),
            "sites": sites,
            "engine": "fts5" if self.fts else "like",
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "PanierCatalog/1.0"
    catalog = None
    secret = None
    limiter = None
    strict = True
    store = None        # translate.TrStore, ou None si la traduction est coupée
    translator = None   # translate.Translator

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def client_key(self):
        """L'IP réelle de l'appelant, pas celle de nginx.

        Le service n'écoutant que sur la boucle locale, toute requête arrive de
        127.0.0.1 : sans X-Forwarded-For, le quota serait partagé par tout le
        monde et le premier venu bloquerait les autres. On ne fait confiance à
        cet en-tête QUE si la connexion vient bien de la machine elle-même —
        ailleurs, il serait dicté par l'appelant.
        """
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1"):
            fwd = self.headers.get("X-Forwarded-For")
            if fwd:
                return fwd.split(",")[0].strip()[:45]
        return peer

    def looks_like_the_app(self):
        """Rejette ce qui vient visiblement d'ailleurs que de l'application.

        Sec-Fetch-Site est posé par le navigateur lui-même et ne peut pas être
        modifié par un script de page : « cross-site » signifie qu'un autre site
        appelle, et c'est un refus net. Une origine inconnue l'est tout autant.

        Ce que ce test n'attrape PAS, et il faut le savoir : un client qui
        fabrique ses en-têtes à la main. C'est le quota, lui, qui s'en charge.
        """
        if not self.strict:
            return True
        origin = self.headers.get("Origin")
        if origin:
            # Une origine explicite tranche à elle seule. Ne pas y ajouter le
            # test Sec-Fetch-Site : l'application servie depuis l'ancienne
            # adresse GitHub Pages est légitimement « cross-site » vis-à-vis du
            # Pi, et se verrait refusée alors qu'elle figure dans la liste.
            return origin in ALLOWED_ORIGINS
        # Pas d'origine : requête de même site, ou client hors navigateur.
        return self.headers.get("Sec-Fetch-Site") != "cross-site"

    def wanted_lang(self, raw):
        """La langue demandée, ou None s'il n'y a rien à traduire.

        Rend None pour « fr » comme pour une langue qu'on ne sait pas produire :
        dans les deux cas la suite du traitement doit servir le catalogue tel
        quel, et c'est le même chemin de code. None dit « ne touche à rien »,
        ce qui vaut mieux qu'un drapeau de plus à tester partout.
        """
        lang = (raw or "").strip().lower()[:5]
        if not lang or lang.startswith("fr"):
            return None
        if self.store is None or self.translator is None:
            return None
        # Une seule langue cible pour l'instant. En accepter d'autres au petit
        # bonheur remplirait le cache de traductions que rien ne produit.
        return "en" if lang.startswith("en") else None

    def _cors(self):
        origin = self.headers.get("Origin")
        # Une origine inconnue ne reçoit PAS d'en-tête : renvoyer « null » ne
        # serait pas un refus mais une origine réelle, celle des iframes en bac
        # à sable. Même raisonnement que dans cors-relay.js.
        if origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        # HEAD : mêmes en-têtes, pas de corps. C'est ce que fait toute sonde de
        # supervision, et sans do_HEAD la classe de base repondait 501.
        if not getattr(self, "head_only", False):
            self.wfile.write(body)

    def do_HEAD(self):
        self.head_only = True
        try:
            self.do_GET()
        finally:
            self.head_only = False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self._cors()
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        # nginx peut transmettre /api/recipes/search ou /search selon la façon
        # dont proxy_pass est écrit : on ne regarde que le dernier segment.
        route = u.path.rstrip("/").rsplit("/", 1)[-1].lower()
        qs = parse_qs(u.query)

        def arg(k, default=""):
            return (qs.get(k) or [default])[0]

        if not self.looks_like_the_app():
            return self._json({"error": "origine non autorisée"}, 403)
        if not self.limiter.allow(self.client_key()):
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Retry-After", "10")
            self._cors()
            self.end_headers()
            if not getattr(self, "head_only", False):
                self.wfile.write(b'{"error":"trop de requetes"}')
            return

        if route == "search":
            q = arg("q").strip()[:100]
            if len(q) < 2:
                return self._json({"results": [], "error": "requête trop courte"})
            try:
                limit = max(1, min(MAX_LIMIT, int(arg("limit", DEFAULT_LIMIT))))
            except ValueError:
                limit = DEFAULT_LIMIT
            site = arg("site").strip()[:20] or None
            lang = self.wanted_lang(arg("lang"))

            # L'index est français ; un anglophone tape « chicken ». Sans ce
            # passage par le lexique en sens inverse, sa recherche ne rendrait
            # rien du tout et le catalogue lui paraîtrait vide.
            cands = tr_mod.search_queries(self.store, q, lang) if lang else [q]

            # Le lexique n'a RIEN su traduire. Pour un anglophone, essayer la
            # saisie brute en premier est alors un mauvais pari : elle ne
            # ramène que les titres français contenant par hasard le mot
            # anglais — « waffle » rendait des recettes de gaufres écrites
            # « waffle », jamais celles écrites « gaufre ». Et comme elle rend
            # QUELQUE CHOSE, aucun repli ne se déclenchait derrière. On demande
            # donc au moteur avant, pas après.
            if lang and cands == [q]:
                fr = tr_mod.translated_query(self.store, self.translator, q, lang)
                if fr:
                    cands = [fr, q]

            results = []
            for cand in cands:
                results = self.catalog.search(cand, limit, site)
                if results:
                    break

            # Les pistes du lexique ont toutes échoué : le moteur en dernier
            # ressort. Le second appel ne coûte rien de plus, la traduction
            # d'une requête étant gardée dès la première fois.
            if lang and not results:
                fr = tr_mod.translated_query(self.store, self.translator, q, lang)
                if fr and fr not in cands:
                    results = self.catalog.search(fr, limit, site)

            # Chaque résultat repart avec son laissez-passer : c'est la seule
            # façon d'obtenir une fiche complète ensuite.
            for r in results:
                r["token"] = sign_id(self.secret, r["id"])

            if lang and results:
                titles = tr_mod.translate_titles(
                    [(r["id"], r["name"]) for r in results], self.store,
                    self.translator, lang,
                )
                for r in results:
                    if r["id"] in titles:
                        r["name"] = titles[r["id"]]
            # La langue réellement servie, comme sur /recipe. Sans elle, un
            # `curl` ne distingue pas « le serveur ignore lang=en » de « le
            # client ne l'envoie pas » — deux pannes très différentes qui se
            # ressemblent trait pour trait.
            return self._json({"results": results, "lang": lang or "fr"})

        if route == "recipe":
            try:
                rid = int(arg("id", "0"))
            except ValueError:
                return self._json({"error": "id invalide"}, 400)
            # Sans jeton valide, aucune fiche : un simple compteur de 1 à 180 000
            # ne mène donc plus nulle part.
            if not check_token(self.secret, rid, arg("t")):
                return self._json({"error": "jeton absent ou expiré"}, 403)
            rec = self.catalog.recipe(rid)
            if not rec:
                return self._json({"error": "recette introuvable"}, 404)
            lang = self.wanted_lang(arg("lang"))
            if lang:
                # get_translated ne lève jamais : quota épuisé ou moteur à
                # l'arrêt, la fiche repart en français avec lang="fr". Refuser
                # l'import pour ça punirait l'utilisateur d'une panne qui ne le
                # regarde pas.
                rec, lang = tr_mod.get_translated(
                    rid, rec, self.store, self.translator, lang
                )
            return self._json({"recipe": rec, "lang": lang or "fr"})

        if route == "stats":
            # Volontairement muet sur la composition du catalogue : le nombre de
            # recettes par site n'apprend rien d'utile à l'application, et
            # renseignerait qui chercherait à en évaluer l'intérêt.
            st = self.catalog.stats()
            return self._json({"total": st["total"]})

        return self._json({"error": "route inconnue"}, 404)


SYSTEMD_UNIT = """\n# /etc/systemd/system/panier-catalog.service
[Unit]
Description=Panier - service de recherche de recettes
After=network.target

[Service]
Type=simple
User=%(user)s
WorkingDirectory=%(dir)s
ExecStart=/usr/bin/python3 %(script)s --db %(db)s --port %(port)d
Restart=on-failure
RestartSec=5

# Traduction (facultatif). La cle DeepL se lit d'elle-meme dans %(db)s.deepl
# si ce fichier existe en 0600 : la mettre ici l'exposerait a systemctl show,
# que tout le monde peut lire. LibreTranslate, lui, n'a pas de secret.
#Environment=PANIER_LIBRETRANSLATE_URL=http://127.0.0.1:5000

# Ce service est joignable depuis internet a travers nginx. Il n'a besoin que
# de lire un fichier dans le home et d'ecouter sur la boucle locale : autant
# lui retirer le reste. Ne PAS ajouter ProtectHome, la base est dans le home.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
"""

NGINX_SNIPPET = """\
# Au niveau http{} (une seule fois, hors de tout server{}) :
limit_req_zone $binary_remote_addr zone=panier_catalog:10m rate=2r/s;

# Dans le server{} qui sert deja l'application :
location /api/recipes/ {
    limit_req zone=panier_catalog burst=20 nodelay;
    proxy_pass http://127.0.0.1:%(port)d/;
    proxy_set_header Host $host;
    # Indispensable : sans lui le service voit toutes les requetes venir de
    # 127.0.0.1, et son quota serait partage par tous les visiteurs a la fois.
    proxy_set_header X-Forwarded-For $remote_addr;
}
"""


def check(args):
    """Dit, en une commande, pourquoi la traduction ne marche pas.

    Elle échoue en silence par construction — une panne de traduction ne doit
    jamais empêcher de servir le français — et c'est très bien pour
    l'utilisateur, très pénible pour qui installe. Ce mode dit tout haut ce que
    le service constate tout bas.
    """
    ok = lambda b: "OK  " if b else "NON "
    print("Script exécuté   : %s" % os.path.abspath(sys.argv[0]))
    print("                   (à comparer avec l'ExecStart de l'unité systemd :")
    print("                    systemctl cat panier-catalog | grep ExecStart)")
    print()

    print("%s catalogue        %s" % (ok(os.path.exists(args.db)), args.db))
    if not os.path.exists(args.db):
        return
    try:
        cat = Catalog(args.db)
        st = cat.stats()
        print("%s index            %d recettes, moteur %s"
              % (ok(True), st["total"], st["engine"]))
    except SystemExit as e:
        print("NON  index            %s" % e)
        return

    print("%s module translate %s"
          % (ok(tr_mod is not None),
             "importé" if tr_mod else "absent ou illisible"))
    if tr_mod is None:
        return

    cache = tr_mod.store_path(args.db)
    try:
        store = tr_mod.TrStore(cache)
        s = store.stats("en")
        nq = store._db().execute("SELECT COUNT(*) FROM query_tr").fetchone()[0]
        print("%s cache            %s" % (ok(True), cache))
        print("                   lexique %d, recettes %d, titres %d, requêtes %d"
              % (s["lexicon"], s["recipes"], s["titles"], nq))
        if s["lexicon"] == 0:
            print("     ⚠ lexique vide : python3 tools/translate.py --db … --seed")
        n = len(store.stale("en"))
        if n:
            print("     ⚠ %d clés périmées : translate.py --db … --renormalize" % n)
    except Exception as e:
        print("NON  cache            %s" % e)
        print("     → c'est CE défaut qui fait servir le français en silence.")
        return

    tr = tr_mod.Translator.from_env(args.db, args.target_lang)
    keyfile = args.db + ".deepl"
    src = ("$PANIER_DEEPL_KEY" if os.environ.get("PANIER_DEEPL_KEY")
           else keyfile if os.path.exists(keyfile) else None)
    key_ok = False
    print("%s clé DeepL        %s" % (ok(bool(tr.deepl_key)), src or "introuvable"))
    if src == keyfile:
        m = os.stat(keyfile).st_mode & 0o777
        print("                   droits %o%s" % (m, "" if m == 0o600 else "  (attendu 600)"))
    if tr.deepl_key:
        # Le choix de l'hôte découle du suffixe de la clé, et une clé gratuite
        # envoyée à api.deepl.com est l'erreur la plus fréquente — invisible
        # sans cette ligne.
        print("                   hôte %s  (clé %s « :fx »)"
              % (tr._deepl_host(),
                 "terminée par" if tr.deepl_key.endswith(":fx") else "SANS"))
        # Une clé DeepL est un UUID, éventuellement suivi de « :fx ». Contrôler
        # sa FORME coûte une ligne et attrape le cas le plus bête et le plus
        # coûteux à diagnostiquer : le fichier contient encore le texte d'un
        # exemple copié-collé, et le serveur répond « clé invalide » sans qu'on
        # ait de raison de soupçonner le fichier.
        key_ok = bool(re.match(
            r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(:fx)?$",
            tr.deepl_key,
        ))
        if not key_ok:
            print("     ⚠ cette clé n'a pas la forme d'une clé DeepL "
                  "(UUID, éventuellement suivi de « :fx »).")
            print("       Vérifie le contenu de %s — il contient peut-être" % keyfile)
            print("       encore le texte d'un exemple plutôt que ta vraie clé.")
            if any(ord(c) > 127 for c in tr.deepl_key):
                # Un en-tête HTTP doit être ASCII. Avec un accent dedans, DeepL
                # ferme la connexion sans répondre au lieu de renvoyer 403 : on
                # cherche alors un problème de réseau qui n'existe pas.
                print("       Elle contient un caractère accentué : l'en-tête HTTP")
                print("       devient invalide et DeepL ferme la connexion sans")
                print("       répondre — d'où « Remote end closed connection ».")
    print("%s LibreTranslate   %s" % (ok(bool(tr.libre_url)), tr.libre_url or "non configuré"))

    if not tr.available():
        print("\n→ Aucun moteur : le service répondra en français, sans erreur.")
        return

    # Sonde DeepL en direct AVANT l'essai de traduction. La façade translate()
    # avale l'erreur pour basculer sur le secours — c'est ce qu'on veut en
    # service, mais ça masque justement le code HTTP qui dit tout ici.
    if tr.deepl_key and not key_ok:
        print("NON  DeepL           sonde inutile tant que la clé est mal formée")
    elif tr.deepl_key:
        try:
            u = tr.usage()
            print("%s DeepL joignable  %s / %s caractères"
                  % (ok(True), u.get("character_count"), u.get("character_limit")))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8")).get("message", "")
            except Exception:
                pass
            raison = {403: "clé refusée", 456: "quota épuisé",
                      429: "trop de requêtes"}.get(e.code, "")
            print("NON  DeepL           HTTP %s%s%s"
                  % (e.code, "  — %s" % raison if raison else "",
                     "\n                   %s" % detail if detail else ""))
        except Exception as e:
            print("NON  DeepL           injoignable : %s" % e)

    print("\nEssai réel (« blanc de poulet », « waffle ») :")
    try:
        out, engine = tr.translate(["blanc de poulet", "waffle"],
                                   context=tr_mod.ING_CONTEXT)
        print("  %s / %s   [moteur : %s]" % (out[0], out[1], engine))
    except Exception as e:
        print("  ÉCHEC : %s" % e)
        print("  → c'est CE défaut qui fait servir le français.")
        if tr.deepl_key:
            print("\n  Pour savoir si le blocage vient de Python ou du réseau,")
            print("  la même requête sans passer par ce script :")
            # Sans -v : la sortie verbeuse imprime l'en-tête d'autorisation,
            # donc la clé en clair — qu'on recopie ensuite sans y penser dans
            # un rapport de bogue ou une conversation.
            print("    curl -s -o /dev/null -w '%{http_code}\\n' -X POST \\")
            print("      '%s/v2/usage' \\" % tr._deepl_host())
            print("      -H \"Authorization: DeepL-Auth-Key $(cat %s)\"" % keyfile)
            print("  403 = clé refusée · 456 = quota épuisé · 000 = réseau bloqué")
        return
    try:
        u = tr.usage()
        if u and u.get("character_limit"):
            print("  Quota DeepL : %d / %d (%.1f %%)"
                  % (u["character_count"], u["character_limit"],
                     100.0 * u["character_count"] / u["character_limit"]))
    except Exception as e:
        print("  Quota DeepL illisible : %s" % e)
    print("\nTout est en place. Si l'application reçoit encore du français,")
    print("c'est qu'elle n'envoie pas ?lang=en :")
    print("  grep -c catalogLang <racine web>/index.html    # doit valoir 3")


def install_help(args):
    print(
        SYSTEMD_UNIT
        % {
            "user": os.environ.get("USER", "pi"),
            "script": os.path.abspath(sys.argv[0]),
            "db": os.path.abspath(args.db),
            "dir": os.path.dirname(os.path.abspath(args.db)),
            "port": args.port,
        }
    )
    print()
    print(NGINX_SNIPPET % {"port": args.port})
    print(
        "Puis :\n"
        "  sudo systemctl daemon-reload\n"
        "  sudo systemctl enable --now panier-catalog\n"
        "  sudo nginx -t && sudo systemctl reload nginx\n"
    )


def main():
    p = argparse.ArgumentParser(
        description="Sert le catalogue moissonné à l'application Panier.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--db",
        default=os.path.expanduser("~/panier-scrape/panier-scrape.sqlite"),
        help="base produite par scrape-recipes.py",
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="127.0.0.1 par défaut : seul nginx, sur la machine, peut joindre "
        "le service. Ne l'ouvrir sur 0.0.0.0 que pour un essai.",
    )
    p.add_argument(
        "--build-index",
        action="store_true",
        help="(re)construit l'index de recherche puis quitte. À relancer après "
        "chaque moisson pour que les nouvelles recettes soient trouvables.",
    )
    p.add_argument(
        "--install-help",
        action="store_true",
        help="affiche l'unité systemd et le bloc nginx à copier",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="diagnostique la traduction (index, cache, cle, essai reel) et quitte",
    )
    p.add_argument(
        "--rate",
        type=float,
        default=1.0,
        help="requetes par seconde et par adresse IP, en regime etabli "
        "(defaut 1 : tres au-dessus d'un usage humain, tres en-dessous de ce "
        "qu'il faudrait pour aspirer le catalogue)",
    )
    p.add_argument(
        "--burst",
        type=float,
        default=20.0,
        help="pointe toleree avant que le quota ne morde (defaut 20)",
    )
    p.add_argument(
        "--no-origin-check",
        action="store_true",
        help="desactive le refus des appels venant d'un autre site. A n'utiliser "
        "que pour un essai en local.",
    )
    p.add_argument(
        "--no-translate",
        action="store_true",
        help="ignore le parametre ?lang= et sert toujours le francais",
    )
    p.add_argument(
        "--target-lang",
        default="EN-GB",
        help="variante anglaise demandee a DeepL (defaut EN-GB, ou EN-US)",
    )
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    if args.install_help:
        return install_help(args)

    if args.check:
        return check(args)

    if not os.path.exists(args.db):
        raise SystemExit("Base introuvable : %s" % args.db)

    if args.build_index:
        return build_index(args.db, verbose=not args.quiet)

    Handler.catalog = Catalog(args.db)
    Handler.secret = load_secret(args.db)
    Handler.limiter = RateLimiter(args.rate, args.burst)
    Handler.strict = not args.no_origin_check

    # La traduction s'installe si elle peut, et se tait si elle ne peut pas.
    # Un cache illisible ou un module absent ne doivent pas empêcher le
    # catalogue de servir le français, qui reste sa raison d'être.
    tr_note = "desactivee"
    if not args.no_translate and tr_mod is not None:
        try:
            store = tr_mod.TrStore(tr_mod.store_path(args.db))
            translator = tr_mod.Translator.from_env(args.db, args.target_lang)
            if translator.available():
                Handler.store = store
                Handler.translator = translator
                lex = store.stats("en")["lexicon"]
                tr_note = "%s, lexique de %d entrees" % (translator.describe(), lex)
                if lex == 0:
                    tr_note += " (vide : lance translate.py --seed)"
            else:
                tr_note = "aucun moteur configure (PANIER_DEEPL_KEY / %s.deepl)" % args.db
        except Exception as e:
            tr_note = "indisponible (%s)" % e

    # Le chemin du script, dans le journal, à chaque démarrage. Plusieurs copies
    # de tools/ sur une machine, dont une seule à jour, est une panne coûteuse à
    # trouver : le service sert le français en silence pendant qu'un --check
    # lancé à la main, sur l'autre copie, affiche tout en vert.
    print("Script : %s" % os.path.abspath(sys.argv[0]), flush=True)
    st = Handler.catalog.stats()
    print(
        "Catalogue : %d recettes (%s), moteur %s"
        % (
            st["total"],
            ", ".join("%s %d" % (k, v) for k, v in sorted(st["sites"].items())),
            st["engine"],
        ),
        flush=True,
    )
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.verbose = not args.quiet
    print(
        "Acces : jetons signes sur /recipe, %.1f req/s par IP (pointe %d)%s"
        % (
            args.rate,
            int(args.burst),
            "" if Handler.strict else ", controle d'origine DESACTIVE",
        ),
        flush=True,
    )
    print("Traduction : %s" % tr_note, flush=True)
    print("À l'écoute sur http://%s:%d" % (args.host, args.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()
