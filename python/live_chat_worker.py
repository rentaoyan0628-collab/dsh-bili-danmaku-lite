"""直播间陪聊 worker（耳朵 + 手 + 叫醒）
用法:
  python live_chat_worker.py --room 32438129 --mid 627306137 [--wake-key group:937904939]
行为:
  - 连上房间弹幕流，把收到的弹幕写进 <dir>/danmaku.jsonl
  - 轮询 <dir>/queue.jsonl，按顺序把 {"id":..., "text":...} 发出去（60 秒节流交给 live_mcp）
  - 每 45 秒查一次开播状态；一旦 liveStatus=0 → 写 closed.json、断开、退出
  - 必须存在 <dir>/armed.json 且 allow=true 才肯启动（管理员许可闸）
  - 出现 <dir>/stop.flag 也会立刻退出

2026-10-25 加「叫醒」（脑子还是 QQ 会话里的模型，worker 只负责判断值不值得叫）：
  - 每 judge-sec 秒读 danmaku.jsonl 的增量（byte offset 存 cursor.txt）
  - 判据照 bili-live-hangout skill：增量里 ≥1 条实质内容 **且** 最近一条弹幕 ≤3 分钟
    （有人 @ 我 / 点名「大肥鱼」直接算够）；够就写 hint.json 并 POST 桥接
    /api/socialV2/wake 把对应 QQ 会话叫醒，带 wake-cooldown 秒冷却，防止刷爆。
  - 不接 wake-key 时只写 hint.json 不叫醒（纯收弹幕模式）。
"""
import argparse, asyncio, json, os, re, sys, time, datetime, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_mcp
from bilibili_api import live

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0.0.0 Safari/537.36"

# 自己被怎么叫：判「点名」用（设置页/宿主会传 --name 或环境变量）
SELF_NAME = (os.environ.get("DSH_BILI_LIVE_NAME") or "").strip() or "主播"

# 运行目录：默认 $DSH_BILI_LIVE_HOME/live-chat，其次插件目录上一层的 live-chat
_DEFAULT_HOME = os.environ.get("DSH_BILI_LIVE_HOME") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DEFAULT_DIR = os.path.join(os.path.abspath(_DEFAULT_HOME), "live-chat")


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def live_status(mid: int) -> dict:
    url = f"https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid={mid}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://live.bilibili.com/"})
    d = json.load(urllib.request.urlopen(req, timeout=15))["data"]
    return {"roomid": d.get("roomid"), "title": d.get("title"), "live": d.get("liveStatus"), "online": d.get("online")}


