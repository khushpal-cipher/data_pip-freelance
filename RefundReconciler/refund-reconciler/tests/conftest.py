import os
import pathlib

import pytest

TEST_DB_PATH = pathlib.Path(__file__).parent / "test.db"
TEST_DB_PATH.unlink(missing_ok=True)

# Must run at conftest IMPORT time (module scope), not inside a fixture:
# pytest imports conftest.py before collecting test modules, but a fixture
# body only runs once tests execute — by then app.config.get_settings()
# would already be @lru_cache'd from whatever the real .env file contains.
os.environ.update(
    DATABASE_URL=f"sqlite:///{TEST_DB_PATH}",
    REDIS_URL="redis://localhost:6379/1",
    CELERY_ALWAYS_EAGER="true",
    STRIPE_WEBHOOK_SECRET="test-stripe-secret",
    SHOPIFY_WEBHOOK_SECRET="test-shopify-secret",
    QBO_INCOME_ACCOUNT_ID="79",
    LOG_LEVEL="WARNING",
)


@pytest.fixture(autouse=True, scope="session")
def _test_env():
    yield
    TEST_DB_PATH.unlink(missing_ok=True)


@pytest.fixture
def session():
    from app.db import engine, get_session, init_db
    from sqlmodel import SQLModel

    init_db()
    s = get_session()
    yield s
    s.close()
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
