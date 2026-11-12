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

from ohmo.gateway.dependencies import (
    _decode_bearer_credentials,
    get_current_user,
    get_runtime,
    require_agent_admin,
)

# Existing shared knowledge-base helpers: source-channel resolution,
# token forwarding and attachment upload to the internal (C#) API. Kept as a
# single implementation on purpose — see that module before changing them.
from ohmo.gateway.legacy_attachment_proxy import (
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
    effective_ns = kb.effective_namespaces()
    hub_titles = await _hub_workspace_titles(kb) if kb.is_configured else {}
    secret = kb.resolved_jwt_secret()
    masked_secret = (
        f"{secret[:6]}...{secret[-4:]}" if len(secret) > 12 else ("***" if secret else "")
    )
    payload: dict[str, Any] = {
        "configured": kb.is_configured,
        "hub_base_url": kb.resolved_base_url(),
        "actor_id": kb.actor_id or "human:1",
        "has_jwt_secret": bool(secret),
        "jwt_secret_masked": masked_secret,
        "default_mode": kb.default_mode,
        "default_top_k": kb.default_top_k,
        # 显示名优先取 RAG 侧 workspace 标题；本地 label 只是 hub 不可达兜底
        "namespaces": [
            {"name": name, "label": hub_titles.get(name) or label or name}
            for name, label in sorted(effective_ns.items())
        ],
        "agents": {
            name: {
                "read": access.read,
                "write": access.write,
                "enabled": access.enabled,
                "scope_mode": getattr(access, "scope_mode", "all") or "all",
                "folder_ids": list(getattr(access, "folder_ids", None) or []),
                "doc_ids": list(getattr(access, "doc_ids", None) or []),
            }
            for name, access in kb.agents.items()
        },
        "writable_namespaces": sorted(kb.writable_namespaces()),
    }
    if request.query_params.get("probe") and kb.is_configured:
        probe_info = await _probe_hub_detailed(kb)
        payload["hub_ok"] = probe_info["ok"]
        payload["doc_count"] = probe_info.get("doc_count")
        payload["searchable_doc_count"] = probe_info.get("searchable_doc_count")
    return payload


@router.put("/Settings")
async def put_kb_settings(
    request: Request,
    _user: Annotated[dict[str, Any], Depends(require_agent_admin)],
) -> dict[str, Any]:
    """Save KB146 gateway connection settings (base URL, JWT secret, actor_id, top_k)."""
    from openharness.config import save_settings
    from openharness.config.settings import DEFAULT_KB146_BASE_URL, DEFAULT_KB146_NAMESPACES

    payload, _channel = await _read_json_body(request)
    settings = load_settings()
    kb = settings.kb

    if "hub_base_url" in payload:
        kb.hub_base_url = _text(payload.get("hub_base_url")) or DEFAULT_KB146_BASE_URL
    if "jwt_secret" in payload:
        new_secret = _text(payload.get("jwt_secret"))
        if new_secret:
            kb.jwt_secret = new_secret
    if "actor_id" in payload:
        kb.actor_id = _text(payload.get("actor_id")) or "human:1"
    if "default_top_k" in payload:
        kb.default_top_k = _clamp_int(payload.get("default_top_k"), kb.default_top_k, 1, _MAX_TOP_K)
    if not kb.namespaces:
        kb.namespaces = dict(DEFAULT_KB146_NAMESPACES)

    save_settings(settings)
    probe_info = await _probe_hub_detailed(kb) if kb.is_configured else {"ok": False}
    secret = kb.resolved_jwt_secret()
    masked_secret = (
        f"{secret[:6]}...{secret[-4:]}" if len(secret) > 12 else ("***" if secret else "")
    )
    return {
        "configured": kb.is_configured,
        "hub_base_url": kb.resolved_base_url(),
        "actor_id": kb.actor_id,
        "has_jwt_secret": bool(secret),
        "jwt_secret_masked": masked_secret,
        "default_top_k": kb.default_top_k,
        "hub_ok": probe_info.get("ok", False),
        "doc_count": probe_info.get("doc_count"),
        "searchable_doc_count": probe_info.get("searchable_doc_count"),
    }


@router.get("/Documents")
async def list_kb_documents(
    request: Request,
    _user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    """List documents in the KB146 namespace (read-only preview for web UI)."""
    kb = _require_kb_configured()
    namespace = _text(request.query_params.get("namespace")) or "official_kb"
    skip = _clamp_int(request.query_params.get("skip"), 0, 0, 10000)
    limit = _clamp_int(request.query_params.get("limit"), 100, 1, 200)

    headers = _build_kb146_headers(kb)
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            docs_task = client.get(
                _hub_url(kb, "/semantic-api/v1/dashboard/documents"),
                params={"namespace": namespace, "skip": skip, "limit": limit},
                headers=headers,
            )
            folders_task = client.get(
                _hub_url(kb, "/semantic-api/v1/dashboard/folders/tree"),
                params={"namespace": namespace},
                headers=headers,
            )
            response, folders_resp = await asyncio.gather(
                docs_task, folders_task, return_exceptions=True
            )
            if isinstance(response, BaseException):
                raise response
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"KB146 documents endpoint unavailable: {exc}",
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"KB146 documents query failed (HTTP {response.status_code}): {response.text[:200]}",
        )

    folders: list[dict[str, Any]] = []
    if isinstance(folders_resp, httpx.Response) and folders_resp.status_code < 400:
        tree_data = _safe_json(folders_resp)
        folders = _flatten_kb146_folder_tree(tree_data.get("tree") or [])

    data = _safe_json(response)
    raw_items = data.get("items") if isinstance(data.get("items"), list) else []
    base_url = kb.resolved_base_url()
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        doc_id = _text(raw.get("doc_id"))
        doc_status = (
            raw.get("document_status") if isinstance(raw.get("document_status"), dict) else {}
        )
        searchable = bool(
            doc_status.get("searchable")
            if "searchable" in doc_status
            else raw.get("semantic_status") in {"ready", "degraded"}
        )
        state_label = _text(doc_status.get("state_label")) or (
            "可检索" if searchable else _text(raw.get("semantic_status")) or "处理中"
        )
        items.append(
            {
                "doc_id": doc_id,
                "namespace": _text(raw.get("namespace")) or namespace,
                "title": _text(raw.get("title")) or _text(raw.get("original_file_name")) or doc_id,
                "original_file_name": _text(raw.get("original_file_name")),
                "folder_id": _text(raw.get("folder_id")),
                "folder_path": _text(raw.get("folder_path")) or "/共享文件/",
                "chunk_count": int(raw.get("chunk_count") or raw.get("chunks_count") or 0),
                "semantic_status": _text(raw.get("semantic_status")) or "ready",
                "state_label": state_label,
                "searchable": searchable,
                "created_at": _text(raw.get("created_at")),
                "updated_at": _text(raw.get("updated_at")),
                "download_url": (
                    f"{base_url}/semantic-api/v1/dashboard/documents/{doc_id}/original"
                    f"?namespace={namespace}&download=true"
                    if doc_id
                    else ""
                ),
            }
        )

    return {
        "namespace": namespace,
        "total": int(data.get("total") or len(items)),
        "searchable_total": sum(1 for item in items if item.get("searchable")),
        "folders": folders,
        "items": items,
    }


@router.get("/Evidence/{ref_id:path}")
async def get_kb_evidence(
    ref_id: str,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    request: Request,
) -> dict[str, Any]:
    """Fetch full chunk evidence and locator metadata from KB146 /v2/rag/evidence/{ref_id}."""
    kb = _require_kb_configured()
    source_channel = _source_channel(request, b"")
    _require_caller(credentials, source_channel)
    clean_ref = ref_id.strip().lstrip("/")
    if not clean_ref:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ref_id is required"
        )

    headers = _build_kb146_headers(kb)
    try:
        async with httpx.AsyncClient(timeout=kb.timeout, trust_env=False) as client:
            response = await client.get(
                _hub_url(kb, f"/v2/rag/evidence/{clean_ref}"),
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"KB146 evidence endpoint unavailable: {exc}",
        ) from exc

    if response.status_code == 404:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evidence not found")
    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"KB146 evidence failed (HTTP {response.status_code}): {response.text[:200]}",
        )
    return _safe_json(response)


