from pathlib import Path

from app.avito.accounts import AccountsAPI
from app.avito.contracts import ContractRegistry
from app.avito.diagnostics import avito_client
from app.avito.exceptions import AccessDenied, AvitoError, ResourceNotFound
from app.avito.providers import AuctionBidProvider, CpxBidProvider


async def lookup_item(settings, item_id):
    if not (settings.avito_client_id and settings.avito_client_secret):
        return None, None, False
    contracts = ContractRegistry.from_official_exports(
        [Path(path) for path in settings.avito_official_spec_paths]
    )
    if not {"getAccessToken", "getUserInfoSelf", "getItemInfo"} <= contracts.matched:
        return None, None, False
    client = avito_client(settings, contracts)
    try:
        accounts = AccountsAPI(client)
        account = await accounts.account()
        if settings.avito_expected_account_id and account.id != settings.avito_expected_account_id:
            raise AccessDenied("Unexpected account")
        # Explicit ownership errors propagate to the wizard.
        try:
            await accounts.item(account.id, item_id)
        except ResourceNotFound:
            raise AccessDenied("Item ownership could not be established") from None
        title = None
        if "getItemsInfo" in contracts.matched:
            try:
                listings = await accounts.listings_page()
                title = next(
                    (item.title for item in listings.resources if item.id == item_id), None
                )
            except AvitoError:
                pass
        bids = []
        for provider in (CpxBidProvider(client), AuctionBidProvider(client)):
            try:
                bids.append(await provider.get_bid_info(item_id))
            except AvitoError:
                continue
        return title, bids[0] if len(bids) == 1 else None, True
    finally:
        await client.aclose()
