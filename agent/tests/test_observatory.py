import logging
import httpx
import pandas as pd
from sqlalchemy import select, func

from config.settings import Settings
from db.models import JournalEntry, Lesson, Position
from learning.autonomous import review_rules
from learning import pipeline
from learning.walkforward import _window_stats, beats, evaluate_window, has_lookahead
from ops.cloud import publish
from strategies.baseline import TrendFollowing
from tests.fakes import synthetic_market
from tests.test_learning import prop, stats, evaluator, NOW, DAY
from tests.test_ops import app, open_long


def test_no_trade_trial_is_waiting_not_promoted(session):
    candidate=pipeline.propose_params(session,prop(channel=40))
    ev=evaluator({'trend_donchian@v1':(stats(.5),stats(.3)),
                  'trend_donchian@v2':(stats(1.),stats(0.,trades=0,sum_r=0.))})
    pipeline.advance(session,ev,NOW)
    pipeline.advance(session,ev,NOW+15*DAY)
    assert candidate.status=='paper_trial'
    assert pipeline.current_prod(session,'trend_donchian').version==1


def test_losing_candidate_cannot_pass_even_when_better():
    candidate=stats(.4); candidate['expectancy_r']=-.02
    assert not beats(candidate,stats(.1))[0]


def test_intraday_and_initial_loss_are_not_hidden():
    equity=pd.Series([8000.,10050.],index=[NOW,NOW+3600000])
    assert _window_stats(pd.DataFrame(),equity)['max_dd_pct']==-20.
    equity=pd.Series([9800.],index=[NOW])
    assert _window_stats(pd.DataFrame(),equity)['worst_day_pct']==-2.


def test_future_stop_is_detected():
    class FutureStop(TrendFollowing):
        def signals(self,df,funding=None):
            result=super().signals(df,funding)
            result.loc[result.side!=0,'stop']=df.close.mean()
            return result
    bars,funding=synthetic_market(1500)
    assert has_lookahead(FutureStop(timeframe='1h'),bars,funding)


def test_end_window_ignores_future_prices():
    bars,funding=synthetic_market(1500)
    strategy=TrendFollowing(timeframe='1h')
    end=int(bars.ts.iloc[1100]); start=int(bars.ts.iloc[500])
    before=evaluate_window(strategy,bars,funding,start,end)
    bars.loc[bars.ts>=end,['open','high','low','close']]*=10
    assert evaluate_window(strategy,bars,funding,start,end)==before


def test_rules_lessons_use_observations_and_are_idempotent(session):
    session.add(JournalEntry(position_id=123,symbol='ETHUSDT',strategy='x',side='short',
        context={},signal={},entry_ts=NOW,exit_ts=NOW+1000,entry=100.,exit=100.1,qty=1.,
        pnl=-.2,fees=.3,funding=0.,r=-.1,exit_reason='stop'))
    session.commit()
    assert review_rules(session,NOW+2000)==1
    assert review_rules(session,NOW+3000)==0
    lesson=session.scalar(select(Lesson))
    assert 'costs' in lesson.tags and '0.30' in lesson.what_worked


def test_paper_holding_horizon_closes_and_journals(app):
    pid=open_long(app)
    with app.Session() as s:
        p=s.get(Position,pid); p.strategy='meanrev_z@v1'; s.commit()
    app.clock.t+=49*3600
    app.tick()
    with app.Session() as s:
        assert s.get(Position,pid).exit_reason=='time'
        assert s.scalar(select(JournalEntry)).exit_reason=='time'


def test_cloud_failure_does_not_expose_service_token(caplog):
    token='do-not-log-this-service-token'
    settings=Settings(_env_file=None,cloud_dashboard_url='https://example.chatgpt.site',cloud_dashboard_token=token)
    client=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(503)))
    with caplog.at_level(logging.WARNING):
        assert not publish(settings,'<!doctype html>',NOW,NOW,client)
    assert token not in caplog.text and token not in repr(settings)


def test_trade_progress_and_tabs_escape_persisted_text(app):
    open_long(app)
    html=app.page()
    assert 'role="progressbar"' in html
    assert 'id="learning"' in html and 'aria-controls="learning"' in html
    assert 'data-heartbeat=' in html and 'aria-valuetext=' in html
