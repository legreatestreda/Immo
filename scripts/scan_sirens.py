#!/usr/bin/env python3
"""
Scan a CSV of real estate agencies, search "<nom_agence> immobilier societe.com"
via Tavily (primary, 3 accounts) then Serper (fallback, 2 accounts), and save
raw search results. SIREN extraction is a separate later step -- this script
only collects raw results.

Resumable: progress is tracked in data/progress.json (last_index processed).
Runs batches of BATCH_SIZE rows, committing + pushing after every batch, and
keeps going immediately into the next batch (no fixed wait) until either:
  - the whole CSV is processed, or
  - every available API key across every provider is exhausted/failing,
    in which case the script exits cleanly and a scheduled workflow run
    (safety net) will resume it later (e.g. after daily quota resets).
"""

import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "data" / "recherche_email_clean_avec_villes.csv"
PROGRESS_PATH = REPO_ROOT / "data" / "progress.json"
RESULTS_PATH = REPO_ROOT / "data" / "results.jsonl"

BATCH_SIZE = 100
QUERY_TEMPLATE = "{nom_agence} immobilier societe.com"
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_QUERIES = 0.3  # be polite to APIs

# Keys are passed as comma-separated env vars (GitHub Secrets), e.g.:
#   TAVILY_KEYS="tvly-dev-aaa,tvly-dev-bbb,tvly-dev-ccc"
#   SERPER_KEYS="serperkey1,serperkey2"
TAVILY_KEYS = [k.strip() for k in os.environ.get("TAVILY_KEYS", "").split(",") if k.strip()]
SERPER_KEYS = [k.strip() for k in os.environ.get("SERPER_KEYS", "").split(",") if k.strip()]

TAVILY_EXHAUSTED_STATUS = {432, 433, 401, 403, 429}
SERPER_EXHAUSTED_STATUS = {401, 403, 429}


# ---------------------------------------------------------------------------
# Key rotation state
# ---------------------------------------------------------------------------

