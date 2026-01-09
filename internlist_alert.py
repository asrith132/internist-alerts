import json
import os
import re
import subprocess
from typing import List, Tuple

import requests
from bs4 import BeautifulSoup

URL = "https://www.intern-list.com/swe-intern-list"
STATE_FILE = "seen.json"

# Real job posting URLs on the SWE page end with _<digits>
JOB_URL_RE = re.compile(r"^https://www\.intern-list\.com/swe-intern-list/.+_\d+$")


def load_seen() -> set[str]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except FileNotFoundError:
        return set()
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)


def normalize_url(href: str) -> str | None:
    href = (href or "").strip()
    if not href:
        return None
    if href.startswith("/"):
        return f"https://www.intern-list.com{href}"
    if href.startswith("https://") or href.startswith("http://"):
        return href
    return None


def fetch_jobs() -> List[Tuple[str, str]]:
    html = requests.get(
        URL,
        timeout=25,
        headers={"User-Agent": "Mozilla/5.0 (internlist-alert-bot)"},
    ).text
    soup = BeautifulSoup(html, "html.parser")

    jobs: List[Tuple[str, str]] = []
    for a in soup.select("a[href]"):
        full = normalize_url(a.get("href"))
        if not full:
            continue

        # ✅ Only real job links
        if not JOB_URL_RE.match(full):
            continue

        title = " ".join(a.get_text(" ", strip=True).split())
        if not title:
            # This is usually the image-only link; skip it
            continue
        
        jobs.append((full, title))


    # Deduplicate
    seen_links = set()
    out: List[Tuple[str, str]] = []
    for link, title in jobs:
        if link not in seen_links:
            seen_links.add(link)
            out.append((link, title))
    return out


def send_telegram(msg: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    api = f"https://api.telegram.org/bot{token}/sendMessage"

    r = requests.post(
        api,
        data={"chat_id": chat_id, "text": msg, "disable_web_page_preview": True},
        timeout=25,
    )
    r.raise_for_status()


def git_commit_if_changed() -> None:
    diff = subprocess.run(
        ["git", "diff", "--name-only"], check=True, capture_output=True, text=True
    ).stdout.strip()

    if "seen.json" not in diff:
        return

    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        check=True,
    )
    subprocess.run(["git", "add", "seen.json"], check=True)
    subprocess.run(["git", "commit", "-m", "Update seen jobs"], check=True)
    subprocess.run(["git", "push"], check=True)


def main():
    seen = load_seen()
    jobs = fetch_jobs()

    new_jobs = [(link, title) for link, title in jobs if link not in seen]
    if not new_jobs:
        return

    lines = ["🆕 New SWE internship postings (Intern-List):"]
    for link, title in new_jobs[:6]:
        lines.append(f"- {title}\n  {link}")

    send_telegram("\n".join(lines))

    for link, _ in new_jobs:
        seen.add(link)
    save_seen(seen)
    git_commit_if_changed()


if __name__ == "__main__":
    main()
