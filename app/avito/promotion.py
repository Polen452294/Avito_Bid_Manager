from app.avito.accounts import item_ids
from app.avito.exceptions import InvalidResponse
from app.avito.schemas import CpxPromotions, Services, parse


class PromotionAPI:
    def __init__(self, client):
        self.client = client

    async def cpx_promotions(self, ids):
        requested = item_ids(ids)
        result = parse(
            CpxPromotions,
            await self.client.operation("getPromotionsByItemIds", body={"itemIDs": requested}),
        )
        returned = [i.itemID for i in result.items]
        if len(set(returned)) != len(returned) or not set(returned) <= set(requested):
            raise InvalidResponse("Unexpected CPX item IDs")
        # Missing IDs do not mean the product is unavailable or the bid is zero.
        return result

    async def services(self, ids):
        requested = item_ids(ids, maximum=100)
        result = parse(
            Services,
            await self.client.operation("get_services_by_items_v2", body={"itemIds": requested}),
        )
        returned = [i.itemId for i in result.items or []]
        errors = [i.itemId for i in result.errors or []]
        if len(set(returned)) != len(returned) or not set(returned + errors) <= set(requested):
            raise InvalidResponse("Unexpected promotion item IDs")
        # Keep degraded/errors; never replace missing services with an empty reliable list.
        return result
