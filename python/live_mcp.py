#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 站直播间 MCP 服务（bili-live）—— 独立插件版

与原先靠 QQ 桥接跑的那份的区别只有两处：
  · 家里（凭证/放行名单/日志）跟脚本目录解耦，由 DSH_BILI_LIVE_HOME 指定；
  · 放行申请不再 POST 到 QQ 桥接，只写本地 live-requests.jsonl，等人放行。


三个工具：
- bili_live_info(room_id)          房间信息 + 最近弹幕历史（只读）
- bili_live_feed(room_id, seconds) 监听实时弹幕/礼物/进场（只读）
- bili_live_send(room_id, text, confirm) 发弹幕，默认只出草稿；每房间 60 秒 1 条
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

from mcp.server.fastmcp import FastMCP

from bilibili_api import Credential, Danmaku, live, search

# 家里（运行数据目录）：默认是脚本同目录，但独立插件会把它指到
# $DSH_HOME/bili-live/ —— 凭证、放行名单、日志都落在那里，**不写进插件包**。
HOME = Path(os.environ.get("DSH_BILI_LIVE_HOME") or Path(__file__).parent).expanduser().resolve()

CRED_FILE = HOME / "bili_credential.json"
STATE_FILE = HOME / "live-send-state.json"
LOG_FILE = HOME / "logs" / "danmaku.jsonl"
ALLOW_FILE = HOME / "live-allow.json"

DEFAULT_ROOM = int(os.environ.get("DSH_BILI_LIVE_DEFAULT_ROOM", "0") or 0)  # 0 = 必须显式指定房间
MIN_INTERVAL_SEC = 60

mcp = FastMCP("bili-live")

_cred_cache: dict = {"cred": None, "mtime": None}


def _cred() -> Credential:
    st = CRED_FILE.stat().st_mtime
    if _cred_cache["cred"] is None or _cred_cache["mtime"] != st:
        c = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        _cred_cache["cred"] = Credential(
            sessdata=c.get("sessdata"),
            bili_jct=c.get("bili_jct"),
            buvid3=c.get("buvid3"),
            dedeuserid=c.get("dedeuserid"),
        )
        _cred_cache["mtime"] = st
    return _cred_cache["cred"]


def _room(room_id: int) -> "live.LiveRoom":
    return live.LiveRoom(room_display_id=int(room_id), credential=_cred())


# ── 房间号解析：房间号 / UP主昵称 / UID 都能用 ──────────────────────────────
#
# 三种写法都收（模型只需要知道对方叫什么，不必先手动查 roomid）：
#   21452505       房间号
#   "某个昵称"     昵称 → 搜用户 → UID → 直播间号
#   8126336        UID（注意：数字既可能是房间号也可能是 UID，这里按房间号优先）
# 昵称撞车（搜出来多个）时不猜，直接报错并列出候选，让人挑。

def _roomid_of_uid(uid: int) -> int:
    """UID → 直播间号（getRoomInfoOld 不需要登录也能查）。"""
    url = f"https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid={int(uid)}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    inner = data.get("data") or {}
    rid = inner.get("roomid") or inner.get("room_id")
    if not rid:
        raise RuntimeError(f"UID {uid} 没有开通直播间（code={data.get('code')}）")
    return int(rid)


def _clean_em(s) -> str:
    return str(s or "").replace('<em class="keyword">', "").replace("</em>", "")


async def _uid_of_name(name: str) -> int:
    """昵称 → UID。完全同名优先，其次唯一子串命中；有歧义就报错列候选。"""
    want = (name or "").strip()
    if not want:
        raise RuntimeError("昵称不能为空")
    r = await search.search_by_type(keyword=want, search_type=search.SearchObjectType.USER, page=1)
    users = r.get("result") or []
    if not users:
        raise RuntimeError(f"B站上没搜到叫「{want}」的 UP 主")
    norm = lambda s: str(s or "").lower().replace(" ", "")  # noqa: E731
    nw = norm(want)
    exact = [u for u in users if norm(_clean_em(u.get("uname"))) == nw]
    if len(exact) == 1:
        return int(exact[0]["mid"])
    fuzzy = [u for u in users if nw in norm(_clean_em(u.get("uname"))) or norm(_clean_em(u.get("uname"))) in nw]
    if len(exact) == 0 and len(fuzzy) == 1:
        return int(fuzzy[0]["mid"])
    cand = (exact or fuzzy)[:8]
    listing = "、".join(f"{_clean_em(u.get('uname'))}(uid {u.get('mid')})" for u in cand)
    raise RuntimeError(f"「{want}」对得上不只一个 UP 主：{listing} —— 说清楚是哪一位，或直接给 UID")


async def _resolve_room(room_id: Union[int, str]) -> int:
    """把 房间号 / 昵称 / UID 统一成数字房间号。

    规则（刻意**不做模糊猜测**——发弹幕发错房间是真事故）：
      - 纯数字 ≤8 位  → 直接当房间号（B站房间号目前最长 8 位）
      - 纯数字 ≥9 位  → 当 UID（用户 mid 都很长，不可能是房间号）
      - "uid:8126336" → 明确指定 UID（短 UID 用这个写法，避免和房间号混淆）
      - 其它字符串     → 当 UP 主昵称去搜，搜到唯一一个才用
    """
    if isinstance(room_id, bool):
        raise RuntimeError("room_id 不能是布尔值")
    if isinstance(room_id, int):
        room_id = str(room_id)
    s = str(room_id or "").strip().lstrip("@")
    if not s:
        if DEFAULT_ROOM:
            return DEFAULT_ROOM
        raise RuntimeError("请给直播间号 / UP主昵称 / UID（也可以设 DSH_BILI_LIVE_DEFAULT_ROOM）")
    low = s.lower()
    if low.startswith("uid:") or low.startswith("uid="):
        return _roomid_of_uid(int(s[4:].strip()))
    if s.isdigit():
        if len(s) >= 9:
            return _roomid_of_uid(int(s))
        return int(s)
    return _roomid_of_uid(await _uid_of_name(s))


