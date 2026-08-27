#!/usr/bin/env python3
"""
Panier — réparation des fiches déjà moissonnées.

Le moissonneur a tourné plus de cent heures avant que trois défauts de son
analyseur d'ingrédients ne soient repérés. Les corriger dans scrape-recipes.py
ne sert que pour l'avenir : les 156 000 fiches en base gardent leurs dégâts.
Ce script les répare EN PLACE, sans rien redemander aux sites.

C'est possible parce que les dégâts sont reconnaissables dans le résultat, sans
avoir besoin de la page d'origine. Ce qui ne l'est pas n'est pas touché : mieux
vaut laisser passer un cas douteux que d'abîmer une fiche correcte.

CE QUI EST RÉPARÉ
-----------------
  1. Intertitres pris pour des ingrédients — « Pour la pâte », « Pour la
     sauce », « Pour le glaçage ». is_section_heading() les filtre depuis, mais
     tout ce qui a été moissonné avant les a gardés : l'utilisateur les voit
     arriver dans sa liste de courses comme des articles à acheter.

  2. « bouquet garni » découpé. « bouquet » figure parmi les unités, si bien que
     « 1 bouquet garni » donnait la quantité « 1 bouquet » et l'ingrédient
     « garni ». Plus de 2 000 fiches.

  3. Unités restées collées au nom. CuisineAZ écrit « <quantité> <unité>
     <Nom> » ; les unités que le moissonneur ne connaissait pas — cube,
     rouleau, zeste, noix, gou. — restaient dans le nom : « cube(s) Bouillon de
     volaille », « gou. Ail », « noix Beurre ».

  4. Noms réduits au mot d'une unité — « Gramme(s) » — qui viennent d'un champ
     resté vide côté site et ne désignent aucun ingrédient.

  5. Doubles appellations : « sucre en poudre ou sucre semoule » devient
     « sucre en poudre ». Ce n'est pas un dégât d'analyse, mais deux noms pour
     un produit empêchent la liste de courses de fusionner.

Ces défauts abîment AUSSI la version française : c'est la liste de courses de
tout le monde qui reçoit « garni » et « Pour la pâte ».

USAGE
-----
    # Voir ce qui serait fait, sans rien écrire (comportement par défaut)
    python3 tools/repair-recipes.py --db ~/panier-scrape/panier-scrape.sqlite

    # Réparer pour de bon
    python3 tools/repair-recipes.py --db … --apply

L'écriture se fait par transactions de mille fiches : une coupure de courant au
milieu laisse une base cohérente, et relancer reprend le travail — le script
est sans effet sur ce qui est déjà propre.

L'index de recherche n'a pas à être reconstruit : il ne porte que les titres, et
aucun titre n'est touché. En revanche les traductions déjà en cache pour les
fiches modifiées sont périmées, et le script les efface (voir --keep-cache).

Python 3.7+, bibliothèque standard uniquement.
"""

import argparse
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# On réutilise le vocabulaire du moissonneur plutôt que de le recopier : deux
# listes d'unités qui divergent seraient une source de bogues silencieux.
_spec = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrape-recipes.py")
try:
    import importlib.util

    _s = importlib.util.spec_from_file_location("scrape_recipes", _spec)
    _m = importlib.util.module_from_spec(_s)
    _s.loader.exec_module(_m)
except Exception as e:  # pragma: no cover
    raise SystemExit("scrape-recipes.py illisible (%s) : il est requis." % e)

UNITS = _m.UNITS
UNIT_MAP = _m.UNIT_MAP
UNIT_WORDS = _m.UNIT_WORDS
PROTECTED_COMPOUNDS = _m.PROTECTED_COMPOUNDS
strip_accents = _m.strip_accents
bare_unit_word = _m.bare_unit_word
SECTION_WORDS = _m.SECTION_WORDS

