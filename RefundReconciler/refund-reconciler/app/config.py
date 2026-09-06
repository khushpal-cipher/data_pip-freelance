from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "sqlite:///./refund_reconciler.db"

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"
    celery_always_eager: bool = False

    # Stripe
    stripe_webhook_secret: str = ""
    stripe_api_key: str = ""
    stripe_webhook_tolerance_seconds: int = 300

    # Shopify
    shopify_webhook_secret: str = ""
    shopify_store_domain: str = ""
    shopify_admin_token: str = ""
    shopify_api_version: str = "2024-10"

    # QuickBooks Online
    qbo_client_id: str = ""
    qbo_client_secret: str = ""
    qbo_realm_id: str = ""
    qbo_refresh_token: str = ""
    qbo_environment: str = "sandbox"
    qbo_income_account_id: str = ""
    qbo_fx_gain_loss_account_id: str = ""

    # Retry policy
    max_task_attempts: int = 6
    retry_backoff_base_seconds: int = 30
    retry_backoff_max_seconds: int = 21600

    # Alerting
    sentry_dsn: str = ""

    # App
    environment: str = "development"
    log_level: str = "INFO"

    @property
    def qbo_base_url(self) -> str:
        if self.qbo_environment == "production":
            return "https://quickbooks.api.intuit.com"
        return "https://sandbox-quickbooks.api.intuit.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
