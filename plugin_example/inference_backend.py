"""示例插件（函数式）：重写模型推理框架。

注意：本文件位于 plugin_example/，不会被自动加载。
要启用请复制到 plugins/ 目录（重启机器人后生效）。

启用后：
- 推理框架被替换成下面的关键词规则后端（priority=10 > 默认框架的 0，自动接管）
- main.py 的默认 HuggingFace 模型**不会被加载**（不下载、不占显存）
- 群里发 `!框架` 查看当前框架列表，发 `!切换异步框架` / `!切换默认框架` 运行时可切换

推理框架约定：任意可调用对象 fn(text, labels)，返回值支持
    {'labels': [...], 'scores': [...]}   HF pipeline 原生格式（取 top1）
    {'label': 'x', 'score': 0.9}         精简格式（可带 'block' 显式覆盖拦截判定）
    '标签名' / ('标签名', 0.9) / {'标签A': 0.1, '标签B': 0.9} / None（放行）
同步、异步（async def）都支持：
- 同步框架：ctx.classify(text) / await ctx.aclassify(text)（后者自动放线程池）
- 异步框架：只能用 await ctx.aclassify(text)
"""
import asyncio

# (关键词元组, 命中后返回的标签, 得分)
KEYWORD_RULES = [
    (("外挂", "辅助", "脚本"), "宣传违规工具", 0.98),
    (("私服", "换服", "退服"), "煽动玩家集体换服或退服", 0.95),
    (("代练", "工作室", "加群"), "宣传其他组织或诱导玩家加群", 0.93),
]


def _classify_by_rules(text, labels):
    """命中关键词 -> 高得分标签；未命中 -> None（放行）"""
    for keywords, label, score in KEYWORD_RULES:
        if any(kw in text for kw in keywords):
            return {"label": label, "score": score}
    return None


def keyword_backend():
    """同步推理框架：零依赖、零显存的关键词规则。"""
    return _classify_by_rules


def async_keyword_backend():
    """异步推理框架：与上面同一套规则，演示 async 后端。

    真实场景可把 sleep 换成 await 外部服务，例如：
        resp = await client.post("http://127.0.0.1:8080/classify", json={"text": text})
    """
    async def classify(text, labels):
        await asyncio.sleep(0)  # 模拟异步 IO
        return _classify_by_rules(text, labels)

    return classify


def register(ctx):
    # 主框架：priority=10 自动接管默认框架（默认模型因此不会被加载）
    ctx.register_classifier(
        keyword_backend,
        name="keyword-rules",
        priority=10,
        description="关键词规则推理框架（示例，零依赖零显存）",
    )
    # 备用框架：priority=0 只注册不接管，之后可用 ctx.use_classifier("keyword-rules-async") 切换
    ctx.register_classifier(
        async_keyword_backend,
        name="keyword-rules-async",
        priority=0,
        description="关键词规则（异步版，演示 async 推理框架）",
    )

    async def on_message(bot, event):
        text = (event.get_plaintext() if hasattr(event, "get_plaintext") else str(event)).strip()
        group_id = getattr(event, "group_id", None)
        if group_id is None:
            return
        if text == "!框架":
            lines = []
            for f in ctx.list_classifiers():
                mark = "->" if f["active"] else "  "
                lines.append(f"{mark} {f['name']} | 优先级 {f['priority']} | 来源 {f['provider']} | "
                             f"已构建 {'是' if f['built'] else '否'}")
            await bot.send_msg("group", group_id, "推理框架:\n" + "\n".join(lines))
        elif text == "!切换异步框架":
            ctx.use_classifier("keyword-rules-async")
            await bot.send_msg("group", group_id, "已切换到异步推理框架")
        elif text == "!切换默认框架":
            # 默认 HF 模型首次加载很慢，放线程池避免卡住事件循环
            await asyncio.to_thread(ctx.use_classifier, "hf-zero-shot")
            await bot.send_msg("group", group_id, "已切换回默认 HF 模型")

    ctx.on_message(on_message)
