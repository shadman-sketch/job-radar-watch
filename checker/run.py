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
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from adapters import ADAPTERS  # noqa: E402
from match import location_kind, parse_date, title_ok  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "results"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
FRESH_DAYS = 3


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


def pagewatch(items):
    """Render career pages that have no feed and pick out links that look like matching jobs."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {cid: (None, "playwright not installed") for cid, _ in items}
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(user_agent=UA)
        for cid, url in items:
            page = ctx.new_page()
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3500)
                links = page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => [e.href, (e.innerText||'').trim().replace(/\\s+/g,' ')])")
                jobs = []
                for href, text in links:
                    title = text[:140]
                    if title and title_ok(title):
                        jobs.append({"id": href, "title": title, "location": "", "remote": False, "url": href, "posted": None})
                out[cid] = (jobs, None)
            except Exception as e:  # noqa: BLE001
                out[cid] = (None, f"{type(e).__name__}")
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
            page_items.append((cid, cfg.get("url") or c["url"]))
        else:
            feed_items.append((c, cfg))

    results = {}
    started = time.time()
    with ThreadPoolExecutor(20) as ex:
        for c, cfg, jobs, err in ex.map(lambda a: fetch(*a), feed_items):
            results[c["id"]] = (cfg, jobs, err)
    print(f"feeds: {len(feed_items)} companies in {time.time() - started:.0f}s")

    started = time.time()
    for cid, (jobs, err) in pagewatch(page_items).items():
        results[cid] = ({"adapter": "pagewatch"}, jobs, err)
    print(f"pages: {len(page_items)} companies in {time.time() - started:.0f}s")

    latest, status = [], {}
    for cid, (cfg, jobs, err) in results.items():
        c = companies[cid]
        first_time = cid not in seen["companies"]
        st = {"company": c["company"], "adapter": cfg.get("adapter"), "ok": err is None,
              "error": err, "jobs": len(jobs or []), "matches": 0}
        for j in jobs or []:
            title = (j.get("title") or "").strip()
            if not title_ok(title):
                continue
            kind = location_kind(j.get("location"), j.get("remote"))
            if cfg.get("adapter") == "pagewatch":
                kind = kind or "unknown"
            if kind is None:
                continue
            st["matches"] += 1
            key = f"{cid}:{j.get('id') or j.get('url')}"
            if key in seen["jobs"]:
                continue
            posted = parse_date(j.get("posted"))
            if posted and posted < cutoff:
                seen["jobs"][key] = today.isoformat()  # too old: remember it, don't report it
                continue
            seen["jobs"][key] = today.isoformat()
            latest.append({
                "company_id": cid, "company": c["company"], "priority": c["priority"], "sector": c["sector"],
                "title": title, "location": j.get("location") or "", "location_kind": kind,
                "remote": bool(j.get("remote")), "url": j.get("url"), "posted": posted,
                "baseline": first_time and not posted, "system": cfg.get("adapter"),
            })
        if err is None:
            seen["companies"][cid] = today.isoformat()
        status[cid] = st

    order = {"dfw": 0, "remote": 1, "texas": 2, "unknown": 3}
    latest.sort(key=lambda x: (order.get(x["location_kind"], 9), x["priority"], x["company"]))
    payload = {"run_at": now.isoformat(timespec="seconds"), "date": today.isoformat(), "count": len(latest), "jobs": latest}
    (RES / "latest.json").write_text(json.dumps(payload, indent=1))
    (RES / "history" / f"{today.isoformat()}.json").write_text(json.dumps(payload, indent=1))
    seen_path.write_text(json.dumps(seen))
    (RES / "status.json").write_text(json.dumps(status, indent=1, sort_keys=True))

    ok = sum(1 for s in status.values() if s["ok"])
    by_adapter = {}
    for s in status.values():
        a = by_adapter.setdefault(s["adapter"], [0, 0, 0])
        a[0] += 1
        a[1] += 1 if s["ok"] else 0
        a[2] += s["jobs"]
    lines = [f"# Watchlist run {today.isoformat()}", "",
             f"- Companies checked: {ok} of {len(status)} answered",
             f"- New matching jobs: {len(latest)}", "", "| System | Companies | Answered | Jobs seen |", "|---|---|---|---|"]
    for a, (n, k, jn) in sorted(by_adapter.items(), key=lambda x: -x[1][0]):
        lines.append(f"| {a} | {n} | {k} | {jn} |")
    lines += ["", "## New matches", ""]
    for j in latest[:200]:
        lines.append(f"- **{j['title']}** — {j['company']} ({j['location'] or j['location_kind']}) {j['posted'] or ''} {j['url']}")
    (RES / "summary.md").write_text("\n".join(lines) + "\n")
    print(f"done: {ok}/{len(status)} companies answered, {len(latest)} new matches")


if __name__ == "__main__":
    main()
