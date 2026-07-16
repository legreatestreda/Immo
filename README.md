# Extraction infos agences immobilières

Récupère email / CRM détecté / nb d'annonces / taille d'équipe à partir de pages
HTML d'agences immobilières stockées en zip sur Google Drive. Fonctionne sans IA
(extraction par regex) — contrairement à l'ancienne version qui utilisait Fireworks AI.

## Structure

```
.
├── .github/workflows/extraction.yml   → tourne toutes les 5h sur GitHub Actions
├── scripts/extraire_infos.py          → script principal (Drive → HTML → regex → CSV)
├── data/resultats.csv                 → résultats cumulés (généré automatiquement)
├── data/progress.json                 → zips déjà traités, pour la reprise auto
└── requirements.txt
```

## Secrets GitHub à configurer

Dans Settings → Secrets and variables → Actions :

- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`
- `GOOGLE_OAUTH_REFRESH_TOKEN`
- `GDRIVE_FOLDER_ID`

(Plus besoin de `FIREWORKS_API_KEY` — l'extraction ne passe plus par une IA.)

## Lancer manuellement

Sur GitHub : onglet **Actions** → "Extraction Infos Agences" → **Run workflow**.

En local :
```bash
pip install -r requirements.txt
export GOOGLE_OAUTH_CLIENT_ID=...
export GOOGLE_OAUTH_CLIENT_SECRET=...
export GOOGLE_OAUTH_REFRESH_TOKEN=...
export GDRIVE_FOLDER_ID=...
python scripts/extraire_infos.py
```

## Limites connues

- `nom_gerant` reste vide : pas de regex fiable pour extraire un nom propre en texte
  libre. Nécessiterait une passe IA ciblée, mais seulement sur les lignes où le champ
  est vide (coûte beaucoup moins cher qu'un run complet).
- `nb_annonces` / `taille_equipe` : dépendent de formulations standards ("X annonces",
  "équipe de X personnes"). Les sites avec une formulation inhabituelle ne seront pas
  détectés.
