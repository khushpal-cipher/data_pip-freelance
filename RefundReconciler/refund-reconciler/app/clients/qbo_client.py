"""QuickBooks Online client with OAuth2 refresh-token handling.

Same lazy-refresh pattern as SyncGuard's QBOClient: refresh on 401 or once
the cached token goes stale, keep the rotated refresh token in memory for
the process lifetime.
"""

import time

import httpx

from app.config import Settings

TOKEN_URL = "https://oauth.quickbooks.api.intuit.com/oauth2/v1/tokens/bearer"


class QBOAuthError(Exception):
    pass


class QBOClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._access_token: str | None = None
        self._refresh_token = settings.qbo_refresh_token
        self._expires_at: float = 0.0
        self._client = httpx.Client(timeout=20.0)

    def _refresh_access_token(self) -> None:
        resp = self._client.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
            auth=(self.settings.qbo_client_id, self.settings.qbo_client_secret),
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise QBOAuthError(f"QBO token refresh failed: {resp.status_code} {resp.text}")
        data = resp.json()
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        self._expires_at = time.time() + int(data.get("expires_in", 3600)) - 60

    def _ensure_token(self) -> str:
        if not self._access_token or time.time() >= self._expires_at:
            self._refresh_access_token()
        return self._access_token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._ensure_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, json_body: dict) -> dict:
        url = f"{self.settings.qbo_base_url}/v3/company/{self.settings.qbo_realm_id}/{path}"
        resp = self._client.post(url, json=json_body, headers=self._headers())
        if resp.status_code == 401:
            self._refresh_access_token()
            resp = self._client.post(url, json=json_body, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.settings.qbo_base_url}/v3/company/{self.settings.qbo_realm_id}/{path}"
        resp = self._client.get(url, params=params, headers=self._headers())
        if resp.status_code == 401:
            self._refresh_access_token()
            resp = self._client.get(url, params=params, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def find_refund_receipt_by_doc_number(self, doc_number: str) -> dict | None:
        """Lookup-before-create recovery: if a prior sync crashed after QBO created the
        receipt but before the ledger row was marked synced, this finds it instead of
        creating a duplicate."""
        query = f"select * from RefundReceipt where DocNumber = '{doc_number}'"
        data = self._get("query", params={"query": query})
        rows = data.get("QueryResponse", {}).get("RefundReceipt", [])
        return rows[0] if rows else None

    def create_refund_receipt(self, refund_receipt: dict) -> dict:
        existing = self.find_refund_receipt_by_doc_number(refund_receipt["DocNumber"])
        if existing:
            return existing
        data = self._post("refundreceipt", refund_receipt)
        return data["RefundReceipt"]

    def close(self) -> None:
        self._client.close()
