"""Application paths and persisted settings."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "111477 Downloader"
BASE_URL = "https://a.111477.xyz"
PROXY_URL = "https://p.111477.xyz"
DEFAULT_SECTIONS = ("asiandrama", "kdrama", "misc", "movies", "tvs")
DEFAULT_MEDIA_EXTS = [
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".flv", ".webm", ".ts",
    ".m2ts", ".mpg", ".mpeg", ".srt", ".ass", ".ssa", ".sub", ".idx", ".nfo",
]


def app_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = Path(root) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


DATA_DIR = app_dir()
SETTINGS_PATH = DATA_DIR / "settings.json"
CACHE_DB = DATA_DIR / "cache.db"
BROWSER_DIR = DATA_DIR / "browser_profile"
SESSION_PATH = DATA_DIR / "session.json"
LOG_PATH = DATA_DIR / "app.log"


def default_download_dir() -> Path:
    downloads = Path(os.path.expanduser("~")) / "Downloads"
    return downloads / "111477"


@dataclass
class Settings:
    download_dir: str = field(default_factory=lambda: str(default_download_dir()))
    idm_path: str = ""
    queue_only: bool = True
    preserve_folders: bool = True
    skip_existing: bool = True
    only_media: bool = False
    media_exts: list[str] = field(default_factory=lambda: list(DEFAULT_MEDIA_EXTS))
    idm_prev_connections: int | None = None
    max_crawl_dirs: int = 400
    index_ttl_minutes: int = 15
    last_path: str = "/"

    @classmethod
    def load(cls) -> "Settings":
        data: dict = {}
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        known = {f for f in cls.__dataclass_fields__}
        obj = cls(**{k: v for k, v in data.items() if k in known})
        if not obj.download_dir:
            obj.download_dir = str(default_download_dir())
        return obj

    def save(self) -> None:
        SETTINGS_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        stale = SETTINGS_PATH.with_suffix(".tmp")
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass
