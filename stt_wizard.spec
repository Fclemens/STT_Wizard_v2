# PyInstaller spec: one-folder build of the desktop app ("STT Wizard.exe").
# Build with BUILD_EXE.bat (or build.ps1), not by hand.
import importlib.util
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH)


def package_dir(name):
    spec = importlib.util.find_spec(name)
    return Path(list(spec.submodule_search_locations)[0])


# Variant (set by build.ps1): "nvidia" bundles cuBLAS for GPU transcription (~740 MB);
# "cpu" leaves it out and the app always runs on the CPU.
VARIANT = os.environ.get("STTW_VARIANT", "nvidia")
if VARIANT not in ("nvidia", "cpu"):
    raise SystemExit(f"Unknown STTW_VARIANT: {VARIANT}")

# GPU: CTranslate2 needs only cuBLAS for Whisper (cuDNN's 1+ GB is not required; tested).
# stt_engine looks for them in <bundle>/nvidia/*/bin.
gpu_binaries = []
if VARIANT == "nvidia":
    cublas_bin = package_dir("nvidia.cublas") / "bin"
    gpu_binaries = [(str(cublas_bin / dll), "nvidia/cublas/bin") for dll in ("cublas64_12.dll", "cublasLt64_12.dll")]

binaries = (
    gpu_binaries
    + collect_dynamic_libs("ctranslate2")   # ctranslate2.dll, cuDNN loader stub, OpenMP
    + collect_dynamic_libs("onnxruntime")   # Silero voice-activity detection
)
datas = (
    [(str(ROOT / "templates"), "templates"), (str(ROOT / "static"), "static")]
    + collect_data_files("faster_whisper")  # assets/silero_vad*.onnx
    + collect_data_files("webview")         # pywebview's injected JS
)
hiddenimports = (
    collect_submodules("uvicorn")
    + ["app", "pyaudiowpatch", "webview.platforms.edgechromium", "webview.platforms.winforms"]
)
# Never bundle these even if they are installed in the build environment.
excludes = ["torch", "whisper", "numba", "llvmlite", "tkinter", "matplotlib", "IPython", "pytest"]
if VARIANT == "cpu":
    excludes.append("nvidia")  # the Python package only locates the DLLs

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="STT Wizard",
    icon=str(ROOT / "assets" / "icon.ico"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="STT Wizard", upx=False)
