#!/usr/bin/env python3
"""Offline, standard-library-only candidate highlight index; never captures or sends."""
import argparse, bisect, csv, hashlib, html, json, re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

BJT = timezone(timedelta(hours=8))
REACTIONS = {
    '喝彩/精彩操作': r'好杀|帅|牛|喝彩|强|漂亮|厉害|666|过了|过啦|打过|可以可以|哇|！！|!!',
    '搞笑/意外': r'哈哈|笑死|绷不住|乐了|好笑|草[！!]|卧槽|我去|什么鬼|逆天|离谱|抽象',
}
FAREWELL = re.compile(r'拜拜|再见|晚安|下播|下班|^8{2,}$|^bye|^baibai', re.I)
BOT = re.compile(r'大肥鱼|猫猫鱼|月野鱼鱼卷|机器人|api.?key|token|插件', re.I)
WELCOME = re.compile(r'^(晚上好|晚上豪|你好|早上好|早安|欢迎|来了|打卡)[呀啊喵哈！!~～。 ]*$')
EXPLICIT = re.compile(r'这段.{0,5}(必剪|剪辑|切片|录下来)|高光|记.{0,2}时间点|这.{0,2}录下来')


def norm(text):
    return re.sub(r'[\W_]+', '', text.casefold())


def dt(value, tz):
    value = str(value).replace('Z', '+00:00')
    t = datetime.fromisoformat(value)
    return t.replace(tzinfo=tz) if t.tzinfo is None else t


def stamp(t):
    return t.astimezone(BJT).isoformat(timespec='seconds')


def public(row):
    return {k: v for k, v in row.items() if k not in ('t', 'person')}


def ref(source, line):
    return {'path': source['path'], 'line': line}


def load_sources(args):
    if args.snapshot:
        return json.loads(Path(args.snapshot).read_text(encoding='utf-8-sig'))['sources']
    result = []
    for kind in ('danmaku', 'audio_log', 'audio_jsonl', 'closed', 'sent_audit', 'archive'):
        for filename in getattr(args, kind, None) or []:
            path = Path(filename)
            lines = path.read_text(encoding='utf-8-sig').splitlines()
            result.append({'kind': kind, 'path': str(path.resolve()), 'total_lines': len(lines),
                           'lines': [{'line': i, 'text': s} for i, s in enumerate(lines, 1)]})
    return result


