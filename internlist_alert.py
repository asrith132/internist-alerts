import json
import os
import re
import subprocess
from typing import List, Tuple, Optional

import requests
from bs4 import BeautifulSoup

# =========================
# Intern-List (SWE list page)
# =========================
INTERNLIST_URL = "https://www.intern-list.com/swe-intern-list"
INTERNLIST_JOB_URL_RE = re.compile(r"^https://www\.intern-list\.com/swe-intern-list/.+_\d+$")

# =========================
# SimplifyJobs GitHub repo
# =========================
SIMPLIFY_RAW_URLS = [
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/main/README.md",
]
SWE_SECTION_RE = re.compile(r"^##\s+Software Engineering Internship Roles\s*$", re.MULTILINE)

STATE_FILE = "seen.json"


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


def send_telegram(msg: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    api = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        api,
        data={
            "chat_id": chat_id,
            "text": msg,
            "disable_web_page_preview": True,
        },
        timeout=25,
    )
    r.raise_for_status()


def git_commit_if_changed() -> None:
    diff = subprocess.run(
        ["git", "diff", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
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


def normalize_internlist_url(href: str) -> Optional[str]:
    href = (href or "").strip()
    if not href:
        return None
    if href.startswith("/"):
        return f"https://www.intern-list.com{href}"
    if href.startswith("https://") or href.startswith("http://"):
        return href
    return None


# ----------------------------
# Intern-List scraper
# ----------------------------
def fetch_internlist_jobs() -> List[Tuple[str, str, str]]:
    """
    Returns list of (job_id, title, link)
    job_id is prefixed with 'internlist:' to avoid collisions.
    """
    html = requests.get(
        INTERNLIST_URL,
        timeout=25,
        headers={"User-Agent": "Mozilla/5.0 (internlist-alert-bot)"},
    ).text
    soup = BeautifulSoup(html, "html.parser")

    out: List[Tuple[str, str, str]] = []
    seen_links = set()

    for a in soup.select("a[href]"):
        link = normalize_internlist_url(a.get("href"))
        if not link:
            continue
        if not INTERNLIST_JOB_URL_RE.match(link):
            continue

        raw = " ".join(a.get_text(" ", strip=True).split())
        if not raw:
            # image-only anchors -> skip
            continue

        # Move date to the front: [January 8, 2026] ...
        date_match = re.search(r"([A-Za-z]+ \d{1,2}, \d{4})", raw)
        date = date_match.group(1) if date_match else None
        title = re.sub(r"\s*[A-Za-z]+ \d{1,2}, \d{4}\s*", " ", raw)
        title = re.sub(r"\s+", " ", title).strip()
        if date:
            title = f"[{date}] {title}"

        if link in seen_links:
            continue
        seen_links.add(link)

        job_id = f"internlist:{link}"
        out.append((job_id, title, link))

    return out


# ----------------------------
# Simplify scraper (robust)
# ----------------------------
def _first_md_link_url(cell: str) -> Optional[str]:
    m = re.search(r"\[[^\]]+\]\((https?://[^)]+)\)", cell)
    return m.group(1) if m else None


def fetch_simplify_swe_jobs() -> List[Tuple[str, str, str]]:
    """
    Returns list of (job_id, title, link) from SimplifyJobs/Summer2026-Internships SWE table.
    """
    md = None
    used_url = None

    for url in SIMPLIFY_RAW_URLS:
        try:
            r = requests.get(url, timeout=35, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and r.text and "Summer 2026" in r.text:
                md = r.text
                used_url = url
                break
        except Exception:
            continue

    if not md:
        return []

    m = SWE_SECTION_RE.search(md)
    if not m:
        return []

    start = m.end()
    m2 = re.search(r"\n##\s+", md[start:])
    section = md[start : start + m2.start()] if m2 else md[start:]

    # Pipe table rows
    lines = [ln.rstrip() for ln in section.splitlines() if ln.strip().startswith("|")]
    if not lines:
        return []

    jobs: List[Tuple[str, str, str]] = []

    for ln in lines:
        # Skip header row and separator row
        if re.match(r"^\|\s*Company\s*\|", ln, flags=re.IGNORECASE):
            continue
        if re.match(r"^\|\s*[-: ]+\|\s*[-: ]+\|", ln):
            continue

        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue

        company = re.sub(r"\s+", " ", re.sub(r"\[|\]|\(.*?\)", "", cells[0])).strip()
        role = re.sub(r"\s+", " ", re.sub(r"\[|\]|\(.*?\)", "", cells[1])).strip()
        location = re.sub(r"\s+", " ", cells[2]).strip()
        app_cell = cells[3]
        age = cells[4].strip()

        link = _first_md_link_url(app_cell)
        if not link:
            continue

        title = f"[Simplify | {age}] {company} — {role} ({location})"
        job_id = f"simplify:{company}|{role}|{location}|{link}"

        jobs.append((job_id, title, link))

    return jobs


def main():
    seen = load_seen()

    internlist_jobs = fetch_internlist_jobs()
    simplify_jobs = fetch_simplify_swe_jobs()

    new_internlist = [(jid, title, link) for (jid, title, link) in internlist_jobs if jid not in seen]
    new_simplify = [(jid, title, link) for (jid, title, link) in simplify_jobs if jid not in seen]

    # If nothing new, do nothing (avoid spam)
    if not new_internlist and not new_simplify:
        return

    lines = ["🆕 New Internship Postings\n"]

    if new_internlist:
        lines.append("📌 Intern-List")
        for _, title, link in new_internlist[:6]:
            lines.append(f"- {title}\n  {link}\n")

    # Always show Simplify section so you can tell if it's broken/empty
    lines.append("📌 Simplify (GitHub)")
    if new_simplify:
        for _, title, link in new_simplify[:6]:
            lines.append(f"- {title}\n  {link}\n")
    else:
        lines.append("- (No new postings found from Simplify on this run)\n")

    send_telegram("\n".join(lines))

    # Mark all new items as seen
    for jid, _, _ in (new_internlist + new_simplify):
        seen.add(jid)

    save_seen(seen)
    git_commit_if_changed()


if __name__ == "__main__":
    main()
