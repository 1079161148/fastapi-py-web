#!/usr/bin/env bash
# 从备份恢复数据库（会覆盖目标库，务必确认）。
#
# 用法：
#   ./deploy/restore.sh deploy/backups/aics-db-20261022-033000.sql.gz
#   # 演练：恢复到另一个库，不动生产库
#   TARGET_DB=aics_restore_drill ./deploy/restore.sh deploy/backups/aics-db-....sql.gz
#
# 注意：恢复到生产库前先停掉 api 与 worker，避免恢复过程中有写入：
#   docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env stop api worker

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "用法: $0 <备份文件.sql.gz>" >&2
  exit 1
fi

DUMP_FILE="$1"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/docker-compose.prod.yml"
ENV_FILE="${ROOT_DIR}/deploy/.env"

if [ ! -f "${DUMP_FILE}" ]; then
  echo "[restore] 备份文件不存在：${DUMP_FILE}" >&2
  exit 1
fi

# 容器内运行时变量已由 compose 注入，不需要 env 文件；宿主机上必须有
if [ -f "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  set -a; . "${ENV_FILE}"; set +a
elif [ -z "${POSTGRES_USER:-}" ] || [ -z "${POSTGRES_PASSWORD:-}" ]; then
  echo "[restore] 既没有 ${ENV_FILE}，环境变量也没有数据库凭证" >&2
  exit 1
fi

# 演练用：TARGET_DB 指向临时库时既不覆盖生产库，也不需要交互确认
TARGET_DB="${TARGET_DB:-${POSTGRES_DB}}"
CONFIRM_REQUIRED="${CONFIRM_REQUIRED:-true}"
if [ "${TARGET_DB}" != "${POSTGRES_DB}" ]; then
  CONFIRM_REQUIRED=false
fi

echo "[restore] 将用 ${DUMP_FILE} 覆盖数据库 ${TARGET_DB}"
if [ "${CONFIRM_REQUIRED}" = "true" ]; then
  read -r -p "确认请输入 yes： " answer
  if [ "${answer}" != "yes" ]; then
    echo "[restore] 已取消"
    exit 0
  fi
fi

# --clean --if-exists：先删再建，避免残留旧表结构；
# 单事务恢复，中途失败整体回滚，不会留下"恢复了一半"的库
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  gunzip -c "${DUMP_FILE}" | docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" \
    exec -T postgres pg_restore \
      -U "${POSTGRES_USER}" -d "${TARGET_DB}" --clean --if-exists --single-transaction
  echo "[restore] 完成。重新启动应用："
  echo "  docker compose -f ${COMPOSE_FILE} --env-file ${ENV_FILE} start api worker"
else
  gunzip -c "${DUMP_FILE}" | PGPASSWORD="${POSTGRES_PASSWORD}" pg_restore \
    -h "${POSTGRES_HOST:-127.0.0.1}" -p "${POSTGRES_PORT:-5432}" \
    -U "${POSTGRES_USER}" -d "${TARGET_DB}" --clean --if-exists --single-transaction
  echo "[restore] 完成。重新启动应用："
  echo "  systemctl restart aics-api aics-worker"
fi
