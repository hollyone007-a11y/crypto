"""Conservative demo adaptation of newly observed fills, through the existing risk gate."""
import hashlib
import json
import math
import re
from sqlalchemy import select
from analyst.news_risk import assess, read_state
from db.models import AgentState, JournalEntry, Ohlcv, AnalystRun
from strategies.base import Signal

POLICY = ('Публичный Hyperliquid: 3 кошелька по ROI месяца, капитал ≥100k, ROI 1–200%, '
          'положительный PnL недели/месяца/всего, объём месяца ≥1m. Обновление отбора 6ч. '
          'Это фильтр прошлых результатов, не доказательство навыка: просадка и срок жизни не установлены.')


def store(session, key, value):
    row = session.get(AgentState, key)
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if row:
        row.value = payload
    else:
        session.add(AgentState(key=key, value=payload))
    session.commit()


def source_of(session, position):
    journal = session.scalar(select(JournalEntry).where(JournalEntry.position_id == position.id))
    return (journal.context or {}).get('copy_source', {}) if journal else {}


def apply(session, engine, symbols, now, exchange='binanceusdm'):
    external = read_state(session, 'copy_sources')
    state = read_state(session, 'copy_state')
    # The first cycle starts observing now; historical fills are never copied at startup.
    started = state.setdefault('started_ts', now)
    seen = set(state.get('seen', []))
    events = list(state.get('events', []))
    wallets = external.get('wallets', {}) if 0 <= now - external.get('ts', 0) <= 120_000 else {}
    ranked = {row['address'] for row in external.get('traders', [])} if 0 <= now-external.get('rank_ts', 0) <= 6*3_600_000 else set()
    # Source position reduction: conservatively close our entire adapted demo position.
    for position in list(engine.broker.open_positions()):
        if position.strategy != 'copy_hl@v1':
            continue
        source = source_of(session, position)
        wallet = wallets.get(source.get('address'))
        reason = None
        if now - position.opened_ts >= 24 * 3_600_000:
            reason = 'copy_timeout'
        elif wallet:
            size = wallet['positions'].get(source.get('coin'), 0)
            if size == 0 or (size > 0) != (position.side == 'long'):
                reason = 'copy_source_exit'
            elif any(x.get('coin') == source.get('coin') and
                     x.get('dir') in ('Close Long', 'Close Short') and
                     position.opened_ts < int(x.get('time', 0)) <= now for x in wallet['fills']):
                reason = 'copy_reduce'
        if reason:
            try:
                closed = engine.broker.close(position, reason)
                engine.journal.record_close(closed)
                events.append(dict(ts=now, symbol=position.symbol, address=source.get('address'),
                                   status='демовыход', reason=reason))
            except Exception as exc:
                events.append(dict(ts=now, symbol=position.symbol, status='ошибка выхода', reason=type(exc).__name__))
    # Process most recent execution first to avoid chasing older partial orders.
    records = [(address, fill) for address, wallet in wallets.items() for fill in wallet['fills']]
    records.sort(key=lambda item: int(item[1].get('time', 0)), reverse=True)
    for address, fill in records:
        if fill.get('dir') not in ('Open Long', 'Open Short'):
            continue
        uid = hashlib.sha256(f"{address}:{fill.get('tid')}:{fill.get('time')}:{fill.get('coin')}".encode()).hexdigest()[:40]
        if uid in seen:
            continue
        seen.add(uid)
        try:
            ts = int(fill['time']); coin = str(fill['coin']); px = float(fill['px'])
            if ts < started or not 0 <= now - ts <= 120_000:
                continue
            symbol = coin + 'USDT'
            item = dict(ts=now, source_ts=ts, address=address, symbol=symbol,
                        source_price=px if math.isfinite(px) else None, status='пропущен', reason='')
            events.append(item)
            def reject(reason):
                item['reason'] = reason
            if address not in ranked:
                reject('Отбор устарел или кошелёк больше не входит в выбранные.'); continue
            if not re.fullmatch(r'[A-Z0-9]{2,15}', coin) or symbol not in symbols:
                reject('Нет точного соответствия в текущем Binance USDT universe.'); continue
            if not math.isfinite(px) or px <= 0:
                reject('Некорректная цена источника.'); continue
            side = 'long' if fill['dir'] == 'Open Long' else 'short'
            source_size = wallets[address]['positions'].get(coin, 0)
            if source_size == 0 or (source_size > 0) != (side == 'long'):
                reject('Источник уже закрыл или развернул позицию.'); continue
            news = assess(session, symbol, now)
            item['news'] = news
            if not news['coverage_fresh'] or news['blockers']:
                reject('; '.join(news['blockers']) or 'Нет свежего успешного опроса новостей.'); continue
            bars = list(session.scalars(select(Ohlcv).where(Ohlcv.exchange == exchange,
                Ohlcv.symbol == symbol, Ohlcv.timeframe == '1h', Ohlcv.ts + 3_600_000 <= now)
                .order_by(Ohlcv.ts.desc()).limit(200)))
            if len(bars) < 200 or now - bars[0].ts > 2*3_600_000:
                reject('Нет 200 свежих закрытых часовых свечей.'); continue
            bid, ask = engine.broker.quotes(symbol)
            entry = ask if side == 'long' else bid
            if abs(entry / px - 1) > .005:
                reject('Цена Binance отличается от исполнения источника больше чем на 0.5%.'); continue
            atr = sum(max(bars[i].high-bars[i].low, abs(bars[i].high-bars[i+1].close),
                          abs(bars[i].low-bars[i+1].close)) for i in range(14))/14
            distance = min(.05*entry, max(.01*entry, 2*atr))
            sign = 1 if side == 'long' else -1
            signal = Signal(side, entry, entry-sign*distance, entry+sign*2*distance, .5,
                'Адаптация публичного исполнения Hyperliquid; собственный стоп 1–5%, цель 2R.')
            # Additional experiment cap 0.1% equity; leader leverage and quantity never copied.
            quantity = engine.broker.equity() * .001 / distance
            source = dict(address=address, coin=coin, tid=fill.get('tid'), time=ts, price=px)
            result = engine.submit(symbol, signal, 'copy_hl@v1',
                thesis=signal.rationale, context=dict(copy_source=source, news=news, policy=POLICY),
                requested_qty=quantity, requested_leverage=1, client_order_id='hl-'+uid)
            item['status'] = 'демовход' if result.ok else 'пропущен'
            item['reason'] = 'Открыт через общий RiskManager; объём и стоп собственные.' if result.ok else '; '.join(result.reasons)
            item['position_id'] = result.position_id
        except Exception as exc:
            events.append(dict(ts=now, address=address, status='ошибка сигнала', reason=type(exc).__name__))
    state.update(ts=now, seen=sorted(seen)[-2000:], events=events[-100:], policy=POLICY)
    store(session, 'copy_state', state)
    session.add(AnalystRun(ts=now, purpose='copy_demo', model='hyperliquid-public-v1', ok=True,
        request={'source_ts': external.get('ts', 0)}, response=json.dumps(dict(events=events[-10:]),ensure_ascii=False)))
    session.commit()
    return state
