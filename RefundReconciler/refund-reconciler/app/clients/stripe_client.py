import httpx

from app.config import Settings

BASE_URL = "https://api.stripe.com/v1"


class StripeClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.Client(
            base_url=BASE_URL,
            auth=(settings.stripe_api_key, ""),
            timeout=15.0,
        )

    def get_charge(self, charge_id: str) -> dict:
        resp = self._client.get(f"/charges/{charge_id}")
        resp.raise_for_status()
        return resp.json()

    def get_balance_transaction(self, balance_transaction_id: str) -> dict:
        """Stripe stamps the FX rate used for the payout on the balance transaction —
        this is the authoritative "rate at the time" source for cross-currency charges."""
        resp = self._client.get(f"/balance_transactions/{balance_transaction_id}")
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()
