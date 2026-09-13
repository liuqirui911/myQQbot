"""插件系统：自动发现并加载 plugins/ 目录下的插件。

约定：
- 每个插件是 plugins/ 下的一个 .py 文件（跳过 _ 开头的文件）。
- 两种写法（二选一）：
  1) 函数式：定义模块级函数 register(ctx: PluginContext) -> None。
  2) 类式：定义一个继承 PluginMixin 的类，并实现 register(self, ctx) 方法。
- 在 register 中通过 ctx.on_message(handler) 注册消息处理器，
  或通过 ctx.on_event(handler) 注册全事件处理器；
  handler 签名为 async def handler(bot, event)，event 为 OneBot v11 事件对象。
- 插件 API（ctx 上可用）：
    ctx.bot            机器人实例（连接后可用），可 send_msg / delete_msg / call_api 等
    ctx.classifier     零样本分类 pipeline，用法: classifier(text, labels)
    ctx.classify(text) 便捷分类：用当前候选标签分类，返回 {'label','score','block'}
    ctx.webui          webui 模块，可调用 record_message / get_labels 等
    ctx.config         NoneBot driver 配置对象（.env 中的配置项）
    ctx.candidate_labels / ctx.safe_labels / ctx.threshold  当前标签与阈值（热重载后自动更新）
- 类式插件可直接在 self 上调用 PluginMixin 提供的便捷方法
  （send_group / send_private / delete / call_api / classify / record 等）。
- 钩住核心逻辑：通过 ctx.on_hook(name, handler) 注册钩子处理器，拦截机器人审核流程：
    before_check / after_check / before_delete / after_block / before_request
- 单个插件加载失败只打印错误并跳过，不影响机器人启动。
"""
import asyncio
import importlib.util
import inspect
import os
import sys

PLUGINS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")


class PluginContext:
    """传给插件 register(ctx) 的上下文对象。"""

    def __init__(self, classifier=None, webui=None, config=None, main=None):
        self._bot = None
        self._classifier = classifier
        self._webui = webui
        self._config = config
        self._main = main
        self._message_handlers: list = []
        self._event_handlers: list = []
        self._hooks: dict = {}

    @property
    def bot(self):
        """机器人实例（NapCat 连接后可用，之前为 None）。
        可调用 send_msg / delete_msg / get_group_member_list / call_api 等 OneBot v11 API。"""
        return self._bot

    @bot.setter
    def bot(self, value):
        """由 main.py 在 NapCat 连接/断开时注入。"""
        self._bot = value

    @property
    def classifier(self):
        """零样本分类 pipeline，用法: classifier(text, labels) -> {'labels': [...], 'scores': [...]}"""
        return self._classifier

    @property
    def webui(self):
        """webui 模块，可调用 record_message / get_labels / get_napcat_status 等"""
        return self._webui

    @property
    def config(self):
        """NoneBot driver 配置对象（.env 中的配置项）"""
        return self._config

    @property
    def candidate_labels(self):
        """当前候选标签列表（WebUI 热重载后自动更新）"""
        return list(self._main.candidate_labels) if self._main else []

    @property
    def safe_labels(self):
        """当前安全标签集合（WebUI 热重载后自动更新）"""
        return set(self._main.safe_labels) if self._main else set()

    @property
    def threshold(self):
        """当前拦截阈值"""
        return self._main.threshold if self._main else 0.7

    def classify(self, text: str) -> dict:
        """用当前候选标签对文本做零样本分类（同步，勿在事件循环中阻塞调用大文本）。
        返回 {'label': str, 'score': float, 'block': bool}，
        block = score > threshold 且 label 不在安全标签中。"""
        result = self._classifier(text, self.candidate_labels)
        label = result["labels"][0]
        score = result["scores"][0]
        return {
            "label": label,
            "score": score,
            "block": (score > self.threshold) and (label not in self.safe_labels),
        }

    def on_message(self, handler):
        """注册消息处理器。handler 为 async def handler(bot, event) 或同步函数。
        所有处理器在每条消息事件上按注册顺序依次调用。"""
        if not callable(handler):
            raise TypeError("handler 必须是可调用对象")
        self._message_handlers.append(handler)
        return handler

    def on_event(self, handler):
        """注册全事件处理器。handler 为 async def handler(bot, event) 或同步函数。
        所有处理器在每条事件（消息 / 请求 / 其他）上按注册顺序依次调用。"""
        if not callable(handler):
            raise TypeError("handler 必须是可调用对象")
        self._event_handlers.append(handler)
        return handler

    def on_hook(self, name, handler):
        """注册钩子处理器，拦截机器人核心逻辑。handler 为 async def 或同步函数。

        可用钩子点（name）及签名：
        - before_check(bot, event, text) -> dict|None
            AI 审核前调用。返回 dict 可短路 AI 审核并直接作为审核结果
            （如 {'block': True, 'label': 'manual', 'score': 1.0}）；返回 None 表示不干预。
        - after_check(bot, event, text, result) -> dict|None
            AI 审核后、拦截动作前调用。返回 dict 可覆盖审核结果。
        - before_delete(bot, event, message_id, result) -> bool|None
            删除被拦截消息前调用。返回 False 可取消删除。
        - after_block(bot, event, result) -> None
            拦截动作（删除 + 记录）完成后调用。
        - before_request(bot, event) -> bool|None
            处理加群请求前调用。返回 True/False 可覆盖自动审批决定。
        """
        if not callable(handler):
            raise TypeError("handler 必须是可调用对象")
        self._hooks.setdefault(name, []).append(handler)
        return handler

    async def run_hook(self, name, *args):
        """按注册顺序执行某钩子的所有处理器，返回非 None 的返回值列表。
        单个处理器异常只打印，不影响其他处理器与主流程。"""
        results = []
        for handler in self._hooks.get(name, []):
            try:
                ret = handler(*args)
                if inspect.isawaitable(ret):
                    ret = await ret
                if ret is not None:
                    results.append(ret)
            except Exception as e:
                print(f"[Plugins] 钩子 {name} 处理器 {getattr(handler, '__name__', handler)} 执行失败: {e}")
        return results


