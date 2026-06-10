"""
Browser-based search using Playwright.
Handles sites that require login or don't have yt-dlp search support.
"""
from __future__ import annotations

import os
import re
import json
import time
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

_VIDEO_EXTS = re.compile(r'\.(mp4|webm|mov|m3u8|mpd)(\?|$)', re.I)
_VIDEO_MIME  = re.compile(r'video/', re.I)

# Known platform embed/link patterns → canonical watchable URLs
_PLATFORM_PATTERNS = [
    (re.compile(r'(?:youtube\.com/embed/|youtu\.be/)([A-Za-z0-9_-]{11})'),
     'https://www.youtube.com/watch?v={id}'),
    (re.compile(r'player\.vimeo\.com/video/(\d+)'),
     'https://vimeo.com/{id}'),
    (re.compile(r'vimeo\.com/(\d{5,})'),
     'https://vimeo.com/{id}'),
    (re.compile(r'dailymotion\.com/(?:embed/video|video)/([A-Za-z0-9]+)'),
     'https://www.dailymotion.com/video/{id}'),
    (re.compile(r'streamable\.com/(?:e/)?([A-Za-z0-9]+)$'),
     'https://streamable.com/{id}'),
    (re.compile(r'twitch\.tv/videos/(\d+)'),
     'https://www.twitch.tv/videos/{id}'),
    (re.compile(r'clips\.twitch\.tv/([A-Za-z0-9_-]+)'),
     'https://clips.twitch.tv/{id}'),
]

# HTML attributes that commonly hold hover/preview video URLs
_VIDEO_DATA_ATTRS = [
    'data-preview', 'data-preview-src', 'data-preview-url', 'data-preview-video',
    'data-hover-video', 'data-hover-src',
    'data-video', 'data-video-src', 'data-video-url',
    'data-mp4', 'data-clip', 'data-clip-src', 'data-clip-url',
]


def search(
    source: str,
    query: str,
    username: str = None,
    password: str = None,
    cookie_out_path: str = None,
    site: str = None,
) -> tuple:
    """
    Launch a Chromium browser, optionally log in, search/crawl, and return
    (results, cookie_file_path).  cookie_file_path is None if not saved.
    results is a list of {url, title, thumbnail, duration} dicts.

    source='facebook_page': query must be the Facebook page/profile URL.
    source='site_search':   site is the website name/URL, query is the search term.
    """
    source = source.lower()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
            ],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        # Basic stealth — hide the headless/automation tells most bot checks use
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = window.chrome || { runtime: {} };
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        """)
        page = ctx.new_page()
        try:
            if source == "reddit":
                results = _reddit(page, query, username, password)
            elif source in ("twitter", "x"):
                results = _twitter(page, query, username, password)
            elif source == "facebook_page":
                results = _facebook_page(page, query, username, password)
            elif source == "crawl":
                results = _generic_crawl(page, query)
            elif source == "site_search":
                results = _site_search(page, site or "", query)
            else:
                results = []

            # Export session cookies for yt-dlp downloads
            saved_path = None
            if cookie_out_path:
                try:
                    _save_cookies_netscape(ctx.cookies(), cookie_out_path)
                    saved_path = cookie_out_path
                except Exception:
                    pass
        finally:
            browser.close()
    return results, saved_path


def _save_cookies_netscape(cookies: list, path: str) -> None:
    """Write Playwright cookies to a Netscape cookies.txt file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            domain = c.get("domain", "")
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            p_ = c.get("path", "/")
            secure = "TRUE" if c.get("secure", False) else "FALSE"
            exp = c.get("expires", 0)
            expires = int(exp) if exp and exp > 0 else 0
            name = c.get("name", "")
            value = c.get("value", "")
            f.write(f"{domain}\t{flag}\t{p_}\t{secure}\t{expires}\t{name}\t{value}\n")


# ── Reddit ─────────────────────────────────────────────────────────────────────

def _reddit(page, query: str, username: str, password: str) -> list:
    if username and password:
        _reddit_login(page, username, password)

    results = _reddit_json(page, query, video_only=True)

    # Fallback 1: try without is_video filter (cloud IPs often get empty results)
    if not results:
        results = _reddit_json(page, query, video_only=False)

    # Fallback 2: HTML scrape if JSON blocked entirely
    if not results:
        results = _reddit_html(page, query)

    return results[:10]


