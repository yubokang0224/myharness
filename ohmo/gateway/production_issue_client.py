"""The production assistant's only business-data destination is MES."""
from __future__ import annotations

import json
import mimetypes
import base64
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from ohmo.gateway.config import load_gateway_config
from openharness.utils.production_issue_identity import (
    IssueIdentity, IssueIdentityError, consume_attachments, prepare_issue_body,
    resolve_identity,
)

from ohmo.gateway.production_issue_errors import (
    IssueRequestError, field, upstream_failure, invalid_response, transport_failure,
)


@dataclass(frozen=True, repr=False)
class _LoginToken:
    value: str
    expires_at: float


_token_cache: dict[tuple, _LoginToken] = {}
_token_lock = threading.Lock()


def _token_key(config, identity):
    return (config.mes_base_url.rstrip('/'), identity.corp_id,
            identity.app_id, identity.ding_user_id)


async def _login(client, config, identity, action):
    key = _token_key(config, identity)
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and cached.expires_at > time.monotonic():
            return cached.value
    try:
        response = await client.post(config.mes_base_url.rstrip('/') + '/ProductionIssue/Login',
            json={'dingUserId': identity.ding_user_id},
            follow_redirects=False)
    except httpx.HTTPError as exc:
        raise transport_failure(exc, stage='login', action=action, write=False) from None
    if response.is_redirect or response.status_code >= 400:
        raise upstream_failure(response, stage='login', action=action)
    try:
        data = response.json()
    except ValueError:
        raise invalid_response(response, stage='login', action=action) from None
    if field(data, 'isSuccess') is False:
        raise upstream_failure(response, stage='login', action=action)
    result = field(data, 'backResult')
    token = field(result, 'token')
    if field(data, 'isSuccess') is not True or not isinstance(token, str) or not token.strip():
        raise invalid_response(response, stage='login', action=action)
    token = token.strip()
    # Expiry decoding is only a cache hint; MOM validates the JWT on every business call.
    lifetimes = []
    try:
        seconds = float(field(result, 'expiredTime', 0))
        if seconds > 0:
            lifetimes.append(seconds)
    except (TypeError, ValueError):
        pass
    try:
        payload = token.split('.')[1]
        expiry = float(json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))['exp'])
        lifetimes.append(max(0, expiry - time.time()))
    except (IndexError, ValueError, KeyError, TypeError):
        pass
    if lifetimes:
        lifetime = min(*lifetimes, 3600.0)
        with _token_lock:
            for old in [k for k, v in _token_cache.items() if v.expires_at <= time.monotonic()]:
                _token_cache.pop(old, None)
            if len(_token_cache) >= 4096:
                _token_cache.pop(next(iter(_token_cache)))
            _token_cache[key] = _LoginToken(token, time.monotonic() + lifetime - min(30, lifetime / 10))
    return token


READ_ACTIONS = {"Me", "Projects", "Contacts", "Departments", "GetListPaged", "Get", "Attachment"}
WRITE_ACTIONS = {"Insert", "Assign", "Accept", "AppendProcess", "Resolve", "Verify",
                 "Reopen", "Invalidate"}
ALL_ACTIONS = READ_ACTIONS | WRITE_ACTIONS
_ACTION_MAP = {a.lower(): a for a in ALL_ACTIONS}
_QUERY_FIELDS = {
    "Me": set(), "Projects": {"keyword", "projectId"}, "Contacts": {"projectId", "projectNo", "keyword"},
    "Departments": {"scope", "status", "keyword", "projectNo", "projectId"},
    "GetListPaged": {"page", "size", "scope", "status", "keyword", "projectNo", "projectId", "departmentId"},
    "Get": {"id"}, "Attachment": {"id"},
}
_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def issue_action(url: str) -> str:
    path = urlsplit(url).path
    for _ in range(3):
        path = unquote(path)
    parts = path.rstrip("/").split("/")
    if len(parts) < 2 or parts[-2].lower() != "productionissue":
        raise IssueIdentityError("不支持的生产问题接口。")
    action = _ACTION_MAP.get(parts[-1].lower())
    if not action or any(p in {".", ".."} for p in parts):
        raise IssueIdentityError("生产问题接口已升级，请使用明确的业务动作，禁止旧 UpdateStatus。")
    return action


