"""示例插件（类式）：演示 PluginMixin 混入基类 + 钩子。

注意：本文件位于 plugin_example/，不会被自动加载。
要启用请复制到 plugins/ 目录（重启机器人后生效）。

类式插件约定：
- 定义一个继承 plugin_loader.PluginMixin 的类。
- 类必须定义 register(self, ctx) 方法，在其中注册处理器 / 钩子。
- self 上可直接调用 mixin 提供的便捷方法
  （send_group / send_private / delete / call_api / classify / record 等）。
- self.on_hook(name, handler) 可钩住机器人核心审核逻辑
  （before_check / after_check / before_delete / after_block / before_request）。
"""
from plugin_loader import PluginMixin


class PingPlugin(PluginMixin):
    """演示：群里发 'ping' 时回复 'pong'。"""

    def register(self, ctx):
        ctx.on_message(self.on_message)

    async def on_message(self, bot, event):
        text = (event.get_plaintext() if hasattr(event, "get_plaintext") else str(event)).strip()
        group_id = getattr(event, "group_id", None)
        if text.lower() == "ping" and group_id is not None:
            await self.send_group(group_id, "pong")
            print(f"[PingPlugin] 已回复 pong (群 {group_id})")


class KeywordBlockPlugin(PluginMixin):
    """演示：钩住核心审核逻辑 —— 含敏感关键词的消息直接拦截（短路 AI 审核）。

    注意：before_check 按注册顺序执行，第一个返回 dict 的插件会短路后续插件。
    若与 core.py 同在 plugins/，需让本插件文件名字母序在 core.py 之前才会先生效。
    """

    BLOCK_KEYWORDS = ["广告", "加我微信"]

    def register(self, ctx):
        self.on_hook("before_check", self.before_check)

    async def before_check(self, bot, event, text):
        for kw in self.BLOCK_KEYWORDS:
            if kw in text:
                print(f"[KeywordBlockPlugin] 命中关键词 '{kw}'，直接拦截（跳过 AI 审核）")
                return {"block": True, "label": f"keyword:{kw}", "score": 1.0}
        return None
