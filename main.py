import asyncio
import os
import socket
import sys
import time
import nonebot
from nonebot import on_message, on_request
from nonebot.adapters.onebot.v11 import MessageEvent, RequestEvent, Adapter as OneBotV11Adapter
import uvicorn
import anyio

# ================== HF 镜像（必须在 import transformers 之前设置） ==================
# huggingface_hub 在 import 时读取 HF_ENDPOINT，所以要先写入 os.environ
# 国内下载模型可在 .env 配置: HF_ENDPOINT=https://hf-mirror.com
def _load_hf_endpoint() -> str | None:
    """从 .env 读取 HF_ENDPOINT（不存在则回退到系统环境变量）"""
    try:
        with open(os.path.join(os.getcwd(), ".env"), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("HF_ENDPOINT="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except FileNotFoundError:
        pass
    return None

_hf_endpoint = _load_hf_endpoint() or os.environ.get("HF_ENDPOINT")
if _hf_endpoint:
    os.environ["HF_ENDPOINT"] = _hf_endpoint
    print(f"[AI] HF 镜像已启用: {_hf_endpoint}")

from transformers import pipeline
import torch

import webui
import plugin_loader

# ================== 零样本分类模型（自动 GPU/CPU 选择） ==================
def _pick_device() -> tuple[int, str]:
    """优先用空闲显存最多的 GPU；无 CUDA 时回退 CPU(-1)。
    多卡下挑最空闲的那张，避免与 llama.cpp / ai-draw 抢满的卡。
    """
    if not torch.cuda.is_available():
        return -1, "cpu"
    best_i, best_free = 0, -1
    for i in range(torch.cuda.device_count()):
        free, _ = torch.cuda.mem_get_info(i)
        if free > best_free:
            best_free, best_i = free, i
    desc = f"cuda:{best_i} ({torch.cuda.get_device_name(best_i)}, 空闲 {best_free/2**30:.1f}GiB)"
    return best_i, desc

_device, _device_desc = _pick_device()
print(f"[AI] 推理设备: {_device_desc}")

classifier = pipeline(
    "zero-shot-classification",
    model="joeddav/xlm-roberta-large-xnli",
    cache_dir="model_cache",
    device=_device,
    hypothesis_template="这条消息是{}的。",
)

# ================== 从 labels.txt 加载标签（支持 WebUI 热重载） ==================
current_dir = os.path.abspath(os.getcwd())
labels_path = os.path.join(current_dir, "labels.txt")
safe_labels_path = os.path.join(current_dir, "safe_labels.txt")

DEFAULT_CANDIDATE_LABELS = [
    "严重辱骂或人身攻击",
    "贬低或诅咒服务器",
    "煽动玩家集体换服或退服",
    "宣传其他组织或诱导玩家加群",
    "正常讨论",
    "普通吐槽或抱怨",
    "日常闲聊",
    "游戏问题讨论",
]
DEFAULT_SAFE_LABELS = {"正常讨论", "普通吐槽或抱怨", "日常闲聊", "游戏问题讨论"}

def _load_candidate_labels() -> list:
    try:
        with open(labels_path, "r", encoding="utf-8") as f:
            labels = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        labels = []
    if not labels:
        print("[Warning] labels.txt not found or empty, using default labels.")
        return list(DEFAULT_CANDIDATE_LABELS)
    return labels

def _load_safe_labels() -> set:
    try:
        with open(safe_labels_path, "r", encoding="utf-8") as f:
            safe = {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set(DEFAULT_SAFE_LABELS)
    return safe

candidate_labels = _load_candidate_labels()

# 审核阈值（得分超过此值且不属于安全标签则拦截）
threshold = 0.7

# 安全标签：命中这些标签的消息不拦截（safe_labels.txt 不存在时用默认值）
safe_labels = _load_safe_labels()

def reload_labels() -> None:
    """WebUI 保存标签后调用：重新读取 labels.txt / safe_labels.txt，立即生效"""
    global candidate_labels, safe_labels
    candidate_labels = _load_candidate_labels()
    safe_labels = _load_safe_labels()
    print(f"[Labels] 热重载完成: {len(candidate_labels)} 个候选标签, {len(safe_labels)} 个安全标签")

# 注册标签热重载回调（WebUI 保存标签时调用）
webui.register_labels_reload(reload_labels)

# ================== 同步分类函数 ==================
def classify_sync(text: str) -> dict:
    result = classifier(text, candidate_labels)
    # result: {'sequence': ..., 'labels': [...], 'scores': [...]}
    top_label = result["labels"][0]
    top_score = result["scores"][0]

    block = (top_score > threshold) and (top_label not in safe_labels)
    return {
        "block": block,
        "label": top_label,
        "score": top_score,
    }

# ================== NoneBot 初始化 ==================
nonebot.init(_env_file=".env")
driver = nonebot.get_driver()

class NapCatAdapter(OneBotV11Adapter):
    """追踪 NapCat 连接状态，供 WebUI 展示"""
    def bot_connect(self, bot):
        super().bot_connect(bot)
        webui.set_napcat_status(True, bot.self_id)
        plugin_manager.bot = bot  # 暴露给插件：ctx.bot

    def bot_disconnect(self, bot):
        super().bot_disconnect(bot)
        webui.set_napcat_status(False, bot.self_id)

driver.register_adapter(NapCatAdapter)

# 挂载 WebUI 路由
nonebot.get_app().include_router(webui.router)

# ================== 插件系统 ==================
# 插件管理器：加载 plugins/ 目录下的插件，负责事件分发与钩子执行
# 传入当前运行模块（__main__）供 ctx.classify / 标签 / 阈值读取实时值（热重载后自动更新）
plugin_manager = plugin_loader.PluginManager(
    classifier=classifier, webui=webui, config=driver.config, main=sys.modules[__name__]
)
plugin_manager.load()

# 将 .env 中的 webui_token 同步到环境变量（NoneBot2 不会自动写入 os.environ）
_webui_token = str(getattr(driver.config, "webui_token", "") or "").strip()
if _webui_token:
    os.environ["webui_token"] = _webui_token

# ================== 群白名单 ==================
# 只审核白名单内的群；留空 = 审核所有群
# 在 .env 中配置: GROUP_WHITELIST=544514362,123456789
_group_whitelist_raw = str(getattr(driver.config, "group_whitelist", "") or "")
group_whitelist = {g.strip() for g in _group_whitelist_raw.replace("，", ",").split(",") if g.strip()}
if group_whitelist:
    print(f"[Config] 群白名单已启用: {sorted(group_whitelist)}")
else:
    print("[Config] 未配置群白名单，将审核所有群")

# ================== 预热 ==================
@driver.on_startup
async def warmup():
    await anyio.to_thread.run_sync(classify_sync, "预热消息")
    print("[AI] Model warmed up and ready.")

# ================== WebUI 初始化 + 定时清理 ==================
@driver.on_startup
async def init_webui():
    webui.init_db()
    deleted = webui.cleanup_old()
    print(f"[WebUI] 数据库就绪 ({webui.DB_PATH})，清理过期记录 {deleted} 条")
    port = getattr(driver.config, "onebot_port", 28269)
    print(f"[WebUI] 访问地址: http://127.0.0.1:{port}/webui")

async def _cleanup_loop():
    while True:
        try:
            await anyio.sleep(3600)
            deleted = webui.cleanup_old()
            if deleted:
                print(f"[WebUI] 定时清理过期记录 {deleted} 条")
        except Exception as e:
            print(f"[WebUI] Cleanup error: {e}")

_cleanup_task: asyncio.Task | None = None

@driver.on_startup
async def start_cleanup():
    global _cleanup_task
    _cleanup_task = asyncio.create_task(_cleanup_loop())

# ================== 事件处理 ==================
async def onEvent(bot, event):
    user_id = getattr(event, "user_id", "Unknown")
    if isinstance(event, MessageEvent):
        message_text = event.get_plaintext()
        # 群白名单过滤：配置了白名单时，只审核白名单内的群
        group_id = getattr(event, "group_id", None)
        if group_whitelist and (group_id is None or str(group_id) not in group_whitelist):
            print(f"[Skip] 群 {group_id or '私聊'} 不在白名单内，跳过审核")
            return
        # 钩子：AI 审核（由 core 插件实现；返回 dict 短路，否则放行）
        result = None
        for r in await plugin_manager.run_hook("before_check", bot, event, message_text):
            if isinstance(r, dict):
                result = {"block": bool(r.get("block", False)), "label": r.get("label"), "score": r.get("score")}
                break
        if result is None:
            result = {"block": False, "label": None, "score": None}
        if result["block"]:
            print(f"[Blocked] Message from {user_id} failed compliance check. Attempting to delete...")
            # 钩子：删除前（插件返回 False 可取消删除）
            cancel_delete = False
            for r in await plugin_manager.run_hook("before_delete", bot, event, event.message_id, result):
                if r is False:
                    cancel_delete = True
                    break
            if cancel_delete:
                print(f"[Blocked] 删除被插件钩子取消，消息 {event.message_id} 保留")
            else:
                try:
                    await bot.delete_msg(message_id=event.message_id)
                    print(f"Successfully deleted message {event.message_id}")
                except Exception as e:
                    print(f"Failed to delete message {event.message_id}: {e}")
            webui.record_message(
                user_id, getattr(event, "group_id", None), event.message_id,
                message_text, result["label"], result["score"], "blocked",
            )
            # 钩子：拦截动作（删除 + 记录）完成后
            await plugin_manager.run_hook("after_block", bot, event, result)
            return
        # 涉嫌消息：top 标签是违规标签（非安全标签）且得分 >= threshold - 0.2
        score = result["score"]
        if (score is not None and score >= threshold - 0.2
                and result["label"] is not None and result["label"] not in safe_labels):
            webui.record_message(
                user_id, getattr(event, "group_id", None), event.message_id,
                message_text, result["label"], score, "suspicious",
            )
        msg = event.get_message()
        image_urls = [seg.data.get("url") for seg in msg if seg.type == "image"]
        print(f"[消息事件] 来自 {user_id}: {message_text} | 图片: {image_urls}")
        # 插件消息处理器（按注册顺序依次调用，单个插件异常不影响其他插件）
        await plugin_manager.dispatch_message(bot, event)
    elif isinstance(event, RequestEvent):
        print(f"[请求事件-原始数据] {event.model_dump_json()}")
        req_type = getattr(event, "request_type", "unknown_request")
        print(f"[请求事件] 类型: {req_type} | 来自: {user_id}")
    group_id = getattr(event, "group_id", None)
    print(f"Context -> Group: {group_id}, Bot: {getattr(event, 'bot_id', None)}")
    # 插件全事件处理器（消息 / 请求 / 其他，按注册顺序依次调用，单个插件异常不影响其他）
    await plugin_manager.dispatch_event(bot, event)

msg_matcher = on_message()
@msg_matcher.handle()
async def handle_msg(bot, event: MessageEvent):
    await onEvent(bot, event)

req_matcher = on_request()
@req_matcher.handle()
async def handle_req(bot, event: RequestEvent):
    req_type = getattr(event, "request_type", None)
    sub_type = getattr(event, "sub_type", None)
    if (req_type == "group_add") or (req_type == "group" and sub_type == "add"):
        # 钩子：加群请求审批（由 core 插件实现；返回 True 则自动通过）
        for r in await plugin_manager.run_hook("before_request", bot, event):
            if r is True:
                try:
                    await bot.call_api("set_group_add_request",
                                       group_id=event.group_id,
                                       user_id=event.user_id,
                                       approve=True)
                    print(f"[Request] 自动通过加群请求 user={event.user_id} group={event.group_id}")
                except Exception as e:
                    print(f"Failed to call set_group_add_request: {e}")
                break
    await onEvent(bot, event)

def _wait_port_free(port: int, timeout: float = 30.0) -> None:
    """重启场景：等待端口释放（旧进程退出后端口才可用），避免 bind 冲突"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            s.close()
            return
        except OSError:
            s.close()
            time.sleep(0.5)
    print(f"[Warning] 端口 {port} 在 {timeout}s 内未释放，仍尝试启动")

if __name__ == "__main__":
    port = int(getattr(driver.config, "onebot_port", 28269))
    _wait_port_free(port)
    uvicorn.run(nonebot.get_app(), host="0.0.0.0", port=port)