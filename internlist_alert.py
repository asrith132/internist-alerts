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
# SimplifyJobs GitHub (rendered HTML)
# =========================
SIMPLIFY_REPO_PAGE_URL = "https://github.com/SimplifyJobs/Summer2026-Internships"

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
        timeout=30,
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
    html = requests.get(
        INTERNLIST_URL,
        timeout=30,
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
            continue  # image-only anchors

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
# Simplify (GitHub HTML) scraper
# ----------------------------
def fetch_simplify_swe_jobs() -> Tuple[List[Tuple[str, str, str]], int]:
    html = requests.get(
        SIMPLIFY_REPO_PAGE_URL,
        timeout=45,
        headers={"User-Agent": "Mozilla/5.0 (internlist-alert-bot)"},
    ).text

    soup = BeautifulSoup(html, "html.parser")

    header = None
    for tag in soup.find_all(["h2", "h3"]):
        text = " ".join(tag.get_text(" ", strip=True).split()).lower()
        if "software engineering internship roles" in text:
            header = tag
            break

    if not header:
        return [], 0

    table = header.find_next("table")
    if not table:
        return [], 0

    # Identify column indexes by header labels
    ths = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]

    def col_idx(name: str) -> int:
        for i, t in enumerate(ths):
            if name in t:
                return i
        return -1

    i_company = col_idx("company")
    i_role = col_idx("role")
    i_location = col_idx("location")
    i_app = col_idx("application")
    i_age = col_idx("age")

    if min(i_company, i_role, i_location, i_app, i_age) < 0:
        return [], 0

    jobs: List[Tuple[str, str, str]] = []
    parsed_rows = 0

    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        parsed_rows += 1

        def cell_text(idx: int) -> str:
            return " ".join(tds[idx].get_text(" ", strip=True).split())

        company = cell_text(i_company)
        role = cell_text(i_role)
        location = cell_text(i_location)
        age = cell_text(i_age) or "?"

        app_cell = tds[i_app]
        a = app_cell.find("a", href=True)
        link = a["href"].strip() if a else ""

        # GitHub sometimes uses relative links; convert to absolute
        if link.startswith("/"):
            link = "https://github.com" + link

        if not link:
            continue

        title = f"[Simplify | {age}] {company} — {role} ({location})"
        job_id = f"simplify:{company}|{role}|{location}|{link}"

        jobs.append((job_id, title, link))

    return jobs, parsed_rows


def is_fresh_simplify_age(age_str: str) -> bool:
    """
    Keep only very fresh Simplify rows: 0d or 1d.
    Age strings are like '0d', '1d', '10d', etc.
    """
    age_str = (age_str or "").strip().lower()
    return age_str in {"0d", "1d"}


def main():
    seen = load_seen()

    internlist_jobs = fetch_internlist_jobs()
    simplify_jobs, simplify_parsed_rows = fetch_simplify_swe_jobs()

    # ----------------------------
    # Filter 1: "Prime silently" on first run (seen.json empty)
    # ----------------------------
    if not seen:
        # Mark everything currently visible as seen and exit without messaging
        for jid, _, _ in internlist_jobs + simplify_jobs:
            seen.add(jid)
        save_seen(seen)
        git_commit_if_changed()
        return

    # Intern-List new items (no extra age filter)
    new_internlist = [(jid, title, link) for (jid, title, link) in internlist_jobs if jid not in seen]

    # Simplify new items + Filter 2: only 0d/1d
    new_simplify = []
    for jid, title, link in simplify_jobs:
        if jid in seen:
            continue
        m = re.search(r"\[Simplify \|\s*([^\]]+)\]", title)
        age = m.group(1).strip() if m else ""
        if not is_fresh_simplify_age(age):
            continue
        new_simplify.append((jid, title, link))

    # Avoid spam if nothing new
    if not new_internlist and not new_simplify:
        return

    lines = ["🆕 New Internship Postings\n"]

    if new_internlist:
        lines.append("📌 Intern-List")
        for _, title, link in new_internlist[:6]:
            lines.append(f"- {title}\n  {link}\n")

    # Always show Simplify section so you can see status
    lines.append("📌 Simplify (GitHub)")
    lines.append(f"- Parsed {simplify_parsed_rows} rows; {len(new_simplify)} new (age 0d/1d)\n")

    if new_simplify:
        for _, title, link in new_simplify[:6]:
            lines.append(f"- {title}\n  {link}\n")
    else:
        lines.append("- (No new postings found from Simplify on this run)\n")

    send_telegram("\n".join(lines))

    # Mark as seen
    for jid, _, _ in (new_internlist + new_simplify):
        seen.add(jid)

    save_seen(seen)
    git_commit_if_changed()


if __name__ == "__main__":
    main()
