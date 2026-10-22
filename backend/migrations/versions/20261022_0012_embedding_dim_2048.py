"""M9 修正：向量维度 1536 → 2048，并去掉超限的 HNSW 索引

两件事必须一起做，否则按迁移部署的环境一写入向量就报错：

1. 0003 把 chunks.embedding 固定成 vector(1536)，但实际使用的嵌入模型
   （nvidia/llama-nemotron-embed-vl-1b-v2:free）输出 2048 维。开发库当初是
   手工 ALTER 过的，迁移没跟上。
2. pgvector 的 ANN 索引（hnsw / ivfflat）**上限 2000 维**，2048 维建不了索引
   （"column cannot have more than 2000 dimensions for hnsw index"）。
   所以本迁移只改列类型，不再建 ANN 索引——向量检索退化为精确最近邻
   （顺序扫描 + 排序）。

精确扫描的适用边界：单租户知识库在数千 chunk 量级时每次查询毫秒级，
可接受；到十万级 chunk 就必须换方案：
- 换 <=2000 维的嵌入模型，重新建 hnsw 索引；或
- 升级 pgvector 到支持 halfvec 的版本，用 halfvec(2048) 建 hnsw
  （halfvec 上限 4000 维，但需要 PG 侧版本与召回精度验证）。

维度与嵌入模型是绑定关系：换模型 = 换维度，必须走新迁移重建列，不能只改环境变量。

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 2048


def upgrade() -> None:
    # ANN 索引必须显式删掉：列类型都变了，索引留着也无法 ALTER
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute(
        f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({EMBEDDING_DIM}) "
        "USING CASE WHEN embedding IS NULL THEN NULL "
        f"ELSE (embedding::vector({EMBEDDING_DIM})) END"
    )
    # 有意不重建 ANN 索引：2048 维超出 pgvector 的 2000 维上限，见模块 docstring


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute(
        "ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(1536) USING embedding::vector(1536)"
    )
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)"
    )
