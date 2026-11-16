import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.avito.accounts import AccountsAPI
from app.avito.auth import ClientCredentialsAuth
from app.avito.client import BASE_URL, AvitoClient
from app.avito.contracts import ContractRegistry
from app.avito.exceptions import (
    AccessDenied,
    AuthenticationError,
    AvitoError,
    ContractUnverified,
    InvalidResponse,
    ModelUnavailable,
    RateLimited,
    ResourceNotFound,
)
from app.avito.promotion import PromotionAPI
from app.avito.providers import AuctionBidProvider, CpxBidProvider
from app.avito.statistics import StatisticsAPI

logger = logging.getLogger(__name__)


def avito_client(settings, contracts):
    http = httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=20,
        limits=httpx.Limits(max_connections=2),
        follow_redirects=False,
    )
    auth = ClientCredentialsAuth(
        http,
        settings.avito_client_id.get_secret_value(),
        settings.avito_client_secret.get_secret_value(),
        contracts,
    )
    return AvitoClient(http, auth, contracts=contracts)


async def diagnose(settings, client=None) -> dict:
    report = await _diagnose(settings, client)
    results = {entry["name"]: entry for entry in report["checks"]}
    checks = {name: entry["status"] for name, entry in results.items()}
    items = report.get("selected_items", [])
    bid_reads = bool(items) and all(
        checks.get(f"item:{item}") == "AVAILABLE"
        and any(
            results.get(f"{model}:{item}", {}).get("current_bid_kopecks") is not None
            for model in ("cpx", "auction")
        )
        for item in items
    )
    report["capabilities"] = {
        "authorization": checks.get("authorization", "UNVERIFIED"),
        "account": (
            "ACCOUNT_MISMATCH"
            if report["status"] == "ACCOUNT_MISMATCH"
            else checks.get("account", "UNVERIFIED")
        ),
        "listings": checks.get("listings", "UNVERIFIED"),
        "bid_read": "AVAILABLE" if bid_reads else "UNVERIFIED",
        "bid_write": "UNVERIFIED",
        "finance": checks.get("spendings", "UNVERIFIED"),
        "live_positions": "UNAVAILABLE",
    }
    report["blocking_reasons"] = {
        name: status for name, status in report["capabilities"].items() if status != "AVAILABLE"
    }
    report["checked_at"] = datetime.now(UTC).isoformat()
    report["last_successful_check_at"] = (
        report["checked_at"]
        if not report["simulated"]
        and all(
            report["capabilities"][name] == "AVAILABLE"
            for name in ("authorization", "account", "listings", "bid_read", "finance")
        )
        else None
    )
    logger.info(
        "event=avito_diagnostic status=%s authorization=%s finance=%s simulated=%s",
        report["status"],
        report["capabilities"]["authorization"],
        report["capabilities"]["finance"],
        report["simulated"],
    )
    return report


