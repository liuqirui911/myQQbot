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
- 重写模型推理框架：插件可以接管/替换分类器（同步、异步后端都支持）：
    ctx.register_classifier(factory, name="my-backend", priority=1)  # priority>0 且最高者自动接管默认框架
    ctx.set_classifier(instance)                                     # 用已有实例直接替换
    ctx.use_classifier("my-backend")                                 # 在多个框架之间切换
    ctx.list_classifiers()                                           # 查看已注册框架（来源 / 优先级 / 是否激活）
  框架签名：fn(text, labels) -> 下列任意一种返回值
    {'labels': [...], 'scores': [...]}   HF pipeline 原生格式（取 top1）
    {'label': 'x', 'score': 0.9}         精简格式（可带 'block' 显式覆盖拦截判定）
    '标签名' / ('标签名', 0.9) / {'标签A': 0.1, '标签B': 0.9} / None（放行）
  框架为惰性构建：被插件接管后，main.py 的默认 HuggingFace 模型不会被加载（不下载、不占显存）。
- 单个插件加载失败只打印错误并跳过，不影响机器人启动。
"""
import asyncio
import importlib.util
import inspect
import os
import sys

PLUGINS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")

# 默认推理框架的注册名（main.py 提供，可被插件重写）
DEFAULT_CLASSIFIER_NAME = "default"


def _to_float(value):
    """宽松转 float，失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_async_callable(fn) -> bool:
    """判断可调用对象是否为 async（含 async __call__ 与 functools.partial 包装）。"""
    if inspect.iscoroutinefunction(fn):
        return True
    return inspect.iscoroutinefunction(getattr(fn, "__call__", None))


def normalize_classify_result(result, threshold, safe_labels) -> dict:
    """把推理框架的返回值统一成 {'label', 'score', 'block'}。

    可接受的返回值：
    - {'labels': [...], 'scores': [...]}   HF pipeline 原生格式（取 top1）
    - {'label': 'x', 'score': 0.9}         精简格式（可额外带 'block' 覆盖判定）
    - {'标签A': 0.1, '标签B': 0.9}         标签 -> 得分映射（取最大）
    - ('标签A', 0.9) / ['标签A', 0.9]      二元组
    - '标签A'                              字符串（视为得分 1.0）
    - None / 空                            放行
    block 未显式给出时 = score > threshold 且 label 不在 safe_labels。
    """
    label, score, block = None, None, None

    if isinstance(result, dict):
        if "labels" in result and "scores" in result:
            labels = result.get("labels") or []
            scores = result.get("scores") or []
            if labels:
                label = str(labels[0])
                if scores:
                    score = _to_float(scores[0])
        elif "label" in result or "score" in result:
            raw_label = result.get("label")
            label = None if raw_label is None else str(raw_label)
            score = _to_float(result.get("score"))
            if result.get("block") is not None:
                block = bool(result["block"])
        else:
            numeric = {str(k): _to_float(v) for k, v in result.items()}
            numeric = {k: v for k, v in numeric.items() if v is not None}
            if numeric:
                label = max(numeric, key=numeric.get)
                score = numeric[label]
    elif isinstance(result, str):
        label, score = result, 1.0
    elif isinstance(result, (tuple, list)) and len(result) == 2:
        label = None if result[0] is None else str(result[0])
        score = _to_float(result[1])

    if label is None:
        return {"label": None, "score": score, "block": False}
    if block is None:
        block = (score is not None) and (score > threshold) and (label not in safe_labels)
    return {"label": label, "score": score, "block": bool(block)}


