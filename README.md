# myQQbot

QQ 群消息 AI 审核机器人：基于 **NoneBot2 + OneBot v11 (NapCat)**，使用 HuggingFace 零样本分类模型（`joeddav/xlm-roberta-large-xnli`）对群消息做合规审核，自动删除违规消息、记录涉嫌消息，并提供 WebUI 审核控制台。

## 功能

- **AI 消息审核**：零样本分类，命中违规标签且得分超阈值自动删消息
- **涉嫌记录**：得分接近阈值的消息记入审核日志，人工复核
- **群白名单**：只审核指定群（不配置则审核所有群）
- **加群请求自动审批**：申请留言含关键词（验证/申请/进群/hello/hi）自动通过
- **WebUI 审核控制台**：审核日志查询/搜索/批量操作、标签管理（保存即热重载）、NapCat 连接状态、一键重启
- **插件系统**：消息处理器、全事件处理器、核心逻辑钩子（hook），支持函数式与类式（Mixin）两种写法
- **自动 GPU/CPU 选择**：多卡时挑空闲显存最多的 GPU，无 CUDA 回退 CPU

## 目录结构

```
main.py              # 框架入口：事件分发、白名单过滤、执行动作（删/记/批）、插件加载
plugin_loader.py     # 插件系统：PluginContext / PluginMixin / PluginManager
webui.py             # WebUI：FastAPI 路由 + 页面模板（Cloudflare 风格）
plugins/
  core.py            # 核心审核插件：AI 审核 + 加群审批（通过钩子接入）
plugin_example/      # 示例插件（不被自动加载，复制到 plugins/ 即启用）
  message_handler.py #   函数式示例
  mixin_demo.py      #   类式示例（PluginMixin + hook）
  README.md          #   插件 API / 钩子速查
labels.txt           # 候选标签（每行一个）
safe_labels.txt      # 安全标签（命中不拦截）
audit.db             # SQLite 审核日志（自动生成，30 天保留，置顶永久）
NapCatDocs/          # NapCat 官方文档（本地副本）
```

## 环境要求

- Python 3.12 ~ 3.13
- [uv](https://docs.astral.sh/uv/)（包管理）
- [NapCat](https://napneko.github.io/)（OneBot v11 协议端，QQ 登录）
- 可选：NVIDIA GPU + CUDA（无则自动用 CPU，速度较慢）

## 安装

```powershell
uv sync
```

首次启动会自动下载分类模型到 `model_cache/`（约 1.3GB）。

## 配置

### 1. NapCat

在 NapCat 的 OneBot v11 配置中添加一个 **WebSocket 客户端**（正向 WS）：

- 地址：`ws://127.0.0.1:28269`（与 `ONEBOT_PORT` 一致）
- 访问令牌：与 `.env` 的 `ONEBOT_ACCESS_TOKEN` 一致

### 2. `.env`

```ini
DRIVER=fastapi
HOST=0.0.0.0
ONEBOT_PORT=28269
ONEBOT_ACCESS_TOKEN=你的token
webui_token=WebUI访问token
# 群白名单：只审核列表内的群，多个群号用英文逗号分隔；留空 = 审核所有群
GROUP_WHITELIST=544514362
# HuggingFace 镜像（国内下载模型用，取消注释即可）
#HF_ENDPOINT=https://hf-mirror.com
```

| 配置项 | 说明 |
|---|---|
| `ONEBOT_PORT` | 机器人监听端口，NapCat 正向 WS 连这里，WebUI 也在这个端口 |
| `ONEBOT_ACCESS_TOKEN` | OneBot 连接鉴权 token |
| `webui_token` | WebUI 访问 token（未配置则启动时自动生成并打印） |
| `GROUP_WHITELIST` | 群白名单，留空审核所有群 |
| `HF_ENDPOINT` | HuggingFace 镜像（国内下载模型用，如 `https://hf-mirror.com`），默认直连 |

## 运行

```powershell
uv run main.py
```

启动日志会打印推理设备（GPU/CPU）、模型预热、WebUI 地址。

- WebUI：`http://127.0.0.1:28269/webui`（审核日志）
- 标签管理：`http://127.0.0.1:28269/webui/labels`
- 日志文件：`bot.log`（WebUI 一键重启时写入）

## 审核逻辑

| 结果 | 条件 | 动作 |
|---|---|---|
| 拦截 (blocked) | 得分 > 0.7 且 top 标签不在安全标签 | 删除消息 + 记入日志 |
| 涉嫌 (suspicious) | 得分 >= 0.5 且 top 标签不在安全标签 | 记入日志（不删） |
| 通过 | 其他 | 放行 |

- 短消息（< 4 字符）直接放行
- 标签在 `labels.txt`（候选）/ `safe_labels.txt`（安全）配置，WebUI 保存后**热重载**，无需重启
- 审核决策在 `plugins/core.py`，删掉该插件机器人即不再 AI 审核

## 插件系统

在 `plugins/` 放一个 `.py` 文件，重启即生效。两种写法：

### 函数式

```python
def register(ctx):
    async def on_message(bot, event):
        await ctx.bot.send_msg("group", event.group_id, "hi")
    ctx.on_message(on_message)
```

### 类式（PluginMixin）

```python
from plugin_loader import PluginMixin

class MyPlugin(PluginMixin):
    def register(self, ctx):
        ctx.on_message(self.on_message)

    async def on_message(self, bot, event):
        await self.send_group(event.group_id, "hi")
```

### 插件 API（ctx 上可用）

| 成员 | 说明 |
|---|---|
| `ctx.bot` | 机器人实例（连接后可用）：`send_msg` / `delete_msg` / `call_api` 等 |
| `ctx.classifier` | 零样本分类 pipeline |
| `ctx.classify(text)` | 便捷分类，返回 `{'label', 'score', 'block'}` |
| `ctx.webui` | webui 模块：`record_message` / `get_labels` 等 |
| `ctx.config` | `.env` 配置对象 |
| `ctx.candidate_labels` / `ctx.safe_labels` / `ctx.threshold` | 当前标签与阈值（热重载后自动更新） |
| `ctx.on_message(handler)` | 注册消息处理器 |
| `ctx.on_event(handler)` | 注册全事件处理器（消息/请求/其他） |
| `ctx.on_hook(name, handler)` | 注册钩子，拦截核心逻辑 |

### 钩子（hook）

| 钩子点 | 时机 | 返回值 |
|---|---|---|
| `before_check(bot, event, text)` | AI 审核前 | `dict` 短路审核 / `None` 不干预 |
| `after_check(bot, event, text, result)` | 审核后、拦截前 | `dict` 覆盖审核结果 |
| `before_delete(bot, event, message_id, result)` | 删除前 | `False` 取消删除 |
| `after_block(bot, event, result)` | 拦截（删除+记录）后 | 无 |
| `before_request(bot, event)` | 加群请求审批前 | `True/False` 覆盖审批决定 |

钩子按注册顺序执行，第一个返回有效值的短路后续；单个插件异常只打印不影响其他。

更多示例见 `plugin_example/`。

## WebUI

- **审核日志页**：统计卡片、搜索（文本/用户/群/标签）、状态/标签过滤、分页、批量置顶/删除、30 秒自动刷新、NapCat 连接状态徽章
- **标签管理页**：增删候选标签、勾选安全标签，保存即热重载
- **一键重启**：派生新进程重启机器人（日志写 `bot.log`）
- 鉴权：访问时输入 `webui_token`
