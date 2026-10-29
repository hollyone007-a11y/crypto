import threading
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import insert, select, text
from db.models import AgentState, StrategyVersion
from db.session import init_db, make_engine, make_session_factory
from tests.test_ops import app


def test_core_writer_waits_but_reader_can_continue(tmp_path):
    engine=make_engine('sqlite:///'+str(tmp_path/'parallel.db'))
    init_db(engine)
    factory=make_session_factory(engine)
    started=threading.Event();done=threading.Event()
    def writer():
        with factory() as s:
            s.execute(text('PRAGMA busy_timeout=20'))
            started.set()
            s.execute(insert(AgentState).values(key='second',value='2'))
            s.commit();done.set()
    with factory() as first, ThreadPoolExecutor(1) as pool:
        first.add(AgentState(key='first',value='1'));first.flush()
        task=pool.submit(writer)
        try:
            assert started.wait(2)
            assert not done.wait(.1)
            with factory() as reader:
                assert reader.scalar(select(AgentState).where(AgentState.key=='first')) is None
        finally:
            first.commit()
        task.result(timeout=3)
    with factory() as s:
        assert len(list(s.scalars(select(AgentState))))==2


def test_rollback_releases_writer_across_factories(tmp_path):
    url='sqlite:///'+str(tmp_path/'rollback.db')
    engine=make_engine(url);init_db(engine)
    first_factory=make_session_factory(engine)
    second_factory=make_session_factory(make_engine(url))
    with first_factory() as s:
        s.add(AgentState(key='discarded',value='1'));s.flush();s.rollback()
    def write():
        with second_factory() as s:
            s.add(AgentState(key='saved',value='2'));s.commit()
    with ThreadPoolExecutor(1) as pool:
        pool.submit(write).result(timeout=3)


def test_frozen_replay_does_not_hold_pending_metric_write(tmp_path,monkeypatch):
    from learning import evaluate as module
    from strategies.registry import seed
    engine=make_engine('sqlite:///'+str(tmp_path/'research.db'));init_db(engine)
    factory=make_session_factory(engine)
    def concurrent_account_update(*args,**kwargs):
        def update():
            with factory() as other:
                other.add(AgentState(key='heartbeat',value='fresh'));other.commit()
        with ThreadPoolExecutor(1) as pool:
            pool.submit(update).result(timeout=3)
        return {'trades':0}
    monkeypatch.setattr(module,'evaluate_in_process',concurrent_account_update)
    with factory() as s:
        seed(s)
        version=s.scalar(select(StrategyVersion))
        version.metrics={'pending':'persist before CPU'}
        s.flush()  # reproduce an autoflushed research transaction
        result=module.frozen_evaluator(s,[],1)(version,None)
        assert result['trades']==0


def test_idle_executor_records_reason(app):
    from analyst.news_risk import read_state
    object.__setattr__(app.settings,'multiframe_enabled',True)
    object.__setattr__(app.settings,'entry_margin_pct',10)
    object.__setattr__(app.settings,'entry_leverage',10)
    object.__setattr__(app.settings,'max_hold_minutes',60)
    object.__setattr__(app.settings,'stalled_exit_minutes',30)
    from dataclasses import replace
    app.limits=replace(app.limits,max_risk_per_trade_pct=.5)
    result=app.multiframe_execute()
    assert result['status']=='waiting_validation'
    with app.Session() as s:
        assert read_state(s,'mtf_decisions')['reason']==result['reason']
    assert 'id="execution-status"' in app.page()
    assert result['reason'] in app.page()


def test_fresh_qualified_signal_opens_once_and_reaches_journal(app,monkeypatch):
    from dataclasses import replace
    from db.models import Position, JournalEntry
    from learning import multiframe
    for key,value in dict(multiframe_enabled=True,entry_margin_pct=10,entry_leverage=10,max_hold_minutes=60,stalled_exit_minutes=30).items():
        object.__setattr__(app.settings,key,value)
    app.limits=replace(app.limits,max_leverage=10,max_risk_per_trade_pct=.5)
    candidate=dict(symbol='BTCUSDT',method='st_flip',timeframe='5m',policy='balanced',key='mtf_st_flip_5m',signal_ts=app.now_ms(),dataset_id='test-only',test={'expectancy_r':.5},
        signal=dict(side='long',entry=100.05,stop=99.9,take=101,confidence=.55,rationale='synthetic qualified signal'))
    monkeypatch.setattr(multiframe,'pending_signals',lambda *_:[candidate])
    app.multiframe_execute();app.multiframe_execute()
    with app.Session() as s:
        positions=list(s.scalars(select(Position)))
        assert len(positions)==1 and positions[0].status=='open'
        assert s.scalar(select(JournalEntry)).context['execution_profile']['target_margin']==1000
