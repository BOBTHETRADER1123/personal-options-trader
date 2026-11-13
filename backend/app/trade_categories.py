"""One mutually exclusive trading category; watchlists are tools, not accounts."""
CATEGORIES=('stocks','fno','commodities')
LABELS={'stocks':'Stocks','fno':'F&O','commodities':'Commodities'}

def instrument_category(contract):
    if str(contract.get('exchange','')).upper()=='MCX' or str(contract.get('segment','')).lower()=='mcx_fo':
        return 'commodities'
    if contract.get('option_type')=='EQ' or contract.get('instrument_type')=='stock':
        return 'stocks'
    return 'fno'

def trade_category(trade):
    legs=trade.get('legs') or []
    # Historical mixed-venue records remain whole and appear exactly once.
    # New mixed-category baskets are rejected by the execution validation.
    values={instrument_category(leg) for leg in legs}
    if 'commodities' in values:return 'commodities'
    if 'stocks' in values or trade.get('instrument_type')=='stock':return 'stocks'
    return 'fno'

def category_summary(trades):
    fields=('net_pnl','realized_pnl','unrealized_pnl','gross_pnl','charges')
    result={key:{'label':LABELS[key],**{field:0.0 for field in fields},'active_trades':0,'closed_trades':0,'trade_count':0} for key in CATEGORIES}
    for trade in trades:
        target=result[trade.get('category') if trade.get('category') in CATEGORIES else trade_category(trade)]
        for field in fields:target[field]+=float(trade.get(field) or 0)
        target['active_trades']+=trade.get('status') in ('open','pending','OPEN','PENDING')
        target['closed_trades']+=trade.get('status') in ('closed','CLOSED')
        target['trade_count']+=1
    for target in result.values():
        for field in fields:target[field]=round(target[field],2)
    result['total']={'label':'Total',**{field:round(sum(target[field] for target in result.values()),2) for field in fields},
                     **{field:sum(target[field] for target in result.values()) for field in ('active_trades','closed_trades','trade_count')}}
    return result
