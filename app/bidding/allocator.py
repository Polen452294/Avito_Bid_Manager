from app.bidding.optimizer import Recommendation


class BudgetAllocator:
    """Suggest one bounded zero-sum transfer supported by two complete weeks."""

    def __init__(self, minimum_contacts):
        self.minimum_contacts = minimum_contacts

    def allocate(self, entries, account_limit, now):
        if sum(item.daily_budget for item, _, _ in entries) > account_limit:
            return []
        eligible = [
            (item, recent, earlier)
            for item, recent, earlier in entries
            if item.active
            and item.promotion_model.value in {"CPX", "CPA_AUCTION"}
            and recent.days == earlier.days == 7
            and recent.start == earlier.end
            and recent.context is not None
            and recent.context == earlier.context
            and all(p.contacts >= self.minimum_contacts and p.spend for p in (recent, earlier))
        ]
        options = []
        for recipient, recent, earlier in eligible:
            if recipient.maximum_daily_budget is None:
                continue
            for donor, costly, previous in eligible:
                if (
                    recipient.item_id == donor.item_id
                    or recent.context[:2] != costly.context[:2]
                    or recent.context[3] != costly.context[3]
                    or (recent.start, recent.end) != (costly.start, costly.end)
                    or recent.cpl * 4 > costly.cpl * 3
                    or earlier.cpl * 4 > previous.cpl * 3
                ):
                    continue
                transfer = min(
                    donor.daily_budget // 10,
                    donor.daily_budget - donor.minimum_daily_budget,
                    recipient.maximum_daily_budget - recipient.daily_budget,
                    account_limit // 10,
                )
                if transfer:
                    options.append(
                        (costly.cpl - recent.cpl, transfer, donor, recipient, earlier, recent)
                    )
        if not options:
            return []
        _, transfer, donor, recipient, earlier, recent = max(
            options, key=lambda o: (o[0], o[1], -o[2].item_id, -o[3].item_id)
        )
        return [
            Recommendation(
                item_id=item.item_id,
                recommendation_type="REALLOCATE_BUDGET",
                current_value=item.daily_budget,
                suggested_value=item.daily_budget + delta,
                reason_code=f"TWO_WEEK_CPL_GAP_WITH_ITEM_{other.item_id}",
                calculated_at=now,
                analysis_period=(earlier.start, recent.end),
                data_quality="COMPARABLE",
                expected_effect="Перенос части лимита может улучшить CPL аккаунта. "
                "Предельная эффективность дополнительного бюджета неизвестна.",
            )
            for item, other, delta in ((donor, recipient, -transfer), (recipient, donor, transfer))
        ]
