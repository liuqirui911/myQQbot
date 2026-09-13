"""核心审核插件：实现 AI 消息审核 + 加群请求自动审批。

这是机器人的"业务逻辑"插件，通过钩子（hook）接入 main.py 的核心流程：
- before_check   -> AI 零样本审核（含测试消息 / 短消息短路）
- before_request -> 加群请求自动审批

main.py 只负责事件分发与执行动作（删除 / 记录），审核"决策"逻辑都在这里。
删除本插件（或移出 plugins/）后，机器人将不再做任何 AI 审核。
"""
import anyio

from plugin_loader import PluginMixin


class CoreAuditPlugin(PluginMixin):
    """AI 消息审核 + 加群请求自动审批。"""

    # 加群请求自动审批关键词（命中任一即通过）
    REQUEST_ALLOW_KEYWORDS = ["验证", "申请", "进群", "hello", "hi"]

    def register(self, ctx):
        self.on_hook("before_check", self.ai_check)
        self.on_hook("before_request", self.handle_request)

    # ---- AI 消息审核 ----
    async def ai_check(self, bot, event, text):
        """返回 {'block': bool, 'label': str|None, 'score': float|None}。"""
        stripped = text.strip()
        if stripped == "<TEST_DELETE_MESSAGE>":
            return {"block": True, "label": "TEST_DELETE_MESSAGE", "score": None}
        if len(stripped) < 4:
            print(f"[AI] Too short, pass: {text}")
            return {"block": False, "label": None, "score": None}
        try:
            # 在线程池跑推理，避免阻塞事件循环
            result = await anyio.to_thread.run_sync(self.classify, text)
            print(f"[AI] Checked: '{text[:30]}...' -> label: {result['label'] or 'None'}, score: {result['score']:.2f}, block: {result['block']}")
            if result["block"]:
                print(f"[AI] Blocked: {result['label']} (score={result['score']:.2f}) - '{text[:50]}...'")
            return result
        except Exception as e:
            print(f"[AI Check Error] {e}")
            return {"block": False, "label": None, "score": None}

    # ---- 加群请求审批 ----
    async def handle_request(self, bot, event):
        """返回 True/False 决定是否自动通过加群请求。"""
        comment = getattr(event, "comment", "") or ""
        should_approve = any(kw in comment.lower() for kw in self.REQUEST_ALLOW_KEYWORDS)
        print(f"[Core] 加群请求 user={event.user_id} group={event.group_id} comment={comment!r} -> approve={should_approve}")
        return should_approve
