#!/usr/bin/env python3
"""
Retry pass for agencies whose SIREN wasn't confidently found by name
(confiance BASSE/AUCUNE from extract_sirens.py). This time the query uses
the agency's website domain instead of its (often generic) display name:
    "<domaine> immobilier societe.com"
which tends to be far more specific and avoid false-positive matches on
common agency names ("La Bonne Agence", "PARIS IMMO", etc.).

Same engine as scan_sirens.py: Tavily only this time (3 accounts, key
rotation kept), batches of 100 committed + pushed back-to-back, resumable
via data/domain_retry_progress.json.
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
CSV_PATH = REPO_ROOT / "data" / "a_retester.csv"
PROGRESS_PATH = REPO_ROOT / "data" / "domain_retry_progress.json"
RESULTS_PATH = REPO_ROOT / "data" / "results_domain_retry.jsonl"
LOG_PATH = REPO_ROOT / "data" / "domain_retry.log"


def log(message: str):
    """Print to the Actions console AND append to a persistent log file
    that gets committed alongside results/progress each batch."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line, file=sys.stderr)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


BATCH_SIZE = 100
QUERY_TEMPLATE = "{domaine} immobilier societe.com"
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_QUERIES = 0.3  # be polite to APIs

# Keys are passed as a comma-separated env var (GitHub Secret), e.g.:
#   TAVILY_KEYS="tvly-dev-aaa,tvly-dev-bbb,tvly-dev-ccc"
TAVILY_KEYS = [k.strip() for k in os.environ.get("TAVILY_KEYS", "").split(",") if k.strip()]

TAVILY_EXHAUSTED_STATUS = {432, 433, 401, 403, 429}


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


# ---------------------------------------------------------------------------
# Search providers
# ---------------------------------------------------------------------------

def search_tavily(query: str):
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
                log(f"  [tavily] key ...{key[-6:]} exhausted/rejected "
                    f"(HTTP {resp.status_code}), rotating")
                tavily_pool.mark_dead(key)
                continue
            resp.raise_for_status()
            return resp.json(), key
        except requests.RequestException as e:
            log(f"  [tavily] key ...{key[-6:]} request error: {e}, rotating")
            tavily_pool.mark_dead(key)
            continue
    return None, None


def search_with_rotation(query: str):
    raw, key = search_tavily(query)
    if raw is not None:
        return {"provider": "tavily", "key_tail": key[-6:], "raw": raw}
    return None


# ---------------------------------------------------------------------------
# Progress + persistence
# ---------------------------------------------------------------------------

def load_progress(total_rows: int) -> dict:
    data = {}
    if PROGRESS_PATH.exists():
        try:
            with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
        except (json.JSONDecodeError, OSError) as e:
            log(f"Warning: could not read domain_retry_progress.json ({e}), starting fresh")
            data = {}
    data.setdefault("last_index", -1)
    data["total"] = total_rows
    return data


def save_progress(progress: dict):
    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def append_result(record: dict):
    with open(RESULTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def git_commit_and_push(message: str, max_retries: int = 5):
    subprocess.run(["git", "add", "data/results_domain_retry.jsonl",
                     "data/domain_retry_progress.json", "data/domain_retry.log"],
                    cwd=REPO_ROOT, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT)
    if diff.returncode == 0:
        log("  [git] nothing to commit")
        return
    subprocess.run(["git", "commit", "-m", message], cwd=REPO_ROOT, check=True)

    for attempt in range(1, max_retries + 1):
        push = subprocess.run(["git", "push"], cwd=REPO_ROOT)
        if push.returncode == 0:
            return
        print(f"  [git] push rejected (attempt {attempt}/{max_retries}), "
              f"fetching + rebasing before retry", file=sys.stderr)
        subprocess.run(["git", "fetch", "origin"], cwd=REPO_ROOT, check=True)
        rebase = subprocess.run(["git", "rebase", "origin/main"], cwd=REPO_ROOT)
        if rebase.returncode != 0:
            print("  [git] rebase conflict, aborting rebase and giving up on this push",
                  file=sys.stderr)
            subprocess.run(["git", "rebase", "--abort"], cwd=REPO_ROOT)
            raise RuntimeError("git push failed after rebase conflict")
        time.sleep(2)

    raise RuntimeError(f"git push failed after {max_retries} attempts")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    if not TAVILY_KEYS:
        log("No API keys provided (TAVILY_KEYS env var empty). Aborting.")
        sys.exit(1)

    with open(CSV_PATH, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    total_rows = len(rows)

    progress = load_progress(total_rows)
    start_index = progress["last_index"] + 1

    if start_index >= total_rows:
        log(f"Already complete: {total_rows}/{total_rows} rows processed. Nothing to do.")
        return

    log(f"Resuming from row {start_index}/{total_rows}")

    while start_index < total_rows:
        batch_end = min(start_index + BATCH_SIZE, total_rows)
        log(f"=== Batch: rows {start_index}..{batch_end - 1} ===")

        for i in range(start_index, batch_end):
            row = rows[i]
            domaine = (row.get("domaine") or "").strip()
            nom_agence = (row.get("nom_agence") or "").strip()
            ville = (row.get("ville") or "").strip()

            if not domaine:
                # shouldn't happen since a_retester.csv is pre-filtered, but
                # skip safely rather than crash if it does
                progress["last_index"] = i
                continue

            query = QUERY_TEMPLATE.format(domaine=domaine)
            pct = (i + 1) / total_rows * 100

            log(f"[{i + 1}/{total_rows} | {pct:.1f}%] {query}")

            result = search_with_rotation(query)

            if result is None:
                log("All provider keys exhausted/failing. Stopping run; "
                    "a later scheduled run will resume from here.")
                progress["last_index"] = i - 1
                save_progress(progress)
                git_commit_and_push(
                    f"domain-retry: partial batch, stopped at row {i} (keys exhausted)"
                )
                sys.exit(0)

            record = {
                "index": row.get("index", i),
                "email": row.get("email", ""),
                "nom_agence": nom_agence,
                "ville": ville,
                "site_web": row.get("site_web", ""),
                "domaine": domaine,
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
            f"domain-retry: completed rows {start_index}..{batch_end - 1} "
            f"({batch_end}/{total_rows})"
        )

        start_index = batch_end

    log(f"All {total_rows} rows processed. Done.")


if __name__ == "__main__":
    main()