def append_jsonl(path: str, obj: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()


def read_queue(path: str) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def rewrite_queue(path: str, items: list) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


async def main_async(args):
    d = args.dir
    os.makedirs(d, exist_ok=True)
    log_path = os.path.join(d, "worker.log")

    def log(msg):
        line = f"[{now()}] {msg}"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        # ⚠️ 2026-10-25：原来这里裸 print，控制台是 GBK 时遇到弹幕里的 emoji（🐟 之类）
        #    会抛 UnicodeEncodeError —— 而 judge_task 里 log() 一炸，整个 worker 就退出
        #    （22:29:58 就这么"睡着"的，房间里的人还在问"猫猫鱼怎么掉线了"）。
        #    日志是辅助功能，永远不该把主进程带走。
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or "utf-8"
            try:
                print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)
            except Exception:
                pass
        except Exception:
            pass

    armed = os.path.join(d, "armed.json")
    if not os.path.exists(armed):
        log("REFUSED 没有 armed.json（管理员未放行），不进场")
        return 2
    try:
        allow = json.load(open(armed, encoding="utf-8")).get("allow")
    except Exception:
        allow = False
    if not allow:
        log("REFUSED armed.json allow=false，不进场")
        return 2

    st = live_status(args.mid)
    log(f"进场前检查: {json.dumps(st, ensure_ascii=False)}")
    if not st.get("live"):
        log("主播没在播，直接退出（不进直播间）")
        append_jsonl(os.path.join(d, "closed.jsonl"), {"ts": now(), "reason": "not_live", **st})
        return 0

    dm_log = os.path.join(d, "danmaku.jsonl")
    _seen = set()
    queue = os.path.join(d, "queue.jsonl")
    sent_log = os.path.join(d, "sent.jsonl")
    stop_flag = os.path.join(d, "stop.flag")
    cursor_path = os.path.join(d, "cursor.txt")
    hint_path = os.path.join(d, "hint.json")
    wake_log = os.path.join(d, "wake.jsonl")

    cred = live_mcp._cred()
    dm = live.LiveDanmaku(room_display_id=int(args.room), credential=cred)
    self_uid = str(getattr(cred, "dedeuserid", "") or "")

    @dm.on("DANMU_MSG")
    async def on_danmaku(event):  # noqa: ANN001
        try:
            info = event["data"]["info"]
            text = info[1]
            uid = str(info[2][0])
            uname = info[2][1]
            if uid == self_uid:
                return
            if (uname, text) in _seen:
                return
            _seen.add((uname, text))
            rec = {"ts": now(), "uid": uid, "uname": uname, "text": text}
            if SELF_NAME in text:
                rec["at_me"] = True
            append_jsonl(dm_log, rec)
        except Exception as exc:  # noqa: BLE001
            log(f"danmaku parse error: {type(exc).__name__}: {exc}")

    async def ws_task():
        # ⚠️ 2026-10-25：原来 connect() 一返回就整个 worker 退出（main 用 FIRST_COMPLETED），
        #    B 站弹幕长连接半夜断一次，人就"睡着"了。改成断了自动重连，只有 stop.flag 才收工。
        attempt = 0
        while True:
            if os.path.exists(stop_flag):
                return
            try:
                if attempt == 0:
                    log(f"连接弹幕流 room={args.room} ...")
                else:
                    log(f"重连弹幕流（第 {attempt} 次）room={args.room} ...")
                await dm.connect()
                log("弹幕流断开（connect 返回）")
            except Exception as e:  # noqa: BLE001
                log(f"弹幕流异常：{type(e).__name__}: {e}")
            attempt += 1
            wait_s = min(60, 5 * attempt)
            log(f"{wait_s}s 后重连（worker 不退出）")
            await asyncio.sleep(wait_s)

    async def queue_task():
        while True:
            if os.path.exists(stop_flag):
                return
            items = read_queue(queue)
            if items:
                first = items[0]
                text = (first.get("text") or "").strip()
                if not text:
                    rewrite_queue(queue, items[1:])
                    continue
                try:
                    raw = await live_mcp.bili_live_send(int(args.room), text, confirm=True)
                    res = json.loads(raw)
                except Exception as exc:  # noqa: BLE001
                    log(f"send error: {type(exc).__name__}: {exc}")
                    res = {"ok": False, "status": "error"}
                status = res.get("status")
                if status == "sent":
                    log(f"SENT {text}")
                    append_jsonl(sent_log, {"ts": now(), "text": text, "id_str": res.get("id_str")})
                    rewrite_queue(queue, items[1:])
                elif status in ("throttled", "stop_retrying"):
                    wait = float(res.get("cooldown_remaining") or 30) + 0.2
                    log(f"throttled, 等 {round(wait, 1)}s 后再试（{status}）")
                    await asyncio.sleep(max(2.0, wait))
                else:
                    log(f"SEND FAILED {json.dumps(res, ensure_ascii=False)[:200]}")
                    rewrite_queue(queue, items[1:])
            await asyncio.sleep(1)

    async def watch_task():
        while True:
            await asyncio.sleep(45)
            if os.path.exists(stop_flag):
                log("发现 stop.flag，收工")
                append_jsonl(os.path.join(d, "closed.jsonl"), {"ts": now(), "reason": "stop_flag"})
                return
            try:
                st2 = live_status(args.mid)
            except Exception as exc:  # noqa: BLE001
                log(f"状态查询失败: {type(exc).__name__}")
                continue
            if not st2.get("live"):
                log(f"直播结束: {json.dumps(st2, ensure_ascii=False)}")
                append_jsonl(os.path.join(d, "closed.jsonl"), {"ts": now(), "reason": "stream_ended", **st2})
                return

    async def backfill_task():
        """每 20 秒用接口历史补一刀：弹幕流偶尔漏帧，历史里有的补进来。"""
        seen = _seen
        try:
            with open(dm_log, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    seen.add((r.get("uname") or "", r.get("text") or ""))
        except FileNotFoundError:
            pass
        while True:
            await asyncio.sleep(20)
            if os.path.exists(stop_flag):
                return
            try:
                hist = live_mcp._history(int(args.room)) or []
            except Exception as exc:  # noqa: BLE001
                log(f"history 拉取失败: {type(exc).__name__}")
                continue
            added = 0
            for m in hist:
                uname = str(m.get("uname") or "")
                text = (m.get("text") or "").strip()
                if not text or uname == SELF_NAME:
                    continue
                key = (uname, text)
                if key in seen:
                    continue
                seen.add(key)
                rec = {"ts": now(), "uid": str(m.get("uid") or ""), "uname": uname, "text": text, "src": "history"}
                if SELF_NAME in text:
                    rec["at_me"] = True
                append_jsonl(dm_log, rec)
                added += 1
            if added:
                log(f"history 回填 {added} 条")

    # ── 叫醒（判据照 bili-live-hangout skill：≥1 条实质 + 最近一条 ≤3 分钟）─────
    _SUBSTANTIVE_JUNK = {"6666", "666", "签到", "打卡", "抽奖", "点歌", "来了", "来了来了", "好耶"}

    def _read_cursor() -> int:
        try:
            raw = open(cursor_path, encoding="utf-8").read().strip()
            return max(0, int(raw))
        except Exception:
            return 0

    def _write_cursor(pos: int) -> None:
        try:
            with open(cursor_path, "w", encoding="utf-8") as f:
                f.write(str(int(pos)))
        except Exception:
            pass

    def _read_new(pos: int):
        if not os.path.exists(dm_log):
            return [], pos
        recs = []
        with open(dm_log, encoding="utf-8") as f:
            f.seek(0, 2)
            size = f.tell()
            if pos > size:  # 日志被截断或重写
                pos = 0
            f.seek(pos)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
        return recs, size

    def _substantive(text: str) -> bool:
        t = (text or "").strip()
        if len(t) < 4 or t in _SUBSTANTIVE_JUNK:
            return False
        if re.fullmatch(r"[\d\W_]+", t):
            return False
        return bool(re.search(r"[0-9A-Za-z\u4e00-\u9fff]", t))

    def _minutes_since(ts_text: str) -> float:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%H:%M:%S"):
            try:
                dt = datetime.datetime.strptime(ts_text, fmt)
                if fmt == "%H:%M:%S":
                    now_dt = datetime.datetime.now()
                    dt = dt.replace(year=now_dt.year, month=now_dt.month, day=now_dt.day)
                return (datetime.datetime.now() - dt).total_seconds() / 60.0
            except Exception:
                continue
        return 999.0

    async def _wake(reason: str, recs: list) -> None:
        """叫醒 = POST 到用户配的 webhook；没配就只留 hint.json / wake.jsonl。

        独立插件不知道你的机器人在哪，所以这里不写死任何地址：
        想让它去叫你自己的 bot，就在设置页里填一个 webhook 地址。
        """
        url = str(getattr(args, "webhook", "") or "").strip()
        if not url:
            res = {"ok": True, "skipped": "没有配 webhook，只写 hint"}
        else:
            payload = {
                "source": "dsh-bili-live",
                "room": int(args.room),
                "reason": reason,
                "count": len(recs),
                "sample": [
                    {"uname": r.get("uname"), "text": str(r.get("text") or "")[:120]}
                    for r in recs[-5:]
                ],
            }

            def _post() -> dict:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={"content-type": "application/json; charset=utf-8"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - 用户自己填的地址
                    raw = resp.read().decode("utf-8", "replace")
                try:
                    return json.loads(raw)
                except Exception:
                    return {"ok": True, "http_status": getattr(resp, "status", 0), "body": raw[:200]}

            try:
                res = await asyncio.to_thread(_post)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        append_jsonl(wake_log, {"ts": now(), "reason": reason, "result": res, "lines": len(recs)})
        log(f"WAKE {reason} -> {json.dumps(res, ensure_ascii=False)[:160]}")

    async def judge_task():
        """每 judge-sec 秒看一次增量：够得着就写 hint.json，并按冷却叫醒 QQ 会话。"""
        last_wake = 0.0
        wake_times = []
        cap = int(args.max_wakes_per_hour)
        while True:
            await asyncio.sleep(max(10, int(args.judge_sec)))
            if os.path.exists(stop_flag):
                return
            pos = _read_cursor()
            recs, new_pos = _read_new(pos)
            _write_cursor(new_pos)
            if not recs:
                continue
            fresh = [r for r in recs if _minutes_since(str(r.get("ts") or "")) <= 3.0]
            at_me = [r for r in fresh if r.get("at_me") or SELF_NAME in str(r.get("text") or "")]
            solid = [r for r in fresh if _substantive(str(r.get("text") or ""))]
            hint = {
                "ts": now(),
                "room": int(args.room),
                "new": len(recs),
                "fresh": len(fresh),
                "substantive": len(solid),
                "at_me": len(at_me),
                "lines": [
                    {"uname": r.get("uname"), "text": r.get("text"), "at_me": bool(r.get("at_me"))}
                    for r in recs[-8:]
                ],
            }
            try:
                with open(hint_path, "w", encoding="utf-8") as f:
                    json.dump(hint, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            # 判据（2026-10-25 按用户 m02807 改）：
            #   ① 被 @ / 被点名 → 立刻（不受冷却限制，但仍受每小时 cap 约束）
            #   ② 否则必须 ≥2 条实质内容才叫（1 条不够 —— 半句话不值得起一个模型回合）
            need = bool(at_me) or len(solid) >= 2
            if not need:
                continue
            if not str(getattr(args, "webhook", "") or "").strip():
                log(f"够得着但没配 webhook，只写 hint（新增 {len(recs)}，实质 {len(solid)}）")
                continue
            gap = time.time() - last_wake
            if gap < float(args.wake_cooldown) and not at_me:
                log(f"够得着但冷却中（还剩 {int(float(args.wake_cooldown) - gap)}s，实质 {len(solid)}），只写 hint")
                continue
            now_ts = time.time()
            wake_times = [t for t in wake_times if now_ts - t < 3600]
            if len(wake_times) >= cap:
                log(f"够得着但这一小时已经叫醒 {len(wake_times)} 次（上限 {cap}），只写 hint")
                continue
            last_wake = now_ts
            wake_times.append(now_ts)
            try:
                await _wake(f"live:{int(args.room)}", recs)
            except Exception as e:  # noqa: BLE001
                log(f"叫醒失败（忽略继续）：{type(e).__name__}: {e}")
                continue
            sample = " / ".join(f"{r.get('uname')}：{str(r.get('text') or '')[:24]}" for r in (at_me or solid)[-3:])
            log(f"叫醒：{sample[:160]}")

    tasks = [asyncio.create_task(ws_task()), asyncio.create_task(queue_task()), asyncio.create_task(watch_task()), asyncio.create_task(backfill_task()), asyncio.create_task(judge_task())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    log("worker 退出")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", type=int, default=32438129)
    ap.add_argument("--mid", type=int, default=627306137)
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--name", default="", help="自己叫什么（判「点名」用），留空读 DSH_BILI_LIVE_NAME")
    ap.add_argument("--webhook", default="", help="够得着时 POST 到这个地址（留空=只写 hint.json，不打扰任何人）")
    ap.add_argument("--wake-cooldown", type=float, default=90.0, help="两次叫醒之间最少隔多少秒（被 @ / 点名时不受此限）")
    ap.add_argument("--judge-sec", type=float, default=60.0, help="多久看一次增量弹幕")
    ap.add_argument("--max-wakes-per-hour", type=int, default=30, help="每小时最多叫醒几次（防刷爆 token）")
    args = ap.parse_args()
    global SELF_NAME
    if str(args.name or "").strip():
        SELF_NAME = str(args.name).strip()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
