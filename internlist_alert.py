import json
import os
import re
import subprocess
from typing import List, Tuple

import requests
from bs4 import BeautifulSoup


URL = "https://www.intern-list.com/swe-intern-list"
STATE_FILE = "seen.json"


def load_seen() -> set[str]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()
    except Exception:
        # If file got corrupted somehow, don't crash the workflow
        return set()


def save_seen(seen: set[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)


def fetch_jobs() -> List[Tuple[str, str]]:
    html = requests.get(URL, timeout=25).text
    soup = BeautifulSoup(html, "html.parser")

    jobs: List[Tuple[str, str]] = []
    for a in soup.select("a[href]"):
        href = a["href"].strip()
        text = " ".join(a.get_text(" ", strip=True).split())

        # Normalize absolute URL
        if href.startswith("/"):
            full = f"https://www.intern-list.com{href}"
        elif href.startswith("http"):
            full = href
        else:
            continue

        # Heuristic filter: keep intern-list links that look like postings
        # (You can tune this if you see false positives.)
        if "intern-list.com" not in full:
            continue
        if not re.search(r"(intern|internship)", full, re.IGNORECASE):
            continue

        if text:
            jobs.append((full, text))

    # Deduplicate while preserving order
    seen = set()
    out = []
    for link, title in jobs:
        if link not in seen:
            seen.add(link)
            out.append((link, title))
    return out


def send_telegram(msg: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    api = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(
        api,
        data={"chat_id": chat_id, "text": msg, "disable_web_page_preview": True},
        timeout=25,
    ).raise_for_status()


def git_commit_if_changed() -> None:
    # Only commit if seen.json changed
    subprocess.run(["git", "status", "--porcelain"], check=True, capture_output=True, text=True)
    diff = subprocess.run(["git", "diff", "--name-only"], check=True, capture_output=True, text=True).stdout.strip()
    if "seen.json" not in diff:
        return

    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", "seen.json"], check=True)
    subprocess.run(["git", "commit", "-m", "Update seen jobs"], check=True)
    subprocess.run(["git", "push"], check=True)


def main():
    seen = load_seen()
    jobs = fetch_jobs()

    new_jobs = [(link, title) for link, title in jobs if link not in seen]

    if new_jobs:
        # Send up to 6 at a time to avoid spam
        lines = ["🆕 New SWE internship postings:"]
        for link, title in new_jobs[:6]:
            lines.append(f"- {title}\n  {link}")
        send_telegram("\n".join(lines))

        # Mark all new as seen (even beyond the first 6)
        for link, _ in new_jobs:
            seen.add(link)
        save_seen(seen)

        # Persist state back into repo
        git_commit_if_changed()
    else:
        # No new postings: do nothing
        pass


if __name__ == "__main__":
    main()
