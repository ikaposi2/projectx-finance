from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "projectX-finance"
    environment: str = "dev"
    database_url: str | None = None
    db_user: str = Field(default="projectx", validation_alias="PX_DB_USER")
    db_password: str = Field(default="change-me-now", validation_alias="PX_DB_PASSWORD")
    db_host: str = Field(default="postgres", validation_alias="PX_DB_HOST")
    db_port: int = Field(default=5432, validation_alias="PX_DB_PORT")
    db_name: str = Field(default="finance", validation_alias="PX_DB_NAME")
    jwt_secret: str = "dev-only-change-me"
    jwt_algorithm: str = "HS256"
    nats_url: str = Field(default="nats://nats:4222", validation_alias="NATS_URL")
    nats_stream: str = "PROJECTX_EVENTS"
    nats_consumer: str = "finance-time-entries"
    nats_filter_subject: str = "projectx.events.time.>"
    internal_rate_eur: float = Field(default=75.0, validation_alias="INTERNAL_RATE_EUR")
    reserve_target_eur: float = Field(default=50_000.0, validation_alias="RESERVE_TARGET_EUR")
    milestone_threshold_eur: float = Field(default=30_000.0, validation_alias="MILESTONE_THRESHOLD_EUR")
    default_vat_rate: float = Field(default=21.0, validation_alias="DEFAULT_VAT_RATE")
    project_service_url: str = Field(default="http://project:8000", validation_alias="PROJECT_SERVICE_URL")
    customer_service_url: str = Field(default="http://customer:8000", validation_alias="CUSTOMER_SERVICE_URL")
    identity_service_url: str = Field(default="http://identity:8000", validation_alias="IDENTITY_SERVICE_URL")
    time_service_url: str = Field(default="http://time:8000", validation_alias="TIME_SERVICE_URL")
    company_legal_name: str = Field(default="Platform BV", validation_alias="COMPANY_LEGAL_NAME")
    company_vat_id: str = Field(default="", validation_alias="COMPANY_VAT_ID")
    company_coc_number: str = Field(default="", validation_alias="COMPANY_COC_NUMBER")
    company_bank_account: str = Field(default="", validation_alias="COMPANY_BANK_ACCOUNT")
    company_address: str = Field(default="", validation_alias="COMPANY_ADDRESS")
    company_invoice_email: str = Field(default="", validation_alias="COMPANY_INVOICE_EMAIL")
    archive_root: str = Field(default="/var/archive", validation_alias="ARCHIVE_ROOT")
    otel_exporter_otlp_endpoint: str | None = None
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
