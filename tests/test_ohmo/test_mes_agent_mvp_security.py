from __future__ import annotations

import asyncio
import io
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt
from starlette.datastructures import Headers, UploadFile

from ohmo import record_index
from ohmo.gateway.config import (
    configure_mes_workspace_from_appsettings,
    resolve_mes_workspace_config,
    save_gateway_config,
)
from ohmo.gateway.models import GatewayConfig, MesWorkspaceConfig
from ohmo.gateway.dependencies import (
    AuthContext,
    JWT_AUDIENCE,
    JWT_ISSUER,
    JWT_SECRET_KEY,
    Principal,
    _authorize_workspace_access,
    _decode_bearer_credentials,
    _decode_bearer_with_principal,
    principal_from_claims,
)
from ohmo.gateway.routers.chat import (
    _anonymous_api_enabled,
    _abort_events,
    _pending_approval_sessions,
    _pending_approvals,
    _load_authorized_session,
    _validate_attachments,
    abort_message,
    approve_tool,
    create_session,
    delete_attachment,
    delete_session,
    get_memory,
    get_messages,
    list_artifacts,
    list_sessions,
    send_message_sync,
    upload_attachment,
)
from ohmo.gateway.schemas.chat import ApproveRequest, CreateSessionRequest, MessageRequest
from ohmo.session_storage import load_by_id, save_session_snapshot
from ohmo.workspace import get_attachments_dir
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage
from openharness.tools.artifacts import TRACKED_ARTIFACT_EXTENSIONS, preview_kind_for_path


def _auth(source: str, subject: str, *, admin: bool = False) -> AuthContext:
    principal = Principal(
        source=source,
        subject=subject,
        account=f"{source}-{subject}",
        is_admin=admin,
    )
    return AuthContext(user={"sub": subject}, raw_token="token", principal=principal)


def test_os_and_mes_tokens_are_both_validated(monkeypatch):
    monkeypatch.setenv("OHMO_JWT_MES_SECRET_KEY", "mes-secret-key-for-test")
    monkeypatch.setenv("OHMO_JWT_MES_ISSUER", "hj-robot.com")
    monkeypatch.setenv("OHMO_JWT_MES_AUDIENCE", "hj-robot.com")

    os_token = jwt.encode(
        {"sub": "os-user", "iss": JWT_ISSUER, "aud": JWT_AUDIENCE},
        JWT_SECRET_KEY,
        algorithm="HS256",
    )
    os_payload, raw_os = _decode_bearer_credentials(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=os_token)
    )
    assert raw_os == os_token
    assert os_payload["_ohmo_owner_source"] == "os"

    mes_token = jwt.encode(
        {
            "CLAINM_USERID": "mes-user",
            "CLAINM_ACCOUNT": "zhangsan",
            "CLAINM_SUPERADMIN": 999,
            "iss": "hj-robot.com",
            "aud": "hj-robot.com",
        },
        "mes-secret-key-for-test",
        algorithm="HS256",
    )
    payload, raw, principal = _decode_bearer_with_principal(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=mes_token)
    )
    assert raw == mes_token
    assert payload["_ohmo_owner_source"] == "mes"
    assert principal == Principal(
        source="mes",
        subject="mes-user",
        account="zhangsan",
        name=None,
        is_admin=True,
    )
    with pytest.raises(HTTPException) as management_auth:
        _decode_bearer_credentials(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=mes_token)
        )
    assert management_auth.value.status_code == 401


def test_only_explicit_admin_values_grant_admin():
    assert principal_from_claims(
        {"sub": "u1", "CLAINM_SUPERADMIN": 999}, source="mes"
    ).is_admin
    assert not principal_from_claims(
        {"sub": "u1", "role": "admin"}, source="mes"
    ).is_admin


def test_anonymous_api_fails_closed_when_mes_integration_is_configured(monkeypatch, tmp_path):
    workspace = tmp_path / ".ohmo"
    monkeypatch.delenv("OHMO_ALLOW_ANONYMOUS_API", raising=False)
    monkeypatch.delenv("OHMO_JWT_MES_SECRET_KEY", raising=False)
    monkeypatch.delenv("OHMO_MES_REQUIRED_PERMISSION", raising=False)
    assert _anonymous_api_enabled(workspace)

    monkeypatch.setenv("OHMO_JWT_MES_SECRET_KEY", "configured")
    assert not _anonymous_api_enabled(workspace)

    monkeypatch.setenv("OHMO_ALLOW_ANONYMOUS_API", "true")
    assert _anonymous_api_enabled(workspace)


