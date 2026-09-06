import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    database_url: str = os.getenv("DATABASE_URL", "postgresql://localhost/leadbridge")
    allowed_origins: list[str] = [
        o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",") if o.strip()
    ]
    hubspot_token: str | None = os.getenv("HUBSPOT_TOKEN") or None
    resend_api_key: str | None = os.getenv("RESEND_API_KEY") or None
    resend_from: str = os.getenv("RESEND_FROM", "onboarding@resend.dev")
    sentry_dsn: str | None = os.getenv("SENTRY_DSN") or None
    rate_limit_per_minute: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "10"))
    max_delivery_attempts: int = int(os.getenv("MAX_DELIVERY_ATTEMPTS", "3"))


settings = Settings()
