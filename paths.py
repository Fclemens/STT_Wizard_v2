"""Where the app reads its bundled files and writes user data.

* Source checkout: both are this folder.
* Packaged exe (PyInstaller one-folder): resources live in ``_internal``
  (``sys._MEIPASS``); user data lives next to ``STT Wizard.exe`` so the whole
  folder stays portable.
"""

from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

# Read-only files shipped with the app: templates, static, CUDA libraries.
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

# Writable: data/, models/, settings.json, .env, logs/.
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