def test_disabling_mes_does_not_reopen_anonymous_api(monkeypatch, tmp_path):
    workspace = tmp_path / ".ohmo"
    save_gateway_config(
        GatewayConfig(
            mes=MesWorkspaceConfig(
                enabled=True,
                issuer="hj-robot.com",
                audience="hj-robot.com",
                api_base_url="http://localhost:2401",
                allow_anonymous_api=False,
            )
        ),
        workspace,
    )
    monkeypatch.setenv("OHMO_MES_ENABLED", "false")
    monkeypatch.delenv("OHMO_ALLOW_ANONYMOUS_API", raising=False)

    config = resolve_mes_workspace_config(workspace)
    assert not config.enabled
    assert not config.allow_anonymous_api


def test_mes_appsettings_is_imported_once_without_env_variables(monkeypatch, tmp_path):
    workspace = tmp_path / ".ohmo"
    appsettings = tmp_path / "appsettings.Development.json"
    appsettings.write_text(
        """
        {
          // URLs may contain // and must survive JSONC parsing.
          "WebConfig": {
            "JWTConfig": {
              "SecurityKey": "mes-secret-from-settings",
              "Issuer": "hj-robot.com",
              "Audience": "hj-robot.com"
            }
          },
          "urls": "http://*:2401"
        }
        """,
        encoding="utf-8",
    )
    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        "ohmo.gateway.config.store_credential",
        lambda provider, key, value: stored.__setitem__((provider, key), value),
    )
    monkeypatch.setattr(
        "ohmo.gateway.config.load_credential",
        lambda provider, key: stored.get((provider, key)),
    )
    for name in (
        "OHMO_MES_ENABLED",
        "OHMO_JWT_MES_SECRET_KEY",
        "OHMO_JWT_MES_ISSUER",
        "OHMO_JWT_MES_AUDIENCE",
        "OHMO_JWT_MES_ALGORITHM",
        "OHMO_MES_REQUIRED_PERMISSION",
        "OHMO_MES_PERMISSION_URL",
        "OHMO_MES_API_BASE_URL",
        "OHMO_ALLOW_ANONYMOUS_API",
    ):
        monkeypatch.delenv(name, raising=False)

    config_path = configure_mes_workspace_from_appsettings(
        appsettings,
        workspace=workspace,
    )
    config = resolve_mes_workspace_config(workspace)
    persisted = config_path.read_text(encoding="utf-8")

    assert config.enabled
    assert config.secret_key == "mes-secret-from-settings"
    assert config.issuer == "hj-robot.com"
    assert config.audience == "hj-robot.com"
    assert config.api_base_url == "http://localhost:2401"
    assert config.required_permission == "report.ai"
    assert not config.allow_anonymous_api
    assert "mes-secret-from-settings" not in persisted


def test_anonymous_policy_is_scoped_to_the_request_workspace(monkeypatch, tmp_path):
    for name in (
        "OHMO_MES_ENABLED",
        "OHMO_JWT_MES_SECRET_KEY",
        "OHMO_JWT_MES_ISSUER",
        "OHMO_JWT_MES_AUDIENCE",
        "OHMO_MES_REQUIRED_PERMISSION",
        "OHMO_MES_PERMISSION_URL",
        "OHMO_MES_API_BASE_URL",
        "OHMO_ALLOW_ANONYMOUS_API",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "ohmo.gateway.config.load_credential",
        lambda _provider, _key: "stored-mes-secret",
    )

    mes_workspace = tmp_path / "mes-workspace"
    legacy_workspace = tmp_path / "legacy-workspace"
    save_gateway_config(
        GatewayConfig(
            mes=MesWorkspaceConfig(
                enabled=True,
                issuer="hj-robot.com",
                audience="hj-robot.com",
                api_base_url="http://localhost:2401",
                required_permission="report.ai",
                allow_anonymous_api=False,
            )
        ),
        mes_workspace,
    )

    assert not _anonymous_api_enabled(mes_workspace)
    assert _anonymous_api_enabled(legacy_workspace)


def test_office_artifacts_are_tracked_without_breaking_legacy_preview_kinds():
    assert {".xls", ".xlsx", ".xlsm", ".ods"} <= TRACKED_ARTIFACT_EXTENSIONS
    assert preview_kind_for_path("report.xlsx") == "binary"
    assert preview_kind_for_path("report.csv") == "text"
    assert preview_kind_for_path("briefing.pptx") == "binary"


