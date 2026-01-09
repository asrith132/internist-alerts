import json
import os
import re
import subprocess
from typing import List, Tuple, Optional

import requests
from bs4 import BeautifulSoup

# ---------- Intern-List ----------
INTERNLIST_URL = "https://www.intern-list.com/swe-intern-list"
INTERNLIST_JOB_URL_RE = re.compile(r"^https://www\.intern-list\.com/swe-intern-list/.+_\d+$")

# ---------- SimplifyJobs GitHub Repo ----------
# Raw README (dev branch). This is the main list repo page: https://github.com/SimplifyJobs/Summer2026-Internships
SIMPLIFY_RAW_README_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md"
# We only parse the "Software Engineering Internship Roles" section in the README.
SIMPLIFY_SECTION_HEADER_RE = re.compile(r"^##\s+Software Engineering Internship Roles\s*$", re.MULTILINE)

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


def normalize_url(href: str) -> Optional[str]:
    href = (href or "").strip()
    if not href:
        return None
    if href.startswith("/"):
        return f"https://www.intern-list.com{href}"
    if href.startswith("https://") or href.startswith("http://"):
        return href
    return None


# -------------------- Intern-List scraper --------------------
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
        link = normalize_url(a.get("href"))
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


# -------------------- SimplifyJobs GitHub scraper --------------------
def _extract_markdown_link(cell: str) -> Optional[str]:
    # Matches [text](url)
    m = re.search(r"\[[^\]]+\]\((https?://[^)]+)\)", cell)
    return m.group(1) if m else None


def fetch_simplify_swe_jobs() -> List[Tuple[str, str, str]]:
    """
    Parses the SimplifyJobs/Summer2026-Internships README.md (dev branch),
    extracts the Software Engineering table rows, returns (job_id, title, link).

    We use the application link as the primary link when available.
    """
    md = requests.get(
        SIMPLIFY_RAW_README_URL,
        timeout=25,
        headers={"User-Agent": "Mozilla/5.0 (internlist-alert-bot)"},
    ).text

    # Find the SWE section
    m = SIMPLIFY_SECTION_HEADER_RE.search(md)
    if not m:
        return []

    # Take text from SWE header until the next "## " header
    start = m.end()
    next_header = re.search(r"\n##\s+", md[start:])
    section = md[start : start + next_header.start()] if next_header else md[start:]

    # Extract markdown table rows (pipes)
    # Typical table: | Company | Role | Location | Application | Age |
    lines = [ln.strip() for ln in section.splitlines() if ln.strip().startswith("|")]

    jobs: List[Tuple[str, str, str]] = []
    seen_ids = set()

    for ln in lines:
        # skip header + separator rows
        if re.match(r"^\|\s*Company\s*\|", ln, re.IGNORECASE):
            continue
        if set(ln.replace("|", "").strip()) <= {"-", ":", " "}:
            continue

        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) < 4:
            continue

        company = cells[0]
        role = cells[1]
        location = cells[2]
        app_cell = cells[3]

        link = _extract_markdown_link(app_cell)
        if not link:
            # Some rows might have empty app cells; skip those.
            continue

        title = f"[Simplify] {company} — {role} ({location})"
        job_id = f"simplify:{company}|{role}|{location}|{link}"

        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        jobs.append((job_id, title, link))

    return jobs


def main():
    seen = load_seen()

    jobs = []
    jobs.extend(fetch_internlist_jobs())
    jobs.extend(fetch_simplify_swe_jobs())

    new_jobs = [(jid, title, link) for (jid, title, link) in jobs if jid not in seen]
    if not new_jobs:
        return

    lines = ["🆕 New internship postings (Intern-List + Simplify):"]

    # send up to 8 per run
    for jid, title, link in new_jobs[:8]:
        # one blank line after each posting
        lines.append(f"- {title}\n  {link}\n")

    send_telegram("\n".join(lines))

    # mark all as seen (not only first 8)
    for jid, _, _ in new_jobs:
        seen.add(jid)

    save_seen(seen)
    git_commit_if_changed()


if __name__ == "__main__":
    main()
