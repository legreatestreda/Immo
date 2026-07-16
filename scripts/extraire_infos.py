"""
extraire_infos.py
Télécharge les zips depuis Google Drive, lit le HTML, extrait email / CRM /
nb_annonces / taille_equipe via regex (pas d'IA nécessaire).

Écrit :
  - data/resultats.csv       → résultats cumulés
  - data/progress.json       → zips déjà traités (reprise automatique)

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

from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ─── Config ─────────────────────────────────────────────────────────────────

GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")
DATA_DIR         = "data"
OUTPUT_CSV       = os.path.join(DATA_DIR, "resultats.csv")
PROGRESS_FILE    = os.path.join(DATA_DIR, "progress.json")
ZIPS_BLOQUES_FILE = os.path.join(DATA_DIR, "zips_bloques.txt")

# Nombre max de zips traités par run — le run s'arrête proprement une fois
# cette limite atteinte, et le prochain cron reprendra là où on s'est arrêté.
MAX_ZIPS_PAR_RUN = 40

CHAMPS_CSV = ["zip", "site", "email", "nom_gerant", "nb_annonces", "taille_equipe", "crm_detecte"]

# ─── Google Drive ───────────────────────────────────────────────────────────

def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        # scopes volontairement omis : le forcer ici peut provoquer une
        # erreur "invalid_scope" au refresh s'il ne correspond pas EXACTEMENT
        # au scope réellement accordé lors de la création du refresh_token.
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def lister_zips(service):
    resultats, page_token = [], None
    while True:
        resp = service.files().list(
            q=f"'{GDRIVE_FOLDER_ID}' in parents and name contains '.zip' and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
            pageSize=1000,
        ).execute()
        resultats.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return sorted(resultats, key=lambda f: f["name"])


def telecharger_zip(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()

# ─── HTML → texte ───────────────────────────────────────────────────────────

def html_to_text(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "meta", "link", "noscript"]):
            tag.decompose()
        return " ".join(soup.get_text(separator=" ").split())
    except Exception:
        return ""

# ─── Extraction par regex ───────────────────────────────────────────────────

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

EMAIL_BLOCKLIST_DOMAINS = (
    "sentry.io", "wixpress.com", "example.com", "godaddy.com",
    "schema.org", "w3.org",
)
EMAIL_BLOCKLIST_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

CRM_CONNUS = [
    "Apimo", "Netty", "Immoweb", "Ubiflow", "Hektor", "Perfect Immo",
    "AC3D", "Crypto Immo", "Immoblog", "Kayak Immo", "Booster Immo",
    "IMMOFACILE", "Adapt Immo", "Praxidoo", "Sequoiasoft",
    "Zimmo", "IZYMO", "Simply", "Iad", "Century21", "Guy Hoquet",
]

NB_ANNONCES_RE = re.compile(
    r"(\d{1,5})\s*(?:annonces?|biens?(?:\s+à\s+vendre)?|propriétés)", re.IGNORECASE,
)
TAILLE_EQUIPE_RE = re.compile(
    r"(?:équipe|agence)\s+de\s+(\d{1,3})\s*(?:personnes?|collaborateurs?|agents?)"
    r"|(\d{1,3})\s*(?:collaborateurs?|agents?|négociateurs?)",
    re.IGNORECASE,
)


def extraire_emails(texte: str) -> str:
    trouves = []
    for match in EMAIL_RE.findall(texte):
        low = match.lower()
        if low.endswith(EMAIL_BLOCKLIST_EXT) or any(d in low for d in EMAIL_BLOCKLIST_DOMAINS):
            continue
        if match not in trouves:  # évite les doublons si le même email apparaît plusieurs fois
            trouves.append(match)
    return ", ".join(trouves)


def extraire_crm(texte: str) -> str:
    low = texte.lower()
    for crm in CRM_CONNUS:
        if crm.lower() in low:
            return crm
    return ""


def extraire_nb_annonces(texte: str) -> str:
    m = NB_ANNONCES_RE.search(texte)
    return m.group(1) if m else ""


def extraire_taille_equipe(texte: str) -> str:
    m = TAILLE_EQUIPE_RE.search(texte)
    return (m.group(1) or m.group(2)) if m else ""


def extraire_infos(texte: str) -> dict:
    return {
        "email":         extraire_emails(texte),
        "nom_gerant":    "",  # non fiable en regex — nécessite une passe IA ciblée
        "nb_annonces":   extraire_nb_annonces(texte),
        "taille_equipe": extraire_taille_equipe(texte),
        "crm_detecte":   extraire_crm(texte),
    }

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
    traites = charger_progress()
    reprise = len(traites) > 0

    drive = get_drive_service()
    zips  = lister_zips(drive)
    tous_restants = [z for z in zips if z["name"] not in traites]
    zips_restants = tous_restants[:MAX_ZIPS_PAR_RUN]
    apres_ce_run  = len(tous_restants) - len(zips_restants)

    print(f"{'▶️  Reprise' if reprise else '🚀 Démarrage'} — {len(zips)} zips au total | "
          f"{len(traites)} déjà traités | {len(tous_restants)} restants | "
          f"{len(zips_restants)} traités dans ce run\n")

    if not zips_restants:
        print("✅ Tous les zips ont déjà été traités.")
        github_output = os.getenv("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as f:
                f.write("restants_apres=0\n")
        return

    mode = "a" if reprise else "w"
    with open(OUTPUT_CSV, mode, newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=CHAMPS_CSV)
        if not reprise:
            writer.writeheader()

        total_sites = 0
        for i, fichier in enumerate(zips_restants, 1):
            nom_zip = fichier["name"]
            print(f"\n📦 [{i}/{len(zips_restants)}] {nom_zip}", flush=True)

            try:
                contenu = telecharger_zip(drive, fichier["id"])
            except Exception as e:
                print(f"   ❌ Téléchargement échoué : {e}")
                logger_zip_bloque(nom_zip, f"téléchargement échoué: {e}")
                traites.add(nom_zip)  # évite de le retenter à chaque run
                sauver_progress(traites)
                continue

            sites: dict[str, str] = {}
            try:
                with zipfile.ZipFile(io.BytesIO(contenu)) as zf:
                    for chemin in zf.namelist():
                        parties = chemin.split("/")
                        if len(parties) >= 2 and chemin.endswith(".html") and parties[0]:
                            site_id = parties[0]
                            html    = zf.read(chemin).decode("utf-8", errors="ignore")
                            sites[site_id] = sites.get(site_id, "") + " " + html_to_text(html)
            except Exception as e:
                print(f"   ❌ Lecture zip échouée : {e}")
                logger_zip_bloque(nom_zip, f"lecture échouée: {e}")
                traites.add(nom_zip)  # évite de le retenter à chaque run
                sauver_progress(traites)
                continue

            for site_id, texte_complet in sites.items():
                total_sites += 1
                print(f"   [{total_sites}] {site_id[:50]}", end=" ... ", flush=True)

                infos = extraire_infos(texte_complet)
                writer.writerow({"zip": nom_zip, "site": site_id, **infos})
                f_out.flush()

                print(f"✅  email={infos['email'] or '-'}  crm={infos['crm_detecte'] or '-'}")

            traites.add(nom_zip)
            sauver_progress(traites)

    print(f"\n─── TERMINÉ (ce run) ───────────────────────")
    print(f"Zips traités ce run : {len(zips_restants)}")
    print(f"Sites traités       : {total_sites}")
    print(f"Zips restants       : {apres_ce_run}")
    print(f"Résultats           : {OUTPUT_CSV}")

    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"restants_apres={apres_ce_run}\n")


if __name__ == "__main__":
    main()
