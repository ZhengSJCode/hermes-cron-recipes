#!/bin/bash
# cron no_agent 模式下 script 字段不接受参数，用 wrapper 包装 360 SkillHub 每日榜调用
exec ${PYTHON:-python3} ${HERMES_SCRIPTS:-$HOME/.hermes/scripts}/skillhub360_collect.py --limit 20 --sort downloads