@pytest.mark.asyncio
async def test_session_owner_isolation_and_legacy_admin_access(tmp_path):
    owner = _auth("mes", "u1")
    other = _auth("mes", "u2")
    admin = _auth("os", "root", admin=True)
    os_user = _auth("os", "operator")
    runtime = SimpleNamespace(workspace=tmp_path)

    created = await create_session(
        CreateSessionRequest(title="MES session", persist_mode="session"),
        owner,
        runtime,
    )
    snapshot = load_by_id(workspace=tmp_path, session_id=created.id)
    assert snapshot is not None
    assert snapshot["owner_source"] == "mes"
    assert snapshot["owner_subject"] == "u1"

    own_sessions = await list_sessions(owner, runtime, False, None, None)
    other_sessions = await list_sessions(other, runtime, False, None, None)
    assert [item.id for item in own_sessions] == [created.id]
    assert other_sessions == []

    with pytest.raises(HTTPException) as forbidden:
        await get_messages(created.id, other, runtime)
    assert forbidden.value.status_code == 404

    for protected_call in (
        lambda: get_memory(created.id, other, runtime),
        lambda: list_artifacts(created.id, other, runtime),
        lambda: abort_message(created.id, other, runtime),
        lambda: delete_session(created.id, other, runtime),
    ):
        with pytest.raises(HTTPException) as protected:
            await protected_call()
        assert protected.value.status_code == 404
    assert load_by_id(workspace=tmp_path, session_id=created.id) is not None

    save_session_snapshot(
        cwd=tmp_path,
        workspace=tmp_path,
        model="test",
        system_prompt="system",
        messages=[ConversationMessage.from_user_text("legacy")],
        usage=UsageSnapshot(),
        session_id="legacy-session",
    )
    assert "legacy-session" not in {item.id for item in await list_sessions(owner, runtime, True, None, None)}
    assert "legacy-session" in {item.id for item in await list_sessions(admin, runtime, True, None, None)}
    assert "legacy-session" in {item.id for item in await list_sessions(os_user, runtime, True, None, None)}
    assert _load_authorized_session(runtime, "legacy-session", admin.principal)["session_id"] == "legacy-session"


@pytest.mark.asyncio
async def test_missing_session_keeps_os_web_contract_but_mes_stays_strict(tmp_path):
    runtime = SimpleNamespace(workspace=tmp_path)
    os_user = _auth("os", "operator")
    mes_user = _auth("mes", "u1")

    assert await get_messages("missing-session", os_user, runtime) == []
    assert await list_artifacts("missing-session", os_user, runtime) == []
    memory = await get_memory("missing-session", os_user, runtime)
    assert isinstance(memory.entries, list)
    await approve_tool(
        "missing-session",
        ApproveRequest(request_id="missing-request", approved=False, scope="once"),
        os_user,
        runtime,
    )
    await abort_message("missing-session", os_user, runtime)
    await delete_session("missing-session", os_user, runtime)

    for protected_call in (
        lambda: get_messages("missing-session", mes_user, runtime),
        lambda: list_artifacts("missing-session", mes_user, runtime),
        lambda: get_memory("missing-session", mes_user, runtime),
        lambda: abort_message("missing-session", mes_user, runtime),
        lambda: delete_session("missing-session", mes_user, runtime),
    ):
        with pytest.raises(HTTPException) as protected:
            await protected_call()
        assert protected.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_is_exact_and_persistent_direct_send_requires_bound_owner(tmp_path):
    owner = _auth("mes", "u1")
    runtime = SimpleNamespace(workspace=tmp_path)
    for session_id in ("session", "victim-session"):
        save_session_snapshot(
            cwd=tmp_path,
            workspace=tmp_path,
            model="test",
            system_prompt="system",
            messages=[ConversationMessage.from_user_text(session_id)],
            usage=UsageSnapshot(),
            session_id=session_id,
            owner_source="mes",
            owner_subject="u1",
        )

    await delete_session("session", owner, runtime)
    assert load_by_id(workspace=tmp_path, session_id="session") is None
    assert load_by_id(workspace=tmp_path, session_id="victim-session") is not None

    with pytest.raises(HTTPException) as missing:
        await send_message_sync(
            "not-created",
            MessageRequest(content="hello", persist_mode="session"),
            owner,
            runtime,
        )
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_mes_workspace_permission_is_checked_server_side(monkeypatch):
    monkeypatch.setenv("OHMO_MES_REQUIRED_PERMISSION", "report.ai")
    monkeypatch.setenv("OHMO_MES_API_BASE_URL", "http://mes-api:8012")
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://mes-api:8012/SysUser/GetPermissions"
        assert request.headers["Authorization"] == "Bearer token"
        return httpx.Response(200, json={"backResult": ["report.ai"]})

    monkeypatch.setattr(
        "ohmo.gateway.dependencies.httpx.AsyncClient",
        lambda **_kwargs: original_client(transport=httpx.MockTransport(handler)),
    )
    auth = _auth("mes", "u1")
    assert await _authorize_workspace_access(auth) is auth


