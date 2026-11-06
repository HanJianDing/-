from enterprise.sales import SalesQuery, query_sales


def test_sales_totals_and_filter():
    totals = query_sales(SalesQuery())["totals"]
    assert totals == {"revenue_cents": 50000000, "cost_cents": 32900000,
                      "gross_profit_cents": 17100000, "orders": 450, "gross_margin_pct": 34.2}
    assert query_sales(SalesQuery(region="east"))["totals"]["revenue_cents"] == 27000000
    assert query_sales(SalesQuery(start_date="2026-09-01", end_date="2026-09-30"))["totals"]["gross_margin_pct"] is None
