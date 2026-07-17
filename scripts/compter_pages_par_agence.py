"""
compter_pages_par_agence.py
Pour chaque agence de data/recherche_email_clean_avec_villes.csv (colonne
site_id), retélécharge UNIQUEMENT les zips qui la contiennent (grâce à la
correspondance zip<->site déjà connue via data/resultats_final.csv), liste
le nom de chaque fichier .html qui lui appartient, et produit un comptage
par agence.

Entrées attendues :
  - data/recherche_email_clean_avec_villes.csv (colonne : site_id, ...)
  - data/resultats_final.csv (colonnes : zip, site, ...) -> sert à savoir
    quel zip contient quel site_id, pour éviter de télécharger des zips
    qui ne contiennent aucune agence ciblée.

Écrit :
  - data/pages_html_par_agence.csv   → 1 ligne par fichier .html trouvé
                                        (site_id, nom_fichier, chemin_complet, zip)
  - data/comptage_par_agence.csv     → 1 ligne par agence : site_id, nombre_pages
  - data/progress_pages_par_agence.json → zips déjà traités (reprise automatique)
  - data/zips_bloques.txt            → zips en échec (partagé avec les autres scripts)

Variables d'environnement requises :
  GOOGLE_OAUTH_CLIENT_ID
  GOOGLE_OAUTH_CLIENT_SECRET
  GOOGLE_OAUTH_REFRESH_TOKEN
  GDRIVE_FOLDER_ID
"""

import csv
import io
import json
import os
import zipfile
from collections import Counter, defaultdict

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ─── Config ─────────────────────────────────────────────────────────────────

GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")
DATA_DIR         = "data"

INPUT_AGENCES    = os.path.join(DATA_DIR, "recherche_email_clean_avec_villes.csv")
INPUT_MAPPING    = os.path.join(DATA_DIR, "resultats_final.csv")  # colonnes zip, site

OUTPUT_PAGES     = os.path.join(DATA_DIR, "pages_html_par_agence.csv")
OUTPUT_COMPTAGE  = os.path.join(DATA_DIR, "comptage_par_agence.csv")
PROGRESS_FILE    = os.path.join(DATA_DIR, "progress_pages_par_agence.json")
ZIPS_BLOQUES_FILE = os.path.join(DATA_DIR, "zips_bloques.txt")

MAX_ZIPS_PAR_RUN = 40

CHAMPS_PAGES = ["site_id", "nom_fichier", "chemin_complet", "zip"]

# ─── Google Drive ───────────────────────────────────────────────────────────

def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        # scopes volontairement omis (cf. extraire_infos.py)
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def lister_zips_drive(service):
    """Retourne {nom_zip: file_id} pour tous les zips du dossier Drive."""
    resultats, page_token = {}, None
    while True:
        resp = service.files().list(
            q=f"'{GDRIVE_FOLDER_ID}' in parents and name contains '.zip' and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
            pageSize=1000,
        ).execute()
        for f in resp.get("files", []):
            resultats[f["name"]] = f["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return resultats


def telecharger_zip(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()

# ─── Chargement des entrées ─────────────────────────────────────────────────

def charger_site_ids_cibles() -> set:
    with open(INPUT_AGENCES, encoding="utf-8") as f:
        return {r["site_id"].strip() for r in csv.DictReader(f) if r["site_id"].strip()}


def charger_mapping_zip_par_site() -> dict:
    """Renvoie {zip: set(site_id présents dans ce zip)} à partir de resultats_final.csv."""
    mapping = defaultdict(set)
    with open(INPUT_MAPPING, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            mapping[r["zip"]].add(r["site"].strip())
    return mapping

# ─── Progress ───────────────────────────────────────────────────────────────

def charger_progress() -> set:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("traites", []))
    return set()


def sauver_progress(traites: set):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"traites": list(traites)}, f)


def logger_zip_bloque(nom_zip: str, raison: str):
    with open(ZIPS_BLOQUES_FILE, "a", encoding="utf-8") as f:
        f.write(f"{nom_zip}\t{raison}\n")

# ─── Recalcul du comptage final ─────────────────────────────────────────────

