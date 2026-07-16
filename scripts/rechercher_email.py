"""
rechercher_email.py
Pour chaque agence du CSV (1 email par agence), interroge Google via Serper
avec l'email brut comme requête (pas de site: ni de filtre). Sauvegarde le
JSON brut retourné, sans parsing — l'extraction du nom d'agence se fera
dans une étape séparée.

Écrit :
  - data/recherche_email_raw.jsonl   → 1 ligne JSON par agence (résultat brut Serper)
  - data/progress_recherche_email.json → agences déjà traitées (reprise automatique)
  - data/emails_bloques.txt          → agences en échec (toutes les clés épuisées / erreur)

Variables d'environnement requises :
  SERPER_API_KEYS   → une ou plusieurs clés séparées par des virgules
                       ex: "cle1,cle2,cle3"

Entrée attendue : data/resultats_final.csv (colonnes : zip,site,email,nom_gerant,
nb_annonces,taille_equipe,crm_detecte)
"""

import csv
import http.client
import json
import os

# ─── Config ─────────────────────────────────────────────────────────────────

DATA_DIR         = "data"
INPUT_CSV        = os.path.join(DATA_DIR, "resultats_final.csv")
OUTPUT_JSONL     = os.path.join(DATA_DIR, "recherche_email_raw.jsonl")
PROGRESS_FILE    = os.path.join(DATA_DIR, "progress_recherche_email.json")
EMAILS_BLOQUES_FILE = os.path.join(DATA_DIR, "emails_bloques.txt")

MAX_PAR_RUN = 300  # nombre d'agences traitées par run avant arrêt propre

# ─── Clés API (rotation) ────────────────────────────────────────────────────

def charger_cles_api() -> list:
    brut = os.environ.get("SERPER_API_KEYS", "")
    cles = [c.strip() for c in brut.split(",") if c.strip()]
    if not cles:
        raise RuntimeError("Aucune clé API trouvée dans SERPER_API_KEYS")
    return cles


def appeler_serper(query: str, cles: list, index_cle: int):
    """Interroge Serper avec la clé courante. Si quota épuisé (403/429),
    passe à la clé suivante et réessaie. Renvoie (json_ou_None, nouvel_index_cle)."""
    tentatives = 0
    while tentatives < len(cles):
        cle = cles[index_cle]
        try:
            conn = http.client.HTTPSConnection("google.serper.dev", timeout=30)
            payload = json.dumps({"q": query, "gl": "fr", "hl": "fr"})
            headers = {
                "X-API-KEY": cle,
                "Content-Type": "application/json",
            }
            conn.request("POST", "/search", payload, headers)
            res = conn.getresponse()
            corps = res.read()
            conn.close()

            if res.status == 200:
                return json.loads(corps.decode("utf-8")), index_cle

            if res.status in (403, 429):
                # quota épuisé ou clé invalide -> clé suivante
                print(f"      ⚠️  Clé #{index_cle + 1} épuisée/refusée (HTTP {res.status}), rotation...")
                index_cle = (index_cle + 1) % len(cles)
                tentatives += 1
                continue

            # autre erreur HTTP -> pas la peine de retenter avec une autre clé
            print(f"      ❌ Erreur HTTP {res.status} : {corps.decode('utf-8', errors='ignore')[:200]}")
            return None, index_cle

        except Exception as e:
            print(f"      ❌ Erreur réseau : {e}")
            index_cle = (index_cle + 1) % len(cles)
            tentatives += 1
            continue

    print("      ❌ Toutes les clés API sont épuisées ou invalides.")
    return None, index_cle

# ─── Progress ───────────────────────────────────────────────────────────────

def charger_progress() -> set:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("traites", []))
    return set()


def sauver_progress(traites: set):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"traites": list(traites)}, f)


def logger_email_bloque(id_ligne: str, raison: str):
    with open(EMAILS_BLOQUES_FILE, "a", encoding="utf-8") as f:
        f.write(f"{id_ligne}\t{raison}\n")

# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    cles = charger_cles_api()
    index_cle = 0

    traites = charger_progress()
    reprise = len(traites) > 0

    with open(INPUT_CSV, encoding="utf-8") as f:
        toutes_lignes = list(csv.DictReader(f))

    def id_ligne(r):
        return f"{r['zip']}::{r['site']}"

    restantes = [r for r in toutes_lignes if id_ligne(r) not in traites]
    lot = restantes[:MAX_PAR_RUN]
    apres_ce_run = len(restantes) - len(lot)

    print(f"{'▶️  Reprise' if reprise else '🚀 Démarrage'} — {len(toutes_lignes)} agences au total | "
          f"{len(traites)} déjà traitées | {len(restantes)} restantes | "
          f"{len(lot)} traitées dans ce run\n")

    if not lot:
        print("✅ Toutes les agences ont déjà été traitées.")
        github_output = os.getenv("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as f:
                f.write("restants_apres=0\n")
        return

    with open(OUTPUT_JSONL, "a", encoding="utf-8") as f_out:
        for i, ligne in enumerate(lot, 1):
            uid = id_ligne(ligne)
            email = ligne["email"].split(",")[0].strip()

            print(f"[{i}/{len(lot)}] {email}", end=" ... ", flush=True)

            if not email:
                logger_email_bloque(uid, "pas d'email")
                traites.add(uid)
                sauver_progress(traites)
                print("⏭️  pas d'email, ignoré")
                continue

            resultat, index_cle = appeler_serper(email, cles, index_cle)

            if resultat is None:
                logger_email_bloque(uid, "requête échouée (voir logs)")
                traites.add(uid)
                sauver_progress(traites)
                print("❌")
                continue

            enregistrement = {
                "zip": ligne["zip"],
                "site": ligne["site"],
                "email_utilise": email,
                "resultat_serper": resultat,
            }
            f_out.write(json.dumps(enregistrement, ensure_ascii=False) + "\n")
            f_out.flush()

            traites.add(uid)
            sauver_progress(traites)
            print("✅")

    print(f"\n─── TERMINÉ (ce run) ───────────────────────")
    print(f"Agences traitées ce run : {len(lot)}")
    print(f"Agences restantes       : {apres_ce_run}")
    print(f"Résultats                : {OUTPUT_JSONL}")

    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"restants_apres={apres_ce_run}\n")


if __name__ == "__main__":
    main()
