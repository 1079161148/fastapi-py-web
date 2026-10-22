#!/bin/sh
# 容器内定时备份（宿主机没有 cron / systemd 时的兜底方案）。
#
# Linux 宿主机更推荐 deploy/systemd/aics-backup.timer：少一个常驻容器，
# 失败也能在 journalctl 里看到。这里是 Docker-only 环境的等价物。

set -eu

echo "[backup-loop] 间隔 ${BACKUP_INTERVAL_SECONDS:-86400}s，保留 ${RETENTION_DAYS:-14} 天"

while true; do
  if /backup.sh; then
    echo "[backup-loop] $(date -Is) 备份完成"
  else
    # 单次失败不退出：备份是周期任务，下一次成功比"整个容器挂掉"有用
    echo "[backup-loop] $(date -Is) 备份失败，下个周期重试" >&2
  fi
  sleep "${BACKUP_INTERVAL_SECONDS:-86400}"
done
