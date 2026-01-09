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
# SimplifyJobs GitHub repo (raw README)
# =========================
SIMPLIFY_RAW_URLS = [
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/main/README.md",
]

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
    """
    Returns list of (job_id, title, link)
    """
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
# Simplify scraper (table-header based)
# ----------------------------
def _extract_any_url(text: str) -> Optional[str]:
    """
    Pull the first URL from:
      - markdown links: [Apply](https://...)
      - raw urls: https://...
      - html links: <a href="https://...">
    """
    if not text:
        return None

    # HTML href first
    m = re.search(r'href="(https?://[^"]+)"', text)
    if m:
        return m.group(1)

    # Markdown link
    m = re.search(r"\[[^\]]+\]\((https?://[^)]+)\)", text)
    if m:
        return m.group(1)

    # Raw URL
    m = re.search(r"(https?://[^\s)]+)", text)
    if m:
        return m.group(1)

    return None


def _clean_md_text(s: str) -> str:
    # Convert [text](url) -> text
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    # Remove leftover HTML tags if any
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def fetch_simplify_swe_jobs() -> Tuple[List[Tuple[str, str, str]], int]:
    """
    Finds the SWE table by matching the table header row:
      | Company | Role | Location | Application | Age |
    Then parses subsequent pipe rows until the table ends.

    Returns (jobs, parsed_row_count)
    jobs: (job_id, title, link)
    """
    md = None
    for url in SIMPLIFY_RAW_URLS:
        try:
            r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and r.text:
                md = r.text
                break
        except Exception:
            continue

    if not md:
        return [], 0

    lines = md.splitlines()

    # 1) Find the table header row for SWE
    header_idx = -1
    for i, ln in enumerate(lines):
        norm = re.sub(r"\s+", " ", ln.strip().lower())
        if (
            norm.startswith("| company |")
            and " | role | " in norm
            and " | location | " in norm
            and " | application | " in norm
            and " | age |" in norm
        ):
            header_idx = i
            break

    if header_idx == -1:
        return [], 0

    # 2) Table starts after header + separator row
    start = header_idx + 2

    jobs: List[Tuple[str, str, str]] = []
    parsed_rows = 0

    for ln in lines[start:]:
        if not ln.strip().startswith("|"):
            # table ended
            break

        # skip separator rows if any appear again
        if set(ln.replace("|", "").strip()) <= {"-", ":", " "}:
            continue

        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue

        parsed_rows += 1

        company = _clean_md_text(cells[0])
        role = _clean_md_text(cells[1])
        location = _clean_md_text(cells[2])
        app_cell = cells[3]
        age = _clean_md_text(cells[4]) or "?"

        # Extract application URL
        link = _extract_any_url(app_cell) or _extract_any_url(ln)
        if not link:
            continue

        title = f"[Simplify | {age}] {company} — {role} ({location})"
        job_id = f"simplify:{company}|{role}|{location}|{link}"

        jobs.append((job_id, title, link))

    return jobs, parsed_rows


def main():
    seen = load_seen()

    internlist_jobs = fetch_internlist_jobs()
    simplify_jobs, simplify_parsed_rows = fetch_simplify_swe_jobs()

    new_internlist = [(jid, title, link) for (jid, title, link) in internlist_jobs if jid not in seen]
    new_simplify = [(jid, title, link) for (jid, title, link) in simplify_jobs if jid not in seen]

    # Avoid spam if nothing new
    if not new_internlist and not new_simplify:
        return

    lines = ["🆕 New Internship Postings\n"]

    if new_internlist:
        lines.append("📌 Intern-List")
        for _, title, link in new_internlist[:6]:
            lines.append(f"- {title}\n  {link}\n")

    # Always show Simplify section so it's obvious if it's empty/broken
    lines.append("📌 Simplify (GitHub)")
    lines.append(f"- Parsed {simplify_parsed_rows} rows; {len(new_simplify)} new this run\n")

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