@router.put("/AgentAccess/{agent_name}")
async def put_kb_agent_access(
    agent_name: str,
    request: Request,
    _user: Annotated[dict[str, Any], Depends(require_agent_admin)],
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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="agent name is required"
        )

    settings = load_settings()
    kb = settings.kb
    known = set(kb.effective_namespaces())
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

    scope_mode = _text(payload.get("scope_mode")) or "all"
    if scope_mode not in {"all", "folders", "documents"}:
        scope_mode = "all"
    folder_ids = _string_list(payload.get("folder_ids"))
    doc_ids = _string_list(payload.get("doc_ids"))

    access = KbAgentAccess(
        read=read,
        write=write,
        enabled=bool(payload.get("enabled", True)),
        scope_mode=scope_mode,
        folder_ids=folder_ids,
        doc_ids=doc_ids,
    )
    kb.agents[agent_name] = access
    save_settings(settings)
    logger.info(
        "Kb agent access updated agent=%s read=%s write=%s enabled=%s scope_mode=%s folders=%s docs=%s",
        agent_name,
        read,
        write,
        access.enabled,
        scope_mode,
        len(folder_ids),
        len(doc_ids),
    )
    return {
        "agent": agent_name,
        "read": read,
        "write": write,
        "enabled": access.enabled,
        "scope_mode": scope_mode,
        "folder_ids": folder_ids,
        "doc_ids": doc_ids,
    }


