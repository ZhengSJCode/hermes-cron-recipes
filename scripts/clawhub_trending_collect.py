#!/usr/bin/env python3
"""
ClawHub 热门 Skill 采集脚本（v1）
- 抓取 https://clawhub.ai/api/v1/trending?limit=N（官方 24h 滚动热度榜，rankingVersion=skills-trending-v4）
- 解析技能列表（名/链接/简介/24h热度/累计安装/发布者/官方标记/安全结论）
- 用 https://clawhub.ai/api/v1/search?q=<slug> 精确匹配 install.reference 补 topics/categories/stats
- 本地模型（127.0.0.1 的 OpenAI 兼容端点）批量生成三列：给谁用 / 同类技能对比 / 中文用途
- 生成表格：| # | 应用 | 给谁用 | 同类技能对比 | 用途 | 标签 |
- 导入 docx 并 move 到飞书 wiki 节点下（新建子文档）；或 --doc 就地覆盖已有文档
- stdout 输出中文汇报（cron no_agent 模式直接投递）

用法:
  python3 clawhub_trending_collect.py [--limit N]
  python3 clawhub_trending_collect.py --doc "<wiki/docx URL 或 token>"   # 覆盖已有文档
  python3 clawhub_trending_collect.py ... --no-llm                        # 跳过本地模型
"""
import subprocess, re, html, json, sys, os, datetime, time, urllib.request, urllib.parse

LIMIT = 20
DOC = None          # 指定则就地覆盖该文档，不再新建
USE_LLM = True
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
BASE = 'https://clawhub.ai'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'


def fetch_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))


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


def parse_trending(d):
    items = []
    for it in d.get('items', []):
        metrics = it.get('metrics') or {}
        src = it.get('sourceIdentity') or {}
        canon = it.get('canonicalUrl') or (it.get('links') or {}).get('canonical') or ''
        items.append({
            'rank': it.get('rank'),
            'name': html.unescape(it.get('displayName') or it.get('slug') or ''),
            'slug': it.get('slug') or '',
            'url': BASE + canon if canon.startswith('/') else canon,
            'ref': (it.get('install') or {}).get('reference') or '',
            'desc': html.unescape(it.get('summary') or ''),
            'dl24h': metrics.get('trending24hDownloads'),
            'inst24h': metrics.get('trending24hInstalls'),
            'life': metrics.get('lifetimeInstalls', src.get('lifetimeInstalls')),
            'publisher': (it.get('publisher') or {}).get('handle') or src.get('owner') or '',
            'official': bool(it.get('official')),
            'verdict': (it.get('trust') or {}).get('clawHubVerdict') or '',
            'source': it.get('source') or '',
            'topics': [], 'categories': [], 'stats': {},
        })
    return items


def enrich(it):
    """search 精确匹配 install.reference 补 topics/categories/stats；失败留空"""
    ref = it.get('ref') or ''
    q = ref.split('/')[-1] if ref else it.get('slug', '')
    if not q:
        return
    try:
        d = fetch_json(f'{BASE}/api/v1/search?q={urllib.parse.quote(q)}')
    except Exception:
        return
    hit = None
    for r in d.get('results', []):
        if (r.get('install') or {}).get('reference') == ref:
            hit = r
            break
    if not hit:
        return
    sk = ((hit.get('native') or {}).get('skill') or {})
    it['topics'] = sk.get('topics') or []
    it['categories'] = sk.get('categories') or []
    it['stats'] = sk.get('stats') or {}
    if not it.get('life') and it['stats'].get('installs') is not None:
        it['life'] = it['stats'].get('installs')


def _llm_key():
    try:
        cfg = open(CONFIG_PATH, encoding='utf-8').read()
        m = re.search(r'^\s*api_key:\s*(\S+)\s*$', cfg, re.M)
        return m.group(1) if m else None
    except Exception:
        return None


PROMPT_TMPL = """你是 ClawHub（OpenClaw/Claude 技能市场）榜单编辑。下面是 ClawHub Trending 榜单的技能列表（JSON）。请为每个技能生成三个字段，**只输出 JSON 数组**，不要 markdown 代码块、不要任何解释。

字段要求：
- audience（给谁用）：结合技能用途推断目标用户，中文，≤18 字，要具体（例：多步 Agent 开发者 / 视频创作者 / 投研人员）。
- compare（同类技能对比）：**只在本列表内**找同类技能做对比；若列表内没有同类，写「本榜内无同类」；再用一句话说明它相对同类的差异或优势。中文，≤45 字。**严禁编造本列表之外的技能。**
- purpose_cn（用途）：把它的 summary 忠实翻译/概括成中文，≤40 字，不夸大、不加戏。

输入列表：
{payload}

输出格式（严格）：
[{{"ref":"<install reference 原样>","audience":"...","compare":"...","purpose_cn":"..."}}, ...]
要求：数组长度与输入一致，ref 原样返回。"""


