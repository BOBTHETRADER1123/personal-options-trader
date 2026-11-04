"""Analytical terminal payoff; broker margin is a separate external calculation.

For settlement S >= 0 each leg contributes sign * quantity * (intrinsic(S)
- entry premium). Realized P&L is added and caller-supplied charges deducted
once. All extrema and roots use exact linear pieces, never chart samples.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from collections.abc import Mapping

ZERO=Decimal(0)
CENT=Decimal('0.01')


def _decimal(value,label):
    if isinstance(value,bool) or value is None:
        raise ValueError(f'{label} must be a finite number.')
    try:number=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError):raise ValueError(f'{label} must be a finite number.') from None
    if not number.is_finite():raise ValueError(f'{label} must be a finite number.')
    return number


def _money(value):
    return float(value.quantize(CENT,rounding=ROUND_HALF_UP))


def _validated_leg(leg,identity=False):
    if not isinstance(leg,Mapping):raise ValueError('Each payoff leg must be an object.')
    if leg.get('side') not in ('BUY','SELL') or leg.get('option_type') not in ('CE','PE','FUT'):
        raise ValueError('Invalid instrument type or direction.')
    lot_size=_decimal(leg.get('lot_size'),'Lot size')
    lots=_decimal(leg.get('lots',1),'Lots')
    open_lots=_decimal(leg.get('open_lots',lots),'Open lots')
    if lot_size<=0 or lot_size!=lot_size.to_integral_value():raise ValueError('Lot size must be a positive integer.')
    if any(value<0 or value!=value.to_integral_value() for value in (lots,open_lots)):
        raise ValueError('Lots must be nonnegative integers.')
    if 'lots' in leg and open_lots>lots:raise ValueError('Open lots cannot exceed total lots.')
    strike=_decimal(leg.get('strike',0),'Strike')
    if strike<0 or (leg['option_type']!='FUT' and strike==0):raise ValueError('Options require a finite positive strike.')
    entry=_decimal(leg.get('entry_price',leg.get('price')),'Entry price')
    if entry<0:raise ValueError('Entry prices must be finite and nonnegative.')
    multiplier=leg.get('price_multiplier')
    if multiplier is None:
        if leg.get('exchange')=='MCX' and not leg.get('demo'):
            raise ValueError('MCX monetary multiplier is unavailable; verified contract units are required.')
        multiplier=1
    multiplier=_decimal(multiplier,'Monetary multiplier')
    if multiplier<=0:raise ValueError('Monetary multiplier must be positive.')
    expiry=leg.get('expiry')
    if expiry is not None:
        if not isinstance(expiry,str) or len(expiry)!=10:
            raise ValueError('Contract expiry must be an ISO date (YYYY-MM-DD).')
        try:date.fromisoformat(expiry)
        except ValueError:raise ValueError('Contract expiry must be a valid ISO date.') from None
    if identity and (not isinstance(leg.get('underlying'),str) or not leg['underlying'].strip() or expiry is None):
        raise ValueError('A payoff group requires a named underlying and contract expiry.')
    return {'source':leg,'side':leg['side'],'kind':leg['option_type'],'strike':strike,
            'entry':entry,'quantity':open_lots*lot_size*multiplier,'expiry':expiry,
            'underlying':leg.get('underlying'),'exchange':leg.get('exchange') or '',
            'broker':leg.get('broker') or ''}


def _payoff(leg,settlement):
    intrinsic=(settlement if leg['kind']=='FUT' else max(ZERO,settlement-leg['strike'])
               if leg['kind']=='CE' else max(ZERO,leg['strike']-settlement))
    return (1 if leg['side']=='BUY' else -1)*leg['quantity']*(intrinsic-leg['entry'])


def leg_payoff(leg,settlement):
    with localcontext() as context:
        context.prec=50
        price=_decimal(settlement,'Settlement')
        if price<0:raise ValueError('Settlement must be nonnegative.')
        return float(_payoff(_validated_leg(leg),price))


def _roots(knots,values,slope):
    roots=set();intervals=[]
    for index in range(len(knots)-1):
        x1,x2=knots[index:index+2];y1,y2=values[index:index+2]
        if y1==0 and y2==0:intervals.append((x1,x2));continue
        if y1==0:roots.add(x1)
        if y2==0:roots.add(x2)
        if y1*y2<0:roots.add(x1+(x2-x1)*(-y1)/(y2-y1))
    last=knots[-1];value=values[-1]
    if slope:
        root=last-value/slope
        if root>=last:roots.add(root)
    elif value==0:intervals.append((last,None))
    merged=[]
    for first,last in intervals:
        if merged and merged[-1][1]==first:merged[-1]=(merged[-1][0],last)
        else:merged.append((first,last))
    roots=[root for root in sorted(roots) if not any(first<=root and (last is None or root<=last) for first,last in merged)]
    return roots,merged


def _margin_unavailable():
    # Payoff loss is not SPAN/ELM, and summing standalone order margins cannot
    # establish the broker's hedged portfolio requirement.
    return {'margin_status':'unavailable','margin_reason':'Broker margin unavailable',
            'estimated_initial_margin':None,'estimated_margin':None,'hedge_benefit':None,
            'span_margin':None,'elm_margin':None,'additional_margin':None,
            'basket_margin':None,'final_capital_required':None}


def _premium_warnings(legs):
    warnings=[]
    for kind in ('CE','PE'):
        options=sorted((leg for leg in legs if leg['kind']==kind),key=lambda leg:leg['strike'])
        for low,high in zip(options,options[1:]):
            if low['strike']==high['strike']:continue
            inconsistent=(high['entry']>low['entry'] if kind=='CE' else high['entry']<low['entry'])
            if inconsistent:
                warnings.append(f"Entry premiums are non-monotone for {kind} strikes {low['strike']} and {high['strike']}. Compare original fill times and bid/ask; fills at different times are not proof of an invalid market quote. Provided prices are retained unchanged.")
    return warnings


def _single_risk(legs,spot,charges,realized_pnl):
    strikes=sorted({leg['strike'] for leg in legs if leg['kind']!='FUT'})
    knots=sorted({ZERO,*strikes})
    gross=[sum((_payoff(leg,settlement) for leg in legs),ZERO)+realized_pnl for settlement in knots]
    values=[value-charges for value in gross]
    slope=sum(((1 if leg['side']=='BUY' else -1)*leg['quantity'] for leg in legs if leg['kind'] in ('CE','FUT')),ZERO)
    roots,intervals=_roots(knots,values,slope)
    gross_roots,gross_intervals=_roots(knots,gross,slope)
    max_profit=None if slope>0 else max(values)
    max_loss=None if slope<0 else max(ZERO,-min(values))
    gross_profit=None if slope>0 else max(gross)
    gross_loss=None if slope<0 else max(ZERO,-min(gross))
    premium=sum((-(1 if leg['side']=='BUY' else -1)*leg['entry']*leg['quantity'] for leg in legs if leg['kind']!='FUT'),ZERO)
    points=[]
    if legs:
        references=strikes or [leg['entry'] for leg in legs]
        reference=spot if spot is not None else sum(references,ZERO)/len(references)
        reference=max(Decimal(1),reference)
        low=max(ZERO,min(references[0]*Decimal('.97'),reference*Decimal('.95'),min(roots)*Decimal('.98') if roots else reference))
        high=max(max(references)*Decimal('1.03'),reference*Decimal('1.05'),max(roots)*Decimal('1.02') if roots else reference)
        grid={low+(high-low)*index/100 for index in range(101)}
        grid.update(knot for knot in knots if low<=knot<=high)
        grid.update(root for root in roots if low<=root<=high)
        points=[{'price':_money(settlement),'pnl':_money(sum((_payoff(leg,settlement) for leg in legs),ZERO)+realized_pnl-charges)} for settlement in sorted(grid)]
    return {'supported':True,'closed':not bool(legs),'calculation_method':'analytic_piecewise_linear',
            'max_profit':None if max_profit is None else _money(max_profit),
            'max_loss':None if max_loss is None else _money(max_loss),
            'gross_max_profit':None if gross_profit is None else _money(gross_profit),
            'gross_max_loss':None if gross_loss is None else _money(gross_loss),
            'profit_unlimited':slope>0,'loss_unlimited':slope<0,'upper_tail_slope':float(slope),
            'breakevens':sorted({_money(root) for root in roots}),
            'breakevens_unrounded':[float(root) for root in roots],
            'breakeven_intervals':[{'from':float(first),'to':None if last is None else float(last)} for first,last in intervals],
            'breakevens_excluding_charges':sorted({_money(root) for root in gross_roots}),
            'gross_breakeven_intervals':[{'from':float(first),'to':None if last is None else float(last)} for first,last in gross_intervals],
            'net_premium':_money(premium),'charges':_money(charges),'realized_pnl':_money(realized_pnl),
            'risk_reward':_money(max_profit/max_loss) if max_profit is not None and max_loss not in (None,ZERO) else None,
            'points':points,'premium_warnings':_premium_warnings(legs),
            'assumptions':'Analytical expiry intrinsic payoff over nonnegative underlying settlements for one broker, exchange, underlying and expiry. Open lots only; realized P&L is included. Caller-supplied estimated charges are deducted once; future exit fees are excluded unless supplied. Chart samples do not determine theoretical extrema or breakevens. Broker margin is separate.',
            **_margin_unavailable()}


def strategy_risk(legs,spot=None,charges=0,realized_pnl=0):
    with localcontext() as context:
        context.prec=50
        costs=_decimal(charges,'Charges')
        realized=_decimal(realized_pnl,'Realized P&L')
        if costs<0:raise ValueError('Charges must be nonnegative.')
        reference=None if spot is None else _decimal(spot,'Reference price')
        if reference is not None and reference<=0:raise ValueError('Reference price must be positive.')
        if not isinstance(legs,(list,tuple)):raise ValueError('Payoff legs must be a list.')
        normalized=[]
        for leg in legs:
            if isinstance(leg,Mapping) and leg.get('enabled') is False:continue
            value=_validated_leg(leg,identity=True)
            if value['quantity']>0:normalized.append(value)
        groups=defaultdict(list)
        for leg in normalized:groups[(leg['broker'],leg['exchange'],leg['underlying'],leg['expiry'])].append(leg)
        if len(groups)<=1:return _single_risk(normalized,reference,costs,realized)
        breakdown=[]
        for (broker,exchange,underlying,expiry),group in groups.items():
            breakdown.append({'broker':broker or None,'exchange':exchange or None,'underlying':underlying,'expiry':expiry,
                              **_single_risk(group,reference,ZERO,ZERO)})
        return {'supported':False,'closed':False,'reason':'A combined expiry payoff requires the same broker, exchange, underlying and expiry. Separate groups are shown without assuming a common settlement.',
                'max_profit':None,'max_loss':None,'gross_max_profit':None,'gross_max_loss':None,
                'profit_unlimited':False,'loss_unlimited':False,'breakevens':[],'breakevens_unrounded':[],
                'breakeven_intervals':[],'breakevens_excluding_charges':[],'gross_breakeven_intervals':[],
                'points':[],'groups':breakdown,'shared_costs_unallocated':True,
                'charges':_money(costs),'realized_pnl':_money(realized),
                'net_premium':_money(sum((-(1 if leg['side']=='BUY' else -1)*leg['entry']*leg['quantity'] for leg in normalized if leg['kind']!='FUT'),ZERO)),
                'risk_reward':None,'premium_warnings':[warning for group in breakdown for warning in group['premium_warnings']],
                'calculation_method':'separate_analytic_expiry_groups',
                'assumptions':'Each group is calculated independently. Strategy-wide charges and realized P&L are not allocated to group payoffs; no cross-expiry, cross-exchange or cross-broker hedge or common settlement is assumed.',
                **_margin_unavailable()}
