from analyst.news_risk import read_state


def render(session):
    from ops.presentation import e,number
    profile=read_state(session,'execution_profile')
    if not profile.get('margin_pct'): return ''
    return f'''<div class="card" id="execution-profile"><h2>Профиль новых демовходов: {number(profile['leverage'],0)}× · залог {number(profile['margin_pct'],0)}%</h2>
<p>Залог 10% текущего капитала × плечо 10 = номинал позиции 100% капитала. Это виртуальная модель, реальное плечо на бирже не включено. Если полный размер не проходит лимит потери 0.5% со стопом и оценкой издержек, вход пропускается. Стоп ради нужного объёма не сужается.</p>
<p>Выход максимум через {e(profile['max_hold_minutes'])} минут. После {e(profile['stalled_exit_minutes'])} минут — выход при чистом прогрессе меньше 0.25R. Закрытие может быть убыточным. Новые условия записываются в каждую новую позицию; ранее открытые следуют прежним правилам.</p>
<p class="muted">Самообучение: наблюдение → гипотеза → отдельные проверки → отчёт. Только прибыльные сделки невозможны; успех теста не гарантирует будущую прибыль.</p></div>'''
