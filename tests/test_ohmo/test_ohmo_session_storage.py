import json
from pathlib import Path

from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage

from ohmo import record_index
from ohmo.session_storage import (
    OhmoSessionBackend,
    count_invocation_records,
    get_invocation_dir,
    get_session_dir,
    list_invocation_records,
    list_snapshots,
    load_invocation_record,
    save_invocation_record,
)
from ohmo.workspace import initialize_workspace


def test_ohmo_session_backend_uses_workspace_sessions(tmp_path: Path):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    backend = OhmoSessionBackend(workspace)
    message = ConversationMessage.from_user_text("hello ohmo")
    backend.save_snapshot(
        cwd=tmp_path,
        model="gpt-5.4",
        system_prompt="system",
        messages=[message],
        usage=UsageSnapshot(),
        session_id="abc123",
    )

    session_dir = get_session_dir(workspace)
    assert session_dir == workspace / "sessions"
    assert (session_dir / "latest.json").exists()
    assert backend.load_by_id(tmp_path, "abc123") is not None


def test_ohmo_session_backend_loads_latest_for_session_key(tmp_path: Path):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    backend = OhmoSessionBackend(workspace)
    message = ConversationMessage.from_user_text("hello thread")
    backend.save_snapshot(
        cwd=tmp_path,
        model="gpt-5.4",
        system_prompt="system",
        messages=[message],
        usage=UsageSnapshot(),
        session_id="abc123",
        session_key="feishu:chat-1",
        tool_metadata={
            "task_focus_state": {"goal": "Continue the same Feishu task"},
            "recent_verified_work": ["Verified the compact attachment order"],
        },
    )

    session_dir = get_session_dir(workspace)
    assert not (session_dir / "latest.json").exists()

    loaded = backend.load_latest_for_session_key("feishu:chat-1")
    assert loaded is not None
    assert loaded["session_id"] == "abc123"
    assert loaded["session_key"] == "feishu:chat-1"
    assert loaded["tool_metadata"]["task_focus_state"]["goal"] == "Continue the same Feishu task"


def test_ohmo_invocation_records_can_be_listed_and_loaded(tmp_path: Path):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    path = save_invocation_record(
        cwd=tmp_path,
        workspace=workspace,
        model="test-model",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("invoke me")],
        usage=UsageSnapshot(input_tokens=1, output_tokens=2),
        session_id="api-call-1",
        agent_name="production-agent",
        request_content="invoke me",
        response_text="done",
        tool_calls=[{"tool_name": "read_file"}],
    )

    invocation_dir = get_invocation_dir(workspace)
    assert invocation_dir == workspace / "invocations"
    assert path.exists()

    records = list_invocation_records(workspace, agent_name="production-agent")
    assert len(records) == 1
    assert records[0]["invocation_id"]
    assert records[0]["session_id"] == "api-call-1"
    assert records[0]["tool_call_count"] == 1

    loaded = load_invocation_record(workspace, records[0]["invocation_id"])
    assert loaded is not None
    assert loaded["agent_name"] == "production-agent"
    assert loaded["response_text"] == "done"


def test_ohmo_invocation_records_support_offset_pagination(tmp_path: Path):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    for index in range(3):
        save_invocation_record(
            cwd=tmp_path,
            workspace=workspace,
            model="test-model",
            system_prompt="system",
            messages=[ConversationMessage.from_user_text(f"invoke {index}")],
            usage=UsageSnapshot(),
            session_id=f"api-call-{index}",
            agent_name="production-agent",
            request_content=f"invoke {index}",
            response_text=f"done {index}",
        )

    first_page = list_invocation_records(workspace, limit=2, offset=0)
    second_page = list_invocation_records(workspace, limit=2, offset=2)

    assert count_invocation_records(workspace, agent_name="production-agent") == 3
    assert len(first_page) == 2
    assert len(second_page) == 1
    assert {item["session_id"] for item in first_page}.isdisjoint({item["session_id"] for item in second_page})


def test_ohmo_invocation_records_support_time_filters(tmp_path: Path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    current_time = {"value": 1_000.0}
    monkeypatch.setattr("ohmo.session_storage.time.time", lambda: current_time["value"])
    save_invocation_record(
        cwd=tmp_path,
        workspace=workspace,
        model="test-model",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("old invoke")],
        usage=UsageSnapshot(),
        session_id="old-api-call",
        agent_name="production-agent",
        request_content="old invoke",
        response_text="old done",
    )
    current_time["value"] = 2_000.0
    save_invocation_record(
        cwd=tmp_path,
        workspace=workspace,
        model="test-model",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("new invoke")],
        usage=UsageSnapshot(),
        session_id="new-api-call",
        agent_name="production-agent",
        request_content="new invoke",
        response_text="new done",
    )

    records = list_invocation_records(workspace, start_at=1_500.0, end_at=2_500.0)

    assert [record["session_id"] for record in records] == ["new-api-call"]
    assert count_invocation_records(workspace, start_at=1_500.0, end_at=2_500.0) == 1


