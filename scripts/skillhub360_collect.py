#!/usr/bin/env python3
"""
360 SkillHub 热门技能采集脚本（v1）
- 抓取 https://skillhub.360.com/api/v1/skills?limit=N&sort=downloads（下载量榜=热门口径）
- 360 接口已把名称/简介本地化为中文，用途直接取官方 summary_short（无需翻译）
- 本地模型（127.0.0.1 的 OpenAI 兼容端点）只生成两列：给谁用 / 同类技能对比
- 生成表格：| # | 应用 | 给谁用 | 同类技能对比 | 用途 | 标签 |
- 导入 docx 并 move 到飞书 wiki 节点下（新建子文档）；或 --doc 就地覆盖已有文档
- stdout 输出中文汇报（cron no_agent 模式直接投递）

用法:
  python3 skillhub360_collect.py [--limit N] [--sort downloads|stars|updated|created]
  python3 skillhub360_collect.py --doc "<wiki/docx URL 或 token>"   # 覆盖已有文档
  python3 skillhub360_collect.py ... --no-llm                        # 跳过本地模型
"""
import subprocess, re, html, json, sys, os, datetime, time, urllib.request

LIMIT = 20
SORT = 'downloads'   # 360 站内「热门」口径：下载量最多
DOC = None
USE_LLM = True
if '--limit' in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index('--limit') + 1])
if '--sort' in sys.argv:
    SORT = sys.argv[sys.argv.index('--sort') + 1]
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
BASE = 'https://skillhub.360.com'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'

SORT_LABEL = {'downloads': '下载量', 'stars': '标星数', 'updated': '最近更新', 'created': '最新发布'}


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
    last = out.rfind('{')
    while last != -1:
        try:
            return json.loads(out[last:])
        except Exception:
            last = out.rfind('{', 0, last)
    return None


def extract_json_array(txt):
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


def parse_items(d):
    items = []
    for it in d.get('items', []):
        tags = it.get('tags') or {}
        lv = it.get('latestVersion') or {}
        items.append({
            'slug': it.get('slug') or '',
            'name': it.get('displayName') or it.get('skillName') or it.get('slug') or '',
            'url': f"{BASE}/skills/{it.get('slug')}",
            'desc': it.get('summary') or '',
            'short': it.get('summary_short') or '',
            'category': tags.get('category') or '',
            'scene_tags': tags.get('scene_tags') or [],
            'tool_deps': tags.get('tool_dependencies') or [],
            'downloads': it.get('merged_downloads') or 0,
            'stars': it.get('merged_stars') or 0,
            'owner': it.get('ownerDisplayName') or it.get('ownerHandle') or '',
            'version': lv.get('version') or '',
            'license': it.get('license') or lv.get('license') or '',
            'security': it.get('securityStatus') or '',
            'compat': it.get('agentCompat') or '',
            'source': it.get('source') or '',
        })
    return items


def _llm_key():
    try:
        cfg = open(CONFIG_PATH, encoding='utf-8').read()
        m = re.search(r'^\s*api_key:\s*(\S+)\s*$', cfg, re.M)
        return m.group(1) if m else None
    except Exception:
        return None


PROMPT_TMPL = """你是 360 SkillHub（Agent 技能市场）榜单编辑。下面是「下载量」热门榜单的技能列表（JSON）。请为每个技能生成两个字段，**只输出 JSON 数组**，不要 markdown 代码块、不要任何解释。

字段要求：
- audience（给谁用）：结合技能用途推断目标用户，中文，≤18 字，要具体（例：多步 Agent 开发者 / 视频创作者 / 投研人员）。
- compare（同类技能对比）：**只在本列表内**找同类技能做对比；若列表内没有同类，写「本榜内无同类」；再用一句话说明它相对同类的差异或优势。中文，≤45 字。**严禁编造本列表之外的技能。**

输入列表：
{payload}

输出格式（严格）：
[{{"slug":"...","audience":"...","compare":"..."}}, ...]
要求：数组长度与输入一致，slug 原样返回。"""


