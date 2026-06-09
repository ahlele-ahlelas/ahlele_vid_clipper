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
) -> tuple:
    """
    Launch a Chromium browser, optionally log in, search/crawl, and return
    (results, cookie_file_path).  cookie_file_path is None if not saved.
    results is a list of {url, title, thumbnail, duration} dicts.

    source='facebook_page': query must be the Facebook page/profile URL.
    """
    source = source.lower()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
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


def _generic_crawl(page, url: str, max_seconds: int = 75) -> list:
    """
    Crawl any webpage for full videos.
    Strategy order:
      1. Platform embeds/links (YouTube/Vimeo/etc from iframes and <a> tags)
      2. Same-domain video page links (let yt-dlp handle them on clip)
      3. Direct video files from DOM (<video>, <source>)
      4. Network-intercepted streams (HLS/MP4) — large ones only (skip preview blobs)
    Hover scan removed: it only captures short hover-preview clips, not real videos.
    """
    deadline = time.time() + max_seconds
    net_direct = set()

    def _on_response(response):
        try:
            rurl  = response.url
            ctype = response.headers.get("content-type", "")
            if not (_VIDEO_MIME.search(ctype) or _VIDEO_EXTS.search(rurl.split("?")[0])):
                return
            if rurl.startswith("blob:"):
                return
            # Skip obvious hover/preview hint in URL
            if _PREVIEW_HINTS.search(rurl):
                return
            # Skip small files when content-length is known (likely preview clips)
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

    page_title = _page_title(page)

    # DOM scan — (direct_files set, platform_urls set)
    dom_direct, platform_urls = _crawl_dom(page, url)

    # Same-domain video page links
    page_links = _find_video_page_links(page, url)

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
            links.append({"url": full, "title": title or full, "thumbnail": "", "duration": 0})
        except Exception:
            continue

    return links[:25]


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
