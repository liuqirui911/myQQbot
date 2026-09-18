# 插件示例

这里的插件是**示例**，不会被自动加载（自动加载只扫描 `plugins/` 目录）。

要启用某个示例，把它复制到 `plugins/` 目录即可（重启机器人后生效）。

## 示例列表
- `message_handler.py` — 函数式插件：打印所有消息 + 演示 `ctx.classify` / `ctx.on_event`
- `mixin_demo.py` — 类式插件：演示 `PluginMixin`（`send_group` / `on_hook` 等）
- `inference_backend.py` — 重写模型推理框架：用关键词规则接管默认 HF 模型（不下载、不占显存），含运行时可切换框架的演示

## 重写模型推理框架速查
框架 = 可调用对象 `fn(text, labels)`，同步 / 异步（`async def`）都支持，返回值支持
`{'labels','scores'}`（HF 原生）/ `{'label','score'[, 'block']}` / `'标签名'` / `('标签名', 0.9)` / `None`（放行）。

| 需求 | 写法 |
|---|---|
| 接管默认框架（默认 HF 模型不再加载） | `ctx.register_classifier(factory, name="x", priority=1)` |
| 只注册成备用框架，之后切换 | `ctx.register_classifier(factory, priority=0)` + `ctx.use_classifier("x")` |
| 已有实例直接替换 | `ctx.set_classifier(fn, name="manual")` |
| 查看已注册框架 | `ctx.list_classifiers()` |
| 分类 | `ctx.classify(text)`（同步）/ `await ctx.aclassify(text)`（异步框架必须用这个） |

- 工厂惰性调用：只有在首次真正分类时才构建框架实例
- 默认框架优先级 0，插件框架 `priority>0` 且高于当前激活框架时自动接管
- `use_classifier` / `set_classifier` 之后会固定选择，不再按优先级自动切换

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