def llm_enrich(items):
    """批量调用本地模型，返回 {ref: {audience, compare, purpose_cn}}；失败返回 {}"""
    if not USE_LLM or not items:
        return {}
    key = _llm_key()
    if not key:
        print('[llm] 未读到本地模型 api_key，跳过三列生成')
        return {}
    payload = json.dumps(
        [{'ref': it['ref'], 'name': it['name'], 'desc': it.get('desc', '')} for it in items],
        ensure_ascii=False)
    body = json.dumps({
        'model': LLM_MODEL,
        'temperature': 0,
        'messages': [
            {'role': 'system', 'content': '你是严谨的 ClawHub 榜单编辑，只输出合法 JSON。'},
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
        if isinstance(row, dict) and row.get('ref'):
            out[row['ref']] = {
                'audience': (row.get('audience') or '').strip(),
                'compare': (row.get('compare') or '').strip(),
                'purpose_cn': (row.get('purpose_cn') or '').strip(),
            }
    print(f'[llm] 三列生成完成：{len(out)}/{len(items)}')
    return out


def cell(s):
    """表格单元格：去掉换行、转义竖线"""
    return (s or '').replace('\n', ' ').replace('|', '\\|').strip()


def fmt_metric(it):
    """应用列下的热度数字：优先 24h 下载，回退累计安装"""
    d = it.get('dl24h')
    if isinstance(d, (int, float)) and d > 0:
        return f'24h 下载 {int(d):,}'
    l = it.get('life')
    if isinstance(l, (int, float)) and l > 0:
        return f'累计安装 {int(l):,}'
    if isinstance(l, (int, float)):
        return f'累计安装 {int(l):,}'
    return '暂无公开热度'


def build_md(items, date_str):
    lines = [f'> 数据来源：{BASE}/api/v1/trending（官方 24 小时滚动热度榜） · 采集时间 {date_str}',
             '> 列说明：应用=技能名(链接)+24h 热度；给谁用/同类技能对比/用途 由本机模型基于官方信息生成；标签=官方 topics/categories',
             '']
    lines.append('| # | 应用 | 给谁用 | 同类技能对比 | 用途 | 标签 |')
    lines.append('|---|------|------|------|------|------|')
    for i, it in enumerate(items, 1):
        name = f"[{cell(it['name'])}]({it['url']})<br/>{cell(fmt_metric(it))}"
        audience = cell(it.get('audience'))
        compare = cell(it.get('compare'))
        purpose = cell(it.get('purpose_cn')) or cell(it.get('desc')) or '（无描述）'
        tags_src = it.get('topics') or it.get('categories')
        if tags_src:
            tags = ' '.join(f'`{cell(t)}`' for t in tags_src)
        elif it.get('source') == 'skills-sh':
            tags = '外部来源（skills.sh），无官方 topics'
        else:
            tags = '官方未设置 topics'
        lines.append(f'| {i} | {name} | {audience} | {compare} | {purpose} | {tags} |')
    lines.append('')
    lines.append('## 备注')
    lines.append('')
    lines.append('- 本表为自动采集（脚本 clawhub_trending_collect.py）：热度为 ClawHub 官方 Trending 24 小时滚动口径；'
                 '给谁用/同类技能对比/用途 由本地模型生成（可能有个体偏差，人工可复核）；标签为技能官方 topics/categories')
    return '\n'.join(lines)


def main():
    date_str = datetime.date.today().strftime('%Y-%m-%d')
    print(f'[1/5] 抓取 ClawHub Trending (24h, top {LIMIT})...')
    d = fetch_json(f'{BASE}/api/v1/trending?limit={LIMIT}')
    items = parse_trending(d)
    if not items:
        print('抓取失败：未解析到技能。请稍后重试或检查网络。')
        sys.exit(1)
    items = items[:LIMIT]
    print(f'[2/5] 解析到 {len(items)} 个技能，补充 topics/categories...')
    for it in items:
        enrich(it)
        time.sleep(0.2)
    print('[3/5] 本地模型生成 给谁用/同类技能对比/中文用途...')
    llm = llm_enrich(items)
    for it in items:
        it.update(llm.get(it['ref'], {}))
    print('[4/5] 生成 Markdown 并写入飞书...')
    md = build_md(items, date_str)
    fname = f'clawhub-trending-{date_str}.md'
    md_path = os.path.join(WORKDIR, fname)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md)
    title = f'ClawHub 热门技能榜（{date_str} 采集）'
    node = None
    if DOC:
        out, rc = sh(f'lark-cli docs +update --doc "{DOC}" --command overwrite --doc-format markdown '
                     f'--content - --as user --format json 2>&1', stdin=md)
        j = extract_json(out)
        if not (j and j.get('ok')):
            print(f'覆盖文档失败：{out[:300]}')
            sys.exit(1)
        node = DOC
    else:
        # import 走相对路径（lark-cli 只接受 cwd 相对路径），文件已落在 WORKDIR
        out, rc = sh(f'lark-cli drive +import --file ./{fname} '
                     f'--type docx --name "{title}" --as user --format json 2>&1')
        j = extract_json(out)
        token = j.get('data', {}).get('token') if j else None
        if not token:
            print(f'导入失败：{out[:300]}')
            sys.exit(1)
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
    top5 = '、'.join(it['name'] for it in items[:5])
    print(f'\n✅ ClawHub 热门技能榜采集完成：共 {len(items)} 个技能')
    print(f'Top 5：{top5}')
    if node:
        url = node if node.startswith('http') else f'https://my.feishu.cn/wiki/{node}'
        print(f'文档：{url}')


if __name__ == '__main__':
    main()