def test_record_index_backfills_historical_json_once(tmp_path: Path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    invocation_path = get_invocation_dir(workspace) / "invocation-legacy.json"
    invocation_path.write_text(
        json.dumps(
            {
                "invocation_id": "legacy",
                "session_id": "legacy-session",
                "agent_name": "production-agent",
                "channel": "api",
                "model": "test-model",
                "status": "completed",
                "created_at": 2_000.0,
                "messages": [],
                "usage": {"input_tokens": 3, "output_tokens": 4},
                "tool_calls": [],
            }
        ),
        encoding="utf-8",
    )

    assert [item["invocation_id"] for item in list_invocation_records(workspace)] == ["legacy"]

    def fail_json_read(*_args, **_kwargs):
        raise AssertionError("indexed list/count queries must not reread JSON files")

    monkeypatch.setattr(Path, "read_text", fail_json_read)
    assert count_invocation_records(workspace) == 1
    assert [item["invocation_id"] for item in list_invocation_records(workspace)] == ["legacy"]
    assert record_index.invocation_daily(workspace, start_at=0.0, agent_name=None)[0][
        "invocations"
    ] == 1


def test_record_index_aggregates_daily_metrics_in_sql(tmp_path: Path, monkeypatch):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    current_time = {"value": 2_000.0}
    monkeypatch.setattr("ohmo.session_storage.time.time", lambda: current_time["value"])

    save_invocation_record(
        cwd=tmp_path,
        workspace=workspace,
        model="test-model",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("success")],
        usage=UsageSnapshot(input_tokens=10, output_tokens=5),
        agent_name="production-agent",
        tool_calls=[{"tool_name": "read_file"}],
        duration_ms=100,
    )
    save_invocation_record(
        cwd=tmp_path,
        workspace=workspace,
        model="test-model",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("failure")],
        usage=UsageSnapshot(input_tokens=20, output_tokens=7),
        agent_name="production-agent",
        status="failed",
        error="boom",
        duration_ms=300,
    )

    rows = record_index.invocation_daily(
        workspace,
        start_at=1_000.0,
        agent_name="production-agent",
    )

    assert len(rows) == 1
    assert rows[0]["invocations"] == 2
    assert rows[0]["completed"] == 1
    assert rows[0]["errors"] == 1
    assert rows[0]["tool_calls"] == 1
    assert rows[0]["input_tokens"] == 30
    assert rows[0]["output_tokens"] == 12
    assert rows[0]["avg_duration_ms"] == 200


def test_ohmo_invocation_list_falls_back_to_api_session_snapshots(tmp_path: Path):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    backend = OhmoSessionBackend(workspace)
    backend.save_snapshot(
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
        messages=[],
        usage=UsageSnapshot(),
        session_id="api-session-1",
        title="api call",
        agent_name="production-agent",
        channel="api",
        platform="api",
    )

    records = list_invocation_records(workspace)

    assert len(records) == 1
    assert records[0]["invocation_id"] == "session-api-session-1"
    assert records[0]["status"] == "created"
    loaded = load_invocation_record(workspace, records[0]["invocation_id"])
    assert loaded is not None
    assert loaded["agent_name"] == "production-agent"


def test_ohmo_session_backend_records_remote_session_metadata(tmp_path: Path):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    backend = OhmoSessionBackend(workspace)
    backend.save_snapshot(
        cwd=tmp_path,
        model="gpt-5.4",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("hello dingtalk")],
        usage=UsageSnapshot(),
        session_id="abc123",
        session_key="dingtalk:production-bot:production-agent:chat-1:user-1",
        sender_name="张三",
    )

    snapshots = list_snapshots(workspace)

    assert snapshots[0]["conversation_id"]
    assert snapshots[0]["channel"] == "dingtalk"
    assert snapshots[0]["bot_name"] == "production-bot"
    assert snapshots[0]["agent_name"] == "production-agent"
    assert snapshots[0]["chat_id"] == "chat-1"
    assert snapshots[0]["sender_id"] == "user-1"
    assert snapshots[0]["sender_name"] == "张三"


def test_ohmo_session_backend_sanitizes_legacy_empty_assistant_messages(tmp_path: Path):
    workspace = tmp_path / ".ohmo-home"
    initialize_workspace(workspace)
    backend = OhmoSessionBackend(workspace)
    session_dir = get_session_dir(workspace)
    (session_dir / "latest.json").write_text(
        json.dumps(
            {
                "app": "ohmo",
                "session_id": "abc123",
                "session_key": "feishu:chat-1",
                "cwd": str(tmp_path),
                "model": "gpt-5.4",
                "system_prompt": "system",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                    {"role": "assistant", "content": None},
                    {"role": "assistant", "content": []},
                ],
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "tool_metadata": {},
                "created_at": 1.0,
                "summary": "hello",
                "message_count": 3,
            }
        ),
        encoding="utf-8",
    )

    loaded = backend.load_latest(tmp_path)
    assert loaded is not None
    assert loaded["message_count"] == 1
    assert loaded["messages"][0]["role"] == "user"
