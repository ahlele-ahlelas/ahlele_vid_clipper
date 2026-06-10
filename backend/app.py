from __future__ import annotations

import io
import os
import re
import sys
import threading
import zipfile

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

import uuid as _uuid

import jobs
import clipper
import browser_search as bs
from utils import check_ffmpeg

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
COOKIES_DIR = os.path.join(BASE_DIR, "tmp", "cookies")

_conversions: dict = {}      # conv_id -> {status, output, job_id, error}
_renders: dict       = {}   # render_id -> {status, output, job_id, error}
_render_cancels: dict = {}  # render_id -> threading.Event
_cookie_sessions: dict = {}  # session_id -> file_path

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
CORS(app)

FFMPEG_OK = check_ffmpeg()


# ── Static ─────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return app.send_static_file("index.html")


# ── API ────────────────────────────────────────────────────────────────────────

@app.get("/api/ffmpeg-status")
def ffmpeg_status():
    return jsonify({"available": FFMPEG_OK})


def _resolve_cookiefile(session_id: str) -> str | None:
    """Return cookiefile path for a session_id, or None."""
    path = _cookie_sessions.get(session_id)
    return path if path and os.path.exists(path) else None


@app.post("/api/cookies")
def upload_cookies():
    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400
    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".txt"):
        return jsonify({"error": "Must be a .txt file."}), 400

    os.makedirs(COOKIES_DIR, exist_ok=True)
    session_id = str(_uuid.uuid4())
    path = os.path.join(COOKIES_DIR, f"{session_id}.txt")
    f.save(path)
    _cookie_sessions[session_id] = path

    # Auto-cleanup after 1 hour
    def _cleanup():
        _cookie_sessions.pop(session_id, None)
        try:
            os.remove(path)
        except OSError:
            pass
    t = threading.Timer(3600, _cleanup)
    t.daemon = True
    t.start()

    return jsonify({"session_id": session_id}), 201


@app.post("/api/search")
def search():
    data        = request.get_json(force=True, silent=True) or {}
    query       = (data.get("query")            or "").strip()
    source_type = (data.get("source_type")      or "auto").strip().lower()
    browser     = (data.get("browser")          or "").strip().lower() or None
    session_id  = (data.get("cookie_session_id") or "").strip() or None
    cookiefile  = _resolve_cookiefile(session_id) if session_id else None
    try:
        count = int(data.get("count") or 5)
    except (ValueError, TypeError):
        count = 5

    if not query:
        return jsonify({"error": "Query is required."}), 400
    try:
        results = clipper.fetch_metadata(query, source_type, browser, cookiefile, count)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not results:
        return jsonify({"results": [], "message": "No compatible videos found for this search."})
    return jsonify({"results": results})


@app.post("/api/clip")
def clip():
    if not FFMPEG_OK:
        return jsonify({"error": "FFmpeg is not installed. See the banner for installation instructions."}), 500

    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL is required."}), 400

    clip_duration = data.get("clip_duration")
    if clip_duration is not None:
        try:
            clip_duration = int(clip_duration)
        except (TypeError, ValueError):
            clip_duration = 30

    quality    = data.get("quality") or "best"
    browser    = (data.get("browser")           or "").strip().lower() or None
    session_id = (data.get("cookie_session_id") or "").strip() or None
    cookiefile = _resolve_cookiefile(session_id) if session_id else None

    start_time = data.get("start_time")
    end_time = data.get("end_time")
    if start_time is not None:
        try:
            start_time = float(start_time)
        except (TypeError, ValueError):
            start_time = None
    if end_time is not None:
        try:
            end_time = float(end_time)
        except (TypeError, ValueError):
            end_time = None

    job_id = jobs.create(url, clip_duration, quality, start_time, end_time, browser, cookiefile)
    t = threading.Thread(target=clipper.download_and_clip, args=[job_id], daemon=True)
    t.start()
    return jsonify({"job_id": job_id}), 201