class PluginMixin:
    """类式插件的混入基类：把 ctx 的能力封装成 self 上的便捷方法。

    用法（类式插件）：
        from plugin_loader import PluginMixin

        class MyPlugin(PluginMixin):
            def register(self, ctx):
                ctx.on_message(self.on_message)

            async def on_message(self, bot, event):
                await self.send_group(event.group_id, "hi")
                r = self.classify(event.get_plaintext())

    也可在函数式插件里直接实例化当 helper 用：
        def register(ctx):
            helper = PluginMixin(ctx)
            ...

    注意：依赖 ctx.bot 的方法（send_* / delete / call_api）需在 NapCat 连接后调用。
    """

    def __init__(self, ctx):
        self.ctx = ctx

    # ---- 属性透传 ----
    @property
    def bot(self):
        return self.ctx.bot

    @property
    def classifier(self):
        return self.ctx.classifier

    @property
    def webui(self):
        return self.ctx.webui

    @property
    def config(self):
        return self.ctx.config

    @property
    def candidate_labels(self):
        return self.ctx.candidate_labels

    @property
    def safe_labels(self):
        return self.ctx.safe_labels

    @property
    def threshold(self):
        return self.ctx.threshold

    # ---- 消息 ----
    async def send(self, message_type, target_id, text):
        """发送消息。message_type: 'group' | 'private' | 'friend' 等。"""
        return await self.ctx.bot.send_msg(message_type, target_id, text)

    async def send_group(self, group_id, text):
        return await self.send("group", group_id, text)

    async def send_private(self, user_id, text):
        return await self.send("private", user_id, text)

    async def delete(self, message_id):
        return await self.ctx.bot.delete_msg(message_id)

    async def call_api(self, api, **kwargs):
        return await self.ctx.bot.call_api(api, **kwargs)

    # ---- 分类 ----
    def classify(self, text):
        return self.ctx.classify(text)

    # ---- 审核记录 ----
    def record(self, user_id, group_id, message_id, text, label, score, status):
        return self.ctx.webui.record_message(
            user_id, group_id, message_id, text, label, score, status
        )

    # ---- 钩子 ----
    def on_hook(self, name, handler):
        return self.ctx.on_hook(name, handler)


