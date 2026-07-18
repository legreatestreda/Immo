"""
enrichir_siren.py
Interroge l'API gratuite recherche-entreprises.api.gouv.fr pour chaque SIREN
du CSV, et récupère : dénomination officielle, état administratif, dirigeant
déclaré, activité principale.

Entrée  : data/sirens_pour_verif.csv (colonnes : nom_agence,email,site_web,siren)
Sortie  : data/sirens_enrichis.csv (mêmes colonnes + denomination,etat,dirigeant,activite)

Reprise automatique : une ligne est considérée traitée si sa colonne
'activite' est déjà remplie (y compris "" volontaire — voir NON_TROUVE ci-dessous).
"""

import csv
import os
import time
import urllib.request
import urllib.error
import json

DATA_DIR      = "data"
INPUT_CSV     = os.path.join(DATA_DIR, "sirens_pour_verif.csv")
OUTPUT_CSV    = os.path.join(DATA_DIR, "sirens_enrichis.csv")

MAX_PAR_RUN   = 3000       # large marge sous le timeout, l'API gouv est rapide et gratuite
DELAI_ENTRE_APPELS = 0.15  # délai de courtoisie entre deux appels API

CHAMPS_SORTIE = ["nom_agence", "email", "site_web", "siren",
                  "denomination", "etat", "dirigeant", "activite"]

MARQUEUR_NON_TROUVE = "NON_TROUVE"  # utilisé quand l'API ne renvoie rien, pour ne pas retenter en boucle


def get_datagouv_info(siren: str):
    """Interroge l'API. Renvoie (denomination, etat, dirigeant, activite)."""
    url = f"https://recherche-entreprises.api.gouv.fr/search?q={siren}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"      ❌ Erreur réseau : {e}")
        return None

    results = data.get("results", [])
    if not results:
        return "", "", "", MARQUEUR_NON_TROUVE

    res = results[0]
    denomination = res.get("nom_complet", "")
    etat = res.get("etat_administratif", "")
    activite = res.get("activite_principale", "") or MARQUEUR_NON_TROUVE

    dirigeants = res.get("dirigeants", [])
    dirigeant_name = ""
    if dirigeants:
        d = dirigeants[0]
        nom = d.get("nom", "")
        prenom = d.get("prenoms", "")
        dirigeant_name = f"{prenom} {nom}".strip()

    return denomination, etat, dirigeant_name, activite


def charger_lignes_existantes() -> dict:
    """Charge le CSV de sortie s'il existe déjà (reprise). Clé = siren."""
    if not os.path.exists(OUTPUT_CSV):
        return {}
    with open(OUTPUT_CSV, encoding="utf-8") as f:
        return {r["siren"]: r for r in csv.DictReader(f)}


def sauver_toutes_les_lignes(lignes: dict):
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CHAMPS_SORTIE)
        writer.writeheader()
        for r in lignes.values():
            writer.writerow(r)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    with open(INPUT_CSV, encoding="utf-8") as f:
        lignes_source = list(csv.DictReader(f))

    lignes_resultat = charger_lignes_existantes()
    reprise = len(lignes_resultat) > 0

    a_traiter = [r for r in lignes_source if r["siren"] not in lignes_resultat
                 or not lignes_resultat[r["siren"]].get("activite", "").strip()]

    lot = a_traiter[:MAX_PAR_RUN]
    apres_ce_run = len(a_traiter) - len(lot)

    print(f"{'▶️  Reprise' if reprise else '🚀 Démarrage'} — {len(lignes_source)} SIREN au total | "
          f"{len(a_traiter)} restants | {len(lot)} traités dans ce run\n")

    if not lot:
        print("✅ Tous les SIREN ont déjà été traités.")
        github_output = os.getenv("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as f:
                f.write("restants_apres=0\n")
        return

    for i, ligne in enumerate(lot, 1):
        siren = str(ligne["siren"]).strip().zfill(9)
        print(f"[{i}/{len(lot)}] {siren} ({ligne.get('nom_agence', '')[:40]})", end=" ... ", flush=True)

        info = get_datagouv_info(siren)

        if info is None:
            # échec réseau : on ne marque PAS traité, on retentera au prochain run
            print("⏸️  erreur réseau, retenté plus tard")
            time.sleep(DELAI_ENTRE_APPELS)
            continue

        denom, etat, dirigeant, activite = info
        lignes_resultat[siren] = {
            "nom_agence": ligne.get("nom_agence", ""),
            "email": ligne.get("email", ""),
            "site_web": ligne.get("site_web", ""),
            "siren": siren,
            "denomination": denom,
            "etat": etat,
            "dirigeant": dirigeant,
            "activite": activite,
        }

        if i % 50 == 0:
            sauver_toutes_les_lignes(lignes_resultat)
            print(f"\n   💾 sauvegarde intermédiaire ({i}/{len(lot)})")

        etat_affiche = "✅" if dirigeant else ("➖ trouvé, pas de dirigeant" if activite != MARQUEUR_NON_TROUVE else "❓ non trouvé")
        print(etat_affiche)

        time.sleep(DELAI_ENTRE_APPELS)

    sauver_toutes_les_lignes(lignes_resultat)

    print(f"\n─── TERMINÉ (ce run) ───────────────────────")
    print(f"SIREN traités ce run : {len(lot)}")
    print(f"SIREN restants       : {apres_ce_run}")
    print(f"Résultats             : {OUTPUT_CSV}")

    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"restants_apres={apres_ce_run}\n")


if __name__ == "__main__":
    main()
