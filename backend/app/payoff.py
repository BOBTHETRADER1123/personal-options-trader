"""Piecewise-linear expiry payoff. Prices/quantities are in contract units."""
import math

def leg_payoff(leg, settlement):
    direction = 1 if str(leg['side']).upper() == 'BUY' else -1
    quantity = leg.get('open_lots', leg.get('lots',1))*leg['lot_size']*float(leg.get('price_multiplier') or 1)
    intrinsic = max(0, settlement-leg['strike']) if leg['option_type']=='CE' else max(0,leg['strike']-settlement)
    return direction*(intrinsic-leg.get('entry_price',leg.get('price',0)))*quantity

def strategy_risk(legs, spot=None, charges=0):
    if not math.isfinite(float(charges)) or charges<0:raise ValueError('Charges must be finite and nonnegative.')
    if spot is not None and (not math.isfinite(float(spot)) or spot<=0):raise ValueError('Spot must be finite and positive.')
    for leg in legs:
        if leg.get('side') not in ('BUY','SELL') or leg.get('option_type') not in ('CE','PE'):raise ValueError('Invalid option type or direction.')
        for key in ('strike','lot_size'):
            if not math.isfinite(float(leg[key])) or float(leg[key])<=0:raise ValueError('Strike and lot size must be finite and positive.')
        for key in ('lots','open_lots'):
            if key in leg and (not math.isfinite(float(leg[key])) or float(leg[key])<0 or not float(leg[key]).is_integer()):raise ValueError('Lots must be nonnegative integers.')
        entry=leg.get('entry_price',leg.get('price',0))
        if not math.isfinite(float(entry)) or float(entry)<0:raise ValueError('Entry prices must be finite and nonnegative.')
        factor=leg.get('price_multiplier')
        if factor is not None and (not math.isfinite(float(factor)) or float(factor)<=0):raise ValueError('Invalid monetary multiplier.')
    legs = [l for l in legs if l.get('enabled',True) and l.get('open_lots',l.get('lots',1))>0]
    if not legs:
        return {'supported':False,'reason':'Select an open leg.','points':[]}
    if len({(l.get('underlying'),l.get('expiry')) for l in legs}) != 1:
        # A calendar cannot have one combined expiry payoff. Its open legs still
        # require an honest standalone margin estimate; zero would falsely imply
        # that an uncovered short needs no capital. Never grant a cross-expiry
        # hedge credit. For different underlyings, one supplied spot cannot price
        # them all: use each leg's explicit reference, falling back to its strike.
        one_underlying=len({l.get('underlying') for l in legs})==1
        standalone=0.0
        premium=0.0
        for leg in legs:
            quantity=leg.get('open_lots',leg.get('lots',1))*leg['lot_size']*float(leg.get('price_multiplier') or 1)
            entry=float(leg.get('entry_price',leg.get('price',0)))
            direction=1 if leg['side']=='BUY' else -1
            premium-=direction*entry*quantity
            if leg['side']=='BUY':
                standalone+=entry*quantity
            else:
                reference=spot if one_underlying and spot is not None else leg.get('spot')
                try:reference=float(reference)
                except (TypeError,ValueError):reference=float(leg['strike'])
                if not math.isfinite(reference) or reference<=0:reference=float(leg['strike'])
                standalone+=reference*.2*quantity
        return {'supported':False,'reason':'Expiry payoff requires the same underlying and expiry for all legs.',
                'points':[],'net_premium':round(premium,2),'estimated_initial_margin':round(standalone,2),
                'estimated_margin':round(standalone,2),'hedge_benefit':0.0,'risk_reward':None,
                'assumptions':'Combined expiry payoff is unsupported. Standalone margin adds long premiums and 20% of each short leg reference notional, using the supplied same-underlying spot or each leg spot/strike reference. No cross-expiry or cross-underlying hedge benefit is assumed. This heuristic is not broker SPAN or blocked margin.'}
    strikes = sorted({float(l['strike']) for l in legs})
    spot = spot or sum(strikes)/len(strikes)
    def value(s): return sum(leg_payoff(l,s) for l in legs)-charges
    knots = sorted({0.0,*strikes})
    values = [value(s) for s in knots]
    upper_slope = sum((1 if l['side']=='BUY' else -1)*l.get('open_lots',l.get('lots',1))*l['lot_size']*float(l.get('price_multiplier') or 1) for l in legs if l['option_type']=='CE')
    max_profit = None if upper_slope>0 else max(values)
    max_loss = None if upper_slope<0 else max(0,-min(values))
    breakevens=[]
    for i in range(len(knots)-1):
        x1,x2=knots[i:i+2]; y1,y2=values[i:i+2]
        if y1==0: breakevens.append(x1)
        if y1*y2<0: breakevens.append(x1+(x2-x1)*(-y1)/(y2-y1))
    if values[-1]==0:breakevens.append(knots[-1])
    if upper_slope and -values[-1]/upper_slope>0:
        breakevens.append(knots[-1]-values[-1]/upper_slope)
    low=max(0,min(strikes[0]*.97,spot*.95)); high=max(strikes[-1]*1.03,spot*1.05)
    points=[{'price':round(low+(high-low)*i/100,2),'pnl':round(value(low+(high-low)*i/100),2)} for i in range(101)]
    premium=sum(-(1 if l['side']=='BUY' else -1)*l.get('entry_price',l.get('price',0))*l.get('open_lots',l.get('lots',1))*l['lot_size']*float(l.get('price_multiplier') or 1) for l in legs)
    # Conservative educational estimate; broker/exchange SPAN is intentionally not implied.
    long_cost=sum(l.get('entry_price',l.get('price',0))*l.get('open_lots',l.get('lots',1))*l['lot_size']*float(l.get('price_multiplier') or 1) for l in legs if l['side']=='BUY')
    naked=sum(spot*.2*l.get('open_lots',l.get('lots',1))*l['lot_size']*float(l.get('price_multiplier') or 1) for l in legs if l['side']=='SELL')
    standalone=long_cost+naked
    margin=standalone
    if max_loss is not None and any(l['side']=='SELL' for l in legs):
        margin=min(standalone,max_loss+max(0,premium))
    return {'supported':True,'max_profit':None if max_profit is None else round(max_profit,2),'max_loss':None if max_loss is None else round(max_loss,2),'profit_unlimited':upper_slope>0,'loss_unlimited':upper_slope<0,'breakevens':sorted(set(round(b,2) for b in breakevens)), 'net_premium':round(premium,2),'estimated_initial_margin':round(standalone,2),'estimated_margin':round(margin,2),'hedge_benefit':round(max(0,standalone-margin),2),'risk_reward':round(max_profit/max_loss,2) if max_profit is not None and max_loss not in (None,0) else None,'points':points,'assumptions':'Expiry intrinsic payoff on underlying prices >= 0; same underlying and expiry. Estimated costs deducted. Margin uses long premium plus 20% underlying notional for uncovered shorts, capped at defined risk plus credit for hedged spreads; this is not broker SPAN margin.'}