def regenerer_comptage(site_ids_cibles: set):
    """Relit pages_html_par_agence.csv en entier et régénère le comptage par agence
    (inclut les agences à 0 page trouvée jusqu'ici)."""
    compte = Counter()
    if os.path.exists(OUTPUT_PAGES):
        with open(OUTPUT_PAGES, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                compte[r["site_id"]] += 1

    with open(OUTPUT_COMPTAGE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["site_id", "nombre_pages"])
        for sid in sorted(site_ids_cibles):
            writer.writerow([sid, compte.get(sid, 0)])

# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    site_ids_cibles = charger_site_ids_cibles()
    mapping_zip = charger_mapping_zip_par_site()

    # ne garder que les zips qui contiennent au moins 1 site_id ciblé
    zips_utiles = sorted(
        nom_zip for nom_zip, sites in mapping_zip.items()
        if sites & site_ids_cibles
    )

    print(f"Agences ciblées : {len(site_ids_cibles)}")
    print(f"Zips contenant au moins 1 agence ciblée : {len(zips_utiles)} / {len(mapping_zip)}\n")

    traites = charger_progress()
    reprise = len(traites) > 0

    zips_disponibles = lister_zips_drive(get_drive_service())
    drive = get_drive_service()

    restants = [z for z in zips_utiles if z not in traites]
    lot = restants[:MAX_ZIPS_PAR_RUN]
    apres_ce_run = len(restants) - len(lot)

    print(f"{'▶️  Reprise' if reprise else '🚀 Démarrage'} — {len(traites)} zips déjà traités | "
          f"{len(restants)} restants | {len(lot)} traités dans ce run\n")

    if not lot:
        print("✅ Tous les zips utiles ont déjà été traités.")
        regenerer_comptage(site_ids_cibles)
        github_output = os.getenv("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as f:
                f.write("restants_apres=0\n")
        return

    mode = "a" if os.path.exists(OUTPUT_PAGES) else "w"
    with open(OUTPUT_PAGES, mode, newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=CHAMPS_PAGES)
        if mode == "w":
            writer.writeheader()

        for i, nom_zip in enumerate(lot, 1):
            print(f"📦 [{i}/{len(lot)}] {nom_zip}", end=" ... ", flush=True)

            file_id = zips_disponibles.get(nom_zip)
            if not file_id:
                print("❌ introuvable sur Drive (renommé/supprimé ?)")
                logger_zip_bloque(nom_zip, "introuvable sur Drive")
                traites.add(nom_zip)
                sauver_progress(traites)
                continue

            try:
                contenu = telecharger_zip(drive, file_id)
            except Exception as e:
                print(f"❌ téléchargement échoué : {e}")
                logger_zip_bloque(nom_zip, f"téléchargement échoué: {e}")
                traites.add(nom_zip)
                sauver_progress(traites)
                continue

            try:
                nb_trouvees = 0
                with zipfile.ZipFile(io.BytesIO(contenu)) as zf:
                    for chemin in zf.namelist():
                        if not chemin.endswith(".html"):
                            continue
                        parties = chemin.split("/")
                        if len(parties) < 2:
                            continue
                        site_id = parties[0]
                        if site_id not in site_ids_cibles:
                            continue
                        writer.writerow({
                            "site_id": site_id,
                            "nom_fichier": parties[-1],
                            "chemin_complet": chemin,
                            "zip": nom_zip,
                        })
                        nb_trouvees += 1
                f_out.flush()
                print(f"✅ {nb_trouvees} pages pour les agences ciblées")
            except Exception as e:
                print(f"❌ lecture échouée : {e}")
                logger_zip_bloque(nom_zip, f"lecture échouée: {e}")
                traites.add(nom_zip)
                sauver_progress(traites)
                continue

            traites.add(nom_zip)
            sauver_progress(traites)

    regenerer_comptage(site_ids_cibles)

    print(f"\n─── TERMINÉ (ce run) ───────────────────────")
    print(f"Zips traités ce run : {len(lot)}")
    print(f"Zips restants       : {apres_ce_run}")
    print(f"Résultats           : {OUTPUT_PAGES}")
    print(f"Comptage par agence : {OUTPUT_COMPTAGE}")

    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"restants_apres={apres_ce_run}\n")


if __name__ == "__main__":
    main()
