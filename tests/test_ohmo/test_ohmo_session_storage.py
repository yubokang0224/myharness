import json
from pathlib import Path

from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage

from ohmo.session_storage import (
    OhmoSessionBackend,
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
