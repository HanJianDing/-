from .sales import SalesQuery

SNAPSHOT_DATE = '2026-10-31'
ROWS = [
    {'region': 'east', 'product': 'A', 'on_hand': 180, 'reserved': 40, 'safety_stock': 100, 'unit_cost_cents': 60000},
    {'region': 'east', 'product': 'B', 'on_hand': 60, 'reserved': 20, 'safety_stock': 80, 'unit_cost_cents': 90000},
    {'region': 'south', 'product': 'A', 'on_hand': 100, 'reserved': 20, 'safety_stock': 60, 'unit_cost_cents': 70000},
    {'region': 'south', 'product': 'B', 'on_hand': 220, 'reserved': 20, 'safety_stock': 70, 'unit_cost_cents': 70000},
    {'region': 'north', 'product': 'B', 'on_hand': 30, 'reserved': 10, 'safety_stock': 50, 'unit_cost_cents': 80000},
]


def query_inventory(query: SalesQuery):
    rows = []
    if query.start_date.isoformat() <= SNAPSHOT_DATE <= query.end_date.isoformat():
        for item in ROWS:
            if query.region != 'all' and query.region != item['region']:
                continue
            available = item['on_hand'] - item['reserved']
            rows.append({**item, 'available': available,
                         'shortage': max(0, item['safety_stock'] - available),
                         'stock_value_cents': item['on_hand'] * item['unit_cost_cents']})
    totals = {key: sum(r[key] for r in rows) for key in
              ['on_hand', 'reserved', 'available', 'safety_stock', 'shortage', 'stock_value_cents']}
    totals['safety_coverage_pct'] = round(totals['available'] / totals['safety_stock'] * 100, 2) if totals['safety_stock'] else None
    return {'source': 'simulated-inventory-v1', 'snapshot_date': SNAPSHOT_DATE,
            'currency': 'CNY', 'amount_unit': 'cent', 'quantity_unit': 'unit',
            'query': query.model_dump(mode='json'), 'rows': rows,
            'totals': totals,
            'risk_count': sum(r['shortage'] > 0 for r in rows)}
