import shutil


def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def is_url(text: str) -> bool:
    t = text.strip()
    return t.startswith("http://") or t.startswith("https://")


def format_clip_name(index: int, start: float, end: float) -> str:
    return f"clip_{index:02d}_{int(start):03d}s-{int(end):03d}s.mp4"


def hms_to_seconds(hms: str) -> float:
    parts = hms.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def format_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"
