"""Title, location and date rules for Shadman's IT support search."""
import re
from datetime import datetime, timedelta, timezone

TITLE_INCLUDE = re.compile(
    r"\b(help ?desk|service ?desk|desktop (support|tech\w*|specialist|analyst|engineer)|deskside|"
    r"end[- ]?user (support|services|computing|technician|specialist)|it support|i\.t\. support|"
    r"technical support (specialist|technician|analyst|associate|representative)|tech support|"
    r"it (technician|tech|specialist|analyst|associate|coordinator|intern|internship|operations technician)|"
    r"information technology (technician|specialist|support|intern|internship|analyst|associate)|"
    r"(computer|pc|systems?|user|client|field|onsite|on-site|noc|network|endpoint|hardware|av/it|it/av) "
    r"(support )?(technician|tech|specialist|support)|"
    r"support (technician|specialist|analyst|associate|engineer i\b|engineer 1\b)|"
    r"security (systems )?technician|(personal |end[- ]user )?computing (specialist|support|technician)|"
    r"technology (support|specialist|technician|associate)|desktop|low[- ]voltage technician|"
    r"(tier|level) (1|i|2|ii|one|two)\b|data ?cent(er|re) technician|"
    r"service ?now (analyst|administrator)|identity (and|&) access (analyst|specialist|associate)|"
    r"iam (analyst|specialist)|soc analyst (i|1)\b|cyber ?security (analyst|intern|associate) ?(i|1)?\b)",
    re.I,
)

TITLE_EXCLUDE = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff (engineer|scientist|analyst)|manager|mgr|director|head|vp|vice president|chief|architect|"
    r"supervisor|iii|iv|v\b|level (3|iii|4|iv)|tier (3|iii)|"
    r"software (engineer|developer)|developer|sales|account executive|business development|recruiter|"
    r"nurse|rn\b|physician|pharmac|clinical|medical assistant|driver|mechanic|hvac|electrician|plumber|"
    r"automotive|diesel|aircraft|avionics|lab technician|laboratory|veterinary|dental|"
    r"customer success manager|call center|teacher|professor|faculty)\b",
    re.I,
)

DFW = [
    "arlington", "dallas", "fort worth", "ft worth", "ft. worth", "irving", "las colinas", "grand prairie",
    "mansfield", "grapevine", "bedford", "euless", "hurst", "southlake", "westlake", "keller", "colleyville",
    "north richland hills", "haltom city", "plano", "richardson", "frisco", "mckinney", "allen", "carrollton",
    "addison", "garland", "mesquite", "flower mound", "lewisville", "coppell", "wylie", "farmers branch",
    "denton", "the colony", "little elm", "prosper", "rockwall", "rowlett", "cedar hill", "desoto", "duncanville",
    "lancaster", "burleson", "weatherford", "roanoke", "trophy club", "midlothian", "waxahachie", "sachse",
    "murphy", "celina", "anna", "forney", "alliance", "dfw", "metroplex",
]
NON_US = re.compile(
    r"\b(canada|uk|united kingdom|england|london|ireland|dublin|germany|berlin|france|paris|india|bangalore|"
    r"bengaluru|hyderabad|pune|mumbai|delhi|gurgaon|noida|chennai|philippines|manila|mexico|brazil|"
    r"argentina|colombia|poland|romania|spain|portugal|netherlands|amsterdam|israel|tel aviv|singapore|"
    r"japan|tokyo|australia|sydney|emea|apac|latam|europe|toronto|vancouver|montreal|ontario|costa rica|"
    r"south africa|egypt|ukraine|serbia|czech|sweden|denmark|finland|norway|switzerland|italy|china|"
    r"korea|taiwan|vietnam|malaysia|indonesia|pakistan|bangladesh|nigeria|kenya|uae|dubai|saudi)\b",
    re.I,
)


def title_ok(title):
    t = title or ""
    return bool(TITLE_INCLUDE.search(t)) and not TITLE_EXCLUDE.search(t)


def location_kind(location, remote_flag=False):
    """Return 'dfw', 'remote', 'texas', or None."""
    loc = (location or "").lower()
    if any(c in loc for c in DFW):
        return "dfw"
    is_remote = remote_flag or "remote" in loc or "work from home" in loc or "anywhere" in loc
    if is_remote and not NON_US.search(loc):
        return "remote"
    if re.search(r"\btexas\b|,\s*tx\b|\btx\b", loc) and not re.search(r"houston|austin|san antonio|el paso|lubbock|corpus|mcallen|amarillo|waco|tyler|beaumont|midland|odessa|laredo|brownsville|killeen|college station", loc):
        return "texas"
    return None


def parse_date(value):
    """Best-effort ISO date (YYYY-MM-DD) from many formats; None if unknown."""
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        s = str(value).strip()
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if m:
            return m.group(1)
        low = s.lower()
        today = datetime.now(timezone.utc).date()
        if "today" in low or "just posted" in low or "hour" in low or "minute" in low:
            return today.isoformat()
        if "yesterday" in low:
            return (today - timedelta(days=1)).isoformat()
        m = re.search(r"(\d+)\+?\s*days?\s*ago", low)
        if m:
            if "+" in low:
                return (today - timedelta(days=int(m.group(1)))).isoformat()
            return (today - timedelta(days=int(m.group(1)))).isoformat()
        for fmt in ("%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(s[:20].strip(), fmt).date().isoformat()
            except ValueError:
                pass
    except Exception:
        return None
    return None
