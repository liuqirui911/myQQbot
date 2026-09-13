"""
WebUI: 消息审核日志
- SQLite 存储 (audit.db)
- Token 鉴权 (.env 中配置 webui_token，未配置则启动时自动生成)
- 30 天保留期，置顶 (pinned) 的消息永久保留
"""
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

DB_PATH = os.path.join(os.path.abspath(os.getcwd()), "audit.db")
RETENTION_DAYS = 30

router = APIRouter()

# ================== 数据库 ==================

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                user_id TEXT,
                group_id TEXT,
                message_id TEXT,
                text TEXT NOT NULL,
                label TEXT,
                score REAL,
                status TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_status ON audit_log(status)")

def record_message(user_id, group_id, message_id, text, label, score, status) -> None:
    """记录一条拦截/涉嫌消息"""
    ts = datetime.now().isoformat(timespec="seconds")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, user_id, group_id, message_id, text, label, score, status, pinned) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (ts, str(user_id), str(group_id) if group_id else None,
             str(message_id), text, label, score, status),
        )

def cleanup_old() -> int:
    """删除超过保留期且未置顶的记录，返回删除条数"""
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    with _conn() as conn:
        cur = conn.execute("DELETE FROM audit_log WHERE pinned = 0 AND ts < ?", (cutoff,))
        return cur.rowcount

# ================== 标签管理（热重载） ==================

LABELS_PATH = os.path.join(os.path.abspath(os.getcwd()), "labels.txt")
SAFE_LABELS_PATH = os.path.join(os.path.abspath(os.getcwd()), "safe_labels.txt")

# 由 main.py 在启动时注册：保存标签后重新加载，立即生效
_labels_reload_cb: Optional[Callable[[], None]] = None

def register_labels_reload(cb: Callable[[], None]) -> None:
    global _labels_reload_cb
    _labels_reload_cb = cb

