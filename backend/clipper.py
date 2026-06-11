from __future__ import annotations

import json
import os
import re
import sys
import math
import time
import threading
import subprocess
from urllib.parse import urlparse
import yt_dlp

import jobs
from utils import is_url, format_clip_name

# Per-job locks guarding the shared raw/full download so concurrent renders
# (e.g. "Render All" → N parallel threads on one job) don't all download the
# same file at once and corrupt it. Keyed by job_id.
_DL_LOCKS: dict[str, threading.Lock] = {}
_DL_LOCKS_GUARD = threading.Lock()


def _job_dl_lock(job_id: str) -> threading.Lock:
    with _DL_LOCKS_GUARD:
        lock = _DL_LOCKS.get(job_id)
        if lock is None:
            lock = threading.Lock()
            _DL_LOCKS[job_id] = lock
        return lock


class _CaptureLogger:
    """yt-dlp logger that captures messages to a buffer (thread-safe — no
    global sys.stderr swap). Suppresses DPAPI/cookie-decrypt noise from stderr
    but keeps the text for failure detection."""

    def __init__(self):
        self.lines: list[str] = []

    def debug(self, msg):
        self.lines.append(str(msg))

    def info(self, msg):
        self.lines.append(str(msg))

    def warning(self, msg):
        self.lines.append(str(msg))

    def error(self, msg):
        s = str(msg)
        self.lines.append(s)
        low = s.lower()
        if "dpapi" not in low and "decrypt" not in low:
            print(s, file=sys.stderr)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

QUALITY_MAP = {
    "best":  "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "4k": "bestvideo[ext=mp4][height<=2160]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best",
    "1080p": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p":  "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p":  "bestvideo[ext=mp4][height<=480]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best",
}


# Sources that support keyword search via yt-dlp search prefixes
_SEARCH_PREFIXES = {
    "youtube": "ytsearch{n}:",
    "vimeo":   "vimeosearch{n}:",
    "auto":    "ytsearch{n}:",
}

# Sources that require a direct URL — keyword search not supported
_URL_ONLY_SOURCES = {"reddit", "twitter", "direct"}


_DIRECT_VIDEO_RE = re.compile(r'\.(mp4|webm|mov|mkv|m4v|m3u8|mpd|ts)(\?|$)', re.I)


def _direct_video_meta(url: str) -> list:
    fname = urlparse(url).path.rsplit("/", 1)[-1] or "video"
    title = fname.rsplit(".", 1)[0].replace("-", " ").replace("_", " ") if "." in fname else fname
    return [{"url": url, "title": title or "Direct Video", "duration": 0, "thumbnail": ""}]


def _streamlink_url(url: str) -> str:
    """Return best stream URL via streamlink, or '' on failure/not-installed."""
    try:
        res = subprocess.run(
            ["streamlink", "--stream-url", url, "best"],
            capture_output=True, text=True, timeout=30,
        )
        out = res.stdout.strip()
        return out if out.startswith("http") else ""
    except Exception:
        return ""


def _streamlink_fetch(url: str) -> list:
    """Return metadata list via streamlink, or [] if unavailable/unsupported."""
    stream_url = _streamlink_url(url)
    if not stream_url:
        return []
    title = ""
    try:
        res = subprocess.run(
            ["streamlink", "--json", url],
            capture_output=True, text=True, timeout=20,
        )
        if res.returncode == 0:
            data = json.loads(res.stdout)
            meta = data.get("metadata") or {}
            title = meta.get("title") or meta.get("author") or ""
    except Exception:
        pass
    return [{"url": stream_url, "title": title or urlparse(url).netloc or "Stream",
             "duration": 0, "thumbnail": ""}]


def _gallery_dl_fetch(url: str) -> list:
    """Return video metadata list via gallery-dl, or [] if unavailable/unsupported."""
    try:
        res = subprocess.run(
            ["gallery-dl", "-j", url],
            capture_output=True, text=True, timeout=40,
        )
        if res.returncode not in (0, 1):
            return []
        results = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                # Array format: [status_code, url, {metadata}]
                if isinstance(item, list) and len(item) >= 2 and item[0] == 1:
                    file_url = item[1] if isinstance(item[1], str) else ""
                    meta = item[2] if len(item) > 2 and isinstance(item[2], dict) else {}
                elif isinstance(item, dict):
                    file_url = item.get("url", "")
                    meta = item
                else:
                    continue
                if not file_url.startswith("http"):
                    continue
                if not _DIRECT_VIDEO_RE.search(urlparse(file_url).path):
                    continue
                fname = urlparse(file_url).path.rsplit("/", 1)[-1]
                title = (meta.get("title") or
                         meta.get("filename", "").rsplit(".", 1)[0] or
                         fname.rsplit(".", 1)[0])
                results.append({
                    "url": file_url,
                    "title": title or fname,
                    "duration": int(meta.get("duration") or 0),
                    "thumbnail": meta.get("thumbnail") or "",
                })
            except Exception:
                continue
        return results[:10]
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _apply_browser_cookies(ydl_opts: dict, browser: str | None, cookiefile: str | None = None) -> None:
    if cookiefile and os.path.exists(cookiefile):
        ydl_opts["cookiefile"] = cookiefile
    elif browser:
        ydl_opts["cookiesfrombrowser"] = (browser,)


