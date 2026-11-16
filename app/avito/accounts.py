from app.avito.exceptions import AccessDenied, InvalidRequest, InvalidResponse, ResourceNotFound
from app.avito.schemas import Account, ItemDetail, ListingsPage, parse


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise InvalidRequest("Expected a positive integer identifier")
    return value


def item_ids(values, maximum=200):
    if not 1 <= len(values) <= maximum or len(set(values)) != len(values):
        raise InvalidRequest(f"Expected 1..{maximum} distinct item IDs")
    return [positive_id(v) for v in values]


class AccountsAPI:
    def __init__(self, client):
        self.client = client

    async def account(self) -> Account:
        return parse(Account, await self.client.operation("getUserInfoSelf"))

    async def listings_page(self, page=1, per_page=25, status="active") -> ListingsPage:
        positive_id(page)
        if type(per_page) is not int or not 1 <= per_page <= 25:
            raise InvalidRequest("Client page size must be 1..25")
        if status not in {"active", "removed", "old", "blocked", "rejected"}:
            raise InvalidRequest("Unknown listing status")
        result = parse(
            ListingsPage,
            await self.client.operation(
                "getItemsInfo", params={"page": page, "per_page": per_page, "status": status}
            ),
        )
        if result.meta.page != page or result.meta.per_page != per_page:
            raise InvalidResponse("Unexpected listing pagination")
        if len(result.resources) > per_page or len({i.id for i in result.resources}) != len(
            result.resources
        ):
            raise InvalidResponse("Invalid listing page")
        return result

    async def listings(self, max_pages=20):
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise InvalidRequest("Invalid pagination bound")
        result, seen = [], set()
        for page in range(1, max_pages + 1):
            batch = await self.listings_page(page)
            for item in batch.resources:
                if item.id in seen:
                    raise InvalidResponse("Listing pagination repeated an item")
                seen.add(item.id)
                result.append(item)
            if len(batch.resources) < batch.meta.per_page:
                return result
        raise InvalidResponse("Listing page bound reached; result incomplete")

    async def item(self, account_id: int, item_id: int) -> ItemDetail:
        result = parse(
            ItemDetail,
            await self.client.operation(
                "getItemInfo",
                path_values={"user_id": positive_id(account_id), "item_id": positive_id(item_id)},
            ),
        )
        if result.status == "another_user":
            raise AccessDenied("Item belongs to a different account")
        if result.status == "not_found":
            raise ResourceNotFound("Item not found for this account")
        return result
