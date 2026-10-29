#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_audio —— 直播间「听声音」worker（v0.4.0）

干一件事：把这个直播间的声音切成小段，转成带时间戳的文字，写进
  live-chat/<房间>/audio.jsonl
给上层（插件的自动陪聊 / 设置页 / 模型工具）当证据用。

链路：
  ① 解析直播间 HLS 地址（B站 web-room 播放信息接口，匿名可读）
  ② ffmpeg 拉流 → 16k 单声道 s16le PCM 走 stdout（不落盘）
  ③ 每 window 秒取一段（相邻段重叠 overlap 秒），喂给语音识别引擎
  ④ 结果追加 audio.jsonl，只保留最近 keep 秒

引擎：
  faster   —— faster-whisper（CPU int8；CTranslate2 只认 CUDA，AMD 卡开 cuda 必失败）
  whispercpp —— whisper.cpp 的 whisper-cli.exe（Vulkan 版能吃到 AMD 显卡）
  auto     —— 有 whisper.cpp 就用它，否则 faster-whisper

设备回退：请求 cuda/vulkan 但初始化失败时，把异常原文写进 audio.json 的 device_error，
然后自动退回 CPU —— 不静默假装成功，也不直接退出。

停止：同目录出现 stop.flag 就收工（和 live_chat_worker.py 一致）。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2  # s16le 单声道
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg: str, fh=None) -> None:
    line = "[%s] %s" % (now_iso(), msg)
    print(line, flush=True)
    if fh is not None:
        try:
            fh.write(line + "\n")
            fh.flush()
        except Exception:  # noqa: BLE001
            pass


def write_json(path: Path, value: dict) -> None:
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:  # noqa: BLE001
        pass


# ────────────────────────── ① 拿直播流地址 ──────────────────────────

def resolve_stream_url(room_id: int) -> str:
    """返回可直接喂给 ffmpeg 的 HLS 地址；拿不到就抛 RuntimeError。"""
    qs = urllib.parse.urlencode({
        "room_id": room_id,
        "protocol": "0,1",
        "format": "0,1,2",
        "codec": "0,1",
        "qn": 10000,
        "platform": "web",
        "ptype": 8,
    })
    url = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?" + qs
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://live.bilibili.com/",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if int(data.get("code") or 0) != 0:
        raise RuntimeError("B站 播放信息接口报错：code=%s %s" % (data.get("code"), data.get("message")))
    playurl = ((data.get("data") or {}).get("playurl_info") or {}).get("playurl") or {}
    streams = playurl.get("stream") or []
    if not streams:
        raise RuntimeError("没拿到播放流（房间没开播，或者这个房间需要登录）")
    # Audio-only capture: prefer HTTP-FLV; this room's HLS CDN fails TLS reads.
    # Start with the alternate CDN, which passed the bounded PCM capture probe.
    def rank(stream):
        proto = str(stream.get("protocol_name") or "")
        return 0 if proto == "http_stream" else 1
    for stream in sorted(streams, key=rank):
        best = None
        for fmt in stream.get("format") or []:
            for codec in fmt.get("codec") or []:
                # base_url belongs to codec, not to its CDN url_info entries.
                base_url = str(codec.get("base_url") or "")
                if not base_url.startswith("/") or base_url.startswith("//"):
                    continue
                for info in reversed(codec.get("url_info") or []):
                    host = str(info.get("host") or "")
                    extra = str(info.get("extra") or "")
                    try:
                        origin = urllib.parse.urlsplit(host)
                        if (origin.scheme not in ("http", "https") or not origin.hostname
                                or origin.username or origin.password or origin.query or origin.fragment
                                or origin.path not in ("", "/")):
                            continue
                        origin.port  # Reject malformed port values before invoking ffmpeg.
                    except ValueError:
                        continue
                    candidate = host.rstrip("/") + base_url
                    if extra:
                        # Bilibili base_url may already end in ? or contain query fields.
                        separator = "" if candidate.endswith(("?", "&")) else ("&" if "?" in base_url else "?")
                        candidate += separator + extra.lstrip("?&")
                    if any(ch.isspace() or ord(ch) < 32 for ch in candidate):
                        continue
                    qn = int(codec.get("current_qn") or 0)
                    if best is None or qn > best[0]:
                        best = (qn, candidate)
        if best and best[1]:
            return best[1]
    raise RuntimeError("播放信息里没有可用的 url_info")


# ────────────────────────── ②③ 引擎 ──────────────────────────