def _reddit_json(page, query: str, video_only: bool = True) -> list:
    api_url = (
        f"https://www.reddit.com/search.json"
        f"?q={_enc(query)}&type=link&sort=relevance&limit=50"
    )
    try:
        page.goto(api_url, timeout=25_000)
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PwTimeout:
            pass
        raw = page.evaluate("() => document.body.innerText")
        data = json.loads(raw)
    except Exception:
        return []

    posts = data.get("data", {}).get("children", [])
    results = []
    for post in posts:
        p = post.get("data", {})
        if video_only:
            has_video = (
                p.get("is_video")
                or bool(p.get("media"))
                or bool(p.get("secure_media"))
            )
            if not has_video:
                continue
        permalink = p.get("permalink", "")
        if not permalink:
            continue
        post_url = f"https://www.reddit.com{permalink}"
        thumb = p.get("thumbnail", "")
        if not thumb.startswith("http"):
            thumb = ""
        duration = 0
        media = p.get("media") or {}
        rv = media.get("reddit_video", {})
        if rv:
            duration = int(rv.get("duration", 0))
        results.append({
            "url": post_url,
            "title": p.get("title", ""),
            "thumbnail": thumb,
            "duration": duration,
        })
    return results


def _reddit_html(page, query: str) -> list:
    """HTML scrape fallback when JSON API is blocked."""
    search_url = (
        f"https://www.reddit.com/search/"
        f"?q={_enc(query)}&type=link&sort=relevance"
    )
    try:
        page.goto(search_url, timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except PwTimeout:
            pass
        page.evaluate("window.scrollBy(0, 1000)")
        page.wait_for_timeout(2000)
    except Exception:
        return []

    results = []
    _SKIP = {"image", "gallery", "text", "self"}
    for post in page.locator("shreddit-post").all():
        try:
            if (post.get_attribute("post-type") or "").lower() in _SKIP:
                continue
            permalink = post.get_attribute("permalink") or post.get_attribute("content-href") or ""
            title = post.get_attribute("post-title") or ""
            thumb = post.get_attribute("thumbnail-url") or ""
            if permalink:
                url = permalink if permalink.startswith("http") else f"https://www.reddit.com{permalink}"
                results.append({"url": url, "title": title, "thumbnail": thumb, "duration": 0})
        except Exception:
            continue
    return results


def _reddit_login(page, username: str, password: str):
    try:
        page.goto("https://www.reddit.com/login/", timeout=20_000)
        page.wait_for_load_state("domcontentloaded", timeout=10_000)

        # Try multiple possible selectors for the username field
        for sel in ['input[name="username"]', "#loginUsername", 'input[id*="login"][id*="user"]']:
            if page.locator(sel).count() > 0:
                page.fill(sel, username)
                break

        for sel in ['input[name="password"]', "#loginPassword", 'input[type="password"]']:
            if page.locator(sel).count() > 0:
                page.fill(sel, password)
                break

        page.click('button[type="submit"]')
        page.wait_for_timeout(3000)
        # Check for login errors
        if page.locator('[id*="error"], .AnimatedForm__errorMessage').count() > 0:
            raise ValueError("Reddit login failed — check username/password.")
    except PwTimeout:
        pass  # login page slow, continue anyway


def _extract_reddit_posts(page) -> list:
    results = []

    return results


# ── Twitter / X ────────────────────────────────────────────────────────────────

def _twitter(page, query: str, username: str, password: str) -> list:
    if username and password:
        _twitter_login(page, username, password)

    search_url = f"https://x.com/search?q={_enc(query)}&f=video"
    page.goto(search_url, timeout=30_000)
    _wait_any(page, ['article[data-testid="tweet"]'], timeout=12_000)

    return _extract_tweets(page)[:10]


def _twitter_login(page, username: str, password: str):
    try:
        page.goto("https://x.com/login", timeout=20_000)
        page.wait_for_load_state("domcontentloaded", timeout=10_000)

        # Step 1: username/email
        page.fill('input[autocomplete="username"]', username)
        page.keyboard.press("Enter")
        page.wait_for_timeout(2000)

        # Step 2: may ask for phone/email confirmation (unusual login)
        unusual = page.locator('input[data-testid="ocfEnterTextTextInput"]')
        if unusual.count() > 0:
            unusual.fill(username)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1500)

        # Step 3: password
        page.fill('input[name="password"]', password)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
    except PwTimeout:
        pass