@pytest.mark.asyncio
async def test_permission_request_is_bound_to_its_session(tmp_path):
    owner = _auth("mes", "u1")
    runtime = SimpleNamespace(workspace=tmp_path)
    first = await create_session(
        CreateSessionRequest(title="First", persist_mode="session"), owner, runtime
    )
    second = await create_session(
        CreateSessionRequest(title="Second", persist_mode="session"), owner, runtime
    )
    request_id = "bound-request"
    future = asyncio.get_running_loop().create_future()
    _pending_approvals[request_id] = future
    _pending_approval_sessions[request_id] = first.id
    try:
        await approve_tool(
            second.id,
            ApproveRequest(request_id=request_id, approved=True, scope="once"),
            owner,
            runtime,
        )
        assert not future.done()

        await approve_tool(
            first.id,
            ApproveRequest(request_id=request_id, approved=True, scope="once"),
            owner,
            runtime,
        )
        assert future.result().approved
    finally:
        _pending_approvals.pop(request_id, None)
        _pending_approval_sessions.pop(request_id, None)


@pytest.mark.asyncio
async def test_abort_run_id_cannot_stop_a_newer_stream(tmp_path):
    runtime = SimpleNamespace(workspace=tmp_path)
    os_user = _auth("os", "operator")
    current_event = asyncio.Event()
    _abort_events["same-session"] = ("run-new", current_event)
    try:
        await abort_message("same-session", os_user, runtime, "run-old")
        assert not current_event.is_set()

        await abort_message("same-session", os_user, runtime, "run-new")
        assert current_event.is_set()

        legacy_event = asyncio.Event()
        _abort_events["same-session"] = ("another-run", legacy_event)
        await abort_message("same-session", os_user, runtime)
        assert legacy_event.is_set()
    finally:
        _abort_events.pop("same-session", None)


@pytest.mark.asyncio
async def test_anonymous_api_compatibility_cannot_create_web_session(tmp_path, monkeypatch):
    monkeypatch.setenv("OHMO_ALLOW_ANONYMOUS_API", "true")
    runtime = SimpleNamespace(workspace=tmp_path)

    api_session = await create_session(
        CreateSessionRequest(title="API compatibility"),
        None,
        runtime,
    )
    snapshot = load_by_id(workspace=tmp_path, session_id=api_session.id)
    assert api_session.channel == "api"
    assert snapshot is not None
    assert snapshot["owner_source"] == "anonymous"

    with pytest.raises(HTTPException) as forbidden:
        await create_session(
            CreateSessionRequest(title="Web session", persist_mode="session"),
            None,
            runtime,
        )
    assert forbidden.value.status_code == 401

    owned = await create_session(
        CreateSessionRequest(title="Owned web", persist_mode="session"),
        _auth("mes", "u1"),
        runtime,
    )
    with pytest.raises(HTTPException) as hidden:
        await send_message_sync(
            owned.id,
            MessageRequest(content="must not resume"),
            None,
            runtime,
        )
    assert hidden.value.status_code == 404


