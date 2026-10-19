"""Work out which job system each company's career page uses.

Writes data/resolved.json: {company_id: {adapter config..., "resolved_at": date, "via": how}}.
Run with --all to redo everything, otherwise only companies that are new or still unresolved
(unresolved ones are retried at most once a week)."""
import argparse
import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests

sys.path.insert(0, str(Path(__file__).parent))
from adapters import detect, detect_phenom  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
JOBLINK = re.compile(r"(search|view|open|all|current|browse|find)[\s-]*(jobs|positions|openings|roles|opportunities|careers)|job ?search|join (our|the) team|career opportunities|see (all )?(jobs|openings)", re.I)


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def static_probe(s, url):
    r = s.get(url, timeout=25, allow_redirects=True)
    text = r.url + "\n" + r.text
    cands = detect(text)
    if cands:
        return cands[0], "page"
    ph = detect_phenom(r.text, r.url)
    if ph:
        return ph, "page"
    links = []
    for m in re.finditer(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', r.text, re.S | re.I):
        href, inner = m.group(1), re.sub("<[^>]+>", " ", m.group(2))
        if JOBLINK.search(inner) or re.search(r"/(jobs|search-jobs|job-search|openings|positions)(/|\?|$)", href, re.I):
            links.append(urljoin(r.url, href))
    for link in list(dict.fromkeys(links))[:3]:
        c2 = detect(link)
        if c2:
            return c2[0], "link"
        try:
            r2 = s.get(link, timeout=25, allow_redirects=True)
        except Exception:
            continue
        c2 = detect(r2.url + "\n" + r2.text)
        if c2:
            return c2[0], "link-page"
        ph = detect_phenom(r2.text, r2.url)
        if ph:
            return ph, "link-page"
    return None, r.url


def browser_probe(urls):
    """Render pages that hid their job system behind JavaScript; watch network calls too."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {}
    found = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(user_agent=UA)
        for cid, url in urls:
            page = ctx.new_page()
            reqs = []
            page.on("request", lambda rq: reqs.append(rq.url))
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
                html_text = page.content()
                hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href + ' ' + (e.innerText||''))")
                blob = page.url + "\n" + "\n".join(reqs) + "\n" + "\n".join(hrefs) + "\n" + html_text
                c = detect(blob)
                if c:
                    found[cid] = (c[0], "browser")
                else:
                    ph = detect_phenom(html_text, page.url)
                    found[cid] = (ph, "browser") if ph else ({"adapter": "pagewatch", "url": page.url}, "browser-none")
            except Exception as e:  # noqa: BLE001
                found[cid] = ({"adapter": "pagewatch", "url": url}, f"browser-error: {type(e).__name__}")
            finally:
                page.close()
        b.close()
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    companies = list(csv.DictReader(open(ROOT / "data/companies.csv", encoding="utf-8")))
    path = ROOT / "data/resolved.json"
    resolved = json.loads(path.read_text()) if path.exists() else {}
    today = date.today()
    week_ago = (today - timedelta(days=7)).isoformat()

    todo = []
    for c in companies:
        r = resolved.get(c["id"])
        if args.all or r is None or r.get("url_source") != c["url"]:
            todo.append(c)
        elif r.get("adapter") == "pagewatch" and r.get("resolved_at", "") <= week_ago:
            todo.append(c)
    print(f"discover: {len(todo)} of {len(companies)} companies to resolve")

    def work(c):
        cands = detect(c["url"])
        if cands:
            return c, cands[0], "url"
        try:
            got, via = static_probe(session(), c["url"])
            return c, got, via if got else "unresolved:" + str(via)
        except Exception as e:  # noqa: BLE001
            return c, None, f"error: {type(e).__name__}"

    pending_browser = []
    with ThreadPoolExecutor(16) as ex:
        for c, got, via in ex.map(work, todo):
            if got:
                resolved[c["id"]] = {**got, "via": via, "resolved_at": today.isoformat(), "url_source": c["url"]}
            else:
                final = via.split(":", 1)[1] if via.startswith("unresolved:") else c["url"]
                pending_browser.append((c["id"], final if final.startswith("http") else c["url"]))
                resolved[c["id"]] = {"adapter": "pagewatch", "url": c["url"], "via": via,
                                     "resolved_at": today.isoformat(), "url_source": c["url"]}

    if pending_browser and not args.no_browser:
        print(f"discover: rendering {len(pending_browser)} pages in a browser")
        for cid, (cfg, via) in browser_probe(pending_browser).items():
            src = resolved[cid]["url_source"]
            resolved[cid] = {**cfg, "via": via, "resolved_at": today.isoformat(), "url_source": src}

    path.write_text(json.dumps(resolved, indent=1, sort_keys=True))
    counts = {}
    for r in resolved.values():
        counts[r["adapter"]] = counts.get(r["adapter"], 0) + 1
    print("discover: adapters", json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