class FasterWhisperEngine:
    name = "faster-whisper"

    def __init__(self, model: str, device: str, language: str):
        from faster_whisper import WhisperModel

        self.language = language or None
        self.device = device or "cpu"
        # cuda 用 float16 快；cpu 用 int8 省内存
        compute = "float16" if self.device == "cuda" else "int8"
        self._model = WhisperModel(model, device=self.device, compute_type=compute)
        self.compute_type = compute
        self.model_name = model

    def transcribe(self, pcm: bytes) -> str:
        import numpy as np

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(
            audio,
            language=self.language,
            vad_filter=True,
            beam_size=1,
            condition_on_previous_text=False,
        )
        return "".join(seg.text for seg in segments).strip()


class WhisperCppEngine:
    name = "whisper.cpp"
    device = "vulkan"

    def __init__(self, binary: str, model_path: str, language: str):
        if not Path(binary).is_file():
            raise RuntimeError("whisper.cpp 可执行文件不存在：%s" % binary)
        if not Path(model_path).is_file():
            raise RuntimeError("whisper.cpp 模型不存在：%s（要 ggml 格式的 .bin）" % model_path)
        self.binary = binary
        self.model_path = model_path
        self.language = language or "auto"

    def transcribe(self, pcm: bytes) -> str:
        import wave

        with tempfile.TemporaryDirectory(prefix="bili-live-audio-") as tmp:
            wav = Path(tmp) / "chunk.wav"
            with wave.open(str(wav), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(pcm)
            out_prefix = Path(tmp) / "out"
            cmd = [
                self.binary,
                "-m", self.model_path,
                "-f", str(wav),
                "-l", self.language,
                "-nt",              # 不要时间戳
                "-otxt",            # 输出纯文本
                "-of", str(out_prefix),
                "-np",              # 不要进度条
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=180)
            txt = out_prefix.with_suffix(".txt")
            if proc.returncode != 0 or not txt.is_file():
                raise RuntimeError("whisper.cpp 失败（%s）：%s" % (proc.returncode, (proc.stderr or b"")[-300:].decode("utf-8", "replace")))
            return txt.read_text(encoding="utf-8", errors="replace").strip()


def _whisper_cpp_binary(home: Path, given: str) -> str:
    for c in (given, os.environ.get("DSH_BILI_LIVE_WHISPERCPP") or "", str(home / "bin" / "whisper-cli.exe"), str(home / "bin" / "main.exe"), shutil.which("whisper-cli") or ""):
        if c and Path(c).is_file():
            return str(c)
    return ""


def build_engine(args, home: Path):
    """返回 (engine, device_note)。设备失败会在 device_note 里留下异常原文。"""
    note = {"requested_device": args.device, "device_error": "", "fallback": False}
    binary = _whisper_cpp_binary(home, args.whispercpp) if args.engine in ("auto", "whispercpp") else ""
    model_bin = args.whispercpp_model or os.environ.get("DSH_BILI_LIVE_WHISPERCPP_MODEL") or str(home / "bin" / "ggml-%s.bin" % args.model)

    def faster(device):
        return FasterWhisperEngine(args.model, device, args.language)

    # 1) 明确要 GPU 的那几条路
    if args.device in ("cuda", "gpu", "auto") and args.engine in ("auto", "faster") and not binary:
        if args.device != "auto":
            try:
                eng = faster("cuda")
                return eng, note
            except Exception as exc:  # noqa: BLE001
                note["device_error"] = "%s: %s" % (type(exc).__name__, exc)
                note["fallback"] = True
                log("GPU 初始化失败，退回 CPU —— " + note["device_error"])
    # 2) whisper.cpp（Vulkan 版能吃 AMD 卡）
    if binary and args.device != "cpu" and args.engine in ("auto", "whispercpp"):
        try:
            return WhisperCppEngine(binary, model_bin, args.language), note
        except Exception as exc:  # noqa: BLE001
            note["device_error"] = (note["device_error"] + " | " if note["device_error"] else "") + "%s: %s" % (type(exc).__name__, exc)
            note["fallback"] = True
            log("whisper.cpp 用不了，退回 CPU —— " + str(exc))
    # 3) CPU 兜底
    return faster("cpu"), note


# ────────────────────────── ④ 落盘与裁剪 ──────────────────────────

def append_line(path: Path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def trim(path: Path, keep_sec: float) -> int:
    """只留最近 keep_sec 秒的行；返回剩下的行数。"""
    cutoff = datetime.now().astimezone() - timedelta(seconds=keep_sec)
    keep = []
    dropped = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        try:
            t = datetime.fromisoformat(str(rec.get("end") or rec.get("ts") or ""))
        except Exception:  # noqa: BLE001
            keep.append(rec)
            continue
        if t >= cutoff:
            keep.append(rec)
        else:
            dropped += 1
    if dropped:
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
        os.replace(str(tmp), str(path))
    return len(keep)


# ────────────────────────── 主循环 ──────────────────────────

class PcmReader(threading.Thread):
    """把 ffmpeg 的 stdout 收进一个环形缓冲。

    转写一段要好几百毫秒到好几秒，这段时间主线程没法继续 read；如果直接在主线程读，
    ffmpeg 的管道会堵住，直播流就掉队了。所以读流单独开一个线程，主线程只管取窗口。
    """

    def __init__(self, stream, cap_bytes: int, step_bytes: int, chunk: int = 8000):
        super().__init__(daemon=True)
        self.stream = stream
        self.cap = max(cap_bytes, step_bytes * 2)
        self.step = max(1, step_bytes)
        self.chunk = max(1024, chunk)
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.stop = False

    def run(self) -> None:
        while not self.stop:
            try:
                block = self.stream.read(self.chunk)
            except Exception:  # noqa: BLE001
                break
            if not block:
                break
            with self.lock:
                self.buf.extend(block)
                extra = len(self.buf) - self.cap
                if extra > 0:  # 转写跟不上就丢最旧的，宁可漏一句也不要堵死管道
                    del self.buf[:extra]

    def take(self, n: int):
        with self.lock:
            if len(self.buf) < n:
                return None
            pcm = bytes(self.buf[-n:])
            del self.buf[: min(self.step, len(self.buf))]
            return pcm


def tail_text(path: Path, limit: int = 1200) -> str:
    try:
        with path.open("rb") as fh:
            try:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - limit))
            except Exception:  # noqa: BLE001
                pass
            return fh.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="B站直播间语音转写 worker")
    ap.add_argument("--room", type=int, required=True)
    ap.add_argument("--dir", required=True, help="live-chat/<房间> 目录")
    ap.add_argument("--window", type=float, default=15.0, help="每段音频秒数")
    ap.add_argument("--overlap", type=float, default=2.0, help="相邻段重叠秒数")
    ap.add_argument("--keep", type=float, default=300.0, help="转录文本保留秒数")
    ap.add_argument("--model", default="small")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda", "gpu"])
    ap.add_argument("--engine", default="auto", choices=["auto", "faster", "whispercpp"])
    ap.add_argument("--language", default="zh")
    ap.add_argument("--ffmpeg", default="")
    ap.add_argument("--whispercpp", default="", help="whisper-cli.exe 路径")
    ap.add_argument("--whispercpp-model", default="", help="ggml 模型 .bin 路径")
    args = ap.parse_args()

    home = Path(os.environ.get("DSH_BILI_LIVE_HOME") or Path(__file__).parent).expanduser().resolve()
    d = Path(args.dir).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    status_path = d / "audio.json"
    jsonl_path = d / "audio.jsonl"
    stop_flag = d / "stop.flag"
    log_path = d / "audio.log"

    window = max(5.0, float(args.window))
    overlap = max(0.0, min(float(args.overlap), window - 2.0))
    keep = max(60.0, float(args.keep))
    step = window - overlap

    status = {
        "pid": os.getpid(),
        "room": args.room,
        "started_at": now_iso(),
        "window": window,
        "overlap": overlap,
        "model": args.model,
        "requested_device": args.device,
        "engine": "",
        "device": "",
        "device_error": "",
        "fallback": False,
        "segments": 0,
        "chars": 0,
        "last_ok_at": "",
        "last_error": "",
        "state": "starting",
    }

    with log_path.open("a", encoding="utf-8") as logf:
        write_json(status_path, status)
        log("启动：房间 %s，窗口 %.0fs/重叠 %.0fs，模型 %s，设备 %s" % (args.room, window, overlap, args.model, args.device), logf)

        engine, note = build_engine(args, home)
        status["device_error"] = note.get("device_error") or ""
        status["fallback"] = bool(note.get("fallback"))
        status["requested_device"] = note.get("requested_device") or args.device
        status["engine"] = getattr(engine, "name", "?")
        status["device"] = getattr(engine, "device", "cpu")
        status["state"] = "running"
        write_json(status_path, status)
        log("引擎就绪：%s / %s" % (status["engine"], status["device"]), logf)
        if status["device_error"]:
            log("GPU 那条路失败了，已退回 CPU。原始报错：" + status["device_error"], logf)

        ffmpeg = args.ffmpeg or os.environ.get("DSH_BILI_LIVE_FFMPEG") or shutil.which("ffmpeg") or ""
        if not ffmpeg or not Path(ffmpeg).is_file():
            status["state"] = "error"
            status["last_error"] = "找不到 ffmpeg（用 --ffmpeg 指定，或设 DSH_BILI_LIVE_FFMPEG）"
            write_json(status_path, status)
            log(status["last_error"], logf)
            return 2

        window_bytes = int(window * BYTES_PER_SEC)
        step_bytes = int(step * BYTES_PER_SEC)
        fail_count = 0

        while not stop_flag.exists():
            try:
                stream_url = resolve_stream_url(args.room)
            except Exception as exc:  # noqa: BLE001
                status["last_error"] = "拿流地址失败：%s: %s" % (type(exc).__name__, exc)
                status["state"] = "waiting_stream"
                write_json(status_path, status)
                log(status["last_error"], logf)
                fail_count += 1
                if fail_count >= 5:
                    status["state"] = "error"
                    write_json(status_path, status)
                    return 3
                time.sleep(min(30, 5 * fail_count))
                continue
            fail_count = 0
            cmd = [
                ffmpeg,
                "-nostdin", "-hide_banner", "-loglevel", "error",
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-user_agent", UA,
                "-headers", "Referer: https://live.bilibili.com/\r\n",
                "-i", stream_url,
                "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                "-f", "s16le", "-",
            ]
            log("开始拉流（%s）" % urllib.parse.urlsplit(stream_url).hostname, logf)
            err_path = d / "audio.ffmpeg.log"
            try:
                errf = err_path.open("ab")
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf)
            except Exception as exc:  # noqa: BLE001
                status["last_error"] = "ffmpeg 起不来：%s" % exc
                write_json(status_path, status)
                log(status["last_error"], logf)
                time.sleep(5)
                continue

            # 读流在单独线程里，转写期间 ffmpeg 也不会被堵住
            reader = PcmReader(proc.stdout, cap_bytes=window_bytes * 4, step_bytes=step_bytes, chunk=BYTES_PER_SEC // 2)
            reader.start()
            try:
                while not stop_flag.exists():
                    pcm = reader.take(window_bytes)
                    if pcm is None:
                        if not reader.is_alive():
                            break
                        time.sleep(0.2)
                        continue
                    start = datetime.now().astimezone() - timedelta(seconds=window)
                    try:
                        text = engine.transcribe(pcm)
                    except Exception as exc:  # noqa: BLE001
                        status["last_error"] = "转写失败：%s: %s" % (type(exc).__name__, exc)
                        write_json(status_path, status)
                        log(status["last_error"], logf)
                        if isinstance(engine, WhisperCppEngine):
                            # Vulkan errors occur during inference, not the file-only constructor.
                            status["device_error"] = "%s: %s" % (type(exc).__name__, exc)
                            status["fallback"] = True
                            write_json(status_path, status)
                            log("Vulkan 失败，保留原始报错并切换 CPU：" + status["device_error"], logf)
                            try:
                                engine = FasterWhisperEngine(args.model, "cpu", args.language)
                                status["engine"] = engine.name
                                status["device"] = "cpu"
                            except Exception as cpu_exc:
                                status["state"] = "error"
                                status["last_error"] += " | CPU fallback: %s: %s" % (type(cpu_exc).__name__, cpu_exc)
                                write_json(status_path, status)
                                return 4
                            write_json(status_path, status)
                        time.sleep(1)
                        continue
                    if text:
                        rec = {
                            "ts": start.isoformat(timespec="seconds"),
                            "end": now_iso(),
                            "dur": round(window, 1),
                            "text": text[:400],
                            "engine": status["engine"],
                        }
                        append_line(jsonl_path, rec)
                        status["segments"] += 1
                        status["chars"] += len(text)
                        status["last_ok_at"] = rec["end"]
                        status["last_error"] = ""
                        status["state"] = "running"
                        if status["segments"] % 5 == 1:
                            status["kept_lines"] = trim(jsonl_path, keep)
                        write_json(status_path, status)
                        log("听到：" + text[:80], logf)
                    else:
                        status["last_ok_at"] = now_iso()
                        write_json(status_path, status)
            finally:
                reader.stop = True
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    errf.close()
                except Exception:  # noqa: BLE001
                    pass
            err = tail_text(err_path)
            if stop_flag.exists():
                break
            status["last_error"] = ("ffmpeg 断流：" + err[-300:]) if err else "ffmpeg 断流（没有报错输出）"
            status["state"] = "reconnecting"
            write_json(status_path, status)
            log(status["last_error"] + "，5 秒后重连", logf)
            time.sleep(5)

        status["state"] = "stopped"
        status["stopped_at"] = now_iso()
        write_json(status_path, status)
        log("收工", logf)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
