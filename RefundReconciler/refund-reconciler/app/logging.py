import logging
import re
import sys

import structlog

from app.config import get_settings

# Field names whose values get masked before anything hits stdout/Sentry.
_REDACT_KEYS = {
    "stripe_webhook_secret",
    "shopify_webhook_secret",
    "qbo_client_secret",
    "qbo_refresh_token",
    "shopify_admin_token",
    "authorization",
    "signature",
    "api_key",
    "token",
}


def _redact_processor(logger, method_name, event_dict):
    for key in list(event_dict.keys()):
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _redact_processor,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(**bind):
    return structlog.get_logger(**bind)


def redact_payload(payload: dict) -> dict:
    """Shallow-redact a webhook payload dict before it's logged or stored in logs."""
    out = {}
    for k, v in payload.items():
        if k.lower() in _REDACT_KEYS or re.search(r"secret|token|password|authoriz", k, re.I):
            out[k] = "***REDACTED***"
        else:
            out[k] = v
    return out