def llm_enrich(items):
    if not USE_LLM or not items:
        return {}
    key = _llm_key()
    if not key:
        print('[llm] 未读到本地模型 api_key，跳过两列生成')
        return {}
    payload = json.dumps(
        [{'slug': it['slug'], 'name': it['name'], 'category': it['category'],
          'scene_tags': it['scene_tags'], 'desc': it.get('short') or it.get('desc', '')}
         for it in items], ensure_ascii=False)
    body = json.dumps({
        'model': LLM_MODEL,
        'temperature': 0,
        'messages': [
            {'role': 'system', 'content': '你是严谨的 360 SkillHub 榜单编辑，只输出合法 JSON。'},
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
            print('[llm] 调用失败（%s），两列留空：%s' % (type(_e).__name__, str(_e)[:120]))
            return {}
    content = resp['choices'][0]['message']['content']
    arr = extract_json_array(content)
    if not arr:
        print('[llm] 返回无法解析为 JSON，两列留空')
        return {}
    out = {}
    for row in arr:
        if isinstance(row, dict) and row.get('slug'):
            out[row['slug']] = {
                'audience': (row.get('audience') or '').strip(),
                'compare': (row.get('compare') or '').strip(),
            }
    print(f'[llm] 两列生成完成：{len(out)}/{len(items)}')
    return out


def cell(s):
    return (s or '').replace('\n', ' ').replace('|', '\\|').strip()


def purpose_of(it):
    txt = (it.get('short') or it.get('desc') or '').strip()
    return txt or '（无简介）'


def tags_of(it):
    parts = []
    if it.get('security') == 'warning':
        parts.append('`⚠️安全警告`')
    if it.get('category'):
        parts.append(f"`{it['category']}`")
    for t in it.get('scene_tags', []):
        parts.append(f'`{t}`')
    for t in it.get('tool_deps', []):
        parts.append(f'`{t}`')
    tags = ' '.join(parts) if parts else '官方未设置标签'
    return tags


def build_md(items, date_str):
    lines = [f'> 数据来源：{BASE}/api/v1/skills（站内热门榜，排序：{SORT_LABEL.get(SORT, SORT)}） · 采集时间 {date_str}',
             '> 列说明：应用=技能名(链接)+下载量/标星；给谁用/同类技能对比 由本机模型基于官方信息生成；'
             '用途=官方中文简介；标签=官方分类/场景/依赖（⚠️ 为官方安全提示）',
             '',
             '| # | 应用 | 给谁用 | 同类技能对比 | 用途 | 标签 |',
             '|---|------|------|------|------|------|']
    for i, it in enumerate(items, 1):
        name = f"[{cell(it['name'])}]({it['url']})<br/>下载 {int(it['downloads']):,} · 星 {int(it['stars']):,}"
        audience = cell(it.get('audience'))
        compare = cell(it.get('compare'))
        purpose = cell(purpose_of(it))
        lines.append(f"| {i} | {name} | {audience} | {compare} | {purpose} | {tags_of(it)} |")
    lines.append('')
    lines.append('## 备注')
    lines.append('')
    lines.append('- 本表为自动采集（脚本 skillhub360_collect.py）：下载量/标星为 360 SkillHub 官方合并口径；'
                 '给谁用/同类技能对比 由本地模型生成（可能有个体偏差，人工可复核）；'
                 '用途为官方中文简介；标签为官方分类/场景标签/工具依赖；`⚠️安全警告` 表示官方安全状态非 safe')
    return '\n'.join(lines)


def main():
    date_str = datetime.date.today().strftime('%Y-%m-%d')
    print(f'[1/5] 抓取 360 SkillHub 热门技能 (sort={SORT}, top {LIMIT})...')
    d = fetch_json(f'{BASE}/api/v1/skills?limit={LIMIT}&sort={SORT}')
    items = parse_items(d)
    if not items:
        print('抓取失败：未解析到技能。请稍后重试或检查网络。')
        sys.exit(1)
    items = items[:LIMIT]
    print(f'[2/5] 解析到 {len(items)} 个技能（官方中文简介已就绪）')
    print('[3/5] 本地模型生成 给谁用/同类技能对比...')
    llm = llm_enrich(items)
    for it in items:
        it.update(llm.get(it['slug'], {}))
    print('[4/5] 生成 Markdown 并写入飞书...')
    md = build_md(items, date_str)
    fname = f'skillhub360-trending-{date_str}.md'
    md_path = os.path.join(WORKDIR, fname)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md)
    title = f'360 SkillHub 热门技能榜（{date_str} 采集）'
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
    print(f'\n✅ 360 SkillHub 热门技能榜采集完成：共 {len(items)} 个技能')
    print(f'Top 5：{top5}')
    if node:
        url = node if node.startswith('http') else f'https://my.feishu.cn/wiki/{node}'
        print(f'文档：{url}')


if __name__ == '__main__':
    main()
