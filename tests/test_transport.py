import httpx
import pytest

from app.avito.auth import TokenCache, TokenResponse
from app.avito.client import AvitoClient
from app.avito.exceptions import AccessDenied, AvitoError, CapabilityBlocked


async def no_sleep(_):
    pass


async def test_token_cache_and_expiry():
    calls, clock = [], [0]

    async def exchange():
        calls.append(1)
        return TokenResponse(access_token="test-secret", expires_in=60)

    cache = TokenCache(exchange, clock=lambda: clock[0])
    assert await cache.token() == "test-secret"
    await cache.token()
    assert len(calls) == 1
    clock[0] = 31
    await cache.token()
    assert len(calls) == 2


@pytest.mark.parametrize("codes", [(401, 200), (429, 200), (503, 200)])
async def test_read_retries(codes):
    responses = iter(codes)
    calls = []

    async def exchange():
        calls.append(1)
        return TokenResponse(access_token="test", expires_in=300)

    async with httpx.AsyncClient(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(next(responses), json={"ok": True})),
    ) as http:
        client = AvitoClient(http, TokenCache(exchange), sleep=no_sleep)
        assert await client.read("/test") == {"ok": True}
    assert len(calls) == (2 if codes[0] == 401 else 1)


@pytest.mark.parametrize(
    ("code", "error"),
    [(403, AccessDenied), (404, CapabilityBlocked), (500, AvitoError), (429, AvitoError)],
)
async def test_errors(code, error):
    async def exchange():
        return TokenResponse(access_token="test", expires_in=300)

    async with httpx.AsyncClient(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(code, json={})),
    ) as http:
        with pytest.raises(error):
            await AvitoClient(http, TokenCache(exchange), sleep=no_sleep).read("/test")
