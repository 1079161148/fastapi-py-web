#!/usr/bin/env bash
# 数据库与上传文件的备份脚本。
#
# 用法：
#   ./deploy/backup.sh                 # 备份到 deploy/backups/
#   BACKUP_DIR=/data/aics-backup ./deploy/backup.sh
#
# 定时（每天 03:30）：
#   30 3 * * * /path/to/py-web-all/deploy/backup.sh >> /var/log/aics-backup.log 2>&1
# 或用 systemd timer（见 deploy/systemd/aics-backup.timer）。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 容器内运行时由 compose 注入 BACKUP_DIR=/backups
BACKUP_DIR="${BACKUP_DIR:-${ROOT_DIR}/deploy/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
# 上传文件（知识库原件、语音、图片）体积可能很大，默认不打包
BACKUP_UPLOADS="${BACKUP_UPLOADS:-false}"
COMPOSE_FILE="${ROOT_DIR}/deploy/docker-compose.prod.yml"
ENV_FILE="${ROOT_DIR}/deploy/.env"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "${BACKUP_DIR}"

# 三种运行形态：容器内（变量直接注入）、宿主机 + compose、裸机直连
if [ -f "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  set -a; . "${ENV_FILE}"; set +a
elif [ -z "${POSTGRES_USER:-}" ] || [ -z "${POSTGRES_PASSWORD:-}" ]; then
  echo "[backup] 既没有 ${ENV_FILE}，环境变量也没有数据库凭证" >&2
  exit 1
fi

IN_COMPOSE=false
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 && [ -f "${COMPOSE_FILE}" ]; then
  IN_COMPOSE=true
fi

DUMP_FILE="${BACKUP_DIR}/aics-db-${STAMP}.sql.gz"
echo "[backup] 导出数据库 -> ${DUMP_FILE}"
# -Fc 自定义格式：pg_restore 可选择性恢复单表，纯 SQL 文本做不到
if [ "${IN_COMPOSE}" = "true" ]; then
  docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" exec -T postgres \
    pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -Fc | gzip > "${DUMP_FILE}"
else
  # 裸机与容器内都走这条：POSTGRES_HOST 在容器里由 compose 注入为 postgres
  PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump \
    -h "${POSTGRES_HOST:-127.0.0.1}" -p "${POSTGRES_PORT:-5432}" \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -Fc | gzip > "${DUMP_FILE}"
fi

if [ "${BACKUP_UPLOADS}" = "true" ]; then
  UPLOADS_FILE="${BACKUP_DIR}/aics-uploads-${STAMP}.tar.gz"
  echo "[backup] 打包上传目录 -> ${UPLOADS_FILE}"
  if [ "${IN_COMPOSE}" = "true" ]; then
    # 在临时容器里打包：不依赖宿主机上的卷挂载路径
    docker run --rm \
      -v aics-prod_uploads_data:/data:ro \
      -v "${BACKUP_DIR}":/out \
      alpine tar czf "/out/aics-uploads-${STAMP}.tar.gz" -C /data .
  else
    tar czf "${UPLOADS_FILE}" -C "${UPLOADS_DIR:-/opt/aics/backend/var/uploads}" .
  fi
fi

echo "[backup] 清理 ${RETENTION_DAYS} 天前的备份"
find "${BACKUP_DIR}" -name 'aics-*' -type f -mtime "+${RETENTION_DAYS}" -delete

echo "[backup] 完成：$(ls -1 "${BACKUP_DIR}" | tail -n 3 | tr '\n' ' ')"
