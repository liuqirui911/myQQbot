# 插件示例

这里的插件是**示例**，不会被自动加载（自动加载只扫描 `plugins/` 目录）。

要启用某个示例，把它复制到 `plugins/` 目录即可（重启机器人后生效）。

## 示例列表
- `message_handler.py` — 函数式插件：打印所有消息 + 演示 `ctx.classify` / `ctx.on_event`
- `mixin_demo.py` — 类式插件：演示 `PluginMixin`（`send_group` / `on_hook` 等）

## 钩子（hook）速查
通过 `ctx.on_hook(name, handler)` 拦截机器人核心逻辑：

| 钩子点 | 时机 | 返回值 |
|---|---|---|
| `before_check(bot, event, text)` | AI 审核前 | `dict` 短路审核 / `None` 不干预 |
| `after_check(bot, event, text, result)` | 审核后、拦截前 | `dict` 覆盖结果 |
| `before_delete(bot, event, message_id, result)` | 删除前 | `False` 取消删除 |
| `after_block(bot, event, result)` | 拦截（删除+记录）后 | 无 |
| `before_request(bot, event)` | 加群请求审批前 | `True/False` 覆盖决定 |

## 注意
- `before_check` 按注册顺序执行，**第一个返回 dict 的插件会短路**后续插件。
  核心审核逻辑在 `plugins/core.py`（`CoreAuditPlugin`）。若想让关键词拦截等插件
  在 AI 审核前生效，需让其文件名字母序排在 `core.py` 之前。
