"""QuickBooks Online client with OAuth2 refresh-token handling.

QBO access tokens expire after 1 hour; refresh tokens rotate on every use and
expire after 100 days of inactivity. This client refreshes lazily (on 401, or
proactively once the cached token is stale) and keeps the latest refresh
token in memory for the process lifetime. In production, persist the rotated
refresh token somewhere durable (e.g. back into the settings store) — logged
here as a TODO rather than wired to a secrets manager, since none was
specified.
"""

import time

import httpx

from app.config import Settings, get_settings

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

    def find_receipt_by_doc_number(self, doc_number: str) -> dict | None:
        """Lookup-before-create recovery: if a prior sync crashed after QBO
        created the receipt but before the ledger row was marked synced, this
        finds the existing receipt instead of creating a duplicate."""
        query = f"select * from SalesReceipt where DocNumber = '{doc_number}'"
        data = self._get("query", params={"query": query})
        rows = data.get("QueryResponse", {}).get("SalesReceipt", [])
        return rows[0] if rows else None

    def create_sales_receipt(self, receipt: dict) -> dict:
        existing = self.find_receipt_by_doc_number(receipt["DocNumber"])
        if existing:
            return existing
        data = self._post("salesreceipt", receipt)
        return data["SalesReceipt"]

    def create_refund_receipt(self, refund_receipt: dict) -> dict:
        existing = self.find_receipt_by_doc_number(refund_receipt["DocNumber"])
        if existing:
            return existing
        data = self._post("refundreceipt", refund_receipt)
        return data["RefundReceipt"]

    def count_sales_receipts(self, *, date_min: str, date_max: str) -> int:
        query = f"select count(*) from SalesReceipt where TxnDate >= '{date_min}' and TxnDate <= '{date_max}'"
        data = self._get("query", params={"query": query})
        return data.get("QueryResponse", {}).get("totalCount", 0)

    def close(self) -> None:
        self._client.close()


def _list_accounts_cli() -> None:  # pragma: no cover - operator utility
    settings = get_settings()
    client = QBOClient(settings)
    data = client._get("query", params={"query": "select Id, Name, AccountType from Account"})
    for acct in data.get("QueryResponse", {}).get("Account", []):
        print(f"{acct['Id']:>6}  {acct['AccountType']:<20}  {acct['Name']}")


if __name__ == "__main__":  # pragma: no cover
    import sys

    if "--list-accounts" in sys.argv:
        _list_accounts_cli()
    else:
        print("usage: python -m app.clients.qbo_client --list-accounts")
