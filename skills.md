# 小锦 - 技能清单

> 完整的工具和技能介绍文档

---

## 🛠️ 核心技能 (Skills)

### 1. debug - 调试与修复
系统化地诊断和修复 Bug，定位问题根源。

### 2. commit - 代码提交
创建结构清晰、格式规范的 git 提交。

### 3. test - 测试
编写和运行测试用例，验证代码正确性。

### 4. review - 代码审查
审查代码中的 Bug、安全问题和质量隐患。

### 5. plan - 实现计划
在编码之前设计详细的实现方案。

### 6. simplify - 代码简化
将代码重构为更简洁、更易维护的形式。

### 7. diagnose - 故障诊断
分析 agent 运行失败或表现异常的原因，基于证据而非直觉。

### 8. harness-eval - 集成测试
使用真实 API 调用验证功能特性，运行端到端测试。

### 9. pr-merge - PR 合并
审查、合并和集成外部贡献的 Pull Requests。

### 10. cad - CAD 工作流
编程式生成 STEP/STL/3MF/DXF/GLB 文件；重新生成、检查、验证、快照。

### 11. robot-motion - 机器人运动
URDF 逆运动学和路径规划的机器人运动设置、生成和验证。

### 12. urdf - URDF 生成
URDF 文件的创建、编辑、验证和机器人模型定义。

---

## 🔧 可用工具 (Tools)

### 文件操作
| 工具 | 用途 |
|------|------|
| `read_file` | 读取文本文件内容 |
| `write_file` | 创建或覆盖文件 |
| `edit_file` | 编辑文件，替换指定字符串 |
| `notebook_edit` | 创建或编辑 Jupyter Notebook 单元格 |

### 代码智能
| 工具 | 用途 |
|------|------|
| `lsp` | 检查 Python 代码符号、定义、引用和悬停信息 |
| `glob` | 匹配文件路径的搜索 |
| `grep` | 正则表达式内容搜索 |
| `tool_search` | 搜索可用工具列表 |

### 搜索与网络
| 工具 | 用途 |
|------|------|
| `web_search` | 网络搜索 |
| `web_fetch` | 获取单个网页内容 |

### 进程与任务
| 工具 | 用途 |
|------|------|
| `task_create` | 创建后台 Shell 或 Agent 任务 |
| `task_get` | 获取后台任务详情 |
| `task_list` | 列出后台任务 |
| `task_stop` | 停止后台任务 |
| `task_output` | 读取后台任务输出 |
| `task_update` | 更新任务描述、进度或状态 |

### 代理与团队
| 工具 | 用途 |
|------|------|
| `agent` | 派生后台 Agent 任务 |
| `send_message` | 向运行中的 Agent 发送消息 |
| `team_create` | 创建轻量级内存团队 |
| `team_delete` | 删除内存团队 |

### Git 工作区
| 工具 | 用途 |
|------|------|
| `enter_worktree` | 创建 Git worktree |
| `exit_worktree` | 移除 Git worktree |

### 定时任务
| 工具 | 用途 |
|------|------|
| `cron_create` | 创建定时任务 |
| `cron_list` | 列出定时任务 |
| `cron_delete` | 删除定时任务 |
| `cron_toggle` | 启用/禁用定时任务 |
| `remote_trigger` | 立即触发定时任务 |

### 外部资源
| 工具 | 用途 |
|------|------|
| `list_mcp_resources` | 列出可用 MCP 资源 |
| `read_mcp_resource` | 按服务器和 URI 读取 MCP 资源 |
| `internal_api_request` | 调用内部 HTTP API |

### 系统工具
| 工具 | 用途 |
|------|------|
| `bash` | 执行 Shell 命令 |
| `sleep` | 短暂休眠 |
| `brief` | 文本压缩摘要 |
| `config` | 读取或更新配置 |
| `image_to_text` | 图片转文字描述 |
| `ask_user_question` | 向用户提问 |

### 任务管理
| 工具 | 用途 |
|------|------|
| `todo_write` | 添加或标记 TODO 事项 |

### 其他
| 工具 | 用途 |
|------|------|
| `enter_plan_mode` | 切换为计划模式 |
| `exit_plan_mode` | 退出计划模式 |

---

## 📋 使用示例

### 调试 Bug
```
skill: debug
→ 系统化分析错误，定位根因
```

### 代码审查
```
skill: review
→ 检查 Bug、安全问题和代码质量
```

### 编写测试
```
skill: test
→ 生成单元测试和集成测试
```

### 简化代码
```
skill: simplify
→ 重构复杂逻辑，提升可读性
```

---

*小锦 — 让编程变得更简单 🚀*