def parse_sources(sources, tz, room):
    danmaku, audio, closed, sent = [], [], [], []
    errors, source_stats = [], []
    seen_dm, seen_audit = {}, set()
    for source in sources:
        kind = source['kind']
        content = '\n'.join(x['text'] for x in source['lines'])
        source_stats.append({'kind': kind, 'path': source['path'], 'lines': source['total_lines'],
                             'read_lines': len(source['lines']), 'sha256_text_lf': hashlib.sha256(content.encode()).hexdigest()})
        for entry in source['lines']:
            text, line = entry['text'], entry['line']
            try:
                if kind == 'audio_log':
                    m = re.match(r'^\[([^\]]+)\] 听到：(.*)$', text)
                    if m:
                        t = dt(m[1], tz)
                        audio.append({'t': t.timestamp(), 'time_beijing': stamp(t), 'text': m[2],
                                      'time_quality': '识别完成时间，非事件时间；文本最多80字符', 'refs': [ref(source, line)]})
                    continue
                if not text.strip():
                    continue
                row = json.loads(text)
                if str(row.get('room_id', row.get('room', room))) != room:
                    continue
                actual = kind
                if kind == 'archive':
                    typ = row.get('kind', row.get('type', ''))
                    actual = {'danmaku': 'danmaku', 'audio': 'audio_jsonl', 'transcript': 'audio_jsonl',
                              'closed': 'closed', 'sent': 'sent_audit'}.get(typ, '')
                    name = Path(source['path']).name
                    if not actual and name.startswith('audio-audio-'):
                        actual = 'audio_jsonl'
                    if not actual and name.startswith('danmaku-chat-'):
                        actual = 'danmaku'
                    if not actual:
                        continue  # coverage records are not utterances or audience reactions
                if actual == 'audio_jsonl' and row.get('status') == 'empty':
                    continue
                raw_time = row.get('event_ts') or row.get('event_time') or row.get('capture_start') or row.get('audio_start') or row.get('ts') or row.get('received_at')
                t = dt(raw_time, tz)
                base = {'t': t.timestamp(), 'time_beijing': stamp(t), 'refs': [ref(source, line)]}
                if actual == 'danmaku':
                    body = str(row.get('text', ''))
                    person = str(row.get('uid') or row.get('user_id') or row.get('uname') or 'unknown')
                    key = (round(t.timestamp(), 3), person, body)
                    if key in seen_dm:
                        seen_dm[key]['refs'].extend(base['refs'])
                        continue
                    base.update(text=body, person=person, history=row.get('src') == 'history',
                                time_quality='原始事件时间' if row.get('event_ts') or row.get('event_time') else '接收/回填本机时间；无时区按指定源时区推定')
                    seen_dm[key] = base
                    danmaku.append(base)
                elif actual == 'audio_jsonl':
                    base.update(text=str(row.get('text', '')),
                                time_quality='PCM接收估计时间，非媒体PTS' if row.get('capture_start') else ('音频采集时间' if row.get('audio_start') else '旧版近似窗口起点；end含识别耗时'))
                    audio.append(base)
                elif actual == 'closed':
                    base.update(reason=row.get('reason'), title=row.get('title', ''))
                    closed.append(base)
                elif actual == 'sent_audit':
                    key = (row.get('id_str') or '', raw_time, row.get('text', ''), row.get('result', ''))
                    if key not in seen_audit:
                        base.update(text=row.get('text', ''), result=row.get('result', 'sent'))
                        sent.append(base)
                        seen_audit.add(key)
            except (ValueError, TypeError, KeyError) as exc:
                errors.append({'ref': ref(source, line), 'error': str(exc)[:120]})
    audio.sort(key=lambda x: (x['t'], '完成' in x['time_quality']))
    merged = []
    for row in audio:
        body = norm(row['text'])
        match = None
        for previous in reversed(merged[-12:]):
            if row['t'] - previous['t'] > 25:
                continue
            old = norm(previous['text'])
            if body and old and (body == old or (min(len(body), len(old)) >= 8 and SequenceMatcher(None, body, old).ratio() >= .72)):
                match = previous
                break
        if match:
            match['refs'].extend(row['refs'])
        else:
            merged.append(row)
    return sorted(danmaku, key=lambda x: x['t']), merged, closed, sent, source_stats, errors, len(audio)


def eligible(row):
    text = row['text'].strip()
    return not row['history'] and not BOT.search(text) and not FAREWELL.search(text) and not WELCOME.search(text)


def detect(danmaku):
    events = [r for r in danmaku if eligible(r)]
    times = [r['t'] for r in events]
    windows = []
    for i, event in enumerate(events):
        end = event['t']
        left = bisect.bisect_left(times, end - 30)
        win = events[left:i+1]
        people = {r['person'] for r in win}
        # Fixed preceding 5 minutes: exclude current 30-second detection window.
        before = bisect.bisect_left(times, end - 330)
        baseline = (left - before) / 10
        ratio = len(win) / max(1, baseline)
        if len(win) >= 4 and len(people) >= 3 and ratio >= 2:
            windows.append({'start': win[0]['t'], 'end': end, 'kind': '弹幕热度上涨',
                            'score': len(win) + len(people)*2 + min(ratio, 8), 'ratio': ratio})
        left60 = bisect.bisect_left(times, end - 60)
        win60 = events[left60:i+1]
        for family, pattern in REACTIONS.items():
            hits = [r for r in win60 if re.search(pattern, r['text'])]
            unique = {r['person'] for r in hits}
            if len(unique) >= 3:
                windows.append({'start': hits[0]['t'], 'end': hits[-1]['t'], 'kind': family,
                                'score': len(hits)*2 + len(unique)*3, 'ratio': 0})
        if EXPLICIT.search(event['text']):
            windows.append({'start': end, 'end': end, 'kind': '观众明确高光提示', 'score': 12, 'ratio': 0})
    windows.sort(key=lambda x: x['start'])
    groups = []
    for w in windows:
        if groups and w['start'] <= groups[-1]['end'] + 60 and max(w['end'], groups[-1]['end']) - groups[-1]['start'] <= 240:
            g = groups[-1]
            g['end'] = max(g['end'], w['end'])
            g['kinds'].add(w['kind'])
            if w['score'] > g['score']:
                g.update(score=w['score'], peak=w['end'])
            g['ratio'] = max(g['ratio'], w['ratio'])
        else:
            groups.append({'start': w['start'], 'end': w['end'], 'peak': w['end'], 'score': w['score'],
                           'ratio': w['ratio'], 'kinds': {w['kind']}})
    # Merge overlapping expanded review ranges; repeated sliding windows never create duplicate clips.
    merged = []
    for g in groups:
        g.update(review_start=g['start']-45, review_end=g['end']+30)
        if merged and g['review_start'] <= merged[-1]['review_end'] and g['review_end']-merged[-1]['review_start'] <= 360:
            p = merged[-1]
            p['end'] = max(p['end'], g['end'])
            p['review_end'] = max(p['review_end'], g['review_end'])
            p['kinds'] |= g['kinds']
            p['ratio'] = max(p['ratio'], g['ratio'])
            if g['score'] > p['score']:
                p.update(score=g['score'], peak=g['peak'])
        else:
            merged.append(g)
    return merged


