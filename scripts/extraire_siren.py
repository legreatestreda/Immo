"""
extraire_siren.py
À partir de data/pages_html_par_agence.csv, repère les pages "mentions
légales" (nom de fichier contenant mention/legal), retélécharge UNIQUEMENT
les zips concernés, extrait le contenu de ces fichiers précis (pas tout le
zip) et en extrait le SIREN par regex.

Entrée attendue :
  - data/pages_html_par_agence.csv (colonnes : site_id, nom_fichier,
    chemin_complet, zip)

Écrit :
  - data/sirens_par_agence.csv       → site_id, siren, source_fichier, zip
  - data/progress_sirens.json        → zips déjà traités (reprise automatique)
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
import re
import zipfile
from collections import defaultdict

from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ─── Config ─────────────────────────────────────────────────────────────────

GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")
DATA_DIR         = "data"

INPUT_PAGES      = os.path.join(DATA_DIR, "pages_html_par_agence.csv")
OUTPUT_CSV       = os.path.join(DATA_DIR, "sirens_par_agence.csv")
PROGRESS_FILE    = os.path.join(DATA_DIR, "progress_sirens.json")
ZIPS_BLOQUES_FILE = os.path.join(DATA_DIR, "zips_bloques.txt")

MAX_ZIPS_PAR_RUN = 40

MOTIF_MENTIONS = re.compile(r"mention|legal", re.IGNORECASE)

CHAMPS_CSV = ["site_id", "siren", "source_fichier", "zip"]

# ─── Regex SIREN ─────────────────────────────────────────────────────────────

SIREN_RE = re.compile(r"SIREN\s*:?\s*(\d{3}\s?\d{3}\s?\d{3})\b", re.IGNORECASE)
SIRET_RE = re.compile(r"SIRET\s*:?\s*(\d{3}\s?\d{3}\s?\d{3}\s?\d{5})\b", re.IGNORECASE)
RCS_RE   = re.compile(r"RCS\s+[A-ZÀ-Üa-zà-ü\-\s]{2,25}?(\d{3}\s?\d{3}\s?\d{3})\b", re.IGNORECASE)

# formulation en toutes lettres, ex: "immatriculée au Registre du Commerce et
# des Sociétés de Paris sous le numéro 844 296 434"
RCS_LONG_RE = re.compile(
    r"Registre du Commerce et des Soci[ée]t[ée]s[^\d]{0,80}?(\d{3}\s?\d{3}\s?\d{3})\b",
    re.IGNORECASE | re.DOTALL,
)

# fallback générique : "immatriculée ... sous le numéro NNN NNN NNN"
IMMATRICULATION_RE = re.compile(
    r"immatricul\w*[^\d]{0,80}?(?:num[ée]ro|n[°o])[^\d]{0,10}?(\d{3}\s?\d{3}\s?\d{3})\b",
    re.IGNORECASE | re.DOTALL,
)


def extraire_siren(texte: str) -> str:
    """Cherche un SIREN par priorité : SIREN explicite > SIRET (9 premiers
    chiffres) > RCS (abrégé) > 'Registre du Commerce...' en toutes lettres >
    'immatriculée ... numéro'. Renvoie 9 chiffres sans espace, ou chaîne
    vide si rien trouvé."""
    for pattern, group_to_siren in (
        (SIREN_RE, lambda m: m.group(1).replace(" ", "")),
        (SIRET_RE, lambda m: m.group(1).replace(" ", "")[:9]),
        (RCS_RE, lambda m: m.group(1).replace(" ", "")),
        (RCS_LONG_RE, lambda m: m.group(1).replace(" ", "")),
        (IMMATRICULATION_RE, lambda m: m.group(1).replace(" ", "")),
    ):
        m = pattern.search(texte)
        if m:
            return group_to_siren(m)

    return ""

# ─── HTML → texte ───────────────────────────────────────────────────────────

def html_to_text(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "meta", "link", "noscript"]):
            tag.decompose()
        return " ".join(soup.get_text(separator=" ").split())
    except Exception:
        return ""

# ─── Google Drive ───────────────────────────────────────────────────────────

def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def lister_zips_drive(service):
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

# ─── Chargement des cibles ───────────────────────────────────────────────────

def charger_fichiers_mentions_legales() -> dict:
    """Renvoie {zip: [(site_id, chemin_complet), ...]} pour les pages qui
    ressemblent à des mentions légales."""
    par_zip = defaultdict(list)
    with open(INPUT_PAGES, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if MOTIF_MENTIONS.search(r["nom_fichier"]):
                par_zip[r["zip"]].append((r["site_id"], r["chemin_complet"]))
    return par_zip

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

# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    fichiers_par_zip = charger_fichiers_mentions_legales()
    print(f"Zips contenant des pages mentions légales : {len(fichiers_par_zip)}")
    print(f"Total de pages mentions légales à traiter : {sum(len(v) for v in fichiers_par_zip.values())}\n")

    traites = charger_progress()
    reprise = len(traites) > 0

    zips_utiles = sorted(fichiers_par_zip.keys())
    restants = [z for z in zips_utiles if z not in traites]
    lot = restants[:MAX_ZIPS_PAR_RUN]
    apres_ce_run = len(restants) - len(lot)

    print(f"{'▶️  Reprise' if reprise else '🚀 Démarrage'} — {len(traites)} zips déjà traités | "
          f"{len(restants)} restants | {len(lot)} traités dans ce run\n")

    if not lot:
        print("✅ Tous les zips utiles ont déjà été traités.")
        github_output = os.getenv("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as f:
                f.write("restants_apres=0\n")
        return

    drive = get_drive_service()
    zips_disponibles = lister_zips_drive(drive)

    mode = "a" if os.path.exists(OUTPUT_CSV) else "w"
    with open(OUTPUT_CSV, mode, newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=CHAMPS_CSV)
        if mode == "w":
            writer.writeheader()

        for i, nom_zip in enumerate(lot, 1):
            cibles = fichiers_par_zip[nom_zip]
            print(f"📦 [{i}/{len(lot)}] {nom_zip} ({len(cibles)} pages à lire)", end=" ... ", flush=True)

            file_id = zips_disponibles.get(nom_zip)
            if not file_id:
                print("❌ introuvable sur Drive")
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
                nb_trouves = 0
                with zipfile.ZipFile(io.BytesIO(contenu)) as zf:
                    noms_dans_zip = set(zf.namelist())
                    for site_id, chemin in cibles:
                        if chemin not in noms_dans_zip:
                            continue
                        html = zf.read(chemin).decode("utf-8", errors="ignore")
                        texte = html_to_text(html)
                        siren = extraire_siren(texte)
                        if siren:
                            nb_trouves += 1
                        writer.writerow({
                            "site_id": site_id,
                            "siren": siren,
                            "source_fichier": chemin,
                            "zip": nom_zip,
                        })
                f_out.flush()
                print(f"✅ {nb_trouves}/{len(cibles)} SIREN trouvés")
            except Exception as e:
                print(f"❌ lecture échouée : {e}")
                logger_zip_bloque(nom_zip, f"lecture échouée: {e}")
                traites.add(nom_zip)
                sauver_progress(traites)
                continue

            traites.add(nom_zip)
            sauver_progress(traites)

    print(f"\n─── TERMINÉ (ce run) ───────────────────────")
    print(f"Zips traités ce run : {len(lot)}")
    print(f"Zips restants       : {apres_ce_run}")
    print(f"Résultats           : {OUTPUT_CSV}")

    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"restants_apres={apres_ce_run}\n")


if __name__ == "__main__":
    main()
