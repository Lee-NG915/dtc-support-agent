"""Environment-backed settings with a cached accessor."""

import json
import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed runtime configuration.

    Secrets stay in SecretStr so accidental str() / logs do not leak keys.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "testing", "production"] = "development"
    app_name: str = "adaptive-rag-cs"
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"

    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    # 采样温度覆盖：None 表示沿用各调用方传入值。
    # 部分 OpenAI 兼容模型（如 Kimi Code kimi-for-coding）只允许 temperature=1，
    # 此时在 .env 设 DEEPSEEK_TEMPERATURE=1，由 Provider 统一覆盖。
    deepseek_temperature: float | None = None
    # 推理模型（如 Kimi Code kimi-for-coding）适配：
    # reasoning_effort 非空时透传给 API（low/high/max），low 可降低延迟与推理 token 消耗。
    deepseek_reasoning_effort: str = ""
    # max_tokens 下限：推理模型的 reasoning_tokens 也计入 max_tokens，
    # 业务节点按非推理模型给的 256~700 预算会被推理耗尽，导致 JSON 截断。
    # 0 表示不调整；使用推理模型时建议 2048。
    deepseek_min_max_tokens: int = 0
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 3

    embedding_api_key: SecretStr = SecretStr("")
    embedding_base_url: str = ""
    embedding_model: str = "bge-small-zh-v1.5"
    embedding_dim: int = 512
    local_embedding_enabled: bool = True
    local_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    local_embedding_device: str = ""
    local_embedding_force: bool = False

    cross_encoder_enabled: bool = True
    cross_encoder_model: str = "BAAI/bge-reranker-base"
    cross_encoder_device: str = ""
    cross_encoder_max_length: int = 512
    cross_encoder_force: bool = False

    database_url: str = "sqlite+aiosqlite:///./data/local/app.db"
    redis_url: str = ""
    qdrant_url: str = ""
    qdrant_collection: str = "cs_knowledge"

    retrieval_top_k_simple: int = 3
    retrieval_top_k_complex: int = 8
    rrf_k: int = 60
    rerank_top_n: int = 5
    max_retrieval_hops: int = 3

    token_budget: int = 128_000
    compress_soft_ratio: float = 0.50
    compress_hard_ratio: float = 0.70
    auto_compact_ratio: float = 0.85

    max_agent_steps: int = 8
    max_tool_calls: int = 6

    rate_limit_per_minute: int = 60
    retrieval_cache_ttl_seconds: int = 60
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    admin_api_token: SecretStr = SecretStr("")
    knowledge_dir: str = "knowledge"
    unstructured_enabled: bool = True
    unstructured_strategy: str = "auto"
    unstructured_ocr_languages: str = "chi_sim+eng"
    unstructured_force: bool = False
    mcp_enabled: bool = True
    mcp_remote_url: str = ""

    # After-sales feature boundary. Disabled by default so the upstream demo
    # keeps its existing behaviour until an operator explicitly opts in.
    after_sales_enabled: bool = False
    business_connector: Literal["fake", "epig"] = "fake"
    epig_base_url: str = ""
    epig_service_token: SecretStr = SecretStr("")
    epig_allowed_markets: str = ""
    epig_timeout_seconds: float = Field(default=5.0, ge=0.5, le=30.0)
    epig_max_connections: int = Field(default=10, ge=1, le=20)

    # Server-trusted SKU -> policy product category map. This is the only
    # source of category scope for after-sales evidence; request fields are
    # never trusted. Empty means no mapping (production stays needs_info).
    after_sales_sku_category_map: str = ""
    after_sales_approval_ttl_seconds: int = Field(default=14400, ge=60, le=86400)
    after_sales_sync_batch_limit: int = Field(default=50, ge=1, le=200)

    # DTC-2c worker limits. The worker is a separate process over the same
    # database; these values bound one cycle and one external attempt.
    after_sales_worker_markets: str = ""
    after_sales_worker_batch_limit: int = Field(default=10, ge=1, le=100)
    after_sales_worker_poll_interval_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    after_sales_worker_lease_seconds: int = Field(default=60, ge=10, le=3600)
    after_sales_worker_operation_timeout_seconds: float = Field(default=20.0, ge=1.0, le=300.0)
    after_sales_worker_lookup_budget: int = Field(default=10, ge=1, le=100)
    after_sales_worker_write_budget: int = Field(default=3, ge=1, le=10)
    after_sales_worker_retry_base_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    after_sales_worker_retry_max_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)
    after_sales_worker_reconciliation_max_seconds: int = Field(default=86400, ge=60, le=604800)

    @field_validator("after_sales_sku_category_map")
    @classmethod
    def validate_sku_category_map(cls, value: str) -> str:
        if not value.strip():
            return ""
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("after_sales_sku_category_map must be valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("after_sales_sku_category_map must be a JSON object")
        for sku, category in decoded.items():
            if (
                not isinstance(sku, str)
                or not sku.strip()
                or len(sku) > 128
                or not isinstance(category, str)
                or not category.strip()
                or len(category) > 64
            ):
                raise ValueError("SKU category map entries must be non-empty short strings")
        return value

    @property
    def sku_category_map(self) -> dict[str, str]:
        if not self.after_sales_sku_category_map.strip():
            return {}
        decoded = json.loads(self.after_sales_sku_category_map)
        return {sku.strip(): category.strip() for sku, category in decoded.items()}

    @property
    def llm_configured(self) -> bool:
        return bool(self.deepseek_api_key.get_secret_value())

    @property
    def embedding_configured(self) -> bool:
        return bool(self.embedding_api_key.get_secret_value() and self.embedding_base_url)

    @property
    def local_embedding_configured(self) -> bool:
        """True when production will attempt to load the local dense model."""
        return self.local_embedding_enabled and (
            self.app_env != "testing" or self.local_embedding_force
        )

    @property
    def cross_encoder_configured(self) -> bool:
        """True when production path will attempt to load the Cross-Encoder.

        Tests stay on lexical fallback unless CROSS_ENCODER_FORCE is set.
        """
        return self.cross_encoder_enabled and (
            self.app_env != "testing" or self.cross_encoder_force
        )

    @property
    def unstructured_configured(self) -> bool:
        """True when ingest will attempt Unstructured partition.

        Tests stay on markdown heading split unless UNSTRUCTURED_FORCE is set.
        """
        return self.unstructured_enabled and (self.app_env != "testing" or self.unstructured_force)

    @property
    def redis_configured(self) -> bool:
        return bool(self.redis_url)

    @property
    def qdrant_configured(self) -> bool:
        return bool(self.qdrant_url)

    @property
    def use_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def epig_configured(self) -> bool:
        return bool(
            self.epig_base_url.strip()
            and self.epig_service_token.get_secret_value().strip()
            and self.epig_allowed_markets.strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Test runs must never read a developer's real .env (which may contain
    # production secrets). The pytest bootstrap sets APP_ENV before import.
    if os.getenv("APP_ENV", "").strip().lower() == "testing":
        return Settings(_env_file=None)  # type: ignore[call-arg]
    return Settings()


def clear_settings_cache() -> None:
    """Used by tests after monkeypatching environment variables."""
    get_settings.cache_clear()