@app.get("/api/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(job)


@app.get("/api/download/<job_id>/<clip_name>")
def download(job_id, clip_name):
    if _unsafe_path(clip_name):
        return jsonify({"error": "Invalid filename."}), 400
    path = os.path.join(jobs.CLIPS_DIR, job_id, clip_name)
    if not os.path.exists(path):
        return jsonify({"error": "File not found."}), 404
    return send_file(path, as_attachment=True, download_name=clip_name)


@app.get("/api/probe/<job_id>/<clip_name>")
def probe(job_id, clip_name):
    """Debug: returns ffprobe stream info for a clip."""
    if _unsafe_path(clip_name):
        return jsonify({"error": "Invalid filename."}), 400
    path = os.path.join(jobs.CLIPS_DIR, job_id, clip_name)
    if not os.path.exists(path):
        path = os.path.join(jobs.PREVIEW_DIR, job_id, clip_name)
    if not os.path.exists(path):
        return jsonify({"error": "File not found."}), 404
    import shutil, subprocess as _sp
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return jsonify({"error": "ffprobe not found"}), 500
    r = _sp.run(
        [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", path],
        capture_output=True, timeout=15
    )
    import json as _json
    try:
        info = _json.loads(r.stdout)
        streams = [{"index": s["index"], "codec_type": s.get("codec_type"), "codec_name": s.get("codec_name")} for s in info.get("streams", [])]
    except Exception:
        streams = []
    return jsonify({"path": path, "streams": streams})


@app.get("/api/preview/<job_id>/<clip_name>")
def preview(job_id, clip_name):
    if _unsafe_path(clip_name):
        return jsonify({"error": "Invalid filename."}), 400
    path = os.path.join(jobs.CLIPS_DIR, job_id, clip_name)
    if not os.path.exists(path):
        path = os.path.join(jobs.PREVIEW_DIR, job_id, clip_name)
    if not os.path.exists(path):
        return jsonify({"error": "File not found."}), 404
    return send_file(path, mimetype="video/mp4", conditional=True, max_age=0)


_bsearches: dict = {}   # search_id -> {status, results, error}


@app.post("/api/browser-search")
def browser_search_start():
    data     = request.get_json(force=True, silent=True) or {}
    source   = (data.get("source")   or "").strip().lower()
    query    = (data.get("query")    or "").strip()
    username = (data.get("username") or "").strip() or None
    password = (data.get("password") or "").strip() or None

    if not source or not query:
        return jsonify({"error": "source and query required."}), 400

    search_id = str(_uuid.uuid4())
    _bsearches[search_id] = {"status": "searching", "results": [], "error": None, "cookie_session_id": None}

    def _run():
        cookie_path = os.path.join(COOKIES_DIR, f"bs_{search_id}.txt")
        try:
            results, saved = bs.search(source, query, username, password, cookie_out_path=cookie_path)
            session_id = None
            if saved and os.path.exists(saved):
                session_id = str(_uuid.uuid4())
                _cookie_sessions[session_id] = saved
                # auto-cleanup after 1 hour
                def _cleanup(sid=session_id, p=saved):
                    _cookie_sessions.pop(sid, None)
                    try: os.remove(p)
                    except OSError: pass
                import threading as _t
                t = _t.Timer(3600, _cleanup)
                t.daemon = True
                t.start()
            _bsearches[search_id].update(status="ready", results=results, cookie_session_id=session_id)
        except Exception as e:
            _bsearches[search_id].update(status="failed", error=str(e))

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"search_id": search_id}), 201


@app.get("/api/browser-search/<search_id>")
def browser_search_status(search_id):
    s = _bsearches.get(search_id)
    if not s:
        return jsonify({"error": "Not found."}), 404
    return jsonify({**s, "search_id": search_id})


@app.post("/api/crawl")
def crawl_start():
    """Start a Playwright crawl on any URL to find video files.
    With `query`, treats `url` as a site name/URL and runs the query through
    the site's own search first (site_search mode)."""
    data  = request.get_json(force=True, silent=True) or {}
    url   = (data.get("url")   or "").strip()
    query = (data.get("query") or "").strip()
    if not url:
        return jsonify({"error": "url required."}), 400

    search_id = str(_uuid.uuid4())
    _bsearches[search_id] = {"status": "searching", "results": [], "error": None, "cookie_session_id": None}

    def _run():
        try:
            if query:
                results, _ = bs.search("site_search", query, site=url, cookie_out_path=None)
            else:
                results, _ = bs.search("crawl", url, cookie_out_path=None)
            _bsearches[search_id].update(status="ready", results=results, cookie_session_id=None)
        except Exception as e:
            _bsearches[search_id].update(status="failed", error=str(e))

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"search_id": search_id}), 201


@app.get("/api/jobs")
def list_jobs():
    return jsonify({"jobs": jobs.all_jobs()})


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id):
    jobs.delete(job_id)
    return jsonify({"ok": True})


@app.post("/api/convert")
def convert():
    if not FFMPEG_OK:
        return jsonify({"error": "FFmpeg not installed."}), 500

    data = request.get_json(force=True, silent=True) or {}
    job_id     = (data.get("job_id")      or "").strip()
    clip_name  = (data.get("clip_name")   or "").strip()
    aspect     = (data.get("aspect_ratio") or "").strip()

    preview_only = bool(data.get("preview_only", True))   # default: temp

    if not job_id or not clip_name or not aspect:
        return jsonify({"error": "job_id, clip_name, aspect_ratio required."}), 400
    if _unsafe_path(clip_name):
        return jsonify({"error": "Invalid filename."}), 400
    if aspect not in clipper.ASPECT_RATIOS:
        return jsonify({"error": f"Unknown ratio. Valid: {list(clipper.ASPECT_RATIOS)}"}), 400

    conv_id = str(_uuid.uuid4())
    _conversions[conv_id] = {"status": "processing", "output": None, "job_id": job_id, "error": None}

    def _run():
        try:
            out_name = clipper.convert_clip(job_id, clip_name, aspect, preview_only=preview_only)
            _conversions[conv_id].update(status="ready", output=out_name)
            if preview_only:
                _path = os.path.join(jobs.PREVIEW_DIR, job_id, out_name)
                def _del(p=_path):
                    try: os.remove(p)
                    except OSError: pass
                t = threading.Timer(3600, _del)
                t.daemon = True
                t.start()
        except Exception as e:
            _conversions[conv_id].update(status="failed", error=str(e)[-300:])

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"conv_id": conv_id}), 201


