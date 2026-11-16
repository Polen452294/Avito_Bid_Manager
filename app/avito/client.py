import asyncio
import json
import random
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime

import httpx

from app.avito.contracts import ContractRegistry
from app.avito.exceptions import (
    AccessDenied,
    AuthenticationError,
    CapabilityBlocked,
    InvalidRequest,
    InvalidResponse,
    RateLimited,
    ReadTimeout,
    ResourceNotFound,
    TemporaryUnavailable,
    UncertainWrite,
)

BASE_URL = "https://api.avito.ru"
WRITE_OPERATIONS = frozenset({"saveManualBid", "saveItemBids"})
READ_OPERATIONS = frozenset(
    {
        "getUserInfoSelf",
        "getItemsInfo",
        "getItemInfo",
        "getBids",
        "getPromotionsByItemIds",
        "getUserBids",
        "get_services_by_items_v2",
        "itemStatsShallow",
        "itemAnalytics",
        "accountSpendings",
    }
)


def decode_response(response: httpx.Response, *, empty_ok=False) -> dict:
    if empty_ok and not response.content:
        return {}
    try:

        def reject_constant(_):
            raise ValueError("Non-finite number")

        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate key")
                result[key] = value
            return result

        result = json.loads(
            response.content,
            parse_float=Decimal,
            parse_constant=reject_constant,
            object_pairs_hook=object_pairs,
        )
    except (ValueError, UnicodeError):
        raise InvalidResponse("Invalid API JSON response") from None
    if not isinstance(result, dict):
        raise InvalidResponse("Expected an API JSON object")
    return result


def retry_delay(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After")
    if not value:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                raise ValueError()
            return max(0, (date - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            raise RateLimited("Unparseable Retry-After; automatic retry blocked") from None


def raise_status(response: httpx.Response):
    status = response.status_code
    # Never expose response messages: servers/proxies may echo secrets or personal data.
    if status == 401:
        raise AuthenticationError("Authentication rejected (401)")
    if status == 403:
        raise AccessDenied("Access denied (403); account rights or product eligibility required")
    if status == 404:
        raise ResourceNotFound("Resource or API method not available (404)")
    if status == 429:
        raise RateLimited(retry_after=retry_delay(response))
    if status >= 500:
        raise TemporaryUnavailable(f"API temporarily unavailable ({status})")
    if not 200 <= status < 300:
        raise InvalidRequest(f"API rejected request ({status})")


class AvitoClient:
    """Schema-gated async transport. Live paid writes are disabled in Phase 2."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        token_provider,
        attempts: int = 3,
        sleep=asyncio.sleep,
        *,
        contracts: ContractRegistry | None = None,
    ):
        if not 1 <= attempts <= 5:
            raise ValueError("attempts must be 1..5")
        self.client, self.token_provider = client, token_provider
        self.attempts, self.sleep = attempts, sleep
        self.contracts = contracts or ContractRegistry()
        self.offline = False
        self._rate_lock = asyncio.Lock()
        self._next: dict[str, float] = {}

    @classmethod
    def for_contract_tests(cls, transport: httpx.MockTransport, token_provider=None, sleep=None):
        if type(transport) is not httpx.MockTransport:
            raise TypeError("Contract tests require exactly httpx.MockTransport")
        from app.avito.auth import ClientCredentialsAuth

        registry = ContractRegistry()
        registry.matched.update(registry.operations)
        http = httpx.AsyncClient(base_url=BASE_URL, transport=transport, trust_env=False)
        auth = token_provider or ClientCredentialsAuth(http, "test-id", "test-secret", registry)
        client = cls(http, auth, contracts=registry, sleep=sleep or asyncio.sleep)
        client.offline = True
        return client

    async def aclose(self):
        await self.client.aclose()

    async def _pace(self, name: str, rpm: int | None):
        async with self._rate_lock:
            delay = max(0, self._next.get(name, 0) - time.monotonic())
            if delay:
                await self.sleep(delay)
            if rpm:
                self._next[name] = time.monotonic() + 60 / rpm

    async def operation(self, name: str, *, path_values=None, params=None, body=None):
        op = self.contracts.require(name)
        if name == "getAccessToken":
            raise InvalidRequest("Use credential exchange for token requests")
        write = name in WRITE_OPERATIONS
        if write and not self.offline:
            raise CapabilityBlocked("Live paid writes are disabled")
        if not write and name not in READ_OPERATIONS:
            raise CapabilityBlocked("Operation is outside the read allowlist")
        path = op["path"]
        for key, value in (path_values or {}).items():
            if type(value) is not int or value <= 0:
                raise InvalidRequest("Path identifiers must be positive integers")
            path = path.replace("{" + key + "}", str(value))
        if "{" in path:
            raise InvalidRequest("Required path identifier missing")
        fallback_rates = {
            "getItemsInfo": 25,
            "getItemInfo": 500,
            "getUserBids": 200,
            "saveItemBids": 200,
        }
        return await self._request(
            op["method"].upper(),
            path,
            params=params,
            body=body,
            safe=not write,
            name=name,
            rpm=op.get("rate_per_minute") or fallback_rates.get(name),
        )

    async def read(self, path: str) -> dict:
        # Compatibility helper for Phase 1 transport tests; not used for production.
        if not re.fullmatch(r"/[A-Za-z0-9_/-]+", path) or path.startswith("//"):
            raise InvalidRequest("Only relative API paths allowed")
        if str(self.client.base_url).rstrip("/") == BASE_URL and not self.offline:
            raise CapabilityBlocked("Use schema-checked named operations for production")
        return await self._request("GET", path)

    async def _request(
        self, method, path, *, params=None, body=None, safe=True, name="transport-test", rpm=None
    ):
        refreshed = False
        force_refresh = False
        for attempt in range(self.attempts if safe else 1):
            token = await self.token_provider.token(force=force_refresh)
            force_refresh = False
            await self._pace(name, rpm)
            try:
                response = await self.client.request(
                    method,
                    path,
                    params=params,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=20,
                    follow_redirects=False,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                if not safe:
                    raise UncertainWrite(
                        "Write transport result unknown; reconcile first"
                    ) from None
                if attempt + 1 == self.attempts:
                    raise ReadTimeout("Read transport unavailable") from None
                await self.sleep(2**attempt + random.random())
                continue
            if response.headers.get("X-RateLimit-Remaining") == "0":
                self._next[name] = max(self._next.get(name, 0), time.monotonic() + 60)
            if response.status_code == 401 and safe and not refreshed:
                refreshed, force_refresh = True, True
                continue
            if not safe and response.status_code >= 500:
                raise UncertainWrite("Write server result unknown; reconcile first")
            if safe and (response.status_code == 429 or response.status_code >= 500):
                delay = retry_delay(response)
                if response.status_code == 429:
                    self._next[name] = max(
                        self._next.get(name, 0), time.monotonic() + max(delay, 60)
                    )
                if delay > 30:
                    raise RateLimited("Server requires deferred retry", retry_after=delay)
                if attempt + 1 < self.attempts:
                    await self.sleep(max(delay, 2**attempt) + random.random())
                    continue
            raise_status(response)
            try:
                return decode_response(response, empty_ok=not safe)
            except InvalidResponse:
                if not safe:
                    raise UncertainWrite("Write response malformed; reconcile first") from None
                raise
        raise AuthenticationError("Authorization retry exhausted")
