"""示例插件（函数式）：打印收到的所有消息，并演示插件 API。

注意：本文件位于 plugin_example/，不会被自动加载。
要启用请复制到 plugins/ 目录（重启机器人后生效）。

插件约定：
- 定义模块级 register(ctx)，通过 ctx.on_message(handler) 注册消息处理器，
  或通过 ctx.on_event(handler) 注册全事件处理器。
- handler 签名为 async def handler(bot, event)，event 为 OneBot v11 事件对象。
- 可用 ctx.bot / ctx.classifier / ctx.classify / ctx.webui / ctx.config /
  ctx.candidate_labels / ctx.safe_labels / ctx.threshold 访问机器人能力。
- 推理框架也可由插件重写：ctx.register_classifier / set_classifier / use_classifier
  / list_classifiers（示例见 inference_backend.py）。
"""


def register(ctx):
    async def on_message(bot, event):
        user_id = getattr(event, "user_id", "Unknown")
        message = event.get_plaintext() if hasattr(event, "get_plaintext") else str(event)
        group_id = getattr(event, "group_id", None)
        message_id = getattr(event, "message_id", None)
        print(f"[message_handler] 收到消息 来自 {user_id} (群 {group_id}): {message} | msg_id={message_id}")
        # 演示 ctx.classify：用当前推理框架做分类（热重载后自动用新标签；分数可能为 None）
        if message.strip():
            try:
                r = ctx.classify(message)
                print(f"[message_handler] 分类: {r['label']} (score={r['score']}, block={r['block']})")
            except Exception as e:
                print(f"[message_handler] 分类失败: {e}")

    def on_event(bot, event):
        # 演示 ctx.on_event：所有事件（消息 / 请求 / 其他）都会触发
        etype = type(event).__name__
        if etype != "MessageEvent":
            print(f"[message_handler] 事件: {etype} | bot={getattr(bot, 'self_id', None)}")

    ctx.on_message(on_message)
    ctx.on_event(on_event)
