"""Application-generated times must advance without weakening evidence validation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from commerceops import actions, core, outcomes
from commerceops.timestamps import timestamp_key


@pytest.fixture
def clock(monkeypatch):
    wall = {"now": datetime(2026, 9, 8, 10, tzinfo=timezone.utc)}

    class WallClock:
        @staticmethod
        def now(tz):
            assert tz is timezone.utc
            return wall["now"]

    # Control the OS clock boundary, NOT utcnow or terminal validation. Reset
    # process-local clock history so this regression is independent of test order.
    monkeypatch.setattr(core, "datetime", WallClock)
    monkeypatch.setattr(core, "_last_utcnow", None, raising=False)
    return wall


@pytest.fixture
def shipment(tmp_path, clock):
    conn = core.connect(str(tmp_path / "clock.db"))
    core.import_source(conn, "tracking_no,postex_remark\nCLOCK,RFD")
    sid = conn.execute("SELECT id FROM shipment WHERE tracking_no='CLOCK'").fetchone()[0]
    yield conn, sid
    conn.close()


TERMINALS = [
    (outcomes.mark_cancelled, {"cancel_reason": "CUSTOMER REFUSED - NH CHAIYE"}, "CLOSED_CANCELLED"),
    (outcomes.mark_delivered, {}, "CLOSED_DELIVERED"),
    (outcomes.mark_returned, {}, "RETURNED"),
]


@pytest.mark.parametrize("terminal,fields,state", TERMINALS)
def test_current_time_terminal_after_observation_on_same_wall_clock_tick(shipment, clock, terminal, fields, state):
    conn, sid = shipment
    clock["now"] += timedelta(seconds=1)
    observed_at = core.utcnow()
    conn.execute(
        "INSERT INTO tracking_event(id,shipment_id,raw_code,imported_at,event_key) "
        "VALUES ('later',?,'DELIVERED',?,'later')", (sid, observed_at))
    core.refresh_state(conn, sid)
    assert core.derive_state(conn, sid) == "NEEDS_ACTION"
    # This is the Slice 5 failure: wall time has not advanced between calls.
    acted_at = core.utcnow()
    aid = terminal(conn, sid, acted_at=acted_at, **fields)
    assert timestamp_key(acted_at) > timestamp_key(observed_at)
    assert conn.execute("SELECT acted_at FROM operator_action WHERE id=?", (aid,)).fetchone()[0] == acted_at
    assert core.derive_state(conn, sid) == state


@pytest.mark.parametrize("rollback", [False, True])
def test_utcnow_advances_across_equal_or_backward_wall_time(clock, rollback):
    first = core.utcnow()
    if rollback:
        clock["now"] -= timedelta(seconds=1)
    second = core.utcnow()
    assert timestamp_key(second) == timestamp_key(first) + timedelta(microseconds=1)
    assert datetime.fromisoformat(second).utcoffset() == timedelta(0)
    clock["now"] = timestamp_key(second) + timedelta(seconds=2)
    assert timestamp_key(core.utcnow()) == clock["now"]


def test_utcnow_serializes_concurrent_allocation(clock):
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: core.utcnow(), range(100)))
    instants = sorted(timestamp_key(value) for value in values)
    assert len(set(instants)) == 100
    assert all(right - left == timedelta(microseconds=1) for left, right in zip(instants, instants[1:]))


@pytest.mark.parametrize("terminal,fields,state", TERMINALS)
@pytest.mark.parametrize("offset_seconds", [0, -1])
def test_explicit_terminal_tie_or_older_still_rejected_verbatim(shipment, terminal, fields, state, offset_seconds):
    conn, sid = shipment
    latest = timestamp_key(conn.execute("SELECT imported_at FROM tracking_event WHERE shipment_id=?", (sid,)).fetchone()[0])
    supplied = (latest + timedelta(seconds=offset_seconds)).astimezone(timezone(timedelta(hours=5))).isoformat()
    before = list(conn.iterdump())
    with pytest.raises(actions.ValidationError, match="ties another event|predates a newer event"):
        terminal(conn, sid, acted_at=supplied, **fields)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("terminal,fields,state", TERMINALS)
def test_default_terminal_uses_database_order_across_clock_restart(shipment, clock, monkeypatch, terminal, fields, state):
    conn, sid = shipment
    future = (clock["now"] + timedelta(days=1)).isoformat()
    actions.record_operator_action(conn, sid, "note", note="Existing source time", acted_at=future)
    # A fresh process has no clock history. The database allocator must still
    # outrank persistent evidence; a supplied stale time must NOT be promoted.
    monkeypatch.setattr(core, "_last_utcnow", None, raising=False)
    with pytest.raises(actions.ValidationError, match="predates a newer event"):
        terminal(conn, sid, acted_at=core.utcnow(), **fields)
    aid = terminal(conn, sid, **fields)
    at = conn.execute("SELECT acted_at FROM operator_action WHERE id=?", (aid,)).fetchone()[0]
    assert timestamp_key(at) == timestamp_key(future) + timedelta(microseconds=1)
    assert core.derive_state(conn, sid) == state


def test_original_slice5_scenario_with_coarse_wall_clock(tmp_path, clock, monkeypatch):
    from test_slice5 import test_q_no_courier_observation_creates_terminal

    original_import = core.import_source

    def import_then_advance(*args, **kwargs):
        result = original_import(*args, **kwargs)
        # Import finishes on the previous tick; observation, refresh and the
        # explicit utcnow() terminal command all see the next identical tick.
        clock["now"] += timedelta(seconds=1)
        return result

    monkeypatch.setattr(core, "import_source", import_then_advance)
    test_q_no_courier_observation_creates_terminal(tmp_path)