class ClassifierRegistry:
    """模型推理框架注册表：默认框架 + 插件注册的框架，按优先级自动激活。

    - 注册：register(name, factory, ...)，factory 为无参可调用对象，返回分类器实例
    - 激活：priority > 0 且高于当前激活框架时自动接管默认框架；
            显式 use() / set() 会固定选择（之后不再按优先级自动切换）
    - 惰性：factory 只在首次真正分类时调用。因此插件接管后，
            默认的 HuggingFace 模型不会被加载（不下载、不占显存）。
    """

    def __init__(self, default_factory=None, default_name=DEFAULT_CLASSIFIER_NAME,
                 default_description="默认推理框架", default_provider="main"):
        self._backends: dict = {}
        self._instances: dict = {}
        self._active: str | None = None
        self._pinned = False  # 被 use()/set() 固定后不再自动切换
        self.register(default_name, default_factory, priority=0,
                      description=default_description, provider=default_provider, is_default=True)

    # ---------- 注册 / 切换 ----------
    def register(self, name, factory, priority=0, description="", provider=None, is_default=False):
        """注册（或覆盖）一个推理框架。"""
        if not isinstance(name, str) or not name:
            raise ValueError("推理框架名称必须是非空字符串")
        if factory is not None and not callable(factory):
            raise TypeError("factory 必须是无参可调用对象（返回分类器实例）")
        old = self._backends.get(name)
        if old is not None and old.get("factory") is not factory:
            self._instances.pop(name, None)  # 工厂变了，丢弃旧实例
        priority = int(priority)
        self._backends[name] = {
            "factory": factory,
            "priority": priority,
            "description": description,
            "provider": provider,
            "is_default": bool(is_default),
        }
        if self._active is None:
            self._active = name
        elif (not self._pinned and not is_default and priority > 0
              and priority > self._backends[self._active]["priority"]):
            self._active = name  # 插件框架自动接管
        return name

    def set(self, name, instance, description="", provider=None):
        """用已有实例直接替换/新增框架：立即生效并固定，不再自动切换。"""
        if not callable(instance):
            raise TypeError("instance 必须是可调用对象")
        self._backends[name] = {
            "factory": (lambda: instance),
            "priority": 999,
            "description": description,
            "provider": provider,
            "is_default": False,
        }
        self._instances[name] = instance
        self._active = name
        self._pinned = True
        return instance

    def use(self, name):
        """切换当前激活框架（立即构建，工厂报错会直接抛出）。"""
        if name not in self._backends:
            raise KeyError(f"未注册的推理框架: {name!r}（已注册: {sorted(self._backends)}）")
        self._active = name
        self._pinned = True
        return self._build(name)

    def unregister(self, name) -> bool:
        """移除框架；若移除的是当前激活框架，回退到剩余中优先级最高者。"""
        if name not in self._backends:
            return False
        self._backends.pop(name, None)
        self._instances.pop(name, None)
        if self._active == name:
            self._pinned = False
            self._active = max(
                self._backends, key=lambda n: self._backends[n]["priority"], default=None
            )
        return True

    # ---------- 取用 ----------
    def current_name(self):
        """当前激活框架的名称（不触发构建）"""
        return self._active

    def current(self):
        """当前激活框架实例（首次访问时才构建）"""
        if self._active is None:
            raise RuntimeError("没有可用的推理框架（可用 ctx.register_classifier 注册一个）")
        return self._build(self._active)

    def _build(self, name):
        if name in self._instances:
            return self._instances[name]
        info = self._backends.get(name)
        if info is None:
            raise KeyError(f"未注册的推理框架: {name!r}")
        factory = info["factory"]
        if factory is None:
            raise RuntimeError(f"推理框架 {name!r} 未提供构建工厂")
        instance = factory()
        if not callable(instance):
            raise TypeError(f"推理框架 {name!r} 的工厂未返回可调用对象: {instance!r}")
        self._instances[name] = instance
        print(f"[Plugins] 推理框架已就绪: {name} (来源: {info.get('provider') or 'unknown'})")
        return instance

    def describe(self) -> list:
        """列出所有已注册框架（不触发构建）。"""
        return [
            {
                "name": n,
                "active": n == self._active,
                "priority": i["priority"],
                "provider": i["provider"],
                "description": i["description"],
                "built": n in self._instances,
                "is_default": i["is_default"],
            }
            for n, i in self._backends.items()
        ]

    def names(self) -> list:
        """所有已注册框架的名称"""
        return list(self._backends)


