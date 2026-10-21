"""Daily check: pull jobs from every watched company, keep the ones that fit, report what's new.

Outputs (all under results/):
  latest.json       new matching jobs found in this run (what the dashboard imports)
  history/DATE.json same, kept per day
  seen.json         every matching job ever reported, so nothing is reported twice
  status.json       per-company health: which system, how many jobs, errors
  summary.md        human-readable run summary
"""
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent))
from adapters import ADAPTERS  # noqa: E402
from match import location_kind, other_place, parse_date, role_band, title_ok  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "results"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SETTINGS = json.loads((ROOT / "data/settings.json").read_text()) if (ROOT / "data/settings.json").exists() else {}
FRESH_DAYS = int(SETTINGS.get("fresh_days", 14))
NAV_WORDS = re.compile(r"^(jobs?|careers?|search( jobs)?|view (all )?(jobs|openings)|apply( now)?|join (us|our team)|"
                       r"open positions|all jobs|benefits|culture|students?|internships?|life at .*|our teams?)$", re.I)


def req_id(url):
    """Requisition / posting id pulled from common job URLs, or None."""
    u = url or ""
    for pat in [r"_(R-?\d{3,})", r"/(R-?\d{4,})\b", r"[?&]gh_jid=(\d+)", r"greenhouse\.io/[^/]+/jobs/(\d+)",
                r"/jobs/(\d{4,})", r"lever\.co/[^/]+/([0-9a-f-]{36})", r"ashbyhq\.com/[^/]+/([0-9a-f-]{36})",
                r"/job/(\d{4,})", r"jobId=([\w-]+)", r"/(JR\d+)", r"/([0-9a-f-]{36})"]:
        m = re.search(pat, u, re.I)
        if m:
            return m.group(1).upper()
    return None


def canon_url(url):
    u = (url or "").split("#")[0]
    keep = re.findall(r"[?&]((?:gh_jid|jobId|jobid|id)=[^&]+)", u)
    u = u.split("?")[0].rstrip("/").lower()
    return u + ("?" + "&".join(keep) if keep else "")


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/html;q=0.9", "Accept-Language": "en-US,en;q=0.9"})
    return s


def fetch(company, cfg):
    fn = ADAPTERS.get(cfg.get("adapter"))
    if not fn:
        return company, cfg, None, "no adapter"
    last = None
    for attempt in range(2):
        try:
            return company, cfg, fn(session(), cfg), None
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:160]}"
            time.sleep(2 + attempt * 3)
    return company, cfg, None, last


JOB_HREF = re.compile(r"/(jobs?|careers?|positions?|openings?|requisitions?|vacanc\w*|opportunit\w*|apply)(/|\?|=|-|$)|jobid=|job_id=|gh_jid=|/req", re.I)
ICIMS_TERMS = ["help desk", "desktop support", "IT support", "technician"]


def _links_to_jobs(links):
    jobs = []
    for href, text in links:
        title = (text or "")[:160].replace("›", "").strip()
        if not title or len(title) > 120 or "@" in title or href.startswith("mailto:"):
            continue
        if not JOB_HREF.search(href) or len(title) < 6 or NAV_WORDS.match(title):
            continue
        jobs.append({"id": href, "title": title, "location": title, "remote": "remote" in title.lower(),
                     "url": href, "posted": None})
    return jobs


