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


def fetch_board(board):
    """board: {"name", "url", "host"}. Returns (jobs, error)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "playwright not installed"
    host = board["host"]
    captured = {}
    jobs, err = [], None
    debug = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(user_agent=UA)
        page = ctx.new_page()

        def on_request(rq):
            if captured or rq.method != "POST":
                return
            if re.search(r"/api-boards/search-jobs|search-jobs|/jobs/search|graphql", rq.url):
                captured.update({"url": rq.url, "body": rq.post_data or "", "headers": rq.headers})

        page.on("request", on_request)
        net = []

        def on_response(resp):
            try:
                rq = resp.request
                if rq.resource_type in ("xhr", "fetch") and len(net) < 60:
                    net.append({"url": rq.url[:300], "method": rq.method, "status": resp.status,
                                "ctype": resp.headers.get("content-type", ""), "body": (rq.post_data or "")[:1500]})
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_response)
        try:
            page.goto(board["url"], timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
            debug = {"captured_url": captured.get("url"), "captured_body": captured.get("body")}
            debug["network"] = net
            try:
                html = page.content()
                debug["html_markers"] = {k: (k in html) for k in ["getro", "consider", "__NEXT_DATA__", "api-boards", "algolia", "typesense", "graphql"]}
                m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
                debug["next_data_head"] = m.group(1)[:3000] if m else None
            except Exception:  # noqa: BLE001
                pass
            if captured and captured["body"].strip().startswith("{"):
                body = json.loads(captured["body"])
                meta = body.setdefault("meta", {})
                meta["size"] = 100
                meta.pop("sequence", None)
                headers = {k: v for k, v in captured["headers"].items()
                           if k.lower() in ("content-type", "accept", "x-requested-with")}
                headers.setdefault("content-type", "application/json")
                seen = set()
                for _ in range(MAX_PAGES):
                    r = page.request.post(captured["url"], data=json.dumps(body), headers=headers, timeout=45000)
                    if not r.ok:
                        err = f"HTTP {r.status} from board search"
                        break
                    raw = r.json()
                    if "first_response_keys" not in debug:
                        debug["first_response_keys"] = list(raw.keys()) if isinstance(raw, dict) else str(type(raw))
                        debug["first_response_meta"] = {k: v for k, v in raw.items() if not isinstance(v, list)} if isinstance(raw, dict) else None
                        lst = raw.get("jobs") if isinstance(raw, dict) else None
                        debug["first_job_sample"] = lst[0] if lst else None
                    batch, rmeta = _parse_jobs(raw, host)
                    new = [j for j in batch if j["id"] not in seen]
                    if not new:
                        break
                    for j in new:
                        seen.add(j["id"])
                    jobs.extend(new)
                    seq = rmeta.get("sequence")
                    debug["pages"] = debug.get("pages", 0) + 1
                    debug["last_batch"] = len(batch)
                    debug["last_meta"] = rmeta
                    if not seq or len(batch) < meta["size"]:
                        break
                    meta["sequence"] = seq
            else:
                # Fallback: read job cards on the page after loading a few more.
                for _ in range(15):
                    more = page.query_selector("button:has-text('Show more'), button:has-text('Load more')")
                    if not more:
                        break
                    more.click()
                    page.wait_for_timeout(1500)
                cards = page.eval_on_selector_all(
                    f"a[href*='/jobs/']",
                    "els => els.map(e => [e.href, ((e.closest('li, article, [class*=job]')||e).innerText||'').trim()])")
                for href, text in cards:
                    if not re.search(r"/jobs/[^/]+/[^/]+--", href):
                        continue
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    if not lines:
                        continue
                    jobs.append({"id": href, "title": lines[0], "company": lines[1] if len(lines) > 1 else "",
                                 "location": " / ".join(lines[2:4]), "remote": "remote" in text.lower(),
                                 "url": href, "posted": None})
                if not jobs:
                    err = "no search API captured and no job cards found"
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
    return jobs, err