def _extract_tweets(page) -> list:
    results = []
    tweets = page.locator('article[data-testid="tweet"]').all()
    for tweet in tweets:
        try:
            link = tweet.locator('a[href*="/status/"]').first
            href = link.get_attribute("href") or ""
            if not href.startswith("http"):
                href = "https://x.com" + href
            text_el = tweet.locator('[data-testid="tweetText"]').first
            title = text_el.inner_text()[:120] if text_el.count() > 0 else "Tweet"
            results.append({"url": href, "title": title, "thumbnail": "", "duration": 0})
        except Exception:
            continue
    return results


# ── Facebook page crawler ───────────────────────────────────────────────────────

def _facebook_page(page, page_url: str, username: str, password: str) -> list:
    if username and password:
        _facebook_login(page, username, password)

    # Navigate to /videos tab
    base = page_url.rstrip("/")
    if not base.endswith("/videos"):
        base += "/videos"

    try:
        page.goto(base, timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except PwTimeout:
            pass
        # Scroll to trigger lazy loading
        for _ in range(3):
            page.evaluate("window.scrollBy(0, 800)")
            page.wait_for_timeout(1500)
    except Exception:
        return []

    return _extract_fb_videos(page)[:10]


def _facebook_login(page, username: str, password: str):
    try:
        page.goto("https://www.facebook.com/login", timeout=20_000)
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
        page.fill('input[name="email"]', username)
        page.fill('input[name="pass"]', password)
        page.click('button[name="login"]')
        page.wait_for_timeout(4000)
    except PwTimeout:
        pass


def _extract_fb_videos(page) -> list:
    results = []
    seen = set()

    # Facebook video links match these patterns
    for sel in [
        'a[href*="/watch/"]',
        'a[href*="/videos/"]',
        'a[href*="/reel/"]',
        'a[href*="watch?v="]',
    ]:
        for el in page.locator(sel).all():
            try:
                href = el.get_attribute("href") or ""
                if not href.startswith("http"):
                    href = "https://www.facebook.com" + href
                # Skip non-video pages and duplicates
                if href in seen:
                    continue
                # Filter out obvious non-video links
                if any(x in href for x in ["/videos/sound/", "/hashtag/", "?ref=", "/category/"]):
                    continue
                seen.add(href)
                # Try to get title from aria-label or nearby text
                title = el.get_attribute("aria-label") or ""
                if not title:
                    try:
                        title = el.inner_text()[:100].strip()
                    except Exception:
                        title = ""
                if not title:
                    title = href.split("/")[-1] or "Facebook Video"
                results.append({"url": href, "title": title, "thumbnail": "", "duration": 0})
            except Exception:
                continue

    return results


# ── Generic website crawler ────────────────────────────────────────────────────

def _page_title(page) -> str:
    try:
        og = page.evaluate("() => (document.querySelector('meta[property=\"og:title\"]') || {}).content || ''")
        if og and og.strip():
            return og.strip()
    except Exception:
        pass
    try:
        return page.title().strip()
    except Exception:
        return ""


_PREVIEW_HINTS = re.compile(
    r'[/_-](?:preview|hover|thumb|teaser|promo|snippet|gif|loop)[/_.-]', re.I
)

_PLAY_SELECTORS = [
    '.vjs-big-play-button',           # Video.js
    '.ytp-large-play-button',         # YouTube embed
    '.jwplayer .jw-icon-display',     # JW Player
    '[class*="BigPlayButton"]',       # React Player / custom
    '[class*="PlayButton"]',
    '[class*="play-button" i]',
    'button[class*="play" i]',
    '[aria-label*="play" i]',
    '[title*="play" i]',
    '[data-testid*="play" i]',
    'video',                          # clicking video itself toggles play
]


def _click_play(page, deadline: float) -> None:
    """Mute all videos then click play buttons to trigger stream network requests."""
    try:
        page.evaluate(
            "document.querySelectorAll('video').forEach(v => { v.muted = true; v.volume = 0; })"
        )
    except Exception:
        pass

    clicked = 0
    for sel in _PLAY_SELECTORS:
        if time.time() > deadline or clicked >= 5:
            break
        try:
            for el in page.locator(sel).all()[:3]:
                if time.time() > deadline:
                    break
                try:
                    if el.is_visible(timeout=500):
                        el.click(timeout=1500, force=True)
                        clicked += 1
                        page.wait_for_timeout(1500)
                except Exception:
                    pass
        except Exception:
            pass

    if clicked:
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except PwTimeout:
            pass


def _generic_crawl(page, url: str, max_seconds: int = 75, query: str = None) -> list:
    """
    Crawl any webpage for full videos.
    Strategy order:
      1. Platform embeds/links (YouTube/Vimeo/etc from iframes and <a> tags)
      2. Same-domain video page links (let yt-dlp handle them on clip)
      3. Direct video files from DOM (<video>, <source>)
      4. Network-intercepted streams (HLS/MP4/DASH) — large ones only (skip preview blobs)
      5. HLS/DASH manifests inferred from intercepted segment requests (.ts / .m4s)
    Play buttons are clicked after page load to trigger stream requests.
    query: when set (site-search results page), links whose text matches the
    query words are also collected even if their URL lacks /video/-style hints.
    """
    deadline = time.time() + max_seconds
    net_direct = set()
    net_manifests = set()  # inferred from HLS/DASH segment URLs

    def _on_response(response):
        try:
            rurl     = response.url
            base_url = rurl.split("?")[0]
            ctype    = response.headers.get("content-type", "")

            # HLS segments (.ts / mp2t) → infer manifest, skip adding segment itself
            if re.search(r'\.ts(\?|$)', base_url, re.I) or "mp2t" in ctype:
                base_dir = base_url.rsplit("/", 1)[0]
                for mname in ("index.m3u8", "playlist.m3u8", "master.m3u8"):
                    net_manifests.add(f"{base_dir}/{mname}")
                return

            # DASH segments (.m4s) → infer manifest
            if re.search(r'\.m4s(\?|$)', base_url, re.I):
                base_dir = base_url.rsplit("/", 1)[0]
                for mname in ("manifest.mpd", "index.mpd"):
                    net_manifests.add(f"{base_dir}/{mname}")
                return

            if not (_VIDEO_MIME.search(ctype) or _VIDEO_EXTS.search(base_url)):
                return
            if rurl.startswith("blob:"):
                return
            if _PREVIEW_HINTS.search(rurl):
                return
            cl = response.headers.get("content-length", "")
            if cl and int(cl) < 500_000:
                return
            net_direct.add(rurl)
        except Exception:
            pass

    page.on("response", _on_response)

    try:
        page.goto(url, timeout=25_000)
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PwTimeout:
            pass
        for _ in range(3):
            if time.time() > deadline:
                break
            page.evaluate("window.scrollBy(0, window.innerHeight)")
            page.wait_for_timeout(800)
    except Exception:
        return []

    # Click play buttons to force stream URLs into network traffic
    _click_play(page, deadline)

    page_title = _page_title(page)

    # DOM scan — (direct_files set, platform_urls set)
    dom_direct, platform_urls = _crawl_dom(page, url)

    # Same-domain video page links
    page_links = _find_video_page_links(page, url)

    # Search-results context: strict /video/-hint scan often misses listing
    # links — fall back to query-word matching on anchor text.
    if query and len(page_links) < 3:
        page_links = page_links + _find_query_links(page, url, query)

    # Merge inferred manifests into direct set
    net_direct |= net_manifests

    # Merge direct-file URLs, deduplicate by stem
    all_direct = net_direct | dom_direct
    seen_stems: dict = {}
    for u in all_direct:
        stem = _url_stem(u)
        if len(u) > len(seen_stems.get(stem, "")):
            seen_stems[stem] = u
    unique_direct = list(seen_stems.values())

    results = []
    seen_urls = set()

    def _add(entry):
        if entry["url"] not in seen_urls:
            seen_urls.add(entry["url"])
            results.append(entry)

    # 1. Platform URLs (YouTube/Vimeo/etc) — yt-dlp handles these best
    for u in platform_urls:
        parts = urlparse(u)
        host  = parts.netloc.replace("www.", "")
        vid   = parts.path.strip("/").split("/")[-1] or parts.query
        _add({"url": u, "title": f"{host} — {vid}", "thumbnail": "", "duration": 0})

    # 2. Same-domain video page links
    for entry in page_links:
        _add(entry)

    # 3. Direct video files from DOM / network
    for u in unique_direct[:20]:
        fname = urlparse(u).path.split("/")[-1] or u
        _add({"url": u, "title": fname or page_title or fname, "thumbnail": "", "duration": 0})

    return results[:30]


_VIDEO_PATH_HINT = re.compile(
    r'/(?:video|watch|clip|reel|short|play|stream|vod|episode|film|movie'
    r'|media|view|show|series)[/\-]|[?&](?:v|vid|video_id)=',
    re.I,
)
_NAV_SKIP = re.compile(
    r'/(?:login|signup|register|search|cart|account|settings|about|contact'
    r'|help|faq|terms|privacy|policy|tag|category|author|sitemap|feed|rss'
    r'|cdn|static|assets|img|css|js)/|#|javascript:',
    re.I,
)


def _find_video_page_links(page, base_url: str) -> list:
    """
    Find <a> links that explicitly look like individual video pages
    (URL contains /video/, /watch/, ?v=, etc.).
    Strict — no generic page links to avoid returning random nav/profile URLs.
    """
    base_host = urlparse(base_url).netloc
    seen  = set()
    links = []

    for el in page.locator("a[href]").all()[:300]:
        try:
            href = el.get_attribute("href") or ""
            if not href.startswith(("http", "/")):
                continue
            full   = urljoin(base_url, href)
            parsed = urlparse(full)

            if parsed.netloc != base_host:
                continue
            if full.split("#")[0].rstrip("/") == base_url.rstrip("/"):
                continue
            if _NAV_SKIP.search(parsed.path):
                continue
            if not _VIDEO_PATH_HINT.search(parsed.path + "?" + (parsed.query or "")):
                continue
            if full in seen:
                continue
            seen.add(full)

            try:
                title = el.inner_text()[:120].strip()
            except Exception:
                title = ""
            links.append({"url": full, "title": title or _slug_title(full), "thumbnail": "", "duration": 0})
        except Exception:
            continue

    return links[:25]


def _slug_title(url: str) -> str:
    """Readable title from a URL slug: /video/ocean-waves-123/ → 'ocean waves'."""
    path = urlparse(url).path.rstrip("/")
    seg = path.rsplit("/", 1)[-1]
    seg = re.sub(r"[-_]?\d{4,}$", "", seg)          # strip trailing numeric ids
    seg = re.sub(r"[-_]+", " ", seg).strip()
    return seg or url


def _find_query_links(page, base_url: str, query: str) -> list:
    """
    Looser link scan for search-results pages: same-domain links whose anchor
    text (or path) contains query words. Ranked by number of word hits.
    """
    words = [w for w in re.split(r"\W+", query.lower()) if len(w) > 2]
    if not words:
        return []
    base_host = urlparse(base_url).netloc
    scored = []
    seen = set()

    for el in page.locator("a[href]").all()[:400]:
        try:
            href = el.get_attribute("href") or ""
            if not href.startswith(("http", "/")):
                continue
            full = urljoin(base_url, href)
            parsed = urlparse(full)
            if parsed.netloc != base_host:
                continue
            if _NAV_SKIP.search(parsed.path):
                continue
            key = full.split("#")[0].rstrip("/")
            if key in seen or key == base_url.rstrip("/"):
                continue
            try:
                text = el.inner_text()[:160].strip()
            except Exception:
                text = ""
            if len(text) < 8:
                continue
            hay = (text + " " + parsed.path).lower()
            hits = sum(1 for w in words if w in hay)
            if hits == 0:
                continue
            seen.add(key)
            scored.append((hits, {"url": full, "title": text[:120], "thumbnail": "", "duration": 0}))
        except Exception:
            continue

    scored.sort(key=lambda t: -t[0])
    return [entry for _, entry in scored[:15]]


# ── Site-name + query search ───────────────────────────────────────────────────

_SEARCH_INPUT_SELECTORS = [
    'input[type="search"]',
    'input[name="q"]',
    'input[name="s"]',
    'input[name="search"]',
    'input[name="query"]',
    'input[name="keyword"]',
    'input[name="search_query"]',
    'input[placeholder*="search" i]',
    'input[aria-label*="search" i]',
    'input[id*="search" i]',
]

# Buttons that reveal a hidden search input when clicked
_SEARCH_TOGGLE_SELECTORS = [
    'button[aria-label*="search" i]',
    'a[aria-label*="search" i]',
    '[class*="search-toggle" i]',
    '[class*="search-icon" i]',
    '[data-testid*="search" i]',
]

_COMMON_SEARCH_PATHS = [
    "/search?q={q}",
    "/search?query={q}",
    "/?s={q}",                  # WordPress
    "/search/{q}",
    "/results?search_query={q}",
    "/videos?q={q}",
]


def _resolve_site_candidates(site: str) -> list:
    """Turn a site name ('dailymotion', 'pexels.com', full URL) into URL candidates."""
    site = site.strip().lower()
    if site.startswith(("http://", "https://")):
        return [site.rstrip("/")]
    site = site.replace(" ", "")
    if "." in site:
        cands = [f"https://{site}"]
        if not site.startswith("www."):
            cands.append(f"https://www.{site}")
        return cands
    return [f"https://www.{site}.com", f"https://{site}.com"]


def _try_search_box(page, query: str, deadline: float) -> bool:
    """Fill the site's own search input and submit. Returns True if URL changed."""
    before = page.url

    def _fill_and_submit(sel) -> bool:
        try:
            el = page.locator(sel).first
            if el.count() == 0 or not el.is_visible(timeout=600):
                return False
            el.click(timeout=1500)
            el.fill(query, timeout=2000)
            el.press("Enter")
            page.wait_for_timeout(2500)
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PwTimeout:
                pass
            return page.url.split("#")[0] != before.split("#")[0]
        except Exception:
            return False

    for sel in _SEARCH_INPUT_SELECTORS:
        if time.time() > deadline:
            return False
        if _fill_and_submit(sel):
            return True

    # Some sites hide the input behind a magnifier icon — click it, retry inputs
    for tsel in _SEARCH_TOGGLE_SELECTORS:
        if time.time() > deadline:
            return False
        try:
            tog = page.locator(tsel).first
            if tog.count() and tog.is_visible(timeout=500):
                tog.click(timeout=1500)
                page.wait_for_timeout(800)
                for sel in _SEARCH_INPUT_SELECTORS:
                    if _fill_and_submit(sel):
                        return True
                break
        except Exception:
            continue
    return False


def _scrape_engine_links(page, host: str, link_selector: str) -> list:
    """Collect search-engine result links that point at the target host."""
    results = []
    seen = set()
    for el in page.locator(link_selector).all()[:30]:
        try:
            href = el.get_attribute("href") or ""
            if not href.startswith("http") or host not in urlparse(href).netloc:
                continue
            if href in seen:
                continue
            seen.add(href)
            title = el.inner_text()[:120].strip()
            results.append({"url": href, "title": title or _slug_title(href), "thumbnail": "", "duration": 0})
        except Exception:
            continue
    return results[:15]


def _ddg_site_search(page, host: str, query: str) -> list:
    """DuckDuckGo HTML search restricted to the site's domain."""
    host = host.replace("www.", "")
    ddg = f"https://duckduckgo.com/html/?q={_enc(f'site:{host} {query}')}"
    try:
        page.goto(ddg, timeout=20_000)
        page.wait_for_timeout(1500)
    except Exception:
        return []
    return _scrape_engine_links(page, host, "a.result__a")


def _bing_site_search(page, host: str, query: str) -> list:
    """Bing search restricted to the site's domain."""
    host = host.replace("www.", "")
    bing = f"https://www.bing.com/search?q={_enc(f'site:{host} {query}')}"
    try:
        page.goto(bing, timeout=20_000)
        page.wait_for_timeout(2500)
    except Exception:
        return []
    return _scrape_engine_links(page, host, "li.b_algo h2 a")


def _http_site_search(host: str, query: str) -> list:
    """
    site:<domain> search via DuckDuckGo over curl_cffi — real Chrome TLS
    fingerprint survives bot checks that block headless Chromium.
    """
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return []
    import html as _html
    from urllib.parse import unquote, parse_qs

    host = host.replace("www.", "")
    try:
        r = creq.get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"site:{host} {query}"},
            impersonate="chrome", timeout=20,
        )
        if r.status_code != 200:
            return []
        text = r.text
    except Exception:
        return []

    results, seen = [], set()
    for href, title_html in re.findall(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text, re.S):
        # DDG html wraps targets in a /l/?uddg= redirect
        if href.startswith("//duckduckgo.com/l/"):
            qs = parse_qs(urlparse("https:" + href).query)
            href = unquote(qs.get("uddg", [""])[0])
        if not href.startswith("http") or host not in urlparse(href).netloc:
            continue
        href = href.replace(" ", "%20")   # DDG returns unquoted target URLs
        if href in seen:
            continue
        seen.add(href)
        title = _html.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
        results.append({"url": href, "title": title or _slug_title(href), "thumbnail": "", "duration": 0})

    # Video-looking pages first
    results.sort(key=lambda e: 0 if _VIDEO_PATH_HINT.search(urlparse(e["url"]).path) else 1)
    return results[:15]