def pagewatch(items):
    """Render career pages that have no feed and pick out links that look like matching jobs.
    items: (company_id, url, icims_host_or_None)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {it[0]: (None, "playwright not installed") for it in items}
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(user_agent=UA)
        for cid, url, icims_host in items:
            page = ctx.new_page()
            try:
                jobs = []
                targets = ([f"https://{icims_host}/jobs/search?ss=1&in_iframe=1&searchKeyword={quote(t)}" for t in ICIMS_TERMS]
                           if icims_host else [url])
                for target in targets:
                    page.goto(target, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    if "confirm you are human" in (page.inner_text("body") or "").lower()[:400]:
                        raise RuntimeError("blocked by bot check")
                    if icims_host:
                        rows = page.eval_on_selector_all(
                            "a[href*='/jobs/']",
                            "els => els.map(e => [e.href, (e.innerText||'').trim().replace(/\\s+/g,' '),"
                            " ((e.closest('.row, li, tr, .iCIMS_JobsTable > div')||e.parentElement).innerText||'').replace(/\\s+/g,' ')])")
                        for href, title, ctxt in rows:
                            m = re.search(r"/jobs/(\d+)/", href)
                            if m and title:
                                jobs.append({"id": m.group(1), "title": title[:140], "location": ctxt[:300],
                                             "remote": "remote" in ctxt.lower(), "url": href.split("?")[0], "posted": None})
                    else:
                        links = page.eval_on_selector_all(
                            "a[href]", "els => els.map(e => [e.href, (e.innerText||'').trim().replace(/\\s+/g,' ')])")
                        jobs.extend(_links_to_jobs(links))
                uniq = {j["id"]: j for j in jobs}
                out[cid] = (list(uniq.values()), None)
            except Exception as e:  # noqa: BLE001
                out[cid] = (None, str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}")
            finally:
                page.close()
        b.close()
    return out


def main():
    RES.mkdir(exist_ok=True)
    (RES / "history").mkdir(exist_ok=True)
    companies = {c["id"]: c for c in csv.DictReader(open(ROOT / "data/companies.csv", encoding="utf-8"))}
    resolved = json.loads((ROOT / "data/resolved.json").read_text())
    seen_path = RES / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {"jobs": {}, "companies": {}}
    today = date.today()
    now = datetime.now(timezone.utc)
    cutoff = (today - timedelta(days=FRESH_DAYS)).isoformat()

    feed_items, page_items = [], []
    for cid, c in companies.items():
        cfg = resolved.get(cid) or {"adapter": "pagewatch", "url": c["url"]}
        if cfg["adapter"] == "pagewatch":
            page_items.append((cid, cfg.get("url") or c["url"], None))
        elif cfg["adapter"] == "icims":
            page_items.append((cid, c["url"], cfg["host"]))
        else:
            feed_items.append((c, cfg))

    results = {}
    started = time.time()
    with ThreadPoolExecutor(20) as ex:
        for c, cfg, jobs, err in ex.map(lambda a: fetch(*a), feed_items):
            results[c["id"]] = (cfg, jobs, err)
    print(f"feeds: {len(feed_items)} companies in {time.time() - started:.0f}s")
    failed = [cid for cid, (cfg, jobs, err) in results.items() if err]
    for cid in failed:  # a feed that failed today still gets a page check
        page_items.append((cid, companies[cid]["url"], None))

    started = time.time()
    icims = {cid for cid, _, h in page_items if h}
    for cid, (jobs, err) in pagewatch(page_items).items():
        prev = results.get(cid)
        if prev and prev[2] and err is None:  # fallback worked for a failed feed
            results[cid] = ({"adapter": prev[0]["adapter"] + "+page"}, jobs, None)
        elif not prev:
            results[cid] = ({"adapter": "icims" if cid in icims else "pagewatch"}, jobs, err)
    print(f"pages: {len(page_items)} companies in {time.time() - started:.0f}s")

    # Multi-company job boards (data/boards.json), e.g. the a16z portfolio board.
    boards_path = ROOT / "data/boards.json"
    if boards_path.exists():
        from boards import fetch_board
        for bd in json.loads(boards_path.read_text()):
            started = time.time()
            bid = "board:" + bd["key"]
            companies[bid] = {"id": bid, "company": bd["name"], "priority": "B", "sector": "Job board", "board": bd["name"]}
            jobs, err = fetch_board(bd)
            results[bid] = ({"adapter": "board"}, jobs, err)
            print(f"board {bd['name']}: {len(jobs or [])} jobs in {time.time() - started:.0f}s {err or ''}")

    prev_status = json.loads((RES / "status.json").read_text()) if (RES / "status.json").exists() else {}
    latest, status, open_now = [], {}, []
    canon_seen = {}
    dupes_removed = 0
    for cid, (cfg, jobs, err) in results.items():
        c = companies[cid]
        first_time = cid not in seen["companies"]
        adapter = cfg.get("adapter")
        page_only = adapter in ("pagewatch", "icims") or str(adapter or "").endswith("+page")
        # A page that loads but shows no job listings is not a successful extraction.
        extracted = err is None and (not page_only or len(jobs or []) > 0)
        state = "failed" if err else ("ok" if extracted else "no_listings_found")
        last_ok = today.isoformat() if extracted else (prev_status.get(cid) or {}).get("last_success")
        st = {"company": c["company"], "adapter": adapter, "ok": err is None, "state": state,
              "extracted": extracted, "last_success": last_ok, "error": err, "jobs": len(jobs or []),
              "role_matches": 0, "matches": 0, "dupes": 0, "priority": c.get("priority")}
        for j in jobs or []:
            title = (j.get("title") or "").strip()
            if not title_ok(title):
                continue
            st["role_matches"] += 1
            kind = location_kind(j.get("location"), j.get("remote"))
            if cfg.get("adapter") in ("pagewatch", "icims") or str(cfg.get("adapter", "")).endswith("+page"):
                if kind is None and not other_place(j.get("location") or title):
                    kind = "unknown"
            if kind is None:
                continue
            st["matches"] += 1
            co_name = j.get("company") or c["company"]
            cu = canon_url(j.get("url"))
            ck = cu or f"{co_name}|{title}|{j.get('location')}".lower()
            if ck in canon_seen:
                st["dupes"] += 1
                dupes_removed += 1
                continue
            canon_seen[ck] = cid
            key = f"{cid}:{j.get('id') or j.get('url')}"
            first_seen = seen["jobs"].get(key) or today.isoformat()
            # Posted dates only come from the employer's own system or board; page checks have none.
            posted_v = None if page_only else parse_date(j.get("posted"))
            open_now.append({"company": co_name, "board": c.get("board"), "priority": c["priority"], "title": title,
                             "role_band": role_band(title), "location": j.get("location") or "", "location_kind": kind,
                             "url": j.get("url"), "canonical_url": cu, "req_id": req_id(j.get("url")),
                             "posted": posted_v, "posted_source": None if page_only else adapter,
                             "first_seen": first_seen, "last_verified": today.isoformat(), "system": adapter})
            if key in seen["jobs"]:
                continue
            posted = posted_v
            if posted and posted < cutoff:
                seen["jobs"][key] = today.isoformat()  # too old: remember it, don't report it
                continue
            seen["jobs"][key] = today.isoformat()
            latest.append({
                "company_id": cid, "company": co_name, "board": c.get("board"), "priority": c["priority"], "sector": c["sector"],
                "title": title, "location": j.get("location") or "", "location_kind": kind,
                "remote": bool(j.get("remote")), "url": j.get("url"), "posted": posted,
                "baseline": first_time and not posted, "system": cfg.get("adapter"),
                "role_band": role_band(title), "req_id": req_id(j.get("url")), "canonical_url": cu,
                "first_seen": today.isoformat(), "last_verified": today.isoformat(),
            })
        if extracted:
            seen["companies"][cid] = today.isoformat()
        status[cid] = st

    order = {"dfw": 0, "remote": 1, "tx_metro": 2, "texas": 3, "unknown": 4}
    latest.sort(key=lambda x: (order.get(x["location_kind"], 9), x["priority"], x["company"]))
    stamp = now.strftime("%Y-%m-%dT%H%M%S")
    run_payload = {"run_at": now.isoformat(timespec="seconds"), "date": today.isoformat(), "count": len(latest), "jobs": latest}
    (RES / "history" / f"{stamp}.json").write_text(json.dumps(run_payload, indent=1))
    # latest.json = new matches from every run in the last 36 hours, so a mid-day run never hides an earlier find.
    # Older runs are re-screened with today's rules.
    window = (now - timedelta(days=FRESH_DAYS)).strftime("%Y-%m-%dT%H%M%S")
    merged = {}
    for f in sorted((RES / "history").glob("*.json")):
        if f.stem < window and f.stem[:10] != today.isoformat():
            continue
        for j in json.loads(f.read_text()).get("jobs", []):
            if not title_ok(j["title"]):
                continue
            if j["system"] == "pagewatch" and not JOB_HREF.search(j.get("url") or ""):
                continue
            kind = location_kind(j.get("location") or j["title"], j.get("remote"))
            if kind is None and (j["system"] != "pagewatch" or other_place(j.get("location") or j["title"])):
                continue
            if j.get("posted") and j["posted"] < cutoff:
                continue
            j.setdefault("role_band", role_band(j["title"]))
            merged[canon_url(j.get("url")) or j["title"] + j["company"]] = j
    latest = list(merged.values())
    latest.sort(key=lambda x: (order.get(x["location_kind"], 9), x["priority"], x["company"]))
    payload = {"run_at": now.isoformat(timespec="seconds"), "date": today.isoformat(), "count": len(latest), "jobs": latest}
    (RES / "latest.json").write_text(json.dumps(payload, indent=1))
    seen_path.write_text(json.dumps(seen))
    open_now.sort(key=lambda x: (order.get(x["location_kind"], 9), x["priority"], x["company"]))
    (RES / "open_now.json").write_text(json.dumps({"date": today.isoformat(), "count": len(open_now), "jobs": open_now}, indent=1))
    (RES / "status.json").write_text(json.dumps(status, indent=1, sort_keys=True))

    ok = sum(1 for s in status.values() if s["extracted"])
    no_list = [s for s in status.values() if s["state"] == "no_listings_found"]
    failed = [s for s in status.values() if s["state"] == "failed"]
    by_adapter = {}
    for s in status.values():
        a = by_adapter.setdefault(s["adapter"], [0, 0, 0])
        a[0] += 1
        a[1] += 1 if s["extracted"] else 0
        a[2] += s["jobs"]
    lines = [f"# Watchlist run {today.isoformat()}", "",
             f"- Companies checked: {ok} of {len(status)} answered",
             f"- Sources with listings extracted: {ok} · page loaded but no listings found: {len(no_list)} · failed: {len(failed)}",
             f"- Duplicates removed across sources: {dupes_removed}",
             f"- Age window: {FRESH_DAYS} days (jobs with a verified older post date are kept in open_now.json only)",
             f"- Jobs read: {sum(s['jobs'] for s in status.values())}",
             f"- Jobs matching your roles (any location): {sum(s['role_matches'] for s in status.values())}",
             f"- ...of those in DFW, remote US or Austin/Houston/San Antonio: {sum(s['matches'] for s in status.values())}",
             f"- New matching jobs: {len(latest)}",
             f"- All open matching jobs at watched companies: {len(open_now)} (see open_now.json)", "", "| System | Companies | Listings extracted | Jobs seen |", "|---|---|---|---|"]
    for a, (n, k, jn) in sorted(by_adapter.items(), key=lambda x: -x[1][0]):
        lines.append(f"| {a} | {n} | {k} | {jn} |")
    lines += ["", "## Needs attention (priority 1-2, no listings extracted)", ""]
    for s_ in sorted(no_list + failed, key=lambda x: (str(x.get("priority")), x["company"])):
        if str(s_.get("priority")) in ("1", "2", "B"):
            lines.append(f"- {s_['company']} ({s_['adapter']}): {s_['state']}{' - ' + s_['error'] if s_.get('error') else ''}"
                         f" · last success: {s_.get('last_success') or 'never'}")
    lines += ["", "## New matches", ""]
    for j in latest[:200]:
        lines.append(f"- **{j['title']}** — {j['company']} ({j['location'] or j['location_kind']}) {j['posted'] or ''} {j['url']}")
    (RES / "summary.md").write_text("\n".join(lines) + "\n")
    print(f"done: {ok}/{len(status)} companies answered, {len(latest)} new matches")


if __name__ == "__main__":
    main()