class PluginManager:
    """插件管理器：拥有 PluginContext，负责加载插件、分发事件、执行钩子。

    用法（main.py）：
        manager = PluginManager(classifier=..., webui=..., config=..., main=...)
        manager.load()
        ...
        manager.bot = bot                        # NapCat 连接时注入
        await manager.dispatch_message(bot, ev)  # 分发消息事件
        await manager.dispatch_event(bot, ev)    # 分发全事件
        await manager.run_hook("before_check", bot, ev, text)
    """

    def __init__(self, classifier=None, webui=None, config=None, main=None, plugins_dir=None):
        self.ctx = PluginContext(classifier=classifier, webui=webui, config=config, main=main)
        self._plugins_dir = plugins_dir or PLUGINS_DIR
        self._loaded: list = []

    @property
    def bot(self):
        """机器人实例（NapCat 连接后可用）。"""
        return self.ctx.bot

    @bot.setter
    def bot(self, value):
        self.ctx.bot = value

    @property
    def loaded(self):
        """已加载的插件文件名列表。"""
        return list(self._loaded)

    def load(self) -> None:
        """扫描 plugins/ 目录，加载所有插件。"""
        if not os.path.isdir(self._plugins_dir):
            print(f"[Plugins] 插件目录不存在: {self._plugins_dir}")
            return
        files = sorted(
            f for f in os.listdir(self._plugins_dir)
            if f.endswith(".py") and not f.startswith("_")
        )
        if not files:
            print("[Plugins] 未发现插件")
            return
        for fname in files:
            path = os.path.join(self._plugins_dir, fname)
            name = f"plugins.{fname[:-3]}"
            try:
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)

                # 方式一：模块级 register(ctx) 函数
                register = getattr(module, "register", None)
                if register is not None:
                    result = register(self.ctx)
                    if inspect.isawaitable(result):
                        # 允许 async def register(ctx)
                        asyncio.get_event_loop().run_until_complete(result)
                    self._loaded.append(fname)
                    print(f"[Plugins] 已加载 {fname}")
                    continue

                # 方式二：继承 PluginMixin 的插件类（类式插件）
                plugin_classes = [
                    obj for _, obj in vars(module).items()
                    if inspect.isclass(obj) and issubclass(obj, PluginMixin)
                    and obj is not PluginMixin and getattr(obj, "__module__", None) == name
                ]
                if plugin_classes:
                    loaded = 0
                    for cls in plugin_classes:
                        if not hasattr(cls, "register"):
                            print(f"[Plugins] 跳过 {cls.__name__}: 未定义 register(ctx)")
                            continue
                        instance = cls(self.ctx)
                        result = instance.register(self.ctx)
                        if inspect.isawaitable(result):
                            asyncio.get_event_loop().run_until_complete(result)
                        loaded += 1
                    self._loaded.append(fname)
                    print(f"[Plugins] 已加载 {fname} ({loaded} 个插件类)")
                else:
                    print(f"[Plugins] 跳过 {fname}: 未定义 register(ctx) 或 PluginMixin 子类")
            except Exception as e:
                print(f"[Plugins] 加载 {fname} 失败: {e}")
        print(f"[Plugins] 共加载 {len(self.ctx._message_handlers)} 个消息处理器, {len(self.ctx._event_handlers)} 个事件处理器")

    async def dispatch_message(self, bot, event) -> None:
        """按注册顺序调用所有消息处理器，单个异常不影响其他。"""
        for handler in self.ctx._message_handlers:
            try:
                ret = handler(bot, event)
                if inspect.isawaitable(ret):
                    await ret
            except Exception as e:
                print(f"[Plugins] 处理器 {getattr(handler, '__name__', handler)} 执行失败: {e}")

    async def dispatch_event(self, bot, event) -> None:
        """按注册顺序调用所有事件处理器，单个异常不影响其他。"""
        for handler in self.ctx._event_handlers:
            try:
                ret = handler(bot, event)
                if inspect.isawaitable(ret):
                    await ret
            except Exception as e:
                print(f"[Plugins] 事件处理器 {getattr(handler, '__name__', handler)} 执行失败: {e}")

    async def run_hook(self, name, *args) -> list:
        """执行某钩子的所有处理器，返回非 None 的返回值列表。"""
        return await self.ctx.run_hook(name, *args)


# 模块级默认管理器（main.py 使用）
manager = PluginManager()


def load_plugins(classifier=None, webui=None, config=None, main=None) -> tuple:
    """[兼容旧接口] 创建并加载插件，返回 (消息处理器列表, 事件处理器列表)。
    新代码请直接用 PluginManager。"""
    global manager
    manager = PluginManager(classifier=classifier, webui=webui, config=config, main=main)
    manager.load()
    return list(manager.ctx._message_handlers), list(manager.ctx._event_handlers)
