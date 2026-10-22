"""并发压测脚本（M8，设计文档 10「并发」）。

用法（backend 目录下）：

    .venv\\Scripts\\python.exe bench/load_test.py --users 20 --rounds 5
    .venv\\Scripts\\python.exe bench/load_test.py --users 50 --rounds 10 \
        --base-url http://127.0.0.1:8000

**只对压测/预发库使用**：每个虚拟用户会真实注册账号并写入会话与消息。

为什么不用 locust：要压的核心是 SSE 流式对话，locust 需要写自定义客户端才能
拿到"首 token 延迟"；而本项目 CI 不安装压测依赖。零依赖脚本在任何环境都能跑，
口径也完全可控。

统计口径：
- ttfb      ：从发请求到收到第一个 delta 事件的时间（体验指标，设计文档 10 目标 P95 < 3s）
- latency   ：整轮对话总时长（含落库与 SSE 收尾）
- errors    ：HTTP 非 2xx + SSE error 事件都算失败（后者 HTTP 仍是 200）
"""

import argparse
import asyncio
import json
import random
import statistics
import string
import time
from dataclasses import dataclass, field

import httpx

PASSWORD = "load-test-password-123"
QUESTIONS = (
    "你们几点上班？",
    "退款流程是怎样的？",
    "如何修改绑定的手机号？",
    "发票什么时候能开出来？",
    "支持哪些付款方式？",
)


@dataclass
class Sample:
    ok: bool
    ttfb_ms: float
    latency_ms: float
    error: str = ""


@dataclass
class Report:
    samples: list[Sample] = field(default_factory=list)

    def add(self, sample: Sample) -> None:
        self.samples.append(sample)


def _random_suffix(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(len(ordered) * ratio), len(ordered) - 1)
    return ordered[index]


async def _parse_sse_stream(
    client: httpx.AsyncClient, url: str, payload: dict[str, str], headers: dict[str, str]
) -> Sample:
    """发一轮对话，统计首 token 与总时长；SSE error 事件计为失败。"""
    started = time.perf_counter()
    ttfb = 0.0
    error = ""

    try:
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code != 200:
                await response.aread()
                return Sample(
                    ok=False,
                    ttfb_ms=0.0,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=f"HTTP {response.status_code}",
                )
            event_name = ""
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_name = line[len("event:") :].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[len("data:") :].strip()
                if event_name == "delta" and ttfb == 0.0:
                    ttfb = time.perf_counter() - started
                elif event_name == "error":
                    try:
                        error = str(json.loads(raw).get("code") or "stream_error")
                    except json.JSONDecodeError:
                        error = "stream_error"
                elif event_name == "done":
                    break
    except httpx.HTTPError as exc:
        return Sample(
            ok=False,
            ttfb_ms=0.0,
            latency_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )

    return Sample(
        ok=not error,
        ttfb_ms=ttfb * 1000,
        latency_ms=(time.perf_counter() - started) * 1000,
        error=error,
    )


async def _virtual_user(
    client: httpx.AsyncClient, *, base_url: str, rounds: int, report: Report
) -> None:
    email = f"load-{_random_suffix(10)}@example.com"
    registered = await client.post(
        f"{base_url}/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "tenant_name": f"压测租户 {_random_suffix(4)}",
        },
    )
    if registered.status_code != 201:
        report.add(
            Sample(
                ok=False,
                ttfb_ms=0.0,
                latency_ms=0.0,
                error=f"register {registered.status_code}",
            )
        )
        return

    token = registered.json()["tokens"]["access_token"]
    tenant_id = registered.json()["user"]["tenant_id"]
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Id": tenant_id}

    # 访客会话：签发 guest cookie，再建会话（对话主体走访客，和真实前端一致）
    guest = await client.post(f"{base_url}/api/v1/guest/sessions", headers=headers)
    if guest.status_code != 200:
        report.add(
            Sample(ok=False, ttfb_ms=0.0, latency_ms=0.0, error=f"guest {guest.status_code}")
        )
        return

    created = await client.post(f"{base_url}/api/v1/conversations", json={}, headers=headers)
    if created.status_code != 201:
        report.add(
            Sample(ok=False, ttfb_ms=0.0, latency_ms=0.0, error=f"conv {created.status_code}")
        )
        return
    conversation_id = created.json()["id"]

    for _ in range(rounds):
        sample = await _parse_sse_stream(
            client,
            f"{base_url}/api/v1/conversations/{conversation_id}/messages/stream",
            {"content": random.choice(QUESTIONS)},
            headers,
        )
        report.add(sample)


async def run(args: argparse.Namespace) -> Report:
    report = Report()
    limits = httpx.Limits(max_connections=args.users * 2, max_keepalive_connections=args.users)
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        started = time.perf_counter()
        await asyncio.gather(
            *(
                _virtual_user(client, base_url=args.base_url, rounds=args.rounds, report=report)
                for _ in range(args.users)
            )
        )
        elapsed = time.perf_counter() - started

    _print_report(report, elapsed)
    return report


def _print_report(report: Report, elapsed: float) -> None:
    samples = report.samples
    ok = [item for item in samples if item.ok]
    failed = [item for item in samples if not item.ok]
    ttfb = [item.ttfb_ms for item in ok if item.ttfb_ms > 0]
    latency = [item.latency_ms for item in ok]

    print("=" * 62)
    print(f"总请求 {len(samples)}  成功 {len(ok)}  失败 {len(failed)}  用时 {elapsed:.2f}s")
    print(f"QPS {len(samples) / elapsed:.2f}" if elapsed > 0 else "QPS -")
    if latency:
        print(
            f"整轮延迟(ms)  mean {statistics.fmean(latency):.0f}  "
            f"p50 {_percentile(latency, 0.5):.0f}  "
            f"p95 {_percentile(latency, 0.95):.0f}  "
            f"p99 {_percentile(latency, 0.99):.0f}"
        )
    if ttfb:
        print(
            f"首token(ms)   mean {statistics.fmean(ttfb):.0f}  "
            f"p50 {_percentile(ttfb, 0.5):.0f}  "
            f"p95 {_percentile(ttfb, 0.95):.0f}  "
            f"p99 {_percentile(ttfb, 0.99):.0f}"
        )
    if failed:
        reasons: dict[str, int] = {}
        for item in failed:
            reasons[item.error] = reasons.get(item.error, 0) + 1
        print("失败分布：")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {count:5d}  {reason}")
    print("=" * 62)


def main() -> None:
    parser = argparse.ArgumentParser(description="AICS 并发压测（SSE 流式对话）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--users", type=int, default=10, help="并发虚拟用户数")
    parser.add_argument("--rounds", type=int, default=3, help="每用户对话轮数")
    parser.add_argument("--timeout", type=float, default=120.0, help="单请求超时（秒）")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
