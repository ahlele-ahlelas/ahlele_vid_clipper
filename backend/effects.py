"""
Creator effects for rendered clips: auto-captions (faster-whisper),
text/logo overlay, silence trimming, GIF/MP3 export, and face-tracked
aspect-ratio cropping.

All functions operate on an existing clip file (CLIPS_DIR first, then
PREVIEW_DIR) and write their output into CLIPS_DIR/<job_id>/ — effects
outputs are always permanent artifacts the user asked for.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import jobs
from clipper import _ffmpeg_bin, _ffmpeg_run, _probe_duration, ASPECT_RATIOS

# Whisper / HF models must never land on C: — default to a project-local dir
# (D:) unless the user already redirected HF_HOME. Must be set before
# faster_whisper is imported.
os.environ.setdefault("HF_HOME", os.path.join(jobs.BASE_DIR, "models"))

_whisper_models: dict = {}   # model_size -> WhisperModel (loaded once, reused)


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _find_clip(job_id: str, clip_name: str) -> str:
    for d in (jobs.CLIPS_DIR, jobs.PREVIEW_DIR):
        path = os.path.join(d, job_id, clip_name)
        if os.path.exists(path):
            return path
    raise FileNotFoundError(f"Source clip not found: {clip_name}")


def _out_dir(job_id: str) -> str:
    d = os.path.join(jobs.CLIPS_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    return d


def _ffprobe_bin() -> str:
    return shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"


def _probe_streams(path: str) -> dict:
    """Return {'width', 'height', 'has_audio', 'duration'} for a media file."""
    r = subprocess.run(
        [_ffprobe_bin(), "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", path],
        capture_output=True, timeout=30,
    )
    try:
        info = json.loads(r.stdout or b"{}")
    except Exception:
        info = {}
    width = height = 0
    has_audio = False
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and not width:
            width, height = int(s.get("width") or 0), int(s.get("height") or 0)
        elif s.get("codec_type") == "audio":
            has_audio = True
    try:
        duration = float((info.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {"width": width, "height": height, "has_audio": has_audio, "duration": duration}


def _run_ffmpeg(cmd: list, cwd: str | None = None, timeout: int = 600) -> None:
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg error: {proc.stderr.decode(errors='replace')[-400:]}")


_ENCODE_ARGS = ["-c:v", "libx264", "-crf", "23", "-preset", "fast",
                "-c:a", "aac", "-b:a", "128k"]


def _find_font(bold: bool = True) -> str:
    candidates = (
        [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf"]
        if bold else []
    ) + [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


def _filter_escape_path(path: str) -> str:
    """Escape a path for use inside an FFmpeg filter argument."""
    return path.replace("\\", "/").replace(":", r"\:")


# ── 1. Auto-captions (faster-whisper → SRT → burn-in) ─────────────────────────

def _get_whisper(model_size: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError("faster-whisper not installed. Run: pip install faster-whisper")
    if model_size not in _whisper_models:
        _whisper_models[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _whisper_models[model_size]


def _srt_ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap_caption(text: str, width: int = 42) -> str:
    """Wrap caption text to ≤2 lines for readability on vertical video."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines[:2]) if len(lines) <= 2 else "\n".join(lines)


