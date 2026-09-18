"""Ephemeral identities issued only by the authenticated DingTalk Stream adapter.

Tokens live in this process, never in prompts, URLs, model arguments or snapshots.
Untrusted HTTP metadata cannot create one. A restart intentionally invalidates them.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import hashlib
import hmac
import secrets
import threading
import time
from typing import Any


class IssueIdentityError(ValueError):
    pass


@dataclass(frozen=True)
class IssueIdentity:
    corp_id: str
    ding_user_id: str
    raw_sender_id: str
    app_id: str
    bot_name: str
    sender_name: str
    message_id: str
    conversation_id: str
    attachment_paths: tuple[str, ...]
    session_key: str = ""
    attachment_batch_id: str = ""


_identities: dict[str, tuple[float, IssueIdentity]] = {}
_lock = threading.Lock()
_TTL = 3600
_MAX_CONTEXTS = 10000


def register_stream_identity(*, corp_id: str, payload_corp_id: str, staff_id: str,
                             raw_sender_id: str, app_id: str, bot_name: str,
                             sender_name: str, message_id: str,
                             conversation_id: str, attachment_paths: list[str]) -> str:
    """Called after the Stream SDK authenticated the app and sender allowlist passed.

    The raw senderId is for auditing only; it is never substituted for staffId.
    An explicit enterprise mismatch or missing staff/app/message ID allows ordinary chat,
    but cannot obtain a production workflow capability.
    """
    # corp_id is optional provenance; existing MOM login needs staff ID and company code.
    if not all((staff_id, app_id, message_id)):
        return ""
    if corp_id and payload_corp_id and payload_corp_id != corp_id:
        return ""
    now = time.monotonic()
    identity = IssueIdentity(payload_corp_id or corp_id, staff_id, raw_sender_id, app_id, bot_name,
                             sender_name, message_id, conversation_id,
                             tuple(str(Path(p).resolve()) for p in attachment_paths),
                             attachment_batch_id=secrets.token_hex(16) if attachment_paths else "")
    token = secrets.token_urlsafe(32)
    with _lock:
        for key in [k for k, (expiry, _) in _identities.items() if expiry <= now]:
            _identities.pop(key, None)
        if len(_identities) >= _MAX_CONTEXTS:
            _identities.pop(next(iter(_identities)))
        _identities[token] = (now + _TTL, identity)
    return token


def _resolve_identity_locked(token: object) -> tuple[float, IssueIdentity]:
    """Resolve an entry while the caller holds _lock."""
    if not isinstance(token, str) or not token:
        raise IssueIdentityError("Python 未收到本次钉钉消息的员工身份上下文，尚未调用 MOM 登录或业务接口。请管理员检查钉钉通道和会话身份传递；此错误不能判断账号是否绑定。")
    entry = _identities.get(token)
    if entry is None or entry[0] <= time.monotonic():
        _identities.pop(token, None)
        raise IssueIdentityError("钉钉身份上下文已失效，请重新发送消息。")
    return entry


def resolve_identity(token: object) -> IssueIdentity:
    with _lock:
        return _resolve_identity_locked(token)[1]


def bind_session(token: str, session_key: str, previous_token: object = None) -> None:
    # Read and replace under the same lock as consumption: a stale copy must
    # never restore attachments after another context submitted them.
    with _lock:
        expires, identity = _resolve_identity_locked(token)
        paths = identity.attachment_paths
        batch_id = identity.attachment_batch_id
        if not paths and previous_token and identity.conversation_id.strip():
            try:
                previous = _resolve_identity_locked(previous_token)[1]
                if (previous.corp_id, previous.ding_user_id, previous.app_id,
                    previous.session_key, previous.conversation_id) == (
                    identity.corp_id, identity.ding_user_id, identity.app_id,
                    session_key, identity.conversation_id
                ):
                    paths = previous.attachment_paths
                    batch_id = previous.attachment_batch_id
            except IssueIdentityError:
                pass
        _identities[token] = (expires, replace(identity, session_key=session_key,
                                              attachment_paths=paths,
                                              attachment_batch_id=batch_id))


def consume_attachments(identity: IssueIdentity, submitted_paths: tuple[str, ...]) -> None:
    """Forget successfully submitted local files in every context of their batch.

    Identity capabilities remain valid. Failed or unconfirmed requests must not
    call this function, and unsubmitted files in a partially used batch remain.
    """
    if not identity.attachment_batch_id or not submitted_paths:
        return
    consumed = set(submitted_paths)
    with _lock:
        for token, (expires, current) in _identities.items():
            if current.attachment_batch_id != identity.attachment_batch_id:
                continue
            remaining = tuple(path for path in current.attachment_paths if path not in consumed)
            _identities[token] = (expires, replace(current, attachment_paths=remaining,
                attachment_batch_id=current.attachment_batch_id if remaining else ""))


def _normalized_key(key: str) -> str:
    return key.replace("_", "").lower()


_ACTOR_FIELDS = {"userid", "empid", "companyid", "reporterempid", "reporteruserid",
                 "departmentid", "departmentname",
                 "reportername", "operatorid", "operatoruserid", "operatorempid",
                 "operatorname", "creatorid", "createrid", "modifierid",
                 "dinguserid", "corpid", "appid", "status", "statusafter", "statusbefore"}


def clean_identity_fields(value: Any) -> Any:
    """Remove all model supplied provenance/actor/state fields, including aliases."""
    if isinstance(value, dict):
        return {k: clean_identity_fields(v) for k, v in value.items()
                if not _normalized_key(k).startswith("source")
                and _normalized_key(k) not in _ACTOR_FIELDS}
    if isinstance(value, list):
        return [clean_identity_fields(v) for v in value]
    return value


def prepare_issue_body(body: Any, action: str, identity: IssueIdentity) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise IssueIdentityError("生产问题操作需要 JSON 对象。")
    # sourceLocalPath is the only model-supplied source field accepted, and only
    # when it exactly names a file received in this verified conversation.
    requested_attachments = body.get("attachments")
    result = clean_identity_fields(body)
    result.pop("idempotency_key", None)
    for key in ("issueId", "projectId", "contactEmpId"):
        if result.get(key) is not None:
            result[key] = str(result[key])
    if not result.get("contactEmpId"):
        result.pop("contactEmpId", None)
    issue_id = str(result.get("issueId") or "")
    seed = "\n".join((identity.corp_id, identity.ding_user_id, identity.app_id,
                      identity.message_id, action.lower(), issue_id))
    result["idempotencyKey"] = "issue:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()
    provenance = {
        "sourceChannel": "dingtalk", "sourceSenderId": identity.ding_user_id,
        "sourceRawSenderId": identity.raw_sender_id, "sourceCorpId": identity.corp_id,
        "sourceAppId": identity.app_id, "sourceSenderName": identity.sender_name,
        "sourceMessageId": identity.message_id, "sourceConversationId": identity.conversation_id,
        "sourceChatId": identity.ding_user_id, "sourceSessionKey": identity.session_key,
    }
    result.update(provenance)
    records = result.get("processRecords")
    if isinstance(records, list):
        for record in records:
            if isinstance(record, dict):
                record.update(provenance)
    attachments = []
    if "attachments" in body:
        if not isinstance(requested_attachments, list):
            raise IssueIdentityError("附件必须为列表。")
        for attachment in requested_attachments:
            if not isinstance(attachment, dict):
                raise IssueIdentityError("附件格式不正确。")
            if attachment.get("id"):
                attachments.append({"id": str(attachment["id"])})
                continue
            raw = attachment.get("sourceLocalPath")
            if not raw:
                raise IssueIdentityError("附件缺少已上传 ID 或本次会话收到的文件。")
            path = str(Path(str(raw)).resolve())
            if path not in identity.attachment_paths:
                raise IssueIdentityError("附件不属于当前已验证的钉钉会话。")
            attachments.append({"sourceLocalPath": path})
    elif action in {"Insert", "AppendProcess", "Resolve"}:
        attachments = [{"sourceLocalPath": p} for p in identity.attachment_paths]
    result["attachments"] = attachments
    # Keep initial process attachments on the problem itself. Nested arbitrary
    # paths/URLs must not bypass the same upload authorization.
    for record in result.get("processRecords") or []:
        if isinstance(record, dict):
            record.pop("attachments", None)
    if action in {"Insert", "Assign"}:
        result["ccProjectManager"] = result.get("ccProjectManager") is True
    return result


def canonical_signature(method: str, path_query: str, timestamp: str, nonce: str,
                        corp_id: str, ding_user_id: str, body: bytes, secret: str) -> str:
    canonical = "\n".join((method.upper(), path_query, timestamp, nonce, corp_id,
                            ding_user_id, hashlib.sha256(body).hexdigest()))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def signed_headers(method: str, path_query: str, body: bytes, *, identity: IssueIdentity,
                   key_id: str, secret: str, timestamp: str | None = None,
                   nonce: str | None = None) -> dict[str, str]:
    timestamp = timestamp or str(int(time.time()))
    nonce = nonce or secrets.token_hex(16)
    return {"X-Issue-Key-Id": key_id, "X-Issue-Timestamp": timestamp,
            "X-Issue-Nonce": nonce, "X-Issue-Corp-Id": identity.corp_id,
            "X-Issue-Ding-User-Id": identity.ding_user_id,
            "X-Issue-Signature": canonical_signature(method, path_query, timestamp, nonce,
                identity.corp_id, identity.ding_user_id, body, secret)}
