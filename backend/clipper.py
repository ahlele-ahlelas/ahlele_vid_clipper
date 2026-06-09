from __future__ import annotations

import io
import os
import sys
import math
import time
import subprocess
import yt_dlp

import jobs
from utils import is_url, format_clip_name

QUALITY_MAP = {
    "best": "bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
}


# Sources that support keyword search via yt-dlp search prefixes
_SEARCH_PREFIXES = {
    "youtube": "ytsearch{n}:",
    "vimeo":   "vimeosearch{n}:",
    "auto":    "ytsearch{n}:",
}

# Sources that require a direct URL — keyword search not supported
_URL_ONLY_SOURCES = {"reddit", "twitter", "direct"}


def _apply_browser_cookies(ydl_opts: dict, browser: str | None, cookiefile: str | None = None) -> None:
    if cookiefile and os.path.exists(cookiefile):
        ydl_opts["cookiefile"] = cookiefile
    elif browser:
        ydl_opts["cookiesfrombrowser"] = (browser,)


def _ydl_extract(ydl_opts: dict, url: str, download: bool = False):
    """
    Run yt-dlp extract_info.
    - If DPAPI cookie-decryption fails (Chrome 127+ App-Bound Encryption),
      strip cookiesfrombrowser and retry without cookies.
    - If browser profile not found (server/Colab env), same fallback.
    Returns (info, browser_cookies_failed: bool).
    """
    buf = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = buf
    exc = None
    info = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=download)
    except Exception as e:
        exc = e
    finally:
        sys.stderr = old_stderr
        captured = buf.getvalue()
        for line in captured.splitlines():
            if "DPAPI" not in line and "decrypt" not in line.lower():
                print(line, file=sys.stderr)

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
        retry_opts = {k: v for k, v in ydl_opts.items() if k != "cookiesfrombrowser"}
        with yt_dlp.YoutubeDL(retry_opts) as ydl:
            info = ydl.extract_info(url, download=download)
        return info, True

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

    ydl_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,   # fast for both searches and channel/playlist URLs
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    }
    _apply_browser_cookies(ydl_opts, browser, cookiefile)

    info, _ = _ydl_extract(ydl_opts, search_target, download=False)

    if not info:
        return []

    # For playlists/channels/search results use entries; single video → wrap in list
    entries = info.get("entries") or [info]
    # Apply count cap (searches already cap via prefix, but channels/playlists may return all)
    n = max(1, min(count, 50))
    entries = list(entries)[:n]

    results = []
    for entry in entries:
        if not entry:
            continue
        url = entry.get("webpage_url") or entry.get("url") or ""
        if not url.startswith("http"):
            vid_id = entry.get("id", "")
            url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""
        if not url:
            continue

        thumbs = entry.get("thumbnails") or []
        thumb = entry.get("thumbnail") or (thumbs[-1].get("url") if thumbs else "")

        results.append({
            "url": url,
            "title": entry.get("title") or "Unknown",
            "duration": int(entry.get("duration") or 0),
            "thumbnail": thumb or "",
        })

    return results


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


def render_segment(job_id: str, seg_start: float, seg_end, cancel_event=None) -> str:
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
    clip_dir    = os.path.join(jobs.CLIPS_DIR, job_id)
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

    # Already rendered
    if os.path.exists(clip_path):
        _add_clip_to_job(job_id, clip_name)
        return clip_name

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

    # ── Try ranged download ────────────────────────────────────────────────────
    section_file = None
    if seg_end_val:
        tmp_prefix = os.path.join(clip_dir, f"_tmp_{int(seg_start)}_{int(seg_end_val)}")
        try:
            from yt_dlp.utils import download_range_func
            ydl_opts = {
                "format": fmt, "quiet": True, "no_warnings": True,
                "merge_output_format": "mp4",
                "outtmpl": tmp_prefix + ".%(ext)s",
                "download_ranges": download_range_func(None, [(seg_start, seg_end_val)]),
                "socket_timeout": 60,
                "http_headers": _base_headers,
                "progress_hooks": [_check_cancel],
            }
            _apply_browser_cookies(ydl_opts, job.get("browser"), job.get("cookiefile"))
            _ydl_extract(ydl_opts, url, download=True)
            matches = sorted(_glob.glob(tmp_prefix + ".*"))
            if matches:
                section_file = matches[0]
        except Exception as e:
            if cancel_event and cancel_event.is_set():
                raise Exception("Cancelled by user")
            section_file = None

    if section_file and os.path.exists(section_file):
        cmd = [_ffmpeg_bin(), "-y", "-i", section_file]
        if clip_dur:
            cmd += ["-ss", "0", "-t", str(clip_dur)]
        cmd += ["-c", "copy", clip_path]
        rc, err_bytes = _ffmpeg_run(cmd, cancel_event)
        _remove(section_file)
        if rc != 0:
            raise RuntimeError(f"FFmpeg error (code {rc}): {err_bytes.decode(errors='replace')[-400:]}")
    else:
        # ── Fallback: full download cached per job + FFmpeg seek ───────────────
        raw_base = os.path.join(jobs.RAW_DIR, job_id)
        raw_file = _find_file(raw_base)
        if not raw_file:
            if cancel_event and cancel_event.is_set():
                raise Exception("Cancelled by user")
            full_opts = {
                "format": fmt, "quiet": True, "no_warnings": True,
                "merge_output_format": "mp4",
                "outtmpl": raw_base + ".%(ext)s",
                "socket_timeout": 60,
                "http_headers": _base_headers,
                "progress_hooks": [_check_cancel],
            }
            _apply_browser_cookies(full_opts, job.get("browser"), job.get("cookiefile"))
            _ydl_extract(full_opts, url, download=True)
            raw_file = _find_file(raw_base)
        if not raw_file:
            raise RuntimeError("Downloaded file not found after full download")
        cmd = [_ffmpeg_bin(), "-y", "-ss", str(seg_start), "-i", raw_file]
        if clip_dur:
            cmd += ["-t", str(clip_dur)]
        cmd += ["-c", "copy", clip_path]
        rc, err_bytes = _ffmpeg_run(cmd, cancel_event)
        if rc != 0:
            raise RuntimeError(f"FFmpeg error (code {rc}): {err_bytes.decode(errors='replace')[-400:]}")

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
    if not s:
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


def convert_clip(job_id: str, clip_name: str, aspect_ratio: str) -> str:
    """Re-encode a clip to the given aspect ratio (center-crop). Returns output filename."""
    if aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"Unknown aspect ratio: {aspect_ratio}")

    w_r, h_r, out_w, out_h = ASPECT_RATIOS[aspect_ratio]
    input_path = os.path.join(jobs.CLIPS_DIR, job_id, clip_name)
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Source clip not found: {clip_name}")

    ratio_tag = aspect_ratio.replace(":", "x")
    base = clip_name.rsplit(".", 1)[0]
    out_name = f"{base}_{ratio_tag}.mp4"
    output_path = os.path.join(jobs.CLIPS_DIR, job_id, out_name)

    # Center-crop to target aspect ratio, then scale to standard resolution.
    # Use min() instead of if() — commas inside if() are parsed as filter
    # separators by FFmpeg before expression eval. Escape min() commas with \,
    vf = (
        f"crop=min(iw\\,ih*{w_r}/{h_r}):min(ih\\,iw*{h_r}/{w_r}),"
        f"scale={out_w}:{out_h}"
    )

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
