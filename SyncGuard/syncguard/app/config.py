from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "sqlite:///./syncguard.db"

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
    qbo_fee_account_id: str = ""
    qbo_clearing_account_id: str = ""

    # Worker
    worker_poll_seconds: int = 15
    max_sync_attempts: int = 6
    reconcile_hour_utc: int = 2

    # Alerting
    sentry_dsn: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_email_from: str = ""
    alert_email_to: str = ""

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
