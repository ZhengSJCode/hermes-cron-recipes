#!/usr/bin/env python3
"""
GitHub Trending 采集脚本（周报/月报通用，v2）
- 抓取 https://github.com/trending?since=weekly|monthly
- 解析仓库列表（repo/desc/lang/week_stars）
- 页面 HTML 抓官方 topics；API 补 license / 总 star（失败留空）
- 本地模型（127.0.0.1 的 OpenAI 兼容端点）批量生成三列：
    给谁用 / 同类仓库对比 / 中文用途
- 生成表格：| # | 应用 | 给谁用 | 同类仓库对比 | 用途 | 标签 |
- 导入 docx 并 move 到飞书 wiki 节点下（新建子文档）；或 --doc 就地覆盖已有文档
- stdout 输出中文汇报（cron no_agent 模式直接投递）

用法:
  python3 github_trending_collect.py --period weekly|monthly [--limit N]
  python3 github_trending_collect.py --period weekly --doc "<wiki/docx URL 或 token>"   # 覆盖已有文档
  python3 github_trending_collect.py ... --no-llm                                       # 跳过本地模型
"""
import subprocess, re, html, json, sys, os, datetime, time, urllib.request

PERIOD = 'weekly'
LIMIT = 15
DOC = None          # 指定则就地覆盖该文档，不再新建
USE_LLM = True
if '--period' in sys.argv:
    PERIOD = sys.argv[sys.argv.index('--period') + 1]
if '--limit' in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index('--limit') + 1])
if '--doc' in sys.argv:
    DOC = sys.argv[sys.argv.index('--doc') + 1]
if '--no-llm' in sys.argv:
    USE_LLM = False

SPACE_ID = os.environ.get('FEISHU_SPACE_ID', '')
PARENT_NODE = os.environ.get('FEISHU_PARENT_TOKEN', '')
WORKDIR = os.environ.get('HERMES_WORKDIR') or os.path.expanduser('~')
CONFIG_PATH = os.environ.get('HERMES_CONFIG') or os.path.expanduser('~/.hermes/config.yaml')
LLM_URL = (os.environ.get('LLM_BASE_URL') or 'http://127.0.0.1:8317/v1').rstrip('/') + '/chat/completions'
LLM_MODEL = os.environ.get('LLM_MODEL') or 'gpt-6-astra'
LLM_MAX_ATTEMPTS = int(os.environ.get('LLM_MAX_ATTEMPTS', '4'))
LLM_BACKOFF = float(os.environ.get('LLM_BACKOFF_SECONDS', '3'))
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode('utf-8', 'ignore')


def sh(cmd, cwd=WORKDIR, stdin=None):
    env = dict(os.environ)
    env['LARKSUITE_CLI_NO_UPDATE_NOTIFIER'] = '1'
    env['LARKSUITE_CLI_NO_SKILLS_NOTIFIER'] = '1'
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd,
                       env=env, timeout=240, input=stdin)
    return p.stdout, p.returncode


def extract_json(out):
    """从混合输出中提取最后一个完整 JSON 对象"""
    last = out.rfind('{')
    while last != -1:
        try:
            return json.loads(out[last:])
        except Exception:
            last = out.rfind('{', 0, last)
    return None


def extract_json_array(txt):
    """从模型输出中提取 JSON 数组（容忍代码块围栏/前后废话）"""
    if not txt:
        return None
    t = re.sub(r'```[a-zA-Z]*', '', txt)
    a, b = t.find('['), t.rfind(']')
    if a == -1 or b == -1 or b <= a:
        return None
    try:
        return json.loads(t[a:b + 1])
    except Exception:
        return None


