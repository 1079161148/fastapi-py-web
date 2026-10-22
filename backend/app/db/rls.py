"""RLS 会话钩子（设计文档 9.2）。

事务开启时执行 SET LOCAL app.tenant_id，使行级安全策略
（current_setting('app.tenant_id', true)::uuid）在当前事务内生效；
SET LOCAL 随事务结束自动失效，天然防止连接复用导致的租户串号。

注意：
- 必须注册在 engine.sync_engine 上（SQLAlchemy 异步引擎的事件入口）
- SET LOCAL 只在事务内有效，因此挂在 "begin" 事件而非 "connect" 事件
- 未设置租户上下文时不执行 SET：策略按 NULL 比较，行全部不可见（fail-closed）
"""

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.tenant import get_current_tenant_id


def register_rls_hooks(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "begin")
    def _set_tenant_context(conn: Connection) -> None:
        tenant_id = get_current_tenant_id()
        if tenant_id is not None:
            # set_config(name, value, true) 即 SET LOCAL 语义（事务级）。
            # 不用 exec_driver_sql 传参：asyncpg 方言不认 %s 占位符；
            # 而 tenant_id 来自 uuid.UUID（仅十六进制与连字符），无注入面。
            conn.exec_driver_sql(f"SELECT set_config('app.tenant_id', '{tenant_id}', true)")


__all__ = ["register_rls_hooks", "Engine"]