def make_candidates(groups, dm, audio, sent, top):
    candidates = []
    for g in groups:
        near = [r for r in dm if g['review_start'] <= r['t'] <= g['review_end']]
        core = [r for r in dm if g['start'] <= r['t'] <= g['end'] and eligible(r)]
        au = [r for r in audio if g['review_start']-25 <= r['t'] <= g['review_end']+25]
        me = [r for r in sent if g['review_start'] <= r['t'] <= g['review_end'] and r['result']=='sent']
        peak60 = [r for r in dm if g['peak']-60 <= r['t'] <= g['peak'] and not r['history']]
        evidence = sorted(core, key=lambda r: (not any(re.search(p, r['text']) for p in REACTIONS.values()), r['t']))[:16]
        evidence.sort(key=lambda r:r['t'])
        date = datetime.fromtimestamp(g['peak'], BJT).strftime('%Y-%m-%d')
        candidates.append({'date_beijing': date, 'title': '疑似'+' / '.join(sorted(g['kinds'])),
                           'peak_beijing': stamp(datetime.fromtimestamp(g['peak'], BJT)),
                           'review_start_beijing': stamp(datetime.fromtimestamp(g['review_start'], BJT)),
                           'review_end_beijing': stamp(datetime.fromtimestamp(g['review_end'], BJT)),
                           'score': round(g['score'], 2), 'baseline_ratio_30s': round(g['ratio'],2),
                           'core_eligible_messages': len(core), 'core_distinct_users': len({r['person'] for r in core}),
                           'peak_preceding_60s_realtime_messages': len(peak60),
                           'peak_preceding_60s_distinct_users': len({r['person'] for r in peak60}),
                           'history_context_count': sum(r['history'] for r in near),
                           'evidence_strength': '较强文字反应' if len({r['person'] for r in core})>=4 and len(core)>=6 else '一般文字线索',
                           'danmaku_evidence': [public(r) for r in evidence],
                           'transcript_context': [public(r) for r in au[:8]],
                           'transcript_rows_in_context': len(au),
                           'sent_context': [public(r) for r in me[:5]],
                           'recording_timecode': None,
                           'limitations': ['候选而非已确认画面；需人工回看', '没有录播起点，不生成录播时间码',
                                           '旧时间戳存在接收、回填或识别延迟',
                                           '转写为近似上下文，非逐字可靠证据' if au else '该范围没有可用转写记录，不代表主播没有说话']})
    daily = defaultdict(list)
    for c in candidates:
        daily[c['date_beijing']].append(c)
    selected = []
    for day in sorted(daily):
        selected.extend(sorted(daily[day], key=lambda c:c['score'], reverse=True)[:top])
    selected.sort(key=lambda c:c['peak_beijing'])
    for i,c in enumerate(selected,1):
        c['id'] = 'H%03d' % i
    return selected, {d:len(v) for d,v in daily.items()}


def csv_safe(value):
    text = '' if value is None else str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t','\r','\n')) else text