def generate_captions(job_id: str, clip_name: str, burn: bool = True,
                      model_size: str = "", language: str = "") -> tuple[str, str]:
    """Transcribe a clip and (optionally) burn subtitles in.
    language: ISO code ('hi', 'en', …) or '' for auto-detect. Captions stay in
    the spoken language — never translated.
    Returns (output_video_name_or_'' , srt_name)."""
    src = _find_clip(job_id, clip_name)
    if not _probe_streams(src)["has_audio"]:
        raise ValueError("Clip has no audio track — nothing to transcribe.")

    language = (language or "").strip().lower() or None
    # base is fine for English but garbles Hindi/other languages —
    # auto-upgrade to small unless the caller pinned a model.
    if model_size not in ("tiny", "base", "small", "medium", "large-v3"):
        model_size = "base" if language == "en" else "small"

    model = _get_whisper(model_size)
    segments, _info = model.transcribe(
        src, vad_filter=True, beam_size=5,
        language=language, task="transcribe",
        # Prevents the repetition-loop hallucinations small models fall into
        condition_on_previous_text=False,
    )

    base = clip_name.rsplit(".", 1)[0]
    out_dir = _out_dir(job_id)
    srt_name = f"{base}.srt"
    srt_path = os.path.join(out_dir, srt_name)

    entries = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            entries.append((seg.start, seg.end, _wrap_caption(text)))
    if not entries:
        raise ValueError("No speech detected in this clip.")

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (s, e, text) in enumerate(entries, 1):
            f.write(f"{i}\n{_srt_ts(s)} --> {_srt_ts(e)}\n{text}\n\n")

    if not burn:
        return "", srt_name

    out_name = f"{base}_cap.mp4"
    out_path = os.path.join(out_dir, out_name)
    # Arial lacks Devanagari/Arabic glyphs — switch to a Windows font that has
    # them when the transcript contains those scripts (Nirmala UI = Indic,
    # Segoe UI = broad Unicode incl. Arabic/Urdu).
    all_text = " ".join(t for _, _, t in entries)
    if any("ऀ" <= c <= "ॿ" for c in all_text):      # Devanagari
        font_name = "Nirmala UI"
    elif any("؀" <= c <= "ۿ" for c in all_text):    # Arabic/Urdu
        font_name = "Segoe UI"
    else:
        font_name = "Arial"
    style = (f"FontName={font_name},FontSize=16,Bold=1,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,Outline=2,Shadow=0,MarginV=36")
    # cwd=out_dir so the subtitles filter sees a bare relative filename —
    # dodges Windows drive-colon escaping entirely.
    vf = f"subtitles={srt_name}:force_style='{style}'"
    _run_ffmpeg(
        [_ffmpeg_bin(), "-y", "-i", src, "-vf", vf] + _ENCODE_ARGS + [out_path],
        cwd=out_dir,
    )
    return out_name, srt_name


# ── 2. Text / logo overlay ─────────────────────────────────────────────────────

_TEXT_POS = {
    "top":    "x=(w-text_w)/2:y=h*0.06",
    "center": "x=(w-text_w)/2:y=(h-text_h)/2",
    "bottom": "x=(w-text_w)/2:y=h-text_h-h*0.06",
}
_LOGO_POS = {
    "top_left":     "24:24",
    "top_right":    "W-w-24:24",
    "bottom_left":  "24:H-h-24",
    "bottom_right": "W-w-24:H-h-24",
}


def overlay_clip(job_id: str, clip_name: str, text: str = "",
                 text_pos: str = "bottom", logo_path: str = "",
                 logo_pos: str = "top_right") -> str:
    """Burn a title text and/or a logo image onto a clip."""
    text = (text or "").strip()
    if not text and not logo_path:
        raise ValueError("Provide text and/or a logo image.")
    if logo_path and not os.path.isfile(logo_path):
        raise ValueError("Logo file not found (upload may have expired).")

    src = _find_clip(job_id, clip_name)
    meta = _probe_streams(src)
    h = meta["height"] or 720

    base = clip_name.rsplit(".", 1)[0]
    out_dir = _out_dir(job_id)
    out_name = f"{base}_brand.mp4"
    out_path = os.path.join(out_dir, out_name)

    draw = ""
    txt_file = ""
    if text:
        # textfile= avoids drawtext escaping bugs with quotes/colons in user text
        txt_file = os.path.join(out_dir, f"_overlay_{base}.txt")
        with open(txt_file, "w", encoding="utf-8") as f:
            f.write(text)
        fontsize = max(18, int(h * 0.045))
        pos = _TEXT_POS.get(text_pos, _TEXT_POS["bottom"])
        font = _find_font()
        font_part = f"fontfile='{_filter_escape_path(font)}':" if font else ""
        draw = (f"drawtext={font_part}textfile='{os.path.basename(txt_file)}':"
                f"fontsize={fontsize}:fontcolor=white:{pos}:"
                f"box=1:boxcolor=black@0.45:boxborderw=12")

    try:
        if logo_path:
            logo_h = max(32, int(h * 0.12))
            pre = f"[0:v]{draw}[v0];" if draw else ""
            v_in = "[v0]" if draw else "[0:v]"
            fc = (f"{pre}[1:v]scale=-1:{logo_h}[lg];"
                  f"{v_in}[lg]overlay={_LOGO_POS.get(logo_pos, _LOGO_POS['top_right'])}[vout]")
            cmd = [_ffmpeg_bin(), "-y", "-i", src, "-i", logo_path,
                   "-filter_complex", fc, "-map", "[vout]", "-map", "0:a?"]
        else:
            cmd = [_ffmpeg_bin(), "-y", "-i", src, "-vf", draw, "-map", "0:v", "-map", "0:a?"]
        _run_ffmpeg(cmd + _ENCODE_ARGS + [out_path], cwd=out_dir)
    finally:
        if txt_file:
            try:
                os.remove(txt_file)
            except OSError:
                pass
    return out_name