def _read_lines(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []

def _write_lines(path: str, lines: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def get_labels() -> dict:
    labels = _read_lines(LABELS_PATH)
    safe = set(_read_lines(SAFE_LABELS_PATH))
    return {"labels": labels, "safe_labels": sorted(safe)}

def save_labels(labels: list, safe_labels: list) -> dict:
    """保存标签到 labels.txt / safe_labels.txt，并触发热重载"""
    labels = [l.strip() for l in labels if l and l.strip()]
    safe_labels = [l.strip() for l in safe_labels if l and l.strip()]
    if not labels:
        raise HTTPException(status_code=400, detail="标签列表不能为空")
    # 安全标签必须是候选标签的子集
    unknown = [l for l in safe_labels if l not in labels]
    if unknown:
        raise HTTPException(status_code=400, detail=f"安全标签不在候选标签中: {', '.join(unknown)}")
    _write_lines(LABELS_PATH, labels)
    _write_lines(SAFE_LABELS_PATH, safe_labels)
    if _labels_reload_cb:
        _labels_reload_cb()
    return {"ok": True, "count": len(labels), "safe_count": len(safe_labels)}

# ================== NapCat 连接状态 ==================

# 由 main.py 在 Adapter 连接/断开时更新
_napcat_status = {
    "connected": False,
    "bot_id": None,
    "last_change": None,
}

def set_napcat_status(connected: bool, bot_id=None) -> None:
    """由 main.py 的 Adapter 钩子调用，更新 NapCat 连接状态"""
    _napcat_status["connected"] = connected
    _napcat_status["bot_id"] = str(bot_id) if bot_id is not None else None
    _napcat_status["last_change"] = datetime.now().isoformat(timespec="seconds")
    print(f"[NapCat] 连接状态: {'已连接' if connected else '已断开'} (bot_id={_napcat_status['bot_id']})")

def get_napcat_status() -> dict:
    return dict(_napcat_status)

# ================== 鉴权 ==================

def get_token() -> str:
    token = os.environ.get("webui_token", "").strip()
    if not token:
        token = secrets.token_urlsafe(16)
        os.environ["webui_token"] = token
        print(f"[WebUI] 未配置 webui_token，已生成临时 token: {token}")
    return token

def check_auth(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    else:
        provided = request.query_params.get("token", "")
    if not provided or not secrets.compare_digest(provided, get_token()):
        raise HTTPException(status_code=401, detail="invalid token")

# ================== API ==================

@router.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

@router.get("/webui", response_class=HTMLResponse)
async def webui_page():
    return HTML_TEMPLATE

@router.get("/api/status")
async def api_status(_=Depends(check_auth)):
    """NapCat 连接状态 + 记录统计"""
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        blocked = conn.execute("SELECT COUNT(*) FROM audit_log WHERE status='blocked'").fetchone()[0]
        suspicious = conn.execute("SELECT COUNT(*) FROM audit_log WHERE status='suspicious'").fetchone()[0]
    return {
        "napcat": get_napcat_status(),
        "stats": {"total": total, "blocked": blocked, "suspicious": suspicious},
    }

@router.post("/api/restart")
async def api_restart(_=Depends(check_auth)):
    """重启机器人：派生脱离进程重新运行 main.py（日志写入 bot.log），当前进程延迟退出释放端口"""
    if getattr(api_restart, "_restarting", False):
        raise HTTPException(status_code=409, detail="restart already in progress")
    api_restart._restarting = True
    cwd = os.path.abspath(os.getcwd())
    log_path = os.path.join(cwd, "bot.log")
    log_file = open(log_path, "ab")
    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        creationflags = 0x00000008 | 0x00000200
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(cwd, "main.py")],
            cwd=cwd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
    finally:
        log_file.close()
    print("[WebUI] 已派生重启进程，当前进程将在 1.5s 后退出")

    def _shutdown():
        time.sleep(1.5)
        os._exit(0)

    threading.Thread(target=_shutdown, daemon=True).start()
    return {"ok": True, "message": "restarting"}

@router.get("/api/messages")
async def list_messages(
    status: Optional[str] = Query(None, pattern="^(blocked|suspicious)$"),
    pinned: Optional[bool] = None,
    label: Optional[str] = Query(None, description="按标签精确过滤"),
    q: Optional[str] = Query(None, description="搜索：匹配消息文本/用户/群/标签"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _=Depends(check_auth),
):
    where = " WHERE 1=1"
    args = []
    if status:
        where += " AND status = ?"
        args.append(status)
    if pinned is not None:
        where += " AND pinned = ?"
        args.append(1 if pinned else 0)
    if label:
        where += " AND label = ?"
        args.append(label)
    if q:
        like = f"%{q}%"
        where += " AND (text LIKE ? OR user_id LIKE ? OR group_id LIKE ? OR label LIKE ?)"
        args += [like, like, like, like]
    with _conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM audit_log{where}", args).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            args + [limit, offset],
        ).fetchall()]
    return {"total": total, "items": rows}

@router.post("/api/messages/batch")
async def batch_messages(payload: dict, _=Depends(check_auth)):
    """批量操作：{"action": "delete"|"pin"|"unpin", "ids": [1,2,3]}"""
    action = payload.get("action")
    ids = payload.get("ids", [])
    if action not in ("delete", "pin", "unpin"):
        raise HTTPException(status_code=400, detail="action 必须是 delete / pin / unpin")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    ids = [int(i) for i in ids]
    placeholders = ",".join("?" * len(ids))
    with _conn() as conn:
        if action == "delete":
            cur = conn.execute(f"DELETE FROM audit_log WHERE id IN ({placeholders})", ids)
        elif action == "pin":
            cur = conn.execute(f"UPDATE audit_log SET pinned = 1 WHERE id IN ({placeholders})", ids)
        else:
            cur = conn.execute(f"UPDATE audit_log SET pinned = 0 WHERE id IN ({placeholders})", ids)
    return {"ok": True, "affected": cur.rowcount}

@router.post("/api/messages/{mid}/pin")
async def toggle_pin(mid: int, _=Depends(check_auth)):
    """切换置顶状态（置顶 = 永久保留）"""
    with _conn() as conn:
        cur = conn.execute("UPDATE audit_log SET pinned = 1 - pinned WHERE id = ?", (mid,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}

@router.delete("/api/messages/{mid}")
async def delete_message(mid: int, _=Depends(check_auth)):
    with _conn() as conn:
        cur = conn.execute("DELETE FROM audit_log WHERE id = ?", (mid,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}

@router.get("/api/labels")
async def api_get_labels(_=Depends(check_auth)):
    return get_labels()

@router.post("/api/labels")
async def api_save_labels(payload: dict, _=Depends(check_auth)):
    labels = payload.get("labels", [])
    safe_labels = payload.get("safe_labels", [])
    if not isinstance(labels, list) or not isinstance(safe_labels, list):
        raise HTTPException(status_code=400, detail="labels / safe_labels 必须是数组")
    return save_labels(labels, safe_labels)

@router.get("/webui/labels", response_class=HTMLResponse)
async def labels_page():
    return LABELS_HTML_TEMPLATE

# ================== 页面 ==================

# Cloudflare Dashboard 设计语言（参考 cloudflare-dash-style.md）
CF_CSS = """
:root{
  --cf-blue-4:#0051c3;--cf-blue-2:#003681;--cf-blue-5:#086fff;--cf-blue-9:#ecf4ff;
  --cf-green-5:#228b49;--cf-green-9:#e3f8eb;
  --cf-orange-5:#c05d08;--cf-orange-9:#fff4e6;
  --cf-red-5:#e81403;--cf-red-9:#ffefee;
  --cf-gray-1:#313131;--cf-gray-4:#595959;--cf-gray-5:#797979;--cf-gray-8:#d9d9d9;--cf-gray-9:#f2f2f2;
  --header-height:58px;
  --card-shadow:0 1px 2px rgba(0,0,0,.08),0 12px 32px -8px rgba(0,0,0,.28);
}
*{box-sizing:border-box;margin:0;padding:0}
html{font-size:16px;-webkit-text-size-adjust:none}
body{background:#fff;color:var(--cf-gray-1);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.5;-webkit-font-smoothing:antialiased}
a{color:var(--cf-blue-4);text-decoration:underline;text-underline-offset:4px;transition:color 150ms ease}
a:hover{color:var(--cf-blue-2)}
.hidden{display:none!important}
.muted{color:var(--cf-gray-5)}
.topbar{height:var(--header-height);display:flex;align-items:center;justify-content:space-between;padding:0 24px;border-bottom:1px solid var(--cf-gray-8);background:#fff;position:sticky;top:0;z-index:10;gap:12px;flex-wrap:wrap}
.topbar .brand{font-size:16px;font-weight:600;white-space:nowrap}
.topbar nav{display:flex;align-items:center;gap:20px}
.topbar nav a{font-size:14px;text-decoration:none;color:var(--cf-gray-4);transition:color 150ms ease}
.topbar nav a:hover{color:var(--cf-blue-2)}
.topbar nav a.active{color:var(--cf-gray-1);font-weight:600}
.page{max-width:1200px;margin:0 auto;padding:32px 24px}
.page-title{font-size:32px;font-weight:400;line-height:1.25;margin-bottom:4px}
.page-sub{color:var(--cf-gray-5);font-size:14px;margin-bottom:24px}
.card{background:#fff;border:1px solid rgba(0,0,0,.06);border-radius:12px;box-shadow:var(--card-shadow);padding:24px;margin-bottom:24px}
.card h2{font-size:20px;font-weight:600;margin-bottom:6px}
.card .hint{color:var(--cf-gray-5);font-size:13px;margin-bottom:16px;line-height:1.6}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:8px 16px;border-radius:8px;border:1px solid var(--cf-blue-4);background:var(--cf-blue-4);color:#fff;font-size:14px;font-weight:500;cursor:pointer;transition:background 150ms ease,border-color 150ms ease;text-decoration:none;white-space:nowrap}
.btn:hover{background:var(--cf-blue-2);border-color:var(--cf-blue-2);color:#fff}
.btn.ghost{background:#fff;border-color:var(--cf-gray-8);color:var(--cf-gray-1)}
.btn.ghost:hover{background:var(--cf-gray-9);border-color:var(--cf-gray-5);color:var(--cf-gray-1)}
.btn.sm{padding:5px 12px;font-size:13px;border-radius:6px}
input[type=text],input[type=password],select{padding:8px 12px;border:1px solid var(--cf-gray-8);border-radius:8px;font-size:14px;color:var(--cf-gray-1);background:#fff;font-family:inherit}
input::placeholder{color:var(--cf-gray-5)}
input:focus,select:focus{outline:none;border-color:var(--cf-blue-5);box-shadow:0 0 0 3px rgba(8,111,255,.15)}
table{width:100%;border-collapse:collapse;border-spacing:0;font-size:14px}
thead th{background:var(--cf-gray-9);font-weight:600;text-align:left;padding:10px 14px;border-bottom:1px solid var(--cf-gray-8);white-space:nowrap}
tbody td{padding:10px 14px;border-bottom:1px solid #e6e6e6;vertical-align:top}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:var(--cf-blue-9)}
.badge{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;font-weight:500;white-space:nowrap}
.badge.blocked{background:var(--cf-red-9);color:var(--cf-red-5)}
.badge.suspicious{background:var(--cf-orange-9);color:var(--cf-orange-5)}
.login{display:flex;align-items:center;justify-content:center;min-height:100vh;background:var(--cf-gray-9)}
.login-box{background:#fff;border:1px solid rgba(0,0,0,.06);border-radius:12px;box-shadow:var(--card-shadow);padding:40px;width:360px;text-align:center}
.login-box h1{font-size:24px;font-weight:600;margin-bottom:8px}
.login-box p{color:var(--cf-gray-5);font-size:14px;margin-bottom:20px}
.login-box input{width:100%;margin-bottom:12px}
.login-box .btn{width:100%}
.err{color:var(--cf-red-5);font-size:13px;margin-top:10px;min-height:16px}
/* ===== 滚动条美化（Cloudflare 浅色风格） ===== */
*{scrollbar-width:thin;scrollbar-color:var(--cf-gray-8) transparent}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--cf-gray-8);border-radius:8px;border:2px solid #fff}
::-webkit-scrollbar-thumb:hover{background:var(--cf-gray-5)}
::-webkit-scrollbar-corner{background:transparent}
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QQ Bot 审核控制台</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --cf-blue-4:#0051c3;--cf-blue-2:#003681;--cf-blue-5:#086fff;--cf-blue-9:#ecf4ff;
  --cf-green-5:#228b49;--cf-green-9:#e3f8eb;
  --cf-orange-5:#c05d08;--cf-orange-9:#fff4e6;
  --cf-red-5:#e81403;--cf-red-9:#ffefee;
  --cf-gray-1:#313131;--cf-gray-4:#595959;--cf-gray-5:#797979;--cf-gray-8:#d9d9d9;--cf-gray-9:#f2f2f2;
  --header-height:58px;
  --card-shadow:0 1px 2px rgba(0,0,0,.08),0 12px 32px -8px rgba(0,0,0,.28);
}
*{box-sizing:border-box;margin:0;padding:0}
html{font-size:16px;-webkit-text-size-adjust:none}
body{background:#fff;color:var(--cf-gray-1);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.5;-webkit-font-smoothing:antialiased}
a{color:var(--cf-blue-4);text-decoration:underline;text-underline-offset:4px;transition:color 150ms ease}
a:hover{color:var(--cf-blue-2)}
.hidden{display:none!important}
.muted{color:var(--cf-gray-5)}
.topbar{height:var(--header-height);display:flex;align-items:center;justify-content:space-between;padding:0 24px;border-bottom:1px solid var(--cf-gray-8);background:#fff;position:sticky;top:0;z-index:10;gap:12px;flex-wrap:wrap}
.topbar .brand{font-size:16px;font-weight:600;white-space:nowrap}
.topbar nav{display:flex;align-items:center;gap:20px}
.topbar nav a{font-size:14px;text-decoration:none;color:var(--cf-gray-4);transition:color 150ms ease}
.topbar nav a:hover{color:var(--cf-blue-2)}
.topbar nav a.active{color:var(--cf-gray-1);font-weight:600}
.page{max-width:1200px;margin:0 auto;padding:32px 24px}
.page-title{font-size:32px;font-weight:400;line-height:1.25;margin-bottom:4px}
.page-sub{color:var(--cf-gray-5);font-size:14px;margin-bottom:24px}
.card{background:#fff;border:1px solid rgba(0,0,0,.06);border-radius:12px;box-shadow:var(--card-shadow);padding:24px;margin-bottom:24px}
.card h2{font-size:20px;font-weight:600;margin-bottom:6px}
.card .hint{color:var(--cf-gray-5);font-size:13px;margin-bottom:16px;line-height:1.6}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:8px 16px;border-radius:8px;border:1px solid var(--cf-blue-4);background:var(--cf-blue-4);color:#fff;font-size:14px;font-weight:500;cursor:pointer;transition:background 150ms ease,border-color 150ms ease;text-decoration:none;white-space:nowrap}
.btn:hover{background:var(--cf-blue-2);border-color:var(--cf-blue-2);color:#fff}
.btn.ghost{background:#fff;border-color:var(--cf-gray-8);color:var(--cf-gray-1)}
.btn.ghost:hover{background:var(--cf-gray-9);border-color:var(--cf-gray-5);color:var(--cf-gray-1)}
.btn.sm{padding:5px 12px;font-size:13px;border-radius:6px}
input[type=text],input[type=password],select{padding:8px 12px;border:1px solid var(--cf-gray-8);border-radius:8px;font-size:14px;color:var(--cf-gray-1);background:#fff;font-family:inherit}
input::placeholder{color:var(--cf-gray-5)}
input:focus,select:focus{outline:none;border-color:var(--cf-blue-5);box-shadow:0 0 0 3px rgba(8,111,255,.15)}
table{width:100%;border-collapse:collapse;border-spacing:0;font-size:14px}
thead th{background:var(--cf-gray-9);font-weight:600;text-align:left;padding:10px 14px;border-bottom:1px solid var(--cf-gray-8);white-space:nowrap}
tbody td{padding:10px 14px;border-bottom:1px solid #e6e6e6;vertical-align:top;background:transparent}
tbody tr:last-child td{border-bottom:none}
/* ★ 修复：把悬停/选中背景放到 tr 上，整行一次性上色 */
tbody tr:hover{background:var(--cf-blue-9)}
tbody tr.selected{background:var(--cf-blue-9)}
.badge{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;font-weight:500;white-space:nowrap}
.badge.blocked{background:var(--cf-red-9);color:var(--cf-red-5)}
.badge.suspicious{background:var(--cf-orange-9);color:var(--cf-orange-5)}
.login{display:flex;align-items:center;justify-content:center;min-height:100vh;background:var(--cf-gray-9)}
.login-box{background:#fff;border:1px solid rgba(0,0,0,.06);border-radius:12px;box-shadow:var(--card-shadow);padding:40px;width:360px;text-align:center}
.login-box h1{font-size:24px;font-weight:600;margin-bottom:8px}
.login-box p{color:var(--cf-gray-5);font-size:14px;margin-bottom:20px}
.login-box input{width:100%;margin-bottom:12px}
.login-box .btn{width:100%}
.err{color:var(--cf-red-5);font-size:13px;margin-top:10px;min-height:16px}

/* ===== 审核日志页 ===== */
.status-pill{display:inline-flex;align-items:center;gap:7px;padding:5px 12px;border-radius:20px;font-size:13px;font-weight:500;border:1px solid var(--cf-gray-8);background:#fff}
.status-pill .dot{width:8px;height:8px;border-radius:50%;background:var(--cf-gray-5)}
.status-pill.on{border-color:var(--cf-green-5);color:var(--cf-green-5);background:var(--cf-green-9)}
.status-pill.on .dot{background:var(--cf-green-5);box-shadow:0 0 0 3px rgba(34,139,73,.18)}
.status-pill.off{border-color:var(--cf-red-5);color:var(--cf-red-5);background:var(--cf-red-9)}
.status-pill.off .dot{background:var(--cf-red-5)}
.stats{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.stat-card{flex:1;min-width:140px;background:#fff;border:1px solid rgba(0,0,0,.06);border-radius:12px;box-shadow:var(--card-shadow);padding:16px 20px}
.stat-card .num{font-size:28px;font-weight:600;line-height:1.2}
.stat-card .lbl{font-size:13px;color:var(--cf-gray-5);margin-top:2px}
.stat-card.blocked .num{color:var(--cf-red-5)}
.stat-card.suspicious .num{color:var(--cf-orange-5)}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.toolbar .search{flex:1;min-width:200px;position:relative}
.toolbar .search input{width:100%;padding-left:34px}
.toolbar .search .icon{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--cf-gray-5);font-size:14px;pointer-events:none}
/* ===== 自定义下拉 ===== */
.dropdown{position:relative}
.dd-btn{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid var(--cf-gray-8);border-radius:8px;background:#fff;font-size:14px;color:var(--cf-gray-1);cursor:pointer;font-family:inherit;min-width:140px;justify-content:space-between;transition:border-color 150ms ease,box-shadow 150ms ease}
.dd-btn:hover{border-color:var(--cf-gray-5)}
.dropdown.open .dd-btn{border-color:var(--cf-blue-5);box-shadow:0 0 0 3px rgba(8,111,255,.15)}
.dd-btn .dd-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px}
.dd-chev{flex:none;color:var(--cf-gray-5);transition:transform 180ms ease}
.dropdown.open .dd-chev{transform:rotate(180deg)}
.dd-menu{position:absolute;top:calc(100% + 6px);left:0;min-width:100%;width:max-content;max-height:320px;overflow:auto;background:#fff;border:1px solid var(--cf-gray-8);border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.14);padding:6px;z-index:30;animation:ddIn 140ms ease}
@keyframes ddIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
.dd-item{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:8px 10px;border-radius:6px;font-size:14px;color:var(--cf-gray-1);cursor:pointer;white-space:nowrap;transition:background 100ms ease}
.dd-item:hover{background:var(--cf-blue-9)}
.dd-item.active{background:var(--cf-blue-9);color:var(--cf-blue-2);font-weight:500}
.dd-item .check{width:14px;height:14px;flex:none;opacity:0;color:var(--cf-blue-4)}
.dd-item.active .check{opacity:1}
.dd-item.safe{color:var(--cf-gray-5);text-decoration:line-through;font-style:italic}
.dd-item.safe:hover{background:var(--cf-gray-9)}
.dd-item.safe.active{background:var(--cf-gray-9);color:var(--cf-gray-5);font-weight:400}
.dd-item.safe .check{color:var(--cf-gray-5)}
.dd-sep{height:1px;background:var(--cf-gray-8);margin:6px 4px}
.batchbar{display:none;align-items:center;gap:10px;padding:10px 14px;background:var(--cf-blue-9);border:1px solid var(--cf-blue-4);border-radius:8px;margin-bottom:14px;font-size:13px}
.batchbar.show{display:flex}
.batchbar .count{font-weight:600;color:var(--cf-blue-2)}
.batchbar .spacer{flex:1}
.score{font-variant-numeric:tabular-nums;color:var(--cf-gray-4)}
.msg-text{max-width:400px;word-break:break-all;white-space:pre-wrap}
/* ★ 修复：去掉 td 上的 display:flex，改为行内对齐，避免破坏表格单元格高度 */
.row-actions{white-space:nowrap;line-height:1}
.row-actions .pin-btn{vertical-align:middle;margin-right:8px}
.row-actions .del-btn{vertical-align:middle}
.pin-btn{background:none;border:1px solid var(--cf-gray-8);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;color:var(--cf-gray-4);transition:all 150ms ease}
.pin-btn:hover{border-color:var(--cf-blue-4);color:var(--cf-blue-4)}
.pin-btn.pinned{color:var(--cf-green-5);border-color:var(--cf-green-5);background:var(--cf-green-9)}
.del-btn{background:none;border:none;cursor:pointer;font-size:12px;color:var(--cf-gray-5);text-decoration:underline;text-underline-offset:3px}
.del-btn:hover{color:var(--cf-red-5)}
input[type=checkbox]{width:15px;height:15px;accent-color:var(--cf-blue-4);cursor:pointer;vertical-align:middle}
.pager{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 4px 0;flex-wrap:wrap}
.pager .info{font-size:13px;color:var(--cf-gray-5)}
.pager .pages{display:flex;gap:6px;align-items:center}
.pager .pages button{min-width:34px;padding:6px 10px;border:1px solid var(--cf-gray-8);background:#fff;border-radius:6px;font-size:13px;cursor:pointer;color:var(--cf-gray-1)}
.pager .pages button:hover:not(:disabled){border-color:var(--cf-blue-4);color:var(--cf-blue-4)}
.pager .pages button.active{background:var(--cf-blue-4);border-color:var(--cf-blue-4);color:#fff}
.pager .pages button:disabled{opacity:.4;cursor:not-allowed}
footer{color:var(--cf-gray-5);font-size:12px;padding:16px 0 32px}
.btn.danger{background:#fff;border-color:var(--cf-red-5);color:var(--cf-red-5)}
.btn.danger:hover{background:var(--cf-red-5);border-color:var(--cf-red-5);color:#fff}
.restart-overlay{position:fixed;inset:0;background:rgba(255,255,255,.82);backdrop-filter:blur(2px);display:flex;align-items:center;justify-content:center;z-index:100}
.restart-box{text-align:center}
.restart-box .spinner{width:40px;height:40px;border:3px solid var(--cf-gray-8);border-top-color:var(--cf-blue-4);border-radius:50%;margin:0 auto 18px;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.restart-box h2{font-size:20px;font-weight:600;margin-bottom:8px}
.restart-box p{color:var(--cf-gray-5);font-size:14px}
/* ===== 滚动条美化（Cloudflare 浅色风格） ===== */
*{scrollbar-width:thin;scrollbar-color:var(--cf-gray-8) transparent}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--cf-gray-8);border-radius:8px;border:2px solid #fff}
::-webkit-scrollbar-thumb:hover{background:var(--cf-gray-5)}
::-webkit-scrollbar-corner{background:transparent}
.dd-menu::-webkit-scrollbar{width:8px;height:8px}
</style>
</head>
<body>
<div id="login" class="login hidden">
  <div class="login-box">
    <h1>审核控制台</h1>
    <p>请输入访问 Token</p>
    <input id="tokenInput" type="password" placeholder="token" autocomplete="off">
    <button class="btn" onclick="doLogin()">登录</button>
    <p id="loginErr" class="err"></p>
  </div>
</div>
<div id="app" class="hidden">
  <div class="topbar">
    <div class="brand">QQ Bot 控制台</div>
    <nav>
      <span id="napcatPill" class="status-pill off"><span class="dot"></span><span id="napcatText">NapCat 未连接</span></span>
      <a href="/webui" class="active">审核日志</a>
      <a href="/webui/labels">标签管理</a>
      <button class="btn danger sm" onclick="restartBot()">重启服务</button>
      <a href="javascript:logout()">退出</a>
    </nav>
  </div>
  <div class="page">
    <h1 class="page-title">消息审核日志</h1>
    <p class="page-sub">已拦截与涉嫌消息 · 未置顶记录 30 天后自动删除</p>
    <div class="stats">
      <div class="stat-card"><div class="num" id="statTotal">0</div><div class="lbl">总记录</div></div>
      <div class="stat-card blocked"><div class="num" id="statBlocked">0</div><div class="lbl">已拦截</div></div>
      <div class="stat-card suspicious"><div class="num" id="statSuspicious">0</div><div class="lbl">涉嫌</div></div>
    </div>
    <div class="toolbar">
      <div class="search">
        <span class="icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/></svg></span>
        <input type="text" id="searchInput" placeholder="搜索消息内容 / 用户 / 群 / 标签">
      </div>
      <div class="dropdown" id="ddStatus">
        <button type="button" class="dd-btn" onclick="toggleDropdown('status')">
          <span class="dd-label" id="ddStatusLabel">全部状态</span>
          <svg class="dd-chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <div class="dd-menu hidden" id="ddStatusMenu"></div>
      </div>
      <div class="dropdown" id="ddLabel">
        <button type="button" class="dd-btn" onclick="toggleDropdown('label')">
          <span class="dd-label" id="ddLabelLabel">全部标签</span>
          <svg class="dd-chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <div class="dd-menu hidden" id="ddLabelMenu"></div>
      </div>
      <button class="btn ghost sm" onclick="load()">刷新</button>
    </div>
    <div class="batchbar" id="batchbar">
      <span>已选 <span class="count" id="selCount">0</span> 条</span>
      <span class="spacer"></span>
      <button class="btn ghost sm" onclick="batchAction('pin')">批量置顶</button>
      <button class="btn ghost sm" onclick="batchAction('unpin')">批量取消置顶</button>
      <button class="btn sm" style="background:var(--cf-red-5);border-color:var(--cf-red-5)" onclick="batchAction('delete')">批量删除</button>
      <button class="btn ghost sm" onclick="clearSel()">取消选择</button>
    </div>
    <div class="card" style="padding:0;overflow:auto">
      <table>
        <thead><tr>
          <th style="width:36px"><input type="checkbox" id="selAll" title="全选本页"></th>
          <th>时间</th><th>用户</th><th>群</th><th>消息</th><th>标签</th><th>得分</th><th>状态</th><th>操作</th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
    <div class="pager">
      <span class="info" id="pageInfo"></span>
      <div class="pages" id="pages"></div>
    </div>
    <footer>每 30 秒自动刷新 · 置顶记录永久保留</footer>
  </div>
  <div id="restartOverlay" class="restart-overlay hidden">
    <div class="restart-box">
      <div class="spinner"></div>
      <h2>正在重启服务</h2>
      <p>正在重新加载模型，请稍候…</p>
    </div>
  </div>
</div>
<script>
let token = localStorage.getItem("audit_token") || "";
let timer = null;
let page = 1;
const PAGE_SIZE = 20;
let selected = new Set();   // 已选 id
let allLabels = [];         // 候选标签（有序）
let safeLabels = new Set(); // 安全标签
let ddState = { status: "", label: "" }; // 下拉当前值

function esc(s){const d=document.createElement("div");d.textContent=s==null?"":String(s);return d.innerHTML}

async function api(path, opts={}){
  opts.headers = Object.assign({"Authorization":"Bearer "+token}, opts.headers||{});
  const r = await fetch(path, opts);
  if(r.status===401){logout();throw new Error("unauthorized")}
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}

function doLogin(){
  token = document.getElementById("tokenInput").value.trim();
  if(!token) return;
  localStorage.setItem("audit_token", token);
  enterApp();
  load().catch(e=>{document.getElementById("loginErr").textContent="登录失败: "+e.message;showLogin()});
}

function showLogin(){
  document.getElementById("login").classList.remove("hidden");
  document.getElementById("app").classList.add("hidden");
  stopTimer();
}
function enterApp(){
  document.getElementById("login").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
  startTimer();
}
function logout(){localStorage.removeItem("audit_token");token="";showLogin()}

function startTimer(){stopTimer();timer=setInterval(()=>{load();loadStatus()},30000)}
function stopTimer(){if(timer){clearInterval(timer);timer=null}}

function currentFilters(){
  return {
    status: ddState.status,
    label: ddState.label,
    q: document.getElementById("searchInput").value.trim(),
  };
}

function onFilterChange(){ page = 1; load(); }

/* ===== 自定义下拉 ===== */
const STATUS_OPTIONS = [
  { value: "", label: "全部状态" },
  { value: "blocked", label: "已拦截" },
  { value: "suspicious", label: "涉嫌" },
];

function toggleDropdown(which){
  const dd = document.getElementById(which==="status" ? "ddStatus" : "ddLabel");
  const menu = document.getElementById(which==="status" ? "ddStatusMenu" : "ddLabelMenu");
  const willOpen = !dd.classList.contains("open");
  closeDropdowns();
  if(willOpen){
    dd.classList.add("open");
    menu.classList.remove("hidden");
    renderDropdownMenu(which);
  }
}

function closeDropdowns(){
  document.querySelectorAll(".dropdown.open").forEach(d=>{
    d.classList.remove("open");
    const menu = d.querySelector(".dd-menu");
    if(menu) menu.classList.add("hidden");
  });
}

function attrEsc(s){return esc(s).replace(/"/g,"&quot;")}

function renderDropdownMenu(which){
  const menu = document.getElementById(which==="status" ? "ddStatusMenu" : "ddLabelMenu");
  const cur = ddState[which];
  const checkSvg = '<svg class="check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
  let html = "";
  if(which==="status"){
    html = STATUS_OPTIONS.map(o=>
      '<div class="dd-item'+(o.value===cur?" active":"")+'" data-which="status" data-value="'+attrEsc(o.value)+'" onclick="pickOption(this.dataset.which,this.dataset.value)"><span>'+o.label+'</span>'+checkSvg+'</div>'
    ).join("");
  }else{
    html = '<div class="dd-item'+(cur===""?" active":"")+'" data-which="label" data-value="" onclick="pickOption(this.dataset.which,this.dataset.value)"><span>全部标签</span>'+checkSvg+'</div>';
    if(allLabels.length) html += '<div class="dd-sep"></div>';
    // 安全标签排在最后（组内保持原顺序）
    const sorted = allLabels.filter(l=>!safeLabels.has(l)).concat(allLabels.filter(l=>safeLabels.has(l)));
    html += sorted.map(l=>{
      const safe = safeLabels.has(l);
      return '<div class="dd-item'+(safe?" safe":"")+(l===cur?" active":"")+'" data-which="label" data-value="'+attrEsc(l)+'" onclick="pickOption(this.dataset.which,this.dataset.value)"><span>'+esc(l)+'</span>'+checkSvg+'</div>';
    }).join("");
  }
  menu.innerHTML = html;
}

function pickOption(which, value){
  ddState[which] = value;
  const labelEl = document.getElementById(which==="status" ? "ddStatusLabel" : "ddLabelLabel");
  if(which==="status"){
    const o = STATUS_OPTIONS.find(o=>o.value===value);
    labelEl.textContent = o ? o.label : "全部状态";
  }else{
    labelEl.textContent = value || "全部标签";
  }
  closeDropdowns();
  onFilterChange();
}

document.addEventListener("click", e=>{
  if(!e.target.closest(".dropdown")) closeDropdowns();
});
document.addEventListener("keydown", e=>{ if(e.key==="Escape") closeDropdowns(); });

async function load(){
  try{
    const f = currentFilters();
    const qs = new URLSearchParams({limit:String(PAGE_SIZE), offset:String((page-1)*PAGE_SIZE)});
    if(f.status) qs.set("status", f.status);
    if(f.label) qs.set("label", f.label);
    if(f.q) qs.set("q", f.q);
    const data = await api("/api/messages?"+qs);
    render(data.items, data.total);
  }catch(e){/* 401 已在 api() 中处理 */}
}

async function loadStatus(){
  try{
    const s = await api("/api/status");
    document.getElementById("statTotal").textContent = s.stats.total;
    document.getElementById("statBlocked").textContent = s.stats.blocked;
    document.getElementById("statSuspicious").textContent = s.stats.suspicious;
    const pill = document.getElementById("napcatPill");
    const txt = document.getElementById("napcatText");
    if(s.napcat.connected){
      pill.className = "status-pill on";
      txt.textContent = "NapCat 已连接" + (s.napcat.bot_id ? " · "+s.napcat.bot_id : "");
    }else{
      pill.className = "status-pill off";
      txt.textContent = "NapCat 未连接";
    }
  }catch(e){}
}

async function loadLabels(){
  try{
    const d = await api("/api/labels");
    allLabels = d.labels || [];
    safeLabels = new Set(d.safe_labels || []);
    // 若当前选中的标签已被删除，重置
    if(ddState.label && !allLabels.includes(ddState.label)){
      ddState.label = "";
      document.getElementById("ddLabelLabel").textContent = "全部标签";
    }
    const dd = document.getElementById("ddLabel");
    if(dd.classList.contains("open")) renderDropdownMenu("label");
  }catch(e){}
}

function render(items, total){
  const tb = document.getElementById("tbody");
  if(!items.length){
    tb.innerHTML = '<tr><td colspan="9" class="muted" style="text-align:center;padding:30px">暂无记录</td></tr>';
  }else{
    tb.innerHTML = items.map(m=>{
      const statusText = m.status==="blocked"?"已拦截":"涉嫌";
      const sel = selected.has(m.id);
      return "<tr data-id='"+m.id+"' class='"+(sel?"selected":"")+"'>"
        +"<td><input type='checkbox' "+(sel?"checked":"")+" onchange='toggleSel("+m.id+",this.checked)'></td>"
        +"<td class='muted'>"+esc(m.ts)+"</td>"
        +"<td>"+esc(m.user_id)+"</td>"
        +"<td>"+(m.group_id?esc(m.group_id):'<span class="muted">私聊</span>')+"</td>"
        +"<td class='msg-text'>"+esc(m.text)+"</td>"
        +"<td>"+esc(m.label||"-")+"</td>"
        +"<td class='score'>"+(m.score!=null?m.score.toFixed(3):"-")+"</td>"
        +"<td><span class='badge "+m.status+"'>"+statusText+"</span></td>"
        +"<td class='row-actions'><button class='pin-btn "+(m.pinned?"pinned":"")+"' onclick='togglePin("+m.id+")'>"+(m.pinned?"已置顶":"置顶")+"</button>"
        +"<button class='del-btn' onclick='delMsg("+m.id+")'>删除</button></td>"
        +"</tr>";
    }).join("");
  }
  renderPager(total);
  updateSelUI();
  const selAll = document.getElementById("selAll");
  selAll.checked = items.length>0 && items.every(m=>selected.has(m.id));
}

function renderPager(total){
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if(page > pages) page = pages;
  const start = total===0 ? 0 : (page-1)*PAGE_SIZE + 1;
  const end = Math.min(total, page*PAGE_SIZE);
  document.getElementById("pageInfo").textContent = "第 "+start+"–"+end+" 条 / 共 "+total+" 条";
  const box = document.getElementById("pages");
  let html = "<button "+(page<=1?"disabled":"")+" onclick='goPage("+(page-1)+")'>上一页</button>";
  for(let i=1;i<=pages;i++){
    if(pages>7 && i>2 && i<pages-1 && Math.abs(i-page)>1){
      if(i===3 || i===pages-2) html += "<span class='muted' style='padding:0 4px'>…</span>";
      continue;
    }
    html += "<button class='"+(i===page?"active":"")+"' onclick='goPage("+i+")'>"+i+"</button>";
  }
  html += "<button "+(page>=pages?"disabled":"")+" onclick='goPage("+(page+1)+")'>下一页</button>";
  box.innerHTML = html;
}

function goPage(p){ page = p; load(); }

/* ===== 批量选择 ===== */
function toggleSel(id, checked){
  if(checked) selected.add(id); else selected.delete(id);
  const row = document.querySelector("#tbody tr[data-id='"+id+"']");
  if(row) row.className = checked?"selected":"";
  updateSelUI();
}
function updateSelUI(){
  document.getElementById("selCount").textContent = selected.size;
  document.getElementById("batchbar").classList.toggle("show", selected.size>0);
}
function clearSel(){
  selected.clear();
  updateSelUI();
  document.querySelectorAll("#tbody tr").forEach(r=>r.className="");
  const sa = document.getElementById("selAll"); if(sa) sa.checked=false;
}
function selectAllPage(checked){
  document.querySelectorAll("#tbody tr").forEach(row=>{
    const id = Number(row.getAttribute("data-id"));
    if(!id) return;
    if(checked) selected.add(id); else selected.delete(id);
    row.className = checked?"selected":"";
    const cb = row.querySelector("input[type=checkbox]");
    if(cb) cb.checked = checked;
  });
  updateSelUI();
}

async function batchAction(action){
  const ids = Array.from(selected);
  if(!ids.length) return;
  const msg = action==="delete" ? "确定删除选中的 "+ids.length+" 条记录？" : "确定对选中的 "+ids.length+" 条执行「"+(action==="pin"?"置顶":"取消置顶")+"」？";
  if(!confirm(msg)) return;
  try{
    await api("/api/messages/batch", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({action, ids})});
    clearSel();
    load(); loadStatus();
  }catch(e){ alert("操作失败: "+e.message); }
}

async function togglePin(id){await api("/api/messages/"+id+"/pin",{method:"POST"});load()}
async function delMsg(id){if(!confirm("确定删除这条记录？"))return;await api("/api/messages/"+id,{method:"DELETE"});load();loadStatus()}

/* ===== 重启服务 ===== */
let restarting = false;
async function restartBot(){
  if(restarting) return;
  if(!confirm("确定重启服务？将重新加载模型，期间机器人短暂离线。")) return;
  restarting = true;
  stopTimer();
  document.getElementById("restartOverlay").classList.remove("hidden");
  try{
    await api("/api/restart", {method:"POST"});
  }catch(e){
    // 401 已在 api() 处理；其他错误提示
    if(e.message !== "unauthorized"){
      alert("重启请求失败: "+e.message);
      restarting = false;
      document.getElementById("restartOverlay").classList.add("hidden");
      startTimer();
    }
    return;
  }
  // 轮询等待新进程就绪（模型加载约需 10-30s）
  const deadline = Date.now() + 90000;
  while(Date.now() < deadline){
    await new Promise(r=>setTimeout(r, 2000));
    try{
      await api("/api/status");
      // 新进程已就绪
      document.getElementById("restartOverlay").classList.add("hidden");
      restarting = false;
      loadLabels(); loadStatus(); load();
      startTimer();
      return;
    }catch(e){ /* 服务尚未就绪，继续等待 */ }
  }
  alert("重启超时，请手动刷新页面");
  location.reload();
}

let searchDebounce = null;
document.getElementById("searchInput").addEventListener("input", ()=>{
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(onFilterChange, 300);
});
document.getElementById("selAll").addEventListener("change", e=>selectAllPage(e.target.checked));
document.getElementById("tokenInput").addEventListener("keydown",e=>{if(e.key==="Enter")doLogin()});

if(token){
  enterApp();
  loadLabels();
  loadStatus();
  load().catch(()=>showLogin());
}else{showLogin()}
</script>
</body>
</html>
"""

# ================== 标签管理页 ==================

LABELS_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>标签管理 - QQ Bot 审核控制台</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
""" + CF_CSS + """
/* ===== 标签管理页 ===== */
.row{display:flex;gap:10px;margin-bottom:16px}
.row input[type=text]{flex:1}
ul.labels{list-style:none}
ul.labels li{display:flex;align-items:center;gap:12px;padding:12px 16px;border:1px solid var(--cf-gray-8);border-radius:8px;margin-bottom:8px;background:#fff;transition:border-color 150ms ease}
ul.labels li:hover{border-color:var(--cf-blue-5)}
ul.labels li .name{flex:1;font-size:14px;word-break:break-all}
ul.labels li .safe-tag{font-size:11px;font-weight:500;color:var(--cf-green-5);background:var(--cf-green-9);border-radius:6px;padding:2px 8px;white-space:nowrap}
ul.labels li label{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--cf-gray-4);cursor:pointer;white-space:nowrap}
ul.labels li input[type=checkbox]{width:15px;height:15px;accent-color:var(--cf-blue-4);cursor:pointer}
ul.labels li .rm{background:none;border:none;color:var(--cf-gray-5);cursor:pointer;font-size:13px;text-decoration:underline;text-underline-offset:3px}
ul.labels li .rm:hover{color:var(--cf-red-5)}
.savebar{display:flex;align-items:center;gap:14px;margin-top:4px}
.savebar .msg{font-size:13px}
.savebar .msg.ok{color:var(--cf-green-5)}
.savebar .msg.err{color:var(--cf-red-5)}
</style>
</head>
<body>
<div id="login" class="login hidden">
  <div class="login-box">
    <h1>审核控制台</h1>
    <p>请输入访问 Token</p>
    <input id="tokenInput" type="password" placeholder="token" autocomplete="off">
    <button class="btn" onclick="doLogin()">登录</button>
    <p id="loginErr" class="err"></p>
  </div>
</div>
<div id="app" class="hidden">
  <div class="topbar">
    <div class="brand">QQ Bot 控制台</div>
    <nav>
      <a href="/webui">审核日志</a>
      <a href="/webui/labels" class="active">标签管理</a>
      <a href="javascript:logout()">退出</a>
    </nav>
  </div>
  <div class="page" style="max-width:820px">
    <h1 class="page-title">标签管理</h1>
    <p class="page-sub">配置 AI 零样本分类的候选标签与安全标签，保存后立即热重载</p>
    <div class="card">
      <h2>候选标签</h2>
      <p class="hint">AI 会在这些标签中做零样本分类。勾选「安全」的标签命中后不会拦截（仍可能记为涉嫌）。</p>
      <div class="row">
        <input type="text" id="newLabel" placeholder="输入新标签，如：贬低或诅咒服务器">
        <button class="btn" onclick="addLabel()">添加</button>
      </div>
      <ul class="labels" id="labelList"></ul>
    </div>
    <div class="card">
      <h2>保存</h2>
      <p class="hint">保存后写入 labels.txt / safe_labels.txt 并立即热重载，无需重启机器人。</p>
      <div class="savebar">
        <button class="btn" onclick="save()">保存并热重载</button>
        <span id="saveMsg" class="msg"></span>
      </div>
    </div>
  </div>
</div>
<script>
let token = localStorage.getItem("audit_token") || "";
let labels = [];      // 候选标签（有序）
let safeSet = new Set(); // 安全标签

function esc(s){const d=document.createElement("div");d.textContent=s==null?"":String(s);return d.innerHTML}

async function api(path, opts={}){
  opts.headers = Object.assign({"Authorization":"Bearer "+token}, opts.headers||{});
  const r = await fetch(path, opts);
  if(r.status===401){logout();throw new Error("unauthorized")}
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}

function doLogin(){
  token = document.getElementById("tokenInput").value.trim();
  if(!token) return;
  localStorage.setItem("audit_token", token);
  enterApp();
  loadLabels().catch(e=>{document.getElementById("loginErr").textContent="登录失败: "+e.message;showLogin()});
}
function showLogin(){
  document.getElementById("login").classList.remove("hidden");
  document.getElementById("app").classList.add("hidden");
}
function enterApp(){
  document.getElementById("login").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
}
function logout(){localStorage.removeItem("audit_token");token="";showLogin()}

async function loadLabels(){
  const data = await api("/api/labels");
  labels = data.labels || [];
  safeSet = new Set(data.safe_labels || []);
  render();
}

function render(){
  const ul = document.getElementById("labelList");
  if(!labels.length){ul.innerHTML='<li class="muted" style="color:var(--muted)">暂无标签</li>';return}
  ul.innerHTML = labels.map((l,i)=>{
    const safe = safeSet.has(l);
    return "<li>"
      +"<span class='name'>"+esc(l)+"</span>"
      +(safe?"<span class='safe-tag'>安全</span>":"")
      +"<label><input type='checkbox' "+(safe?"checked":"")+" onchange='toggleSafe("+i+",this.checked)'> 安全</label>"
      +"<button class='rm' onclick='removeLabel("+i+")'>删除</button>"
      +"</li>";
  }).join("");
}

function addLabel(){
  const inp = document.getElementById("newLabel");
  const v = inp.value.trim();
  if(!v) return;
  if(labels.includes(v)){alert("标签已存在");return}
  labels.push(v);
  inp.value = "";
  render();
}
function removeLabel(i){
  const l = labels[i];
  labels.splice(i,1);
  safeSet.delete(l);
  render();
}
function toggleSafe(i, checked){
  const l = labels[i];
  if(checked) safeSet.add(l); else safeSet.delete(l);
  render();
}

async function save(){
  const msg = document.getElementById("saveMsg");
  msg.className = "msg"; msg.textContent = "保存中...";
  try{
    await api("/api/labels", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({labels: labels, safe_labels: Array.from(safeSet)})});
    msg.className = "msg ok"; msg.textContent = "已保存并热重载";
    loadLabels();
  }catch(e){
    msg.className = "msg err"; msg.textContent = "保存失败: "+e.message;
  }
}

document.getElementById("tokenInput").addEventListener("keydown",e=>{if(e.key==="Enter")doLogin()});
document.getElementById("newLabel").addEventListener("keydown",e=>{if(e.key==="Enter")addLabel()});

if(token){enterApp();loadLabels().catch(()=>showLogin())}else{showLogin()}
</script>
</body>
</html>
"""
