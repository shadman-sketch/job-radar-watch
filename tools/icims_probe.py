"""Diagnostic only: show what an iCIMS search page returns in a headless browser."""
import re, sys
from playwright.sync_api import sync_playwright
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
with sync_playwright() as p:
    b = p.chromium.launch(); ctx = b.new_context(user_agent=UA)
    for host in sys.argv[1:]:
        for url in (f"https://{host}/jobs/search?ss=1&in_iframe=1&searchKeyword=technician", f"https://{host}/jobs/search?ss=1&searchKeyword=technician"):
            pg = ctx.new_page()
            try:
                r = pg.goto(url, timeout=30000, wait_until="domcontentloaded"); pg.wait_for_timeout(4000)
                html = pg.content()
                print("==", url, r.status if r else None, pg.url, len(html), "frames:", [f.url for f in pg.frames][:4])
                print("  job hrefs:", re.findall(r'href="([^"]*/jobs/\d+[^"]*)"', html)[:4])
                for f in pg.frames[1:3]:
                    fh = f.content(); print("  frame", f.url, len(fh), re.findall(r'href="([^"]*/jobs/\d+[^"]*)"', fh)[:4])
                print("  text:", " ".join(pg.inner_text("body").split())[:400])
            except Exception as e:
                print("== ERR", url, e)
            pg.close()
    b.close()