# ── 3. Trim silence ────────────────────────────────────────────────────────────

_SIL_START_RE = re.compile(r"silence_start:\s*([\d.]+)")
_SIL_END_RE   = re.compile(r"silence_end:\s*([\d.]+)")


def trim_silence(job_id: str, clip_name: str, noise_db: float = -35.0,
                 min_silence: float = 0.6, pad: float = 0.15) -> str:
    """Cut silent gaps out of a clip. Returns output filename."""
    src = _find_clip(job_id, clip_name)
    meta = _probe_streams(src)
    if not meta["has_audio"]:
        raise ValueError("Clip has no audio track — cannot detect silence.")
    duration = meta["duration"] or _probe_duration(src)
    if not duration:
        raise ValueError("Could not determine clip duration.")

    det = subprocess.run(
        [_ffmpeg_bin(), "-i", src,
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, timeout=300,
    )
    log = det.stderr.decode(errors="replace")
    starts = [float(m) for m in _SIL_START_RE.findall(log)]
    ends   = [float(m) for m in _SIL_END_RE.findall(log)]
    if not starts:
        raise ValueError("No silence found — clip is already tight.")

    # Build keep-intervals between silences, padded so speech isn't clipped
    silences = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else duration
        silences.append((max(0.0, s + pad), min(duration, e - pad)))
    silences = [(s, e) for s, e in silences if e - s > 0.05]

    keeps = []
    cursor = 0.0
    for s, e in silences:
        if s > cursor + 0.05:
            keeps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration - 0.05:
        keeps.append((cursor, duration))
    if not keeps:
        raise ValueError("Clip is entirely silent.")
    if len(keeps) == 1 and keeps[0][0] < 0.05 and keeps[0][1] > duration - 0.05:
        raise ValueError("No silence found — clip is already tight.")

    # Merge smallest gaps if the segment count would blow up the filter graph
    while len(keeps) > 80:
        gaps = [(keeps[i + 1][0] - keeps[i][1], i) for i in range(len(keeps) - 1)]
        _, i = min(gaps)
        keeps[i] = (keeps[i][0], keeps[i + 1][1])
        del keeps[i + 1]

    parts, labels = [], ""
    for i, (s, e) in enumerate(keeps):
        parts.append(
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
        labels += f"[v{i}][a{i}]"
    fc = ";".join(parts) + f";{labels}concat=n={len(keeps)}:v=1:a=1[v][a]"

    base = clip_name.rsplit(".", 1)[0]
    out_name = f"{base}_tight.mp4"
    out_path = os.path.join(_out_dir(job_id), out_name)
    _run_ffmpeg(
        [_ffmpeg_bin(), "-y", "-i", src, "-filter_complex", fc,
         "-map", "[v]", "-map", "[a]"] + _ENCODE_ARGS + [out_path],
    )
    return out_name


# ── 4. GIF / MP3 export ────────────────────────────────────────────────────────

def export_gif(job_id: str, clip_name: str, fps: int = 12, width: int = 480) -> str:
    src = _find_clip(job_id, clip_name)
    duration = _probe_streams(src)["duration"]
    if duration > 60:
        raise ValueError("GIF export is capped at 60s clips — render a shorter segment first.")

    fc = (f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];"
          f"[a]palettegen=stats_mode=diff[p];"
          f"[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
    base = clip_name.rsplit(".", 1)[0]
    out_name = f"{base}.gif"
    out_path = os.path.join(_out_dir(job_id), out_name)
    _run_ffmpeg(
        [_ffmpeg_bin(), "-y", "-i", src, "-filter_complex", fc,
         "-loop", "0", out_path],
    )
    return out_name


def export_mp3(job_id: str, clip_name: str, bitrate: str = "192k") -> str:
    src = _find_clip(job_id, clip_name)
    if not _probe_streams(src)["has_audio"]:
        raise ValueError("Clip has no audio track.")
    base = clip_name.rsplit(".", 1)[0]
    out_name = f"{base}.mp3"
    out_path = os.path.join(_out_dir(job_id), out_name)
    _run_ffmpeg(
        [_ffmpeg_bin(), "-y", "-i", src, "-vn",
         "-c:a", "libmp3lame", "-b:a", bitrate, out_path],
    )
    return out_name


# ── 5a. Face scan (detect + identify people in a clip) ────────────────────────

_face_scans: dict = {}   # (job_id, clip_name) -> {"samples": [(t, {fid: (cx, cy)})]}

_FACE_MODELS = {
    "yunet": ("face_detection_yunet_2023mar.onnx",
              "https://github.com/opencv/opencv_zoo/raw/main/models/"
              "face_detection_yunet/face_detection_yunet_2023mar.onnx"),
    "sface": ("face_recognition_sface_2021dec.onnx",
              "https://github.com/opencv/opencv_zoo/raw/main/models/"
              "face_recognition_sface/face_recognition_sface_2021dec.onnx"),
}


def _face_model_path(key: str) -> str:
    """Download the ONNX model once into the project models dir (D:)."""
    name, url = _FACE_MODELS[key]
    mdir = os.path.join(jobs.BASE_DIR, "models", "opencv")
    os.makedirs(mdir, exist_ok=True)
    path = os.path.join(mdir, name)
    if not os.path.exists(path):
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
            shutil.copyfileobj(r, f)
    return path


def scan_faces(job_id: str, clip_name: str, max_samples: int = 240) -> list:
    """Detect and cluster the people appearing in a clip.
    Returns [{face_id, thumb (data URL), appearances, coverage}] sorted by
    screen time. Per-identity positions are cached for smart_convert."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise RuntimeError("OpenCV not installed. Run: pip install opencv-python-headless")
    import base64

    src = _find_clip(job_id, clip_name)
    det = cv2.FaceDetectorYN.create(_face_model_path("yunet"), "", (320, 320), 0.6, 0.3, 5000)
    rec = cv2.FaceRecognizerSF.create(_face_model_path("sface"), "")

    cap = cv2.VideoCapture(src)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, int(fps / 2))                    # ~2 samples per second
    if n_frames and n_frames // step > max_samples:
        step = n_frames // max_samples

    # cluster: {"feat_sum": vec, "n": int, "count": int,
    #           "best_score": float, "best_crop": img, "pos": {sample_i: (cx, cy)}}
    clusters: list = []
    samples_n = 0
    times: list = []
    idx = 0
    while True:
        if not cap.grab():
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            t = idx / fps
            times.append(t)
            fh, fw = frame.shape[:2]
            det.setInputSize((fw, fh))
            _, faces = det.detect(frame)
            for f in (faces if faces is not None else []):
                x, y, w, h, score = f[0], f[1], f[2], f[3], f[14]
                try:
                    feat = rec.feature(rec.alignCrop(frame, f)).flatten()
                except cv2.error:
                    continue
                feat = feat / (np.linalg.norm(feat) + 1e-9)
                best_i, best_sim = -1, 0.0
                for i, c in enumerate(clusters):
                    centroid = c["feat_sum"] / c["n"]
                    sim = float(np.dot(feat, centroid / (np.linalg.norm(centroid) + 1e-9)))
                    if sim > best_sim:
                        best_i, best_sim = i, sim
                # 0.4 cosine ≈ SFace same-person threshold (0.363) + margin
                if best_i >= 0 and best_sim > 0.4:
                    c = clusters[best_i]
                    c["feat_sum"] += feat
                    c["n"] += 1
                else:
                    x0, y0 = max(0, int(x)), max(0, int(y))
                    crop = frame[y0:y0 + max(1, int(h)), x0:x0 + max(1, int(w))].copy()
                    c = {"feat_sum": feat.copy(), "n": 1, "count": 0,
                         "best_score": -1.0, "best_crop": crop, "pos": {}}
                    clusters.append(c)
                if samples_n not in c["pos"] or score > c.get("_pos_score", 0):
                    c["pos"][samples_n] = (float(x + w / 2), float(y + h / 2))
                    c["_pos_score"] = float(score)
                c["count"] += 1
                if score > c["best_score"]:
                    x0, y0 = max(0, int(x)), max(0, int(y))
                    c["best_score"] = float(score)
                    c["best_crop"] = frame[y0:y0 + max(1, int(h)), x0:x0 + max(1, int(w))].copy()
            samples_n += 1
        idx += 1
    cap.release()

    if not samples_n:
        raise ValueError("Could not read any frames from this clip.")

    # Drop one-off spurious detections, keep the 8 most-seen people
    keep = [c for c in clusters if c["count"] >= max(3, samples_n // 20)]
    keep.sort(key=lambda c: -c["count"])
    keep = keep[:8]
    if not keep:
        raise ValueError("No faces found in this clip.")

    results = []
    cached = []
    for fid, c in enumerate(keep):
        crop = c["best_crop"]
        if crop.size:
            scale = 96 / max(1, crop.shape[0])
            thumb_img = cv2.resize(crop, (max(1, int(crop.shape[1] * scale)), 96))
            ok, buf = cv2.imencode(".jpg", thumb_img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            thumb = "data:image/jpeg;base64," + base64.b64encode(buf).decode() if ok else ""
        else:
            thumb = ""
        results.append({
            "face_id": fid,
            "thumb": thumb,
            "appearances": c["count"],
            "coverage": round(100 * len(c["pos"]) / samples_n),
        })
        cached.append(c["pos"])

    _face_scans[(job_id, clip_name)] = {"times": times, "positions": cached}
    return results


# ── 5b. Subject-tracking crop ──────────────────────────────────────────────────

def smart_convert(job_id: str, clip_name: str, aspect_ratio: str,
                  face_ids: list | None = None) -> str:
    """Aspect-ratio conversion where the crop window follows detected faces
    instead of center-cropping. face_ids (from scan_faces) restricts tracking
    to the selected people — the crop follows their mean position. Without
    face_ids, tracks the largest face per frame. Falls back to center crop
    when no faces are found."""
    try:
        import cv2
    except ImportError:
        raise RuntimeError("OpenCV not installed. Run: pip install opencv-python-headless")

    if aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"Unknown aspect ratio: {aspect_ratio}")
    _, _, out_w, out_h = ASPECT_RATIOS[aspect_ratio]

    src = _find_clip(job_id, clip_name)
    meta = _probe_streams(src)
    sw, sh = meta["width"], meta["height"]
    if not sw or not sh:
        raise ValueError("Could not read video dimensions.")

    # Crop window in source pixels matching the target AR
    tgt_ar = out_w / out_h
    if sw / sh > tgt_ar:
        ch, cw = sh, int(sh * tgt_ar) & ~1
    else:
        cw, ch = sw, int(sw / tgt_ar) & ~1
    track_x = cw < sw   # horizontal pan possible
    track_y = ch < sh   # vertical pan possible
    if not track_x and not track_y:
        # Nothing to crop — same AR; plain scale via the normal converter
        from clipper import convert_clip
        return convert_clip(job_id, clip_name, aspect_ratio, preview_only=False)

    # ── Build trajectory samples: (t, cx or None, cy or None) ────────────────
    if face_ids:
        # Selected identities from a prior scan — crop follows their mean position
        scan = _face_scans.get((job_id, clip_name))
        if not scan:
            raise ValueError("Face scan expired — click 👥 Pick faces again.")
        positions = scan["positions"]
        chosen = [positions[i] for i in face_ids if 0 <= int(i) < len(positions)]
        if not chosen:
            raise ValueError("Selected faces not found in scan.")
        samples = []
        for si, t in enumerate(scan["times"]):
            pts = [pos[si] for pos in chosen if si in pos]
            if pts:
                samples.append((t, sum(p[0] for p in pts) / len(pts),
                                   sum(p[1] for p in pts) / len(pts)))
            else:
                samples.append((t, None, None))
    else:
        # No selection — sample frames and track the largest face (Haar)
        cap = cv2.VideoCapture(src)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        step = max(1, int(fps / 3))                    # ~3 detections per second
        max_samples = 400
        if n_frames and n_frames // step > max_samples:
            step = n_frames // max_samples

        front = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")

        samples = []   # (t, cx or None, cy or None)
        idx = 0
        while True:
            ok = cap.grab()
            if not ok:
                break
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    break
                t = idx / fps
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                # Downscale for speed on HD sources
                scale = min(1.0, 480 / max(1, gray.shape[0]))
                small = cv2.resize(gray, None, fx=scale, fy=scale) if scale < 1.0 else gray
                faces = front.detectMultiScale(small, 1.1, 4, minSize=(24, 24))
                if len(faces) == 0:
                    faces = profile.detectMultiScale(small, 1.1, 4, minSize=(24, 24))
                if len(faces):
                    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                    samples.append((t, (x + w / 2) / scale, (y + h / 2) / scale))
                else:
                    samples.append((t, None, None))
            idx += 1
        cap.release()

    detected = [s for s in samples if s[1] is not None]
    if not detected:
        # No faces anywhere → center crop is the honest fallback
        from clipper import convert_clip
        return convert_clip(job_id, clip_name, aspect_ratio, preview_only=False)

    # ── Fill gaps + smooth trajectory ─────────────────────────────────────────
    def _fill_and_smooth(vals: list, default: float) -> list:
        # forward-fill None gaps, then back-fill any leading Nones
        out, last = [], None
        for v in vals:
            out.append(v if v is not None else last)
            last = out[-1] if out[-1] is not None else last
        nxt = default
        for i in range(len(out) - 1, -1, -1):
            if out[i] is None:
                out[i] = nxt
            else:
                nxt = out[i]
        # forward+backward EMA ≈ zero-phase smoothing (kills jitter, keeps drift)
        alpha = 0.25
        for i in range(1, len(out)):
            out[i] = alpha * out[i] + (1 - alpha) * out[i - 1]
        for i in range(len(out) - 2, -1, -1):
            out[i] = alpha * out[i] + (1 - alpha) * out[i + 1]
        return out

    cxs = _fill_and_smooth([s[1] for s in samples], sw / 2)
    cys = _fill_and_smooth([s[2] for s in samples], sh / 2)
    times = [s[0] for s in samples]

    def _clamp_x(cx: float) -> int:
        return int(min(max(cx - cw / 2, 0), sw - cw)) & ~1

    def _clamp_y(cy: float) -> int:
        return int(min(max(cy - ch / 2, 0), sh - ch)) & ~1

    # ── Build sendcmd file: interpolate to 10 Hz for smooth panning ───────────
    out_dir = _out_dir(job_id)
    ratio_tag = aspect_ratio.replace(":", "x")
    base = clip_name.rsplit(".", 1)[0]
    cmd_name = f"_track_{base}_{ratio_tag}.cmd"
    cmd_path = os.path.join(out_dir, cmd_name)

    duration = meta["duration"] or times[-1] or 1.0
    lines = []
    t = 0.0
    ki = 0
    while t <= duration:
        while ki < len(times) - 1 and times[ki + 1] <= t:
            ki += 1
        if ki < len(times) - 1 and times[ki + 1] > times[ki]:
            f = (t - times[ki]) / (times[ki + 1] - times[ki])
            f = min(max(f, 0.0), 1.0)
            cx = cxs[ki] + f * (cxs[ki + 1] - cxs[ki])
            cy = cys[ki] + f * (cys[ki + 1] - cys[ki])
        else:
            cx, cy = cxs[ki], cys[ki]
        if track_x:
            lines.append(f"{t:.2f} crop x {_clamp_x(cx)};")
        if track_y:
            lines.append(f"{t:.2f} crop y {_clamp_y(cy)};")
        t += 0.1
    with open(cmd_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    x0 = _clamp_x(cxs[0]) if track_x else int((sw - cw) / 2) & ~1
    y0 = _clamp_y(cys[0]) if track_y else int((sh - ch) / 2) & ~1

    out_name = f"{base}_{ratio_tag}_track.mp4"
    out_path = os.path.join(out_dir, out_name)
    vf = (f"sendcmd=f='{cmd_name}',"
          f"crop={cw}:{ch}:{x0}:{y0},scale={out_w}:{out_h}")
    try:
        _run_ffmpeg(
            [_ffmpeg_bin(), "-y", "-i", src, "-vf", vf,
             "-map", "0:v", "-map", "0:a?"] + _ENCODE_ARGS + [out_path],
            cwd=out_dir,
        )
    finally:
        try:
            os.remove(cmd_path)
        except OSError:
            pass
    return out_name
