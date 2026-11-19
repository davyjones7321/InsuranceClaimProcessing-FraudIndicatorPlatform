"""Application configuration loaded from environment variables."""

from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    ENV: Literal["development", "test", "staging", "production"] = "development"
    DEBUG: bool = False
    APP_NAME: str = "Insurance Claim Processing Platform"
    APP_VERSION: str = "0.1.0"

    # Database
    DATABASE_URL: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/insurance_claims"
    )
    MIGRATOR_DATABASE_URL: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/insurance_claims"
    )
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # Business timezone and currency
    BUSINESS_TIMEZONE: str = "Asia/Kolkata"
    CONFIGURED_CURRENCY: str = "INR"

    # Security & Auth
    SECRET_KEY: str = Field(
        default="dev-secret-key-must-be-changed-in-production-min-32-bytes-long",
        description="Used for session cookies and CSRF tokens",
    )
    AUDIT_HMAC_KEY: str = Field(
        default="dev-audit-hmac-key-must-be-changed-in-production-32b",
        description="Key used for HMAC-SHA256 audit chaining",
    )
    SESSION_COOKIE_NAME: str = "claim_session"
    SESSION_MAX_AGE_SECONDS: int = 86400  # 24 hours
    PASSWORD_MIN_LENGTH: int = 12
    LOGIN_MAX_FAILED_ATTEMPTS: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 15

    # Separation of duties & thresholds
    SEPARATION_OF_DUTIES_ENABLED: bool = True
    MANAGER_APPROVAL_THRESHOLD: Decimal = Decimal("500000.00")
    MIN_DECISION_REMARKS_LENGTH: int = 20

    # Upload configuration
    UPLOAD_MAX_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB
    ALLOWED_UPLOAD_MIME_TYPES: list[str] = [
        "application/pdf",
        "image/jpeg",
        "image/png",
    ]
    UPLOAD_STORAGE_PATH: str = "storage/documents"

    # Ruleset
    ACTIVE_RULESET_VERSION: str = "v1"
    ENGINE_VERSION: str = "1.0.0"

    @field_validator("DEBUG")
    @classmethod
    def validate_debug_in_production(cls, v: bool, info: ValidationInfo) -> bool:
        values = info.data
        env = values.get("ENV")
        if env == "production" and v is True:
            raise ValueError("DEBUG must not be True in production environment")
        return v

    @field_validator("BUSINESS_TIMEZONE")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as e:
            raise ValueError(f"Invalid timezone: {v}") from e
        return v

    @field_validator("SECRET_KEY", "AUDIT_HMAC_KEY")
    @classmethod
    def validate_secret_strength(cls, v: str, info: ValidationInfo) -> str:
        values = info.data
        env = values.get("ENV")
        if env in ("staging", "production"):
            if len(v) < 32 or "dev-" in v:
                raise ValueError(
                    f"{info.field_name} must be a strong secret of at least 32 characters in {env}"
                )
        return v


settings = Settings()
