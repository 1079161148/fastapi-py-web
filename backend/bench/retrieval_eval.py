"""检索质量回归（M8，设计文档 9.4 / 11「检索与回答质量回归」）。

用固定评估集跑一遍混合检索，输出 recall@k 与 MRR，作为改分块策略、
召回参数或 rerank 开关前后的对照基线。

用法（backend 目录下）：

    .venv\\Scripts\\python.exe bench/retrieval_eval.py ^
        --token <access_token> --dataset bench/eval_dataset.example.json

    # 低于阈值时退出码 1，可直接用于 CI 回归门禁
    .venv\\Scripts\\python.exe bench/retrieval_eval.py --token ... --min-recall 0.8

数据集格式：

    {
      "cases": [
        {"query": "退款多久到账", "expected_documents": ["退款政策.md"]}
      ]
    }

为什么用"文档名"而不是 chunk id 做标注：chunk id 会随重新入库变化，
而文档名稳定；评估目标是"能不能找到正确资料"，不需要精确到片段。
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass(frozen=True, slots=True)
class Case:
    query: str
    expected_documents: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CaseResult:
    query: str
    hit_rank: int  # 第一个命中的名次（1 起），0 表示前 k 条都没命中
    top_documents: tuple[str, ...]


def load_dataset(path: Path) -> list[Case]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases: list[Case] = []
    for index, raw in enumerate(payload.get("cases") or []):
        query = str(raw.get("query") or "").strip()
        expected = tuple(str(item) for item in (raw.get("expected_documents") or []) if item)
        if not query or not expected:
            raise ValueError(f"第 {index + 1} 条用例缺少 query 或 expected_documents")
        cases.append(Case(query=query, expected_documents=expected))
    if not cases:
        raise ValueError("数据集中没有任何用例")
    return cases


def evaluate_case(case: Case, items: list[dict]) -> CaseResult:
    documents = tuple(str(item.get("document_name") or "") for item in items)
    hit_rank = 0
    for rank, name in enumerate(documents, start=1):
        if name in case.expected_documents:
            hit_rank = rank
            break
    return CaseResult(query=case.query, hit_rank=hit_rank, top_documents=documents)


def summarize(results: list[CaseResult], top_k: int) -> dict[str, float]:
    total = len(results)
    hits = [item for item in results if item.hit_rank > 0]
    recall = len(hits) / total if total else 0.0
    mrr = sum(1.0 / item.hit_rank for item in hits) / total if total else 0.0
    return {"cases": float(total), "recall": recall, "mrr": mrr, "top_k": float(top_k)}


def main() -> None:
    parser = argparse.ArgumentParser(description="知识库检索质量评估")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True, help="租户成员的 access token")
    parser.add_argument(
        "--dataset", default="bench/eval_dataset.example.json", help="评估集 JSON 路径"
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--kb-id", default="", help="只评估指定知识库（默认全部）")
    parser.add_argument(
        "--min-recall", type=float, default=0.0, help="低于该 recall 时退出码 1"
    )
    args = parser.parse_args()

    cases = load_dataset(Path(args.dataset))
    headers = {"Authorization": f"Bearer {args.token}"}
    params: dict[str, str] = {"top_k": str(args.top_k)}
    if args.kb_id:
        params["kb_id"] = args.kb_id

    results: list[CaseResult] = []
    with httpx.Client(base_url=args.base_url, timeout=60.0) as client:
        for case in cases:
            response = client.get(
                "/api/v1/knowledge/search",
                params={**params, "q": case.query},
                headers=headers,
            )
            if response.status_code != 200:
                print(f"[FAIL] {case.query} → HTTP {response.status_code} {response.text[:200]}")
                results.append(CaseResult(query=case.query, hit_rank=0, top_documents=()))
                continue
            result = evaluate_case(case, response.json().get("items") or [])
            results.append(result)
            mark = "✓" if result.hit_rank else "✗"
            print(f"[{mark}] rank={result.hit_rank} {case.query} → {list(result.top_documents)}")

    stats = summarize(results, args.top_k)
    print("=" * 60)
    print(
        f"用例 {int(stats['cases'])}  recall@{int(stats['top_k'])} {stats['recall']:.3f}  "
        f"MRR {stats['mrr']:.3f}"
    )
    print("=" * 60)

    if stats["recall"] < args.min_recall:
        print(f"recall {stats['recall']:.3f} 低于阈值 {args.min_recall:.3f}，判定为回归")
        sys.exit(1)


if __name__ == "__main__":
    main()
