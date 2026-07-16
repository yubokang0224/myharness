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
    payload: dict[str, Any] = {
        "configured": kb.is_configured,
        "default_mode": kb.default_mode,
        "default_top_k": kb.default_top_k,
        "namespaces": [
            {"name": name, "label": label or name}
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
    namespaces = _resolve_recall_namespaces(kb, payload)

    token = _sign_hub_token(kb, namespaces=namespaces, permission="read")
    started = time.monotonic()
    chunks: list[dict[str, Any]] = []
    warnings: list[str] = []

    async with httpx.AsyncClient(timeout=kb.timeout, trust_env=False) as client:
        results = await asyncio.gather(
            *(
                _recall_one(
                    client,
                    kb,
                    token=token,
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
    writable = kb.writable_namespaces()
    if namespace not in writable:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"namespace '{namespace}' is not writable; writable namespaces: {sorted(writable)}",
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

        token = _sign_hub_token(kb, namespaces=[namespace], permission="write")
        try:
            response = await client.post(
                _hub_url(kb, "/v1/hub/ingest"),
                headers={"Authorization": f"Bearer {token}"},
                json=hub_body,
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
    token: str,
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

    response = await client.post(
        _hub_url(kb, "/v1/hub/recall"),
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
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


def _resolve_recall_namespaces(kb: KbSettings, payload: dict[str, Any]) -> list[str]:
    readable = kb.readable_namespaces()
    if not readable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no readable namespaces configured (settings.kb)",
        )

    requested = _string_list(payload.get("namespaces"))
    single = _text(payload.get("namespace"))
    if single and single not in requested:
        requested.append(single)

    if not requested:
        agent_name = _text(payload.get("agent_name") or payload.get("agentName"))
        access = kb.agents.get(agent_name) if agent_name else None
        requested = list(access.read) if access and access.read else sorted(readable)

    denied = [namespace for namespace in requested if namespace not in readable]
    if denied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"namespaces not allowed: {denied}; readable namespaces: {sorted(readable)}",
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
            detail="knowledge base is not configured (settings.kb.hub_base_url / jwt_secret)",
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


def _hub_url(kb: KbSettings, path: str) -> str:
    return kb.hub_base_url.rstrip("/") + path


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
