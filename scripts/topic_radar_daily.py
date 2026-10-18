#!/usr/bin/env python3
"""AI 选题雷达 · 每日信源抓取
抓取 4 个源，输出结构化 Markdown 到 stdout（供 cron agent 写入飞书 wiki）。
源：
  1. AIHOT（卡兹克）    aihot.virxact.com       —— Next.js RSC payload
  2. Follow Builders     github feed-x/blogs/podcasts.json
  3. best-skills         github data/<date>/rankings/*.csv
  4. GitHub Trending     github.com/trending    —— HTML

用法：python3 topic_radar_daily.py [--date YYYY-MM-DD]
"""
import json, re, sys, html, urllib.request, argparse, datetime, io

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def get(url, timeout=40, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", errors="ignore")


def sec(title, lines):
    return f"\n## {title}\n\n" + "\n".join(lines) + "\n"


# ---------------- 1. AIHOT ----------------
def _aihot_turbo_strings(page):
    """AIHOT 2026-10 起从 Next.js RSC 迁到 React Router：数据在
    window.__reactRouterContext.streamController.enqueue("<turbo-stream JSON>") 里，
    本节从该 payload 平铺收集字符串（旧 RSC 路径失效时的兜底）。"""
    payload = ""
    for raw in re.findall(r'streamController\.enqueue\(("(?:[^"\\]|\\.)*")\)', page, re.S):
        try:
            payload += json.loads(raw)
        except Exception:
            continue
    if not payload:
        return []
    try:
        data = json.loads(payload)
    except Exception:
        return []
    raw_strings = []

    def walk(o):
        if isinstance(o, str):
            raw_strings.append(o)
        elif isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(k)
                walk(v)

    walk(data)
    seen, res = set(), []
    for t in raw_strings:
        t = t.strip()
        if len(t) < 8 or len(t) > 140:
            continue
        if t.startswith(("http", "/", "_", "#")) or t.endswith((".js", ".css", ".json", ".png", ".svg")):
            continue
        if re.fullmatch(r"[A-Za-z0-9_\-\.]+", t):      # slug / key / 字段名
            continue
        if t in seen:
            continue
        seen.add(t)
        res.append(t)
    return res


def fetch_aihot():
    out = []
    try:
        s = get("https://aihot.virxact.com/")
        chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)', s)
        txt = ""
        uniq, mode = [], ""
        if chunks:
            mode = "Next.js RSC"
            for c in chunks:
                try:
                    txt += c.encode("utf-8").decode("unicode_escape")
                except Exception:
                    txt += c
            if "æ" in txt or "ç" in txt:          # UTF-8 被当 latin-1 的乱码修正
                txt = txt.encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
            # 提取标题类字段
            titles = []
            for pat in (r'"title":"([^"]{6,120})"', r'"name":"([^"]{6,120})"'):
                for m in re.findall(pat, txt):
                    m = m.strip()
                    if m and not m.startswith(("http", "/", "_")) and m not in titles:
                        titles.append(m)
            seen = set()
            for t in titles:
                k = t.lower()
                if k not in seen:
                    seen.add(k); uniq.append(t)
        else:
            mode = "React Router turbo-stream"
            uniq = _aihot_turbo_strings(s)
        out.append(f"- 抓取到候选条目 {len(uniq)} 条（页面解析：{mode}，payload {len(txt) or len(s)} 字符）")
        for t in uniq[:25]:
            out.append(f"- {t}")
        if not uniq:
            out.append("- ⚠️ 未解析出条目（页面结构可能变化）")
    except Exception as e:
        out.append(f"- ⚠️ 抓取失败：{type(e).__name__}: {e}")
    return out


# ---------------- 2. Follow Builders ----------------
FB = "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/"


def fetch_follow_builders():
    out = []
    # X
    try:
        d = json.loads(get(FB + "feed-x.json"))
        xs = d.get("x", [])
        out.append(f"- 生成时间 {d.get('generatedAt','?')} · 回看 {d.get('lookbackHours','?')}h · "
                   f"{len(xs)} 位 builder / {d.get('stats',{}).get('totalTweets','?')} 条推文")
        for b in xs[:10]:
            tw = b.get("tweets") or []
            if not tw:
                continue
            t0 = tw[0]
            txt = re.sub(r"\s+", " ", (t0.get("text") or ""))[:150]
            out.append(f"- **{b.get('name')}**（@{b.get('handle')}）：{txt}")
            if len(tw) > 1:
                out.append(f"  （另有 {len(tw)-1} 条）")
    except Exception as e:
        out.append(f"- ⚠️ feed-x 失败：{type(e).__name__}: {e}")
    # blogs / podcasts
    for key, name in (("blogs", "官方博客"), ("podcasts", "播客")):
        try:
            d = json.loads(get(FB + f"feed-{key}.json"))
            items = d.get(key, [])
            out.append(f"- {name}：{len(items)} 条")
            for it in items[:6]:
                ti = it.get("title") or it.get("name") or "?"
                out.append(f"  - {ti}")
        except Exception as e:
            out.append(f"- ⚠️ feed-{key} 失败：{type(e).__name__}: {e}")
    return out


