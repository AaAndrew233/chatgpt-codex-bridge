# 更新与维护

## 更新分类

| 变更 | 本机动作 | ChatGPT 动作 |
| --- | --- | --- |
| Bridge 内部逻辑、超时、分页或安全修复 | 同步代码并重启后台 Runtime | 不需要卸载连接器；建议新建对话验证 |
| 新增/删除工具，或修改工具名称、描述、参数、权限注解 | 同步代码并重启 Runtime | 在连接器页面点击 Refresh，然后新建对话 |
| Tunnel ID、Runtime Key 或账号更换 | 重新配置官方 Tunnel Runtime | 删除旧连接器并重新连接 |
| Codex Desktop 更新 | 先运行只读健康检查 | 如果会话索引或侧边栏行为变化，重新验证会话读取 |

## 推荐更新顺序

```text
1. 备份仓库外的 config.json、Runtime Key 和 Tunnel 配置
2. 同步代码
3. 运行公开发布检查和测试
4. 重启 Bridge / Tunnel Runtime
5. 调用 codex_status 查看 bridge.version 和 tool_schema_version
6. 如果 MCP Schema 变了，在 ChatGPT 连接器页面点击 Refresh
7. 新建 ChatGPT 对话，先做 codex_status，再做只读项目测试
```

不要因为普通代码修复而反复卸载连接器。卸载/重新连接只用于地址、Tunnel、账号或授权主体发生变化的情况。

## 执行协议

后台任务使用以下状态，供 ChatGPT 轮询：

```text
INIT       已创建任务，等待执行资源
EXECUTING  Codex 正在执行
EXECUTED   Codex 已完成，ChatGPT 应读取结果并复核
ERROR      执行失败，应读取错误并决定是否重试
CANCELLED  任务被取消，不应继续读取旧结果
```

`PLAN`、`REVIEW` 和 `DONE` 属于 ChatGPT 的规划/复核状态，不由 Bridge 冒充报告。这样可以避免“任务执行完成”被误解为“方案已经审核通过”。

## 连接器陈旧时的诊断

如果 ChatGPT 仍然说“必须先登记项目”，按以下顺序排查：

1. 在新对话调用 `codex_status`，确认 `available_tools` 包含 `codex_prepare_session_access`。
2. 查看 `bridge.version` 和 `tool_schema_version` 是否为当前运行版本。
3. 如果版本正确但工具仍旧缺失，在连接器页面点击 Refresh。
4. 刷新后新建对话，不要继续复用旧对话的工具清单。
5. 如果 Tunnel Runtime 没有转发新的调用，再检查官方 `tunnel-client runtimes status`。

## 发布同步

公开发布前必须同步检查：

- 英文 README 和中文 README
- `.codex-plugin/plugin.json`
- `VERSION`
- `CHANGELOG.md`
- Git Tag 和 GitHub Release
- 安装、更新和安全文档

不得把 Runtime Key、Tunnel ID、个人路径、Cookie 或本地配置文件提交到仓库。