def parse_trending(content):
    articles = re.findall(r'<article[^>]*>(.*?)</article>', content, re.S)
    results = []
    for art in articles:
        h2 = re.search(r'<h2[^>]*>(.*?)</h2>', art, re.S)
        if not h2:
            continue
        m = re.search(r'href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', h2.group(1))
        if not m:
            continue
        repo = m.group(1)
        p = re.search(r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', art, re.S)
        if not p:
            p = re.search(r'<p[^>]*>(.*?)</p>', art, re.S)
        desc = html.unescape(re.sub(r'<[^>]+>', ' ', p.group(1))).strip() if p else ''
        lang = re.search(r'itemprop="programmingLanguage">([^<]+)<', art)
        lang = lang.group(1).strip() if lang else ''
        m_week = re.search(r'([\d,]+)\s+stars?\s+this\s+week', art)
        week_stars = m_week.group(1) if m_week else ''
        results.append({'repo': repo, 'desc': desc, 'lang': lang, 'week_stars': week_stars})
    return results


def enrich(repo):
    """topics 用页面 HTML 抓（免 API 限速）；license/总star 用 API 补（失败留空）"""
    meta = {'topics': [], 'license': '', 'total_stars': 0}
    try:
        page = fetch(f'https://github.com/{repo}')
        m = re.search(r'"topics":\[(.*?)\]', page)
        if m:
            meta['topics'] = re.findall(r'"name":"([^"]+)"', m.group(1))
    except Exception:
        pass
    try:
        d = json.loads(fetch(f'https://api.github.com/repos/{repo}', {'Accept': 'application/vnd.github+json'}))
        meta['license'] = (d.get('license') or {}).get('spdx_id') or ''
        meta['total_stars'] = d.get('stargazers_count') or 0
    except Exception:
        pass
    return meta


def _llm_key():
    try:
        cfg = open(CONFIG_PATH, encoding='utf-8').read()
        m = re.search(r'^\s*api_key:\s*(\S+)\s*$', cfg, re.M)
        return m.group(1) if m else None
    except Exception:
        return None


PROMPT_TMPL = """你是 GitHub 榜单编辑。下面是 GitHub Trending 榜单的仓库列表（JSON）。请为每个仓库生成三个字段，**只输出 JSON 数组**，不要 markdown 代码块、不要任何解释。

字段要求：
- audience（给谁用）：结合仓库用途推断目标用户，中文，≤18 字，要具体（例：Claude Code 用户 / Rust 后端开发者 / 独立开发者）。
- compare（同类仓库对比）：**只在本列表内**找同类仓库做对比；若列表内没有同类，写「本周榜单内无同类」；再用一句话说明它相对同类的差异或优势。中文，≤45 字。**严禁编造本列表之外的仓库。**
- purpose_cn（用途）：把它的 description 忠实翻译/概括成中文，≤40 字，不夸大、不加戏。

输入列表：
{payload}

输出格式（严格）：
[{{"repo":"owner/name","audience":"...","compare":"...","purpose_cn":"..."}}, ...]
要求：数组长度与输入一致，repo 原样返回。"""


def llm_enrich(items):
    """批量调用本地模型，返回 {repo: {audience, compare, purpose_cn}}；失败返回 {}"""
    if not USE_LLM or not items:
        return {}
    key = _llm_key()
    if not key:
        print('[llm] 未读到本地模型 api_key，跳过三列生成')
        return {}
    payload = json.dumps(
        [{'repo': it['repo'], 'lang': it.get('lang', ''), 'description': it.get('desc', '')} for it in items],
        ensure_ascii=False)
    body = json.dumps({
        'model': LLM_MODEL,
        'temperature': 0,
        'messages': [
            {'role': 'system', 'content': '你是严谨的 GitHub 榜单编辑，只输出合法 JSON。'},
            {'role': 'user', 'content': PROMPT_TMPL.format(payload=payload)},
        ],
    }).encode('utf-8')
    resp = None
    for _attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(LLM_URL, data=body, headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read().decode('utf-8'))
            break
        except Exception as _e:
            _code = getattr(_e, 'code', None)
            _retryable = (_code in (408, 429, 500, 502, 503, 504)
                          or isinstance(_e, (urllib.error.URLError, TimeoutError, ConnectionError)))
            if _attempt < LLM_MAX_ATTEMPTS and _retryable:
                _wait = LLM_BACKOFF * (2 ** (_attempt - 1))
                print('[llm] 第 %d 次调用失败（%s: %s），%ds 后重试'
                      % (_attempt, type(_e).__name__, str(_e)[:60], _wait))
                time.sleep(_wait)
                continue
            print('[llm] 调用失败（%s），三列留空：%s' % (type(_e).__name__, str(_e)[:120]))
            return {}
    content = resp['choices'][0]['message']['content']
    arr = extract_json_array(content)
    if not arr:
        print('[llm] 返回无法解析为 JSON，三列留空')
        return {}
    out = {}
    for row in arr:
        if isinstance(row, dict) and row.get('repo'):
            out[row['repo']] = {
                'audience': (row.get('audience') or '').strip(),
                'compare': (row.get('compare') or '').strip(),
                'purpose_cn': (row.get('purpose_cn') or '').strip(),
            }
    print(f'[llm] 三列生成完成：{len(out)}/{len(items)}')
    return out


def cell(s):
    """表格单元格：去掉换行、转义竖线"""
    return (s or '').replace('\n', ' ').replace('|', '\\|').strip()


def docx_id_from(j):
    """从 overwrite/import 响应里取 docx document_id"""
    d = (j or {}).get('data', {}).get('document', {}) or {}
    if d.get('document_id'):
        return d['document_id']
    m = re.search(r'/docx/([A-Za-z0-9]+)', d.get('url', '') or '')
    return m.group(1) if m else None


def set_doc_title(doc_token, title):
    """把文档标题（根 page block，block_id == document_id）改为 title；失败不阻塞"""
    if not doc_token:
        return False
    body = json.dumps({'update_text_elements': {'elements': [{'text_run': {'content': title}}]}},
                      ensure_ascii=False)
    out, rc = sh(f'lark-cli api PATCH /open-apis/docx/v1/documents/{doc_token}/blocks/{doc_token} '
                 f"--data '{body}' --as user --format json 2>&1")
    if (extract_json(out) or {}).get('ok'):
        return True
    print(f'[title] 设置文档标题失败（忽略，不影响正文）：{out[:160]}')
    return False


def build_md(items, period_label, date_str):
    label = '月榜' if period_label == 'monthly' else '周榜'
    lines = [f'# GitHub 热门{label}（{date_str} 采集）', '',
             f'> 数据来源：https://github.com/trending?since={period_label} · 采集时间 {date_str}',
             f'> 列说明：应用=仓库名(链接)+周期新增 Star；给谁用/同类仓库对比/用途 由本机模型基于官方信息生成；标签=官方 topics',
             '']
    lines.append('| # | 应用 | 给谁用 | 同类仓库对比 | 用途 | 标签 |')
    lines.append('|---|------|------|------|------|------|')
    for i, it in enumerate(items, 1):
        repo = it['repo']
        name = f"[{repo.split('/')[-1]}](https://github.com/{repo})<br/>{cell(it.get('week_stars') or '?')} Star"
        audience = cell(it.get('audience'))
        compare = cell(it.get('compare'))
        purpose = cell(it.get('purpose_cn')) or cell(it.get('desc')) or '（无描述）'
        tags = ', '.join(f'`{t}`' for t in it['topics']) if it.get('topics') else '官方未设置 topics'
        lines.append(f'| {i} | {name} | {audience} | {compare} | {purpose} | {tags} |')
    lines.append('')
    lines.append('## 备注')
    lines.append('')
    lines.append('- 本表为自动采集（脚本 github_trending_collect.py）：Star 为 GitHub Trending 周期新增口径；'
                 '给谁用/同类仓库对比/用途 由本地模型生成（可能有个体偏差，人工可复核）；标签为仓库官方 topics')
    return '\n'.join(lines)


def main():
    date_str = datetime.date.today().strftime('%Y-%m-%d')
    label = '月榜' if PERIOD == 'monthly' else '周榜'
    print(f'[1/5] 抓取 GitHub Trending ({PERIOD})...')
    content = fetch(f'https://github.com/trending?since={PERIOD}')
    items = parse_trending(content)
    if not items:
        print('抓取失败：未解析到仓库。请稍后重试或检查网络。')
        sys.exit(1)
    items = items[:LIMIT]
    print(f'[2/5] 解析到 {len(items)} 个仓库，补充元数据...')
    for it in items:
        it.update(enrich(it['repo']))
        time.sleep(0.2)
    print('[3/5] 本地模型生成 给谁用/同类仓库对比/中文用途...')
    llm = llm_enrich(items)
    for it in items:
        it.update(llm.get(it['repo'], {}))
    print('[4/5] 生成 Markdown 并写入飞书...')
    md = build_md(items, PERIOD, date_str)
    md_path = os.path.join(WORKDIR, f'github-trending-{PERIOD}-{date_str}.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md)
    title = f'GitHub 热门{label}-{date_str}'
    node = None
    if DOC:
        # 就地覆盖已有文档（保留原 wiki 节点/链接）
        out, rc = sh(f'lark-cli docs +update --doc "{DOC}" --command overwrite --doc-format markdown '
                     f'--content - --as user --format json 2>&1', stdin=md)
        j = extract_json(out)
        if not (j and j.get('ok')):
            print(f'覆盖文档失败：{out[:300]}')
            sys.exit(1)
        node = DOC
        # overwrite 会把首个 H1 设成标题 → 统一规范回标准命名
        set_doc_title(docx_id_from(j), title)
    else:
        out, rc = sh(f'lark-cli drive +import --file ./github-trending-{PERIOD}-{date_str}.md '
                     f'--type docx --name "{title}" --as user --format json 2>&1')
        j = extract_json(out)
        token = j.get('data', {}).get('token') if j else None
        if not token:
            print(f'导入失败：{out[:300]}')
            sys.exit(1)
        set_doc_title(token, title)
        out, rc = sh(f'lark-cli wiki +move --obj-type docx --obj-token {token} '
                     f'--target-space-id {SPACE_ID} --target-parent-token {PARENT_NODE} '
                     f'--as user --format json 2>&1')
        for _ in range(10):
            j = extract_json(out)
            if not j:
                break
            nxt = j.get('data', {}).get('next_command')
            if nxt:
                out, rc = sh(nxt)
                time.sleep(2)
                continue
            break
        j = extract_json(out)
        if j:
            node = j.get('data', {}).get('node_token') or (j.get('data', {}).get('node') or {}).get('node_token')
    os.remove(md_path)
    print('[5/5] 完成')
    top5 = '、'.join(it['repo'] for it in items[:5])
    print(f'\n✅ GitHub 热门{label}采集完成：共 {len(items)} 个仓库')
    print(f'Top 5：{top5}')
    if node:
        url = node if node.startswith('http') else f'https://my.feishu.cn/wiki/{node}'
        print(f'文档：{url}')


if __name__ == '__main__':
    main()
