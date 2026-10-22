"""检索回归脚本（设计文档 9.4：评估集必须能跑回归）。

用法（在 backend 目录下执行）：

    .venv\\Scripts\\python.exe scripts\\eval_retrieval.py ^
        --tenant <租户UUID> ^
        --dataset scripts\\eval_dataset.example.jsonl ^
        --top-k 5

数据集为 JSONL，每行一个用例：

    {"query": "退款多久到账", "expect": ["退款", "3 个工作日"], "kb": "客服知识库"}

指标：
- recall@k：top-k 中是否存在命中所有 expect 关键词的片段
- MRR：第一个命中的倒数排名（未命中记 0）
- 分别输出"向量+关键词+RRF"与"再加 rerank"两种配置的对比，便于判断 rerank 是否值得开启

注意：数值只有在配置了真实嵌入模型时才有意义；假嵌入只能验证脚本链路。
退出码：recall@k 低于 --min-recall 时返回 1，可直接用于 CI 门禁。
"""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

# 脚本不在包内，直接运行时需要把 backend 根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.tenant import set_current_tenant  # noqa: E402
from app.db.models.knowledge import KnowledgeBase  # noqa: E402
from app.db.session import async_session_factory, engine  # noqa: E402
from app.llm.base import EmbeddingClient, RerankClient  # noqa: E402
from app.llm.factory import get_embedding_client, get_rerank_client  # noqa: E402
from app.services.retrieval import active_kb_ids, search  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="知识库检索回归")
    parser.add_argument("--tenant", required=True, help="租户 UUID（决定 RLS 上下文）")
    parser.add_argument("--dataset", required=True, help="JSONL 数据集路径")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--min-recall",
        type=float,
        default=0.0,
        help="recall@k 下限，低于该值脚本返回非零退出码（默认不设门禁）",
    )
    return parser.parse_args()


def load_dataset(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    # utf-8-sig：Windows 下用 PowerShell/记事本保存的 JSONL 常带 BOM，
    # 按 utf-8 读取会把 BOM 当成首个字符导致 JSON 解析失败
    content = path.read_text(encoding="utf-8-sig")
    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            case = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"第 {line_number} 行不是合法 JSON：{exc}") from exc
        if not case.get("query") or not case.get("expect"):
            raise SystemExit(f"第 {line_number} 行缺少 query 或 expect 字段")
        cases.append(case)
    if not cases:
        raise SystemExit("数据集为空")
    return cases


async def resolve_kb_ids(
    session: Any, tenant_id: Any, default_ids: list[Any], kb_name: str | None
) -> list[Any]:
    if not kb_name:
        return default_ids
    row = await session.execute(
        select(KnowledgeBase.id).where(
            KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.name == kb_name
        )
    )
    kb_id = row.scalar_one_or_none()
    if kb_id is None:
        raise SystemExit(f"找不到知识库：{kb_name}")
    return [kb_id]


def evaluate_case(case: dict[str, Any], hits: list[Any], top_k: int) -> tuple[bool, float]:
    expects = [str(item) for item in case["expect"]]
    for rank, hit in enumerate(hits[:top_k], start=1):
        if all(keyword in hit.content for keyword in expects):
            return True, 1.0 / rank
    return False, 0.0


async def run(args: argparse.Namespace) -> int:
    tenant_id = uuid.UUID(args.tenant)
    set_current_tenant(tenant_id)
    cases = load_dataset(Path(args.dataset))

    embedder: EmbeddingClient = get_embedding_client()
    reranker: RerankClient | None = get_rerank_client()

    stats: dict[str, list[tuple[bool, float]]] = {"rrf": [], "rrf+rerank": []}
    async with async_session_factory() as session:
        default_ids = await active_kb_ids(session, tenant_id)
        if not default_ids:
            raise SystemExit("该租户没有可用知识库，先上传文档并等待解析完成")

        for case in cases:
            kb_ids = await resolve_kb_ids(session, tenant_id, default_ids, case.get("kb"))
            for label, rerank_client in (("rrf", None), ("rrf+rerank", reranker)):
                result = await search(
                    session,
                    tenant_id=tenant_id,
                    kb_ids=kb_ids,
                    query=case["query"],
                    embedder=embedder,
                    reranker=rerank_client,
                    top_k=args.top_k,
                )
                hits = result.items
                hit, reciprocal_rank = evaluate_case(case, hits, args.top_k)
                stats[label].append((hit, reciprocal_rank))
                if label == "rrf":
                    mark = "HIT " if hit else "MISS"
                    print(f"[{mark}] {case['query']}  top={len(hits[: args.top_k])}")

    exit_code = 0
    for label, results in stats.items():
        recall = sum(1 for hit, _ in results if hit) / len(results)
        mrr = sum(reciprocal_rank for _, reciprocal_rank in results) / len(results)
        print(f"\n{label}: recall@{args.top_k}={recall:.3f}  MRR={mrr:.3f}  n={len(results)}")
        if label == "rrf" and recall < args.min_recall:
            print(f"未达到 recall 门禁 {args.min_recall:.3f}")
            exit_code = 1

    await engine.dispose()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
