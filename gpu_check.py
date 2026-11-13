"""GPU diagnostic for STT Wizard. Changes nothing; writes gpu_check_report.txt next to this file.

Run it with GPU_CHECK.bat (double-click), or:  .venv\\Scripts\\python gpu_check.py
Then send the contents of gpu_check_report.txt. It contains no audio and no personal data
(only versions, DLL file names and error messages; folder paths are shortened).
"""

from __future__ import annotations

import ctypes
import importlib
import importlib.metadata as md
import os
import platform
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "gpu_check_report.txt"
lines: list[str] = []


def out(text: str = "") -> None:
    print(text)
    lines.append(text)


def short(path) -> str:
    text = str(path)
    for base, name in ((str(ROOT), "<app>"), (str(Path.home()), "<home>")):
        text = text.replace(base, name)
    return text


def section(title: str) -> None:
    out()
    out(f"== {title} ==")


def guarded(title: str, fn) -> None:
    section(title)
    try:
        fn()
    except Exception:  # noqa: BLE001
        out("CHECK CRASHED:\n" + short(traceback.format_exc()))


def system() -> None:
    out(f"python   {sys.version.split()[0]} ({platform.architecture()[0]}) frozen={getattr(sys, 'frozen', False)}")
    out(f"os       {platform.platform()}")
    out(f"cpu      {platform.processor()}")


def nvidia_smi() -> None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20,
        )
        out(result.stdout.strip() or result.stderr.strip() or "(no output)")
        banner = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=20).stdout
        for row in banner.splitlines():
            if "CUDA Version" in row:
                out("driver supports: " + row[row.index("CUDA Version"):].split("|")[0].strip())
    except FileNotFoundError:
        out("nvidia-smi NOT FOUND: no NVIDIA driver on PATH.")


PACKAGES = [
    "onnxruntime", "onnxruntime-gpu", "onnx-asr", "ctranslate2", "faster-whisper", "sherpa-onnx",
    "torch", "transformers", "nvidia-cublas-cu12", "nvidia-cudnn-cu12", "nvidia-cuda-runtime-cu12",
    "nvidia-cufft-cu12", "nvidia-curand-cu12", "huggingface-hub", "numpy",
]


def packages() -> None:
    for name in PACKAGES:
        try:
            out(f"{name:28} {md.version(name)}")
        except md.PackageNotFoundError:
            out(f"{name:28} -")
    out("NOTE: onnxruntime and onnxruntime-gpu both installed = they overwrite each other's files."
        if _installed("onnxruntime") and _installed("onnxruntime-gpu") else "")


def _installed(name: str) -> bool:
    try:
        md.version(name)
        return True
    except md.PackageNotFoundError:
        return False


def nvidia_dirs() -> list[Path]:
    found: list[Path] = []
    try:
        import nvidia  # namespace package of the nvidia-* wheels

        for base in nvidia.__path__:
            found += [p for p in Path(base).glob("*/bin") if p.is_dir()]
    except ImportError:
        pass
    found += [p for p in (ROOT / "nvidia").glob("*/bin") if p.is_dir()]
    return found


def dlls() -> None:
    dirs = nvidia_dirs()
    out("nvidia wheel folders: " + (", ".join(short(d) for d in dirs) if dirs else "none"))
    for directory in dirs:
        for dll in sorted(directory.glob("*.dll")):
            out(f"  {short(directory.parent.name)}/{dll.name:36} {dll.stat().st_size / 1e6:8.0f} MB")
        if os.name == "nt":
            os.add_dll_directory(str(directory))
        os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
    out()
    if os.name != "nt":
        out("(not Windows: DLL load tests skipped)")
        return
    for name in ("nvcuda.dll", "cublas64_12.dll", "cublasLt64_12.dll", "cudart64_12.dll", "cudnn64_9.dll",
                 "cudnn_ops64_9.dll", "cufft64_11.dll", "curand64_10.dll"):
        try:
            ctypes.WinDLL(name)
            out(f"load {name:22} OK")
        except OSError as exc:
            out(f"load {name:22} FAILED ({str(exc)[:70]})")


def ctranslate() -> None:
    import ctranslate2

    out(f"ctranslate2 {ctranslate2.__version__}: CUDA devices = {ctranslate2.get_cuda_device_count()}")
    if ctranslate2.get_cuda_device_count():
        out("cuda compute types: " + ", ".join(sorted(ctranslate2.get_supported_compute_types("cuda"))))


def onnxruntime_info() -> None:
    import onnxruntime as ort

    out(f"onnxruntime {ort.__version__}  device={ort.get_device()}")
    out("available providers: " + ", ".join(ort.get_available_providers()))
    capi = Path(ort.__file__).parent / "capi"
    out(f"files in {short(capi)}:")
    for item in sorted(capi.iterdir()):
        if item.suffix.lower() in (".dll", ".so", ".pyd"):
            out(f"  {item.name:40} {item.stat().st_size / 1e6:8.1f} MB")
    out("CUDA provider DLL present: " + str((capi / "onnxruntime_providers_cuda.dll").exists()))


def cohere_session() -> None:
    """Load the real Cohere decoder (if downloaded) on CUDA and say which provider really runs."""
    import onnxruntime as ort

    candidates = [Path(os.getenv("WHISPER_MODEL_DIR") or ROOT / "models") / "cohere-transcribe"]
    model = next((c / "decoder-model.int8.onnx" for c in candidates if (c / "decoder-model.int8.onnx").exists()), None)
    if model is None:
        out("Cohere model not downloaded yet: session test skipped (run one Cohere transcription first, or ignore).")
        return
    for providers in (["CUDAExecutionProvider", "CPUExecutionProvider"], ["CPUExecutionProvider"]):
        start = time.time()
        try:
            session = ort.InferenceSession(str(model), providers=providers)
            out(f"requested {providers[0]:24} -> active {session.get_providers()}  ({time.time() - start:.1f}s)")
        except Exception as exc:  # noqa: BLE001
            out(f"requested {providers[0]:24} -> FAILED: {short(str(exc))[:300]}")


def torch_info() -> None:
    try:
        torch = importlib.import_module("torch")
    except ImportError:
        out("torch not installed (only needed for a Transformers build).")
        return
    out(f"torch {torch.__version__}  cuda available={torch.cuda.is_available()}  cuda build={torch.version.cuda}")
    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        out(f"gpu {props.name}  vram={props.total_memory / 1e9:.1f} GB  capability={props.major}.{props.minor}")


def main() -> None:
    out(f"STT Wizard GPU check  {time.strftime('%Y-%m-%d %H:%M')}")
    guarded("System", system)
    guarded("NVIDIA driver / GPU", nvidia_smi)
    guarded("Installed packages", packages)
    guarded("GPU libraries (DLLs)", dlls)
    guarded("CTranslate2 (Whisper)", ctranslate)
    guarded("ONNX Runtime (Cohere)", onnxruntime_info)
    guarded("Cohere model on GPU", cohere_session)
    guarded("PyTorch (optional)", torch_info)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nDone. Report saved to: {REPORT}")


if __name__ == "__main__":
    main()