def _curl_cffi_available() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


_BLOCKED_HINTS = ("403", "forbidden", "blocked", "impersonat", "429", "captcha", "cloudflare")


def _ydl_extract(ydl_opts: dict, url: str, download: bool = False):
    """
    Run yt-dlp extract_info.
    - If DPAPI cookie-decryption fails (Chrome 127+ App-Bound Encryption),
      strip cookiesfrombrowser and retry without cookies.
    - If browser profile not found (server/Colab env), same fallback.
    - If the site blocks the request (403/Cloudflare/captcha) and curl_cffi
      is installed, retry with full browser TLS impersonation.
    Returns (info, browser_cookies_failed: bool).
    """
    log = _CaptureLogger()
    opts = {**ydl_opts, "logger": log}
    exc = None
    info = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=download)
    except Exception as e:
        exc = e

    captured = log.text
    exc_str = str(exc) if exc is not None else ""
    _combined = (captured + exc_str).lower()
    browser_cookie_fail = (
        "dpapi" in _combined
        or "could not find" in _combined
        or "cookie database" in _combined
        or "could not copy" in _combined
        or "app-bound" in _combined
    )

    # Retry without browser cookies on any cookie-related failure OR any
    # exception when cookiesfrombrowser is set (handles platform-specific
    # decryption errors that don't match known message patterns).
    if browser_cookie_fail or ("cookiesfrombrowser" in ydl_opts and exc is not None):
        retry_opts = {k: v for k, v in opts.items() if k != "cookiesfrombrowser"}
        try:
            with yt_dlp.YoutubeDL(retry_opts) as ydl:
                info = ydl.extract_info(url, download=download)
            return info, True
        except Exception as e:
            exc = e
            _combined += str(e).lower()

    # Blocked by the site → retry impersonating a real Chrome TLS fingerprint
    if exc is not None and any(h in _combined for h in _BLOCKED_HINTS) and _curl_cffi_available():
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            imp_opts = {k: v for k, v in opts.items() if k != "cookiesfrombrowser"}
            imp_opts["impersonate"] = ImpersonateTarget("chrome")
            with yt_dlp.YoutubeDL(imp_opts) as ydl:
                info = ydl.extract_info(url, download=download)
            return info, False
        except Exception:
            pass  # fall through to original error

    if exc is not None:
        raise exc

    return info, False


def fetch_metadata(query: str, source_type: str = "auto", browser: str | None = None, cookiefile: str | None = None, count: int = 5) -> list:
    """Return list of video metadata dicts without downloading."""
    direct = is_url(query)

    if not direct:
        src = source_type.lower() if source_type else "auto"
        if src in _URL_ONLY_SOURCES:
            raise ValueError(
                f"Source '{source_type}' does not support keyword search. "
                "Please paste a direct URL instead."
            )
        n = max(1, min(count, 50))
        prefix_tpl = _SEARCH_PREFIXES.get(src, "ytsearch{n}:")
        prefix = prefix_tpl.replace("{n}", str(n))
        search_target = f"{prefix}{query}"
    else:
        search_target = query

    if direct and _DIRECT_VIDEO_RE.search(urlparse(query).path):
        return _direct_video_meta(query)

    _headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    ydl_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "http_headers": _headers,
    }
    if direct:
        # Full extraction for direct URLs — extract_flat causes yt-dlp to return
        # the page's listing/related entries instead of the target video.
        ydl_opts["extract_flat"] = False
    else:
        ydl_opts["extract_flat"] = True   # fast for search results / playlists
    _apply_browser_cookies(ydl_opts, browser, cookiefile)

    ydl_exc = None
    info = None
    try:
        info, _ = _ydl_extract(ydl_opts, search_target, download=False)
    except Exception as e:
        ydl_exc = e

    results = []
    if info:
        # Direct URL → return exactly that one video; search/playlist → apply count cap
        if direct:
            entries = [info]
        else:
            entries = list(info.get("entries") or [info])
            n = max(1, min(count, 50))
            entries = entries[:n]

        for entry in entries:
            if not entry:
                continue
            entry_url = entry.get("webpage_url") or entry.get("url") or ""
            if not entry_url.startswith("http"):
                vid_id = entry.get("id", "")
                entry_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""
            if not entry_url:
                continue

            thumbs = entry.get("thumbnails") or []
            thumb = entry.get("thumbnail") or (thumbs[-1].get("url") if thumbs else "")

            results.append({
                "url": entry_url,
                "title": entry.get("title") or "Unknown",
                "duration": int(entry.get("duration") or 0),
                "thumbnail": thumb or "",
            })

    if results:
        return results

    # yt-dlp found nothing — try alternative extractors (direct URLs only)
    if direct:
        sl = _streamlink_fetch(query)
        if sl:
            return sl
        gd = _gallery_dl_fetch(query)
        if gd:
            return gd

    if ydl_exc is not None:
        raise ydl_exc
    return []


