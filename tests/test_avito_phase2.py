import copy
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.avito.accounts import AccountsAPI
from app.avito.client import AvitoClient
from app.avito.contracts import ContractRegistry, snapshot
from app.avito.diagnostics import diagnose
from app.avito.exceptions import (
    AccessDenied,
    AuthenticationError,
    CapabilityBlocked,
    ContractUnverified,
    InvalidRequest,
    InvalidResponse,
    ModelUnavailable,
    RateLimited,
    ReadTimeout,
    ResourceNotFound,
    TemporaryUnavailable,
    UncertainWrite,
    VerificationFailed,
)
from app.avito.journal import OperationJournal
from app.avito.promotion import PromotionAPI
from app.avito.providers import AuctionBidProvider, CpxBidProvider
from app.avito.statistics import StatisticsAPI
from app.config import Settings


@pytest.fixture
def responses():
    return json.loads((Path(__file__).parent / "fixtures/avito/responses.json").read_text("utf-8"))


class Server:
    def __init__(self, responses):
        self.data = copy.deepcopy(responses)
        self.calls = []
        self.write_behavior = "success"

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        if path == "/token":
            fields = parse_qs(request.content.decode())
            assert fields == {
                "client_id": ["test-id"],
                "client_secret": ["test-secret"],
                "grant_type": ["client_credentials"],
            }
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            return httpx.Response(200, json=self.data["token"])
        assert request.headers["authorization"] == "Bearer test-access-token"
        if path == "/cpxpromo/1/setManual" or (
            path == "/auction/1/bids" and request.method == "POST"
        ):
            if self.write_behavior == "reject":
                return httpx.Response(400, json={"message": "test-secret must never be logged"})
            if self.write_behavior not in {"not_applied", "timeout_before"}:
                payload = json.loads(request.content)
                if path.startswith("/cpxpromo"):
                    assert set(payload) == {"itemID", "actionTypeID", "bidPenny", "limitPenny"}
                    assert payload["actionTypeID"] == 5 and payload["limitPenny"] == 10000
                    self.data["cpx"]["manual"]["bidPenny"] = payload["bidPenny"]
                else:
                    item = payload["items"][0]
                    assert item["pricePenny"] in (2000, 2200)
                    self.data["auction"]["items"][0]["pricePenny"] = item["pricePenny"]
            if self.write_behavior.startswith("timeout"):
                raise httpx.ReadTimeout("test-secret response timeout", request=request)
            if self.write_behavior == "server_error_after":
                return httpx.Response(503)
            if self.write_behavior == "malformed_after":
                return httpx.Response(200, text="<html>invalid</html>")
            return httpx.Response(200)
        routes = {
            "/core/v1/accounts/self": "account",
            "/core/v1/items": "listings",
            "/core/v1/accounts/10/items/1/": "item",
            "/cpxpromo/1/getBids/1": "cpx",
            "/cpxpromo/1/getPromotionsByItemIds": "cpx_promotions",
            "/auction/1/bids": "auction",
            "/promotion/v2/items/services/get": "services",
            "/stats/v1/accounts/10/items": "counters",
            "/stats/v2/accounts/10/items": "analytics",
            "/stats/v2/accounts/10/spendings": "spendings",
        }
        assert path in routes, f"Unexpected route {path}"
        return httpx.Response(200, json=self.data[routes[path]])


async def no_wait(_):
    pass


@pytest.fixture
async def api(responses):
    server = Server(responses)
    client = AvitoClient.for_contract_tests(httpx.MockTransport(server), sleep=no_wait)
    yield client, server
    await client.aclose()


async def test_oauth_account_list_detail_and_cache(api):
    client, server = api
    accounts = AccountsAPI(client)
    assert (await accounts.account()).id == 10
    assert [i.id for i in await accounts.listings()] == [1]
    assert (await accounts.item(10, 1)).status == "active"
    assert sum(r.url.path == "/token" for r in server.calls) == 1
    client.token_provider._expires = 0
    await accounts.account()
    assert sum(r.url.path == "/token" for r in server.calls) == 2