async def _live_status(room_id: int) -> int:
    """1=在播，0=没播，2=轮播。取不到就返回 -1。"""
    try:
        info = await _room(room_id).get_room_info()
        return int((info.get("room_info") or {}).get("live_status", -1))
    except Exception:  # noqa: BLE001
        return -1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log_danmaku(result: str, **fields) -> None:
    """把每一次弹幕动作（草稿 / 真发 / 被拦 / 失败）追加一行 JSONL 到 logs/danmaku.jsonl。

    用户 2026-10-25 的原话：「给，但是要写日志记录发了什么」——所以连**草稿**也记，
    这样事后能看出「他打算发什么、最后有没有真发出去」。
    日志写失败绝不影响发送本身（全部吞掉）。
    """
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "result": result}
        rec.update(fields)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _load_allow() -> dict:
    """用户放行过的直播间名单：{"enforce": true, "rooms": {"21452505": {...}}}。

    用户 2026-10-25 的规矩：「进入直播间需要管理员同意，只能进入管理员选择的直播间；
    进去之后发消息不用先把草稿给我看，可以直接发，只要做好日志留档」。
    ⇒ 硬闸就落在这份名单上：不在名单里的房间，info/feed/send 一律拒绝；
    名单只能由人在设置页或主会话里改，普通会话里没有 bili_live_allow 这个工具。
    """
    try:
        data = json.loads(ALLOW_FILE.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            data.setdefault("rooms", {})
            data.setdefault("enforce", True)
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"enforce": True, "rooms": {}}


def _save_allow(data: dict) -> None:
    tmp = ALLOW_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ALLOW_FILE)


async def _allow_check(room_id: int) -> str:
    """房间没放行就返回一句拒绝说明（caller 直接回给模型），放行则返回空串。

    名额可以带两种期限：
    - expires_at：ISO 时间，过了就自动失效（顺手从名单里删掉）；
    - until_offline：true = 只放行「这一场直播」，查到现在没在播就自动收回。
    用户说的是「只有这一次直播」时就该用 until_offline。
    """
    data = _load_allow()
    if not data.get("enforce", True):
        return ""
    rooms = data.get("rooms") or {}
    key = str(room_id)
    entry = rooms.get(key)
    dropped = False
    if isinstance(entry, dict):
        exp = entry.get("expires_at")
        if exp:
            try:
                if datetime.fromisoformat(str(exp)).timestamp() <= time.time():
                    rooms.pop(key, None)
                    dropped = True
                    entry = None
            except Exception:  # noqa: BLE001
                pass
        if isinstance(entry, dict) and entry.get("until_offline"):
            if await _live_status(room_id) != 1:
                rooms.pop(key, None)
                dropped = True
                entry = None
    if dropped:
        try:
            _save_allow(data)
        except Exception:  # noqa: BLE001
            pass
    if isinstance(entry, dict):
        return ""
    allowed = "、".join(
        f"{k}（{v.get('anchor') or '未知'}）" for k, v in rooms.items() if isinstance(v, dict)
    ) or "（还没有）"
    return (
        f"房间 {room_id} 不在放行名单里，我不进去。已放行：{allowed}。"
        "想去新的直播间，先问有权限的人：等他本人回一句带房间号或主播名的同意话（比如「放行 21452505」），"
        "再用 bili_live_request 提交申请；桥接会核对管理员本人那条消息。"
    )


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("rooms", {})
                return data
        except Exception:  # noqa: BLE001
            pass
    return {"rooms": {}}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _history(room_id: int, pagesize: int = 10) -> list:
    import urllib.parse
    import urllib.request

    q = urllib.parse.urlencode({"roomid": int(room_id), "page": 1, "pagesize": pagesize})
    req = urllib.request.Request(
        "https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory?" + q,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": f"https://live.bilibili.com/{room_id}",
            "Cookie": f"SESSDATA={_cred().sessdata}",
        },
    )
    raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
    data = json.loads(raw)
    room = ((data.get("data") or {}).get("room")) or []
    return [
        {"uname": h.get("nickname"), "uid": h.get("uid"), "text": h.get("text"), "timeline": h.get("timeline")}
        for h in room
    ]


async def _room_info(room_id: int) -> dict:
    info = await _room(room_id).get_room_info()
    ri = (info or {}).get("room_info") or {}
    anchor = ((info or {}).get("anchor_info") or {}).get("base_info") or {}
    return {
        "room_id": ri.get("room_id"),
        "short_id": ri.get("short_id"),
        "title": ri.get("title"),
        "live_status": ri.get("live_status"),
        "live": ri.get("live_status") == 1,
        "area": f"{ri.get('parent_area_name')}/{ri.get('area_name')}",
        "online": ri.get("online"),
        "attention": ri.get("attention"),
        "anchor": {"uid": ri.get("uid"), "uname": anchor.get("uname")},
        "description": (ri.get("description") or "")[:160],
    }


@mcp.tool()
async def bili_live_info(room_id: Union[int, str] = DEFAULT_ROOM, history_size: int = 10) -> str:
    """查直播间信息（是否在播、标题、分区、在线人数）和最近弹幕历史。只读，不会发送任何内容。

    Args:
        room_id: 直播间号 / UP主昵称 / UID 都能用（必填）
        history_size: 返回最近几条弹幕，1-20
    """
    try:
        room_id = await _resolve_room(room_id)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    denied = await _allow_check(room_id)
    if denied:
        return json.dumps({"ok": False, "status": "not_allowed", "room_id": room_id, "error": denied}, ensure_ascii=False)
    try:
        info = await _room_info(room_id)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    try:
        hist = _history(room_id, max(1, min(int(history_size), 20)))
    except Exception as exc:  # noqa: BLE001
        hist = []
        info["history_error"] = f"{type(exc).__name__}: {exc}"
    return json.dumps({"ok": True, "info": info, "recent_danmaku": hist}, ensure_ascii=False, indent=2)