_OG_IMAGE_PROPS = (
    "og:image:secure_url", "og:image:url", "og:image",
    "twitter:image:src", "twitter:image",
)


def fetch_thumbnail(url: str) -> str:
    """
    Return a poster image URL for a video page by scraping its og:image /
    twitter:image meta tags. Used for crawl/site-search results that have no
    thumbnail from the extractor. Lightweight HTTP — no browser, no yt-dlp.
    """
    if not url or not url.startswith("http"):
        return ""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    html = ""
    # curl_cffi first (real Chrome TLS beats bot walls), then stdlib fallback
    if _curl_cffi_available():
        try:
            from curl_cffi import requests as creq
            r = creq.get(url, headers=headers, impersonate="chrome", timeout=10)
            if r.status_code == 200:
                html = r.text
        except Exception:
            html = ""
    if not html:
        try:
            import urllib.request
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read(200_000).decode("utf-8", "replace")
        except Exception:
            return ""

    # Only need the <head> — meta tags live there; cap work on huge pages
    head = html[:200_000]
    for prop in _OG_IMAGE_PROPS:
        p = re.escape(prop)
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{p}["\'][^>]*\bcontent=["\']([^"\']+)["\']',
            head, re.I,
        ) or re.search(
            rf'<meta[^>]+\bcontent=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']{p}["\']',
            head, re.I,
        )
        if m:
            thumb = m.group(1).strip()
            if thumb.startswith("//"):
                thumb = "https:" + thumb
            elif thumb.startswith("/"):
                from urllib.parse import urljoin
                thumb = urljoin(url, thumb)
            if thumb.startswith("http"):
                return thumb
    return ""


def download_and_clip(job_id: str) -> None:
    """
    Phase 1 only: fetch video metadata, calculate clip segments, mark job ready.
    Actual download + FFmpeg trim happens on demand via render_segment().
    """
    import threading as _threading

    job = jobs.get(job_id)
    if not job:
        return

    url = job["url"]

    _ticker_stop  = _threading.Event()
    _ticker_start = time.time()

    def _start_ticker(msg: str):
        _ticker_stop.clear()
        def _tick():
            while not _ticker_stop.wait(4):
                elapsed = int(time.time() - _ticker_start)
                jobs.update(job_id, message=f"{msg} ({elapsed}s)")
        _threading.Thread(target=_tick, daemon=True).start()

    def _stop_ticker():
        _ticker_stop.set()

    jobs.update(job_id, status="processing", progress=3, message="Extracting video info…")
    _start_ticker("Extracting video info…")

    _base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    meta_opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "socket_timeout": 60,
        "http_headers": _base_headers,
    }
    _apply_browser_cookies(meta_opts, job.get("browser"), job.get("cookiefile"))

    try:
        info, _ = _ydl_extract(meta_opts, url, download=False)
    except yt_dlp.utils.DownloadError as e:
        _stop_ticker()
        jobs.update(job_id, status="failed", error=str(e), message=_dl_error_msg(str(e)))
        return
    except Exception as e:
        _stop_ticker()
        jobs.update(job_id, status="failed", error=str(e),
                    message=f"Failed to fetch video info: {e}")
        return

    _stop_ticker()

    duration  = int(info.get("duration") or 0)
    vid_title = info.get("title") or ""
    thumbnail = info.get("thumbnail") or ""

    # ── Build segment plan ─────────────────────────────────────────────────────
    start_req    = job.get("start_time")
    end_req      = job.get("end_time")
    clip_dur_req = job.get("clip_duration") or 30

    if start_req is not None and end_req is not None:
        raw_segs = [(float(start_req), float(end_req))]
    elif duration:
        n = math.ceil(duration / clip_dur_req)
        raw_segs = [
            (i * clip_dur_req, min((i + 1) * clip_dur_req, duration))
            for i in range(n)
        ]
    else:
        raw_segs = [(0.0, None)]   # unknown duration → full video

    clips_pending = []
    for i, (s, e) in enumerate(raw_segs, 1):
        key   = format_clip_name(i, s, e) if e is not None else f"clip_{i:02d}_full.mp4"
        label = f"{fmtdur(int(s))} – {fmtdur(int(e)) if e is not None else 'end'}"
        clips_pending.append({"start": s, "end": e, "key": key, "label": label})

    n = len(clips_pending)
    jobs.update(
        job_id,
        title=vid_title, duration=duration, thumbnail=thumbnail,
        status="ready", progress=100,
        message=f"Ready — {n} clip{'s' if n != 1 else ''}",
        clips_pending=clips_pending,
        clips=[],
    )