@pytest.mark.parametrize(
    "code,error",
    [
        (401, AuthenticationError),
        (403, AccessDenied),
        (404, ResourceNotFound),
        (429, RateLimited),
        (500, TemporaryUnavailable),
        (503, TemporaryUnavailable),
    ],
)
async def test_real_shaped_http_errors(responses, code, error):
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(200, json=responses["token"])
            if request.url.path == "/token"
            else (httpx.Response(code, json={"error": {"message": "test-secret", "code": code}}))
        )

    client = AvitoClient.for_contract_tests(httpx.MockTransport(handler), sleep=no_wait)
    try:
        with pytest.raises(error) as exc:
            await AccountsAPI(client).account()
        assert "test-secret" not in str(exc.value)
        if code == 401:
            assert sum(r.url.path == "/token" for r in calls) == 2
        else:
            assert len(calls) <= 4
    finally:
        await client.aclose()


async def test_401_refresh_success(responses):
    count = 0

    def handler(request):
        nonlocal count
        if request.url.path == "/token":
            return httpx.Response(200, json=responses["token"])
        count += 1
        return httpx.Response(401) if count == 1 else httpx.Response(200, json=responses["account"])

    client = AvitoClient.for_contract_tests(httpx.MockTransport(handler), sleep=no_wait)
    try:
        assert (await AccountsAPI(client).account()).id == 10
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "text", ["{}", "[]", "null", "not-json", '{"id":false}', '{"id":1,"id":2}', '{"id":NaN}']
)
async def test_malformed_account(responses, text):
    def handler(r):
        return (
            httpx.Response(200, json=responses["token"])
            if r.url.path == "/token"
            else (httpx.Response(200, text=text))
        )

    client = AvitoClient.for_contract_tests(httpx.MockTransport(handler), sleep=no_wait)
    try:
        with pytest.raises(InvalidResponse):
            await AccountsAPI(client).account()
    finally:
        await client.aclose()


async def test_read_timeout_is_bounded(responses):
    reads = []

    def handler(request):
        if request.url.path == "/token":
            return httpx.Response(200, json=responses["token"])
        reads.append(1)
        raise httpx.ReadTimeout("secret", request=request)

    client = AvitoClient.for_contract_tests(httpx.MockTransport(handler), sleep=no_wait)
    try:
        with pytest.raises(ReadTimeout):
            await AccountsAPI(client).account()
        assert len(reads) == 3
    finally:
        await client.aclose()


@pytest.mark.parametrize("provider", [CpxBidProvider, AuctionBidProvider])
async def test_bid_read_and_audited_wire_write(api, database, provider):
    client, server = api
    adapter = provider(client, OperationJournal(database[1]))
    assert (await adapter.get_bid_info(1)).bid == 2000
    result = await adapter.set_bid(1, 2200, "write-1")
    assert result.confirmed_bid == 2200 and result.simulated
    assert await adapter.set_bid(1, 2200, "write-1") == result
    assert (
        sum(
            r.method == "POST"
            and r.url.path == adapter.client.contracts.operations[adapter.write_operation]["path"]
            for r in server.calls
        )
        == 1
    )


@pytest.mark.parametrize(
    "provider,bid",
    [
        (CpxBidProvider, 100),
        (CpxBidProvider, 2101),
        (CpxBidProvider, 5100),
        (AuctionBidProvider, 2100),
        (AuctionBidProvider, 2200.0),
    ],
)
async def test_invalid_bid_no_dispatch(api, database, provider, bid):
    client, server = api
    adapter = provider(client, OperationJournal(database[1]))
    with pytest.raises(InvalidRequest):
        await adapter.set_bid(1, bid, "bad-bid")
    assert not any(
        r.url.path.endswith("setManual") or (r.url.path == "/auction/1/bids" and r.method == "POST")
        for r in server.calls
    )


@pytest.mark.parametrize("behavior", ["timeout_after", "server_error_after", "malformed_after"])
async def test_uncertain_write_reconciled_once(api, database, behavior):
    client, server = api
    server.write_behavior = behavior
    result = await CpxBidProvider(client, OperationJournal(database[1])).set_bid(
        1, 2200, "uncertain"
    )
    assert result.confirmed_bid == 2200
    assert sum(r.url.path.endswith("setManual") for r in server.calls) == 1


@pytest.mark.parametrize("behavior", ["timeout_before", "not_applied"])
async def test_uncertain_write_blocks_after_restart(api, database, behavior):
    client, server = api
    server.write_behavior = behavior
    journal = OperationJournal(database[1])
    with pytest.raises(VerificationFailed):
        await CpxBidProvider(client, journal).set_bid(1, 2200, "unknown")
    restarted = CpxBidProvider(client, OperationJournal(database[1]))
    with pytest.raises(UncertainWrite):
        await restarted.set_bid(1, 2200, "unknown")
    with pytest.raises(UncertainWrite):
        await restarted.set_bid(1, 2200, "different-id")
    assert sum(r.url.path.endswith("setManual") for r in server.calls) == 1


