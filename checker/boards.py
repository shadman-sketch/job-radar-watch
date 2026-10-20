"""Multi-company job boards (e.g. the a16z portfolio board at jobs.a16z.com).

These boards load jobs from a JSON search API after the page opens. We open the board in a
headless browser, capture the search request the page itself makes, then replay that request
page by page from inside the same browser session and read every job. If no API call can be
captured, we fall back to reading the job cards on the page.

Each returned job: {id, title, company, location, remote, url, posted}.
"""
import json
import re

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
MAX_PAGES = 250


def _loc_text(locs):
    out = []
    for l in locs or []:
        if isinstance(l, str):
            out.append(l)
        elif isinstance(l, dict):
            out.append(l.get("name") or l.get("label") or ", ".join(
                [str(x) for x in [l.get("city"), l.get("state") or l.get("region"), l.get("country")] if x]))
    return " / ".join([x for x in out if x])


def _parse_jobs(payload, host):
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if jobs is None and isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "title" in v[0]:
                jobs = v
                break
    out = []
    for j in jobs or []:
        company = j.get("companyName") or (j.get("company") or {}).get("name") if isinstance(j.get("company"), dict) else j.get("companyName") or j.get("company")
        slug = j.get("companySlug") or (j.get("company") or {}).get("slug") if isinstance(j.get("company"), dict) else j.get("companySlug")
        jid = j.get("jobId") or j.get("id") or j.get("url")
        locs = j.get("locations") or j.get("normalizedLocations") or ([j.get("location")] if j.get("location") else [])
        loc = _loc_text(locs)
        remote = bool(j.get("remote") or j.get("isRemote")) or "remote" in json.dumps(j.get("workplaceType", "")).lower()
        url = j.get("url") or j.get("applyUrl")
        if not url and slug and jid:
            url = f"https://{host}/jobs/{slug}/{jid}"
        out.append({"id": jid, "title": j.get("title"), "company": company or "", "location": loc,
                    "remote": remote, "url": url,
                    "posted": j.get("timeStamp") or j.get("postedAt") or j.get("createdAt") or j.get("datePosted")})
    return out, (payload.get("meta") or {}) if isinstance(payload, dict) else {}


BOARD_TERMS = ["help desk", "service desk", "IT support", "desktop support", "IT technician", "technical support",
               "end user", "IT operations", "workplace", "IT specialist", "security operations", "SOC analyst",
               "access management", "identity", "data center technician", "NOC", "IT intern", "IT asset", "deskside",
               "systems administrator", "security technician"]
LOC_RE = re.compile(r"(,\s*[A-Z]{2}\b|remote|united states|\bUSA?\b|, [A-Z][a-z]+)", re.I)


def _cards(page):
    return page.eval_on_selector_all(
        "a[href*='/jobs/']",
        "els => els.map(e => [e.href, (e.innerText||'').trim(), "
        "((e.closest('li, article, [class*=job], [class*=Job]')||e.parentElement||e).innerText||'').trim()])")


def fetch_board(board):
    """board: {"key", "name", "url", "host"}. Searches the board for each target term. Returns (jobs, error)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "playwright not installed"
    from urllib.parse import quote
    jobs, err, debug = {}, None, {"terms": {}}
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(user_agent=UA)
        page = ctx.new_page()
        try:
            for term in BOARD_TERMS:
                page.goto(f"{board['url']}?q={quote(term)}&sort=recent", timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(3500)
                for _ in range(6):
                    more = page.query_selector("button:has-text('Show more'), button:has-text('Load more')")
                    if not more:
                        break
                    more.click()
                    page.wait_for_timeout(1500)
                found = 0
                samples = []
                for href, atext, ctext in _cards(page):
                    m = re.search(r"/jobs/([^/?#]+)/([^/?#]+)--", href)
                    if not m:
                        continue
                    lines = [l.strip() for l in (ctext or atext).split("\n") if l.strip()]
                    title = (atext.split("\n")[0].strip() if atext.strip() else (lines[0] if lines else ""))
                    if not title:
                        continue
                    company = next((l for l in lines if l != title and not LOC_RE.search(l) and len(l) < 60), "") or \
                        m.group(1).replace("-", " ").title()
                    loc = next((l for l in lines if l != title and LOC_RE.search(l) and len(l) < 120), "")
                    if href not in jobs:
                        found += 1
                        jobs[href] = {"id": m.group(2), "title": title[:160], "company": company, "location": loc,
                                      "remote": "remote" in (ctext or "").lower(), "url": href.split("?")[0], "posted": None}
                    if len(samples) < 3:
                        samples.append(ctext[:300])
                debug["terms"][term] = {"new_jobs": found, "samples": samples}
            if not jobs:
                err = "no job cards found for any search"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:160]}"
        finally:
            b.close()
    try:
        from pathlib import Path
        out = Path(__file__).resolve().parents[1] / "results" / f"board_debug_{board['key']}.json"
        out.write_text(json.dumps(debug, indent=1, default=str)[:200000])
    except Exception:  # noqa: BLE001
        pass
    return list(jobs.values()), err