@mcp.tool()
async def bili_live_feed(room_id: Union[int, str] = DEFAULT_ROOM, seconds: int = 20, max_items: int = 40) -> str:
    """监听直播间实时消息若干秒（弹幕/礼物/SC/进场），只读。

    Args:
        room_id: 直播间号 / UP主昵称 / UID 都能用（必填）
        seconds: 监听秒数，5-120，默认 20
        max_items: 最多返回多少条弹幕，默认 40
    """
    try:
        room_id = await _resolve_room(room_id)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    denied = await _allow_check(room_id)
    if denied:
        return json.dumps({"ok": False, "status": "not_allowed", "room_id": room_id, "error": denied}, ensure_ascii=False)
    seconds = max(5, min(int(seconds), 120))
    max_items = max(1, min(int(max_items), 200))
    started = time.time()
    danmakus: list = []
    gifts: list = []
    superchats: list = []
    counts = {"danmaku": 0, "gift": 0, "superchat": 0, "enter": 0, "follow": 0}
    online: list = []
    seen_events: dict = {}

    dm = live.LiveDanmaku(int(room_id), credential=_cred())

    def bump(k: str) -> None:
        seen_events[k] = seen_events.get(k, 0) + 1

    @dm.on("DANMU_MSG")
    async def _on_danmaku(event):
        bump("DANMU_MSG")
        counts["danmaku"] += 1
        try:
            d = event["data"]["info"]
            medal = None
            if d[3]:
                try:
                    medal = {"name": d[3][1], "level": d[3][0]}
                except Exception:  # noqa: BLE001
                    pass
            if len(danmakus) < max_items:
                danmakus.append({
                    "t": round(time.time() - started, 1),
                    "uid": d[2][0],
                    "uname": d[2][1],
                    "text": d[1],
                    "medal": medal,
                })
        except Exception:  # noqa: BLE001
            pass

    @dm.on("SEND_GIFT")
    async def _on_gift(event):
        bump("SEND_GIFT")
        counts["gift"] += 1
        try:
            d = event["data"]["data"]
            if len(gifts) < 20:
                gifts.append({"uname": d.get("uname"), "gift": d.get("giftName"), "num": d.get("num")})
        except Exception:  # noqa: BLE001
            pass

    @dm.on("SUPER_CHAT_MESSAGE")
    async def _on_sc(event):
        bump("SUPER_CHAT_MESSAGE")
        counts["superchat"] += 1
        try:
            d = event["data"]["data"]
            if len(superchats) < 10:
                superchats.append({"uname": (d.get("user_info") or {}).get("uname"), "text": d.get("message"), "price": d.get("price")})
        except Exception:  # noqa: BLE001
            pass

    @dm.on("INTERACT_WORD")
    async def _on_interact(event):
        bump("INTERACT_WORD")
        try:
            mt = event["data"]["data"].get("msg_type")
            if mt == 2:
                counts["follow"] += 1
            elif mt == 1:
                counts["enter"] += 1
        except Exception:  # noqa: BLE001
            pass

    @dm.on("ONLINE_RANK_COUNT")
    async def _on_online(event):
        bump("ONLINE_RANK_COUNT")
        try:
            online.append(event["data"]["data"].get("count"))
        except Exception:  # noqa: BLE001
            pass

    task = asyncio.create_task(dm.connect())
    error = None
    try:
        await asyncio.sleep(seconds)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await asyncio.wait_for(dm.disconnect(), timeout=10)
        except Exception:  # noqa: BLE001
            pass
        task.cancel()
        try:
            await asyncio.gather(task, return_exceptions=True)
        except Exception:  # noqa: BLE001
            pass

    payload = {
        "ok": True,
        "room_id": int(room_id),
        "listened_seconds": seconds,
        "elapsed": round(time.time() - started, 1),
        "error": error,
        "counts": counts,
        "events_seen": seen_events,
        "online": online[-5:],
        "danmaku": danmakus,
        "gifts": gifts,
        "superchats": superchats,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _send_guard(room_id: int) -> dict:
    state = _load_state()
    rec = (state.get("rooms") or {}).get(str(room_id)) or {}
    last = rec.get("lastSentAt")
    remaining = 0.0
    if last:
        try:
            last_ts = datetime.fromisoformat(last).timestamp()
            remaining = max(0.0, MIN_INTERVAL_SEC - (time.time() - last_ts))
        except Exception:  # noqa: BLE001
            remaining = 0.0
    return {"state": state, "record": rec, "remaining": remaining}


_throttle_hits: dict = {}


def _throttle_hit(room_id: int, window_sec: int = 300) -> int:
    """记一次「被节流」，返回该房间最近 window_sec 秒内被节流的次数。

    模型被节流后常常每 5 秒重试一次（2026-10-25 02:39 实况，连刷 10+ 次），
    既没用又脏日志；这里数到第 3 次就明说「别试了，这一轮别发」。
    """
    now = time.time()
    key = str(int(room_id))
    arr = [t for t in _throttle_hits.get(key, []) if now - t <= window_sec]
    arr.append(now)
    _throttle_hits[key] = arr
    return len(arr)


@mcp.tool()
async def bili_live_send(room_id: Union[int, str] = DEFAULT_ROOM, text: str = "", confirm: bool = False) -> str:
    """往直播间发一条弹幕。

    默认 confirm=false 只返回草稿和冷却时间，不发送；必须 confirm=true 才会真的发。
    节流：同一房间 60 秒内只能发 1 条。弹幕发出去删不掉，发送前务必核对内容。
    **每一次调用（含草稿、被拦、失败）都会写进 logs/danmaku.jsonl，用户能查到发了什么。**

    Args:
        room_id: 直播间号 / UP主昵称 / UID 都能用（必填）
        text: 弹幕内容，1-40 字，不要带链接
        confirm: 传 true 才真正发送
    """
    text = (text or "").strip()
    if not text:
        _log_danmaku("rejected_empty", room_id=str(room_id), text="")
        return json.dumps({"ok": False, "error": "text 不能为空"}, ensure_ascii=False)
    if len(text) > 40:
        _log_danmaku("rejected_too_long", room_id=str(room_id), text=text)
        return json.dumps({"ok": False, "error": f"弹幕太长（{len(text)} 字），上限 40"}, ensure_ascii=False)

    try:
        room_id = await _resolve_room(room_id)
    except Exception as exc:  # noqa: BLE001
        _log_danmaku("rejected_room", room_id=str(room_id), text=text, error=f"{type(exc).__name__}: {exc}")
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)

    denied = await _allow_check(room_id)
    if denied:
        _log_danmaku("rejected_not_allowed", room_id=room_id, text=text, error=denied)
        return json.dumps({"ok": False, "status": "not_allowed", "room_id": room_id, "error": denied}, ensure_ascii=False, indent=2)

    # 发之前先看清「这间房是谁的、开没开播」——发错房间是真事故
    anchor: dict = {}
    live_status = -1
    try:
        ri = await _room(room_id).get_room_info()
        room_info = ri.get("room_info") or {}
        live_status = int(room_info.get("live_status", -1))
        base = ((ri.get("anchor_info") or {}).get("base_info")) or {}
        anchor = {"uid": room_info.get("uid"), "uname": base.get("uname") or ""}
    except Exception:  # noqa: BLE001
        pass

    # 房间没开播就别白忙（发了既看不到也容易失败），直接说清楚
    if live_status == 0:
        _log_danmaku("offline", room_id=room_id, anchor=anchor, text=text)
        return json.dumps({
            "ok": False,
            "status": "offline",
            "room_id": room_id,
            "anchor": anchor,
            "error": f"房间 {room_id}（{anchor.get('uname') or anchor.get('uid') or '未知主播'}）现在没开播，弹幕发了也看不到；等她开播再说。",
        }, ensure_ascii=False, indent=2)

    info = _send_guard(int(room_id))
    remaining = info["remaining"]

    if not confirm:
        _log_danmaku("draft", room_id=room_id, anchor=anchor, text=text)
        return json.dumps({
            "ok": True,
            "status": "draft",
            "requires_confirm": True,
            "room_id": int(room_id),
            "anchor": anchor,
            "text": text,
            "throttle_seconds": MIN_INTERVAL_SEC,
            "cooldown_remaining": round(remaining, 1),
            "last_sent_at": info["record"].get("lastSentAt"),
            "note": "确认无误后带 confirm=true 再调用一次才会发出去。",
        }, ensure_ascii=False, indent=2)

    if remaining > 0:
        _log_danmaku("throttled", room_id=room_id, anchor=anchor, text=text, cooldown_remaining=round(remaining, 1))
        hits = _throttle_hit(room_id)
        if hits >= 3:
            return json.dumps({
                "ok": False,
                "status": "stop_retrying",
                "error": (
                    f"这个房间已经连着被节流 {hits} 次了（还是 {int(remaining) + 1} 秒冷却）。"
                    f"**别再重试**——同一房间 {MIN_INTERVAL_SEC} 秒只能一条，再试也发不出去只会刷日志。"
                    "这一轮就别发了：要么等下一段会话，要么换个话题、先听别人说。"
                ),
                "cooldown_remaining": round(remaining, 1),
                "last_sent_at": info["record"].get("lastSentAt"),
            }, ensure_ascii=False, indent=2)
        return json.dumps({
            "ok": False,
            "status": "throttled",
            "error": (
                f"节流中，还需等 {int(remaining) + 1} 秒才能再发（{MIN_INTERVAL_SEC} 秒 1 条）。"
                "不要连续重试——到点再说，或者这一轮就先不发了。"
            ),
            "cooldown_remaining": round(remaining, 1),
            "last_sent_at": info["record"].get("lastSentAt"),
        }, ensure_ascii=False, indent=2)

    try:
        r = await _room(room_id).send_danmaku(Danmaku(text))
    except Exception as exc:  # noqa: BLE001
        _log_danmaku("failed", room_id=room_id, anchor=anchor, text=text, error=f"{type(exc).__name__}: {exc}")
        return json.dumps({"ok": False, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)

    mode_info = ((r or {}).get("mode_info") or {})
    extra_raw = mode_info.get("extra") or "{}"
    try:
        extra = json.loads(extra_raw)
    except Exception:  # noqa: BLE001
        extra = {}

    state = info["state"]
    rec = state["rooms"].setdefault(str(room_id), {"count": 0})
    rec["lastSentAt"] = _now_iso()
    rec["count"] = int(rec.get("count") or 0) + 1
    rec["lastText"] = text
    rec["lastIdStr"] = extra.get("id_str")
    _save_state(state)
    _log_danmaku(
        "sent",
        room_id=room_id,
        anchor=anchor,
        text=text,
        id_str=extra.get("id_str"),
        total_sent_to_room=rec["count"],
    )

    return json.dumps({
        "ok": True,
        "status": "sent",
        "room_id": int(room_id),
        "anchor": anchor,
        "text": text,
        "id_str": extra.get("id_str"),
        "sent_from_me": extra.get("send_from_me"),
        "uid": ((mode_info.get("user") or {}).get("uid")),
        "uname": (((mode_info.get("user") or {}).get("base") or {}).get("name")),
        "sent_at": rec["lastSentAt"],
        "next_allowed_in_seconds": MIN_INTERVAL_SEC,
        "total_sent_to_room": rec["count"],
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def bili_live_log(num: int = 20, room_id: Union[int, str] = "") -> str:
    """读最近的弹幕发送日志（logs/danmaku.jsonl，只读）。

    记录里有：时间、结果（sent 真发 / draft 草稿 / offline 没开播被拦 / throttled 节流 /
    failed 失败 / rejected_* 参数被拒）、房间号、主播、正文、弹幕 id。

    Args:
        num: 返回最近几条，默认 20，最大 100
        room_id: 只看某个房间（房间号/昵称/UID 都行）；留空=全部
    """
    n = max(1, min(int(num or 20), 100))
    want = None
    if str(room_id or "").strip():
        try:
            want = await _resolve_room(room_id)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    if not LOG_FILE.exists():
        return json.dumps({"ok": True, "count": 0, "items": [], "path": str(LOG_FILE)}, ensure_ascii=False, indent=2)
    rows = []
    try:
        with LOG_FILE.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if want is not None and int(rec.get("room_id") or 0) != int(want):
                    continue
                rows.append(rec)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    return json.dumps({
        "ok": True,
        "path": str(LOG_FILE),
        "count": len(rows[-n:]),
        "items": rows[-n:],
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def bili_live_allow(room_id: Union[int, str] = "", action: str = "list", anchor: str = "", until_offline: bool = False, expires_in_hours: float = 0) -> str:
    """【只给用户 / DSH 主会话】管理「允许进入的直播间」名单。

    用户 2026-10-25 的规矩：进入直播间要管理员同意，只能进管理员选过的房间；
    进去之后发弹幕不用再给草稿，直接发。硬闸就是这份名单（live-allow.json）：
    不在名单里的房间，bili_live_info / bili_live_feed / bili_live_send 一律拒绝。
    ⚠️ 这个工具**不在 QQ 侧白名单**里——QQ 会话里的大肥鱼看不到也调不到，
    所以它没法给自己开新的直播间，只能由用户这边放行。

    Args:
        room_id: 直播间号 / UP主昵称 / uid（action=add 或 remove 时必填）
        action: list=看名单 / add=放行 / remove=收回
        anchor: 可选，备注主播名字（不填会自动查）
    """
    act = (action or "list").strip().lower()
    data = _load_allow()
    rooms = data.setdefault("rooms", {})

    if act == "list":
        return json.dumps({
            "ok": True,
            "enforce": bool(data.get("enforce", True)),
            "path": str(ALLOW_FILE),
            "count": len(rooms),
            "rooms": rooms,
        }, ensure_ascii=False, indent=2)

    if act not in ("add", "remove"):
        return json.dumps({"ok": False, "error": "action 只能是 list / add / remove"}, ensure_ascii=False)

    if not str(room_id or "").strip():
        return json.dumps({"ok": False, "error": "action=add/remove 时必须给 room_id"}, ensure_ascii=False)

    try:
        rid = await _resolve_room(room_id)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)

    if act == "remove":
        existed = rooms.pop(str(rid), None)
        _save_allow(data)
        return json.dumps({"ok": True, "action": "remove", "room_id": rid, "removed": bool(existed), "count": len(rooms)}, ensure_ascii=False, indent=2)

    name = (anchor or "").strip()
    if not name:
        try:
            name = ((await _room_info(rid)).get("anchor") or {}).get("uname") or ""
        except Exception:  # noqa: BLE001
            name = ""
    rooms[str(rid)] = {"anchor": name or None, "added_at": _now_iso()}
    if until_offline:
        rooms[str(rid)]["until_offline"] = True
    if expires_in_hours and float(expires_in_hours) > 0:
        rooms[str(rid)]["expires_at"] = (
            datetime.now().astimezone() + timedelta(hours=float(expires_in_hours))
        ).isoformat(timespec="seconds")
    _save_allow(data)
    return json.dumps({"ok": True, "action": "add", "room_id": rid, "anchor": name or None, "count": len(rooms), "rooms": rooms}, ensure_ascii=False, indent=2)


REQUEST_FILE = HOME / "live-requests.jsonl"


def _append_request(rid: int, anchor: str, reason: str, until_offline: bool) -> dict:
    """把「想进这个房间」记进本地待批清单，等**人**在设置页里点放行。

    独立插件没有 QQ 桥接那层「管理员本人刚说过话」的核对，所以这里只记录、不开门：
    放行这一步永远由人做（设置页的「放行」按钮），模型手上没有任何开关权限。
    """
    HOME.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "room_id": int(rid),
        "anchor": anchor or "",
        "reason": str(reason or "").strip()[:200],
        "until_offline": bool(until_offline),
        "status": "pending",
    }
    with open(REQUEST_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


@mcp.tool()
async def bili_live_request(room_id: Union[int, str] = "", reason: str = "", until_offline: bool = True) -> str:
    """**向人申请**进入一个还没放行的直播间（放行由人在设置页里点，我自己开不了门）。

    用法：先跟人说一句「要不要放行 <主播名> 的直播间（房间号 X）？」，得到同意后再调这个；
    它会写一条待批申请，人在 DSH 设置页的「B站直播」里点放行之后，bili_live_info /
     bili_live_feed / bili_live_send / bili_live_watch_start 才进得去。

    Args:
        room_id: 直播间号 / UP主昵称 / uid
        reason: 为什么想进（只记进日志，不参与校验）
        until_offline: True=只想待这一场（默认）；False=想常驻
    """
    if not str(room_id or "").strip():
        return json.dumps({"ok": False, "error": "缺少 room_id（房间号或主播名）"}, ensure_ascii=False)
    try:
        rid = await _resolve_room(room_id)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"找不到这个直播间：{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    anchor = ""
    try:
        anchor = ((await _room_info(rid)).get("anchor") or {}).get("uname") or ""
    except Exception:  # noqa: BLE001
        anchor = ""
    entry = _append_request(rid, anchor, reason, until_offline)
    return json.dumps(
        {
            "ok": True,
            "status": "pending_human_approval",
            "room_id": rid,
            "anchor": anchor or None,
            "request": entry,
            "message": (
                f"申请已记下（房间 {rid}／{anchor or '未知主播'}）。"
                "放行得人来点：DSH 设置页 →「B站直播」→ 待批申请 → 放行。放行前我进不去这个房间。"
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
async def bili_live_watching() -> str:
    """我此刻在看的直播间有哪些（放行名单 + 每个房间现在开没开播 + 我最近在里面说过什么）。

    用途：被问「你在看什么直播 / 现在在看谁」时**先调这个**再回答，别凭印象说「我没在看直播」。
    2026-10-25 的教训：用户在 QQ 私聊问「你现在在看谁的直播」，私聊那个会话手上没有任何
    状态，就答了「没有直播间」——可同时群会话正在小小只suki 的房间里连发弹幕。
    """
    allow = _load_allow()
    rooms = allow.get("rooms") or {}
    state = (_load_state().get("rooms") or {})
    out = []
    for key, entry in rooms.items():
        try:
            rid = int(key)
        except Exception:  # noqa: BLE001
            continue
        item = {
            "room_id": rid,
            "anchor": (entry or {}).get("anchor"),
            "until_offline": bool((entry or {}).get("until_offline")),
            "live": None,
            "title": None,
            "online": None,
        }
        try:
            info = await _room_info(rid)
            anchor = info.get("anchor") or {}
            item["anchor"] = anchor.get("uname") or item["anchor"]
            item["live"] = bool(info.get("live"))
            item["title"] = info.get("title")
            item["online"] = info.get("online")
        except Exception as exc:  # noqa: BLE001
            item["error"] = f"{type(exc).__name__}: {exc}"
        st = state.get(key) or {}
        if st:
            item["my_last_message"] = st.get("lastText")
            item["my_last_sent_at"] = st.get("lastSentAt")
            item["my_message_count"] = st.get("count")
        out.append(item)
    out.sort(key=lambda x: (not bool(x.get("live")), str(x.get("anchor") or "")))
    live_now = [x["anchor"] or str(x["room_id"]) for x in out if x.get("live")]
    if live_now:
        summary = "我此刻在看的直播：" + "、".join(live_now)
    elif out:
        summary = "名单里的直播间现在都没开播：" + "、".join((x["anchor"] or str(x["room_id"])) for x in out)
    else:
        summary = "我手上还没有放行过的直播间"
    return json.dumps({"ok": True, "summary": summary, "live_now": live_now, "watching": out}, ensure_ascii=False, indent=2)


WATCH_ROOT = HOME / "live-chat"
WORKER_SCRIPT = Path(os.environ.get("DSH_BILI_LIVE_WORKER") or (HOME / "live_chat_worker.py")).expanduser().resolve()


def _watch_dir(room_id: int) -> Path:
    return WATCH_ROOT / str(int(room_id))


def _pid_alive(pid: int) -> bool:
    """Windows 上判断 pid 是否还活着（OpenProcess + GetExitCodeProcess == STILL_ACTIVE）。"""
    if not pid:
        return False
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == STILL_ACTIVE
    except Exception:  # noqa: BLE001
        return False


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}


def _count_lines(path: Path) -> int:
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:  # noqa: BLE001
        return 0


def _tail_lines(path: Path, n: int = 3) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            lines = [line.rstrip("\n") for line in f if line.strip()]
        return lines[-n:]
    except Exception:  # noqa: BLE001
        return []


def _watch_snapshot(d: Path) -> dict:
    meta = _read_json(d / "watch.json")
    pid = int(meta.get("pid") or 0)
    hint = _read_json(d / "hint.json")
    closed = _tail_lines(d / "closed.jsonl", 1)
    snap = {
        "room_id": meta.get("room") or d.name,
        "anchor": meta.get("anchor"),
        "pid": pid,
        "alive": _pid_alive(pid),
        "started_at": meta.get("started_at"),
        "webhook": meta.get("webhook") or None,
        "danmaku_count": _count_lines(d / "danmaku.jsonl"),
        "sent_count": _count_lines(d / "sent.jsonl"),
        "wake_count": _count_lines(d / "wake.jsonl"),
        "last_hint": hint or None,
        "last_log": _tail_lines(d / "worker.log", 3),
        "closed": closed[0] if closed else None,
    }
    if snap["alive"] and closed:
        snap["note"] = "进程还在但 closed.jsonl 已有记录，可能正在退出"
    return snap


@mcp.tool()
async def bili_live_watch_start(room_id: Union[int, str] = DEFAULT_ROOM, webhook: str = "", wake_cooldown: float = 90.0, judge_sec: float = 60.0, max_wakes_per_hour: int = 30) -> str:
    """在某个放行过的直播间「挂着听」：开一个后台 worker 收弹幕，够得着时 POST 到你的 webhook。

    这和以前的 scheme 不一样：worker 只负责**收弹幕 + 判断值不值得叫你**，真正接话还是你
    （被叫醒之后自己看 bili_live_feed / hint 再决定发不发 bili_live_send）。
    worker 是 MCP 自己拉起的独立进程，不占 DSH 会话、不需要 pwsh / schedule / job 工具。

    判据（照 bili-live-hangout skill）：增量里 ≥1 条实质内容 **且** 最近一条弹幕 ≤3 分钟；
    有人点名叫你（设置页里填的名字）直接算够。两次叫醒之间至少隔 `wake_cooldown` 秒（默认 90；被 @ / 点名时不受此限）。
    房间必须在 `live-allow.json` 放行名单里（没有就先用 bili_live_request 申请，等人放行）。

    Args:
        room_id: 房间号 / UP主昵称 / uid
        webhook: 够得着时 POST 到这个地址（自己的 bot / 自动化都行）；留空=只收弹幕、只写 hint
        wake_cooldown: 两次叫醒最少间隔秒数（默认 90；被 @ / 点名时立刻叫）
        judge_sec: 多久看一次增量（默认 60）
        max_wakes_per_hour: 每小时最多叫醒几次（默认 30，防刷爆 token）
    """
    try:
        rid = await _resolve_room(room_id)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"找不到这个直播间：{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    denied = await _allow_check(rid)
    if denied:
        return json.dumps({"ok": False, "status": "not_allowed", "room_id": rid, "error": denied}, ensure_ascii=False)
    d = _watch_dir(rid)
    old = _read_json(d / "watch.json")
    if _pid_alive(int(old.get("pid") or 0)):
        return json.dumps({"ok": True, "status": "already_running", "room_id": rid, "worker": _watch_snapshot(d), "message": f"房间 {rid} 已经挂着在听了（pid {old.get('pid')}），不用再开一个"}, ensure_ascii=False, indent=2)
    try:
        info = await _room_info(rid)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"查不到房间信息：{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    anchor = info.get("anchor") or {}
    mid = int(anchor.get("uid") or 0)
    if not info.get("live"):
        return json.dumps({"ok": False, "status": "offline", "room_id": rid, "anchor": anchor.get("uname"), "error": f"房间 {rid}（{anchor.get('uname') or '未知主播'}）现在没开播，挂上去也没弹幕可听；等开播了再调一次。"}, ensure_ascii=False, indent=2)
    d.mkdir(parents=True, exist_ok=True)
    (d / "armed.json").write_text(
        json.dumps(
            {
                "allow": True,
                "room": rid,
                "mid": mid,
                "granted_by": "live-allow.json（已放行名单）",
                "granted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "scope": "由 bili_live_watch_start 拉起；bili_live_watch_stop 或直播结束即退出",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        (d / "stop.flag").unlink()
    except FileNotFoundError:
        pass
    cmd = [
        sys.executable,
        str(WORKER_SCRIPT),
        "--room",
        str(rid),
        "--mid",
        str(mid),
        "--dir",
        str(d),
        "--judge-sec",
        str(float(judge_sec)),
        "--wake-cooldown",
        str(float(wake_cooldown)),
        "--max-wakes-per-hour",
        str(int(max_wakes_per_hour)),
    ]
    if os.environ.get("DSH_BILI_LIVE_NAME"):
        cmd += ["--name", str(os.environ.get("DSH_BILI_LIVE_NAME")).strip()]
    if str(webhook or "").strip():
        cmd += ["--webhook", str(webhook).strip()]
    log_file = d / "worker.out.log"
    with open(log_file, "a", encoding="utf-8") as out:
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(HOME),
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=0x08000000,
            close_fds=True,
        )
    (d / "watch.json").write_text(
        json.dumps(
            {
                "pid": proc.pid,
                "room": rid,
                "mid": mid,
                "anchor": anchor.get("uname"),
                "webhook": str(webhook or "").strip(),
                "judge_sec": float(judge_sec),
                "wake_cooldown": float(wake_cooldown),
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "dir": str(d),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    await asyncio.sleep(3)
    snap = _watch_snapshot(d)
    ok = bool(snap.get("alive"))
    msg = (
        f"挂上了：房间 {rid}（{anchor.get('uname') or '未知主播'}），worker pid {proc.pid}，"
        + (f"够得着会 POST 到 {webhook}" if str(webhook or '').strip() else "只收弹幕，不叫醒任何人")
    )
    if not ok:
        msg = f"worker 起了但 3 秒内就退了，看 worker.log：{' | '.join(snap.get('last_log') or [])[:300]}"
    return json.dumps({"ok": ok, "status": "started" if ok else "exited", "message": msg, "worker": snap}, ensure_ascii=False, indent=2)


@mcp.tool()
async def bili_live_watch_status(room_id: Union[int, str] = "") -> str:
    """看挂机状态：哪个房间挂着、worker 还活着吗、收了多少条弹幕、发了几条、叫醒过几次、
    最近一次 hint（新弹幕样本）是什么。只读。

    Args:
        room_id: 留空=看全部；给了就只看那个房间
    """
    dirs = []
    if str(room_id or "").strip():
        try:
            dirs = [_watch_dir(await _resolve_room(room_id))]
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": f"找不到这个直播间：{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    else:
        try:
            dirs = sorted([p for p in WATCH_ROOT.iterdir() if p.is_dir() and p.name.isdigit()], key=lambda p: p.name)
        except FileNotFoundError:
            dirs = []
    snaps = [_watch_snapshot(d) for d in dirs]
    running = [s for s in snaps if s.get("alive")]
    if running:
        summary = "正在挂着听：" + "、".join(f"{s.get('anchor') or s['room_id']}（弹幕 {s['danmaku_count']} 条，发过 {s['sent_count']} 条，叫醒 {s['wake_count']} 次）" for s in running)
    elif snaps:
        summary = "没有在挂的直播间了（历史记录还在）"
    else:
        summary = "还没挂过直播间"
    return json.dumps({"ok": True, "summary": summary, "watching": snaps}, ensure_ascii=False, indent=2)


@mcp.tool()
async def bili_live_watch_stop(room_id: Union[int, str] = "") -> str:
    """让挂机 worker 收工：写 stop.flag，等它自己退；40 秒还不退就强杀。

    Args:
        room_id: 留空=停全部；给了就只停那个房间
    """
    dirs = []
    if str(room_id or "").strip():
        try:
            dirs = [_watch_dir(await _resolve_room(room_id))]
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": f"找不到这个直播间：{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    else:
        try:
            dirs = sorted([p for p in WATCH_ROOT.iterdir() if p.is_dir() and p.name.isdigit()], key=lambda p: p.name)
        except FileNotFoundError:
            dirs = []
    out = []
    for d in dirs:
        meta = _read_json(d / "watch.json")
        pid = int(meta.get("pid") or 0)
        if not _pid_alive(pid):
            out.append({"room_id": meta.get("room") or d.name, "changed": False, "message": "本来就没在跑"})
            continue
        try:
            (d / "stop.flag").write_text(datetime.now().astimezone().isoformat(timespec="seconds"), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            out.append({"room_id": d.name, "changed": False, "error": f"写 stop.flag 失败：{exc}"})
            continue
        waited = 0.0
        while waited < 40.0 and _pid_alive(pid):
            await asyncio.sleep(2.0)
            waited += 2.0
        killed = False
        if _pid_alive(pid):
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15)
                killed = True
            except Exception:  # noqa: BLE001
                pass
        out.append(
            {
                "room_id": meta.get("room") or d.name,
                "pid": pid,
                "changed": True,
                "graceful": not killed,
                "waited_sec": int(waited),
                "message": "已收工" if not killed else "40 秒没退，已强杀",
            }
        )
    return json.dumps({"ok": True, "stopped": out}, ensure_ascii=False, indent=2)


# ───────────────────────────── 听声音（v0.4.0）─────────────────────────────
#
# 只做耳朵，不做眼睛。链路：live_audio.py（独立 worker）→ ffmpeg 拉流 → 切窗 → 语音识别
# → live-chat/<房间>/audio.jsonl。这个 MCP 只负责 起 / 停 / 看状态 / 读转录，
# 不自己转写，免得一次识别把 MCP 挡住。

AUDIO_SCRIPT = Path(os.environ.get("DSH_BILI_LIVE_AUDIO") or (HOME / "live_audio.py")).expanduser()
AUDIO_PROBE = Path(os.environ.get("DSH_BILI_LIVE_AUDIO_PROBE") or (HOME / "asr_probe.py")).expanduser()


def _audio_snapshot(d: Path) -> dict:
    meta = _read_json(d / "audio.json")
    pid = int(meta.get("pid") or 0)
    tail = []
    for line in _tail_lines(d / "audio.jsonl", 5):
        try:
            tail.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    return {
        "room_id": meta.get("room") or d.name,
        "pid": pid,
        "alive": _pid_alive(pid),
        "state": meta.get("state") or ("unknown" if not meta else "stopped"),
        "engine": meta.get("engine") or "",
        "device": meta.get("device") or "",
        "requested_device": meta.get("requested_device") or "",
        "model": meta.get("model") or "",
        "window": meta.get("window"),
        "overlap": meta.get("overlap"),
        "started_at": meta.get("started_at"),
        "last_ok_at": meta.get("last_ok_at"),
        "last_error": meta.get("last_error") or "",
        "device_error": meta.get("device_error") or "",
        "fallback": bool(meta.get("fallback")),
        "segments": int(meta.get("segments") or 0),
        "chars": int(meta.get("chars") or 0),
        "kept_lines": int(meta.get("kept_lines") or 0),
        "recent": tail,
        "last_log": _tail_lines(d / "audio.log", 3),
    }


def _audio_rooms() -> list:
    if not WATCH_ROOT.exists():
        return []
    return [p for p in sorted(WATCH_ROOT.iterdir()) if p.is_dir() and (p / "audio.json").exists()]


@mcp.tool()
async def bili_live_audio_start(
    room_id: Union[int, str] = DEFAULT_ROOM,
    model: str = "small",
    device: str = "auto",
    window: float = 15.0,
    overlap: float = 2.0,
    keep: float = 300.0,
    engine: str = "auto",
) -> str:
    """给一个已放行的直播间装上「耳朵」：后台把直播声音转成带时间戳的文字。

    只转写，不发任何弹幕。文本写到 live-chat/<房间>/audio.jsonl，只保留最近 keep 秒。
    和 bili_live_watch_start（听弹幕）是两条独立的路，可以同时开。

    Args:
        room_id: 房间号 / UP主昵称 / UID
        model: 识别模型（tiny / base / small / medium），越大越准也越慢
        device: auto=先试 GPU、失败自动退 CPU；cpu=只用 CPU；cuda=强要 GPU（AMD 卡会失败，原始报错会返回）
        window: 每段音频秒数（默认 15，越小延迟越低、越吃 CPU）
        overlap: 相邻段重叠秒数（默认 2，避免一句话被切断）
        keep: 转录文本保留秒数（默认 300）
        engine: auto / faster / whispercpp（whisper.cpp 的 Vulkan 版能吃到 AMD 显卡）
    """
    from datetime import datetime

    try:
        rid = await _resolve_room(room_id)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": "找不到这个直播间：" + type(exc).__name__ + ": " + str(exc)}, ensure_ascii=False)
    denied = await _allow_check(rid)
    if denied:
        return json.dumps({"ok": False, "status": "not_allowed", "room_id": rid, "error": denied}, ensure_ascii=False)
    if not AUDIO_SCRIPT.is_file():
        return json.dumps({"ok": False, "error": "找不到转写脚本：" + str(AUDIO_SCRIPT)}, ensure_ascii=False)
    d = _watch_dir(rid)
    d.mkdir(parents=True, exist_ok=True)
    old = _read_json(d / "audio.json")
    if _pid_alive(int(old.get("pid") or 0)):
        return json.dumps(
            {"ok": True, "status": "already_running", "room_id": rid, "audio": _audio_snapshot(d),
             "message": "房间 " + str(rid) + " 已经在听了（pid " + str(old.get("pid")) + "）"},
            ensure_ascii=False, indent=2)
    try:
        (d / "stop.flag").unlink()
    except FileNotFoundError:
        pass
    # 转写引擎（faster-whisper）可能装在另一个 venv 里：DSH_BILI_LIVE_ASR_PYTHON 指过去即可
    audio_python = os.environ.get("DSH_BILI_LIVE_ASR_PYTHON") or sys.executable
    cmd = [
        audio_python,
        str(AUDIO_SCRIPT),
        "--room", str(rid),
        "--dir", str(d),
        "--window", str(float(window)),
        "--overlap", str(float(overlap)),
        "--keep", str(float(keep)),
        "--model", str(model or "small"),
        "--device", str(device or "auto"),
        "--engine", str(engine or "auto"),
    ]
    with open(d / "audio.out.log", "a", encoding="utf-8") as out:
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(HOME),
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=0x08000000,
            close_fds=True,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
    await asyncio.sleep(3)
    snap = _audio_snapshot(d)
    if not snap.get("pid"):
        (d / "audio.json").write_text(
            json.dumps({"pid": proc.pid, "room": rid, "state": "starting",
                        "started_at": datetime.now().astimezone().isoformat(timespec="seconds")},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        snap = _audio_snapshot(d)
    alive = bool(snap.get("alive"))
    msg = "房间 " + str(rid) + " 开始听声音：引擎 " + (snap.get("engine") or "准备中") + "，设备 " + (snap.get("device") or "?") + "，每 " + str(int(window)) + " 秒一段"
    if snap.get("device_error"):
        msg += "。GPU 没成功，已退回 CPU，原始报错：" + str(snap.get("device_error"))
    if not alive:
        msg = "worker 起了但马上就退了，看 audio.log：" + (" | ".join(snap.get("last_log") or []))[:400]
    return json.dumps({"ok": alive, "status": "started" if alive else "exited", "message": msg, "audio": snap}, ensure_ascii=False, indent=2)


@mcp.tool()
async def bili_live_audio_stop(room_id: Union[int, str] = "") -> str:
    """关掉某个房间（留空=全部房间）的语音转写。只停转写，不影响弹幕监听。"""
    if str(room_id or "").strip():
        try:
            rid = await _resolve_room(room_id)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": "找不到这个直播间：" + type(exc).__name__ + ": " + str(exc)}, ensure_ascii=False)
        dirs = [_watch_dir(rid)]
    else:
        dirs = _audio_rooms()
    out = []
    for d in dirs:
        meta = _read_json(d / "audio.json")
        pid = int(meta.get("pid") or 0)
        if not _pid_alive(pid):
            out.append({"room_id": meta.get("room") or d.name, "changed": False, "message": "本来就没在听"})
            continue
        try:
            (d / "stop.flag").write_text("stop", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            out.append({"room_id": d.name, "changed": False, "error": "写 stop.flag 失败：" + str(exc)})
            continue
        waited = 0.0
        while waited < 20.0 and _pid_alive(pid):
            await asyncio.sleep(1.0)
            waited += 1.0
        killed = False
        if _pid_alive(pid):
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15)
                killed = True
            except Exception:  # noqa: BLE001
                pass
        try:
            (d / "stop.flag").unlink()
        except Exception:  # noqa: BLE001
            pass
        out.append({"room_id": meta.get("room") or d.name, "pid": pid, "changed": True,
                    "graceful": not killed, "message": "已收工" if not killed else "20 秒没退，已强杀"})
    return json.dumps({"ok": True, "stopped": out}, ensure_ascii=False, indent=2)


@mcp.tool()
async def bili_live_audio_status(room_id: Union[int, str] = "") -> str:
    """看语音转写状态：谁在听、哪个引擎/设备、GPU 有没有失败、最近听到什么。只读。"""
    if str(room_id or "").strip():
        try:
            rid = await _resolve_room(room_id)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": "找不到这个直播间：" + type(exc).__name__ + ": " + str(exc)}, ensure_ascii=False)
        return json.dumps({"ok": True, "audio": _audio_snapshot(_watch_dir(rid))}, ensure_ascii=False, indent=2)
    return json.dumps({"ok": True, "items": [_audio_snapshot(p) for p in _audio_rooms()]}, ensure_ascii=False, indent=2)


@mcp.tool()
async def bili_live_transcript(room_id: Union[int, str] = DEFAULT_ROOM, last: int = 20) -> str:
    """读某个房间最近的语音转写（带时间戳）。只读；只保留最近 keep 秒（默认 5 分钟）。

    Args:
        room_id: 房间号 / UP主昵称 / UID
        last: 最近几条（默认 20，最多 200）
    """
    try:
        rid = await _resolve_room(room_id)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": "找不到这个直播间：" + type(exc).__name__ + ": " + str(exc)}, ensure_ascii=False)
    d = _watch_dir(rid)
    n = max(1, min(int(last or 20), 200))
    recs = []
    for line in _tail_lines(d / "audio.jsonl", n):
        try:
            recs.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    return json.dumps({"ok": True, "room_id": rid, "count": len(recs), "lines": recs}, ensure_ascii=False, indent=2)


@mcp.tool()
async def bili_live_asr_probe() -> str:
    """体检这台机器能不能用 GPU 转写：真去点一次 CUDA 拿原始报错，再看 whisper.cpp 与 DirectML。只读。"""
    if not AUDIO_PROBE.is_file():
        return json.dumps({"ok": False, "error": "找不到探测脚本：" + str(AUDIO_PROBE)}, ensure_ascii=False)
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, str(AUDIO_PROBE)],
            capture_output=True,
            timeout=180,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
        text = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            return json.dumps({"ok": False, "error": "探测脚本没输出 JSON", "stdout": text[-800:], "stderr": err[-800:]}, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": type(exc).__name__ + ": " + str(exc)}, ensure_ascii=False)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
