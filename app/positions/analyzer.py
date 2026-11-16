from collections import defaultdict
from datetime import timedelta
from statistics import mean, median

from app.positions.validation import context_key, usable


class PositionAnalyzer:
    def analyze(self, item, observations, now, max_age=timedelta(minutes=60)):
        groups = defaultdict(dict)
        weights = {context_key(item.item_id, query): query.weight for query in item.queries}
        for row in observations:
            key = context_key(row.item_id, row.search_query, row.region, row.filters)
            if key in weights and usable(row, now, max_age):
                groups[key, row.provider, row.position_type][row.checked_at] = row.position

        reports = []
        for (key, provider, kind), samples in groups.items():
            positions = [samples[time] for time in sorted(samples)]
            tail = positions[-3:]
            trend = "STABLE_OR_NOISY"
            if len(tail) == 3:
                a, b, c = tail
                if a <= b <= c and c - a >= 2:
                    trend = "WORSENING"
                elif a >= b >= c and a - c >= 2:
                    trend = "IMPROVING"
            reports.append(
                dict(
                    context=key,
                    provider=provider,
                    kind=kind,
                    weight=weights[key],
                    mean=mean(positions),
                    median=median(positions),
                    trend=trend,
                )
            )
        return sorted(reports, key=lambda report: report["weight"], reverse=True)