async def test_rejected_write_is_not_success(api, database):
    client, server = api
    server.write_behavior = "reject"
    journal = OperationJournal(database[1])
    with pytest.raises(InvalidRequest):
        await CpxBidProvider(client, journal).set_bid(1, 2200, "rejected")
    row, _ = await journal.details("rejected")
    assert row.status == "REJECTED" and row.confirmed_bid is None


async def test_missing_model(api):
    client, server = api
    server.data["cpx"]["selectedType"] = "auto"
    server.data["auction"]["items"] = []
    with pytest.raises(ModelUnavailable):
        await CpxBidProvider(client).get_bid_info(1)
    with pytest.raises(ModelUnavailable):
        await AuctionBidProvider(client).get_bid_info(1)


async def test_live_write_guard_even_with_matched_contract(api, database):
    client, server = api
    client.offline = False
    with pytest.raises(CapabilityBlocked):
        await CpxBidProvider(client, OperationJournal(database[1])).set_bid(1, 2200, "forbidden")
    with pytest.raises(CapabilityBlocked):
        await client.operation("saveManualBid", body={})
    assert not server.calls


async def test_degraded_services_and_missing_counters(api):
    client, server = api
    server.data["services"]["degraded"] = True
    assert (await PromotionAPI(client).services([1])).degraded
    day = datetime.now(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1)
    server.data["counters"]["result"]["items"][0]["stats"] = [{"date": day.isoformat()}]
    result = await StatisticsAPI(client).counters(10, [1], day, day)
    assert result.result.items[0].stats[0].uniqContacts is None


async def test_decimal_json_money_is_exact(responses):
    def handler(request):
        if request.url.path == "/token":
            return httpx.Response(200, json=responses["token"])
        return httpx.Response(
            200,
            text='{"result":{"groupings":[{"date":"2026-11-15",'
            '"type":"day","spendings":[{"slug":"all","value":12345.0}]}],"timestamp":1}}',
        )

    client = AvitoClient.for_contract_tests(httpx.MockTransport(handler), sleep=no_wait)
    try:
        day = datetime.now(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1)
        result = await StatisticsAPI(client).spendings(10, day, day)
        assert result.result.groupings[0].spendings[0].value == Decimal("12345.0")
        assert isinstance(result.result.groupings[0].spendings[0].value, Decimal)
    finally:
        await client.aclose()


async def test_diagnostics_only_reads(api):
    client, server = api
    report = await diagnose(Settings(_env_file=None, avito_item_ids=[1]), client)
    assert report["status"] == "READ_CHECKS_PASSED"
    assert report["write_permissions"] == "NOT_TESTED" and report["simulated"]
    assert report["capabilities"]["bid_read"] == "AVAILABLE"
    assert report["capabilities"]["bid_write"] == "UNVERIFIED"
    assert report["last_successful_check_at"] is None
    assert not any(
        r.url.path.endswith(("setManual", "setAuto", "remove"))
        or (r.url.path == "/auction/1/bids" and r.method == "POST")
        for r in server.calls
    )
    assert "test-access-token" not in json.dumps(report)
    assert "Fixture account" not in json.dumps(report)


async def test_missing_credentials():
    report = await diagnose(
        Settings(_env_file=None, avito_client_id=None, avito_client_secret=None)
    )
    assert report["status"] == "NOT_CONFIGURED"


async def test_credentials_without_official_contract_no_network():
    report = await diagnose(
        Settings(
            _env_file=None,
            avito_client_id="fake",
            avito_client_secret="secret",
            avito_official_spec_paths=[],
        )
    )
    assert report["status"] == "CONTRACT_BLOCKED"
    assert "secret" not in json.dumps(report)


def test_official_export_match_and_drift(tmp_path):
    # Synthetic export used to exercise matching, NOT evidence of an official verification.
    expected = snapshot()["operations"]["getUserInfoSelf"]
    op = copy.deepcopy(expected["signature"])
    document = {"openapi": "3.0.0", "paths": {expected["path"]: {"get": op}}}
    path = tmp_path / "synthetic-export.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    registry = ContractRegistry.from_official_exports([path])
    assert registry.require("getUserInfoSelf")
    with pytest.raises(ContractUnverified):
        registry.require("getAccessToken")
    op["security"] = []
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ContractUnverified):
        ContractRegistry.from_official_exports([path]).require("getUserInfoSelf")
