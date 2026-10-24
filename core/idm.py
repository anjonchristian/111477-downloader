"""Internet Download Manager integration (command line + connection check)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - Windows only app
    winreg = None  # type: ignore

_CANDIDATES = [
    r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe",
    r"C:\Program Files\Internet Download Manager\IDMan.exe",
]

_REG_PATH = r"Software\DownloadManager"


def find_idm() -> str:
    for candidate in _CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    if winreg is not None:
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\IDMan.exe",
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\IDMan.exe",
            ):
                try:
                    with winreg.OpenKey(root, sub) as key:
                        value, _ = winreg.QueryValueEx(key, "")
                        if value and os.path.isfile(value):
                            return value
                except OSError:
                    continue
    return ""


class IDM:
    def __init__(self, path: str = ""):
        self.path = path or find_idm()

    # ------------------------------------------------------------------ checks
    @property
    def available(self) -> bool:
        return bool(self.path) and os.path.isfile(self.path)

    def connections(self) -> int | None:
        if winreg is None:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH) as key:
                value, _ = winreg.QueryValueEx(key, "MaxConnectionsNumber")
                return int(value)
        except (OSError, ValueError):
            return None

    def set_connections(self, number: int) -> None:
        if winreg is None:
            raise RuntimeError("Registry access is unavailable.")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "MaxConnectionsNumber", 0, winreg.REG_DWORD, int(number))

    def is_running(self) -> bool:
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq IDMan.exe", "/NH"],
                capture_output=True, text=True, timeout=10,
                creationflags=flags,
            ).stdout.lower()
            return "idman.exe" in out
        except (OSError, subprocess.SubprocessError):
            return False

    # ----------------------------------------------------------------- actions
    def _spawn(self, args: list[str]) -> None:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(
            args,
            close_fds=True,
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def send(self, url: str, folder: str, filename: str, queue_only: bool = True) -> None:
        if not self.available:
            raise FileNotFoundError(
                "IDM was not found. Set the IDMan.exe path in Settings."
            )
        Path(folder).mkdir(parents=True, exist_ok=True)
        args = [self.path, "/n", "/d", url, "/p", folder, "/f", filename]
        if queue_only:
            args.append("/a")
        self._spawn(args)

    def start_queue(self) -> None:
        if not self.available:
            raise FileNotFoundError("IDM was not found.")
        self._spawn([self.path, "/s"])

    def open(self) -> None:
        if not self.available:
            raise FileNotFoundError("IDM was not found.")
        self._spawn([self.path])
