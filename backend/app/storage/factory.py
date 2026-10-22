"""存储后端工厂。

未实现的后端（minio）显式报 503，而不是无声退回本地目录——
静默回退会让生产把文件写到容器临时盘，重启即丢。
"""

from functools import lru_cache

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.storage.base import ObjectStorage
from app.storage.local import LocalStorage


@lru_cache
def get_storage() -> ObjectStorage:
    settings = get_settings()
    backend = settings.storage_backend.strip().lower()
    if backend == "local":
        from pathlib import Path

        return LocalStorage(Path(settings.storage_local_dir))
    raise AppError(
        code="STORAGE_NOT_CONFIGURED",
        message=(
            f"存储后端 {backend!r} 尚未实现：当前仅支持 local。"
            "MinIO 需在 M5 语音上线前补齐（镜像源受限问题见 deploy/docker-compose.yml）"
        ),
        status_code=503,
    )
