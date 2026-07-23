"""Knowledge-base (RAG semantic hub) proxy routes.

Skills and the web UI never talk to the hub directly: the hub JWT secret
lives only in gateway settings, and this proxy signs short-lived tokens
scoped to the namespaces each request is allowed to touch.

Read path : POST /Kb/Recall  -> fan out /v1/hub/recall per namespace, merge.
Write path: POST /Kb/Ingest  -> upload attachments to the internal API for
             durable URLs, then /v1/hub/ingest with server-side provenance.
Config    : GET  /Kb/Config  -> namespace labels + per-agent access for UI.

Destructive hub endpoints (update/delete/purge) are intentionally NOT
proxied here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt

from openharness.config import load_settings
from openharness.config.settings import KbSettings

from ohmo.gateway.dependencies import _decode_bearer_credentials, get_current_user, get_runtime

# Shared helpers from the production-issue proxy: source-channel resolution,
# token forwarding and attachment upload to the internal (C#) API. Kept as a
# single implementation on purpose — see that module before changing them.
from ohmo.gateway.routers.production_issue import (
    _resolve_forward_token,
    _source_channel,
    _upload_attachments_recursively,
)

router = APIRouter(prefix="/Kb", tags=["Kb"])
logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)

_RECALL_MODES = {"vector", "graph", "hybrid", "express", "text", "anchor"}
_MAX_QUERY_CHARS = 5000
_MAX_ITEM_TEXT_CHARS = 30000
_MAX_CHUNK_TEXT_CHARS = 4000
_MAX_TOP_K = 50
_MAX_STATUS_IDS = 500

# Gateway-local ledger of successful ingests, keyed by item_id (falls back to
# the idempotency key). Lets the web UI mark issue rows as "已入库" without a
# C# schema change and independent of hub-side searchability.
_INGEST_LOG_FILENAME = "kb_ingest_log.json"
_ingest_log_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/Config")
async def get_kb_config(
    request: Request,
    _user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    """Knowledge-base configuration for UI pickers (namespaces, agent access)."""

    kb = load_settings().kb
    hub_titles = await _hub_workspace_titles(kb) if kb.is_configured else {}
    payload: dict[str, Any] = {
        "configured": kb.is_configured,
        "default_mode": kb.default_mode,
        "default_top_k": kb.default_top_k,
        # 显示名优先取 RAG 侧 workspace 标题；本地 label 只是 hub 不可达兜底
        "namespaces": [
            {"name": name, "label": hub_titles.get(name) or label or name}
            for name, label in sorted(kb.namespaces.items())
        ],
        "agents": {
            name: {"read": access.read, "write": access.write, "enabled": access.enabled}
            for name, access in kb.agents.items()
        },
        "writable_namespaces": sorted(kb.writable_namespaces()),
    }
    if request.query_params.get("probe") and kb.is_configured:
        payload["hub_ok"] = await _probe_hub(kb)
    return payload


@router.put("/AgentAccess/{agent_name}")
async def put_kb_agent_access(
    agent_name: str,
    request: Request,
    _user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    """Set one agent's knowledge-base authorization (called by the Agent 配置 UI).

    Stored in settings.kb.agents — the same mapping the Recall/Ingest ACL
    enforces, so changes take effect immediately.
    """
    from openharness.config import save_settings
    from openharness.config.settings import KbAgentAccess

    payload, _channel = await _read_json_body(request)
    agent_name = agent_name.strip()
    if not agent_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="agent name is required")

    settings = load_settings()
    kb = settings.kb
    known = set(kb.namespaces)
    read = _string_list(payload.get("read"))
    write = _string_list(payload.get("write"))
    unknown = [ns for ns in {*read, *write} if ns not in known]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown namespaces: {unknown}; registered namespaces: {sorted(known)}",
        )
    not_readable = [ns for ns in write if ns not in read]
    if not_readable:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"writable namespaces must also be readable: {not_readable}",
        )

    access = KbAgentAccess(read=read, write=write, enabled=bool(payload.get("enabled", True)))
    kb.agents[agent_name] = access
    save_settings(settings)
    logger.info(
        "Kb agent access updated agent=%s read=%s write=%s enabled=%s",
        agent_name, read, write, access.enabled,
    )
    return {"agent": agent_name, "read": read, "write": write, "enabled": access.enabled}


@router.post("/Recall")
async def kb_recall(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> dict[str, Any]:
    """Recall chunks from one or more namespaces and merge by score."""

    kb = _require_kb_configured()
    payload, source_channel = await _read_json_body(request)
    _require_caller(credentials, source_channel)

    query = _text(payload.get("query"))
    if not query:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="query is required")
    query = query[:_MAX_QUERY_CHARS]

    mode = _text(payload.get("mode")) or kb.default_mode
    if mode == "hybrid_dense":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="mode 'hybrid_dense' is not enabled on this deployment; use 'express' or 'hybrid'",
        )
    if mode not in _RECALL_MODES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid mode '{mode}', expected one of {sorted(_RECALL_MODES)}",
        )

    top_k = _clamp_int(payload.get("top_k") or payload.get("topK"), kb.default_top_k, 1, _MAX_TOP_K)
    namespaces = _resolve_recall_namespaces(kb, payload, source_channel)

    started = time.monotonic()
    chunks: list[dict[str, Any]] = []
    warnings: list[str] = []

    async with httpx.AsyncClient(timeout=kb.timeout, trust_env=False) as client:
        results = await asyncio.gather(
            *(
                _recall_one(
                    client,
                    kb,
                    namespace=namespace,
                    query=query,
                    mode=mode,
                    top_k=top_k,
                    min_importance=payload.get("min_importance") or payload.get("minImportance"),
                )
                for namespace in namespaces
            ),
            return_exceptions=True,
        )

    for namespace, result in zip(namespaces, results):
        if isinstance(result, BaseException):
            logger.warning("Kb recall failed namespace=%s err=%s", namespace, result)
            warnings.append(f"namespace '{namespace}' recall failed: {result}")
            continue
        ns_chunks, ns_warnings = result
        chunks.extend(ns_chunks)
        warnings.extend(ns_warnings)

    if not chunks and len(warnings) >= len(namespaces) and namespaces:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"knowledge base unavailable: {warnings[0]}",
        )

    chunks.sort(key=lambda item: item.get("score") or 0.0, reverse=True)
    chunks = chunks[:top_k]
    return {
        "success": True,
        "mode": mode,
        "namespaces": namespaces,
        "total": len(chunks),
        "chunks": chunks,
        "warnings": warnings,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
    }


@router.post("/Ingest")
async def kb_ingest(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> dict[str, Any]:
    """Ingest one knowledge item; uploads local attachments for durable URLs."""

    settings = load_settings()
    kb = _require_kb_configured(settings.kb)
    payload, source_channel = await _read_json_body(request)
    _require_caller(credentials, source_channel)

    namespace = _text(payload.get("namespace"))
    if not namespace:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="namespace is required")
    agent_name, access = _caller_access(kb, payload, source_channel)
    writable = set(access.write) if access is not None else kb.writable_namespaces()
    if namespace not in writable:
        who = f"agent '{agent_name}'" if access is not None else "caller"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"namespace '{namespace}' is not writable for {who}; writable: {sorted(writable)}",
        )

    text = _text(payload.get("text")) or _text(payload.get("content"))
    if not text:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="text is required")
    title = _text(payload.get("title"))
    text_full = f"{title}\n\n{text}" if title and title not in text else text
    text_full = text_full[:_MAX_ITEM_TEXT_CHARS]

    image_urls = _string_list(payload.get("image_urls") or payload.get("imageUrls"))

    async with httpx.AsyncClient(timeout=kb.timeout, trust_env=False) as client:
        # DingTalk skill calls carry local attachment paths; convert them to
        # durable URLs on the internal API before the hub write.
        attachments = payload.get("attachments")
        if isinstance(attachments, list) and attachments:
            upstream_base_url = settings.internal_api.base_url.strip()
            if upstream_base_url:
                forward_token = _resolve_forward_token(
                    credentials, settings.internal_api.dingtalk_token, source_channel
                )
                await _upload_attachments_recursively(
                    payload,
                    upstream_base_url=upstream_base_url,
                    authorization=f"Bearer {forward_token}",
                    source_channel=source_channel,
                    client=client,
                )
            for attachment in attachments:
                if isinstance(attachment, dict):
                    file_url = _text(attachment.get("fileUrl"))
                    if file_url and file_url not in image_urls:
                        image_urls.append(file_url)

        item = _build_ingest_item(
            payload,
            namespace=namespace,
            title=title,
            text_full=text_full,
            image_urls=image_urls,
            source_channel=source_channel,
        )
        idempotency_key = _text(payload.get("idempotency_key") or payload.get("idempotencyKey"))
        if not idempotency_key:
            digest = hashlib.sha256(f"{namespace}:{text_full}".encode("utf-8")).hexdigest()[:16]
            idempotency_key = f"kb-ingest:{namespace}:{digest}"

        hub_body = {
            "namespace": namespace,
            "items": [item],
            "idempotency_key": idempotency_key,
            "request_id": f"os-agent-{uuid.uuid4().hex[:12]}",
        }
        if payload.get("dry_run") or payload.get("dryRun"):
            hub_body["dry_run"] = True

        try:
            response = await _hub_post(
                client, kb, "/v1/hub/ingest", hub_body, namespaces=[namespace], permission="write"
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"knowledge base unavailable: {exc}",
            ) from exc

    if response.status_code >= 400:
        logger.warning(
            "Kb ingest upstream error status=%s namespace=%s body=%s",
            response.status_code,
            namespace,
            response.text[:500],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"hub ingest failed (HTTP {response.status_code}): {response.text[:300]}",
        )

    result = _safe_json(response)
    item_id = _text(payload.get("item_id") or payload.get("itemId"))
    if not hub_body.get("dry_run") and bool(result.get("success", True)):
        try:
            await _record_ingest(
                item_id or idempotency_key,
                {
                    "namespace": namespace,
                    "title": title,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source_channel": source_channel or "web",
                    "agent_name": _text(payload.get("agent_name") or payload.get("agentName")),
                    "idempotency_key": idempotency_key,
                },
            )
        except Exception:
            logger.exception("Kb ingest ledger write failed (ingest itself succeeded)")
    return {
        "success": bool(result.get("success", True)),
        "namespace": namespace,
        "idempotency_key": idempotency_key,
        "item_id": item_id,
        "image_urls": image_urls,
        "hub": result,
    }


@router.post("/IngestStatus")
async def kb_ingest_status(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> dict[str, Any]:
    """Which of the given item ids have been ingested through this gateway."""

    payload, source_channel = await _read_json_body(request)
    _require_caller(credentials, source_channel)
    ids = _string_list(payload.get("itemIds") or payload.get("item_ids"))[:_MAX_STATUS_IDS]
    log = _load_ingest_log()
    return {"found": {item_id: log[item_id] for item_id in ids if item_id in log}}


# ---------------------------------------------------------------------------
# Recall helpers
# ---------------------------------------------------------------------------
async def _recall_one(
    client: httpx.AsyncClient,
    kb: KbSettings,
    *,
    namespace: str,
    query: str,
    mode: str,
    top_k: int,
    min_importance: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    body: dict[str, Any] = {
        "namespace": namespace,
        "query": query,
        "mode": mode,
        "top_k": top_k,
        "include_chunk_text": True,
    }
    if isinstance(min_importance, (int, float)):
        body["min_importance"] = max(0.0, min(1.0, float(min_importance)))

    response = await _hub_post(client, kb, "/v1/hub/recall", body, namespaces=[namespace], permission="read")
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")

    data = _safe_json(response)
    raw_chunks = data.get("fused_results")
    if not isinstance(raw_chunks, list):
        raw_chunks = []
    chunks = [_trim_chunk(raw, namespace) for raw in raw_chunks if isinstance(raw, dict)]
    warnings = [str(w) for w in data.get("warnings") or [] if w]
    return chunks, warnings


def _trim_chunk(raw: dict[str, Any], namespace: str) -> dict[str, Any]:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    image_urls = _string_list(metadata.get("image_urls") or metadata.get("imageUrls"))
    text = raw.get("text") or raw.get("chunk_text") or ""
    return {
        "namespace": namespace,
        "score": raw.get("score"),
        "text": str(text)[:_MAX_CHUNK_TEXT_CHARS],
        "item_id": _text(raw.get("item_id") or raw.get("id")),
        "layer": _text(raw.get("layer")),
        "title": _text(metadata.get("title")),
        "source_uri": _text(source.get("source_uri") or metadata.get("source_uri")),
        "image_urls": image_urls,
    }


def _resolve_caller_agent(kb: KbSettings, payload: dict[str, Any], source_channel: str) -> str:
    """Best-effort caller identity, strongest source first.

    DingTalk skill calls carry a runtime-injected sourceSessionKey of the form
    ``dingtalk:<bot>:<agent>:<chat>:<sender>`` — that agent name is trusted and
    wins over any model-supplied agent_name field.
    """
    if source_channel == "dingtalk":
        key = _text(payload.get("sourceSessionKey") or payload.get("source_session_key"))
        parts = key.split(":")
        if len(parts) >= 3 and parts[0] == "dingtalk" and parts[2] in kb.agents:
            return parts[2]
    return _text(payload.get("agent_name") or payload.get("agentName"))


def _caller_access(kb: KbSettings, payload: dict[str, Any], source_channel: str):
    """Return (agent_name, KbAgentAccess | None) for per-agent enforcement."""
    agent_name = _resolve_caller_agent(kb, payload, source_channel)
    return agent_name, (kb.agents.get(agent_name) if agent_name else None)


def _resolve_recall_namespaces(
    kb: KbSettings, payload: dict[str, Any], source_channel: str
) -> list[str]:
    readable = kb.readable_namespaces()
    if not readable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no readable namespaces configured (settings.kb)",
        )

    agent_name, access = _caller_access(kb, payload, source_channel)
    if access is not None and source_channel == "dingtalk" and not access.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"knowledge base retrieval is disabled for agent '{agent_name}'",
        )
    # Identified agents are confined to their own read list; unidentified
    # callers (web UI constrains choices itself) fall back to the global set.
    allowed = set(access.read) if access is not None else readable

    requested = _string_list(payload.get("namespaces"))
    single = _text(payload.get("namespace"))
    if single and single not in requested:
        requested.append(single)

    if not requested:
        requested = sorted(allowed)

    denied = [namespace for namespace in requested if namespace not in allowed]
    if denied:
        who = f"agent '{agent_name}'" if access is not None else "caller"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"namespaces not allowed for {who}: {denied}; allowed: {sorted(allowed)}",
        )
    # De-duplicate, preserve order.
    return list(dict.fromkeys(requested))


# ---------------------------------------------------------------------------
# Ingest helpers
# ---------------------------------------------------------------------------
def _build_ingest_item(
    payload: dict[str, Any],
    *,
    namespace: str,
    title: str,
    text_full: str,
    image_urls: list[str],
    source_channel: str,
) -> dict[str, Any]:
    provenance: dict[str, str] = {}
    for key in (
        "sourceChannel",
        "sourceChatId",
        "sourceSenderId",
        "sourceSenderName",
        "sourceMessageId",
        "sourceConversationId",
        "sourceSessionKey",
    ):
        value = _text(payload.get(key))
        if value:
            provenance[key] = value
    if source_channel and "sourceChannel" not in provenance:
        provenance["sourceChannel"] = source_channel

    agent_name = _text(payload.get("agent_name") or payload.get("agentName"))
    source_type = _text(payload.get("source_type") or payload.get("sourceType"))
    if not source_type:
        source_type = "chat-turn" if provenance.get("sourceChannel") else "text"
    source_uri = _text(payload.get("source_uri") or payload.get("sourceUri"))
    if not source_uri and image_urls:
        source_uri = image_urls[0]

    metadata: dict[str, Any] = {
        "ingested_via": "os-agent-gateway",
        "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **provenance,
    }
    if title:
        metadata["title"] = title
    if agent_name:
        metadata["agent_name"] = agent_name
    if image_urls:
        metadata["image_urls"] = image_urls

    item: dict[str, Any] = {
        "namespace": namespace,
        "text": text_full,
        "tags": _string_list(payload.get("tags")),
        "importance": _clamp_float(payload.get("importance"), 0.7, 0.0, 1.0),
        "confidence": _clamp_float(payload.get("confidence"), 0.8, 0.0, 1.0),
        "source": {
            "source_type": source_type,
            "source_uri": source_uri,
            "extra": provenance,
        },
        "metadata": metadata,
    }
    item_id = _text(payload.get("item_id") or payload.get("itemId"))
    if item_id:
        item["item_id"] = item_id
    return item


# ---------------------------------------------------------------------------
# Ingest ledger helpers
# ---------------------------------------------------------------------------
def _ingest_log_path() -> Path:
    workspace = get_runtime().workspace
    root = Path(workspace) if workspace else Path.home() / ".ohmo"
    return root / _INGEST_LOG_FILENAME


def _load_ingest_log() -> dict[str, Any]:
    try:
        with open(_ingest_log_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


async def _record_ingest(key: str, entry: dict[str, Any]) -> None:
    if not key:
        return
    async with _ingest_log_lock:
        log = _load_ingest_log()
        log[key] = entry
        path = _ingest_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _require_kb_configured(kb: KbSettings | None = None) -> KbSettings:
    kb = kb or load_settings().kb
    if not kb.is_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="knowledge base is not configured (settings.kb.hub_base_url + hub_username/hub_password or jwt_secret)",
        )
    return kb


def _require_caller(
    credentials: HTTPAuthorizationCredentials | None,
    source_channel: str,
) -> None:
    """Allow DingTalk-channel skill calls; anything else needs a valid user token."""

    if source_channel == "dingtalk":
        return
    _decode_bearer_credentials(credentials)


async def _read_json_body(request: Request) -> tuple[dict[str, Any], str]:
    body = await request.body()
    source_channel = _source_channel(request, body)
    if not body:
        return {}, source_channel
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="request body must be a JSON object",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="request body must be a JSON object",
        )
    return payload, source_channel


def _sign_hub_token(kb: KbSettings, *, namespaces: list[str], permission: str) -> str:
    now = int(time.time())
    payload = {
        "sub": "os-agent-gateway",
        "user_id": "os-agent-gateway",
        "namespaces": namespaces,
        "roles": [],
        "permission": permission,
        "iat": now,
        "exp": now + max(60, kb.jwt_ttl_seconds),
    }
    return jwt.encode(payload, kb.jwt_secret, algorithm="HS256")


# Hub 登录 token 缓存:hub 已从共享 JWT 密钥改为账号登录签发长效 access_token,
# 网关按需登录并缓存,401 时强制重登一次。namespace 级 ACL 仍由本网关自己执行。
_hub_login_lock = asyncio.Lock()
_hub_login_cache: dict[str, Any] = {"key": None, "token": "", "expires_at": 0.0}


async def _resolve_hub_token(
    kb: KbSettings,
    *,
    namespaces: list[str],
    permission: str,
    force_refresh: bool = False,
) -> str:
    username = kb.hub_username.strip()
    if not (username and kb.hub_password):
        # 旧模式:共享密钥自签短时 token
        return _sign_hub_token(kb, namespaces=namespaces, permission=permission)

    key = (kb.hub_base_url.strip(), username)

    def _cached() -> str:
        if (
            _hub_login_cache["key"] == key
            and _hub_login_cache["token"]
            and time.time() < _hub_login_cache["expires_at"]
        ):
            return _hub_login_cache["token"]
        return ""

    if not force_refresh:
        token = _cached()
        if token:
            return token

    async with _hub_login_lock:
        if not force_refresh:
            token = _cached()
            if token:
                return token
        try:
            async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
                response = await client.post(
                    _hub_url(kb, "/api/v1/auth/login"),
                    json={"username": username, "password": kb.hub_password},
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"hub login unavailable: {exc}",
            ) from exc
        if response.status_code >= 400:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"hub login failed (HTTP {response.status_code}): {response.text[:200]}",
            )
        data = _safe_json(response)
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        token = _text(tokens.get("access_token")) or _text(data.get("access_token"))
        if not token:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="hub login succeeded but returned no access_token",
            )
        ttl = None
        for candidate in (data.get("expires_in"), tokens.get("expires_in")):
            try:
                ttl = float(candidate)
                break
            except (TypeError, ValueError):
                continue
        if not ttl or ttl <= 0:
            ttl = 12 * 3600.0
        # 提前 10 分钟视为过期,避免边界 401;真过期还有 401 重登兜底
        _hub_login_cache.update({"key": key, "token": token, "expires_at": time.time() + max(600.0, ttl - 600.0)})
        logger.info("Kb hub login ok user=%s ttl=%ss", username, int(ttl))
        return token


async def _hub_post(
    client: httpx.AsyncClient,
    kb: KbSettings,
    path: str,
    body: dict[str, Any],
    *,
    namespaces: list[str],
    permission: str,
) -> httpx.Response:
    """POST to the hub with auth; on 401 (login mode) re-login once and retry."""
    token = await _resolve_hub_token(kb, namespaces=namespaces, permission=permission)
    response = await client.post(_hub_url(kb, path), headers={"Authorization": f"Bearer {token}"}, json=body)
    if response.status_code == 401 and kb.hub_username.strip():
        logger.info("Kb hub token rejected (401), re-login and retry path=%s", path)
        token = await _resolve_hub_token(kb, namespaces=namespaces, permission=permission, force_refresh=True)
        response = await client.post(_hub_url(kb, path), headers={"Authorization": f"Bearer {token}"}, json=body)
    return response


def _hub_url(kb: KbSettings, path: str) -> str:
    return kb.hub_base_url.rstrip("/") + path


# 库显示名以 RAG 侧 workspace 标题为准（他们改名自动跟随）；本地 label 仅作
# hub 不可达时的兜底。进程内缓存 60s，避免每次打开配置页都打一次 hub。
_workspace_title_cache: dict[str, Any] = {"at": 0.0, "titles": {}}
_WORKSPACE_TITLE_TTL = 60.0


async def _hub_workspace_titles(kb: KbSettings) -> dict[str, str]:
    now = time.monotonic()
    if now - _workspace_title_cache["at"] < _WORKSPACE_TITLE_TTL and _workspace_title_cache["titles"]:
        return _workspace_title_cache["titles"]
    try:
        token = await _resolve_hub_token(kb, namespaces=["*"], permission="read")
        async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
            response = await client.get(
                _hub_url(kb, "/api/v1/dashboard/workspaces"),
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code < 400:
            workspaces = _safe_json(response).get("workspaces") or []
            titles = {
                str(ws.get("namespace")): str(ws.get("title"))
                for ws in workspaces
                if isinstance(ws, dict) and ws.get("namespace") and ws.get("title")
            }
            _workspace_title_cache.update(at=now, titles=titles)
            return titles
        logger.warning("Kb workspace titles fetch failed HTTP %s", response.status_code)
    except (HTTPException, httpx.HTTPError) as exc:
        logger.warning("Kb workspace titles unavailable, using local labels: %s", exc)
    return _workspace_title_cache["titles"] or {}


async def _probe_hub(kb: KbSettings) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(_hub_url(kb, "/v1/hub/health"))
        return response.status_code < 400
    except httpx.HTTPError:
        return False


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))
