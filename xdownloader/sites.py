"""Supported sites and URL helpers.

To support another site, append one `Site(...)` to SITES - URL filtering, normalisation,
login-cookie detection and error handling are all driven by these fields.
"""
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

# Splits glued pastes such as "https://a/1https://b/2" before every scheme.
_URL_START_RE = re.compile(r"(?=https?://)")


@dataclass(frozen=True)
class Site:
    name: str
    url_prefixes: tuple[str, ...]
    cookie_domains: tuple[str, ...]        # cookies of these domains (and subdomains) are used
    login_cookie: str                      # cookie whose presence proves "logged in"
    required_cookies: tuple[str, ...]      # cookies a cookies file must contain to be useful
    probe_url: str                         # cheap URL used to test cookie extraction
    cookies_name: str = ""                 # fallback cookies file in the project dir (default: cookies_<domain>.txt)
    canonical_url: re.Pattern | None = None  # group(1) = the URL to keep; the rest is stripped
    non_retryable: tuple[str, ...] = ()    # yt-dlp messages that retrying cannot fix
    login_error: str = ""                  # yt-dlp message that means "not logged in"
    login_hint: str = ""                   # actionable remediation advice displayed to the user
    keep_query: tuple[str, ...] = ()       # query parameters that identify the video (e.g. ("v",))

    def __post_init__(self):
        # One cookies file per site: exporting one site's cookies must never overwrite another's.
        if not self.cookies_name:
            object.__setattr__(self, "cookies_name", f"cookies_{self.cookie_domains[0]}.txt")

    def matches(self, url: str) -> bool:
        return url.startswith(self.url_prefixes)

    def normalize(self, url: str) -> str:
        """Drops query/fragment/trailing slash and everything after the canonical URL."""
        base, _, query = url.strip().partition('?')
        kept = [(k, v) for k, v in parse_qsl(query.split('#')[0], keep_blank_values=True) if k in self.keep_query]
        cleaned = base.split('#')[0].rstrip('/')
        if self.canonical_url:
            m = self.canonical_url.match(cleaned)
            if m:
                # Match only what follows the status id. A substring search on the whole URL
                # would also cut user names, e.g. "x.com/likesbot/status/1" -> "x.com".
                cleaned = m.group(1)
        return cleaned + ('?' + urlencode(kept) if kept else '')


X = Site(
    name="X (Twitter)",
    url_prefixes=(
        "https://x.com/", "https://www.x.com/", "https://mobile.x.com/",
        "https://twitter.com/", "https://www.twitter.com/", "https://mobile.twitter.com/",
        # Short links are passed through unchanged: yt-dlp's twitter:shortener extractor
        # follows the redirect (to an X post or to any other site the link points to).
        "https://t.co/",
    ),
    cookie_domains=("x.com", "twitter.com"),
    login_cookie="auth_token",
    required_cookies=("auth_token", "ct0"),
    probe_url="https://x.com/x/status/1",
    cookies_name="cookies.txt",            # the file name documented for X (README); keep it
    # https://<host>/<user>/status/<id>  or  /i/web/status/<id>; strips /video/1, /photo/1, ...
    canonical_url=re.compile(
        r"^(https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/(?:i/web|[^/]+)/status(?:es)?/\d+)",
        re.IGNORECASE,
    ),
    non_retryable=(
        "No video could be found",
        "This tweet is unavailable",
        "Account suspended",
        "does not exist",
        "Private video",
        "Requested content is not available",
        "has been withheld",
    ),
    login_error="No video could be found",
    login_hint="Please log into x.com in your browser, then restart the downloader.",
)

SITES: list[Site] = [X]


def find_site(url: str) -> Site | None:
    url = url.strip()
    return next((s for s in SITES if s.matches(url)), None)


def normalize_url(url: str) -> str:
    site = find_site(url)
    if site:
        return site.normalize(url)
    return url.strip().split('?')[0].split('#')[0].rstrip('/')


def extract_urls(text: str) -> list[str]:
    """Returns the supported URLs found in *text*, de-duplicated by normalized form.

    - Tokens are split on whitespace, so notes after a URL are not passed to yt-dlp.
    - URLs pasted back-to-back without a separator are split apart.
    - ".../status/1", ".../status/1/video/1" and ".../status/1?s=20" count as one URL.
    """
    urls: list[str] = []
    seen: set[str] = set()
    for token in text.split():
        for part in _URL_START_RE.split(token):
            if part and find_site(part):
                key = normalize_url(part)
                if key not in seen:
                    seen.add(key)
                    urls.append(part)
    return urls
