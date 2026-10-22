"""应用配置。

环境变量大小写不敏感：AI_API_KEY 自动映射到 ai_api_key。
生产环境缺失 JWT_SECRET 时启动即失败（设计文档第 8 章：失败要快且明确）。
"""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # 应用
    app_name: str = "智能客服 AI 系统"
    app_env: str = "development"
    log_level: str = "INFO"

    # AI 网关（OpenAI 兼容协议）
    ai_api_key: str = ""
    ai_base_url: str = ""
    ai_model: str = ""
    ai_embedding_api_key: str = ""
    ai_embedding_base_url: str = ""
    ai_embedding_model: str = ""
    ai_rerank_model: str = ""
    # 单次对话请求的超时（秒）：流式场景下这是"整体超时"，
    # 设得过长会让前端卡死等待，过短会截断长回答
    ai_timeout_seconds: float = 120.0

    # 对话
    # 组装 prompt 时携带的历史消息条数上限（控制上下文长度与成本）
    chat_history_limit: int = 20
    # 访客会话 TTL，与 cookie Max-Age 对齐（设计文档 5.5）
    guest_session_ttl_days: int = 30

    # 知识库与检索（设计文档 9.4）
    # 向量维度必须与嵌入模型一致，且建表时已固定——换模型需要迁移重建列
    # 向量维度必须与嵌入模型一致，且与库里已建的列匹配（迁移 0012 起为 2048）。
    # 换模型 = 换维度时必须有配套迁移重建列与索引，不能只改环境变量。
    ai_embedding_dim: int = 2048
    # 分块：窗口大小与重叠字符数；策略 by_heading / fixed_window
    chunk_strategy: str = "by_heading"
    chunk_size: int = 800
    chunk_overlap: int = 100
    # 两路召回各取 recall_k，RRF 融合后取 top_k
    retrieval_recall_k: int = 50
    retrieval_top_k: int = 5
    # 关闭后纯模型回答（便于排查检索问题），默认开启
    rag_enabled: bool = True

    # 语音（M5，设计文档 9.3）
    # ASR/TTS 各自独立的模型与超时：转写通常比合成慢，共用一个超时只能二选一
    ai_transcription_model: str = ""
    ai_tts_model: str = ""
    # 合成音色由供应商定义（OpenAI 兼容网关常见 alloy/echo/nova 等）
    ai_tts_voice: str = "alloy"
    # 密钥与端点可回退到 AI_*（与嵌入模型策略一致），模型必须单独指定
    ai_transcription_api_key: str = ""
    ai_transcription_base_url: str = ""
    ai_tts_api_key: str = ""
    ai_tts_base_url: str = ""
    ai_transcription_timeout_seconds: float = 60.0
    ai_tts_timeout_seconds: float = 30.0
    # 单条回复允许合成的最大字符数：超过则不合成（截断成"说到一半"体验更差）
    ai_tts_max_chars: int = 800
    # 单条语音消息时长上限（秒）：语音转写按分钟计费，是成本大头
    voice_max_seconds: int = 60
    # 音频上限（MB）：60s opus 约 0.5MB，留足 wav 无损余量
    voice_max_mb: int = 8
    # 音频访问 URL 有效期（秒）：<audio> 无法携带 Authorization，只能发短时令牌
    voice_audio_url_ttl_seconds: int = 3600

    # 图片消息（M8）
    # 单张上限 MB：base64 后体积约 ×1.37，太大既撑爆网关也会挤占上下文
    chat_image_max_mb: int = 3
    # 图片访问 URL 有效期（秒）：<img> 同样无法携带 Authorization
    asset_url_ttl_seconds: int = 3600
    # 一次请求最多把几张历史图片送给模型：每张都算 vision token，多张会明显加价
    chat_image_vision_limit: int = 1

    # 人工接管（M6，设计文档 5.3 / 5.4）
    # AI 答不了时在回答末尾输出 [[HANDOFF]]，系统据此转人工
    handoff_ai_marker_enabled: bool = True
    # 用户话里命中这些词直接建单（逗号分隔，空串即关闭关键词触发）
    handoff_keywords: str = "转人工,人工客服,找人工,要人,真人客服"
    # 坐席 SSE 心跳间隔（秒）：要比 Nginx 默认 60s 空闲断连小
    agent_stream_heartbeat_seconds: int = 20
    # guest 会话超过 TTL 先标记过期，再保留这么多天后连会话一起删除
    guest_cleanup_retention_days: int = 90

    # 告警（M7，设计文档 10）：留空则只写结构化日志，不推送到外部
    alert_webhook_url: str = ""

    # 限流（M8，设计文档 8）：所有阈值都是"每个窗口内允许的请求数"
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    # 默认兜底：按 IP 计数，防止单机刷接口
    rate_limit_default: int = 300
    # 登录/注册/刷新：按 IP 计数，防撞库与账号枚举
    rate_limit_auth: int = 10
    # 对话：按用户/访客计数（在端点依赖里执行，比 IP 精确）
    rate_limit_chat: int = 30
    # 渠道回调：平台会重推，阈值要高；按租户计数
    rate_limit_webhook: int = 600
    # 是否信任 X-Forwarded-For：只有确定应用端口仅反代可达时才可打开
    trust_proxy_headers: bool = False

    # 用量成本（M7，设计文档 10）
    # 价格按供应商账单填写，单位见字段名。默认 0 表示**只记用量不计成本**——
    # 不内置任何默认价目，避免看板上出现看着像真的、实际是猜的数字。
    # 币种由部署方决定，系统不做换算。
    #
    # 嵌入/重排按"字符数"计价而不是 token：本系统不含 tokenizer，拿不到精确
    # 的 embedding token 数，用字符数作为可复现的近似口径，价格由部署方按
    # 自己账单折算成每千字符。
    usage_price_prompt_per_1k: float = 0.0
    usage_price_completion_per_1k: float = 0.0
    usage_price_embedding_per_1k_chars: float = 0.0
    usage_price_rerank_per_1k_chars: float = 0.0
    usage_price_asr_per_minute: float = 0.0
    usage_price_tts_per_1k_chars: float = 0.0

    # 渠道接入（M4，设计文档 9.1）
    # 凭证缺省即该渠道自动禁用：不报错、不影响其他渠道
    wechat_app_id: str = ""
    wechat_app_secret: str = ""
    # 微信回调验签 token（公众平台后台"服务器配置"里自行填写）
    wechat_token: str = ""
    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""
    # 钉钉机器人发消息用的 robotCode；留空回退到 app_key
    dingtalk_robot_code: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    # 发件人；留空回退到 smtp_username
    smtp_from: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    # IMAP 轮询间隔（秒）：30-60s 是邮件服务商容忍的常见区间，过低易被判异常
    email_poll_seconds: int = 60
    # 单次轮询最多处理邮件数，避免突发大量邮件把 worker 打满
    email_poll_batch: int = 20

    # 文件存储：local（开发）/ minio（生产，M5 语音上线前补齐）
    storage_backend: str = "local"
    storage_local_dir: str = "var/uploads"
    # 单文件上传上限；解析在 worker 中进行，接口只负责落盘与入队
    upload_max_mb: int = 20

    # 数据库 / 缓存 / 对象存储（端口为宿主机错峰映射值，见设计文档第 14 章）
    # database_url：应用运行时连接（aics_app 角色，受 RLS 约束）
    # migration_database_url：迁移连接（超管，执行 DDL 与 RLS 策略）
    database_url: str = "postgresql+asyncpg://aics_app:aics_app@localhost:55432/aics"
    migration_database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:55432/aics"
    redis_url: str = "redis://localhost:56379/0"
    minio_endpoint: str = "localhost:9000"
    minio_root_user: str = "minioadmin"
    minio_root_password: str = "minioadmin"

    # 认证：所有环境必填。留空会把问题拖到签发令牌时才报 500
    # （HMAC key must not be empty），属典型"失败不够快"——启动阶段就拒绝
    jwt_secret: str
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 7

    @model_validator(mode="after")
    def _validate_jwt_secret(self) -> "Settings":
        if len(self.jwt_secret) < 32:
            raise ValueError(
                "JWT_SECRET 长度不足 32：PyJWT 对 HS256 有最小密钥长度要求。"
                '可用 python -c "import secrets; print(secrets.token_hex(32))" 生成。'
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """全局唯一配置入口；lru_cache 保证进程内单例。"""
    # jwt_secret 为必填字段，但值来自环境变量/.env（pydantic-settings 运行时注入），
    # 静态检查无法感知，故显式忽略 call-arg
    return Settings()  # type: ignore[call-arg]