class PluginContext:
    """传给插件 register(ctx) 的上下文对象。"""

    def __init__(self, classifier=None, webui=None, config=None, main=None,
                 classifier_registry=None, default_classifier_factory=None,
                 default_classifier_name=DEFAULT_CLASSIFIER_NAME,
                 default_classifier_description="默认推理框架"):
        self._bot = None
        self._webui = webui
        self._config = config
        self._main = main
        self._loading_plugin = None  # 插件加载期间由 PluginManager 写入（用于推理框架溯源）
        self._message_handlers: list = []
        self._event_handlers: list = []
        self._hooks: dict = {}

        # ---- 模型推理框架注册表（插件可重写） ----
        if classifier_registry is not None:
            self._registry = classifier_registry
        else:
            self._registry = ClassifierRegistry(
                default_factory=default_classifier_factory,
                default_name=default_classifier_name,
                default_description=default_classifier_description,
            )
        if classifier is not None:
            # 兼容旧接口：外部直接传入已构建好的分类器实例（惰性返回该实例）
            self._registry.register(
                default_classifier_name, (lambda: classifier), priority=0,
                description=default_classifier_description, provider="main", is_default=True,
            )

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
        """当前激活的推理框架（可调用对象，插件可通过 ctx.set_classifier / ctx.register_classifier 重写）。
        默认是 HF 零样本分类 pipeline，用法: classifier(text, labels) -> {'labels': [...], 'scores': [...]}
        惰性构建：首次访问时才创建（插件接管后默认模型不会被加载）。"""
        return self._registry.current()

    @property
    def classifier_name(self):
        """当前激活的推理框架名称（不触发构建）"""
        return self._registry.current_name()

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

    # ---- 分类（走当前推理框架，可被插件重写） ----
    def classify(self, text: str) -> dict:
        """用当前推理框架对文本分类（同步，勿在事件循环中阻塞调用大文本）。
        返回 {'label': str|None, 'score': float|None, 'block': bool}，
        block = score > threshold 且 label 不在安全标签中（框架可显式返回 block 覆盖）。
        若当前框架是 async 的，请改用 `await ctx.aclassify(text)`。"""
        backend = self._registry.current()
        if _is_async_callable(backend):
            raise RuntimeError(
                f"当前推理框架 {self._registry.current_name()!r} 是异步的，"
                "请改用 `await ctx.aclassify(text)`"
            )
        raw = backend(text, self.candidate_labels)
        return normalize_classify_result(raw, self.threshold, self.safe_labels)

    async def aclassify(self, text: str) -> dict:
        """异步分类：同步框架放到线程池执行，异步框架直接 await。
        返回结构与 ctx.classify 相同。"""
        backend = self._registry.current()
        if _is_async_callable(backend):
            raw = await backend(text, self.candidate_labels)
        else:
            raw = await asyncio.to_thread(backend, text, self.candidate_labels)
        return normalize_classify_result(raw, self.threshold, self.safe_labels)

    # ---- 模型推理框架：重写 / 注册 / 切换 ----
    def set_classifier(self, classifier, name=None, description="", provider=None):
        """用已有实例直接替换当前推理框架（立即生效并固定，之后不再按优先级自动切换）。

        参数 classifier 为任意可调用对象: fn(text, labels) -> 结果
        （结果格式见模块文档：HF 格式 / {'label','score'} / '标签名' / None 均支持）。
        """
        if not callable(classifier):
            raise TypeError("classifier 必须是可调用对象")
        n = name or getattr(classifier, "__name__", None) or "plugin-classifier"
        provider = provider or self._loading_plugin or "plugin"
        self._registry.set(n, classifier,
                           description=description or f"{provider} 提供的推理框架",
                           provider=provider)
        print(f"[Plugins] 推理框架已被 {provider} 重写: {n}")
        return classifier

    def register_classifier(self, factory, name=None, priority=1, description="", provider=None):
        """注册一个推理框架工厂（惰性构建：真正分类时才调用 factory()）。

        priority > 0 且高于当前激活框架时会自动接管默认框架 —— main.py 的默认
        HuggingFace 模型不会被加载（不下载、不占显存）。
        只想注册成"备用框架"（之后用 ctx.use_classifier 切换）请传 priority=0。
        """
        if not callable(factory):
            raise TypeError("factory 必须是无参可调用对象")
        n = name or getattr(factory, "__name__", None) or "plugin-backend"
        provider = provider or self._loading_plugin or "plugin"
        self._registry.register(n, factory, priority=priority,
                                description=description, provider=provider)
        return n

    def use_classifier(self, name):
        """切换到指定推理框架（立即构建，工厂报错会直接抛出）"""
        instance = self._registry.use(name)
        print(f"[Plugins] 推理框架已切换: {name}")
        return instance

    def unregister_classifier(self, name) -> bool:
        """移除某个推理框架；移除的是当前框架时回退到剩余中优先级最高者。"""
        return self._registry.unregister(name)

    def list_classifiers(self) -> list:
        """列出所有已注册的推理框架（名称 / 来源 / 优先级 / 是否激活 / 是否已构建）"""
        return self._registry.describe()

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
    def classifier_name(self):
        return self.ctx.classifier_name

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

    async def aclassify(self, text):
        return await self.ctx.aclassify(text)

    # ---- 模型推理框架（可重写） ----
    def set_classifier(self, classifier, name=None, description=""):
        """用已有实例直接替换当前推理框架"""
        return self.ctx.set_classifier(classifier, name=name, description=description)

    def register_classifier(self, factory, name=None, priority=1, description=""):
        """注册推理框架工厂（priority>0 且最高者自动接管默认框架）"""
        return self.ctx.register_classifier(factory, name=name, priority=priority, description=description)

    def use_classifier(self, name):
        """切换到指定推理框架"""
        return self.ctx.use_classifier(name)

    def list_classifiers(self):
        """列出所有已注册的推理框架"""
        return self.ctx.list_classifiers()

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
        manager = PluginManager(webui=..., config=..., main=...,
                                default_classifier_factory=build_default_classifier,
                                default_classifier_name="hf-zero-shot")
        manager.load()                           # 插件可在此重写推理框架
        manager.classifier                       # 当前推理框架（惰性构建，插件接管后默认模型不加载）
        manager.classify(text) / await manager.aclassify(text)
        manager.bot = bot                        # NapCat 连接时注入
        await manager.dispatch_message(bot, ev)  # 分发消息事件
        await manager.dispatch_event(bot, ev)    # 分发全事件
        await manager.run_hook("before_check", bot, ev, text)
    """

    def __init__(self, classifier=None, webui=None, config=None, main=None, plugins_dir=None,
                 default_classifier_factory=None, default_classifier_name=DEFAULT_CLASSIFIER_NAME,
                 default_classifier_description="默认推理框架"):
        self.ctx = PluginContext(
            classifier=classifier, webui=webui, config=config, main=main,
            default_classifier_factory=default_classifier_factory,
            default_classifier_name=default_classifier_name,
            default_classifier_description=default_classifier_description,
        )
        self._plugins_dir = plugins_dir or PLUGINS_DIR
        self._loaded: list = []

    # ---- 模型推理框架（插件可重写） ----
    @property
    def classifier(self):
        """当前激活的推理框架（首次访问时才构建）"""
        return self.ctx.classifier

    @property
    def classifier_name(self):
        """当前激活的推理框架名称"""
        return self.ctx.classifier_name

    def classify(self, text: str) -> dict:
        """同步分类（框架是 async 时请用 await manager.aclassify(text)）"""
        return self.ctx.classify(text)

    async def aclassify(self, text: str) -> dict:
        """异步分类（同步框架自动放线程池）"""
        return await self.ctx.aclassify(text)

    def set_classifier(self, classifier, name=None, description=""):
        """用已有实例直接替换当前推理框架"""
        return self.ctx.set_classifier(classifier, name=name, description=description)

    def register_classifier(self, factory, name=None, priority=1, description=""):
        """注册推理框架工厂（priority>0 且最高者自动接管默认框架）"""
        return self.ctx.register_classifier(factory, name=name, priority=priority,
                                           description=description)

    def use_classifier(self, name):
        """切换到指定推理框架"""
        return self.ctx.use_classifier(name)

    def list_classifiers(self) -> list:
        """列出所有已注册的推理框架"""
        return self.ctx.list_classifiers()

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
            self._log_classifier_summary()
            return
        for fname in files:
            path = os.path.join(self._plugins_dir, fname)
            name = f"plugins.{fname[:-3]}"
            self.ctx._loading_plugin = fname  # 供 ctx.register_classifier 溯源
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
            finally:
                self.ctx._loading_plugin = None
        print(f"[Plugins] 共加载 {len(self.ctx._message_handlers)} 个消息处理器, {len(self.ctx._event_handlers)} 个事件处理器")
        self._log_classifier_summary()

    def _log_classifier_summary(self) -> None:
        """打印当前推理框架（不触发构建）"""
        infos = self.ctx.list_classifiers()
        active = next((i for i in infos if i["active"]), None)
        if active is None:
            print("[Plugins] 推理框架: 无（插件可通过 ctx.register_classifier 注册）")
            return
        print(f"[Plugins] 推理框架: {active['name']} "
              f"(来源: {active['provider'] or 'unknown'}, 优先级 {active['priority']}, "
              f"已构建: {'是' if active['built'] else '否（首次分类时构建）'})")
        switched = [i["name"] for i in infos if not i["is_default"] and not i["active"]]
        if switched:
            print(f"[Plugins] 其他可用推理框架: {switched}（ctx.use_classifier(name) 切换）")

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


def load_plugins(classifier=None, webui=None, config=None, main=None,
                 default_classifier_factory=None,
                 default_classifier_name=DEFAULT_CLASSIFIER_NAME) -> tuple:
    """[兼容旧接口] 创建并加载插件，返回 (消息处理器列表, 事件处理器列表)。
    新代码请直接用 PluginManager。"""
    global manager
    manager = PluginManager(classifier=classifier, webui=webui, config=config, main=main,
                            default_classifier_factory=default_classifier_factory,
                            default_classifier_name=default_classifier_name)
    manager.load()
    return list(manager.ctx._message_handlers), list(manager.ctx._event_handlers)