async def _diagnose(settings, client=None) -> dict:
    report = {
        "mode": "DRY_RUN",
        "paid_operations": "DISABLED",
        "write_permissions": "NOT_TESTED",
        "status": "NOT_CONFIGURED",
        "checks": [],
        "simulated": bool(client and client.offline),
    }
    if client is None and not (settings.avito_client_id and settings.avito_client_secret):
        report["reason"] = "Set both AVITO_CLIENT_ID and AVITO_CLIENT_SECRET in local environment"
        return report
    owned_client = client is None
    if owned_client:
        try:
            contracts = ContractRegistry.from_official_exports(
                [Path(p) for p in settings.avito_official_spec_paths]
            )
        except (OSError, ValueError, KeyError, TypeError):
            report.update(
                status="INVALID_CONTRACT_EXPORT", reason="Cannot load official OpenAPI exports"
            )
            return report
        report["contract_evidence"] = "OWNER_SUPPLIED_EXPORT_MATCH"
        report["contracts_matched"] = sorted(contracts.matched)
        report["contracts_blocked"] = contracts.issues
        if "getAccessToken" not in contracts.matched:
            report.update(
                status="CONTRACT_BLOCKED", reason="OAuth contract is not matched; no network calls"
            )
            return report
        client = avito_client(settings, contracts)
    else:
        report["contract_evidence"] = (
            "HTTP_MOCK" if client.offline else "OWNER_SUPPLIED_EXPORT_MATCH"
        )
    blocked = None

    async def probe(name, action, summarize=lambda _: {}):
        nonlocal blocked
        if blocked:
            report["checks"].append({"name": name, "status": "SKIPPED", "reason": blocked})
            return None
        try:
            result = await action()
            details = summarize(result)
            status = details.pop("status", "AVAILABLE")
            report["checks"].append({"name": name, "status": status, **details})
            return result
        except AvitoError as exc:
            status = "ERROR"
            if isinstance(exc, AuthenticationError):
                status, blocked = "AUTH_FAILED", "Authentication failed"
            elif isinstance(exc, RateLimited):
                status, blocked = "RATE_LIMITED", "Server rate limit; remaining checks deferred"
            elif isinstance(exc, ContractUnverified):
                status = "CONTRACT_BLOCKED"
            elif isinstance(exc, AccessDenied):
                status = "ACCESS_DENIED"
            elif isinstance(exc, (ModelUnavailable, ResourceNotFound)):
                status = "UNAVAILABLE_OR_NOT_ELIGIBLE"
            elif isinstance(exc, InvalidResponse):
                status = "INVALID_RESPONSE"
            report["checks"].append({"name": name, "status": status, "reason": str(exc)})
            return None

    try:
        await probe("authorization", client.token_provider.token, lambda _: {})
        accounts = AccountsAPI(client)
        account = await probe("account", accounts.account, lambda a: {"account_id": a.id})
        if account is None:
            report["status"] = "BLOCKED"
            return report
        if settings.avito_expected_account_id and account.id != settings.avito_expected_account_id:
            report.update(
                status="ACCOUNT_MISMATCH", reason="Authenticated account differs from configured ID"
            )
            return report
        listings = await probe(
            "listings",
            accounts.listings_page,
            lambda r: {"count": len(r.resources), "page": r.meta.page},
        )
        selected = settings.avito_item_ids or (
            [i.id for i in listings.resources[:5]] if listings else []
        )
        report["selected_items"] = selected
        eligible = []
        for item_id in selected:
            result = await probe(
                f"item:{item_id}",
                lambda i=item_id: accounts.item(account.id, i),
                lambda r: {"item_status": r.status},
            )
            if result is not None:
                eligible.append(item_id)
        promotions = PromotionAPI(client)
        if eligible:
            await probe(
                "services",
                lambda: promotions.services(eligible),
                lambda r: {
                    "status": "PARTIAL"
                    if r.degraded or r.errors or {i.itemId for i in r.items or []} != set(eligible)
                    else "AVAILABLE",
                    "degraded": r.degraded,
                    "error_codes": [e.errorCode for e in r.errors or []],
                },
            )
            await probe(
                "cpx_promotions",
                lambda: promotions.cpx_promotions(eligible),
                lambda r: {
                    "status": "AVAILABLE"
                    if {i.itemID for i in r.items} == set(eligible)
                    else "PARTIAL",
                    "returned_items": [i.itemID for i in r.items],
                },
            )
            for item_id in eligible:
                await probe(
                    f"cpx:{item_id}",
                    lambda i=item_id: CpxBidProvider(client).current(i),
                    lambda r: {
                        "selected_type": r.selectedType,
                        "action_type_id": r.actionTypeID,
                        "manual_available": r.manual is not None,
                        "current_bid_kopecks": r.manual.bidPenny
                        if r.selectedType == "manual" and r.manual
                        else None,
                        "exclusive_minimum_kopecks": r.manual.minBidPenny if r.manual else None,
                        "maximum_kopecks": r.manual.maxBidPenny if r.manual else None,
                    },
                )
                await probe(
                    f"auction:{item_id}",
                    lambda i=item_id: AuctionBidProvider(client).current(i),
                    lambda r: {
                        "allowed_bid_count": len(r.availablePrices),
                        "allowed_bids_kopecks": sorted({p.pricePenny for p in r.availablePrices}),
                        "current_bid_kopecks": r.pricePenny,
                    },
                )
        else:
            report["checks"].append(
                {
                    "name": "item_promotions",
                    "status": "SKIPPED",
                    "reason": "No verified account-owned item IDs",
                }
            )
        day = datetime.now(ZoneInfo(settings.timezone)).date() - timedelta(days=1)
        stats = StatisticsAPI(client)
        if eligible:
            await probe(
                "counters",
                lambda: stats.counters(account.id, eligible, day, day),
                lambda r: {
                    "status": "PARTIAL"
                    if r.errors or {i.itemId for i in r.result.items} != set(eligible)
                    else "AVAILABLE",
                    "items_returned": len(r.result.items),
                    "errors_present": bool(r.errors),
                },
            )
        await probe(
            "analytics",
            lambda: stats.analytics(account.id, day, day, grouping="totals"),
            lambda r: {
                "metric_slugs": sorted({m.slug for g in r.result.groupings for m in g.metrics}),
                "status": "AVAILABLE" if r.result.groupings else "PARTIAL",
            },
        )
        await probe(
            "spendings",
            lambda: stats.spendings(account.id, day, day),
            lambda r: {
                "status": "AVAILABLE"
                if any(s.slug == "all" for g in r.result.groupings for s in g.spendings)
                else "PARTIAL",
                "timestamp": r.result.timestamp,
                "budget_input_reliable": False,
            },
        )
        report["status"] = (
            "READ_CHECKS_PASSED"
            if report["checks"] and all(c["status"] == "AVAILABLE" for c in report["checks"])
            else "PARTIAL"
        )
        return report
    finally:
        if owned_client:
            await client.aclose()
