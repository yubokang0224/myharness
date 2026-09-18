"""Business-facing failure details shared by the issue tool and HTTP gateway."""
from __future__ import annotations

import re

import httpx

from openharness.utils.production_issue_identity import IssueIdentityError


def field(data, name, default=None):
    if isinstance(data, dict):
        return next((v for k, v in data.items() if k.lower() == name.lower()), default)
    return default


def safe_text(value) -> str:
    text = str(value or "").strip()
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [已隐藏]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[token 已隐藏]", text)
    text = re.sub(r"(?i)((?:access_?token|token|password|secret|authorization)\s*[=:]\s*)[^\s,;]+",
                  r"\1[已隐藏]", text)
    return text[:2000]


def upstream_details(response: httpx.Response) -> tuple[str, dict]:
    try:
        data = response.json()
    except ValueError:
        data = None
    reasons = []
    for name in ("errorMessage", "message", "detail", "title"):
        value = field(data, name)
        if isinstance(value, str) and value.strip() and safe_text(value) not in reasons:
            reasons.append(safe_text(value))
    errors = field(data, "errors", {})
    fields = {}
    if isinstance(errors, dict):
        for name, value in list(errors.items())[:30]:
            if re.search(r"(?i)token|password|secret|authorization", name):
                continue
            values = value if isinstance(value, list) else [value]
            fields[safe_text(name)] = [safe_text(v) for v in values[:10] if isinstance(v, (str, int, float))]
    detail = field(data, "detail")
    if isinstance(detail, list):
        for item in detail[:30]:
            if isinstance(item, dict) and item.get("msg"):
                name = ".".join(str(p) for p in item.get("loc", [])) or "request"
                if not re.search(r"(?i)token|password|secret|authorization", name):
                    fields.setdefault(safe_text(name), []).append(safe_text(item["msg"]))
    # Do not return raw HTML, exception dumps, request bodies or token payloads.
    return "；".join(reasons), fields


class IssueRequestError(IssueIdentityError):
    def __init__(self, message: str, *, stage: str, code: str, action: str = "",
                 status: int = 400, upstream_status: int | None = None,
                 details: dict | None = None, user_action: str = "请根据具体原因处理后再提交。",
                 retryable: bool = False, outcome: str = "not_submitted"):
        super().__init__(message)
        self.status = status
        self.payload = {
            "isSuccess": False, "errorCode": code, "stage": stage, "action": action,
            "errorMessage": safe_text(message), "details": details or {},
            "userAction": user_action, "retryable": retryable, "outcome": outcome,
        }
        if upstream_status is not None:
            self.payload["upstreamStatus"] = upstream_status

    def response(self, request=None) -> httpx.Response:
        return httpx.Response(self.status, json=self.payload, request=request)


def identity_failure(exc: IssueIdentityError, action: str = "") -> IssueRequestError:
    return IssueRequestError(str(exc), stage="prepare", code="ISSUE_CONTEXT_INVALID",
        action=action, status=403,
        user_action="按具体错误处理；缺少钉钉消息身份上下文时，请管理员检查 Python 通道及会话传递。未收到 MOM 登录接口的绑定错误前，不要判断用户未绑定账号。")


def upstream_failure(response: httpx.Response, *, stage: str, action: str,
                     write: bool = False) -> IssueRequestError:
    message, fields = upstream_details(response)
    status = response.status_code
    code = "MOM_REQUEST_REJECTED"
    advice = "按接口给出的具体原因补充或修正信息后再提交；不要更换身份绕过校验。"
    outcome = "rejected" if write else "not_applicable"
    retryable = False
    if stage == "login":
        code, outcome = "MOM_LOGIN_FAILED", "not_submitted"
        advice = "问题尚未提交。请按返回原因检查钉钉员工与系统账号、组织的绑定或账号状态；需要维护时联系管理员。"
        if status == 404:
            code = "MOM_LOGIN_ENDPOINT_MISSING"
            message = "MOM 自动关联登录接口未找到。" + ("接口提示：" + message if message else "")
            advice = "问题尚未提交。请管理员核对 mes_base_url，并确认 MOM 已发布 ProductionIssue/Login 新接口；不要判断为账号未绑定，也不要要求用户填写公司编码或 AppKey。"
    elif stage == "upload":
        code, outcome = "ISSUE_ATTACHMENT_FAILED", "not_submitted"
        advice = "问题业务请求尚未提交。按附件失败原因处理后重试，不要告知用户问题已保存。"
    elif status == 401:
        code = "MOM_LOGIN_EXPIRED"
        advice = "自动重新登录后仍未通过身份验证。请联系管理员检查 MOM 登录配置，暂不继续提交。"
    elif status == 409:
        code = "ISSUE_VERSION_CONFLICT"
        advice = "重新查询这条问题的最新详情，核对变更后再按用户意图操作，不覆盖他人的处理。"
    elif status == 403:
        code = "ISSUE_ACCESS_DENIED"
        advice = "说明接口返回的权限或账号原因，需要维护时联系管理员；不要改用其他人的身份。"
    elif status == 404:
        code = "ISSUE_NOT_FOUND"
        advice = "核对项目或问题标识；如接口地址不存在，请管理员检查 Python 配置的 MOM 地址和已部署接口。"
    if status >= 500:
        retryable = True
        if stage == "business" and write:
            outcome = "unknown"
            advice = "提交结果未确认。先查询记录核对是否已保存；重试时保留原请求信息，不能另建一条。"
        elif stage == "login":
            advice = "问题尚未提交。登录服务异常，请管理员检查 MOM 服务，恢复后重试。"
    if not message:
        message = {
            "login": "MOM 登录失败，接口未提供具体原因。",
            "upload": "附件上传失败，接口未提供具体原因。",
        }.get(stage, "MOM 请求失败，接口未提供具体原因。")
    return IssueRequestError(message, stage=stage, code=code, action=action,
        status=status if status >= 400 else 400, upstream_status=status,
        details={"fieldErrors": fields} if fields else {}, user_action=advice,
        retryable=retryable, outcome=outcome)


def invalid_response(response, *, stage: str, action: str, write: bool = False):
    unknown = stage == "business" and write
    query = stage == "business" and not write
    return IssueRequestError("MOM 返回内容不完整或格式不正确，无法确认本次操作结果。",
        stage=stage, code="MOM_INVALID_RESPONSE", action=action, status=502,
        upstream_status=response.status_code, outcome="unknown" if unknown else "not_applicable" if query else "not_submitted",
        user_action="先查询问题核对是否已保存，不要重复新增。" if unknown else "请管理员检查接口返回格式后重试；不能将无效响应当成操作成功或没有记录。")


def transport_failure(exc, *, stage: str, action: str, write: bool):
    timeout = isinstance(exc, httpx.TimeoutException)
    before_send = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
    unknown = stage == "business" and write and not before_send
    return IssueRequestError(
        ("MOM 请求超时，未收到完整响应。" if timeout else "无法与 MOM 完成通信。"),
        stage=stage, code="MOM_TIMEOUT" if timeout else "MOM_CONNECTION_FAILED", action=action,
        status=504 if timeout else 502, details={"failureType": type(exc).__name__},
        retryable=True, outcome="unknown" if unknown else "not_applicable" if stage == "business" and not write else "not_submitted",
        user_action="提交结果未确认，请先查询记录；不要直接重复新增。" if unknown
            else "问题业务请求尚未完成提交，请管理员检查 MOM 地址、网络或服务状态，恢复后重试。")
