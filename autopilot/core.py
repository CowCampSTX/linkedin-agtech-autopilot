"""Core pieces: AgTech news intake, the draft queue, post checks and the LinkedIn API client.

Standard library only, so fetching, checking and posting run anywhere Python 3.11+ runs. Drafting with
Claude lives in autopilot/cli.py. Nothing here posts unless the caller asks it to; the live switch and the
--confirm gate are enforced in the CLI.

Queue: data/queue.json, kept in git so the scheduled GitHub Actions job and a local run share it (the posts
are public anyway). Each draft moves pending -> approved -> posted, or -> rejected / failed.
"""
import email.utils
import html
import json
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = "linkedin-agtech-autopilot/1.0"


def load_cfg(path: Path = ROOT / "config.toml") -> dict:
    return tomllib.loads(Path(path).read_text())


# ---------------------------------------------------------------- news intake

def _text(el, *names) -> str:
    for name in names:
        found = el.find(name)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def _date(value: str):
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)            # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))   # Atom: ISO 8601
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def parse_feed(data: bytes, source: str) -> list[dict]:
    """RSS 2.0 or Atom -> [{title, url, summary, published, source}]."""
    root = ET.fromstring(data)
    atom = "{http://www.w3.org/2005/Atom}"
    items = []
    for it in root.iter("item"):
        items.append({"title": clean(_text(it, "title")), "url": _text(it, "link"),
                      "summary": clean(_text(it, "description", "{http://purl.org/rss/1.0/modules/content/}encoded"))[:1200],
                      "published": _date(_text(it, "pubDate", "{http://purl.org/dc/elements/1.1/}date")), "source": source})
    for it in root.iter(f"{atom}entry"):
        link = it.find(f"{atom}link")
        items.append({"title": clean(_text(it, f"{atom}title")), "url": link.get("href", "") if link is not None else "",
                      "summary": clean(_text(it, f"{atom}summary", f"{atom}content"))[:1200],
                      "published": _date(_text(it, f"{atom}published", f"{atom}updated")), "source": source})
    return [i for i in items if i["title"] and i["url"]]


def score(item: dict, cfg: dict) -> int:
    """Keyword hits in title (x2) and summary. Skip words veto the story."""
    src = cfg.get("sources", {})
    title, body = item["title"].lower(), item["summary"].lower()
    if any(k.lower() in title or k.lower() in body for k in src.get("skip_keywords", [])):
        return -1
    return sum(2 * (k.lower() in title) + (k.lower() in body) for k in src.get("keywords", []))


