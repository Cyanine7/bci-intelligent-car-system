"""Compatibility for pip Qt 6.11 wheels in an Anaconda-backed Windows venv."""

import os
import sys
from pathlib import Path

_system_icu = None


def prepare_qt_runtime() -> None:
    """Preload the OS ICU required by Qt before Conda's ICU wins DLL lookup.

    Conda adds Library/bin to the process DLL search path. Its versioned ICU
    exports do not match the Windows ICU API used by the Qt 6.11 pip wheel.
    This only affects this process; no PATH, files, or registry are changed.
    """
    global _system_icu
    if sys.platform != "win32" or _system_icu is not None:
        return
    if not (Path(sys.base_prefix) / "conda-meta").is_dir():
        return
    system_icu = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icuuc.dll"
    if system_icu.is_file():
        import ctypes

        _system_icu = ctypes.WinDLL(str(system_icu), winmode=0x00000800)


def prepare_qt_fonts() -> None:
    """The Qt offscreen platform has no OS font database on Windows."""
    if sys.platform != "win32":
        return
    from PySide6.QtGui import QFontDatabase

    if not QFontDatabase.families():
        fonts = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts"
        for name in ("msyh.ttc", "consola.ttf"):
            font = fonts / name
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))
