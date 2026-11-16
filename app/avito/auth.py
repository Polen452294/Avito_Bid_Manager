import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel, Field, SecretStr, ValidationError

from app.avito.contracts import ContractRegistry
from app.avito.exceptions import AuthenticationError, InvalidResponse, ReadTimeout


class TokenResponse(BaseModel):
    access_token: SecretStr
    expires_in: int = Field(gt=0)


class TokenCache:
    """Single-flight client credentials cache; secrets live in memory only."""

    def __init__(self, exchange: Callable[[], Awaitable[TokenResponse]], clock=time.monotonic):
        self.exchange, self.clock = exchange, clock
        self._token: SecretStr | None = None
        self._expires = 0
        self._lock = asyncio.Lock()

    async def token(self, force=False) -> str:
        async with self._lock:
            if force or self._token is None or self.clock() >= self._expires:
                response = await self.exchange()
                self._token = response.access_token
                self._expires = self.clock() + max(0, response.expires_in - 30)
            return self._token.get_secret_value()


class ClientCredentialsAuth(TokenCache):
    """Own-account grant. Renewal repeats client_credentials, not refresh_token."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        client_id: str,
        client_secret: str,
        contracts: ContractRegistry,
    ):
        self.client = client
        self.client_id = SecretStr(client_id)
        self.client_secret = SecretStr(client_secret)
        self.contracts = contracts
        super().__init__(self.exchange)

    async def exchange(self) -> TokenResponse:
        from app.avito.client import decode_response, raise_status

        self.contracts.require("getAccessToken")
        if not all(s.get_secret_value().strip() for s in (self.client_id, self.client_secret)):
            raise AuthenticationError("Empty client credentials")
        try:
            response = await self.client.post(
                "/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id.get_secret_value(),
                    "client_secret": self.client_secret.get_secret_value(),
                },
                timeout=20,
                follow_redirects=False,
            )
        except httpx.TransportError:
            raise ReadTimeout("OAuth transport unavailable") from None
        raise_status(response)
        data = decode_response(response)
        if (
            not isinstance(data.get("token_type"), str)
            or data["token_type"].lower() != "bearer"
            or not isinstance(data.get("access_token"), str)
            or not data["access_token"].strip()
        ):
            raise InvalidResponse("Invalid OAuth token response")
        try:
            return TokenResponse.model_validate(data)
        except ValidationError:
            raise InvalidResponse("Invalid OAuth token lifetime") from None
