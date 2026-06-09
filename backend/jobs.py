import os
import uuid
import time
import shutil
import threading
from typing import Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIPS_DIR = os.path.join(BASE_DIR, "tmp", "clips")
RAW_DIR = os.path.join(BASE_DIR, "tmp", "raw")

_store: dict = {}
_lock = threading.Lock()

CLEANUP_AFTER = 3600  # seconds


def create(
    url: str,
    clip_duration: Optional[int],
    quality: str,
    start_time: Optional[float],
    end_time: Optional[float],
    browser: Optional[str] = None,
    cookiefile: Optional[str] = None,
) -> str:
    job_id = str(uuid.uuid4())
    with _lock:
        _store[job_id] = {
            "id": job_id,
            "url": url,
            "clip_duration": clip_duration,
            "quality": quality,
            "start_time": start_time,
            "end_time": end_time,
            "browser": browser,
            "cookiefile": cookiefile,
            "status": "queued",
            "progress": 0,
            "message": "Queued",
            "clips": [],
            "title": "",
            "thumbnail": "",
            "duration": 0,
            "created_at": time.time(),
            "error": None,
        }
    t = threading.Timer(CLEANUP_AFTER, _cleanup, args=[job_id])
    t.daemon = True
    t.start()
    return job_id


def get(job_id: str) -> Optional[dict]:
    with _lock:
        job = _store.get(job_id)
        return dict(job) if job else None


def update(job_id: str, **kwargs) -> None:
    with _lock:
        if job_id in _store:
            _store[job_id].update(kwargs)


def all_jobs() -> list:
    with _lock:
        return [dict(j) for j in _store.values()]


def delete(job_id: str) -> None:
    with _lock:
        _store.pop(job_id, None)
    clip_dir = os.path.join(CLIPS_DIR, job_id)
    shutil.rmtree(clip_dir, ignore_errors=True)


def _cleanup(job_id: str) -> None:
    delete(job_id)
