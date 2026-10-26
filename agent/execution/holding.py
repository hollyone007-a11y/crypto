"""Predeclared duration exits recorded at entry; existing positions keep their original rules."""
from sqlalchemy import select
from db.models import JournalEntry


def enforce(session, engine, position, now):
    journal=session.scalar(select(JournalEntry).where(JournalEntry.position_id==position.id))
    profile=(journal.context or {}).get('execution_profile',{}) if journal else {}
    legacy=not profile and bool(engine.profile and engine.profile.get('apply_holding_to_legacy'))
    if legacy: profile=engine.profile
    elapsed=(now-position.opened_ts)/60_000
    maximum=profile.get('max_hold_minutes',0)
    stalled=profile.get('stalled_exit_minutes',0)
    reason=None
    if maximum and elapsed>=maximum:
        reason='legacy_time' if legacy else 'time_limit'
    elif stalled and elapsed>=stalled:
        bid,ask=engine.broker.quotes(position.symbol)
        price=bid if position.side=='long' else ask
        sign=1 if position.side=='long' else -1
        net=sign*(price-position.entry)*position.qty-position.fees+position.funding-price*position.qty*engine.broker.fee
        initial_risk=abs(position.entry-position.stop)*position.qty
        if net<initial_risk*.25:
            reason='stalled'
    if not reason: return False
    closed=engine.broker.close(position,reason)
    engine.journal.record_close(closed)
    return True