def validated_config(identity: IssueIdentity):
    config = load_gateway_config().production_issue
    required = (config.mes_base_url,)
    if not config.enabled or not all(value.strip() for value in required):
        raise IssueRequestError("生产问题 MOM 连接尚未启用或配置不完整。", stage="config",
            code="MOM_CONFIG_MISSING", status=503,
            details={"missingFields": [name for name in ("mes_base_url",)
                                      if not getattr(config, name).strip()]},
            user_action="问题未提交，请管理员启用 production_issue.enabled 并填写 mes_base_url；公司和应用信息自动关联，无需另行配置。")
    parsed = urlsplit(config.mes_base_url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        raise IssueRequestError("生产问题 MOM 地址配置无效。", stage="config", code="MOM_CONFIG_INVALID", status=503, user_action="请管理员检查 mes_base_url，填写 MOM API 根地址。")
    if (config.app_id and identity.app_id != config.app_id) or (config.corp_id and identity.corp_id
            and identity.corp_id != config.corp_id):
        raise IssueIdentityError("当前钉钉企业或应用与生产问题连接配置不一致。")
    if config.bot_name and identity.bot_name != config.bot_name:
        raise IssueIdentityError("当前机器人没有生产问题权限。")
    return config


async def _send(client: httpx.AsyncClient, config, identity: IssueIdentity,
                method: str, action: str, *, params=None, body: bytes = b"", files=None):
    url = config.mes_base_url.rstrip("/") + "/ProductionIssue/" + action
    kwargs = {"files": files} if files is not None else {"content": body}
    request = client.build_request(method, url, params=params, **kwargs)
    await request.aread()  # Preserve exactly the same multipart bytes on a single 401 retry.
    if files is None and method == "POST":
        request.headers["Content-Type"] = "application/json; charset=utf-8"
    stage = "upload" if action == "UploadAttachment" else "business"
    for attempt in range(2):
        token = await _login(client, config, identity, action)
        request.headers["Authorization"] = "Bearer " + token
        try:
            response = await client.send(request, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise transport_failure(exc, stage=stage, action=action, write=method == "POST") from None
        if response.status_code != 401 or attempt == 1:
            if response.is_redirect:
                raise IssueRequestError("MOM 返回重定向，未继续转发请求。", stage=stage,
                    code="MOM_REDIRECT", action=action, status=502, upstream_status=response.status_code,
                    user_action="请管理员核对 MOM API 根地址，不使用登录网页或跳转地址。")
            return response
        # Only retry a rejected authentication; timeouts/5xx are never auto-replayed.
        with _token_lock:
            key = _token_key(config, identity)
            cached = _token_cache.get(key)
            if cached and cached.value == token:
                _token_cache.pop(key, None)
    raise AssertionError("unreachable")


async def _upload_attachments(body: dict, client: httpx.AsyncClient, config,
                              identity: IssueIdentity) -> None:
    uploaded = []
    for attachment in body.get("attachments") or []:
        if attachment.get("id"):
            uploaded.append({"id": str(attachment["id"])})
            continue
        path = Path(attachment["sourceLocalPath"]).resolve()
        if str(path) not in identity.attachment_paths or not path.is_file():
            raise IssueIdentityError("现场附件已失效，请重新发送。")
        if path.stat().st_size > _MAX_ATTACHMENT_BYTES:
            raise IssueIdentityError("单个现场附件不能超过 20 MB。")
        content = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = await _send(client, config, identity, "POST", "UploadAttachment",
                               files={"file": (path.name, content, mime)})
        try:
            data = response.json()
            result = field(data, "backResult") or {}
            attachment_id = field(result, "id")
        except (ValueError, AttributeError):
            attachment_id = None
            data = {}
        if response.status_code >= 400 or field(data, "isSuccess") is False:
            error = upstream_failure(response, stage="upload", action="UploadAttachment")
            error.payload["details"]["fileName"] = path.name
            raise error
        if field(data, "isSuccess") is not True or not attachment_id:
            raise invalid_response(response, stage="upload", action="UploadAttachment")
        uploaded.append({"id": str(attachment_id)})
    body["attachments"] = uploaded


def _with_action_links(response: httpx.Response, config) -> httpx.Response:
    """Use only MES-authorized issue IDs and their owning company code for links."""
    if response.status_code >= 400 or not config.mobile_base_url:
        return response
    try:
        data = response.json()
    except ValueError:
        return response
    def attach(value):
        if isinstance(value, dict):
            identity = value.get("issueId") or (value.get("id") if "issueNo" in value else None)
            company_code = value.get("companyCode") or ""
            if identity and company_code:
                from urllib.parse import quote
                value["actionUrl"] = (config.mobile_base_url.rstrip("/") + "/?t=" +
                    quote(str(company_code), safe="") + "#/question-detail?id=" + quote(str(identity), safe=""))
            for child in list(value.values()):
                if isinstance(child, (list, dict)):
                    attach(child)
        elif isinstance(value, list):
            for child in value:
                attach(child)
    attach(data)
    return httpx.Response(response.status_code, json=data, request=response.request)


async def execute_issue_request(*, action: str, method: str, capability: object,
                                params: dict | None = None, body=None,
                                timeout: float = 30.0) -> httpx.Response:
    """Invoked locally by the tool, or by an HTTP route with an opaque capability.

    The Python service obtains and caches the bound employee's MOM token.
    Caller-provided identity headers, tokens and model provenance are never used.
    """
    identity = resolve_identity(capability)
    config = validated_config(identity)
    expected_method = "GET" if action in READ_ACTIONS else "POST"
    if action not in ALL_ACTIONS or method.upper() != expected_method:
        raise IssueIdentityError("生产问题动作或请求方法不正确。")
    allowed = _QUERY_FIELDS.get(action, set())
    query = {k: str(v) for k, v in (params or {}).items() if k in allowed}
    if action == "GetListPaged":
        query.setdefault("scope", "related")
    payload = prepare_issue_body(body, action, identity) if method.upper() == "POST" else None
    submitted_paths = tuple(attachment["sourceLocalPath"]
        for attachment in (payload or {}).get("attachments", []) if "sourceLocalPath" in attachment)
    # Check local files before any login or upload, so a bad file does not start submission.
    for local_path in submitted_paths:
        path = Path(local_path)
        if not path.is_file():
            raise IssueIdentityError("现场附件已失效，请重新发送。")
        if path.stat().st_size > _MAX_ATTACHMENT_BYTES:
            raise IssueIdentityError("单个现场附件不能超过 20 MB。")
    async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
        if payload is not None:
            await _upload_attachments(payload, client, config, identity)
        response = await _send(client, config, identity, method.upper(), action, params=query,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                 if payload is not None else b"")
    if response.status_code >= 400:
        raise upstream_failure(response, stage="business", action=action, write=payload is not None)
    if action != "Attachment":
        try:
            result = response.json()
        except ValueError:
            raise invalid_response(response, stage="business", action=action, write=payload is not None) from None
        if field(result, "isSuccess") is False:
            raise upstream_failure(response, stage="business", action=action, write=payload is not None)
        valid = (isinstance(field(result, "data"), list) and field(result, "total") is not None
                 if action == "GetListPaged" else field(result, "isSuccess") is True)
        if not valid:
            raise invalid_response(response, stage="business", action=action, write=payload is not None)
    if submitted_paths and 200 <= response.status_code < 300:
        try:
            result = response.json()
        except ValueError:
            result = None
        if isinstance(result, dict) and field(result, "isSuccess") is True:
            consume_attachments(identity, submitted_paths)
    return _with_action_links(response, config)