# Les graphies d'unité susceptibles d'être restées collées devant un nom. On ne
# prend que celles d'au moins trois lettres : « g Sucre » existe, mais « c » ou
# « l » devant un nom se confondraient avec une vraie initiale.
GLUED = sorted(
    {k for k, _ in UNITS if len(k) >= 3},
    key=len,
    reverse=True,
)
GLUED_RE = re.compile(
    r"^(%s)(?:\(s\)|\(x\))?\.?\s+(?=\S)"
    % "|".join(re.escape(k) for k in GLUED),
    re.IGNORECASE,
)
CANON = {}
for _k, _v in UNITS:
    CANON.setdefault(strip_accents(_k.lower()).rstrip("."), _v)


def looks_like_heading(name):
    """Intertitre déguisé en ingrédient — la règle de is_section_heading()."""
    n = (name or "").strip()
    if not n:
        return True
    if re.match(r"^pour\b", n, re.IGNORECASE):
        return True
    if strip_accents(n.lower()) in SECTION_WORDS:
        return True
    letters = [c for c in n if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


def repair_ingredient(ing):
    """Rend (ingrédient corrigé ou None si à supprimer, catégorie ou None).

    La catégorie remonte jusqu'à l'appelant plutôt que d'être comptée ici : le
    rapport doit pouvoir montrer QUEL ingrédient a déclenché QUELLE règle, et
    c'est seulement à cet endroit qu'on le sait.
    """
    name = (ing.get("name") or "").strip()
    unit = ing.get("unit")
    qty = ing.get("qty")

    # 2. « bouquet garni » recollé. À faire AVANT le test d'intertitre : seul,
    #    « garni » n'est pas un intertitre, mais il n'est pas un ingrédient non
    #    plus, et on veut le rendre à son bouquet.
    if unit == "bouquet" and strip_accents(name.lower()) in ("garni", "garnis"):
        return {"qty": qty, "unit": None, "name": "bouquet garni"}, "bouquet garni recollé"

    # 1. Intertitres.
    if qty is None and unit is None and looks_like_heading(name):
        return None, "intertitre supprimé"

    # 4. Nom réduit au mot d'une unité.
    if bare_unit_word(name):
        return None, "nom vide (mot d'unité) supprimé"

    # 3. Unité restée collée devant le nom.
    flat = strip_accents(name.lower())
    if not any(flat.startswith(c) for c in PROTECTED_COMPOUNDS):
        m = GLUED_RE.match(name)
        if m:
            rest = name[m.end():].strip()
            if rest and not bare_unit_word(rest):
                key = strip_accents(m.group(1).lower()).rstrip(".")
                # On ne pose l'unité que si le champ est vide : « 2 cl rouleau
                # Pâte » restera en centilitres, ce qui est douteux — mais
                # inventer à la place le serait davantage.
                return {
                    "qty": qty,
                    "unit": unit if unit else CANON.get(key),
                    "name": rest,
                }, "unité décollée du nom"

    # 5. Double appellation.
    m = re.search(r"\s+ou\s+", name, re.IGNORECASE)
    if m and len(name) > 12:
        head = name[: m.start()].strip()
        if len(head) >= 3 and not bare_unit_word(head):
            return {"qty": qty, "unit": unit, "name": head}, "double appellation réduite"

    return ing, None


def repair_recipe(rec, tally, samples, limit):
    """Rend (fiche, True) si quelque chose a changé."""
    ings = rec.get("ingredients")
    if not isinstance(ings, list):
        return rec, False
    out, changed = [], False
    for ing in ings:
        if not isinstance(ing, dict):
            changed = True
            continue
        fixed, cat = repair_ingredient(ing)
        if cat:
            tally[cat] += 1
            if len(samples[cat]) < limit:
                samples[cat].append((ing.get("name"), ing.get("unit"),
                                     fixed["name"] if fixed else None,
                                     fixed["unit"] if fixed else None))
        if fixed is None:
            changed = True
            continue
        if fixed != ing:
            changed = True
        out.append(fixed)
    if not changed:
        return rec, False
    rec = dict(rec)
    rec["ingredients"] = out
    return rec, True


def main():
    p = argparse.ArgumentParser(
        description="Répare les fiches déjà moissonnées, sans rien redemander aux sites.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("USAGE")[-1],
    )
    p.add_argument(
        "--db",
        default=os.path.expanduser("~/panier-scrape/panier-scrape.sqlite"),
        help="base produite par scrape-recipes.py",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="écrit les corrections. Sans ce drapeau, rien n'est modifié.",
    )
    p.add_argument(
        "--examples", type=int, default=12,
        help="nombre d'exemples affichés par catégorie (défaut 12)",
    )
    p.add_argument(
        "--keep-cache", action="store_true",
        help="ne pas effacer les traductions en cache des fiches modifiées",
    )
    args = p.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit("Base introuvable : %s" % args.db)

    mode = "rw" if args.apply else "ro"
    db = sqlite3.connect("file:%s?mode=%s" % (args.db, mode), uri=True, timeout=60)
    if args.apply:
        db.execute("PRAGMA journal_mode=WAL")

    import collections

    tally = collections.Counter()
    samples = collections.defaultdict(list)
    seen = touched = 0
    pending, changed_ids = [], []

    print("Lecture de %s…\n" % args.db, flush=True)
    rows = db.execute(
        "SELECT rowid, recipe FROM urls WHERE status='ok' AND recipe IS NOT NULL"
    ).fetchall()

    for rid, blob in rows:
        try:
            rec = json.loads(blob)
        except (ValueError, TypeError):
            continue
        seen += 1
        fixed, changed = repair_recipe(rec, tally, samples, args.examples)
        if not changed:
            continue
        touched += 1
        changed_ids.append(rid)
        if args.apply:
            pending.append((json.dumps(fixed, ensure_ascii=False), rid))
            if len(pending) >= 1000:
                db.executemany("UPDATE urls SET recipe=? WHERE rowid=?", pending)
                db.commit()
                pending = []
        if seen % 25000 == 0:
            print("  %d fiches lues, %d à corriger…" % (seen, touched), flush=True)

    if args.apply and pending:
        db.executemany("UPDATE urls SET recipe=? WHERE rowid=?", pending)
        db.commit()

    total = sum(tally.values())
    print("\n%d fiches lues, %d à corriger (%.2f %%), %d ingrédients touchés.\n"
          % (seen, touched, 100.0 * touched / max(seen, 1), total))
    def show(nm, un):
        return "%s« %s »" % ("%s " % un if un else "", nm)

    for cat, n in tally.most_common():
        print("  %-34s %7d" % (cat, n))
        for old_n, old_u, new_n, new_u in samples[cat][: args.examples]:
            arrow = "supprimé" if new_n is None else show(new_n, new_u)
            print("       %-42s → %s" % (show(old_n, old_u), arrow))
    print()

    if not args.apply:
        print("Rien n'a été écrit. Pour appliquer :\n"
              "  python3 %s --db %s --apply"
              % (os.path.basename(sys.argv[0]), args.db))
        return

    print("Base mise à jour.")

    # Les traductions en cache des fiches modifiées décrivent l'ancien état :
    # les laisser reviendrait à réparer le français et pas l'anglais.
    cache = args.db + ".tr"
    if args.keep_cache or not os.path.exists(cache) or not changed_ids:
        return
    try:
        tr = sqlite3.connect(cache, timeout=30)
        n = 0
        for i in range(0, len(changed_ids), 400):
            chunk = changed_ids[i : i + 400]
            qs = ",".join("?" * len(chunk))
            n += tr.execute(
                "DELETE FROM recipe_tr WHERE id IN (%s)" % qs, chunk
            ).rowcount
            tr.execute("DELETE FROM title_tr WHERE id IN (%s)" % qs, chunk)
        tr.commit()
        tr.close()
        print("%d traductions périmées effacées du cache (elles seront refaites "
              "au prochain import)." % max(n, 0))
    except sqlite3.Error as e:
        print("Cache de traduction non purgé (%s) — à faire à la main :\n"
              "  sqlite3 %s \"DELETE FROM recipe_tr; DELETE FROM title_tr;\""
              % (e, cache))


if __name__ == "__main__":
    main()
