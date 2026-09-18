"""Production issue gateway: authenticated Stream context to MES only."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ohmo.gateway.production_issue_client import execute_issue_request, issue_action, READ_ACTIONS, WRITE_ACTIONS
from openharness.utils.production_issue_identity import IssueIdentityError
from ohmo.gateway.production_issue_errors import IssueRequestError, identity_failure

router = APIRouter(prefix="/ProductionIssue", tags=["ProductionIssue"])


async def production_issue_proxy(action: str, request: Request) -> Response:
    # This is a random process-local capability, not a user ID or a channel claim.
    # Source headers, posted identity fields and generic service JWTs grant nothing.
    capability = request.headers.get("X-OHMO-Issue-Context")
    try:
        canonical_action = issue_action("/ProductionIssue/" + action)
        body = await request.json() if request.method == "POST" else None
        upstream = await execute_issue_request(action=canonical_action, method=request.method,
            capability=capability, params=dict(request.query_params), body=body)
    except IssueRequestError as exc:
        upstream = exc.response()
    except IssueIdentityError as exc:
        upstream = identity_failure(exc, action).response()
    except ValueError:
        upstream = IssueRequestError("请求 JSON 格式不正确。", stage="prepare",
            code="ISSUE_INPUT_INVALID", action=action).response()
    except OSError:
        upstream = IssueRequestError("无法读取本次现场附件。", stage="upload", action=action,
            code="ISSUE_ATTACHMENT_UNAVAILABLE", user_action="请重新发送附件后重试，问题尚未提交。").response()
    return Response(upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type"))


def _action_endpoint(action: str):
    async def endpoint(request: Request):
        return await production_issue_proxy(action, request)
    endpoint.__name__ = "production_issue_" + action.lower()
    return endpoint


for _action in sorted(READ_ACTIONS | WRITE_ACTIONS):
    router.add_api_route("/" + _action, _action_endpoint(_action),
                         methods=["GET" if _action in READ_ACTIONS else "POST"])
