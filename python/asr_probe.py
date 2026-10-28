#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""asr_probe —— 看看这台机器的语音转写到底能挂到哪块硬件上。

只做检测，不写任何东西、不改任何配置。输出一行 JSON，给插件和设置页用：
  {"ok": true, "cpu": {...}, "gpu": {...}, "engines": [...], "recommend": "cpu" | "gpu"}

为什么要探测而不是直接开 GPU：faster-whisper 的底层是 CTranslate2，只认 NVIDIA CUDA；
AMD 显卡（比如 RX 6750）在 Windows 上没有官方 ROCm，硬开 cuda 会直接抛异常。
所以这里把异常原文抓下来给人看，再决定退回 CPU 还是换别的引擎（whisper.cpp Vulkan / onnxruntime DirectML）。
"""
import json
import os
import shutil
import sys
from pathlib import Path

HOME = Path(os.environ.get("DSH_BILI_LIVE_HOME") or Path(__file__).parent).expanduser().resolve()

FFMPEG_CANDIDATES = [
    os.environ.get("DSH_BILI_LIVE_FFMPEG") or "",
    str(HOME / "bin" / "ffmpeg.exe"),
    r"D:/LosslessCut-win-x64/resources/ffmpeg.exe",
    r"C:/Users/Admin/AppData/Roaming/bilibili/ffmpeg/ffmpeg.exe",
]


def _ffmpeg() -> str:
    for c in FFMPEG_CANDIDATES:
        if c and Path(c).is_file():
            return c
    return shutil.which("ffmpeg") or ""


def _whisper_cpp() -> str:
    """whisper.cpp 的可执行文件：优先环境变量，其次家目录 bin/，最后 PATH。"""
    cands = [
        os.environ.get("DSH_BILI_LIVE_WHISPERCPP") or "",
        str(HOME / "bin" / "whisper-cli.exe"),
        str(HOME / "bin" / "main.exe"),
        shutil.which("whisper-cli") or "",
        shutil.which("whisper") or "",
    ]
    for c in cands:
        if c and Path(c).is_file():
            return c
    return ""


def probe_cpu() -> dict:
    out = {"engine": "faster-whisper", "device": "cpu", "compute_type": "int8", "ok": False}
    try:
        import ctranslate2  # noqa: F401
        import faster_whisper
        out["ctranslate2"] = getattr(ctranslate2, "__version__", "?")
        out["faster_whisper"] = getattr(faster_whisper, "__version__", "?")
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def probe_cuda() -> dict:
    """真去点一次 CUDA：AMD 卡这里会失败，失败原文要原样留下给人看。"""
    out = {"engine": "faster-whisper", "device": "cuda", "compute_type": "float16", "ok": False}
    try:
        import ctranslate2
    except Exception as exc:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out
    try:
        out["cuda_device_count"] = int(ctranslate2.get_cuda_device_count())
    except Exception as exc:  # noqa: BLE001
        out["cuda_device_count"] = -1
        out["count_error"] = "%s: %s" % (type(exc).__name__, exc)
    if not out.get("cuda_device_count"):
        # 设备数为 0 时也真去建一次模型：CTranslate2 自己的原话（比如 CPU-only 轮子那句）比我们转述的有用
        try:
            from faster_whisper import WhisperModel
            WhisperModel("small", device="cuda", compute_type="float16")
            out["ok"] = True
            out["note"] = "设备数报 0，但模型居然建起来了"
        except Exception as exc:  # noqa: BLE001
            out["raw_error"] = "%s: %s" % (type(exc).__name__, exc)
        out["error"] = out.get("count_error") or out.get("raw_error") or "CTranslate2 报告 0 个 CUDA 设备"
        return out
    try:
        from faster_whisper import WhisperModel
        WhisperModel("small", device="cuda", compute_type="float16")
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def probe_directml() -> dict:
    out = {"engine": "onnxruntime-directml", "ok": False}
    try:
        import onnxruntime as ort
        providers = list(ort.get_available_providers())
        out["providers"] = providers
        out["ok"] = "DmlExecutionProvider" in providers
        if not out["ok"]:
            out["error"] = "装了 onnxruntime 但没有 DmlExecutionProvider（装的是 CPU 包，不是 -directml）"
    except Exception as exc:  # noqa: BLE001
        out["error"] = "没装 onnxruntime-directml（%s）" % type(exc).__name__
    return out


def main() -> int:
    report = {
        "ok": True,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "ffmpeg": _ffmpeg(),
        "whisper_cpp": _whisper_cpp(),
        "cpu": probe_cpu(),
        "gpu": probe_cuda(),
        "directml": probe_directml(),
    }
    report["engines"] = sorted({
        "faster-whisper" if report["cpu"].get("ok") else "",
        "whisper.cpp(vulkan)" if report["whisper_cpp"] else "",
        "onnxruntime-directml" if report["directml"].get("ok") else "",
    } - {""})
    if report["gpu"].get("ok"):
        report["recommend"] = "cuda"
        report["advice"] = "CUDA 可用，可以直接用 GPU 转写。"
    elif report["whisper_cpp"]:
        report["recommend"] = "vulkan"
        report["advice"] = "检测到 whisper.cpp：用它的 Vulkan 后端走 AMD 显卡（引擎选 whispercpp、设备选 vulkan）。"
    elif report["directml"].get("ok"):
        report["recommend"] = "directml"
        report["advice"] = "检测到 DirectML：可以走 AMD 显卡，但需要在插件里换成 directml 引擎（当前版本还没接）。"
    else:
        report["recommend"] = "cpu"
        report["advice"] = "这台机器目前只能用 CPU（int8）。GPU 那条路要么装 whisper.cpp 的 Vulkan 版，要么装 onnxruntime-directml。"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
