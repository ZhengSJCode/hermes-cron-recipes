#!/usr/bin/env python3
"""把 jobs/*.job.json 装进本机 Hermes：拷脚本 + 调 `hermes cron create`。

用法:
    python3 install.py --dry-run          # 只打印将执行的命令，不落地
    python3 install.py                    # 真装
    python3 install.py --only portable    # 只装通用 5 个
    python3 install.py --only personalized

前置:
    - Hermes Agent 已安装，`hermes` 在 PATH
    - 已 `cp config/.env.example .env` 并填好，且 `set -a; . ./.env`
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
HERMES_SCRIPTS = os.path.expanduser(
    os.environ.get('HERMES_SCRIPTS') or '~/.hermes/scripts')

ENV_PAT = re.compile(r'\$\{([A-Z0-9_]+)(?::-([^}]*))?\}')


def expand(text):
    """展开 ${VAR} / ${VAR:-default}，未定义且无默认值时保留原样并告警。"""
    missing = set()

    def _sub(m):
        name, default = m.group(1), m.group(2)
        if name == 'HOME':
            return os.path.expanduser('~')
        val = os.environ.get(name)
        if val:
            return val
        if default is not None:
            return default
        missing.add(name)
        return m.group(0)

    out = ENV_PAT.sub(_sub, text)
    return out, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--only', choices=['portable', 'personalized'])
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(ROOT, 'jobs', '*.job.json')))
    if not files:
        sys.exit('jobs/ 下没有 .job.json')

    all_missing = set()
    planned = []

    for fn in files:
        job = json.load(open(fn, encoding='utf-8'))
        tier = job.get('tier')
        if args.only and tier != args.only:
            continue
        slug = job['id_slug']
        prompt, miss = expand(job.get('prompt') or '')
        all_missing |= miss
        workdir, miss2 = expand(job.get('workdir') or '')
        all_missing |= miss2

        cmd = ['hermes', 'cron', 'create', job['schedule']]
        if prompt:
            cmd.append(prompt)
        cmd += ['--name', job.get('name') or slug]
        if job.get('no_agent'):
            cmd.append('--no-agent')
        if job.get('script'):
            cmd += ['--script', job['script']]
        for s in job.get('skills') or []:
            cmd += ['--skill', s]
        if workdir:
            cmd += ['--workdir', workdir]
        if job.get('repeat'):
            rep = job['repeat']
            times = rep.get('times') if isinstance(rep, dict) else rep
            if times:
                cmd += ['--repeat', str(times)]
        planned.append((tier, slug, cmd))

    # 1) 拷脚本
    if not args.dry_run:
        os.makedirs(HERMES_SCRIPTS, exist_ok=True)
        for f in sorted(glob.glob(os.path.join(ROOT, 'scripts', '*'))):
            dst = os.path.join(HERMES_SCRIPTS, os.path.basename(f))
            shutil.copy2(f, dst)
            print('copied  %s -> %s' % (os.path.basename(f), dst))

    # 2) 建 job
    for tier, slug, cmd in planned:
        print()
        print('[%s] %s' % (tier, slug))
        if args.dry_run:
            print('  ' + ' '.join(
                (c if len(c) < 120 else c[:117] + '...') for c in cmd))
            continue
        r = subprocess.run(cmd, capture_output=True, text=True)
        ok = r.returncode == 0
        print('  %s' % ('OK' if ok else 'FAILED rc=%d' % r.returncode))
        tail = (r.stdout or r.stderr or '').strip().splitlines()
        for line in tail[-3:]:
            print('    ' + line)

    if all_missing:
        print()
        print('⚠️  以下环境变量未设置，模板里保留了 ${VAR} 原文，请补 .env 或手动改:')
        for m in sorted(all_missing):
            print('   - ' + m)

    skills = set()
    for fn in files:
        j = json.load(open(fn, encoding='utf-8'))
        if not args.only or j.get('tier') == args.only:
            skills |= set(j.get('skills') or [])
    if skills:
        print()
        print('本仓库不自带以下 skill，请自行安装（示例）:')
        for s in sorted(skills):
            print('   qskillhub install %s -d ~/.hermes/skills -y' % s)


if __name__ == '__main__':
    main()
