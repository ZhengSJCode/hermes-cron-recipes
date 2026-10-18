#!/bin/bash
# cron no_agent 模式下 script 字段不接受参数，用 wrapper 包装月报调用
exec ${PYTHON:-python3} ${HERMES_SCRIPTS:-$HOME/.hermes/scripts}/github_trending_collect.py --period monthly --limit 15