def _engine_site_search(page, host: str, query: str) -> list:
    """site:<domain> search: curl_cffi DDG first, then in-browser DDG, then Bing."""
    results = _http_site_search(host, query)
    if results:
        return results
    results = _ddg_site_search(page, host, query)
    if results:
        return results
    return _bing_site_search(page, host, query)


def _is_challenge_page(page) -> bool:
    """Detect Cloudflare/Turnstile interstitials — no point crawling those."""
    try:
        t = (page.title() or "").lower()
        if "just a moment" in t or "attention required" in t:
            return True
        body = page.evaluate("() => document.body.innerText.slice(0, 500).toLowerCase()")
        return ("security verification" in body
                or "verify you are" in body
                or "checking your browser" in body
                or "complete the following challenge" in body)
    except Exception:
        return False


def _site_search(page, site: str, query: str, max_seconds: int = 110) -> list:
    """
    Open a website by name/URL, run the query through its search, and crawl
    the results page for videos.
    Strategy order:
      1. Site's own search box (incl. hidden behind a magnifier icon)
      2. Common search URL patterns (/search?q=, /?s=, …)
      3. site:<domain> engine search (curl_cffi DDG → browser DDG → Bing)
    Sites behind Cloudflare challenges skip straight to strategy 3.
    """
    if not site:
        raise ValueError("site is required for site_search")
    if not query:
        raise ValueError("query is required for site_search")

    deadline = time.time() + max_seconds

    base = None
    for cand in _resolve_site_candidates(site):
        try:
            page.goto(cand, timeout=20_000)
            parsed = urlparse(page.url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            break
        except Exception:
            continue
    if not base:
        raise ValueError(f"Could not reach site: {site}")

    try:
        page.wait_for_load_state("domcontentloaded", timeout=8_000)
    except PwTimeout:
        pass

    # Cloudflare/Turnstile wall → the browser can't get in; engine search can
    if _is_challenge_page(page):
        return _engine_site_search(page, urlparse(base).netloc, query)

    words = [w for w in re.split(r"\W+", query.lower()) if len(w) > 2]

    def _body_mentions_query() -> bool:
        try:
            body = page.evaluate("() => document.body.innerText.slice(0, 30000).toLowerCase()")
            return any(w in body for w in words)
        except Exception:
            return False

    landed = _try_search_box(page, query, deadline)

    if not landed:
        # Common search-URL patterns. "Strong" landing = results page actually
        # mentions the query (filters out sites that 200 a generic page).
        weak_url = None
        for tpl in _COMMON_SEARCH_PATHS:
            if time.time() > deadline:
                break
            surl = base + tpl.format(q=_enc(query))
            try:
                resp = page.goto(surl, timeout=15_000)
                if not resp or resp.status >= 400:
                    continue
                try:
                    page.wait_for_load_state("networkidle", timeout=6_000)
                except PwTimeout:
                    pass
                if _body_mentions_query():
                    landed = True
                    break
                if weak_url is None:
                    weak_url = page.url
            except Exception:
                continue
        if not landed and weak_url:
            try:
                page.goto(weak_url, timeout=15_000)
                landed = True
            except Exception:
                pass

    if not landed:
        return _engine_site_search(page, urlparse(base).netloc, query)

    # Crawl the results page (re-navigates to attach network interception)
    remaining = max(20, int(deadline - time.time()))
    results = _generic_crawl(page, page.url, max_seconds=remaining, query=query)

    def _relevance(entry) -> int:
        hay = (entry.get("title", "") + " " + entry.get("url", "")).lower()
        return sum(1 for w in words if w in hay)

    if results:
        relevant = [e for e in results if _relevance(e) > 0]
        if relevant:
            rest = [e for e in results if _relevance(e) == 0]
            return relevant + rest
        # Crawl found videos but none match the query (generic/popular page) —
        # DDG site: search is more precise; keep raw results as backstop.
        ddg = _engine_site_search(page, urlparse(base).netloc, query)
        return ddg if ddg else results

    return _engine_site_search(page, urlparse(base).netloc, query)


def _match_platform(url: str) -> str | None:
    """Return canonical watchable URL if url matches a known embed/platform pattern."""
    for pattern, template in _PLATFORM_PATTERNS:
        m = pattern.search(url)
        if m:
            return template.replace("{id}", m.group(1))
    return None


def _crawl_dom(page, base_url: str) -> tuple:
    """
    Extract video URLs from DOM.
    Returns (direct_file_urls: set, platform_urls: set).
    direct_file_urls  — mp4/webm/etc direct links
    platform_urls     — canonical YouTube/Vimeo/etc URLs extracted from iframes and <a> links
    """
    direct = set()
    platform = set()

    def add_direct(raw):
        if not raw:
            return
        raw = raw.strip()
        if raw.startswith(("data:", "blob:")):
            return
        full = urljoin(base_url, raw)
        if _VIDEO_EXTS.search(full.split("?")[0]):
            direct.add(full)

    def add_any(raw):
        if not raw:
            return
        raw = raw.strip()
        if raw.startswith(("data:", "blob:")):
            return
        full = urljoin(base_url, raw)
        canonical = _match_platform(full)
        if canonical:
            platform.add(canonical)
        elif _VIDEO_EXTS.search(full.split("?")[0]):
            direct.add(full)

    # <video> and <source> tags
    for sel in ["video[src]", "source[src]"]:
        for el in page.locator(sel).all():
            try:
                add_direct(el.get_attribute("src"))
            except Exception:
                pass

    # iframes — platform embeds (YouTube, Vimeo, Dailymotion, etc.)
    for el in page.locator("iframe[src]").all():
        try:
            add_any(el.get_attribute("src"))
        except Exception:
            pass

    # <a href> links to known video platforms
    for el in page.locator("a[href]").all():
        try:
            href = el.get_attribute("href") or ""
            if not href.startswith(("http", "/")):
                continue
            full = urljoin(base_url, href)
            canonical = _match_platform(full)
            if canonical:
                platform.add(canonical)
        except Exception:
            pass

    # data-* attributes
    for attr in _VIDEO_DATA_ATTRS:
        for el in page.locator(f"[{attr}]").all():
            try:
                add_direct(el.get_attribute(attr))
            except Exception:
                pass

    # Inline script scan
    try:
        scripts = page.evaluate("""() => {
            const texts = [];
            document.querySelectorAll('script').forEach(s => {
                if (s.textContent) texts.push(s.textContent);
            });
            return texts.join('\\n');
        }""")
        for m in re.findall(r'https?://[^\s\'"<>]+\.(?:mp4|webm|mov)(?:\?[^\s\'"<>]*)?', scripts):
            add_direct(m)
        for m in re.findall(r'https?://[^\s\'"<>]{10,}', scripts):
            canonical = _match_platform(m)
            if canonical:
                platform.add(canonical)
    except Exception:
        pass

    return direct, platform


def _crawl_hover(page, deadline: float = None) -> set:
    """Hover over thumbnail candidates to trigger lazy video preloads."""
    found = set()
    seen_pos = set()
    candidates = []

    selectors = [
        *[f"[{a}]" for a in _VIDEO_DATA_ATTRS],
        "img", ".thumbnail", ".thumb", ".card", ".product-card",
        ".video-thumb", ".preview-thumb", "li.product", "article",
    ]

    for sel in selectors:
        try:
            for el in page.locator(sel).all()[:50]:
                try:
                    box = el.bounding_box()
                    if not box:
                        continue
                    key = (round(box["x"] / 8) * 8, round(box["y"] / 8) * 8)
                    if key in seen_pos:
                        continue
                    seen_pos.add(key)
                    candidates.append(el)
                except Exception:
                    pass
        except Exception:
            pass

    def _on_req(req):
        u = req.url
        if _VIDEO_EXTS.search(u.split("?")[0]) and not u.startswith("blob:"):
            found.add(u)

    page.on("request", _on_req)

    for el in candidates[:30]:
        if deadline and time.time() > deadline:
            break
        try:
            el.scroll_into_view_if_needed(timeout=1500)
            el.hover(timeout=1500)
            page.wait_for_timeout(300)
        except Exception:
            pass

    return found


def _url_stem(url: str) -> str:
    """Stable key for deduplication — path without query string."""
    p = urlparse(url)
    base = os.path.splitext(p.path)[0].lower().rstrip("/")
    return f"{p.netloc}{base}"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _wait_any(page, selectors: list, timeout: int = 10_000):
    """Wait until any of the selectors appears."""
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in selectors:
            if page.locator(sel).count() > 0:
                return
        time.sleep(0.5)


def _enc(s: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(s)
