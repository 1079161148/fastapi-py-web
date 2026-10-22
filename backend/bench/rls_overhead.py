"""RLS 开销对比（M8，设计文档 10「多租户开销」）。

目的：量化行级安全对查询的影响，判断是否需要调整索引或策略，
而不是凭"RLS 会慢"这种印象做决策。

两个连接对比同一查询：
- **应用连接**（DATABASE_URL，aics_app 角色，受 RLS 约束）
- **超管连接**（MIGRATION_DATABASE_URL，超级用户绕过 RLS）

用法（backend 目录下）：

    .venv\\Scripts\\python.exe bench/rls_overhead.py --tenant-id <uuid> --iterations 200

要求库中该租户已有一定量数据（messages 至少几千行），否则差值主要是噪音。
脚本只读，不写任何数据。
"""

import argparse
import asyncio
import statistics
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings

# 应用侧与服务层一致：显式 tenant 条件 + RLS 的第二道过滤
APP_SQL = text(
    """
    SELECT id, conversation_id, role, created_at
      FROM messages
     WHERE tenant_id = :tenant_id
     ORDER BY created_at DESC
     LIMIT :limit
    """
)
# 超管侧：仅保留同样的显式条件（不带 RLS，也没有 SET LOCAL）
ADMIN_SQL = APP_SQL
SET_TENANT_SQL = text("SELECT set_config('app.tenant_id', :tenant_id, true)")


async def _measure(
    engine: AsyncEngine,
    *,
    tenant_id: str,
    limit: int,
    iterations: int,
    with_rls: bool,
) -> list[float]:
    samples: list[float] = []
    async with engine.connect() as conn:
        for _ in range(iterations):
            started = time.perf_counter()
            if with_rls:
                # 与运行时一致：SET LOCAL 在事务内生效
                async with conn.begin():
                    await conn.execute(SET_TENANT_SQL, {"tenant_id": tenant_id})
                    await conn.execute(APP_SQL, {"tenant_id": tenant_id, "limit": limit})
            else:
                await conn.execute(ADMIN_SQL, {"tenant_id": tenant_id, "limit": limit})
            samples.append((time.perf_counter() - started) * 1000)
    return samples


def _summary(name: str, samples: list[float]) -> float:
    p50 = statistics.median(samples)
    p95 = sorted(samples)[min(int(len(samples) * 0.95), len(samples) - 1)]
    mean = statistics.fmean(samples)
    print(f"{name:<12} p50 {p50:7.3f} ms   p95 {p95:7.3f} ms   mean {mean:7.3f} ms")
    return p50


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    admin_engine = create_async_engine(settings.migration_database_url, pool_pre_ping=True)
    try:
        # 预热：首个请求含连接建立与计划缓存，单独丢掉
        await _measure(
            app_engine,
            tenant_id=args.tenant_id,
            limit=args.limit,
            iterations=5,
            with_rls=True,
        )
        await _measure(
            admin_engine,
            tenant_id=args.tenant_id,
            limit=args.limit,
            iterations=5,
            with_rls=False,
        )

        admin = await _measure(
            admin_engine,
            tenant_id=args.tenant_id,
            limit=args.limit,
            iterations=args.iterations,
            with_rls=False,
        )
        app = await _measure(
            app_engine,
            tenant_id=args.tenant_id,
            limit=args.limit,
            iterations=args.iterations,
            with_rls=True,
        )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()

    print("=" * 66)
    print(f"租户 {args.tenant_id}  样本 {args.iterations}  每次取 {args.limit} 行")
    admin_p50 = _summary("超管(无RLS)", admin)
    app_p50 = _summary("应用(RLS)", app)
    if admin_p50 > 0:
        print(
            f"RLS 额外开销（p50）：{app_p50 - admin_p50:+.3f} ms "
            f"({(app_p50 / admin_p50 - 1) * 100:+.1f}%)"
        )
    print("=" * 66)
    print(
        "解读提示：绝对值受数据集与缓存影响很大，关注 p95 与差值是否随数据量增长；\n"
        "若差值明显变大，先确认 messages 的索引是否以 (tenant_id, created_at) 打头。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="RLS 查询开销对比（只读）")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--limit", type=int, default=50)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
