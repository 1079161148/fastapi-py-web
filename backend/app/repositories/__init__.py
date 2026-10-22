"""仓储层：把可替换的基础设施（向量引擎）隔离在接口之后（设计文档 9.4）。"""

from app.repositories.chunks import (
    ChunkHit,
    ChunkRepository,
    PgVectorChunkRepository,
)

__all__ = ["ChunkHit", "ChunkRepository", "PgVectorChunkRepository"]