def norm_url(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"


def pick_stories(items: list[dict], cfg: dict, used_urls: set[str], now=None) -> list[dict]:
    """Fresh, on-topic, not already drafted; best score first, newest breaks ties."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=cfg.get("sources", {}).get("max_age_days", 10))
    seen, out = set(used_urls), []
    for it in items:
        key = norm_url(it["url"])
        if key in seen or (it["published"] and it["published"] < cutoff):
            continue
        seen.add(key)
        it = dict(it, score=score(it, cfg))
        if it["score"] > 0:
            out.append(it)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(out, key=lambda i: (i["score"], i["published"] or epoch), reverse=True)


def fetch_news(cfg: dict, timeout: int = 20) -> tuple[list[dict], list[str]]:
    items, errors = [], []
    for feed in cfg.get("sources", {}).get("feeds", []):
        try:
            req = urllib.request.Request(feed["url"], headers={"User-Agent": UA})
            items += parse_feed(urllib.request.urlopen(req, timeout=timeout).read(), feed["name"])
        except Exception as e:  # one dead feed shouldn't stop the run
            errors.append(f"{feed['name']}: {e}")
    return items, errors


# ---------------------------------------------------------------- queue

def queue_path() -> Path:
    return ROOT / "data" / "queue.json"


def load_queue(path: Path) -> list[dict]:
    return json.loads(path.read_text()) if path.exists() else []


def save_queue(path: Path, queue: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, indent=2, ensure_ascii=False))
    tmp.replace(path)


def next_id(queue: list[dict], prefix: str) -> str:
    nums = [int(d["id"].split("-")[1]) for d in queue if d.get("id", "").startswith(prefix + "-")]
    return f"{prefix}-{max(nums, default=0) + 1:04d}"


# ---------------------------------------------------------------- post checks

HASHTAG = re.compile(r"(?<![\w#])#(\w+)")
URL = re.compile(r"https?://\S+|www\.\S+", re.I)


def check_post(text: str, cfg: dict) -> list[str]:
    """Problems that block approval/posting. Empty list = OK."""
    rules, issues = cfg.get("post_rules", {}), []
    body = text.strip()
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return ["empty post"]
    if not rules.get("min_chars", 0) <= len(body) <= rules.get("max_chars", 3000):
        issues.append(f"length {len(body)} chars; keep it {rules.get('min_chars')}-{rules.get('max_chars')}")
    if len(lines[0]) > rules.get("hook_max_chars", 210):
        issues.append(f"hook line is {len(lines[0])} chars; keep it under {rules.get('hook_max_chars')} so it shows before 'see more'")
    tags = HASHTAG.findall(body)
    if not rules.get("hashtags_min", 0) <= len(tags) <= rules.get("hashtags_max", 5):
        issues.append(f"{len(tags)} hashtags; use {rules.get('hashtags_min')}-{rules.get('hashtags_max')}")
    if cfg.get("linkedin", {}).get("link_in_first_comment", True) and URL.search(body):
        issues.append("link in the post body; put it in the first comment")
    question_zone = [ln for ln in lines if not HASHTAG.sub("", ln).strip() == ""][-2:]
    if not any("?" in ln for ln in question_zone):
        issues.append("no closing question for comments")
    low = body.lower()
    for phrase in rules.get("banned_phrases", []):
        if phrase.lower() in low:
            issues.append(f"banned phrase: {phrase!r}")
    for term in cfg.get("privacy", {}).get("private_terms", []):
        if re.search(rf"\b{re.escape(term.lower())}\b", low):
            issues.append(f"private term: {term!r}")
    return issues


# ---------------------------------------------------------------- LinkedIn API

# LinkedIn's "little text format": these characters must be escaped in post commentary or the
# API rejects the post or drops text. Hashtags become hashtag templates so they stay clickable.
_RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")


def to_commentary(text: str) -> str:
    out, last = [], 0
    for m in HASHTAG.finditer(text):
        out.append(_RESERVED.sub(r"\\\1", text[last:m.start()]))
        out.append("{hashtag|\\#|" + m.group(1) + "}")
        last = m.end()
    out.append(_RESERVED.sub(r"\\\1", text[last:]))
    return "".join(out)


class LinkedInError(RuntimeError):
    pass


class LinkedIn:
    """Member posting with a 3-legged OAuth token (scopes: openid profile w_member_social)."""
    API = "https://api.linkedin.com"

    def __init__(self, token: str, version: str):
        # strip: a token pasted into .env or a GitHub secret often carries a newline or quotes, which breaks the header
        self.token, self.version = token.strip().strip('"').strip("'").strip(), version

    def _call(self, method: str, path: str, body=None, rest: bool = True):
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": UA}
        if rest:
            headers.update({"LinkedIn-Version": self.version, "X-Restli-Protocol-Version": "2.0.0",
                            "Content-Type": "application/json"})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.headers, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise LinkedInError(f"LinkedIn {method} {path} -> {e.code}: {detail}") from None

    def me(self) -> dict:
        """OpenID userinfo; 'sub' is the member id for urn:li:person:<sub>."""
        return self._call("GET", "/v2/userinfo", rest=False)[1]

    def create_post(self, author_urn: str, text: str) -> str:
        headers, _ = self._call("POST", "/rest/posts", {
            "author": author_urn,
            "commentary": to_commentary(text),
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        })
        urn = headers.get("x-restli-id") or headers.get("x-linkedin-id")
        if not urn:
            raise LinkedInError("post created but LinkedIn returned no post id")
        return urn

    def comment(self, author_urn: str, post_urn: str, text: str) -> None:
        self._call("POST", f"/rest/socialActions/{urllib.parse.quote(post_urn, safe='')}/comments",
                   {"actor": author_urn, "object": post_urn, "message": {"text": text}})

    @staticmethod
    def post_url(post_urn: str) -> str:
        return f"https://www.linkedin.com/feed/update/{post_urn}/"
