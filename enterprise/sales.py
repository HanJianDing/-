from datetime import date
from typing import Literal
from pydantic import BaseModel, model_validator


class SalesQuery(BaseModel):
    start_date: date = date(2026, 10, 1)
    end_date: date = date(2026, 10, 31)
    region: Literal["all", "east", "south", "north"] = "all"

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if (self.end_date - self.start_date).days > 366:
            raise ValueError("query range must not exceed 367 days")
        return self


# Fixed fixtures, amounts in integer CNY cents. These are explicitly simulated data.
ROWS = [
    {"date": "2026-10-01", "region": "east", "product": "A", "revenue_cents": 12000000, "cost_cents": 7200000, "orders": 120},
    {"date": "2026-10-08", "region": "south", "product": "A", "revenue_cents": 9000000, "cost_cents": 6300000, "orders": 90},
    {"date": "2026-10-15", "region": "north", "product": "B", "revenue_cents": 6000000, "cost_cents": 4800000, "orders": 60},
    {"date": "2026-10-22", "region": "east", "product": "B", "revenue_cents": 15000000, "cost_cents": 9000000, "orders": 100},
    {"date": "2026-10-28", "region": "south", "product": "B", "revenue_cents": 8000000, "cost_cents": 5600000, "orders": 80},
]


def query_sales(query: SalesQuery) -> dict:
    rows = [r.copy() for r in ROWS if query.start_date <= date.fromisoformat(r["date"]) <= query.end_date
            and (query.region == "all" or r["region"] == query.region)]
    revenue = sum(r["revenue_cents"] for r in rows)
    cost = sum(r["cost_cents"] for r in rows)
    return {"source": "simulated-sales-v1", "currency": "CNY", "amount_unit": "cent",
            "query": query.model_dump(mode="json"), "rows": rows,
            "totals": {"revenue_cents": revenue, "cost_cents": cost,
                       "gross_profit_cents": revenue - cost, "orders": sum(r["orders"] for r in rows),
                       "gross_margin_pct": round((revenue - cost) / revenue * 100, 2) if revenue else None}}
