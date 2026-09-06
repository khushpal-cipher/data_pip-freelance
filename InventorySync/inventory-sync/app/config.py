import os

CHANNELS = ["shopify", "marketplace"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "changeme")
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://localhost:5432/inventorysync",
)
SENTRY_DSN = os.environ.get("SENTRY_DSN")
