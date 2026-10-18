#!/bin/bash
# cron no_agent 模式下 script 字段不接受参数，用 wrapper 包装 ClawHub 每日榜调用
exec ${PYTHON:-python3} ${HERMES_SCRIPTS:-$HOME/.hermes/scripts}/clawhub_trending_collect.py --limit 20