@router.post("/Recall")
async def kb_recall(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> dict[str, Any]:
    """Recall chunks from KB146 (/v2/rag/queries) or legacy semantic hub."""

    kb = _require_kb_configured()
    payload, source_channel = await _read_json_body(request)
    _require_caller(credentials, source_channel)

    query = _text(payload.get("query"))
    if not query:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="query is required"
        )
    query = query[:_MAX_QUERY_CHARS]

    mode = _text(payload.get("mode")) or kb.default_mode or "hybrid"
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
    caller_agent = _resolve_caller_agent(kb, payload, source_channel)
    explicit_folder_ids = _string_list(payload.get("folder_ids"))
    explicit_doc_ids = _string_list(payload.get("doc_ids"))

    return await execute_kb_recall(
        kb=kb,
        query=query,
        namespaces=namespaces,
        mode=mode,
        top_k=top_k,
        min_importance=payload.get("min_importance") or payload.get("minImportance"),
        agent_name=caller_agent or None,
        folder_ids=explicit_folder_ids or None,
        doc_ids=explicit_doc_ids or None,
    )


async def execute_kb_recall(
    *,
    kb: KbSettings | None = None,
    query: str,
    namespaces: list[str] | None = None,
    mode: str | None = None,
    top_k: int | None = None,
    min_importance: Any = None,
    agent_name: str | None = None,
    folder_ids: list[str] | None = None,
    doc_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Execute KB recall in-process (shared by POST /Kb/Recall and the native kb_search tool)."""
    kb = _require_kb_configured(kb)
    clean_query = _text(query)[:_MAX_QUERY_CHARS]
    if not clean_query:
        return {
            "success": True,
            "mode": mode or kb.default_mode or "hybrid",
            "namespaces": namespaces or ["official_kb"],
            "total": 0,
            "chunks": [],
            "warnings": ["query is empty"],
            "latency_ms": 0.0,
        }

    resolved_top_k = _clamp_int(top_k, kb.default_top_k or 5, 1, _MAX_TOP_K)
    resolved_mode = _text(mode) or kb.default_mode or "hybrid"
    if namespaces:
        resolved_namespaces = [ns.strip() for ns in namespaces if str(ns).strip()]
    elif agent_name:
        resolved_namespaces = kb.agent_readable_namespaces(agent_name)
    else:
        resolved_namespaces = sorted(kb.readable_namespaces())
    if not resolved_namespaces:
        resolved_namespaces = ["official_kb"]

    # 解析智能体专属目录 (folder_ids) 或专属文档 (doc_ids) 过滤范围
    effective_folder_ids = [f.strip() for f in (folder_ids or []) if f.strip()]
    effective_doc_ids = [d.strip() for d in (doc_ids or []) if d.strip()]
    if not effective_folder_ids and not effective_doc_ids and agent_name:
        access = kb.agents.get(agent_name.strip())
        if access is not None:
            scope_mode = getattr(access, "scope_mode", "all") or "all"
            if scope_mode == "folders" and getattr(access, "folder_ids", None):
                effective_folder_ids = [f.strip() for f in access.folder_ids if f.strip()]
            elif scope_mode == "documents" and getattr(access, "doc_ids", None):
                effective_doc_ids = [d.strip() for d in access.doc_ids if d.strip()]

    started = time.monotonic()

    # 若配置了 KB_JWT_SECRET 且未强制使用旧版账号密码模式，优先走 KB146 /v2/rag/queries
    if kb.resolved_jwt_secret() and not (kb.hub_username.strip() and kb.hub_password):
        has_scope_filter = bool(effective_folder_ids or effective_doc_ids)
        fetch_top_k = (
            min(_MAX_TOP_K, max(resolved_top_k * 6, 30))
            if has_scope_filter
            else resolved_top_k
        )
        chunks, warnings = await _recall_kb146(
            kb,
            query=clean_query,
            namespaces=resolved_namespaces,
            top_k=fetch_top_k,
        )
        if has_scope_filter and chunks:
            allowed_docs = await _resolve_allowed_doc_ids(
                kb,
                namespace=resolved_namespaces[0],
                folder_ids=effective_folder_ids,
                doc_ids=effective_doc_ids,
            )
            chunks = [c for c in chunks if c.get("doc_id") in allowed_docs]
        return {
            "success": True,
            "mode": resolved_mode,
            "namespaces": resolved_namespaces,
            "total": len(chunks[:resolved_top_k]),
            "chunks": chunks[:resolved_top_k],
            "warnings": warnings,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }

    # 旧版 /v1/hub/recall 兼容分支
    chunks = []
    warnings = []
    async with httpx.AsyncClient(timeout=kb.timeout, trust_env=False) as client:
        results = await asyncio.gather(
            *(
                _recall_one(
                    client,
                    kb,
                    namespace=namespace,
                    query=clean_query,
                    mode=resolved_mode,
                    top_k=resolved_top_k,
                    min_importance=min_importance,
                )
                for namespace in resolved_namespaces
            ),
            return_exceptions=True,
        )

    for namespace, result in zip(resolved_namespaces, results):
        if isinstance(result, BaseException):
            logger.warning("Kb recall failed namespace=%s err=%s", namespace, result)
            warnings.append(f"namespace '{namespace}' recall failed: {result}")
            continue
        ns_chunks, ns_warnings = result
        chunks.extend(ns_chunks)
        warnings.extend(ns_warnings)

    if not chunks and len(warnings) >= len(resolved_namespaces) and resolved_namespaces:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"knowledge base unavailable: {warnings[0]}",
        )

    chunks.sort(key=lambda item: item.get("score") or 0.0, reverse=True)
    chunks = chunks[:resolved_top_k]
    return {
        "success": True,
        "mode": resolved_mode,
        "namespaces": resolved_namespaces,
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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="namespace is required"
        )
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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="text is required"
        )
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
# Recall helpers (KB146 /v2/rag/queries + legacy semantic-hub fallback)
# ---------------------------------------------------------------------------
def _build_kb146_headers(kb: KbSettings, *, actor_id: str | None = None) -> dict[str, str]:
    """构造 KB146 (/v2/rag/queries & /v2/rag/evidence) 标准请求头。

    注意：将 iat 回拨 180 秒以消除客户端与 KB146 服务器之间的时钟漂移（避免 401 Invalid or expired service token）。
    """
    secret = kb.resolved_jwt_secret()
    sub = _text(actor_id) or _text(getattr(kb, "actor_id", "")) or "human:1"
    now = int(time.time())
    token = (
        jwt.encode(
            {
                "sub": sub,
                "iat": now - 180,
                "exp": now + max(600, int(kb.jwt_ttl_seconds or 3600)),
            },
            secret,
            algorithm="HS256",
        )
        if secret
        else ""
    )
    headers = {
        "X-Actor-Id": sub,
        "X-Access-Scope": "rag.search:read",
        "X-Contract-Version": "0.2.0",
        "X-Correlation-Id": str(uuid.uuid4()),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _recall_kb146(
    kb: KbSettings,
    *,
    query: str,
    namespaces: list[str],
    top_k: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """调用 KB146 生产检索网关 POST /v2/rag/queries 并关联文档来源元数据。"""
    headers = _build_kb146_headers(kb)
    body = {
        "query": query,
        "top_k": top_k,
        "scope_filter": {"namespaces": namespaces},
    }
    try:
        async with httpx.AsyncClient(timeout=kb.timeout, trust_env=False) as client:
            response = await client.post(
                _hub_url(kb, "/v2/rag/queries"),
                headers=headers,
                json=body,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"KB146 query gateway unavailable: {exc}",
        ) from exc

    # 手册 2.2 节契约：HTTP 404 (search.004) 代表空库或无相关命中，正常兜底返回空列表
    if response.status_code == 404:
        return [], []

    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"KB146 query failed (HTTP {response.status_code}): {response.text[:300]}",
        )

    data = _safe_json(response)
    default_ns = namespaces[0] if namespaces else "official_kb"
    chunks = _parse_kb146_query_response(data, kb=kb, default_namespace=default_ns)
    warnings = [str(w) for w in data.get("warnings") or [] if w]
    return chunks, warnings


def _parse_kb146_query_response(
    data: dict[str, Any],
    *,
    kb: KbSettings,
    default_namespace: str = "official_kb",
) -> list[dict[str, Any]]:
    """将 KB146 /v2/rag/queries 返回的 chunks 与 sources 关联拼装为统一格式。"""
    raw_sources = data.get("sources") if isinstance(data.get("sources"), list) else []
    sources_by_ref: dict[str, dict[str, Any]] = {}
    sources_by_doc_id: dict[str, dict[str, Any]] = {}
    for src in raw_sources:
        if not isinstance(src, dict):
            continue
        ref_id = _text(src.get("ref_id"))
        doc_id = _text(src.get("doc_id"))
        if ref_id:
            sources_by_ref[ref_id] = src
        if doc_id:
            sources_by_doc_id[doc_id] = src
            sources_by_ref.setdefault(f"{default_namespace}/{doc_id}", src)

    base_url = kb.resolved_base_url()
    raw_chunks = data.get("chunks") if isinstance(data.get("chunks"), list) else []
    parsed_chunks: list[dict[str, Any]] = []

    for raw in raw_chunks:
        if not isinstance(raw, dict):
            continue
        ref_id = _text(raw.get("ref_id") or raw.get("chunk_id") or raw.get("item_id"))
        source_ref = _text(raw.get("source_ref"))
        doc_id = _text(raw.get("doc_id"))
        if not doc_id and "/" in source_ref:
            doc_id = source_ref.split("/", 1)[1]

        src = (
            sources_by_ref.get(source_ref)
            or sources_by_doc_id.get(doc_id)
            or {}
        )
        if not doc_id:
            doc_id = _text(src.get("doc_id"))

        locator = src.get("locator") if isinstance(src.get("locator"), dict) else {}
        ns = (
            _text(locator.get("namespace"))
            or (ref_id.split("/", 1)[0] if "/" in ref_id else "")
            or default_namespace
        )
        chunks_locator_map = (
            locator.get("chunks") if isinstance(locator.get("chunks"), dict) else {}
        )
        chunk_loc = (
            chunks_locator_map.get(ref_id)
            if isinstance(chunks_locator_map.get(ref_id), dict)
            else {}
        )

        title = (
            _text(chunk_loc.get("original_file_name"))
            or _text(locator.get("original_file_name"))
            or _text(src.get("title"))
            or _text(raw.get("title"))
            or doc_id
        )
        heading_path = (
            _text(raw.get("heading_path"))
            or _text(chunk_loc.get("heading_path"))
            or _text(locator.get("heading_path"))
        )
        page_val = raw.get("page")
        if page_val is None:
            page_val = chunk_loc.get("page")
        if page_val is None:
            page_val = locator.get("page")

        source_locator = (
            chunk_loc.get("source_locator")
            if isinstance(chunk_loc.get("source_locator"), dict)
            else {}
        )
        slide_index = source_locator.get("slide_index")
        location_parts: list[str] = []
        if heading_path:
            location_parts.append(heading_path)
        if isinstance(page_val, int) and page_val > 0:
            location_parts.append(f"第 {page_val} 页")
        elif isinstance(slide_index, int) and slide_index > 0:
            page_val = slide_index
            location_parts.append(f"第 {slide_index} 页 (PPT)")

        content_kind = (
            _text(raw.get("content_kind"))
            or _text(chunk_loc.get("content_kind"))
            or "body"
        )
        source_uri = (
            f"{base_url}/semantic-api/v1/dashboard/documents/{doc_id}/original"
            f"?namespace={ns}&download=true"
            if doc_id and base_url
            else _text(raw.get("source_uri"))
        )
        text = str(raw.get("text") or raw.get("chunk_text") or "")[:_MAX_CHUNK_TEXT_CHARS]

        parsed_chunks.append(
            {
                "namespace": ns,
                "score": raw.get("score"),
                "text": text,
                "item_id": ref_id or _text(raw.get("chunk_id")),
                "ref_id": ref_id,
                "doc_id": doc_id,
                "layer": content_kind,
                "title": title,
                "heading_path": heading_path,
                "page": page_val if isinstance(page_val, int) else None,
                "location_label": " · ".join(location_parts),
                "source_uri": source_uri,
                "image_urls": [],
            }
        )

    parsed_chunks.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return parsed_chunks


_kb_catalog_cache: dict[str, dict[str, Any]] = {}
_KB_CATALOG_TTL = 60.0


def _flatten_kb146_folder_tree(tree: Any) -> list[dict[str, Any]]:
    """递归展平 KB146 /semantic-api/v1/dashboard/folders/tree 返回的目录树。"""
    results: list[dict[str, Any]] = []
    if not isinstance(tree, list):
        return results

    def _walk(nodes: list[Any], depth: int = 0) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            folder_id = _text(node.get("folder_id"))
            if not folder_id:
                continue
            path = _text(node.get("path")) or f"/{_text(node.get('name'))}/"
            results.append(
                {
                    "folder_id": folder_id,
                    "name": _text(node.get("name")) or folder_id,
                    "path": path,
                    "parent_folder_id": _text(node.get("parent_folder_id")) or None,
                    "doc_count": int(node.get("doc_count") or 0),
                    "depth": depth,
                }
            )
            children = node.get("children")
            if isinstance(children, list) and children:
                _walk(children, depth + 1)

    _walk(tree, 0)
    return results


async def _fetch_kb146_catalog(kb: KbSettings, namespace: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """拉取并缓存 60 秒 KB146 目录树与文档列表，供按目录/文档范围过滤使用。"""
    now = time.monotonic()
    cached = _kb_catalog_cache.get(namespace)
    if cached and now - float(cached.get("at") or 0.0) < _KB_CATALOG_TTL:
        return cached.get("folders") or [], cached.get("docs") or []

    headers = _build_kb146_headers(kb)
    folders: list[dict[str, Any]] = []
    docs: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            docs_task = client.get(
                _hub_url(kb, "/semantic-api/v1/dashboard/documents"),
                params={"namespace": namespace, "skip": 0, "limit": 200},
                headers=headers,
            )
            folders_task = client.get(
                _hub_url(kb, "/semantic-api/v1/dashboard/folders/tree"),
                params={"namespace": namespace},
                headers=headers,
            )
            docs_resp, folders_resp = await asyncio.gather(
                docs_task, folders_task, return_exceptions=True
            )
            if isinstance(folders_resp, httpx.Response) and folders_resp.status_code < 400:
                folders = _flatten_kb146_folder_tree(_safe_json(folders_resp).get("tree") or [])
            if isinstance(docs_resp, httpx.Response) and docs_resp.status_code < 400:
                raw_items = _safe_json(docs_resp).get("items")
                if isinstance(raw_items, list):
                    for raw in raw_items:
                        if isinstance(raw, dict) and _text(raw.get("doc_id")):
                            docs.append(
                                {
                                    "doc_id": _text(raw.get("doc_id")),
                                    "folder_id": _text(raw.get("folder_id")),
                                    "folder_path": _text(raw.get("folder_path")) or "/共享文件/",
                                }
                            )
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch KB146 catalog for scope filtering: %s", exc)
        if cached:
            return cached.get("folders") or [], cached.get("docs") or []
        return [], []

    _kb_catalog_cache[namespace] = {"at": now, "folders": folders, "docs": docs}
    return folders, docs


async def _resolve_allowed_doc_ids(
    kb: KbSettings,
    *,
    namespace: str,
    folder_ids: list[str],
    doc_ids: list[str],
) -> set[str]:
    """根据智能体或请求指定的 folder_ids（含子目录）与 doc_ids 解析允许召回的 doc_id 集合。"""
    allowed: set[str] = {d.strip() for d in doc_ids if d and d.strip()}
    clean_folders = {f.strip() for f in folder_ids if f and f.strip()}
    if not clean_folders:
        return allowed

    folders, docs = await _fetch_kb146_catalog(kb, namespace)
    matched_folder_ids = set(clean_folders)
    matched_paths: list[str] = []

    # 收集所选目录的路径，并级联纳入所有子目录
    folder_by_id = {f["folder_id"]: f for f in folders if f.get("folder_id")}
    for fid in list(clean_folders):
        f_obj = folder_by_id.get(fid)
        if f_obj and f_obj.get("path"):
            matched_paths.append(str(f_obj["path"]))
        elif fid.startswith("/"):
            matched_paths.append(fid)

    for f_obj in folders:
        f_path = str(f_obj.get("path") or "")
        if f_path and any(f_path.startswith(prefix) for prefix in matched_paths):
            matched_folder_ids.add(str(f_obj["folder_id"]))

    for doc in docs:
        doc_id = doc.get("doc_id")
        if not doc_id:
            continue
        doc_fid = doc.get("folder_id") or ""
        doc_fpath = doc.get("folder_path") or ""
        if doc_fid in matched_folder_ids or (
            doc_fpath and any(doc_fpath.startswith(prefix) for prefix in matched_paths)
        ):
            allowed.add(doc_id)

    return allowed


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

    response = await _hub_post(
        client, kb, "/v1/hub/recall", body, namespaces=[namespace], permission="read"
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
    secret = kb.resolved_jwt_secret()
    sub = _text(getattr(kb, "actor_id", "")) or "human:1"
    payload = {
        "sub": sub,
        "user_id": sub,
        "namespaces": namespaces,
        "roles": [],
        "permission": permission,
        "iat": now - 180,
        "exp": now + max(600, kb.jwt_ttl_seconds),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


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
        _hub_login_cache.update(
            {"key": key, "token": token, "expires_at": time.time() + max(600.0, ttl - 600.0)}
        )
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
    response = await client.post(
        _hub_url(kb, path), headers={"Authorization": f"Bearer {token}"}, json=body
    )
    if response.status_code == 401 and kb.hub_username.strip():
        logger.info("Kb hub token rejected (401), re-login and retry path=%s", path)
        token = await _resolve_hub_token(
            kb, namespaces=namespaces, permission=permission, force_refresh=True
        )
        response = await client.post(
            _hub_url(kb, path), headers={"Authorization": f"Bearer {token}"}, json=body
        )
    return response


def _hub_url(kb: KbSettings, path: str) -> str:
    return kb.resolved_base_url() + path


# 库显示名以 RAG 侧 workspace 标题为准（他们改名自动跟随）；本地 label 仅作
# hub 不可达时的兜底。进程内缓存 60s，避免每次打开配置页都打一次 hub。
_workspace_title_cache: dict[str, Any] = {"at": 0.0, "titles": {}}
_WORKSPACE_TITLE_TTL = 60.0


async def _hub_workspace_titles(kb: KbSettings) -> dict[str, str]:
    now = time.monotonic()
    if (
        now - _workspace_title_cache["at"] < _WORKSPACE_TITLE_TTL
        and _workspace_title_cache["titles"]
    ):
        return _workspace_title_cache["titles"]
    # KB146 (:27517) 使用固定正式空间 official_kb，直接返回本地配置标签即可
    if kb.resolved_jwt_secret() and not (kb.hub_username.strip() and kb.hub_password):
        titles = kb.effective_namespaces()
        _workspace_title_cache.update(at=now, titles=titles)
        return titles
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
    except (HTTPException, httpx.HTTPError) as exc:
        logger.debug("Kb workspace titles unavailable, using local labels: %s", exc)
    return _workspace_title_cache["titles"] or {}


async def _probe_hub_detailed(kb: KbSettings) -> dict[str, Any]:
    """探测 KB146 检索接口与文档列表，返回连通性与文档统计。"""
    headers = _build_kb146_headers(kb)
    try:
        async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
            docs_resp = await client.get(
                _hub_url(kb, "/semantic-api/v1/dashboard/documents"),
                params={"namespace": "official_kb", "limit": 200},
                headers=headers,
            )
            query_resp = await client.post(
                _hub_url(kb, "/v2/rag/queries"),
                headers=headers,
                json={
                    "query": "规范",
                    "top_k": 1,
                    "scope_filter": {"namespaces": ["official_kb"]},
                },
            )
        query_ok = query_resp.status_code in {200, 404}
        if docs_resp.status_code < 400:
            data = _safe_json(docs_resp)
            items = data.get("items") if isinstance(data.get("items"), list) else []
            searchable_count = sum(
                1
                for item in items
                if isinstance(item, dict)
                and (
                    (
                        isinstance(item.get("document_status"), dict)
                        and item["document_status"].get("searchable")
                    )
                    or item.get("semantic_status") in {"ready", "degraded"}
                )
            )
            return {
                "ok": query_ok,
                "doc_count": int(data.get("total") or len(items)),
                "searchable_doc_count": searchable_count,
            }
        if query_ok:
            return {"ok": True, "doc_count": None, "searchable_doc_count": None}
    except httpx.HTTPError:
        pass

    # 回退探测旧版 /v1/hub/health
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(_hub_url(kb, "/v1/hub/health"))
        return {"ok": response.status_code < 400, "doc_count": None, "searchable_doc_count": None}
    except httpx.HTTPError:
        return {"ok": False, "doc_count": None, "searchable_doc_count": None}


async def _probe_hub(kb: KbSettings) -> bool:
    info = await _probe_hub_detailed(kb)
    return bool(info.get("ok"))


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
