"""One fetcher per job system. Each returns a list of raw job dicts:
{id, title, location, remote, url, posted}  (posted is raw; match.parse_date normalizes it)."""
import html
import json
import re
from urllib.parse import urljoin, urlparse, quote

SEARCH_TERMS = ["help desk", "service desk", "desktop support", "IT support", "IT technician",
                "technical support", "IT specialist", "end user", "deskside", "security technician",
                "SOC analyst", "access administrator", "NOC technician", "data center technician",
                "IT asset", "ServiceNow", "IT intern", "IT apprentice"]
TIMEOUT = 25


def _get(s, url, **kw):
    r = s.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def _post(s, url, payload, **kw):
    r = s.post(url, json=payload, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# ---------- public JSON boards (return everything; filtering happens later) ----------

def greenhouse(s, c):
    t = c["token"]
    data = _get(s, f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs").json()
    out = []
    for j in data.get("jobs", []):
        out.append({
            "id": j.get("id"), "title": j.get("title"),
            "location": (j.get("location") or {}).get("name", ""),
            "remote": False, "url": j.get("absolute_url"),
            "posted": j.get("first_published"),
        })
    return out


def lever(s, c):
    t = c["token"]
    data = _get(s, f"https://api.lever.co/v0/postings/{t}?mode=json").json()
    out = []
    for j in data if isinstance(data, list) else []:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location", "")]
        out.append({
            "id": j.get("id"), "title": j.get("text"),
            "location": " / ".join([l for l in locs if l]),
            "remote": (j.get("workplaceType") == "remote"),
            "url": j.get("hostedUrl"), "posted": j.get("createdAt"),
        })
    return out


def ashby(s, c):
    t = c["token"]
    data = _get(s, f"https://api.ashbyhq.com/posting-api/job-board/{t}").json()
    out = []
    for j in data.get("jobs", []):
        locs = [j.get("location") or ""] + [x.get("location", "") for x in (j.get("secondaryLocations") or [])]
        out.append({
            "id": j.get("id") or j.get("jobUrl"), "title": j.get("title"),
            "location": " / ".join([l for l in locs if l]),
            "remote": bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            "url": j.get("jobUrl"), "posted": j.get("publishedAt"),
        })
    return out


def smartrecruiters(s, c):
    t = c["token"]
    out, offset = [], 0
    while offset < 1000:
        data = _get(s, f"https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=100&offset={offset}").json()
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            out.append({
                "id": j.get("id"), "title": j.get("name"),
                "location": ", ".join([x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x]),
                "remote": bool(loc.get("remote")),
                "url": f"https://jobs.smartrecruiters.com/{t}/{j.get('id')}", "posted": j.get("releasedDate"),
            })
        offset += 100
        if offset >= data.get("totalFound", 0) or not items:
            break
    return out


def workable(s, c):
    t = c["token"]
    out, token = [], None
    for _ in range(10):
        body = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
        if token:
            body["token"] = token
        data = _post(s, f"https://apply.workable.com/api/v3/accounts/{t}/jobs", body).json()
        for j in data.get("results", []):
            loc = j.get("location") or {}
            out.append({
                "id": j.get("shortcode"), "title": j.get("title"),
                "location": ", ".join([x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x]),
                "remote": bool(j.get("remote")) or j.get("workplace") == "remote",
                "url": f"https://apply.workable.com/{t}/j/{j.get('shortcode')}/", "posted": j.get("published"),
            })
        token = data.get("nextPage")
        if not token:
            break
    return out


def recruitee(s, c):
    t = c["token"]
    data = _get(s, f"https://{t}.recruitee.com/api/offers/").json()
    return [{
        "id": j.get("id"), "title": j.get("title"),
        "location": j.get("location") or ", ".join([x for x in [j.get("city"), j.get("country")] if x]),
        "remote": bool(j.get("remote")), "url": j.get("careers_url"), "posted": j.get("published_at"),
    } for j in data.get("offers", [])]


def breezy(s, c):
    t = c["token"]
    data = _get(s, f"https://{t}.breezy.hr/json").json()
    out = []
    for j in data if isinstance(data, list) else []:
        loc = j.get("location") or {}
        out.append({
            "id": j.get("id"), "title": j.get("name"),
            "location": loc.get("name") if isinstance(loc, dict) else str(loc),
            "remote": bool(isinstance(loc, dict) and loc.get("is_remote")),
            "url": j.get("url"), "posted": j.get("published_date"),
        })
    return out


def rippling(s, c):
    t = c["token"]
    data = _get(s, f"https://api.rippling.com/platform/api/ats/v1/board/{t}/jobs").json()
    out = []
    for j in data if isinstance(data, list) else []:
        loc = j.get("workLocation") or {}
        out.append({
            "id": j.get("uuid"), "title": j.get("name"),
            "location": loc.get("label", "") if isinstance(loc, dict) else str(loc),
            "remote": "remote" in json.dumps(loc).lower(), "url": j.get("url"), "posted": None,
        })
    return out


# ---------- keyword-search systems (query a few terms instead of downloading everything) ----------

def workday(s, c):
    host, tenant, site = c["host"], c["tenant"], c["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    seen, out = set(), []
    for term in SEARCH_TERMS:
        data = _post(s, api, {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term},
                     headers={"Accept": "application/json"}).json()
        for j in data.get("jobPostings", []) or []:
            path = j.get("externalPath")
            if not path or path in seen:
                continue
            seen.add(path)
            out.append({
                "id": path, "title": j.get("title"), "location": j.get("locationsText", ""),
                "remote": "remote" in (j.get("locationsText", "") + " " + j.get("remoteType", "")).lower(),
                "url": f"https://{host}/{site}{path}", "posted": j.get("postedOn"),
            })
    return out


def oracle(s, c):
    base, site = c["base"], c["site"]
    seen, out = set(), []
    for term in SEARCH_TERMS[:6]:
        url = (f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
               f"&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},"
               f"keyword=%22{quote(term)}%22,limit=25,sortBy=POSTING_DATES_DESC")
        data = _get(s, url).json()
        for item in data.get("items", []):
            for j in item.get("requisitionList", []) or []:
                jid = j.get("Id")
                if jid in seen:
                    continue
                seen.add(jid)
                out.append({
                    "id": jid, "title": j.get("Title"),
                    "location": j.get("PrimaryLocation", ""),
                    "remote": "remote" in (j.get("WorkplaceType", "") or "").lower(),
                    "url": f"{base}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}",
                    "posted": j.get("PostedDate"),
                })
    return out


def icims(s, c):
    host = c["host"]
    seen, out = set(), []
    for term in SEARCH_TERMS[:6]:
        r = _get(s, f"https://{host}/jobs/search?ss=1&searchKeyword={quote(term)}&in_iframe=1")
        for m in re.finditer(r'href="(https://[^"]+/jobs/(\d+)/[^"]+/job[^"]*)"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{3,140})<', r.text):
            url, jid, title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
            if jid in seen:
                continue
            seen.add(jid)
            tail = r.text[m.end(): m.end() + 1500]
            lm = re.search(r'(?:Job Locations?|header left)[^<]*</[^>]+>\s*(?:<[^>]+>\s*)*([^<]{2,120})<', tail)
            out.append({"id": jid, "title": title, "location": html.unescape(lm.group(1)).strip() if lm else "",
                        "remote": False, "url": url.split("?")[0], "posted": None})
    return out


def jobvite(s, c):
    t = c["token"]
    r = _get(s, f"https://jobs.jobvite.com/{t}/jobs")
    out = []
    for m in re.finditer(r'<td class="jv-job-list-name">\s*<a href="([^"]+)">([^<]+)</a>\s*</td>\s*<td class="jv-job-list-location">\s*([^<]*?)\s*</td>', r.text, re.S):
        href, title, loc = m.groups()
        out.append({"id": href, "title": html.unescape(title).strip(), "location": " ".join(html.unescape(loc).split()),
                    "remote": False, "url": urljoin("https://jobs.jobvite.com/", href), "posted": None})
    if not out:  # newer layout
        for m in re.finditer(r'<a[^>]+href="(/[^"]*?/job/[^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
            href, inner = m.groups()
            title = " ".join(html.unescape(re.sub("<[^>]+>", " ", inner)).split())
            if title:
                out.append({"id": href, "title": title[:140], "location": "", "remote": False,
                            "url": urljoin("https://jobs.jobvite.com/", href), "posted": None})
    return out


def ultipro(s, c):
    base = c["base"]  # https://recruiting.ultipro.com/XXX/JobBoard/guid
    seen, out = set(), []
    for term in SEARCH_TERMS[:5]:
        body = {"opportunitySearch": {"Top": 50, "Skip": 0, "QueryString": term, "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}], "Filters": []}}
        data = _post(s, f"{base}/JobBoardView/LoadSearchResults", body).json()
        for j in data.get("opportunities", []):
            jid = j.get("Id")
            if jid in seen:
                continue
            seen.add(jid)
            locs = []
            for l in j.get("Locations", []) or []:
                a = l.get("Address") or {}
                locs.append(", ".join([x for x in [a.get("City"), (a.get("State") or {}).get("Code")] if x]) or l.get("LocalizedDescription", ""))
            out.append({"id": jid, "title": j.get("Title"), "location": " / ".join(locs),
                        "remote": "remote" in json.dumps(j.get("Locations", [])).lower(),
                        "url": f"{base}/OpportunityDetail?opportunityId={jid}", "posted": j.get("PostedDate")})
    return out


def adp(s, c):
    cid = c["cid"]
    data = _get(s, f"https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions?cid={cid}&timeStamp=1&lang=en_US&ccId=19000101_000001&locale=en_US&$top=200").json()
    out = []
    for j in data.get("jobRequisitions", []):
        locs = [", ".join([x for x in [(l.get("address") or {}).get("cityName"), ((l.get("address") or {}).get("countrySubdivisionLevel1") or {}).get("codeValue")] if x]) for l in j.get("requisitionLocations", [])]
        jid = j.get("itemID")
        out.append({"id": jid, "title": j.get("requisitionTitle"), "location": " / ".join(locs), "remote": False,
                    "url": f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&jobId={jid}",
                    "posted": j.get("postDate")})
    return out


def phenom(s, c):
    base = c["base"].rstrip("/")
    seen, out = set(), []
    for term in SEARCH_TERMS[:6]:
        r = _get(s, f"{base}/search-results?keywords={quote(term)}")
        m = re.search(r'"eagerLoadRefineSearch"\s*:\s*(\{.*?\})\s*,\s*"(?:jobwidgetsettings|.*?Settings)"', r.text, re.S)
        jobs = []
        if m:
            try:
                jobs = json.loads(m.group(1)).get("data", {}).get("jobs", [])
            except Exception:
                jobs = []
        if not jobs:
            m2 = re.search(r'"jobs"\s*:\s*(\[\{.*?\}\])\s*,\s*"aggregations"', r.text, re.S)
            if m2:
                try:
                    jobs = json.loads(m2.group(1))
                except Exception:
                    jobs = []
        for j in jobs:
            jid = j.get("jobSeqNo") or j.get("jobId") or j.get("reqId")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            out.append({"id": jid, "title": j.get("title"),
                        "location": j.get("cityStateCountry") or j.get("location") or ", ".join(j.get("multi_location", []) or []),
                        "remote": "remote" in json.dumps(j.get("multi_location", "")).lower() or bool(j.get("isRemote")),
                        "url": f"{base}/job/{j.get('jobId', jid)}", "posted": j.get("postedDate") or j.get("dateCreated")})
    return out


def eightfold(s, c):
    host, domain = c["host"], c["domain"]
    seen, out = set(), []
    for term in SEARCH_TERMS[:6]:
        data = _get(s, f"https://{host}/api/apply/v2/jobs?domain={domain}&query={quote(term)}&num=50&sort_by=timestamp").json()
        for j in data.get("positions", []):
            jid = j.get("id")
            if jid in seen:
                continue
            seen.add(jid)
            out.append({"id": jid, "title": j.get("name"), "location": " / ".join(j.get("locations", []) or [j.get("location", "")]),
                        "remote": (j.get("work_location_option") or "").lower() == "remote",
                        "url": j.get("canonicalPositionUrl") or f"https://{host}/careers/job/{jid}",
                        "posted": j.get("t_create")})
    return out


ADAPTERS = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
    "workable": workable, "recruitee": recruitee, "breezy": breezy, "rippling": rippling,
    "workday": workday, "oracle": oracle, "icims": icims, "jobvite": jobvite, "ultipro": ultipro,
    "adp": adp, "phenom": phenom, "eightfold": eightfold,
}


# ---------- figure out which system a URL (or a page's HTML) points to ----------

BAD_TOKENS = {"embed", "v1", "jobs", "job", "careers", "apply", "api", "j", "search", "en", "en-us", "boards"}

PATTERNS = [
    ("greenhouse", r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)"),
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([A-Za-z0-9_.-]+)"),
    ("lever", r"api\.lever\.co/v0/postings/([A-Za-z0-9_.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    ("smartrecruiters", r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    ("recruitee", r"([a-z0-9-]+)\.recruitee\.com"),
    ("breezy", r"([a-z0-9-]+)\.breezy\.hr"),
    ("rippling", r"ats\.rippling\.com/([A-Za-z0-9_-]+)"),
    ("jobvite", r"jobs\.jobvite\.com/([A-Za-z0-9_-]+)"),
]


def detect(text):
    """Return a list of candidate configs found in a URL or HTML/network text, best first."""
    found = []
    if not text:
        return found
    for name, pat in PATTERNS:
        for m in re.finditer(pat, text):
            tok = m.group(1).strip("/").split("/")[0]
            if tok.lower() in BAD_TOKENS or len(tok) < 2:
                continue
            found.append({"adapter": name, "token": tok})
    for m in re.finditer(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[a-z0-9-]+/)?(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", text):
        tenant, wd, site = m.groups()
        if site.lower() in {"wday", "job", "jobs"}:
            continue
        found.append({"adapter": "workday", "host": f"{tenant}.{wd}.myworkdayjobs.com", "tenant": tenant, "site": site})
    for m in re.finditer(r"(https://[a-z0-9-]+\.fa\.[a-z0-9-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-zA-Z-]+/sites/([A-Za-z0-9_]+)", text):
        found.append({"adapter": "oracle", "base": m.group(1), "site": m.group(2)})
    for m in re.finditer(r"\b((?:careers|jobs|external|uscareers|careers-[a-z0-9-]+|[a-z0-9-]+-careers)\.icims\.com|[a-z0-9-]+\.icims\.com)", text):
        h = m.group(1)
        if not h.startswith(("www.", "api.", "developer.", "community.")):
            found.append({"adapter": "icims", "host": h})
    for m in re.finditer(r"(https://recruiting2?\.ultipro\.com/[A-Z0-9]+/JobBoard/[a-f0-9-]{36})", text, re.I):
        found.append({"adapter": "ultipro", "base": m.group(1)})
    for m in re.finditer(r"workforcenow\.adp\.com/[^\"' ]*?cid=([a-f0-9-]{36})", text, re.I):
        found.append({"adapter": "adp", "cid": m.group(1)})
    for m in re.finditer(r"([a-z0-9-]+\.eightfold\.ai)", text):
        dm = re.search(r'"domain"\s*:\s*"([a-z0-9.-]+\.[a-z]+)"', text)
        if dm:
            found.append({"adapter": "eightfold", "host": m.group(1), "domain": dm.group(1)})
    # de-duplicate, keep order
    out, keys = [], set()
    for f in found:
        k = json.dumps(f, sort_keys=True)
        if k not in keys:
            keys.add(k)
            out.append(f)
    return out


def detect_phenom(html_text, final_url):
    if "phenompeople" in html_text or "phApp" in html_text:
        p = urlparse(final_url)
        m = re.match(r"(/[a-z]{2}(?:-[a-z]{2})?)(?:/|$)", p.path)
        return {"adapter": "phenom", "base": f"{p.scheme}://{p.netloc}{m.group(1) if m else ''}"}
    return None
