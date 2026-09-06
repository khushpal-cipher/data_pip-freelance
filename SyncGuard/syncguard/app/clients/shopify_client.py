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

    def count_orders(self, *, created_at_min: str, created_at_max: str) -> int:
        resp = self._client.get(
            "/orders/count.json",
            params={"created_at_min": created_at_min, "created_at_max": created_at_max, "status": "any"},
        )
        resp.raise_for_status()
        return resp.json()["count"]

    def list_order_ids(self, *, created_at_min: str, created_at_max: str) -> list[str]:
        resp = self._client.get(
            "/orders.json",
            params={
                "created_at_min": created_at_min,
                "created_at_max": created_at_max,
                "status": "any",
                "fields": "id",
                "limit": 250,
            },
        )
        resp.raise_for_status()
        return [str(o["id"]) for o in resp.json()["orders"]]

    def close(self) -> None:
        self._client.close()