@pytest.mark.asyncio
async def test_attachment_id_preserves_legacy_fields_and_enforces_owner(tmp_path):
    runtime = SimpleNamespace(workspace=tmp_path)
    owner = _auth("mes", "u1")
    other = _auth("mes", "u2")
    admin = _auth("os", "root", admin=True)
    upload = UploadFile(
        filename="report.txt",
        file=io.BytesIO(b"safe content"),
        headers=Headers({"content-type": "text/plain"}),
    )

    result = await upload_attachment(upload, owner, runtime)
    assert {"uid", "attachment_id", "name", "path", "fileSize", "fileType"} <= result.keys()
    assert result["fileSize"] == len(b"safe content")

    disguised = UploadFile(
        filename="disguised.pdf",
        file=io.BytesIO(b"MZ" + b"\x00" * 64),
        headers=Headers({"content-type": "application/pdf"}),
    )
    with pytest.raises(HTTPException) as mismatched:
        await upload_attachment(disguised, owner, runtime)
    assert mismatched.value.status_code == 415

    macro_payload = io.BytesIO()
    with zipfile.ZipFile(macro_payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("xl/workbook.xml", "<workbook />")
        archive.writestr("xl/vbaProject.bin", b"macro")
    macro_payload.seek(0)
    macro_file = UploadFile(
        filename="macro-disguised-as-xlsx.xlsx",
        file=macro_payload,
        headers=Headers({
            "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        }),
    )
    with pytest.raises(HTTPException) as macro_rejected:
        await upload_attachment(macro_file, owner, runtime)
    assert macro_rejected.value.status_code == 415

    validated = _validate_attachments(
        [{"attachment_id": result["attachment_id"], "name": "report.txt"}],
        principal=owner.principal,
        workspace=tmp_path,
    )
    assert validated[0]["path"] == result["path"]

    with pytest.raises(HTTPException) as forbidden:
        _validate_attachments(
            [{"attachment_id": result["attachment_id"]}],
            principal=other.principal,
            workspace=tmp_path,
        )
    assert forbidden.value.status_code == 400

    assert _validate_attachments(
        [{"attachment_id": result["attachment_id"]}],
        principal=admin.principal,
        workspace=tmp_path,
    )[0]["path"] == result["path"]

    attachments_root = get_attachments_dir(tmp_path).resolve()
    assert str(result["path"]).startswith(str(attachments_root))

    with pytest.raises(HTTPException) as other_delete:
        await delete_attachment(result["attachment_id"], other, runtime)
    assert other_delete.value.status_code == 404
    await delete_attachment(result["attachment_id"], owner, runtime)
    assert not Path(result["path"]).exists()


@pytest.mark.asyncio
async def test_attachment_size_limit_removes_partial_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OHMO_ATTACHMENT_MAX_BYTES", "3")
    runtime = SimpleNamespace(workspace=tmp_path)
    upload = UploadFile(filename="too-large.txt", file=io.BytesIO(b"four"))

    with pytest.raises(HTTPException) as too_large:
        await upload_attachment(upload, _auth("mes", "u1"), runtime)
    assert too_large.value.status_code == 413
    assert not list(get_attachments_dir(tmp_path).rglob("*too-large.txt"))


@pytest.mark.asyncio
async def test_attachment_owner_quota_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("OHMO_ATTACHMENT_MAX_BYTES", "10")
    monkeypatch.setenv("OHMO_ATTACHMENT_OWNER_QUOTA_BYTES", "3")
    upload = UploadFile(filename="quota.txt", file=io.BytesIO(b"four"))
    with pytest.raises(HTTPException) as quota:
        await upload_attachment(
            upload,
            _auth("mes", "u1"),
            SimpleNamespace(workspace=tmp_path),
        )
    assert quota.value.status_code == 413
    assert not list(get_attachments_dir(tmp_path).rglob("*quota.txt"))


def test_existing_sqlite_index_is_migrated_with_owner_columns(tmp_path):
    index_path = record_index._index_path(tmp_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(index_path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
                session_key TEXT, conversation_id TEXT, channel TEXT NOT NULL,
                platform TEXT NOT NULL, bot_name TEXT, agent_name TEXT,
                chat_id TEXT, sender_id TEXT, sender_name TEXT, summary TEXT NOT NULL,
                response_text TEXT, message_count INTEGER NOT NULL, model TEXT NOT NULL,
                created_at REAL NOT NULL, input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL
            );
            CREATE TABLE invocations (
                invocation_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
                session_id TEXT, agent_name TEXT, channel TEXT NOT NULL,
                platform TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                request_content TEXT, response_text TEXT, error TEXT, created_at REAL NOT NULL,
                message_count INTEGER NOT NULL, tool_call_count INTEGER NOT NULL,
                trace_id TEXT, duration_ms INTEGER, input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL
            );
            """
        )

    record_index._initialized_indexes.discard(index_path)
    with record_index._connect(tmp_path) as connection:
        session_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(sessions)")
        }
        invocation_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(invocations)")
        }
    assert {"owner_source", "owner_subject", "owner_account"} <= session_columns
    assert {"owner_source", "owner_subject", "owner_account"} <= invocation_columns
