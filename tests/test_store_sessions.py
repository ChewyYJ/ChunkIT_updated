import app.session.store as store
from tests.conftest import make_block


def _use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "sessions.db"))
    store.init_db()


def test_sessions_do_not_collide(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)

    block_a = make_block(0, content="session A secret content")
    block_b = make_block(0, content="session B different content")

    store.store_block("session-a", block_a, tokens=10)
    store.store_block("session-b", block_b, tokens=12)

    row_a = store.get_original("session-a", "msg_0")
    row_b = store.get_original("session-b", "msg_0")

    assert row_a["original_content"] == "session A secret content"
    assert row_b["original_content"] == "session B different content"


def test_missing_block_returns_none(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    assert store.get_original("session-x", "msg_99") is None


def test_wrong_session_cannot_read_other_sessions_block(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    store.store_block("session-a", make_block(0, content="private"), tokens=5)
    assert store.get_original("session-b", "msg_0") is None


def test_same_session_rewrite_replaces(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    store.store_block("s", make_block(0, content="v1"), tokens=1)
    store.store_block("s", make_block(0, content="v2"), tokens=1)
    assert store.get_original("s", "msg_0")["original_content"] == "v2"


def test_execute_retrieve_uses_session_scope(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    from app.kimi.tools import execute_retrieve

    store.store_block("s1", make_block(3, content="It processes CSV files."), tokens=6)

    found = execute_retrieve(["msg_3"], "s1")
    assert "It processes CSV files." in found

    other_session = execute_retrieve(["msg_3"], "s2")
    assert "not found" in other_session.lower()