# ---------------- 3. best-skills ----------------
BEST_SKILLS_RANKINGS = ["best-100", "trending-7d", "rising-stars", "most-active", "social-buzz", "top-installs", "top-repos"]


def fetch_best_skills(date=None):
    out = []
    base = "https://raw.githubusercontent.com/LinklyAI/best-skills/main/data/"
    # 找最新可用日期（往前回溯最多 7 天）
    d0 = datetime.date.fromisoformat(date) if date else datetime.date.today()
    used = None
    for back in range(0, 8):
        d = (d0 - datetime.timedelta(days=back)).isoformat()
        try:
            csv = get(base + d + "/rankings/trending-7d.csv")
            if csv.strip().startswith("rank,"):
                used = d
                break
        except Exception:
            continue
    if not used:
        return ["- ⚠️ 未找到近 7 日内可用数据（仓库结构可能变化）"]
    out.append(f"- 数据日期：{used}")
    for rk in ("best-100", "trending-7d", "rising-stars"):
        try:
            csv = get(base + used + f"/rankings/{rk}.csv")
            rows = [l for l in csv.splitlines() if l.strip()]
            if len(rows) < 2:
                continue
            hdr = rows[0].split(",")
            i_skill = hdr.index("skill") if "skill" in hdr else 1
            i_vendor = hdr.index("vendor") if "vendor" in hdr else None
            i_inst = hdr.index("installs_skillssh") if "installs_skillssh" in hdr else None
            i_zh = hdr.index("description_zh") if "description_zh" in hdr else None
            out.append(f"- **{rk}**（{len(rows)-1} 条）Top 8：")
            for r in rows[1:9]:
                c = r.split(",")
                nm = c[i_skill] if i_skill < len(c) else "?"
                ven = c[i_vendor] if (i_vendor is not None and i_vendor < len(c)) else ""
                inst = c[i_inst] if (i_inst is not None and i_inst < len(c)) else ""
                zh = c[i_zh] if (i_zh is not None and i_zh < len(c)) else ""
                extra = " / ".join(x for x in [ven, (inst + " installs" if inst else ""), zh[:40]] if x)
                out.append(f"  - {nm}" + (f" — {extra}" if extra else ""))
        except Exception as e:
            out.append(f"- ⚠️ {rk} 失败：{type(e).__name__}")
    return out


# ---------------- 4. GitHub Trending ----------------
def fetch_trending():
    out = []
    try:
        s = get("https://github.com/trending?since=daily")
        arts = re.findall(r'<article class="Box-row">(.*?)</article>', s, re.S)
        if not arts:
            arts = re.findall(r'<article[^>]*>(.*?)</article>', s, re.S)
        out.append(f"- 仓库数：{len(arts)}")
        for a in arts[:12]:
            m = re.search(r'<h2[^>]*>(.*?)</h2>', a, re.S)
            repo = "?"
            if m:
                h = re.search(r'href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', m.group(1))
                if h:
                    repo = h.group(1)
            d = re.search(r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', a, re.S)
            desc = re.sub(r"<[^>]+>", "", d.group(1)).strip() if d else ""
            desc = re.sub(r"\s+", " ", html.unescape(desc))
            st = re.search(r'([\d,]+)\s+stars\s+(today|this week|this month)', a)
            out.append(f"- **{repo}** — {desc[:110]}" + (f"（{st.group(1)} stars {st.group(2)}）" if st else ""))
    except Exception as e:
        out.append(f"- ⚠️ 抓取失败：{type(e).__name__}: {e}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--only", choices=["aihot", "builders", "best-skills", "trending"])
    a = ap.parse_args()
    today = a.date or datetime.date.today().isoformat()
    buf = io.StringIO()
    buf.write(f"# AI 选题雷达 · 每日信源快照\n\n采集日期：{today}\n")
    jobs = [("aihot", "1. AIHOT（数字生命卡兹克 · AI 热点监控）", fetch_aihot),
            ("builders", "2. Follow Builders（张咋啦 · AI builders 摘要）", fetch_follow_builders),
            ("best-skills", "3. best-skills（@BlueeonY · Agent Skill 榜单）", fetch_best_skills),
            ("trending", "4. GitHub Trending（每日榜）", fetch_trending)]
    for key, title, fn in jobs:
        if a.only and a.only != key:
            continue
        try:
            lines = fn()
        except Exception as e:
            lines = [f"- ⚠️ 模块异常：{type(e).__name__}: {e}"]
        buf.write(sec(title, lines))
    sys.stdout.write(buf.getvalue())


if __name__ == "__main__":
    main()
