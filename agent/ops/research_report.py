"""Downloadable combined evidence, not an assertion of learning/profit."""
from sqlalchemy import select
from analyst.news_risk import read_state
from db.models import AgentState, Lesson
from learning.lab import report_text
from strategies.public_methods import METHODS


def build(session):
    import json
    scan=read_state(session,'mtf_scan')
    catalog=read_state(session,'market_catalog')
    records=[]
    for row in session.scalars(select(AgentState).where(AgentState.key.like('mtf:%'))):
        records.append(json.loads(row.value))
    from ops.quality import audit
    import time
    quality=audit(session,int(time.time()*1000))
    lines=['АУДИТ КАЧЕСТВА · PAPER',quality['accuracy_reason'],
           f"Закрыто {quality['closed_total']}; новый профиль {quality['new_profile_closed']}; разобрано {quality['reviewed']}; без разбора {quality['missing_reviews']}.",
           'Активное демо: '+quality['active_limits'],
           *quality['caveats'],'','ЛАБОРАТОРИЯ · MTF · PAPER',
           'Профиль: 10×; залог 10%; стоп-риск ≤0.5%; выход 30/60 мин.',
           f"Контрактов в каталоге: {len(catalog.get('contracts',[]))}; получили проверку: {len(records)}.",
           f'ТФ: 5м, 15м, 1ч, 4ч. Методов {len(METHODS)}; гипотез {len(METHODS)*6} на монету.',
           'Общий календарь: 14д train, затем пауза 1ч, 14д test; прогрев до scored-окна.',
           '5м OHLC replay со стопом первым — приближение. Исторических новостей и совместного портфеля нет.',
           'Допуск только к экспериментальному демо, не к production. Прибыльность не подтверждена.','']
    for key,method in METHODS.items():
        lines += [method['name']+' · '+method['author'],method['url'],method['rules'],method['scope'],'']
    lines+=['TRADINGVIEW: только просмотр графиков; автоматический импорт/торговые webhooks не подключены.',
            'https://www.tradingview.com/policies/',
            'Наблюдения Hyperliquid: исполнения, не полные сделки; стратегия трейдера неизвестна; reported closedPnL не равен чистой прибыли счёта.']
    for row in session.scalars(select(AgentState).where(AgentState.key.like('trader_observations:%')).limit(6)):
        value=json.loads(row.value)
        lines.append(f"{value['address']}: исполнений {value.get('fill_count',0)}, закрывающих {value.get('closing_fill_count',0)}, reported closedPnL {value.get('reported_closed_pnl',0)}, неполных окон/пауз {value.get('coverage_gaps',0)}.")
    for item in sorted(records,key=lambda x:-x.get('checked_ts',0))[:30]:
        lines.append(f"{item['symbol']} · {item['stage']} · {item.get('reason','')}")
        for score in item.get('scores',[]):
            test=score['test']
            lines.append(f"  {score['method']} / {score['timeframe']} / {score.get('policy','balanced')}: {score['status']}; test {test['trades']} сделок, net {test['net_usdt']:+.2f} USDT, среднее {test['expectancy_r']:+.3f} R; t={test['t_stat']}; пропущено по профилю {test['rejected_by_profile']}; demo {score['paper_closed']} закрытых, {score['paper_net']:+.2f} USDT. {score['reason']}")
    lines+=['','РАЗБОР ЗАКРЫТЫХ СДЕЛОК']
    for lesson in session.scalars(select(Lesson).order_by(Lesson.ts.desc()).limit(20)):
        lines += [f'{lesson.symbol} · {lesson.strategy}: {lesson.what_worked}',
                  'Проверить: '+lesson.lesson,'Причина: '+lesson.mistake]
    previous=read_state(session,'research_attempt') or read_state(session,'research_report')
    if previous:
        lines += ['','БАЗОВЫЕ ИССЛЕДОВАНИЯ (ДРУГАЯ ОБЛАСТЬ ДОКАЗАТЕЛЬСТВ)',report_text(previous)]
    return '\n'.join(lines)
