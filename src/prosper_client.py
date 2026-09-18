import httpx

from src.config import PROSPER_API_KEY, PROSPER_BASE_URL


class ProsperClient:
    def __init__(self) -> None:
        self.base_url = PROSPER_BASE_URL.rstrip("/")
        self.headers = {"X-Api-Key": PROSPER_API_KEY}

    async def health(self) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{self.base_url}/health", headers=self.headers)
            response.raise_for_status()
            return response.json() if response.content else {"status": "ok"}

    async def list_call_logs(self, limit: int = 5) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{self.base_url}/call-logs",
                headers=self.headers,
                params={"limit": limit},
            )
            response.raise_for_status()
            return response.json()

    async def get_target(self, target_id: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{self.base_url}/targets/{target_id}",
                headers=self.headers,
            )
            response.raise_for_status()
            return response.json()