@app.get("/api/convert/<conv_id>")
def convert_status(conv_id):
    c = _conversions.get(conv_id)
    if not c:
        return jsonify({"error": "Not found."}), 404
    return jsonify({**c, "conv_id": conv_id})


@app.post("/api/render")
def render_start():
    data   = request.get_json(force=True, silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"error": "job_id required."}), 400
    try:
        start = float(data["start"]) if data.get("start") is not None else 0.0
        end   = float(data["end"])   if data.get("end")   is not None else None
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid start/end times."}), 400

    preview_only = bool(data.get("preview_only"))
    render_id  = str(_uuid.uuid4())
    cancel_evt = threading.Event()
    _renders[render_id]        = {"status": "processing", "output": None, "job_id": job_id,
                                   "error": None, "preview_only": preview_only}
    _render_cancels[render_id] = cancel_evt

    def _run():
        try:
            out_name = clipper.render_segment(job_id, start, end,
                                              cancel_event=cancel_evt, preview_only=preview_only)
            if cancel_evt.is_set():
                _renders[render_id].update(status="cancelled", error="Cancelled by user")
            else:
                _renders[render_id].update(status="ready", output=out_name)
                if preview_only:
                    # Auto-delete preview file after 5 minutes
                    import os as _os
                    _path = _os.path.join(jobs.PREVIEW_DIR, job_id, out_name)
                    def _del(p=_path):
                        try: _os.remove(p)
                        except OSError: pass
                    t = threading.Timer(3600, _del)
                    t.daemon = True
                    t.start()
        except Exception as e:
            msg = str(e)[-400:]
            status = "cancelled" if "Cancelled" in msg else "failed"
            _renders[render_id].update(status=status, error=msg)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"render_id": render_id}), 201


@app.get("/api/render/<render_id>")
def render_status(render_id):
    r = _renders.get(render_id)
    if not r:
        return jsonify({"error": "Not found."}), 404
    return jsonify({**r, "render_id": render_id})


@app.delete("/api/render/<render_id>")
def render_cancel(render_id):
    evt = _render_cancels.get(render_id)
    if not evt:
        return jsonify({"error": "Not found."}), 404
    evt.set()
    _renders.get(render_id, {}).update(status="cancelled", error="Cancelled by user")
    return jsonify({"ok": True})


@app.post("/api/render-full")
def render_full_start():
    data   = request.get_json(force=True, silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    if not job_id or not jobs.get(job_id):
        return jsonify({"error": "job_id required or not found."}), 400

    render_id  = str(_uuid.uuid4())
    cancel_evt = threading.Event()
    _renders[render_id]        = {"status": "processing", "output": None,
                                   "job_id": job_id, "error": None, "preview_only": False}
    _render_cancels[render_id] = cancel_evt

    def _run():
        try:
            out_name = clipper.render_full_video(job_id, cancel_event=cancel_evt)
            if cancel_evt.is_set():
                _renders[render_id].update(status="cancelled", error="Cancelled by user")
            else:
                _renders[render_id].update(status="ready", output=out_name)
        except Exception as e:
            msg = str(e)[-400:]
            status = "cancelled" if "Cancelled" in msg else "failed"
            _renders[render_id].update(status=status, error=msg)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"render_id": render_id}), 201


@app.get("/api/download-zip/<job_id>")
def download_zip(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    clips      = job.get("clips") or []
    clip_dir   = os.path.join(jobs.CLIPS_DIR, job_id)
    clip_paths = [(c, os.path.join(clip_dir, c)) for c in clips if os.path.isfile(os.path.join(clip_dir, c))]
    if not clip_paths:
        return jsonify({"error": "No rendered clips to zip."}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, path in clip_paths:
            zf.write(path, name)
    buf.seek(0)

    raw_title  = re.sub(r"[^\w\s-]", "", job.get("title") or job_id)[:50].strip()
    safe_title = re.sub(r"\s+", "_", raw_title) or job_id
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{safe_title}_clips.zip")


def _unsafe_path(name: str) -> bool:
    return ".." in name or "/" in name or "\\" in name


if __name__ == "__main__":
    if not FFMPEG_OK:
        print("[WARNING] FFmpeg not found in PATH. Clipping will be unavailable.")
    print(f"[INFO] Serving frontend from: {FRONTEND_DIR}")
    app.run(debug=True, port=5000, use_reloader=False)