def render_full_video(job_id: str, cancel_event=None) -> str:
    """Download the complete video without any clipping. Returns clip_name."""
    job = jobs.get(job_id)
    if not job:
        raise ValueError("Job not found")

    url         = job["url"]
    quality_key = job.get("quality") or "best"
    fmt         = QUALITY_MAP.get(quality_key, QUALITY_MAP["best"])

    out_dir = os.path.join(jobs.CLIPS_DIR, job_id)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(jobs.RAW_DIR, exist_ok=True)

    out_name = "full_video.mp4"
    out_path = os.path.join(out_dir, out_name)
    if os.path.exists(out_path):
        _add_clip_to_job(job_id, out_name)
        return out_name

    _base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _check_cancel(d):
        if cancel_event and cancel_event.is_set():
            raise Exception("Cancelled by user")

    raw_base = os.path.join(jobs.RAW_DIR, job_id)
    raw_file = _find_file(raw_base)
    if not raw_file:
        # Serialize per job so a concurrent segment render and this full render
        # share ONE download of the raw file instead of clobbering each other.
        with _job_dl_lock(job_id):
            raw_file = _find_file(raw_base)   # re-check after acquiring lock
            if not raw_file:
                if cancel_event and cancel_event.is_set():
                    raise Exception("Cancelled by user")
                if not _parallel_http_download(url, raw_base + ".mp4",
                                               headers=_base_headers, cancel_event=cancel_event):
                    full_opts = {
                        "format": fmt, "quiet": True, "no_warnings": True,
                        "merge_output_format": "mp4",
                        "outtmpl": raw_base + ".%(ext)s",
                        **_fast_dl_opts(),
                        "http_headers": _base_headers,
                        "progress_hooks": [_check_cancel],
                    }
                    _apply_browser_cookies(full_opts, job.get("browser"), job.get("cookiefile"))
                    _apply_fast_dl(full_opts)
                    _ydl_extract(full_opts, url, download=True)
                raw_file = _find_file(raw_base)

    if not raw_file:
        raise RuntimeError("Failed to download full video")

    _audio_args = ["-map", "0:v?", "-map", "0:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k"]
    cmd = [_ffmpeg_bin(), "-y", "-i", raw_file] + _audio_args + [out_path]
    rc, err_bytes = _ffmpeg_run(cmd, cancel_event)
    if rc != 0:
        raise RuntimeError(f"FFmpeg error (code {rc}): {err_bytes.decode(errors='replace')[-400:]}")

    _add_clip_to_job(job_id, out_name)
    return out_name


