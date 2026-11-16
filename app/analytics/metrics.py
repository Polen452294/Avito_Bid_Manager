from decimal import Decimal


def ratio(numerator, denominator):
    return Decimal(numerator) / denominator if numerator is not None and denominator else None


def metrics(spent_kopecks: int, views: int, unique_contacts: int | None) -> dict:
    if (
        type(spent_kopecks) is not int
        or spent_kopecks < 0
        or views < 0
        or (unique_contacts is not None and unique_contacts < 0)
    ):
        raise ValueError("Non-negative integer money and counts required")
    return {
        "cpl_rubles": ratio(spent_kopecks, unique_contacts * 100 if unique_contacts else None),
        "cpv_rubles": ratio(spent_kopecks, views * 100),
        "conversion_percent": ratio(
            unique_contacts * 100 if unique_contacts is not None else None, views
        ),
    }
