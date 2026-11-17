"""Supported sites and URL helpers.

To support another site, append one `Site(...)` to SITES - URL filtering, normalisation,
login-cookie detection and error handling are all driven by these fields.
"""
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

# Splits glued pastes such as "https://a/1https://b/2" before every scheme (in any case, like Site.matches).
_URL_START_RE = re.compile(r"(?=https?://)", re.IGNORECASE)
# Copied along from web pages (zero-width spaces and joiners, byte order marks); never part of a URL.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))
# Punctuation of the text around a URL: "(https://t.co/x),", "see https://t.co/x.", "「https://x.com/u/status/1」<...".
# Not "…": it marks a URL cut short in display text, and without it that would be another, shorter one.
_TRAILING_PUNCTUATION = ".,;:!?'\"()[]{}<>，。、；：！？（）「」『』【】《》〈〉“”‘’"


@dataclass(frozen=True)
class Site:
    name: str
    url_prefixes: tuple[str, ...]
    cookie_domains: tuple[str, ...]        # cookies of these domains (and subdomains) are used
    login_cookie: str                      # cookie whose presence proves "logged in"
    required_cookies: tuple[str, ...]      # cookies a cookies file must contain to be useful
    probe_url: str                         # cheap URL used to test cookie extraction
    cookies_name: str = ""                 # fallback cookies file in the project dir (default: cookies_<domain>.txt)
    # group(1) = the URL to keep, the rest is stripped. A URL of the site that it does not match (a profile page, the
    # home page) is not one the site can download, so it does not count. None: every URL with a prefix counts.
    canonical_url: re.Pattern | None = None
    non_retryable: tuple[str, ...] = ()    # yt-dlp messages that retrying cannot fix
    login_error: str = ""                  # yt-dlp message that means "not logged in"
    login_hint: str = ""                   # actionable remediation advice displayed to the user
    keep_query: tuple[str, ...] = ()       # query parameters that identify the video (e.g. ("v",))

    def __post_init__(self):
        # One cookies file per site: exporting one site's cookies must never overwrite another's.
        if not self.cookies_name:
            object.__setattr__(self, "cookies_name", f"cookies_{self.cookie_domains[0]}.txt")

    def matches(self, url: str) -> bool:
        """True if this site can download *url*."""
        # A scheme or host in capitals is the same URL (typed, or capitalised by a phone); url_prefixes are lowercase.
        if not url.lower().startswith(self.url_prefixes):
            return False
        return self.canonical_url is None or self.canonical_url.match(url) is not None

    def normalize(self, url: str) -> str:
        """Drops query/fragment/trailing slash and everything after the canonical URL; scheme and host in lowercase."""
        base, _, query = url.strip().partition('?')
        kept = [(k, v) for k, v in parse_qsl(query.split('#')[0], keep_blank_values=True) if k in self.keep_query]
        cleaned = base.split('#')[0].rstrip('/')
        if self.canonical_url:
            m = self.canonical_url.match(cleaned)
            if m:
                # Match only what follows the status id. A substring search on the whole URL
                # would also cut user names, e.g. "x.com/likesbot/status/1" -> "x.com".
                cleaned = m.group(1)
        scheme, sep, rest = cleaned.partition("://")
        if sep:                     # the path keeps its case: a t.co code is case-sensitive
            host, slash, path = rest.partition("/")
            cleaned = f"{scheme.lower()}://{host.lower()}{slash}{path}"
        return cleaned + ('?' + urlencode(kept) if kept else '')


X = Site(
    name="X (Twitter)",
    url_prefixes=(
        "https://x.com/", "https://www.x.com/", "https://mobile.x.com/", "https://m.x.com/",
        "https://twitter.com/", "https://www.twitter.com/", "https://mobile.twitter.com/", "https://m.twitter.com/",
        # Short links are passed through unchanged: yt-dlp's twitter:shortener extractor
        # follows the redirect (to an X post or to any other site the link points to).
        "https://t.co/",
    ),
    cookie_domains=("x.com", "twitter.com"),
    login_cookie="auth_token",
    required_cookies=("auth_token", "ct0"),
    probe_url="https://x.com/x/status/1",
    cookies_name="cookies.txt",            # the file name documented for X (README); keep it
    # What the X extractors of yt-dlp take (measured), and only that: a post (/<user>/status/<id>, /i/web/status/<id>,
    # /statuses/<id>; /video/1, /photo/1 ... are stripped), a broadcast or Space, a card or video page, a t.co short
    # link. Other pages of X (the home page, a profile, a search) go to yt-dlp's generic extractor and fail. The id or
    # code must end there: cut at anything else, a short link would become another one.
    canonical_url=re.compile(
        r"^(https?://(?:www\.|mobile\.|m\.)?(?:x|twitter)\.com/"
        r"(?:(?:i/web|[^/]+)/status(?:es)?/\d+|statuses/\d+|i/(?:broadcasts|spaces)/[0-9a-z]+"
        r"|i/cards/tfw/v1/\d+|i/videos(?:/tweet)?/\d+)"
        r"|https?://t\.co/[0-9a-z]+)(?=[/?#]|$)",
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
    - Punctuation of the text around a URL, and invisible characters copied from a web page, are not part of it.
    - ".../status/1", ".../status/1/video/1" and ".../status/1?s=20" count as one URL.
    """
    urls: list[str] = []
    seen: set[str] = set()
    for token in text.translate(_INVISIBLE).split():
        for part in _URL_START_RE.split(token):
            part = part.rstrip(_TRAILING_PUNCTUATION)
            if part and find_site(part):
                key = normalize_url(part)
                if key not in seen:
                    seen.add(key)
                    urls.append(part)
    return urls
