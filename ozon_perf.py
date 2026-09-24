import os
import time
import httpx
from dotenv import load_dotenv

load_dotenv()

# ============ PERFORMANCE API (реклама) ============
BASE_URL = "https://api-performance.ozon.ru"
CLIENT_ID = os.getenv("OZON_PERF_CLIENT_ID")
CLIENT_SECRET = os.getenv("OZON_PERF_CLIENT_SECRET")

# ============ SELLER API (баланс) ============
SELLER_BASE_URL = "https://api-seller.ozon.ru"
SELLER_CLIENT_ID = os.getenv("OZON_SELLER_CLIENT_ID")
SELLER_API_KEY = os.getenv("OZON_SELLER_API_KEY")

_token_cache = {"access_token": None, "expires_at": 0}

BID_DIVIDER = 1_000_000  # Ozon хранит ставки в микроединицах


# ---------- PERFORMANCE: токен ----------
async def get_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BASE_URL}/api/client/token",
            json={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 1800)
    return data["access_token"]


async def get_campaigns() -> list:
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{BASE_URL}/api/client/campaign", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        for key in ("list", "campaigns", "result", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return []


async def activate_campaign(campaign_id: int) -> dict:
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{BASE_URL}/api/client/campaign/{campaign_id}/activate", headers=headers)
        resp.raise_for_status()
        return resp.json()


async def deactivate_campaign(campaign_id: int) -> dict:
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{BASE_URL}/api/client/campaign/{campaign_id}/deactivate", headers=headers)
        resp.raise_for_status()
        return resp.json()


async def get_daily_stats(date_from: str, date_to: str) -> list:
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{BASE_URL}/api/client/statistics/daily/json",
            headers=headers,
            params={"dateFrom": date_from, "dateTo": date_to},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        for key in ("rows", "result", "items", "list"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return []


async def get_product_stats(date_from: str, date_to: str, campaign_ids: list = None) -> list:
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}"}
    params = {"dateFrom": date_from, "dateTo": date_to}
    if campaign_ids:
        params["campaignIds"] = campaign_ids
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{BASE_URL}/api/client/statistics/campaign/product/json",
            headers=headers,
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        for key in ("rows", "result", "items", "list"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return []


async def get_campaign_skus(campaign_id: int) -> list:
    """Список SKU кампании с текущими ставками (bid в микроединицах)."""
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}"}
    params = {"page": 1, "limit": 1000}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{BASE_URL}/api/client/campaign/{campaign_id}/v2/products",
            headers=headers,
            params=params,
        )
        if resp.status_code != 200:
            print(f"=== get_campaign_skus: Status {resp.status_code} ===")
            print(f"Текст: {resp.text[:300]}")
            print("=========================================")
            return []

        data = resp.json()
        items = []
        if isinstance(data, list):
            items = data
        else:
            for key in ("products", "items", "list", "rows", "result", "skus"):
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break

        normalized = []
        for it in items:
            sku = (
                it.get("sku")
                or it.get("SKU")
                or it.get("id")
                or it.get("productId")
            )
            title = (
                it.get("title")
                or it.get("name")
                or it.get("productName")
                or str(sku)
            )
            raw_bid = (
                it.get("bid")
                or it.get("rate")
                or it.get("currentBid")
                or it.get("bidValue")
                or 0
            )
            try:
                bid_rub = float(str(raw_bid).replace(",", ".")) / BID_DIVIDER
            except Exception:
                bid_rub = 0.0

            if sku is None:
                continue
            normalized.append({
                "sku": str(sku),
                "title": title,
                "bid": round(bid_rub, 2),
                "raw": it,
            })

        return normalized


async def set_campaign_bid(campaign_id: int, sku: str, bid_rub: float) -> dict:
    """Изменить ставку для SKU. Принимает рубли, отправляет в микроединицах."""
    token = await get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    bid_micro = int(round(bid_rub * BID_DIVIDER))
    bid_str = str(bid_micro)

    payload = {
        "bids": [
            {
                "sku": str(sku),
                "bid": bid_str,
            }
        ]
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{BASE_URL}/api/client/campaign/{campaign_id}/products",
            headers=headers,
            json=payload,
        )

        print(f"=== set_campaign_bid ===")
        print(f"URL: {resp.url}")
        print(f"Body: {payload}")
        print(f"Status: {resp.status_code}")
        try:
            print(f"JSON: {resp.json()}")
        except Exception:
            print(f"Text: {resp.text[:300]}")
        print("========================")

        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}


# ---------- SELLER API: БАЛАНС ----------
async def get_balance(date_from: str = None, date_to: str = None) -> dict:
    """
    Получить отчёт о балансе через Seller API.
    POST /v1/finance/balance
    Максимальный период — 30 дней.
    """
    from datetime import date, timedelta

    if not date_to:
        date_to = date.today().isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=29)).isoformat()

    headers = {
        "Client-Id": SELLER_CLIENT_ID,
        "Api-Key": SELLER_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"date_from": date_from, "date_to": date_to}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{SELLER_BASE_URL}/v1/finance/balance",
            headers=headers,
            json=payload,
        )

        print("=== get_balance ===")
        print(f"URL: {resp.url}")
        print(f"Status: {resp.status_code}")
        try:
            print(f"JSON: {resp.json()}")
        except Exception:
            print(f"Text: {resp.text[:500]}")
        print("===================")

        resp.raise_for_status()
        return resp.json()
