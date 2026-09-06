import httpx

from app.config import Settings


class ShopifyClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = f"https://{settings.shopify_store_domain}/admin/api/{settings.shopify_api_version}"
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"X-Shopify-Access-Token": settings.shopify_admin_token},
            timeout=15.0,
        )

    def get_order(self, order_id: str) -> dict:
        resp = self._client.get(f"/orders/{order_id}.json")
        resp.raise_for_status()
        return resp.json()["order"]

    def close(self) -> None:
        self._client.close()