class KeyPool:
    """Tracks which keys in a provider are still usable this run."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.dead = set()

    def alive_keys(self):
        return [k for k in self.keys if k not in self.dead]

    def mark_dead(self, key):
        self.dead.add(key)

    def all_dead(self):
        return len(self.alive_keys()) == 0


tavily_pool = KeyPool(TAVILY_KEYS)
serper_pool = KeyPool(SERPER_KEYS)


# ---------------------------------------------------------------------------
# Search providers
# ---------------------------------------------------------------------------

def search_tavily(query: str):
    """
    Try every remaining Tavily key in order. Returns (raw_json, key_used)
    on success, or (None, None) if every Tavily key is exhausted/erroring.
    """
    for key in tavily_pool.alive_keys():
        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": key,
                    "query": query,
                    "search_depth": "basic",
                    "include_domains": ["societe.com"],
                    "max_results": 5,
                },
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code in TAVILY_EXHAUSTED_STATUS:
                print(f"  [tavily] key ...{key[-6:]} exhausted/rejected "
                      f"(HTTP {resp.status_code}), rotating", file=sys.stderr)
                tavily_pool.mark_dead(key)
                continue
            resp.raise_for_status()
            return resp.json(), key
        except requests.RequestException as e:
            print(f"  [tavily] key ...{key[-6:]} request error: {e}, rotating",
                  file=sys.stderr)
            tavily_pool.mark_dead(key)
            continue
    return None, None


def search_serper(query: str):
    """
    Fallback provider. Returns (raw_json, key_used) on success,
    or (None, None) if every Serper key is exhausted/erroring.
    """
    for key in serper_pool.alive_keys():
        try:
            resp = requests.post(
                "https://google.serper.dev/search",
                headers={
                    "X-API-KEY": key,
                    "Content-Type": "application/json",
                },
                data=json.dumps({"q": f"{query} site:societe.com"}),
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code in SERPER_EXHAUSTED_STATUS:
                print(f"  [serper] key ...{key[-6:]} exhausted/rejected "
                      f"(HTTP {resp.status_code}), rotating", file=sys.stderr)
                serper_pool.mark_dead(key)
                continue
            resp.raise_for_status()
            return resp.json(), key
        except requests.RequestException as e:
            print(f"  [serper] key ...{key[-6:]} request error: {e}, rotating",
                  file=sys.stderr)
            serper_pool.mark_dead(key)
            continue
    return None, None


def search_with_rotation(query: str):
    """
    Tavily first (all 3 accounts), then Serper as fallback (all accounts).
    Returns dict: {"provider": ..., "key_tail": ..., "raw": ...} or None if
    every provider/key combination failed.
    """
    raw, key = search_tavily(query)
    if raw is not None:
        return {"provider": "tavily", "key_tail": key[-6:], "raw": raw}

    raw, key = search_serper(query)
    if raw is not None:
        return {"provider": "serper", "key_tail": key[-6:], "raw": raw}

    return None


# ---------------------------------------------------------------------------
# Progress + persistence
# ---------------------------------------------------------------------------

def load_progress(total_rows: int) -> dict:
    if PROGRESS_PATH.exists():
        with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"last_index": -1, "total": total_rows}
    data["total"] = total_rows
    return data


def save_progress(progress: dict):
    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def append_result(record: dict):
    with open(RESULTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def git_commit_and_push(message: str):
    """Commit + push data/ changes. No-op (with a warning) if nothing changed."""
    subprocess.run(["git", "add", "data/results.jsonl", "data/progress.json"],
                    cwd=REPO_ROOT, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT)
    if diff.returncode == 0:
        print("  [git] nothing to commit", file=sys.stderr)
        return
    subprocess.run(["git", "commit", "-m", message], cwd=REPO_ROOT, check=True)
    subprocess.run(["git", "push"], cwd=REPO_ROOT, check=True)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    if not TAVILY_KEYS and not SERPER_KEYS:
        print("No API keys provided (TAVILY_KEYS / SERPER_KEYS env vars empty). "
              "Aborting.", file=sys.stderr)
        sys.exit(1)

    with open(CSV_PATH, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    total_rows = len(rows)

    progress = load_progress(total_rows)
    start_index = progress["last_index"] + 1

    if start_index >= total_rows:
        print(f"Already complete: {total_rows}/{total_rows} rows processed. Nothing to do.")
        return

    print(f"Resuming from row {start_index}/{total_rows}")

    while start_index < total_rows:
        batch_end = min(start_index + BATCH_SIZE, total_rows)
        print(f"\n=== Batch: rows {start_index}..{batch_end - 1} ===")

        for i in range(start_index, batch_end):
            row = rows[i]
            nom_agence = (row.get("nom_agence") or "").strip()
            ville = (row.get("ville") or "").strip()
            query = QUERY_TEMPLATE.format(nom_agence=nom_agence)

            print(f"[{i}] {query}")

            result = search_with_rotation(query)

            if result is None:
                print("All provider keys exhausted/failing. Stopping run; "
                      "a later scheduled run will resume from here.",
                      file=sys.stderr)
                # Persist whatever we have so far, then exit without error
                # so the workflow doesn't get marked as failed.
                progress["last_index"] = i - 1
                save_progress(progress)
                git_commit_and_push(
                    f"siren-scan: partial batch, stopped at row {i} (keys exhausted)"
                )
                sys.exit(0)

            record = {
                "index": i,
                "email": row.get("email", ""),
                "nom_agence": nom_agence,
                "ville": ville,
                "site_web": row.get("site_web", ""),
                "site_id": row.get("site_id", ""),
                "query": query,
                "provider": result["provider"],
                "key_tail": result["key_tail"],
                "raw_result": result["raw"],
            }
            append_result(record)

            progress["last_index"] = i
            time.sleep(SLEEP_BETWEEN_QUERIES)

        save_progress(progress)
        git_commit_and_push(
            f"siren-scan: completed rows {start_index}..{batch_end - 1} "
            f"({batch_end}/{total_rows})"
        )

        start_index = batch_end

    print(f"\nAll {total_rows} rows processed. Done.")


if __name__ == "__main__":
    main()
