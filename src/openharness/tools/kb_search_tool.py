# -*- coding: utf-8 -*-
"""企业知识库（KB146 / official_kb）原生只读检索工具：kb_search。"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from openharness.config import load_settings
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)


class KbSearchToolInput(BaseModel):
    """知识库语义检索参数。"""

    query: str = Field(
        description=(
            "检索问题或关键词（建议使用自然语言完整描述问题或提取核心技术短语，"
            "例如：'电缆走线规范'、'合成检查周期为多少天'、'装配钳工基本规范'）"
        )
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="返回最相关的文档切片条数，默认 5 条",
    )
    namespaces: list[str] | None = Field(
        default=None,
        description="可选，指定检索的知识库命名空间列表，默认使用 ['official_kb']",
    )


class KbSearchTool(BaseTool):
    """在企业正式知识库（KB146 / official_kb）中执行混合语义检索。"""

    name = "kb_search"
    description = (
        "在企业正式知识库（KB146 / official_kb）中混合语义检索规章制度、工艺参数、"
        "电气/钳工/设备标准、培训资料与历史经验，返回高相关文档切片（含文档名、章节路径、页码与原文）。"
    )
    input_model = KbSearchToolInput

    def is_read_only(self, arguments: KbSearchToolInput) -> bool:
        del arguments
        return True

    async def execute(
        self,
        arguments: KbSearchToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        agent_name = None
        if context and isinstance(getattr(context, "metadata", None), dict):
            agent_name = str(context.metadata.get("active_agent_name") or "").strip() or None
        settings = load_settings()
        kb = settings.kb
        if not kb.is_configured:
            return ToolResult(
                output=json.dumps(
                    {
                        "success": False,
                        "error": "知识库尚未配置（缺少 KB146 地址或 KB_JWT_SECRET 密钥）",
                        "chunks": [],
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )

        try:
            from ohmo.gateway.routers.kb import execute_kb_recall

            result: dict[str, Any] = await execute_kb_recall(
                kb=kb,
                query=arguments.query,
                namespaces=arguments.namespaces,
                top_k=arguments.top_k,
                agent_name=agent_name,
            )
        except Exception as exc:
            logger.warning("kb_search execution failed query=%r err=%s", arguments.query, exc)
            return ToolResult(
                output=json.dumps(
                    {
                        "success": False,
                        "query": arguments.query,
                        "error": f"知识库检索失败：{exc}",
                        "chunks": [],
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )

        chunks = result.get("chunks") or []
        note = (
            f"在知识库中检索到 {len(chunks)} 条相关切片。"
            if chunks
            else "知识库中暂未收录与该查询直接匹配的文档切片（已正常完成检索，无需盲目重复重试同一词条）。"
        )
        payload = {
            "success": True,
            "query": arguments.query,
            "namespaces": result.get("namespaces") or ["official_kb"],
            "total": len(chunks),
            "note": note,
            "chunks": chunks,
        }
        return ToolResult(
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            metadata={"kb_search": True, "total": len(chunks)},
        )