def render_segment(job_id: str, seg_start: float, seg_end, cancel_event=None, preview_only: bool = False) -> str:
    """
    Download and trim one clip [seg_start, seg_end] on demand.
    seg_end=None → full video from seg_start.
    Returns clip filename and updates job.clips.
    """
    import glob as _glob

    job = jobs.get(job_id)
    if not job:
        raise ValueError("Job not found")

    url         = job["url"]
    quality_key = job.get("quality") or "best"
    fmt         = QUALITY_MAP.get(quality_key, QUALITY_MAP["best"])
    clip_dir    = os.path.join(jobs.PREVIEW_DIR if preview_only else jobs.CLIPS_DIR, job_id)
    os.makedirs(clip_dir, exist_ok=True)
    os.makedirs(jobs.RAW_DIR, exist_ok=True)

    # Match clips_pending entry by start/end for consistent naming
    clip_name = None
    for seg in (job.get("clips_pending") or []):
        s_match = abs(seg["start"] - seg_start) < 0.5
        e_match = (seg["end"] is None and seg_end is None) or (
            seg["end"] is not None and seg_end is not None and abs(seg["end"] - seg_end) < 0.5
        )
        if s_match and e_match:
            clip_name = seg["key"]
            break
    if not clip_name:
        idx = len(job.get("clips") or []) + 1
        clip_name = format_clip_name(idx, seg_start, seg_end) if seg_end is not None else f"clip_{idx:02d}_full.mp4"

    clip_path = os.path.join(clip_dir, clip_name)

    # Use cached clips (both permanent and preview) when they pass stream
    # validation — repeat previews shouldn't re-download from the network.
    # Audio not required here: a legitimately silent source would otherwise
    # re-render forever. Missing-audio merges are caught at section stage.
    if os.path.exists(clip_path):
        if _has_video(clip_path):
            if not preview_only:
                _add_clip_to_job(job_id, clip_name)
            return clip_name
        _remove(clip_path)   # broken artifact (e.g. audio-only merge) — re-render

    _base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    seg_end_val = float(seg_end) if seg_end is not None else 0.0
    clip_dur    = (seg_end_val - seg_start) if seg_end_val else None

    def _check_cancel(d):
        if cancel_event and cancel_event.is_set():
            raise Exception("Cancelled by user")

    raw_base = os.path.join(jobs.RAW_DIR, job_id)

    # ── Try ranged download ────────────────────────────────────────────────────
    # Attempt 1: the user's quality format (bestvideo+bestaudio split DASH) —
    # the only way to get >720p from YouTube; combined streams cap at 720p.
    # Split DASH sections occasionally merge without audio, so verify with
    # ffprobe and fall back to a combined progressive stream (attempt 2).
    # The job remembers which attempt worked (range_fmt memo) so later renders
    # skip straight to it instead of re-failing attempt 1 every time.
    # Skip ranged entirely if the full video is already cached (e.g. a prior clip
    # or "Render All" triggered a full download) — slicing the local file is
    # instant vs. a fresh network fetch per clip.
    # For short videos with many pending clips ("Render All"), ONE full download
    # shared via the per-job lock beats N separate section downloads — skip
    # ranged and let the fallback path download once, then slice locally.
    combined_fmt = "best[ext=mp4][acodec!=none]/best[acodec!=none]/best"
    pending_n    = len(job.get("clips_pending") or [])
    vid_duration = job.get("duration") or 0
    prefer_full  = bool(vid_duration and vid_duration <= 900 and pending_n >= 4)

    section_file = None
    if seg_end_val and not prefer_full and not _find_file(raw_base):
        tmp_prefix = os.path.join(clip_dir, f"_tmp_{int(seg_start)}_{int(seg_end_val)}")
        from yt_dlp.utils import download_range_func
        # Stale partials from a previous crashed run would confuse candidate
        # selection — clear them before starting.
        for m in _glob.glob(tmp_prefix + ".*"):
            _remove(m)
        # [acodec!=none] ensures the combined fallback has an audio track —
        # without it, yt-dlp picks DASH video-only mp4 streams (no audio).
        memo = job.get("range_fmt")
        if memo == "combined":
            range_fmts = [combined_fmt]
        else:
            range_fmts = [fmt, combined_fmt]
        for range_fmt in range_fmts:
            try:
                ydl_opts = {
                    "format": range_fmt, "quiet": True, "no_warnings": True,
                    "merge_output_format": "mp4",
                    "noplaylist": True,
                    "outtmpl": tmp_prefix + ".%(ext)s",
                    "download_ranges": download_range_func(None, [(seg_start, seg_end_val)]),
                    **_fast_dl_opts(),
                    "http_headers": _base_headers,
                    "progress_hooks": [_check_cancel],
                }
                _apply_browser_cookies(ydl_opts, job.get("browser"), job.get("cookiefile"))
                _apply_fast_dl(ydl_opts)
                _ydl_extract(ydl_opts, url, download=True)
                cand = _final_section_file(tmp_prefix, _glob)
                # Section must carry BOTH streams — split-DASH merges can drop
                # either one; a bad pick here renders a black/silent clip.
                if cand and _has_video(cand) and _has_audio(cand):
                    section_file = cand
                    jobs.update(job_id, range_fmt=("split" if range_fmt == fmt else "combined"))
                    break
                for m in _glob.glob(tmp_prefix + ".*"):
                    _remove(m)
            except Exception:
                if cancel_event and cancel_event.is_set():
                    raise Exception("Cancelled by user")
                for m in _glob.glob(tmp_prefix + ".*"):
                    _remove(m)

    # -c:v copy keeps original video; -c:a aac converts to browser-compatible audio.
    # -map 0:v? and 0:a? are optional — won't fail if a stream is missing.
    _audio_args = ["-map", "0:v?", "-map", "0:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k"]

    if section_file and os.path.exists(section_file):
        cmd = [_ffmpeg_bin(), "-y", "-i", section_file]
        if clip_dur:
            cmd += ["-ss", "0", "-t", str(clip_dur)]
        cmd += _audio_args + [clip_path]
        rc, err_bytes = _ffmpeg_run(cmd, cancel_event)
        _remove(section_file)
        if rc != 0:
            raise RuntimeError(f"FFmpeg error (code {rc}): {err_bytes.decode(errors='replace')[-400:]}")
    else:
        # ── Fallback: full download cached per job + FFmpeg seek ───────────────
        # Serialize per job so concurrent renders ("Render All") share ONE
        # download instead of each writing the same raw file at once.
        raw_file = _find_file(raw_base)
        if not raw_file:
            with _job_dl_lock(job_id):
                raw_file = _find_file(raw_base)   # re-check: another thread may have finished
                if not raw_file:
                    if cancel_event and cancel_event.is_set():
                        raise Exception("Cancelled by user")
                    if not _parallel_http_download(url, raw_base + ".mp4",
                                                   headers=_base_headers, cancel_event=cancel_event):
                        full_opts = {
                            "format": fmt, "quiet": True, "no_warnings": True,
                            "merge_output_format": "mp4",
                            "outtmpl": raw_base + ".%(ext)s",
                            **_fast_dl_opts(),
                            "http_headers": _base_headers,
                            "progress_hooks": [_check_cancel],
                        }
                        _apply_browser_cookies(full_opts, job.get("browser"), job.get("cookiefile"))
                        _apply_fast_dl(full_opts)
                        _ydl_extract(full_opts, url, download=True)
                    raw_file = _find_file(raw_base)
        if not raw_file:
            # Last resort: streamlink → ffmpeg direct stream
            _sl_url = _streamlink_url(url)
            if _sl_url:
                cmd = [_ffmpeg_bin(), "-y", "-ss", str(seg_start), "-i", _sl_url]
                if clip_dur:
                    cmd += ["-t", str(clip_dur)]
                cmd += _audio_args + [clip_path]
                rc, err_bytes = _ffmpeg_run(cmd, cancel_event)
                if rc != 0:
                    raise RuntimeError(
                        f"FFmpeg (streamlink fallback) error (code {rc}): "
                        f"{err_bytes.decode(errors='replace')[-400:]}"
                    )
                if not preview_only:
                    _add_clip_to_job(job_id, clip_name)
                return clip_name
            # IDM fallback — Windows only, silent download then ffmpeg clip
            idm_raw = _idm_download(url, jobs.RAW_DIR, f"{job_id}_idm.mp4")
            if idm_raw and os.path.isfile(idm_raw):
                cmd = [_ffmpeg_bin(), "-y", "-ss", str(seg_start), "-i", idm_raw]
                if clip_dur:
                    cmd += ["-t", str(clip_dur)]
                cmd += _audio_args + [clip_path]
                rc, err_bytes = _ffmpeg_run(cmd, cancel_event)
                _remove(idm_raw)
                if rc != 0:
                    raise RuntimeError(
                        f"FFmpeg (IDM fallback) error (code {rc}): "
                        f"{err_bytes.decode(errors='replace')[-400:]}"
                    )
                if not preview_only:
                    _add_clip_to_job(job_id, clip_name)
                return clip_name
            raise RuntimeError("Downloaded file not found after full download")
        cmd = [_ffmpeg_bin(), "-y", "-ss", str(seg_start), "-i", raw_file]
        if clip_dur:
            cmd += ["-t", str(clip_dur)]
        cmd += _audio_args + [clip_path]
        rc, err_bytes = _ffmpeg_run(cmd, cancel_event)
        if rc != 0:
            raise RuntimeError(f"FFmpeg error (code {rc}): {err_bytes.decode(errors='replace')[-400:]}")

    if not preview_only:
        _add_clip_to_job(job_id, clip_name)
    return clip_name


