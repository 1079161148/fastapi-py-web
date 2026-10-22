"""统一异常与错误响应。

错误体固定为 {code, message, detail}（设计文档第 7 章）。
5xx 记日志并隐藏内部细节；4xx 直接返回可操作信息。
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """业务异常基类。

    code: 前端分支用的稳定标识，禁止随时间漂移
    message: 面向用户/前端的可读信息
    status_code: HTTP 状态码
    detail: 附加上下文（字段级校验错误等）
    expose: 5xx 是否原样返回 code/message。默认 False（隐藏内部细节）；
        仅当错误信息本身对使用者可操作时（如"模型未配置，请设置 AI_API_KEY"）置 True
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        detail: dict[str, Any] | None = None,
        *,
        expose: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail
        self.expose = expose


class NotFoundError(AppError):
    def __init__(self, resource: str = "资源") -> None:
        super().__init__(code="NOT_FOUND", message=f"{resource}不存在", status_code=404)


class ConflictError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(code="CONFLICT", message=message, status_code=409)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("app_error", code=exc.code, message=exc.message, detail=exc.detail)
            if not exc.expose:
                # 5xx 只进日志，不外泄内部细节
                return JSONResponse(
                    status_code=exc.status_code,
                    content={
                        "code": "INTERNAL_ERROR",
                        "message": "服务内部错误",
                        "detail": None,
                    },
                )
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "detail": exc.detail},
        )
