"""Predeclared author-inspired hypotheses. Own exits; never represented as original systems."""
import pandas as pd
from research.features import atr, adx, ema

TIMEFRAMES={'5m':300_000,'15m':900_000,'1h':3_600_000,'4h':14_400_000}
METHODS={
 'st_flip':dict(name='Supertrend · подтверждённый поворот',author='Независимая открытая гипотеза',
  url='https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/supported-indicators/super-trend',
  rules='ATR10 × 3; смена направления после закрытия свечи с фильтром EMA50. Стоп — линия индикатора; цель — 2 исходных риска.',
  scope='Собственная реализация на данных Binance по открытой математической идее. Не импорт TradingView и не подтверждённая прибыльная стратегия; временные выходы и активный вариант — наши правила.'),
 'bb_cont':dict(name='Bollinger · продолжение',author='John Bollinger',
  url='https://www.bollingerbands.com/bollinger-band-rules',
  rules='Закрытие впервые за SMA20 ± 2σ; стоп 2 ATR14, цель 3 ATR14. Касание полосы не считается входом.',
  scope='Собственная гипотеза продолжения по описанному автором принципу; не готовая система автора.'),
 'donchian':dict(name='Donchian · пробой',author='Richard Dennis / Curtis Faith',
  url='https://oxfordstrat.com/coasdfASD32/uploads/2016/01/turtle-rules.pdf',
  rules='Первое закрытие выше/ниже предыдущего 20-барного канала; стоп 2 ATR20, цель 4 ATR20.',
  scope='Внутридневная адаптация идеи Turtle. Нет оригинального пирамидинга, фильтра последнего пробоя и исходных дневных выходов.'),
 'grail':dict(name='ADX · откат',author='Linda Raschke',
  url='https://lindaraschke.net/wp-content/uploads/2026/01/august1997.pdf',
  rules='ADX14 >30 и растёт, EMA20 задаёт направление; подтверждённый возврат после касания EMA20 предыдущей свечой; стоп 2 ATR14, цель 3 ATR14.',
  scope='Адаптация Holy Grail: закрытие-подтверждение и собственные ATR-выходы заменяют авторский отложенный вход/свинг-стоп.')}


def signals(bars, method):
    out=pd.DataFrame({'side':0,'stop':float('nan'),'take':float('nan')},index=bars.index)
    if bars.empty: return out
    close=bars.close
    a=atr(bars,20 if method=='donchian' else 14)
    if method=='bb_cont':
        mean=close.rolling(20).mean();sigma=close.rolling(20).std(ddof=0)
        upper,lower=mean+2*sigma,mean-2*sigma
        up=(close>upper)&(close.shift(1)<=upper.shift(1))
        down=(close<lower)&(close.shift(1)>=lower.shift(1));target=3
    elif method=='donchian':
        high=bars.high.rolling(20).max().shift(1);low=bars.low.rolling(20).min().shift(1)
        up=(close>high)&(close.shift(1)<=high.shift(1))
        down=(close<low)&(close.shift(1)>=low.shift(1));target=4
    elif method=='st_flip':
        from research.supertrend import calculate
        direction,level=calculate(bars)
        average=ema(close,50)
        up=(direction==1)&(direction.shift(1)==-1)&(close>average)
        down=(direction==-1)&(direction.shift(1)==1)&(close<average)
        a=(close-level).abs()/2;target=4
    elif method=='grail':
        trend=ema(close,20);strength=adx(bars)
        rising=(strength>30)&(strength>strength.shift(1))
        up=rising&(trend>trend.shift(1))&(bars.low.shift(1)<=trend.shift(1))&(close>bars.high.shift(1))
        down=rising&(trend<trend.shift(1))&(bars.high.shift(1)>=trend.shift(1))&(close<bars.low.shift(1));target=3
    else: raise ValueError('unknown predeclared method')
    up=(up&a.gt(0)).fillna(False);down=(down&a.gt(0)&~up).fillna(False)
    out.loc[up,'side']=1;out.loc[down,'side']=-1
    out['stop']=close-out.side*2*a;out['take']=close+out.side*target*a
    return out