def write_reports(out, report):
    out.mkdir(parents=True, exist_ok=True)
    (out/'highlights.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    cols = ['id','date_beijing','title','peak_beijing','review_start_beijing','review_end_beijing','evidence_strength',
            'score','core_eligible_messages','core_distinct_users','transcript_rows_in_context','recording_timecode']
    with (out/'highlights.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        for c in report['candidates']:
            writer.writerow([csv_safe(c.get(k)) for k in cols])
    esc = lambda s: html.escape(str(s), quote=True)
    cards = []
    for c in report['candidates']:
        evidence = ''.join('<li>'+esc(x['time_beijing'][11:19])+' '+esc(x['text'])+' <small>'+esc('; '.join(r['path']+':'+str(r['line']) for r in x['refs']))+'</small></li>' for x in c['danmaku_evidence'])
        audio = ''.join('<li>'+esc(x['time_beijing'][11:19])+' '+esc(x['text'])+' <small>'+esc(x['time_quality'])+'；'+esc('; '.join(r['path']+':'+str(r['line']) for r in x['refs']))+'</small></li>' for x in c['transcript_context']) or '<li>该范围未找到转写，不补写、不猜测。</li>'
        cards.append('<article data-day="'+esc(c['date_beijing'])+'"><h2>'+esc(c['id']+' '+c['title'])+'</h2><p><b>北京时间 '+esc(c['review_start_beijing'])+' ～ '+esc(c['review_end_beijing'])+'</b></p><p>反应峰值 '+esc(c['peak_beijing'])+'；'+esc(c['evidence_strength'])+'；核心有效弹幕 '+str(c['core_eligible_messages'])+' 条 / '+str(c['core_distinct_users'])+' 人；规则排序分 '+str(c['score'])+'（不是概率）</p><details open><summary>弹幕依据（来源行号）</summary><ul>'+evidence+'</ul></details><details><summary>近似转写上下文</summary><ul>'+audio+'</ul></details><p class="warn">'+esc('；'.join(c['limitations']))+'</p></article>')
    options = ''.join('<option>'+esc(d)+'</option>' for d in sorted({c['date_beijing'] for c in report['candidates']}))
    sources = ''.join('<li>'+esc(x['kind']+'：'+x['path']+'，'+str(x['read_lines'])+'/'+str(x['lines'])+' 行')+'</li>' for x in report['sources'])
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(report.get('title') or '直播高光候选索引')+'</title><style>body{font:16px/1.65 system-ui,sans-serif;background:#f4f6fa;color:#172238;max-width:1080px;margin:32px auto;padding:0 20px}article,header{background:white;border:1px solid #dce2ed;border-radius:12px;padding:22px;margin:20px 0}h1{font-size:28px}h2{font-size:20px}small{display:block;color:#65718a;overflow-wrap:anywhere}li{margin:9px 0}.warn{color:#80551a}input,select{padding:10px;font:inherit;max-width:95%}[hidden]{display:none!important}@media print{nav{display:none}article{break-inside:avoid}}</style><header><h1>'+esc(report.get('title') or '直播高光候选索引')+'</h1><p>房间 '+esc(str(report.get('room_id') or ''))+' · '+str(len(report['candidates']))+' 个候选 · 全部展示北京时间 UTC+08:00</p><p>仅文本规则分析，未看画面。无时区弹幕按 '+esc(str(report.get('assumed_naive_source_timezone') or ''))+' 推定；此处按北京时间自然日分组，不声称识别了开播时间或完整场次。没有录播起点，不生成录播时间码。</p><p>历史回填不参与密度；欢迎、告别、机器人提及被排除。旧采集去重会低估重复喝彩；转写日志仅80字符、时间多为识别完成时间；相邻近重复已合并。未记录区间不等于冷场。</p><nav><label>日期 <select id="day"><option value="">全部</option>'+options+'</select></label> <label>查找 <input id="query" placeholder="输入弹幕或关键词"></label> <span id="count"></span></nav></header>'+''.join(cards)+'<footer><details><summary>输入来源与完整性</summary><ul>'+sources+'</ul><p>结构化统计、原始行号和来源内容哈希见同目录 JSON；输入原文见 source-snapshot.json。报告无外链请求、无网络发送。</p></details></footer><script>const day=document.getElementById("day"),q=document.getElementById("query"),cards=[...document.querySelectorAll("article")];function filter(){let n=0;for(const c of cards){c.hidden=!!((day.value&&c.dataset.day!==day.value)||(q.value&&!c.textContent.toLowerCase().includes(q.value.toLowerCase())));if(!c.hidden)n++}document.getElementById("count").textContent=n+" 个候选"}day.addEventListener("change",filter);q.addEventListener("input",filter);filter();</script></html>'
    (out/'highlights.html').write_text(page, encoding='utf-8')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot')
    for k in ('danmaku','audio_log','audio_jsonl','closed','sent_audit','archive'):
        p.add_argument('--'+k.replace('_','-'),action='append',dest=k)
    p.add_argument('--source-timezone',default='Asia/Shanghai')
    p.add_argument('--room',default='',help='直播间号（必填，报告里用来定位和展示）')
    p.add_argument('--title',default='直播高光候选索引',help='报告 HTML 的标题')
    p.add_argument('--top-per-day',type=int,default=5)
    p.add_argument('--out',default=str(Path(__file__).resolve().parent))
    args=p.parse_args()
    if args.top_per_day<1:
        p.error('--top-per-day must be positive')
    if not args.room:
        p.error('Provide --room（直播间号）')
    sources=load_sources(args)
    if not sources:
        p.error('Provide --snapshot or input paths')
    dm,audio,closed,sent,stats,errors,audio_raw=parse_sources(sources,ZoneInfo(args.source_timezone),args.room)
    groups=detect(dm)
    candidates,daily_detected=make_candidates(groups,dm,audio,sent,args.top_per_day)
    by_day=defaultdict(lambda:Counter())
    for r in dm:
        by_day[r['time_beijing'][:10]]['danmaku']+=1
        by_day[r['time_beijing'][:10]]['history']+=r['history']
    report={'schema_version':1,'title':args.title,'room_id':args.room,'generated_at':datetime.now(timezone.utc).isoformat(),
            'display_timezone':'Asia/Shanghai UTC+08:00','assumed_naive_source_timezone':args.source_timezone,
            'grouping':'北京时间自然日；不是完整直播场次','top_per_day':args.top_per_day,
            'method':{'burst':'30秒>=4条且>=3人；相对前5分钟30秒均值>=2倍，基线最小1',
                      'reaction':'60秒同反应类别>=3人','merge':'核心间隔<=60秒且跨度<=240秒；扩展后重叠且<=360秒继续合并',
                      'review_padding':'前45秒、后30秒；只供回看，不是剪辑边界',
                      'audio_dedup':'25秒内规范化相同，或至少8字且SequenceMatcher>=0.72；保留全部来源引用',
                      'ranking':'规则分仅用于同日排序，不是精彩概率；未调用模型或网络'},
            'counts':{'danmaku_unique':len(dm),'history':sum(r['history'] for r in dm),
                      'eligible_realtime':sum(eligible(r) for r in dm),'transcript_raw_records':audio_raw,
                      'transcript_after_near_duplicate_merge':len(audio),'sent_audit_records':len(sent),
                      'sent_success':sum(r['result']=='sent' for r in sent),'closed_records':len(closed),
                      'detected_merged_groups':len(groups),'selected_candidates':len(candidates)},
            'daily_source_counts':dict(by_day),'daily_detected_before_top':daily_detected,'sources':stats,
            'parse_errors':errors,'closed_events':[public(r) for r in closed],
            'sent_audit':[public(r) for r in sent],'candidates':candidates,
            'limitations':['只覆盖已采集文本，不等于完整直播；无来源时不补写事实',
                           '历史回填写接收时间，排除密度统计；本机无时区时间按参数推定',
                           '旧采集按昵称和全文长期去重可能低估重复弹幕',
                           'audio.log为最多80字符和完成时间；旧audio.jsonl为近似窗口时间',
                           '近重复转写合并是启发式，可能合并真实短时复述；原始引用仍保留',
                           '缺少视频和录播起点，不能确认画面或输出录播码']}
    write_reports(Path(args.out),report)
    print(json.dumps({'out':str(Path(args.out).resolve()),'counts':report['counts'],'parse_errors':len(errors),
                      'candidates':[{'id':c['id'],'time':c['peak_beijing'],'title':c['title'],'score':c['score']} for c in candidates]},ensure_ascii=False))


if __name__=='__main__':
    main()
