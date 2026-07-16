"""
nettoyer_faux_echecs.py
À lancer UNE FOIS pour réparer les dégâts causés par le bug de détection
de quota (avant le correctif) : retire du progress les lignes qui avaient
été marquées "traitées" à tort à cause d'un "Not enough credits", pour
qu'elles soient retentées au prochain run.
"""

import json
import os

DATA_DIR = "data"
PROGRESS_FILE = os.path.join(DATA_DIR, "progress_recherche_email.json")
EMAILS_BLOQUES_FILE = os.path.join(DATA_DIR, "emails_bloques.txt")


def main():
    with open(PROGRESS_FILE, encoding="utf-8") as f:
        traites = set(json.load(f).get("traites", []))

    a_retirer = set()
    lignes_gardees = []

    if os.path.exists(EMAILS_BLOQUES_FILE):
        with open(EMAILS_BLOQUES_FILE, encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.rstrip("\n")
                if not ligne:
                    continue
                if "credit" in ligne.lower() or "Not enough credits" in ligne:
                    uid = ligne.split("\t")[0]
                    a_retirer.add(uid)
                else:
                    lignes_gardees.append(ligne)

    avant = len(traites)
    traites -= a_retirer
    print(f"Lignes retirées du progress (faux échecs crédit) : {len(a_retirer)}")
    print(f"Progress avant : {avant} -> après : {len(traites)}")

    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"traites": list(traites)}, f)

    with open(EMAILS_BLOQUES_FILE, "w", encoding="utf-8") as f:
        for ligne in lignes_gardees:
            f.write(ligne + "\n")

    print("Terminé. Ces agences seront retraitées au prochain run.")


if __name__ == "__main__":
    main()