def _add_clip_to_job(job_id: str, clip_name: str) -> None:
    job = jobs.get(job_id)
    existing = list(job.get("clips") or [])
    if clip_name not in existing:
        jobs.update(job_id, clips=existing + [clip_name])


def _ffmpeg_run(cmd: list, cancel_event=None, timeout: int = 300):
    """Run FFmpeg via Popen so it can be killed mid-run if cancel_event is set.
    Returns (returncode, stderr_bytes)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + timeout
    while True:
        try:
            _, stderr = proc.communicate(timeout=1)
            return proc.returncode, stderr
        except subprocess.TimeoutExpired:
            if cancel_event and cancel_event.is_set():
                proc.kill()
                proc.communicate()
                raise Exception("Cancelled by user")
            if time.time() > deadline:
                proc.kill()
                proc.communicate()
                raise TimeoutError(f"FFmpeg timed out after {timeout}s")


def fmtdur(s: int) -> str:
    if s is None:
        return "unknown"
    m, sec = divmod(s, 60)
    h, m   = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _dl_error_msg(err: str) -> str:
    e = err.lower()
    if "no media" in e or "no video" in e:
        return "This post has no downloadable video (text or image post)."
    if "private" in e or "login" in e or "authentication" in e:
        return "Video is private or requires login."
    if "drm" in e:
        return "Video is DRM-protected and cannot be downloaded."
    if "geolocation" in e or "geo_restricted" in e or "not available in your country" in e:
        return "Video is geo-restricted (not available in your region)."
    if "copyright" in e or "removed" in e:
        return "Video was removed or blocked due to copyright."
    if "403" in err or "blocked" in e:
        return "Access blocked by the hosting site (403 Forbidden)."
    if "404" in err or "not found" in e:
        return "Video not found (deleted or invalid URL)."
    if "unsupported url" in e:
        return "yt-dlp can't handle this URL — paste a direct video URL, not a listing/channel page."
    if "impersonat" in e:
        return "Site requires browser impersonation — run: pip install curl_cffi"
    if "getaddrinfo" in e or "errno 11001" in e or "transport" in e:
        return "Network error — DNS failed or request blocked. Try again."
    return f"Download failed: {err}"


ASPECT_RATIOS = {
    "9:16": (9,  16, 1080, 1920),
    "4:5":  (4,  5,  1080, 1350),
    "1:1":  (1,  1,  1080, 1080),
    "16:9": (16, 9,  1920, 1080),
    "4:3":  (4,  3,  1440, 1080),
}


def convert_clip(job_id: str, clip_name: str, aspect_ratio: str, preview_only: bool = False) -> str:
    """Re-encode a clip to the given aspect ratio (center-crop). Returns output filename."""
    if aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"Unknown aspect ratio: {aspect_ratio}")

    w_r, h_r, out_w, out_h = ASPECT_RATIOS[aspect_ratio]

    # Find source — check clips dir first, then preview dir
    input_path = os.path.join(jobs.CLIPS_DIR, job_id, clip_name)
    if not os.path.exists(input_path):
        preview_src = os.path.join(jobs.PREVIEW_DIR, job_id, clip_name)
        if os.path.exists(preview_src):
            input_path = preview_src
        else:
            raise FileNotFoundError(
                f"Source clip not found: {clip_name}. "
                f"Checked CLIPS={input_path} "
                f"and PREVIEW={preview_src}"
            )

    ratio_tag = aspect_ratio.replace(":", "x")
    base = clip_name.rsplit(".", 1)[0]
    out_name = f"{base}_{ratio_tag}.mp4"
    out_dir = os.path.join(jobs.PREVIEW_DIR if preview_only else jobs.CLIPS_DIR, job_id)
    os.makedirs(out_dir, exist_ok=True)
    output_path = os.path.join(out_dir, out_name)

    # Scale up to fill target box (maintains AR, no letterbox), then center-crop to exact size.
    vf = f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h}"

    cmd = [
        _ffmpeg_bin(), "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[-500:])
    return out_name


def _parallel_http_download(url: str, out_path: str, n_threads: int = 16,
                             headers: dict | None = None, cancel_event=None) -> bool:
    """
    Download url using N parallel HTTP range-request threads (IDM-style).
    Returns True on success; False if server doesn't support ranges or any part fails.
    Requires only stdlib — no extra packages.
    """
    import urllib.request
    import tempfile
    from concurrent.futures import ThreadPoolExecutor, as_completed

    hdrs: dict = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        **(headers or {}),
    }

    def _is_html(ctype: str) -> bool:
        c = (ctype or "").lower()
        return "text/html" in c or "application/xhtml" in c

    total = 0
    accept = ""

    # HEAD to confirm range support and get total size
    try:
        req = urllib.request.Request(url, headers=hdrs, method="HEAD")
        with urllib.request.urlopen(req, timeout=20) as r:
            # url is a webpage (e.g. a YouTube watch page), not a media file —
            # never byte-range download HTML or we'd save the page as .mp4.
            if _is_html(r.headers.get("Content-Type")):
                return False
            total = int(r.headers.get("Content-Length") or 0)
            accept = (r.headers.get("Accept-Ranges") or "").lower()
    except Exception:
        pass

    # Many CDNs omit Content-Length on HEAD but return it on GET with a Range probe
    if not total or accept != "bytes":
        try:
            probe_hdrs = {**hdrs, "Range": "bytes=0-0"}
            req2 = urllib.request.Request(url, headers=probe_hdrs)
            with urllib.request.urlopen(req2, timeout=20) as r:
                if _is_html(r.headers.get("Content-Type")):
                    return False
                if r.status == 206:
                    accept = "bytes"
                    cr = r.headers.get("Content-Range") or ""  # bytes 0-0/TOTAL
                    if "/" in cr:
                        total = int(cr.rsplit("/", 1)[1])
                    if not total:
                        total = int(r.headers.get("Content-Length") or 0)
        except Exception:
            pass

    if not total or accept != "bytes":
        return False

    # Keep each part ≥256 KB — tiny files don't benefit from splitting and a
    # naive total//n_threads would give chunk=0 (broken ranges) when total<n.
    n_threads = max(1, min(n_threads, total // (256 * 1024) or 1))
    chunk = total // n_threads
    ranges = [
        (i * chunk, total - 1 if i == n_threads - 1 else (i + 1) * chunk - 1)
        for i in range(n_threads)
    ]
    tmp_dir = os.path.dirname(out_path) or "."
    part_paths: list[str | None] = [None] * n_threads

    def _get_part(idx: int) -> str:
        start, end = ranges[idx]
        h = {**hdrs, "Range": f"bytes={start}-{end}"}
        req = urllib.request.Request(url, headers=h)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".part", dir=tmp_dir)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                if r.status != 206:
                    raise RuntimeError(f"Range not supported: HTTP {r.status}")
                while True:
                    if cancel_event and cancel_event.is_set():
                        raise Exception("Cancelled")
                    data = r.read(65536)
                    if not data:
                        break
                    tmp.write(data)
            tmp.close()
            return tmp.name
        except Exception:
            tmp.close()
            _remove(tmp.name)
            raise

    try:
        with ThreadPoolExecutor(max_workers=n_threads) as ex:
            futs = {ex.submit(_get_part, i): i for i in range(n_threads)}
            for fut in as_completed(futs):
                part_paths[futs[fut]] = fut.result()  # raises on any failure

        with open(out_path, "wb") as out:
            for pp in part_paths:
                with open(pp, "rb") as ph:
                    while True:
                        d = ph.read(65536)
                        if not d:
                            break
                        out.write(d)
                _remove(pp)
        return True
    except Exception:
        for pp in part_paths:
            if pp:
                _remove(pp)
        return False


def _idm_exe() -> str:
    """Find IDMan.exe via registry then common install paths. Returns '' if not installed."""
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for subkey in (
                r"SOFTWARE\Internet Download Manager",
                r"SOFTWARE\WOW6432Node\Internet Download Manager",
            ):
                try:
                    with winreg.OpenKey(root, subkey) as k:
                        path, _ = winreg.QueryValueEx(k, "ExePath")
                        if os.path.isfile(path):
                            return path
                except Exception:
                    pass
    except ImportError:
        pass
    for candidate in (
        r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe",
        r"C:\Program Files\Internet Download Manager\IDMan.exe",
    ):
        if os.path.isfile(candidate):
            return candidate
    return ""


def _idm_download(url: str, save_dir: str, filename: str, timeout: int = 300) -> str:
    """
    Download url via IDM silently (/q /n flags).
    Returns output path once size stabilises for 3 consecutive seconds.
    Returns '' if IDM not installed or download times out.
    """
    exe = _idm_exe()
    if not exe:
        return ""
    os.makedirs(save_dir, exist_ok=True)
    out_path = os.path.join(save_dir, filename)
    _remove(out_path)
    subprocess.Popen(
        [exe, "/d", url, "/p", save_dir, "/f", filename, "/q", "/n"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + timeout
    last_size = -1
    stable = 0
    while time.time() < deadline:
        time.sleep(1)
        if not os.path.isfile(out_path):
            continue
        sz = os.path.getsize(out_path)
        if sz > 0 and sz == last_size:
            stable += 1
            if stable >= 3:
                return out_path
        else:
            stable = 0
            last_size = sz
    return ""


def _fast_dl_opts() -> dict:
    """Shared yt-dlp tuning: parallel fragments, big buffers, aggressive retries."""
    return {
        "concurrent_fragment_downloads": 16,
        "buffersize": 1024 * 1024,
        "http_chunk_size": 10 * 1024 * 1024,
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 10,
        "skip_unavailable_fragments": True,
    }


def _aria2c_available() -> bool:
    import shutil
    return bool(shutil.which("aria2c"))


def _apply_fast_dl(opts: dict) -> None:
    """Inject aria2c external downloader if available; otherwise keep stdlib downloader."""
    if _aria2c_available():
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {
            "aria2c": [
                "--max-connection-per-server=16",
                "--min-split-size=1M",
                "--split=16",
                "--file-allocation=none",   # skip preallocation — faster start on Windows
                "--continue=true",
                "--quiet=true",
            ]
        }


def _ffmpeg_bin() -> str:
    import shutil
    # Try PATH first, then common Windows location
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidate = r"C:\ffmpeg\bin\ffmpeg.exe"
    if os.path.exists(candidate):
        return candidate
    return "ffmpeg"  # last resort — let subprocess throw a useful error


def _find_file(base: str) -> str:
    for ext in ("mp4", "mkv", "webm", "mov", "avi", "m4v"):
        path = f"{base}.{ext}"
        if os.path.exists(path):
            return path
    return ""


_INTERMEDIATE_RE = re.compile(r"\.f\d+\.\w+(\.part)?$|\.part$|\.ytdl$", re.I)


def _final_section_file(tmp_prefix: str, _glob) -> str:
    """Pick the final merged section file, never a yt-dlp intermediate.
    Plain glob is unsafe: split-DASH downloads leave .fNNN.* / .part files
    that can sort before the merged output (→ audio-only clips)."""
    exact = tmp_prefix + ".mp4"
    if os.path.exists(exact):
        return exact
    finals = [m for m in sorted(_glob.glob(tmp_prefix + ".*"))
              if not _INTERMEDIATE_RE.search(m)]
    return finals[0] if finals else ""


def _has_video(filepath: str) -> bool:
    import shutil
    ffprobe = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=30,
        )
        return "video" in result.stdout
    except Exception:
        return False


def _has_audio(filepath: str) -> bool:
    import shutil
    ffprobe = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=30,
        )
        return "audio" in result.stdout
    except Exception:
        return False


def _probe_duration(filepath: str) -> float:
    import shutil
    ffprobe = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except Exception:
        pass
