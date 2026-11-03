#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高光索引：把离线算好的高光报告洗成「可以对外说」的脱敏索引。

为什么单独一个文件：
  · 原始报告（highlights.json）里带着本机路径和观众原文，不能整份丢给 QQ；
  · QQ 那边只该看到「哪一天、几点、为什么算高光、两三句证据」。

纯标准库、不联网、不启动任何采集。两个用法：
  · 命令行建索引：python highlights_index.py --build <报告.json>
  · 命令行试查询：python highlights_index.py --query --room 5370564 --date 10-30

落盘位置：$DSH_BILI_LIVE_HOME/highlights/<房间号>/index.json
（没设这个环境变量就落在脚本同目录的 highlights/ 下，和 live_mcp.py 的 HOME 一致）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

INDEX_VERSION = 1


def _home() -> Path:
    env = os.environ.get("DSH_BILI_LIVE_HOME")
    base = Path(env).expanduser() if env else Path(__file__).resolve().parent
    return base.resolve()


def index_path(room_id) -> Path:
    return _home() / "highlights" / str(room_id) / "index.json"


# ── 小工具 ──────────────────────────────────────────────────────────────

def _iso_dt(s: str):
    s = str(s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        return None


def _hhmm(s: str) -> str:
    d = _iso_dt(s)
    return d.strftime("%H:%M") if d else ""


def _hhmmss(s: str) -> str:
    d = _iso_dt(s)
    return d.strftime("%H:%M:%S") if d else ""


def _date_of(s: str) -> str:
    d = _iso_dt(s)
    return d.strftime("%Y-%m-%d") if d else ""


_PATHY = re.compile(r"([A-Za-z]:[\\/]|\\\\|/home/|/Users/|\.dsh\b|\.jsonl\b)")


def _clean_text(s, limit: int = 40) -> str:
    """弹幕原文：去换行、截断；带路径的一律不往外给。"""
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    if not t or _PATHY.search(t):
        return ""
    return t[:limit]


def _clean_note(s, limit: int = 60) -> str:
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    if not t or _PATHY.search(t):
        return ""
    return t[:limit]


def _num(v, default=None):
    try:
        f = float(v)
        return int(f) if f == int(f) else round(f, 2)
    except Exception:  # noqa: BLE001
        return default


def _why(c: dict) -> str:
    """一句话讲清「凭什么说这里是高光」。缺字段就少说，不编。"""
    msgs = _num(c.get("core_eligible_messages"))
    users = _num(c.get("core_distinct_users"))
    ratio = _num(c.get("baseline_ratio_30s"))
    bits = []
    if msgs is not None:
        bits.append("30 秒里 %d 条弹幕" % msgs)
    if users is not None:
        bits.append("%d 个不同的人在说" % users)
    if ratio is not None and ratio >= 1.5:
        bits.append("密度是平时的 %.1f 倍" % float(ratio))
    if not bits:
        return "弹幕密度高于同场平均"
    return "，".join(bits)


def _one(c: dict, idx: int) -> dict:
    peak = c.get("peak_beijing") or ""
    start = c.get("review_start_beijing") or ""
    end = c.get("review_end_beijing") or ""
    ev = []
    for e in (c.get("danmaku_evidence") or []):
        if not isinstance(e, dict):
            continue
        t = _clean_text(e.get("text"))
        if not t:
            continue
        if t in [x["text"] for x in ev]:
            continue
        ev.append({"time": _hhmmss(e.get("time_beijing") or ""), "text": t,
                   "backfill": bool(e.get("history"))})
    # 优先用实时（非回填）的，回填的放后面
    ev.sort(key=lambda x: x["backfill"])
    ev = ev[:4]
    notes = [n for n in (_clean_note(x) for x in (c.get("limitations") or [])) if n][:2]
    return {
        "id": str(c.get("id") or ("H%03d" % (idx + 1))),
        "date": _date_of(peak) or str(c.get("date_beijing") or ""),
        "peak": _hhmmss(peak),
        "window": ("%s–%s" % (_hhmm(start), _hhmm(end))) if (start and end) else "",
        "strength": str(c.get("evidence_strength") or ""),
        "why": _why(c),
        "score": _num(c.get("score")),
        "evidence": ev,
        "notes": notes,
    }


# ── 建索引 ──────────────────────────────────────────────────────────────

def build(report_path, room_id=None) -> dict:
    """读一份离线高光报告，写出脱敏索引。只读报告，不改它。"""
    src = Path(report_path).expanduser().resolve()
    data = json.loads(src.read_text(encoding="utf-8-sig"))
    rid = str(room_id or data.get("room_id") or "").strip()
    if not rid:
        raise RuntimeError("报告里没有 room_id，也没手动指定")

    cands = [c for c in (data.get("candidates") or []) if isinstance(c, dict)]
    items = [_one(c, i) for i, c in enumerate(cands)]
    items = [x for x in items if x["date"] and x["peak"]]
    items.sort(key=lambda x: (x["date"], x["peak"]))

    dates = sorted({x["date"] for x in items})
    payload = {
        "index_version": INDEX_VERSION,
        "room_id": rid,
        "generated_at": str(data.get("generated_at") or ""),
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_digest": "%s|%d" % (src.name, len(items)),
        "coverage": {"from": dates[0] if dates else "", "to": dates[-1] if dates else "",
                     "dates": dates},
        "counts": {"candidates": len(items)},
        "candidates": items,
    }
    dst = index_path(rid)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(dst)
    return {"ok": True, "index": str(dst), "room_id": rid, "candidates": len(items),
            "dates": dates}


# ── 查询 ────────────────────────────────────────────────────────────────

def _norm_date(s: str, known_dates) -> str:
    """把 2026-10-30 / 10-30 / 10月30日 都归一成 2026-10-30。"""
    t = str(s or "").strip()
    if not t:
        return ""
    m = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$", t)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{1,2})[-/.月](\d{1,2})日?$", t)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        for d in sorted(known_dates, reverse=True):
            if d.endswith("-%02d-%02d" % (mm, dd)):
                return d
        return "????-%02d-%02d" % (mm, dd)  # 让上层报「那天没有」
    return t


