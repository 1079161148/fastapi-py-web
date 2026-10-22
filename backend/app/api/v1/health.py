"""健康检查。

/health 为存活探针：不依赖外部服务，保证进程本身可被探测。
/health/ready 为就绪探针：验证数据库连通，未就绪时返回 503，
由编排系统（Docker/K8s）据此决定是否放流量。
"""

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import get_session

logger = get_logger(__name__)
router = APIRouter(tags=["health"])
settings = get_settings()


@router.get("/health")
async def liveness() -> dict[str, str]:
    return {"status": "ok", "app": settings.app_name}


@router.get("/health/ready")
async def readiness(
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        # 就绪探针需要把任何连接失败转为 503，异常信息只进日志
        logger.warning("readiness_failed", error=str(exc))
        response.status_code = 503
        return {"status": "unavailable", "app": settings.app_name}
    return {"status": "ok", "app": settings.app_name}