def _norm_id(s: str) -> str:
    t = str(s or "").strip().upper()
    if not t:
        return ""
    m = re.match(r"^H?0*(\d+)$", t)
    return "H%03d" % int(m.group(1)) if m else t


def query(room_id, date="", candidate_id="", keyword="", limit=5) -> dict:
    """在脱敏索引里查高光。索引不存在就直说，不现算、不联网。"""
    rid = str(room_id).strip()
    p = index_path(rid)
    if not p.is_file():
        return {"ok": False, "error": "这个房间还没有高光索引（要先生成一份高光报告）",
                "room_id": rid}
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    items = list(data.get("candidates") or [])
    known = (data.get("coverage") or {}).get("dates") or []

    want_id = _norm_id(candidate_id)
    want_date = _norm_date(date, known)
    want_kw = str(keyword or "").strip()

    hits = items
    if want_id:
        hits = [x for x in hits if _norm_id(x.get("id")) == want_id]
    if want_date:
        hits = [x for x in hits if x.get("date") == want_date]
    if want_kw:
        def _has(x):
            blob = " ".join([x.get("why", ""), x.get("strength", "")] +
                            [e["text"] for e in x.get("evidence") or []])
            return want_kw in blob
        hits = [x for x in hits if _has(x)]

    hits.sort(key=lambda x: (x.get("date", ""), x.get("peak", "")), reverse=True)
    n = max(1, min(int(limit or 5), 10))
    shown = hits[:n]
    payload = {
        "ok": True,
        "room_id": rid,
        "report_generated_at": data.get("generated_at", ""),
        "coverage": {"from": (data.get("coverage") or {}).get("from", ""),
                     "to": (data.get("coverage") or {}).get("to", ""),
                     "dates": known},
        "matched": len(hits),
        "shown": len(shown),
        "candidates": shown,
    }
    if want_id and not hits:
        payload["hint"] = "没有编号 %s 这一段；可用的是 %s" % (
            want_id, "、".join(x.get("id", "") for x in items[:12]))
    elif want_date and not hits:
        payload["hint"] = "索引里没有 %s 这一天的记录；有的日期是 %s" % (
            want_date, "、".join(known))
    elif not hits and want_kw:
        payload["hint"] = "没找到含「%s」的段落，可以不传 keyword 看全部" % want_kw
    payload["text"] = render_text(payload)
    return payload


def render_text(payload: dict) -> str:
    """给一段能直接读的中文，省得调用方自己拼。"""
    if not payload.get("ok"):
        return str(payload.get("error") or "查询失败")
    rows = payload.get("candidates") or []
    cov = payload.get("coverage") or {}
    head = "高光候选（数据截止 %s，北京时间）" % (cov.get("to") or "未知")
    if not rows:
        return head + "\n" + str(payload.get("hint") or "这一天没有记录")
    lines = [head]
    for x in rows:
        w = ("（回看 %s）" % x["window"]) if x.get("window") else ""
        lines.append("· %s %s %s%s" % (x["date"], x["peak"], x.get("why", ""), w))
        ev = "｜".join("%s %s" % (e["time"], e["text"]) for e in (x.get("evidence") or [])[:3])
        if ev:
            lines.append("  现场：" + ev)
    if payload.get("matched", 0) > payload.get("shown", 0):
        lines.append("（还有 %d 段没列出来）" % (payload["matched"] - payload["shown"]))
    lines.append("注：这些是按弹幕推出来的候选时间点，不是画面确认；没有录播起点，只给北京时间。")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="高光索引：建索引 / 试查询")
    ap.add_argument("--build", metavar="REPORT_JSON", help="离线高光报告路径")
    ap.add_argument("--room", default="", help="房间号（建索引时可选，默认用报告里的）")
    ap.add_argument("--query", action="store_true", help="查询模式")
    ap.add_argument("--date", default="", help="日期：2026-10-30 / 10-30 / 10月30日")
    ap.add_argument("--id", dest="candidate_id", default="", help="候选编号，如 H012")
    ap.add_argument("--keyword", default="", help="关键词（在理由和证据里找）")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是中文")
    args = ap.parse_args(argv)

    if args.build:
        out = build(args.build, args.room or None)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out.get("ok") else 1
    if args.query:
        if not args.room:
            print("查询要带 --room", file=sys.stderr)
            return 2
        out = query(args.room, args.date, args.candidate_id, args.keyword, args.limit)
        print(json.dumps(out, ensure_ascii=False, indent=1) if args.json else out.get("text", ""))
        return 0 if out.get("ok") else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
