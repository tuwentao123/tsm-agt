from __future__ import annotations

import asyncio
import json
import logging
import os
import ipaddress
import subprocess
import sys
from collections.abc import Mapping
from contextlib import aclosing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse, JSONResponse, Response, StreamingResponse,
)

from tsm_agt.core import (
    AgentLoopLimitExceeded, ApprovalDecision, RuntimeInputIntent,
    ProjectTrustLevel, note_stream_released,
)
from tsm_agt.sdk.ui_events import _TURN_PROGRESS_EVENT_TYPES, _turn_progress_item, _bounded_text, _incomplete_reason
from tsm_agt.local_api import LocalEventApiServer
from tsm_agt.bootstrap.composition import (
    configure_request_diagnostics, user_skills_store_for_application,
    skill_installation_prompt, skill_catalog_for_application, builtin_skill_names,
    mcp_runtime_status,
)
from tsm_agt.bootstrap.model_configuration import load_configuration_document
from tsm_agt.bootstrap.mcp_status import describe_mcp_servers, mcp_configuration_summary
from tsm_agt.ports import ImageBlock, MessageRole, MCPServerStartupError

app = FastAPI(title="tsm-agt-web")
logger = logging.getLogger(__name__)
WEBUI_VERSION = "deepseek-cn-v20261013-workspace-trust"

INDEX_HTML = """
<!doctype html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>任务协同助手</title>
<style>
:root{color-scheme:light;--bg:#f8fafc;--bg-soft:#ffffff;--panel:#ffffff;--panel-elevated:#ffffff;--panel-muted:#f9fbfd;--border:#edf2f7;--border-strong:#e2e8f0;--text:#1f2937;--text-soft:#475569;--muted:#7b8794;--accent:#1677ff;--accent-soft:#eef6ff;--shadow-sm:0 10px 30px rgba(15,23,42,.035);--shadow-md:0 24px 60px rgba(15,23,42,.05);--radius-lg:20px;--radius-md:14px}
*{box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{margin:0;background:linear-gradient(180deg,#fbfdff 0%,#f6f9fc 100%);color:var(--text);font:14px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
body::before{content:"";position:fixed;inset:0;background:radial-gradient(circle at top,rgba(22,119,255,.08),transparent 30%);pointer-events:none}
button{font:inherit;color:inherit;background:none;border:none;cursor:pointer;border-radius:10px}
button:disabled{opacity:.4;cursor:not-allowed}
.layout{display:grid;grid-template-columns:260px minmax(0,1fr);height:100vh;height:100dvh;overflow:hidden}
.sidebar{border-right:1px solid var(--border);padding:20px 14px;display:flex;flex-direction:column;gap:14px;min-height:0;height:100vh;overflow-y:auto;overscroll-behavior:contain;background:rgba(255,255,255,.96);box-shadow:inset -1px 0 0 rgba(217,225,236,.7)}
.brand{font-size:15px;font-weight:600;padding:0 6px}
.new-session-btn{border:1px solid var(--border);padding:8px 10px;text-align:left;color:var(--muted)}
.new-session-btn:hover:not(:disabled){color:var(--text);border-color:#3c4046}
.sidebar-section{display:flex;flex-direction:column;gap:4px;min-height:0}
.section-header{display:flex;align-items:center;justify-content:space-between;padding:0 6px;margin-bottom:4px}
.section-title{font-size:12px;color:var(--muted)}
.icon-btn{color:var(--muted);padding:2px 6px;font-size:14px}
.icon-btn:hover{color:var(--text)}
.list{display:flex;flex-direction:column;gap:2px}
.workspace-group{display:flex;flex-direction:column;margin-bottom:6px}
.workspace-row{display:flex;align-items:center;gap:2px}
.workspace-remove{color:var(--muted);opacity:.5;padding:2px 6px;font-size:14px}
.workspace-remove:hover{color:#b42318;opacity:1}
.session-row{display:flex;align-items:center;gap:2px}
.session-row .session-item{flex:1;min-width:0}
.session-remove{color:var(--muted);opacity:0;padding:0 6px;font-size:14px}
.session-row:hover .session-remove{opacity:.6}
.session-remove:hover{color:#b42318;opacity:1}
.workspace-entry{flex:1;min-width:0}
.workspace-trust{font-size:10px;color:#64748b;margin-left:6px}.workspace-trust.trusted{color:#15803d}.workspace-trust.untrusted{color:#b45309}
.session-list{display:flex;flex-direction:column;gap:1px;margin:2px 0 0 10px;padding-left:8px;border-left:1px solid var(--border)}
.session-item{padding:5px 8px}
.session-item .item-title{font-size:13px}
.session-empty{padding:4px 8px;font-size:12px}
.list-item{text-align:left;padding:7px 8px;border-radius:6px;color:var(--text);overflow:hidden}
.list-item:hover{background:var(--panel-muted)}
.list-item.active{background:#e8f3ff;color:#1677ff}
.item-title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:500}
.item-meta{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl;text-align:left}
.hint{font-size:12px;color:var(--muted);padding:6px}
.main{display:flex;flex-direction:column;min-width:0;min-height:0;height:100vh;height:100dvh;overflow:hidden;background:transparent}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:18px 32px;border-bottom:1px solid var(--border);background:rgba(255,255,255,.9);backdrop-filter:blur(12px)}
#conversation-name{font-weight:500}
.session-id{margin-top:2px;font-family:"SFMono-Regular","JetBrains Mono","Fira Code",monospace;font-size:11px;color:#7b8794;cursor:pointer;user-select:text;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:52ch}
.session-id:hover{color:#2563eb}
.session-id.copied{color:#0f766e}
.turn-marker{align-self:flex-end;margin:0 2px -2px auto;font-family:"SFMono-Regular","JetBrains Mono","Fira Code",monospace;font-size:10px;color:#94a3b8;cursor:pointer;user-select:text}
.turn-marker:hover{color:#2563eb}
.turn-marker.copied{color:#0f766e}
.topbar .icon-btn{font-size:12px}
.chat-shell{flex:1 1 0;min-height:0;height:0;display:flex;flex-direction:column;overflow:hidden;padding:18px 22px 14px}
.workspace-stage{flex:1 1 0;min-height:0;height:100%;width:100%;display:grid;grid-template-columns:minmax(0,1.18fr) minmax(320px,.82fr);gap:14px;align-items:stretch;overflow:hidden;position:relative}
.workspace-resizer{position:absolute;top:0;bottom:0;width:14px;cursor:col-resize;z-index:10;touch-action:none;outline:none}.workspace-resizer[hidden]{display:none}.workspace-stage.resizing{user-select:none;cursor:col-resize}
.workspace-resizer-handle{position:absolute;top:0;bottom:0;width:2px;background-color:rgba(148,163,184,.9);left:6px}
.workspace-resizer:hover .workspace-resizer-handle,.workspace-resizer:focus-visible .workspace-resizer-handle{background-color:rgba(59,130,246,1)}
.pane-shell{position:relative;min-height:0;height:100%;display:flex;flex-direction:column;background:transparent;border:none;border-radius:0;overflow:hidden;backdrop-filter:none;box-shadow:none}
.pane-header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:14px 18px 10px;border-bottom:1px solid rgba(237,242,247,.75);background:rgba(255,255,255,.78)}
.pane-title{font-size:15px;font-weight:700;color:#111827}
.pane-subtitle{margin-top:4px;font-size:12px;color:#64748b}
.pane-badge{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:999px;background:#eff6ff;color:#2563eb;font-size:12px;font-weight:600}
.chat-area,.trace-pane{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;padding:10px;display:flex;flex-direction:column;gap:8px;background:transparent}
.chat-area.has-approval-dock{padding-bottom:min(52vh,460px)}
.chat-jump-bottom{position:absolute;left:50%;bottom:18px;transform:translateX(-50%);z-index:22;width:40px;height:40px;display:flex;align-items:center;justify-content:center;border:none;border-radius:50%;background:#fff;color:#475569;box-shadow:0 3px 14px rgba(15,23,42,.18);cursor:pointer}
.chat-jump-bottom[hidden]{display:none}
.chat-jump-bottom:hover{background:#eff6ff;color:#2563eb}
.chat-jump-bottom:focus-visible{outline:2px solid #3b82f6;outline-offset:3px}
.approval-dock{display:none;position:absolute;left:10px;right:10px;bottom:10px;z-index:20;pointer-events:none}
.approval-dock.visible{display:block}
.approval-dock .message.approval{width:100%;max-width:none;min-height:0;max-height:min(48vh,420px);overflow:auto;pointer-events:auto;padding:9px 11px;gap:6px;box-shadow:0 12px 28px rgba(15,23,42,.16)}
.approval-dock .approval-title{font-size:14px}.approval-dock .approval-subtitle{display:none}.approval-dock .approval-risk{padding:3px 8px;font-size:10px}.approval-dock .approval-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.approval-dock .approval-item{padding:5px 7px}.approval-dock .approval-label{margin-bottom:2px;font-size:9px}.approval-dock .approval-value{font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.approval-dock .approval-actions{gap:7px}.approval-dock .approval-btn{min-height:32px;padding:7px 13px;border-radius:10px;font-size:12px}.approval-details{border-top:1px solid #e5edf5;padding-top:5px}.approval-details>summary{cursor:pointer;font-size:11px;color:#64748b;user-select:none}.approval-details[open]>summary{margin-bottom:6px}.approval-details .approval-preview{max-height:140px}
.trace-pane{gap:10px;padding:12px 0;background:transparent;border-radius:0;box-shadow:none}
.trace-timeline-head{display:flex;align-items:baseline;justify-content:space-between;gap:10px;padding:0 4px}
.trace-timeline-heading{font-size:13px;font-weight:700;color:#0f172a}
.trace-timeline-caption{font-size:11px;color:#64748b}
.trace-overview-panel{background:linear-gradient(180deg,rgba(255,255,255,.94),rgba(248,250,252,.98));border-radius:14px;padding:10px;display:flex;flex-direction:column;gap:8px}
.trace-panel-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:4px}
.trace-panel-title{font-size:12px;font-weight:600;color:#0f172a}
.trace-panel-caption{font-size:11px;color:#64748b}
.trace-timeline-list{list-style:none;margin:0;padding:2px 0 8px 16px;position:relative;display:flex;flex-direction:column;gap:6px}
.trace-timeline-list::before{content:'';position:absolute;left:5px;top:6px;bottom:12px;width:2px;background:linear-gradient(180deg,#dbe5f0,#e8eef6)}
.trace-event{position:relative}
.trace-event::before{content:'';position:absolute;left:-14px;top:11px;width:7px;height:7px;border-radius:50%;background:#94a3b8;box-shadow:0 0 0 2px #fff}
.trace-event-running::before{background:#1677ff}
.trace-event-waiting::before{background:#faad14}
.trace-event-done::before{background:#52c41a}
.trace-event-failed::before{background:#ff4d4f}
.trace-event-card{background:rgba(255,255,255,.96);border-radius:10px;padding:7px 10px;display:flex;flex-direction:column;gap:3px}
.trace-event-head{display:flex;align-items:center;gap:7px;min-width:0}
.trace-event-kind{flex:none;font-size:10px;font-weight:600;color:#2563eb;background:#eff6ff;border-radius:999px;padding:1px 7px}
.trace-event-kind.kind-tool{color:#0f766e;background:#ecfdf5}
.trace-event-kind.kind-task{color:#475569;background:#f1f5f9}
.trace-event-kind.kind-approval,.trace-event-kind.kind-input{color:#b45309;background:#fffbeb}
.trace-event-title{flex:1;min-width:0;font-size:12px;font-weight:600;color:#111827;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trace-event-state{flex:none;font-size:10px;color:#64748b}
.trace-event-state.state-running{color:#2563eb}
.trace-event-state.state-waiting{color:#b45309}
.trace-event-state.state-done{color:#15803d}
.trace-event-state.state-failed{color:#b42318}
.trace-event-body{margin-top:1px;font-size:11px;line-height:1.5;color:#64748b;white-space:pre-wrap;word-break:break-word}
.trace-event-body.clamped{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;cursor:pointer}
.trace-timeline-tools{margin-top:4px;border-top:1px solid #edf2f7;padding-top:6px}
.trace-timeline-tools > summary{cursor:pointer;font-size:11px;color:#64748b;list-style:none;user-select:none}
.trace-timeline-tools > summary::-webkit-details-marker{display:none}
.trace-timeline-tools .trace-overview-panel{margin-top:6px}
.message{display:flex;flex-direction:column;position:relative;max-width:min(920px,100%);word-break:break-word;gap:8px}
.message.assistant{align-self:flex-start;width:100%;padding:18px 8px 22px;background:linear-gradient(180deg,rgba(255,255,255,.72),rgba(248,250,252,.58));border:none;border-radius:0;box-shadow:none}
.message.user{display:inline-flex;align-self:flex-end !important;margin-left:auto !important;margin-right:0 !important;width:fit-content;max-width:min(720px,calc(100% - 24px));background:#f3f4f6;color:#334155;border:none !important;outline:none !important;box-shadow:none !important;border-radius:16px;padding:12px 14px;text-align:left}
.message-body{width:100%;margin:0;white-space:normal;font-size:15px;line-height:1.68;letter-spacing:-.003em;color:var(--text);font-family:Inter,"SF Pro Display","Segoe UI",sans-serif}
.message-body > *:first-child{margin-top:0}
.message-body > *:last-child{margin-bottom:0}
.stream-text-label{width:100%;margin:4px 0 4px;color:#2563eb;font-size:12px;font-weight:600}
.message-body.streaming-text{white-space:pre-wrap;overflow-wrap:anywhere}
.message.stream-commentary{margin-top:6px;padding:0;border-radius:0;border:none;background:transparent}
.message.stream-commentary .message-body{font-size:13px;color:#374151}
.turn-trace{margin:6px 0 10px;padding:4px 10px;border-radius:0;border-left:3px solid #cbd5f5;background:transparent}
.message-body p{margin:.42em 0;color:var(--text-soft);max-width:88ch}
.message-body h1,.message-body h2,.message-body h3{line-height:1.24;font-weight:650;letter-spacing:-.022em;color:var(--text)}
.message-body h1{margin:1.1em 0 .42em;font-size:28px}
.message-body h2{margin:.95em 0 .38em;font-size:22px}
.message-body h3{margin:.8em 0 .3em;font-size:18px}
.message-body ul,.message-body ol{margin:.42em 0 .7em;padding-left:1.35em;color:var(--text-soft);max-width:88ch}
.message-body li{margin:.18em 0;line-height:1.68}
.message-body li > ul,.message-body li > ol{margin:.3em 0 .15em}
.message-body strong{color:var(--text);font-weight:600}
.message-body a{color:var(--accent);text-decoration:none}
.message-body a:hover{text-decoration:underline}
.message-body code{font-family:"SFMono-Regular","JetBrains Mono","Fira Code",monospace;background:#eef4fb;border:1px solid #d7e3f1;padding:.14rem .42rem;border-radius:6px;font-size:.9em;color:#1e293b}
.message-body pre{max-width:none;width:100%;margin:1rem 0;padding:16px 18px;background:linear-gradient(180deg,#f8fbff,#f1f5f9);border:1px solid #d9e2ec;border-radius:16px;overflow:auto;box-shadow:inset 0 1px 0 rgba(255,255,255,.65)}
.message-body pre code{display:block;padding:0;background:none;border:none;border-radius:0;color:#0f172a;line-height:1.7;min-width:max-content;font-size:13px;font-weight:500}
.message-body pre code .token.comment,.message-body pre code .token.prolog,.message-body pre code .token.doctype,.message-body pre code .token.cdata{color:#64748b}
.message-body pre code .token.keyword,.message-body pre code .token.selector,.message-body pre code .token.important{color:#7c3aed}
.message-body pre code .token.string,.message-body pre code .token.attr-value,.message-body pre code .token.char,.message-body pre code .token.builtin,.message-body pre code .token.inserted{color:#047857}
.message-body pre code .token.function,.message-body pre code .token.class-name{color:#0f766e}
.message-body pre code .token.number,.message-body pre code .token.boolean,.message-body pre code .token.constant{color:#b45309}
.message-body blockquote{margin:1.15rem 0;padding:10px 18px;border-left:3px solid #94a3b8;background:rgba(241,245,249,.78);border-radius:0 14px 14px 0;color:#475569;max-width:84ch}
.message-body blockquote p{color:inherit}
.message-body table{width:100%;border-collapse:collapse;margin:1.1rem 0;display:block;overflow-x:auto}
.message-body th,.message-body td{padding:10px 12px;border:1px solid #dbe3ee;text-align:left}
.message-body th{background:#f8fafc;color:#0f172a;font-weight:600}
.message-body hr{border:none;height:1px;background:#e5edf5;margin:1.5rem 0}
.message-section{display:flex;flex-direction:column;gap:10px;padding:0;background:transparent;border:none;box-shadow:none}
.message-section + .message-section{margin-top:12px}
.message-section-label{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#6b7280}
.code-block-shell{position:relative}
.code-block-header{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid #3a4350;background:#20252d;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#aeb7c3}
.terminal-block{margin:1rem 0;border-radius:16px;background:#353e49;border:1px solid #596577;overflow:hidden;box-shadow:none}
.terminal-header{display:flex;align-items:center;gap:8px;padding:10px 14px;background:#414b58;border-bottom:1px solid #596577;font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#edf2f7}
.terminal-dot{width:8px;height:8px;border-radius:50%;background:#ff7875;box-shadow:14px 0 0 #ffc53d,28px 0 0 #73d13d;margin-right:22px}
.terminal-body{padding:16px 18px;font-family:"SFMono-Regular","JetBrains Mono","Fira Code",monospace;font-size:13px;line-height:1.7;color:#f3f4f6;white-space:pre-wrap;overflow:auto;background:#313943}
.message-images{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
.message-images img{width:56px;height:56px;object-fit:cover;border-radius:10px;display:block;cursor:zoom-in;border:1px solid #dbe5f0}
.message.user,.message.assistant{position:relative;display:flex;flex-direction:column;gap:8px;padding:12px 14px;border-radius:14px;box-shadow:none;backdrop-filter:none}
.message.user{justify-content:flex-end;align-self:flex-end !important;margin-left:auto !important;margin-right:0 !important;background:#f3f4f6;border:none !important;outline:none !important;box-shadow:none !important;border-bottom-right-radius:8px;color:#334155;text-align:left}
.message.assistant{align-self:flex-start;background:transparent;border:none;border-bottom-left-radius:0;color:#1f2937;width:100%}
.message-role{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#64748b}
.message-role::before{content:'';width:8px;height:8px;border-radius:999px;background:currentColor;opacity:.85}
.message.assistant .message-role{color:#4b5563}
.message.user .message-role{color:#d1d5db}
.message-body{font-size:15px;line-height:inherit;color:inherit}
.message-body p:first-child,.message-body h1:first-child,.message-body h2:first-child,.message-body h3:first-child{margin-top:0}
.message-body p:last-child{margin-bottom:0}
.message.system{align-self:flex-start;max-width:100%;color:var(--muted);font-size:12px;padding:0 2px}
.message.task{align-self:stretch;max-width:100%;padding:0}
.task-card{border-radius:18px;background:linear-gradient(180deg,#ffffff,#f8fafc);padding:14px 16px;display:flex;flex-direction:column;gap:10px;box-shadow:0 2px 6px rgba(15,23,42,.04)}
.task-card-header{display:flex;justify-content:space-between;gap:10px;align-items:center}
.task-card-meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.task-phase-badge{padding:3px 8px;border-radius:999px;font-size:10px;background:#dbeafe;color:#2563eb;border:1px solid #93c5fd;font-weight:600}
.task-card-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;align-items:stretch}
.task-summary-item{background:#f8fafc;border-radius:10px;padding:8px 10px;min-height:0}
.task-summary-label{font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.06em}
.task-summary-value{margin-top:3px;font-size:13px;font-weight:700;color:#111827;line-height:1.2}
.todo-panel{display:flex;flex-direction:column;gap:9px;padding:11px 12px;border-radius:12px;background:#f8fafc}
.todo-panel-header{display:flex;align-items:center;justify-content:space-between;gap:10px}
.todo-panel-title{font-size:12px;font-weight:700;color:#111827}
.todo-panel-summary{font-size:11px;color:#64748b}
.todo-list{display:flex;flex-direction:column;gap:7px}
.todo-item{display:grid;grid-template-columns:18px minmax(0,1fr);gap:8px;align-items:start;padding:7px 8px;border-radius:9px;background:#f1f5f9}
.todo-marker{width:16px;height:16px;border-radius:50%;display:flex;align-items:center;justify-content:center;border:1px solid #94a3b8;color:#64748b;font-size:10px;font-weight:700;margin-top:1px}
.todo-item.in_progress{border-color:#93c5fd;background:#eff6ff}.todo-item.in_progress .todo-marker{border-color:#3b82f6;background:#3b82f6;color:#fff}
.todo-item.completed .todo-marker{border-color:#22c55e;background:#22c55e;color:#fff}.todo-item.completed .todo-content{text-decoration:line-through;color:#94a3b8}
.todo-item.blocked{border-color:#fca5a5;background:#fff1f2}.todo-item.blocked .todo-marker{border-color:#ef4444;background:#ef4444;color:#fff}
.todo-content{font-size:12px;color:#334155;line-height:1.45;word-break:break-word}
.todo-criteria{margin-top:2px;font-size:10px;color:#94a3b8;line-height:1.35}
.task-panel{display:none}
.task-panel.active{display:flex;flex-direction:column;gap:8px}
.task-collapse-toggle{display:flex;align-items:center;justify-content:center;gap:6px;padding:8px 12px;border-radius:10px;border:1px solid #d9e1ec;background:#f8fafc;color:#475569;font-size:12px;font-weight:600;cursor:pointer;transition:all .15s ease}
.task-collapse-toggle:hover{background:#eef4ff;border-color:#93c5fd;color:#2563eb}
.task-collapsible{position:relative;overflow:hidden;transition:max-height .2s ease}
.task-collapsible.collapsed{max-height:220px}
.task-collapsible.collapsed::after{content:'';position:absolute;left:0;right:0;bottom:0;height:64px;background:linear-gradient(180deg,rgba(248,250,252,0),rgba(248,250,252,.96))}
.task-progress{display:flex;flex-direction:column;gap:6px}
.task-progress-line{font-size:12px;color:#334155;white-space:pre-wrap;word-break:break-word;background:#f8fafc;border-left:2px solid #91caff;border-radius:10px;padding:8px 10px}
.task-progress-empty{font-size:12px;color:#94a3b8}
.task-card-header{display:flex;justify-content:space-between;gap:12px;align-items:center}
.task-card-title{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task-card-state{font-size:12px;color:var(--muted);flex:none}
.task-card-state.running{color:#8ab4f8}.task-card-state.waiting{color:#f0c674}.task-card-state.incomplete{color:#c68a00}.task-card-state.failed{color:#f4837a}.task-card-state.done{color:#81c995}
.incomplete-boundary{border:1px solid #f1d18a;background:#fffaf0;border-radius:12px;padding:12px;display:flex;flex-direction:column;gap:8px}.incomplete-title{font-weight:700;color:#7c5200}.incomplete-copy,.incomplete-gap{font-size:12px;color:#6b5a36}.incomplete-actions{display:flex;flex-wrap:wrap;gap:8px}.incomplete-actions button{border:1px solid #d9b95c;padding:6px 10px;background:#fff;color:#6b4b00}.incomplete-actions button.primary{background:#9a6700;color:#fff;border-color:#9a6700}
.completion-panel{display:flex;flex-direction:column;gap:6px;padding:10px 12px;border-radius:12px;background:#f1f5f9}.completion-title{font-size:12px;font-weight:700;color:#1e293b}.completion-summary,.completion-item{font-size:11px;line-height:1.45;color:#475569;word-break:break-word}.completion-item{display:grid;grid-template-columns:108px minmax(0,1fr);gap:8px}.completion-label{font-weight:600;color:#334155}.completion-value{color:#64748b}
.task-progress{display:flex;flex-direction:column;gap:3px;border-top:1px solid var(--border);padding-top:7px}
.task-progress-line{font-size:12px;color:var(--muted);white-space:pre-wrap;word-break:break-word}
.task-progress-line.markdown-progress{padding:0;border:none;background:transparent;white-space:normal;color:#475569;font-size:14px;line-height:1.68}
.task-progress-line.markdown-progress .message-section{gap:0}
.task-progress-line.markdown-progress .tool-call-markdown{max-width:100%}
.turn-calls{width:100%;margin:0;padding:0;border:none;border-radius:0;background:transparent;overflow:visible}
.turn-calls.has-calls{border:none;background:transparent}
.turn-trace{align-self:stretch;width:100%;max-width:none;padding:0;margin:0;background:transparent;border:none;box-shadow:none}
.turn-calls > summary{display:flex;align-items:center;gap:8px;padding:4px 0;font-size:11px;color:#64748b;cursor:pointer;list-style:none}
.turn-calls > summary::-webkit-details-marker{display:none}
.turn-calls > summary::before{content:'▸';font-size:10px;color:#94a3b8;transition:transform .15s ease;flex:none}
.turn-calls[open] > summary::before{transform:rotate(90deg)}
.turn-calls-label{font-weight:600;color:#475569;flex:none}
.turn-calls-names{color:#7b8794;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.turn-call-list{display:flex;flex-direction:column;gap:4px;padding:4px 0 6px;border-top:1px solid #edf2f7}
.turn-call-row{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;font-size:11px;line-height:1.5}
.turn-call-name{font-family:"SFMono-Regular","JetBrains Mono","Fira Code",monospace;font-size:11px;color:#1e293b;background:#eef4fb;border:1px solid #d7e3f1;border-radius:6px;padding:1px 6px;flex:none}
.turn-call-status{font-size:10px;border-radius:999px;padding:1px 7px;flex:none}
.turn-call-status.ok{color:#0f766e;background:#e8f7f3;border:1px solid #bfe6dd}
.turn-call-status.failed{color:#b42318;background:#fdeceb;border:1px solid #f6cdc9}
.turn-call-target{color:#64748b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1}
.turn-source-evaluation{margin:0 11px 8px;padding:8px 9px;border-radius:9px;background:#f5f9ff}.turn-source-evaluation-heading{font-size:11px;font-weight:700;color:#1d4ed8}.turn-source-evaluation-heading span{font-weight:500;color:#64748b}.turn-source-evaluation-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin-top:6px}.turn-source-evaluation-grid div{display:flex;flex-direction:column;gap:1px}.turn-source-evaluation-grid small{font-size:9px;color:#64748b}.turn-source-evaluation-grid strong{font-size:10px;color:#334155;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.turn-source-evaluation-grid strong.good{color:#047857}.turn-source-evaluation-grid strong.warn{color:#b45309}.turn-source-evaluation-note{margin-top:6px;font-size:10px;line-height:1.4;color:#475569}
.live-turn{opacity:.98}
.live-turn-panel{width:min(88ch,100%);margin:0 auto;border-radius:12px;background:linear-gradient(180deg,#f7fbff,#f2f8ff);padding:9px 12px;display:flex;flex-direction:column;gap:5px}
.live-turn-head{display:flex;align-items:center;gap:7px;font-size:11px;font-weight:600;color:#2563eb}
.live-turn-dot{width:7px;height:7px;border-radius:50%;background:#1677ff;box-shadow:0 0 0 4px rgba(22,119,255,.14);animation:live-turn-pulse 1.4s ease-in-out infinite;flex:none}
@keyframes live-turn-pulse{0%,100%{opacity:1}50%{opacity:.35}}
.live-turn-line{font-size:12px;color:#475569;line-height:1.55;word-break:break-word;padding-left:14px;position:relative}
.live-turn-line::before{content:'·';position:absolute;left:4px;color:#94a3b8}
.live-turn-line.active{color:#1d4ed8}
.live-turn-line.active::before{content:'▸';color:#1677ff}
.live-turn-line.failed{color:#b42318}
.live-turn-line.failed::before{content:'✕';color:#d92d20}
.task-progress-empty{font-size:12px;color:#686d75}
.task-card .message.approval{max-width:100%;align-self:stretch;margin-top:4px}
.message.approval{align-self:stretch;width:100%;max-width:none;scroll-margin-block:12px 24px;background:linear-gradient(180deg,#ffffff,#f8fafc);border:1px solid #d9e1ec;border-radius:18px;padding:10px 12px;display:flex;flex-direction:column;gap:8px;box-shadow:0 6px 14px rgba(15,23,42,.05);position:relative;overflow:hidden}
.message.approval::before{content:'';position:absolute;inset:0;background:radial-gradient(circle at top right,rgba(255,240,201,.08),transparent 32%);pointer-events:none}
.approval-hero{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
.approval-title{font-size:15px;font-weight:700;color:#1f2937;letter-spacing:-.02em;line-height:1.1}
.approval-subtitle{margin-top:1px;font-size:11px;color:#64748b;line-height:1.35}
.approval-risk{padding:5px 10px;border-radius:999px;background:#f5efe3;border:1px solid #e6d9bd;font-size:11px;color:#7c5b2a;white-space:nowrap}
.approval-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:8px}
.approval-item{padding:7px 9px;border-radius:10px;background:#f8fafc;border:1px solid #d9e1ec}
.approval-label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#94a3b8;margin-bottom:5px}
.approval-value{font-size:13px;color:#334155;line-height:1.5;word-break:break-word}
.approval-preview{margin:0;padding:8px 10px;background:#f8fafc;border:1px solid #d9e1ec;border-radius:10px;font-size:11px;max-height:180px;overflow:auto;white-space:pre-wrap;color:#334155;line-height:1.45;box-shadow:none}
.approval-actions{display:flex;gap:10px;margin-top:0;align-items:center}
.approval-btn{padding:11px 18px;border-radius:14px;border:1px solid transparent;font-weight:700;letter-spacing:.01em;transition:transform .18s ease,box-shadow .18s ease,filter .18s ease,background .18s ease,border-color .18s ease;position:relative;overflow:hidden}
.approval-btn:hover{transform:translateY(-1px);filter:brightness(1.04)}
.approval-btn:active{transform:translateY(1px) scale(.98)}
.approval-btn:focus-visible{outline:none;box-shadow:0 0 0 4px rgba(255,248,220,.18)}
.approval-btn::after{content:'';position:absolute;inset:0;background:linear-gradient(120deg,transparent,rgba(255,255,255,.18),transparent);opacity:0;transition:opacity .18s ease}
.approval-btn:hover::after{opacity:1}
.approval-btn.approve{background:linear-gradient(135deg,#809671,#5f7752);border-color:#a9be9b;color:#f6fff2;box-shadow:0 10px 20px rgba(96,119,82,.28)}
.approval-btn.deny{background:linear-gradient(135deg,#8f5c5c,#704545);border-color:#b78d8d;color:#fff4f4;box-shadow:0 10px 20px rgba(112,69,69,.24)}
.markdown-list{margin:.42em 0 .7em;padding-left:1.35em;color:#475569;display:block;line-height:1.68}
.markdown-list li{margin:.18em 0;padding:0}
.markdown-list li::marker{color:#94a3b8;font-size:.95em}
.markdown-blockquote{margin:18px 0;padding:14px 18px;border-left:4px solid #93c5fd;background:linear-gradient(180deg,#f8fbff 0%,#f8fafc 100%);border-radius:0 16px 16px 0;color:#475569;line-height:1.8;box-shadow:inset 0 1px 0 rgba(255,255,255,.5)}
.markdown-blockquote p{margin:.25em 0}
.markdown-table{width:100%;border-collapse:separate;border-spacing:0;margin:22px 0;background:#ffffff;border:1px solid #d9e1ec;border-radius:18px;overflow:hidden;box-shadow:0 12px 32px rgba(15,23,42,.07)}
.markdown-table th,.markdown-table td{padding:14px 16px;border-bottom:1px solid #e8edf5;text-align:left;font-size:13px;color:#475569;vertical-align:top;line-height:1.7}
.markdown-table tr:nth-child(even) td{background:rgba(248,250,252,.7)}
.markdown-table tr:last-child td{border-bottom:none}
.markdown-table th{background:#f8fafc;color:#0f172a;font-weight:700}
.tool-call-card{margin:16px 0;padding:18px 20px;border-radius:20px;background:#fff;border:1px solid #dbe3ee;box-shadow:0 10px 28px rgba(15,23,42,.06);display:flex;flex-direction:column;gap:14px}
.tool-call-card.collapsed .tool-call-toggle-icon{transform:rotate(-90deg)}
.tool-call-header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding-bottom:12px;border-bottom:1px solid #edf2f7}
.tool-call-toggle{display:flex;align-items:center;gap:10px;border:none;background:transparent;padding:0;cursor:pointer;text-align:left}
.tool-call-toggle-icon{font-size:11px;color:#64748b;transition:transform .18s ease}
.tool-call-title-wrap{display:flex;align-items:center;gap:10px;min-width:0}
.tool-call-title{font-size:15px;font-weight:700;color:#0f172a;white-space:nowrap}
.tool-call-subtitle{font-size:13px;color:#64748b;line-height:1.5;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tool-call-badge{padding:5px 10px;border-radius:999px;background:#f1f5f9;color:#475569;font-size:11px;font-weight:600;white-space:nowrap}
.tool-call-grid{display:flex;flex-direction:column;gap:10px}
.tool-call-card.collapsed .tool-call-grid{display:none}
.tool-call-item{display:grid;grid-template-columns:120px minmax(0,1fr);gap:14px;padding:10px 0;border-bottom:1px dashed #e2e8f0;align-items:start}
.tool-call-item:last-child{border-bottom:none;padding-bottom:0}
.tool-call-label{font-size:12px;font-weight:600;color:#64748b}
.tool-call-value{font-size:14px;color:#1e293b;line-height:1.7;word-break:break-word}
.tool-call-primary{font-weight:700;color:#0f172a}
.tool-call-markdown{display:block;padding:0;background:transparent;border:none;border-radius:0;overflow:visible;line-height:1.68}
.tool-call-markdown>*:first-child{margin-top:0!important}
.tool-call-markdown>*:last-child{margin-bottom:0!important}
.tool-call-markdown p{margin:.42em 0;color:#475569;line-height:1.68;white-space:normal;word-break:break-word}
.tool-call-markdown h1,.tool-call-markdown h2,.tool-call-markdown h3{margin:.65em 0 .55em;color:#0f172a;letter-spacing:-.02em;line-height:1.28}
.tool-call-markdown h1{font-size:1.24rem}
.tool-call-markdown h2{font-size:1.08rem}
.tool-call-markdown h3{font-size:.98rem}
.tool-call-markdown strong{color:#0f172a}
.tool-call-markdown ul,.tool-call-markdown ol{margin:.2em 0;padding-left:1.35rem}
.tool-call-markdown li + li{margin-top:.45em}
.tool-call-markdown blockquote{margin:.3em 0;border-left:none}
.tool-call-markdown blockquote>div,.tool-call-markdown blockquote>p,.tool-call-markdown blockquote{background:none}
.tool-call-markdown code{background:#eef2ff;color:#4338ca;padding:2px 7px;border-radius:7px;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;border:1px solid rgba(99,102,241,.12)}
.tool-call-markdown pre{margin:.8em 0;padding:16px 18px;background:#0f172a;color:#e2e8f0;border-radius:16px;overflow:auto;border:1px solid rgba(148,163,184,.18);box-shadow:inset 0 1px 0 rgba(255,255,255,.04),0 10px 24px rgba(15,23,42,.16)}
.tool-call-markdown pre code{display:block;background:transparent;border:none;padding:0;color:inherit;font-size:12.5px;line-height:1.75}
.tool-call-markdown hr{border:none;border-top:1px solid #dbe3ee;margin:14px 0}
.diff-viewer{display:flex;flex-direction:column;gap:14px;border:1px solid #d9e2ec;border-radius:16px;background:#ffffff;overflow:hidden;box-shadow:0 8px 24px rgba(15,23,42,.05)}
.diff-viewer-header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 16px;background:#f8fafc;border-bottom:1px solid #e5edf5}
.diff-viewer-title{font-size:13px;font-weight:700;color:#0f172a}
.diff-viewer-actions{display:flex;align-items:center;gap:10px}
.diff-viewer-toggle{border:1px solid #cbd5e1;background:#ffffff;color:#334155;border-radius:999px;padding:6px 12px;font-size:12px;font-weight:700;cursor:pointer;transition:all .18s ease}
.diff-viewer-toggle:hover{background:#eff6ff;border-color:#93c5fd;color:#1d4ed8}
.diff-viewer-meta{font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em}
.diff-viewer-body{display:flex;flex-direction:column;gap:14px;padding-bottom:14px}
.diff-viewer-body.collapsed{max-height:240px;overflow:hidden;position:relative}
.diff-viewer-body.collapsed::after{content:'';position:absolute;left:0;right:0;bottom:0;height:72px;background:linear-gradient(180deg,rgba(255,255,255,0) 0%,#ffffff 100%);pointer-events:none}
.diff-section{display:flex;flex-direction:column;gap:8px;padding:0 14px}
.diff-section-label{display:flex;align-items:center;justify-content:space-between;font-size:12px;font-weight:700;color:#475569}
.diff-code-block{position:relative;border-radius:14px;border:1px solid #e2e8f0;background:#fcfcfd;overflow:auto}
.diff-code-block.added{background:#f3fbf5;border-color:#cfe8d5}
.diff-code-block.removed{background:#fff6f5;border-color:#efd4d1}
.diff-code-block pre{margin:0;padding:16px 18px;font-size:12px;line-height:1.75;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#334155;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}
.diff-code-block.added pre{color:#1f5130}
.diff-code-block.removed pre{color:#7f1d1d}
.diff-inline-tag{display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border-radius:999px;font-size:11px;font-weight:700}
.diff-inline-tag.added{background:#dcfce7;color:#166534}
.diff-inline-tag.removed{background:#fee2e2;color:#991b1b}
.diff-muted{font-size:12px;color:#94a3b8;line-height:1.6}
.change-list{display:flex;flex-direction:column;gap:8px;margin:6px 0 2px}
.change-item{padding:10px 12px;background:#fff;border:1px solid #e2e8f0;border-radius:10px;box-shadow:0 1px 2px rgba(15,23,42,.03)}
.change-item-header{display:flex;align-items:center;justify-content:space-between;gap:10px}
.change-item-toggle{display:inline-flex;align-items:center;gap:8px;border:none;background:transparent;padding:0;cursor:pointer;width:100%;text-align:left}
.change-item-toggle-icon{font-size:11px;color:#64748b;transition:transform .18s ease}
.change-item.collapsed .change-item-toggle-icon{transform:rotate(-90deg)}
.change-item-title{font-size:11px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:.04em}
.change-item-meta{font-size:11px;color:#94a3b8}
.change-item-body{font-size:12px;color:#475569;line-height:1.45;margin-top:6px}
.change-item.collapsed .change-item-body{display:none}
.change-bullet{display:flex;gap:8px;margin-top:6px}
.change-bullet::before{content:'•';color:#94a3b8;font-weight:700}
@media(max-width:720px){.tool-call-header{align-items:flex-start;flex-direction:column}.tool-call-item{grid-template-columns:1fr;gap:6px}}
.empty-state{margin:auto;text-align:center;color:var(--muted);display:flex;flex-direction:column;gap:12px;align-items:center}
.empty-state h2{margin:0;font-size:15px;font-weight:500;color:var(--text)}
.link-btn{color:var(--accent)}
.composer{flex:0 0 auto;position:relative;z-index:30;padding:8px 20px 10px;display:flex;flex-direction:column;gap:6px;background:#f7f9fc;border-top:1px solid #e2e8f0}
.todo-dock[hidden]{display:none}
.todo-dock .todo-panel{gap:7px;padding:9px 12px;border-radius:12px;background:#fff}
.todo-dock .todo-list{max-height:180px;overflow-y:auto;gap:4px}
.todo-dock .todo-item{padding:5px 7px;background:transparent;border-color:transparent}
.todo-dock .todo-item.in_progress{background:#eff6ff}
.todo-dock .todo-item.blocked{background:#fff1f2}
.composer-box{border:none;border-radius:16px;padding:10px 12px;background:#f3f4f6;box-shadow:none}
.composer-box:focus-within{border-color:#91caff;box-shadow:0 0 0 2px rgba(22,119,255,.08)}
.composer-meta{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px}
.composer-title{font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#94a3b8}
.composer-hint{font-size:11px;color:var(--muted);white-space:nowrap;display:flex;align-items:center;min-height:36px}
.composer-input-row{display:flex;align-items:flex-start;gap:10px;min-width:0}
.textarea-wrap{flex:1;min-width:0;display:flex;align-items:center}
textarea{width:100%;min-height:48px;max-height:140px;background:#f3f4f6;border:none;color:#0f172a;font:inherit;font-size:14px;line-height:1.45;resize:none;outline:none;padding:10px 12px;border-radius:12px;transition:background .2s ease,box-shadow .2s ease}
textarea:focus{border-color:#91caff;background:#ffffff;box-shadow:0 0 0 3px rgba(22,119,255,.08)}
textarea::placeholder{color:#94a3b8}
.composer-actions{display:flex;flex-direction:column;align-items:flex-start;justify-content:flex-start;gap:8px;flex-shrink:0;min-height:40px}
.composer-action-buttons{display:flex;align-items:center;gap:10px}
.send-btn,.cancel-btn{display:inline-flex;align-items:center;justify-content:center;min-height:32px;color:#fff;padding:6px 12px;border-radius:6px;font-weight:600;letter-spacing:.01em;transition:background .2s ease,box-shadow .2s ease,border-color .2s ease}
.send-btn{background:linear-gradient(135deg,#4096ff,#1677ff);box-shadow:0 4px 10px rgba(22,119,255,.16)}
/* Softer cancel button: slightly smaller, lighter background and non-solid border */
.cancel-btn{background:linear-gradient(135deg,#ffd1cc,#ff7875);border:1px solid rgba(255,120,117,.6);color:#8c1f1a;box-shadow:0 3px 8px rgba(207,19,34,.18)}
.cancel-btn.hidden{display:none}
.send-btn:disabled,.cancel-btn:disabled{background:#3a4560;box-shadow:none;cursor:not-allowed;border-color:transparent}
.preview-list{display:flex;gap:8px;overflow:auto}
.preview-item{position:relative;flex:none}
.preview-item img{width:40px;height:40px;object-fit:cover;border-radius:6px;display:block;border:1px solid var(--border);cursor:pointer;transition:transform .16s ease,box-shadow .16s ease}
.preview-item img:hover{transform:scale(1.05);box-shadow:0 4px 12px rgba(15,23,42,.18)}
.preview-remove{position:absolute;top:-6px;right:-6px;width:18px;height:18px;line-height:1;border-radius:50%;background:#3a3d42;color:var(--text);font-size:12px}
.preview-remove:hover{background:#4a4e55}
.image-preview-modal{position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:rgba(15,23,42,.45);backdrop-filter:blur(2px);z-index:1000;padding:24px}
.image-preview-modal.visible{display:flex}
.image-preview-content{position:relative;display:flex;align-items:center;justify-content:center;padding:14px;border-radius:18px;background:rgba(255,255,255,.96);box-shadow:0 18px 50px rgba(15,23,42,.28)}
.image-preview-content img{max-width:min(88vw,1600px);max-height:82vh;border-radius:12px;display:block;object-fit:contain}
.image-preview-close{position:absolute;top:-10px;right:-10px;width:30px;height:30px;border-radius:50%;border:none;background:#0f172a;color:#fff;font-size:18px;cursor:pointer;box-shadow:0 4px 14px rgba(15,23,42,.24)}
.loading,.error-banner,.config-banner{display:none;font-size:13px;color:var(--muted);padding:0 20px}
.loading.visible,.error-banner.visible,.config-banner.visible{display:block}
.error-banner{color:#f4837a}
.config-banner{color:#d98b2b;white-space:pre-line}
@media(max-width:1500px){.workspace-stage{grid-template-columns:minmax(0,1fr) 380px}}
@media(max-width:1280px){.workspace-stage{grid-template-columns:1fr}.task-card-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:1024px){.layout{grid-template-columns:1fr;height:auto}.sidebar{display:none}.topbar{padding-inline:16px}.chat-shell{padding:16px 16px 14px}.chat-area,.trace-pane{padding:14px}.message.user{max-width:100%}.composer{padding:8px 14px 10px}.composer-box{padding:9px 10px}.textarea{min-height:44px}}
@media(max-width:720px){.task-card-summary{grid-template-columns:1fr}.chat-area,.trace-pane{padding:10px 12px 18px}.task-card{padding:9px 10px}.composer{padding:8px 10px}.composer-meta{align-items:flex-start;flex-direction:column;gap:3px}.composer-input-row{align-items:stretch;flex-wrap:wrap}.composer-actions{width:100%;justify-content:space-between}.send-btn{padding:8px 14px}.composer-hint{white-space:normal}}
/* 设置面板：左侧分类导航 + 右侧卡片区（对齐插件广场的信息层级） */
.settings-dialog { width: min(1320px, 96vw); height: min(864px, 92vh); padding: 0; border: none; border-radius: 20px; background: var(--panel, #fff); color: inherit; box-shadow: 0 32px 80px rgba(15, 23, 42, .32); overflow: hidden; }
.settings-dialog[open] { display: flex; flex-direction: column; }
.settings-dialog::backdrop { background: rgba(15, 23, 42, .48); }
.settings-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 22px 30px 18px; border-bottom: 1px solid var(--border, #e2e8f0); }
.settings-title { margin: 0; font-size: 17px; font-weight: 650; letter-spacing: .01em; }
.settings-subtitle { margin: 3px 0 0; font-size: 12px; line-height: 1.6; color: var(--muted, #64748b); }
.settings-close { border: none; background: transparent; font-size: 20px; line-height: 1; width: 30px; height: 30px; border-radius: 8px; color: var(--muted, #64748b); cursor: pointer; }
.settings-close:hover { background: var(--panel-muted, #f1f5f9); color: inherit; }
.settings-shell { flex: 1; display: grid; grid-template-columns: 244px minmax(0, 1fr); min-height: 0; }
.settings-nav { display: flex; flex-direction: column; gap: 2px; padding: 12px 10px; border-right: 1px solid var(--border, #e2e8f0); background: var(--panel-muted, #f8fafc); overflow-y: auto; }
.settings-nav-group { padding: 10px 10px 6px; font-size: 11px; letter-spacing: .06em; color: var(--muted, #94a3b8); }
.settings-nav-item { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border: 1px solid transparent; border-radius: 10px; background: transparent; color: var(--muted, #475569); font-size: 13px; font-family: inherit; text-align: left; cursor: pointer; }
.settings-nav-item:hover { background: rgba(148, 163, 184, .18); color: inherit; }
.settings-nav-item.active { background: var(--panel, #fff); border-color: var(--border, #e2e8f0); color: inherit; font-weight: 600; box-shadow: 0 1px 2px rgba(15, 23, 42, .06); }
.settings-nav-glyph { display: flex; align-items: center; justify-content: center; width: 24px; height: 24px; border-radius: 8px; background: rgba(148, 163, 184, .22); font-size: 12px; flex: none; }
.settings-nav-item.active .settings-nav-glyph { background: linear-gradient(135deg, #dbeafe, #ede9fe); color: #1d4ed8; }
.settings-nav-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.settings-tab-count { min-width: 20px; padding: 0 6px; border-radius: 999px; background: rgba(148, 163, 184, .22); font-size: 11px; line-height: 18px; text-align: center; flex: none; }
.settings-content { min-width: 0; overflow-y: auto; padding: 22px 30px 30px; }
.settings-panel { display: none; flex-direction: column; gap: 14px; }
.settings-panel.active { display: flex; }
.settings-toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.settings-toolbar-title { font-size: 15px; font-weight: 650; }
.settings-toolbar-caption { margin-top: 4px; font-size: 12px; line-height: 1.6; color: var(--muted, #64748b); overflow-wrap: anywhere; }
.settings-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.settings-btn { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--border-strong, #cbd5e1); background: var(--panel, #fff); color: inherit; padding: 7px 13px; border-radius: 9px; font-size: 12px; font-family: inherit; cursor: pointer; }
.settings-btn:hover { background: var(--panel-muted, #f1f5f9); }
.settings-btn.primary { background: #2563eb; border-color: #2563eb; color: #fff; }
.settings-btn.primary:hover { background: #1d4ed8; }
.settings-btn.ghost { border-color: transparent; background: transparent; color: var(--muted, #64748b); padding-inline: 8px; }
.settings-btn.ghost:hover { background: #fef2f2; color: #b91c1c; }
.settings-input { flex: 1; min-width: 0; padding: 8px 11px; border: 1px solid var(--border-strong, #cbd5e1); border-radius: 9px; font-size: 13px; font-family: inherit; background: var(--panel, #fff); color: inherit; }
.settings-inline { display: flex; gap: 8px; }
.settings-search { display: flex; align-items: center; gap: 8px; padding: 0 12px; border: 1px solid var(--border, #e2e8f0); border-radius: 10px; background: var(--panel-muted, #f8fafc); }
.settings-search:focus-within { border-color: #93c5fd; background: var(--panel, #fff); box-shadow: 0 0 0 3px rgba(37, 99, 235, .12); }
.settings-search-glyph { font-size: 13px; color: var(--muted, #94a3b8); }
.settings-search input { flex: 1; min-width: 0; padding: 9px 0; border: none; outline: none; background: transparent; color: inherit; font-family: inherit; font-size: 13px; }
.settings-summary { display: flex; gap: 10px; flex-wrap: wrap; }
.settings-stat { min-width: 108px; padding: 10px 14px; border: 1px solid var(--border, #e2e8f0); border-radius: 12px; background: var(--panel-muted, #f8fafc); }
.settings-stat-value { font-size: 18px; font-weight: 650; }
.settings-stat-label { margin-top: 2px; font-size: 11px; color: var(--muted, #64748b); }
.settings-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(288px, 1fr)); gap: 14px; }
.settings-card { display: flex; flex-direction: column; gap: 10px; padding: 14px 15px; border: 1px solid var(--border, #e2e8f0); border-radius: 14px; background: var(--panel, #fff); transition: border-color .15s, box-shadow .15s, transform .15s; }
.settings-card:hover { border-color: #bfdbfe; box-shadow: 0 10px 24px rgba(37, 99, 235, .1); transform: translateY(-1px); }
.settings-card-head { display: flex; align-items: center; gap: 10px; min-width: 0; }
.settings-avatar { display: flex; align-items: center; justify-content: center; width: 34px; height: 34px; border-radius: 10px; background: linear-gradient(135deg, #dbeafe, #ede9fe); color: #1d4ed8; font-size: 14px; font-weight: 650; flex: none; }
.settings-card-headtext { flex: 1; min-width: 0; }
.settings-card-title { font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.settings-card-sub { margin-top: 2px; font-size: 11px; color: var(--muted, #94a3b8); overflow-wrap: anywhere; }
.settings-card-desc { margin: 0; font-size: 12px; line-height: 1.6; color: var(--muted, #64748b); display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.settings-card-foot { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: auto; padding-top: 9px; border-top: 1px dashed var(--border, #e2e8f0); }
.settings-card-meta { font-size: 11px; color: var(--muted, #94a3b8); overflow-wrap: anywhere; text-align: right; }
.settings-tags { display: flex; gap: 6px; flex-wrap: wrap; }
.settings-tag { display: inline-flex; align-items: center; padding: 1px 8px; border-radius: 999px; font-size: 11px; line-height: 18px; background: rgba(148, 163, 184, .18); color: var(--muted, #475569); }
.settings-tag.ok { background: #ecfdf5; color: #047857; }
.settings-tag.warn { background: #fffbeb; color: #b45309; }
.settings-tag.bad { background: #fef2f2; color: #b91c1c; }
.settings-tag.accent { background: #eff6ff; color: #1d4ed8; }
.settings-switch { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted, #64748b); cursor: pointer; }
.settings-list { display: flex; flex-direction: column; gap: 8px; }
.settings-list-row { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border: 1px solid var(--border, #e2e8f0); border-radius: 10px; background: var(--panel, #fff); font-size: 12px; }
.settings-row-desc { flex: 1; min-width: 0; font-size: 12px; color: var(--muted, #64748b); overflow-wrap: anywhere; }
.settings-path { flex: 1; min-width: 0; overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
.settings-install { display: flex; flex-direction: column; gap: 6px; padding: 12px 14px; border: 1px solid var(--border, #e2e8f0); border-radius: 12px; background: var(--panel-muted, #f8fafc); }
@media (max-width: 980px) {
  .settings-dialog { width: 96vw; height: 92vh; }
  .settings-shell { grid-template-columns: 184px minmax(0, 1fr); }
  .settings-head { padding: 18px 20px 14px; }
  .settings-content { padding: 16px 18px 22px; }
}
@media (max-width: 640px) {
  .settings-shell { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); }
  .settings-nav { flex-direction: row; gap: 6px; overflow-x: auto; overflow-y: hidden; border-right: none; border-bottom: 1px solid var(--border, #e2e8f0); }
  .settings-nav-group { display: none; }
  .settings-nav-item { flex: none; }
}
.settings-install-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12.5px; font-weight: 600; }
.settings-install-line { margin: 0; font-size: 12px; line-height: 1.6; color: var(--muted, #64748b); overflow-wrap: anywhere; }
.settings-install > .settings-btn { align-self: flex-start; }
.settings-caption { grid-column: 1 / -1; font-size: 12px; line-height: 1.6; color: var(--muted, #64748b); }
.settings-empty { padding: 24px; border: 1px dashed var(--border-strong, #cbd5e1); border-radius: 12px; text-align: center; font-size: 12px; color: var(--muted, #94a3b8); }
.settings-status { margin: 14px 0 0; font-size: 12px; white-space: pre-wrap; color: var(--muted, #64748b); }
.settings-status:empty { display: none; }
@media (max-width: 860px) {
  .settings-dialog { width: 96vw; height: 92vh; }
  .settings-shell { grid-template-columns: 1fr; }
  .settings-nav { flex-flow: row wrap; overflow-x: auto; border-right: none; border-bottom: 1px solid var(--border, #e2e8f0); }
  .settings-nav-group { display: none; }
  .settings-content { padding: 14px; }
  .settings-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<div class=\"layout\">
  <aside class=\"sidebar\">
    <div class=\"brand\">任务协同助手</div>
    <button id=\"new-conversation-btn\" class=\"new-session-btn\" onclick=\"createConversation()\" disabled>+ 新建会话</button>

    <div class=\"sidebar-section\">
      <div class=\"section-header\">
        <span class=\"section-title\">工作区</span>
        <button id=\"workspace-create-btn\" class=\"icon-btn\" onclick=\"pickWorkspace()\" title=\"添加本地目录\">+</button>
      </div>
      <div id=\"workspace-empty-state\" class=\"hint\">点击右上角 + 添加本地目录</div>
      <div id=\"workspace-list\" class=\"list\"></div>
    </div>
  </aside>

  <main class=\"main\">
    <div class=\"topbar\">
      <div>
        <div id=\"conversation-name\">新建会话</div>
        <div id=\"conversation-session-id\" class=\"session-id\" title=\"点击复制会话 id\" hidden></div>
      </div>
      <div>
        <span id=\"workspace-status\" class=\"section-title\"></span>
        <button class=\"icon-btn\" onclick=\"openSkillSettings()\">设置</button>
        <button class=\"icon-btn\" onclick=\"clearMessages()\">清空</button>
      </div>
    </div>

    <dialog id="skill-settings-dialog" class="settings-dialog" aria-label="设置">
      <header class="settings-head">
        <div>
          <h2 class="settings-title">设置</h2>
          <p class="settings-subtitle">Skill 与 MCP 扩展 · 用户级配置，所有项目和会话共享</p>
        </div>
        <button class="settings-close" type="button" onclick="document.getElementById('skill-settings-dialog').close()" aria-label="关闭设置">×</button>
      </header>
      <div class="settings-shell">
        <nav class="settings-nav" role="tablist" aria-label="设置分类">
          <div class="settings-nav-group">Skill 扩展</div>
          <button class="settings-nav-item active" id="settings-nav-skills" type="button" role="tab" aria-controls="settings-panel-skills" aria-selected="true" onclick="showSettingsTab('skills')">
            <span class="settings-nav-glyph" aria-hidden="true">技</span>
            <span class="settings-nav-label">已安装 Skills</span>
            <span class="settings-tab-count" id="settings-tab-count-skills">0</span>
          </button>
          <button class="settings-nav-item" id="settings-nav-builtin" type="button" role="tab" aria-controls="settings-panel-builtin" aria-selected="false" onclick="showSettingsTab('builtin')">
            <span class="settings-nav-glyph" aria-hidden="true">流</span>
            <span class="settings-nav-label">内置工作流</span>
            <span class="settings-tab-count" id="settings-tab-count-builtin">0</span>
          </button>
          <button class="settings-nav-item" id="settings-nav-directories" type="button" role="tab" aria-controls="settings-panel-directories" aria-selected="false" onclick="showSettingsTab('directories')">
            <span class="settings-nav-glyph" aria-hidden="true">路</span>
            <span class="settings-nav-label">目录与发现</span>
            <span class="settings-tab-count" id="settings-tab-count-directories">0</span>
          </button>
          <div class="settings-nav-group">连接</div>
          <button class="settings-nav-item" id="settings-nav-mcp" type="button" role="tab" aria-controls="settings-panel-mcp" aria-selected="false" onclick="showSettingsTab('mcp')">
            <span class="settings-nav-glyph" aria-hidden="true">链</span>
            <span class="settings-nav-label">MCP 服务器</span>
            <span class="settings-tab-count" id="settings-tab-count-mcp">0</span>
          </button>
        </nav>
        <div class="settings-content" id="settings-content">
          <section class="settings-panel active" id="settings-panel-skills" role="tabpanel" data-settings-panel="skills">
            <div class="settings-toolbar">
              <div>
                <div class="settings-toolbar-title">已安装与可用的 Skill</div>
                <div class="settings-toolbar-caption">安装目录：<span id="skill-install-directory"></span></div>
                <div class="settings-toolbar-caption">安装后自动在最近使用的工作区新建安装会话，由模型按 Skill 文档检查、按需准备并验证环境。</div>
              </div>
              <div class="settings-actions">
                <button class="settings-btn primary" type="button" data-skill-install onclick="const input = document.getElementById('settings-skill-upload'); input.value = ''; input.click()">从本地目录安装</button>
              </div>
            </div>
            <label class="settings-search">
              <span class="settings-search-glyph" aria-hidden="true">⌕</span>
              <input id="settings-search-skills" type="search" placeholder="按名称、描述或来源搜索 Skill" oninput="filterSettingsCards('skills', this.value)" />
            </label>
            <input type="file" id="settings-skill-upload" webkitdirectory hidden onchange="uploadSkillDirectoryAndInstall(this.files, this)" />
            <div id="skill-installations" role="status" class="settings-list"></div>
            <div id="installed-skills-list" class="settings-grid"></div>
            <div id="settings-empty-skills" class="settings-empty" style="display:none">没有匹配的 Skill。</div>
          </section>

          <section class="settings-panel" id="settings-panel-builtin" role="tabpanel" data-settings-panel="builtin">
            <div class="settings-toolbar">
              <div>
                <div class="settings-toolbar-title">内置工作流</div>
                <div class="settings-toolbar-caption">随程序提供。取消勾选后点击「保存并刷新」才会生效。</div>
              </div>
              <div class="settings-actions">
                <button class="settings-btn primary" type="button" id="save-builtin-skill-settings" onclick="saveSkillSettings('save-builtin-skill-settings')">保存并刷新</button>
              </div>
            </div>
            <div id="skill-builtin-catalog" class="settings-grid"></div>
            <div id="settings-empty-builtin" class="settings-empty" style="display:none">没有匹配的内置工作流。</div>
          </section>

          <section class="settings-panel" id="settings-panel-directories" role="tabpanel" data-settings-panel="directories">
            <div class="settings-toolbar">
              <div>
                <div class="settings-toolbar-title">可发现 Skill 的目录</div>
                <div class="settings-toolbar-caption">默认包含安装目录。可添加多个目录，例如 ~/.kiro/skills；外部目录仅用于发现和读取。</div>
              </div>
              <div class="settings-actions">
                <button class="settings-btn" type="button" onclick="pickSkillDirectories()">选择多个目录…</button>
                <button class="settings-btn primary" type="button" id="save-skill-settings" onclick="saveSkillSettings()">保存并刷新</button>
              </div>
            </div>
            <div id="skill-discovery-list" class="settings-list"></div>
            <div class="settings-inline">
              <input id="skill-directory-path" class="settings-input" placeholder="输入目录路径，如 ~/.kiro/skills" />
              <button class="settings-btn" type="button" onclick="addSkillDirectoryPath()">添加</button>
            </div>
            <div id="skill-discovered-catalog" class="settings-grid"></div>
          </section>

          <section class="settings-panel" id="settings-panel-mcp" role="tabpanel" data-settings-panel="mcp">
            <div class="settings-toolbar">
              <div>
                <div class="settings-toolbar-title">已注册的 MCP 服务器</div>
                <div class="settings-toolbar-caption">数据来自私有 YAML 配置的 <code>mcp.enabled</code> 与 <code>mcp.servers</code>；重新检测会真实初始化每个服务器。</div>
              </div>
              <div class="settings-actions">
                <button class="settings-btn primary" type="button" onclick="loadMcpSettings(true)">重新检测</button>
                <button class="settings-btn" type="button" onclick="loadMcpSettings(false)">只看配置</button>
              </div>
            </div>
            <label class="settings-search">
              <span class="settings-search-glyph" aria-hidden="true">⌕</span>
              <input id="settings-search-mcp" type="search" placeholder="按名称、类型或命令搜索服务器" oninput="filterSettingsCards('mcp', this.value)" />
            </label>
            <p id="mcp-settings-status" role="status" class="settings-status"></p>
            <div id="mcp-server-list" class="settings-grid"></div>
            <div id="settings-empty-mcp" class="settings-empty" style="display:none">没有匹配的 MCP 服务器。</div>
          </section>

          <p id="skill-settings-status" role="status" class="settings-status"></p>
        </div>
      </div>
    </dialog>
    <div id=\"error-banner\" class=\"error-banner\"></div>
    <div id=\"config-banner\" class=\"config-banner\"></div>
    <div id=\"loading\" class=\"loading\">正在等待运行结果…</div>
    <div class=\"chat-shell\">
      <div class=\"workspace-stage\">
        <section class=\"pane-shell\">
          <section id=\"chat-area\" class=\"chat-area\"></section>
          <button id=\"chat-jump-bottom\" class=\"chat-jump-bottom\" type=\"button\" title=\"回到最新消息\" aria-label=\"回到最新消息\" aria-controls=\"chat-area\" onclick=\"jumpToChatBottom()\" hidden><svg width=\"20\" height=\"20\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\"><path d=\"M12 4v16m-6-6 6 6 6-6\" /></svg></button>
          <section id=\"approval-dock\" class=\"approval-dock\" aria-live=\"polite\"></section>
        </section>
        <div id=\"workspace-resizer\" class=\"workspace-resizer\" role=\"separator\" tabindex=\"0\" aria-label=\"调整对话与轨迹宽度\" aria-orientation=\"vertical\" aria-controls=\"chat-area trace-pane\" hidden><div class=\"workspace-resizer-handle\"></div></div>
        <aside class=\"pane-shell\">
          <div class=\"pane-header\">
            <div>
              <div class=\"pane-title\">执行轨迹</div>
              <div class=\"pane-subtitle\">实时 Trace Timeline，并为未来节点图与链路分析预留结构</div>
            </div>
            <div class=\"pane-badge\">Trace Graph Ready</div>
          </div>
          <section id=\"trace-pane\" class=\"trace-pane\"></section>
        </aside>
      </div>
    </div>

    <div class=\"composer\">
      <div id=\"todo-dock\" class=\"todo-dock\" hidden role=\"region\" aria-label=\"任务清单\"></div>
      <div id=\"preview-list\" class=\"preview-list\"></div>
      <div class=\"composer-meta\" style=\"margin-bottom:6px;display:flex;align-items:center;gap:12px;justify-content:space-between;\">
        <div class=\"composer-title\">会话输入</div>
        <div style=\"display:flex;align-items:center;gap:8px;\">
          <label for=\"model-profile\" style=\"font-size:11px;color:#64748b;\">模型
            <select id=\"model-profile\" aria-label=\"会话模型\" title=\"新建会话时选择模型；已有会话保持原模型\" style=\"max-width:250px;\">
              <option value=\"\">跟随服务配置</option>
            </select>
          </label>
          <label for=\"reasoning-effort\" style=\"font-size:11px;color:#64748b;display:flex;align-items:center;gap:6px;\">
            推理强度
            <select id=\"reasoning-effort\" style=\"font-size:11px;padding:2px 6px;border-radius:6px;border:1px solid #cbd5e1;background:#ffffff;color:#334155;\">
              <option value=\"\">跟随服务配置</option>
              <option value=\"none\">关闭（none）</option>
              <option value=\"minimal\">极低（minimal）</option>
              <option value=\"low\">低（low）</option>
              <option value=\"medium\">中（medium）</option>
              <option value=\"high\">高（high）</option>
              <option value=\"xhigh\">极高（xhigh）</option>
              <option value=\"max\">最大（max）</option>
            </select>
          </label>
        </div>
      </div>
      <div id=\"image-preview-modal\" class=\"image-preview-modal\" role=\"dialog\" aria-modal=\"true\" aria-label=\"图片预览\" aria-hidden=\"true\">
        <div class=\"image-preview-content\">
          <button id=\"image-preview-close\" class=\"image-preview-close\" title=\"关闭预览\">×</button>
          <img id=\"image-preview-target\" src=\"\" alt=\"图片预览\" />
        </div>
      </div>
      <!-- 输入区域整体改为单行紧凑布局：输入框在左，图片按钮与发送按钮在右侧同一行垂直居中 -->
      <div class=\"composer-input-row\" style=\"display:flex;align-items:center;gap:8px;padding:8px 12px;\">
        <button id=\"cancel-btn\" class=\"cancel-btn hidden\" onclick=\"handleCancelAction()\" style=\"margin-right:8px;\">取消</button>
        <div class=\"textarea-wrap\" style=\"flex:1;min-height:auto;\">
          <textarea id=\"prompt\" placeholder=\"描述你希望完成的任务、修复的问题或需要分析的内容…\" onpaste=\"handlePaste(event)\" style=\"min-height:40px;resize:vertical;\"></textarea>
        </div>
        <div class=\"composer-actions\" style=\"display:flex;align-items:center;margin-top:0;\">
          <div class=\"composer-action-buttons\" style=\"display:flex;align-items:center;gap:8px;\">
            <button id=\"diagnostic-toggle\" class=\"icon-btn\" onclick=\"toggleTurnDiagnostic()\">诊断下一轮</button>
            <button class=\"icon-btn\" onclick=\"showTurnDiagnostic()\">查看诊断</button>
            <button class=\"icon-btn\" onclick=\"document.getElementById('image-input').click()\" title=\"上传图片\">＋ 图片</button>
            <input id=\"image-input\" type=\"file\" accept=\"image/*\" multiple onchange=\"handleImages(event)\" style=\"display:none\" />
            <button id=\"send-btn\" class=\"send-btn\" onclick=\"runTask()\">发送</button>
          </div>
        </div>
      </div>
    </div>
  </main>
</div>
<dialog id=\"diagnostic-dialog\" style=\"max-width:90vw;max-height:85vh;width:1000px\"><button onclick=\"document.getElementById('diagnostic-dialog').close()\">关闭</button><p>请求内容可能包含敏感信息；脱敏不保证完整。过期后服务端自动清除。</p><div id=\"diagnostic-summary\"></div><details><summary>查看敏感请求正文</summary><pre id=\"diagnostic-output\" style=\"white-space:pre-wrap;word-break:break-all\"></pre></details></dialog>

<script>
""" + r"""
const selectedImages = [];
let diagnoseNextInput = false;
let diagnosticSessionId = null;
let latestDiagnostic = null;

function toggleTurnDiagnostic() {
  if (!diagnoseNextInput && !confirm('诊断将暂存下一轮发送给模型的请求，可能包含代码、对话、工具结果和未识别的密钥。仅在本机内存保存最多 1 小时；脱敏不保证完整。确认启用？')) return;
  diagnoseNextInput = !diagnoseNextInput;
  diagnosticSessionId = diagnoseNextInput ? activeConversationId : null;
  document.getElementById('diagnostic-toggle').textContent = diagnoseNextInput ? '取消诊断' : '诊断下一轮';
}

async function showTurnDiagnostic() {
  if (!latestDiagnostic || latestDiagnostic.sessionId !== activeConversationId) {
    showError('当前会话没有可查看的诊断，或已刷新页面。'); return;
  }
  try {
    const response = await fetch('/turn-diagnostics/read', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key: latestDiagnostic.key, session_id: latestDiagnostic.sessionId})
    });
    if (!response.ok) throw new Error('诊断已过期或不可用');
    const data = await response.json();
    document.getElementById('diagnostic-summary').textContent = `Turn：${data.turn_id || '尚未开始'}；已采集 ${data.requests.length} 次；超限遗漏 ${data.dropped} 次；约 ${data.expires_in_seconds} 秒后过期。`;
    document.getElementById('diagnostic-output').textContent = JSON.stringify(data, null, 2);
    document.getElementById('diagnostic-dialog').showModal();
  } catch (error) { showError(String(error.message || error)); }
}
document.getElementById('diagnostic-dialog').addEventListener('close', () => {
  document.getElementById('diagnostic-output').textContent = '';
  document.getElementById('diagnostic-dialog').querySelector('details').open = false;
});
// Approval cards can be created after a long-running Turn.  Reveal each new
// request once, but do not fight the user's own scrolling while they inspect
// its preview.
const revealedApprovalRequests = new Set();
const workspaces = [];
const conversations = [];
const ACTIVE_WORKSPACE_STORAGE_KEY = 'tsm-agt.active-workspace';
const DRAFT_STORAGE_PREFIX = 'tsm-agt.draft.';

function readBrowserStorage(storageName, key) {
  try {
    return window[storageName]?.getItem(key) ?? null;
  } catch (error) {
    console.warn(`无法读取 ${storageName}：`, error);
    return null;
  }
}

function writeBrowserStorage(storageName, key, value) {
  try {
    window[storageName]?.setItem(key, value);
  } catch (error) {
    console.warn(`无法写入 ${storageName}：`, error);
  }
}

function removeBrowserStorage(storageName, key) {
  try {
    window[storageName]?.removeItem(key);
  } catch (error) {
    console.warn(`无法删除 ${storageName}：`, error);
  }
}

function setupWorkspaceResizer() {
  const stage = document.querySelector('.workspace-stage');
  const resizer = document.getElementById('workspace-resizer');
  if (!stage || !resizer) return;
  const desktop = window.matchMedia('(min-width: 1281px)');
  const storageKey = 'tsm-agt.workspace-split';
  const stored = Number(readBrowserStorage('localStorage', storageKey));
  let ratio = stored > 0 && stored < 1 ? stored : 0.59;
  let activePointer = null;
  const bounds = () => {
    const rect = stage.getBoundingClientRect();
    const width = Math.max(0, rect.width - 14);
    const minimum = Math.min(320, width / 2);
    return { rect, width, minimum };
  };
  const stop = () => {
    const pointer = activePointer;
    activePointer = null;
    stage.classList.remove('resizing');
    if (pointer !== null && resizer.hasPointerCapture(pointer)) resizer.releasePointerCapture(pointer);
  };
  const apply = (left, persist = false) => {
    if (!desktop.matches) {
      stop();
      stage.style.gridTemplateColumns = '';
      resizer.hidden = true;
      return;
    }
    const { width, minimum } = bounds();
    if (!width) return;
    const clamped = Math.min(Math.max(left, minimum), width - minimum);
    ratio = clamped / width;
    stage.style.gridTemplateColumns = `${clamped}px minmax(0, 1fr)`;
    resizer.style.left = `${clamped}px`;
    resizer.hidden = false;
    resizer.setAttribute('aria-valuemin', String(Math.round(minimum / width * 100)));
    resizer.setAttribute('aria-valuemax', String(Math.round((width - minimum) / width * 100)));
    resizer.setAttribute('aria-valuenow', String(Math.round(ratio * 100)));
    if (persist) writeBrowserStorage('localStorage', storageKey, String(ratio));
  };
  resizer.addEventListener('pointerdown', (event) => {
    if (!desktop.matches || event.button !== 0) return;
    activePointer = event.pointerId;
    resizer.setPointerCapture(activePointer);
    stage.classList.add('resizing');
    event.preventDefault();
  });
  resizer.addEventListener('pointermove', (event) => {
    if (event.pointerId !== activePointer) return;
    const { rect } = bounds();
    apply(event.clientX - rect.left - 7, true);
  });
  resizer.addEventListener('pointerup', stop);
  resizer.addEventListener('pointercancel', stop);
  resizer.addEventListener('lostpointercapture', stop);
  resizer.addEventListener('keydown', (event) => {
    if (!desktop.matches) return;
    const { width, minimum } = bounds();
    let left = ratio * width;
    if (event.key === 'ArrowLeft') left -= 24;
    else if (event.key === 'ArrowRight') left += 24;
    else if (event.key === 'Home') left = minimum;
    else if (event.key === 'End') left = width - minimum;
    else return;
    event.preventDefault();
    apply(left, true);
  });
  const resize = () => apply(ratio * bounds().width);
  desktop.addEventListener('change', resize);
  new ResizeObserver(resize).observe(stage);
  resize();
}

let activeConversationId = null;
let activeWorkspaceId = readBrowserStorage(
  'localStorage', ACTIVE_WORKSPACE_STORAGE_KEY,
);
const traceVisualizationState = {
  mode: 'timeline',
  graph: {
    nodes: [],
    edges: [],
    lanes: [],
  },
  timeline: {
    groups: [],
    liveCursor: 0,
  },
};

function getConversationById(conversationId) {
  return conversations.find((item) => item.id === conversationId);
}

function draftStorageKey(conversationId) {
  return `${DRAFT_STORAGE_PREFIX}${conversationId}`;
}

function persistActiveDraft() {
  const active = getActiveConversation();
  if (!active) return;
  const value = document.getElementById('prompt').value;
  active.draft = value;
  writeBrowserStorage('sessionStorage', draftStorageKey(active.id), value);
}

function resolveConversationDraft(activeConversation, persistedDraft) {
  // Composer restore is intentionally isolated from task projections,
  // summaries, titles, runtime trace text, and message-derived metadata.
  // Only explicit draft state may hydrate the input field after refresh.
  if (typeof persistedDraft === 'string') {
    return persistedDraft;
  }

  if (typeof activeConversation?.draft === 'string') {
    return activeConversation.draft;
  }

  return '';
}

function restoreConversationDraft() {
  const active = getActiveConversation();
  if (!active) {
    document.getElementById('prompt').value = '';
    return;
  }

  const persistedDraft = readBrowserStorage(
    'sessionStorage', draftStorageKey(active.id),
  );
  const draft = resolveConversationDraft(active, persistedDraft);

  active.draft = draft;
  document.getElementById('prompt').value = draft;
}

function getActiveWorkspace() {
  return workspaces.find((item) => item.id === activeWorkspaceId);
}

function updateComposerButtonState() {
  const sendButton = document.getElementById('send-btn');
  const cancelButton = document.getElementById('cancel-btn');
  if (!sendButton || !cancelButton) return;

  sendButton.disabled = !getActiveWorkspace();

  // 取消按钮优先跟 Turn 状态绑定：
  // - 只要当前会话的当前任务存在“未结算的前台 Turn”，就显示取消；
  // - 只要该 Turn 在 Session 视角下进入终态（包括 awaiting_input/awaiting_user
  //   这类本轮已结束但任务挂起的状态），就隐藏取消按钮。
  // 但是在 Runtime 还没分配 turn_id 的窗口期（卡片已进入 PREPARING/RUNNING，
  // turnCalls 里还没有未结算 turn）也需要立即展示取消按钮，否则“一轮正在开始”
  // 时用户看不到可取消入口。

  const active = getActiveConversation();
  const activeTaskId = active?.activeTaskId || null;
  let runningTurn = null;

  // 从 authoritative Session 的 turnCalls 判断是否存在未结算的 turn。
  // 如果有多个未结算的 turn，选择最后一个（最新一轮）。
  if (active && activeTaskId && active.turnCalls instanceof Map) {
    active.turnCalls.forEach((turn) => {
      if (!turn || turn.task_id !== activeTaskId) return;
      if (turn.settled) return;
      // 覆盖之前的 runningTurn，最终得到最后一个未结算的 turn。
      runningTurn = turn;
    });
  }

  // 已停止/中断的 Turn 不会写入 Session 的结果记录，它在 turnCalls 里的
  // settled 会一直是 false；只按 turnCalls 判断会让取消按钮在一轮结束后长期
  // 滞留。这里再叠加 Task 自身的终态：任务已进入终态、且该终态属于当前这个
  // 未结算 Turn 时，这一轮已经无法取消。
  // settledTurnId 记录“上一次终态快照属于哪个 Turn”，这样在同一任务上开启
  // 新一轮、而快照还停留在上一轮终态时，取消按钮不会被误隐藏。
  if (runningTurn && activeTaskId) {
    const card = taskMessage(active.id, activeTaskId);
    const task = card ? card.task : null;
    const phase = String(task?.phase1State || '').toUpperCase();
    const status = String(task?.status || '').toLowerCase();
    const settledTurnId = String(task?.settledTurnId || '');
    const sameTurn = !settledTurnId
      || settledTurnId === String(runningTurn.turn_id || '');
    const taskFinished = TERMINAL_STATES.includes(phase)
      || ['stopped', 'interrupted', 'cancelled'].includes(status);
    if (taskFinished && sameTurn) runningTurn = null;
  }

  if (runningTurn && activeTaskId) {
    cancelButton.classList.remove('hidden');
    cancelButton.dataset.taskId = activeTaskId;
    cancelButton.dataset.turnId = runningTurn?.turn_id || '';
    cancelButton.disabled = false;
  } else {
    cancelButton.classList.add('hidden');
    cancelButton.disabled = false;
    cancelButton.dataset.taskId = '';
    cancelButton.dataset.turnId = '';
  }
}

function updateWorkspaceState() {
  const activeWorkspace = getActiveWorkspace();
  const disabled = !activeWorkspace;

  document.getElementById('new-conversation-btn').disabled = disabled;
  document.getElementById('prompt').disabled = disabled;
  document.getElementById('image-input').disabled = disabled;
  updateComposerButtonState();
  document.getElementById('workspace-status').textContent = activeWorkspace
    ? activeWorkspace.name
    : '';

  document.getElementById('workspace-empty-state').style.display =
    workspaces.length === 0 ? 'block' : 'none';

  renderWorkspaceList();
}

function renderWorkspaceList() {
  const list = document.getElementById('workspace-list');
  list.innerHTML = '';

  workspaces.forEach((workspace) => {
    const group = document.createElement('div');
    group.className = 'workspace-group';

    const header = document.createElement('div');
    header.className = 'workspace-row';

    const entry = document.createElement('button');
    entry.className = `list-item workspace-entry ${workspace.id === activeWorkspaceId ? 'active' : ''}`;
    entry.onclick = () => activateWorkspace(workspace.id);
    entry.title = workspace.path;
    const trustLevel = String(workspace.trust_level || 'UNTRUSTED');
    const trustLabel = trustLevel === 'TRUSTED_FULL'
      ? '完全信任' : trustLevel === 'TRUSTED_BUILD'
        ? '可执行' : trustLevel === 'TRUSTED_READ' ? '可读' : '只读未信任';
    const trustClass = trustLevel === 'UNTRUSTED' ? 'untrusted' : 'trusted';
    entry.innerHTML = `
      <div class='item-title'>${workspace.name}</div>
      <div class='item-meta'>${workspace.path}<span class='workspace-trust ${trustClass}'>${trustLabel}</span></div>
    `;

    const add = document.createElement('button');
    add.className = 'icon-btn';
    add.textContent = '+';
    add.title = '在此工作区新建会话';
    add.onclick = (event) => {
      event.stopPropagation();
      if (workspace.id !== activeWorkspaceId) activateWorkspace(workspace.id);
      createConversation();
    };

    header.append(entry, add);
    group.appendChild(header);

    const removeWorkspaceBtn = document.createElement('button');
    removeWorkspaceBtn.className = 'icon-btn workspace-remove';
    removeWorkspaceBtn.textContent = '−';
    removeWorkspaceBtn.title = '移除当前工作区';
    removeWorkspaceBtn.onclick = (event) => {
      event.stopPropagation();
      removeWorkspace(workspace.id);
    };
    header.appendChild(removeWorkspaceBtn);

    // Sessions belong to a workspace, so they are nested under their owner
    // instead of living in a separate flat list.
    const sessions = conversations.filter(
      (item) => item.workspaceId === workspace.id,
    );
    const nested = document.createElement('div');
    nested.className = 'session-list';
    if (sessions.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'hint session-empty';
      empty.textContent = '暂无会话';
      nested.appendChild(empty);
    } else {
      sessions.forEach((conversation) => {
        const item = document.createElement('button');
        item.className = `list-item session-item ${conversation.id === activeConversationId ? 'active' : ''}`;
        item.onclick = () => {
          persistActiveDraft();
          activeWorkspaceId = workspace.id;
          rememberWorkspaceUse(workspace.id);
          writeBrowserStorage(
            'localStorage', ACTIVE_WORKSPACE_STORAGE_KEY, workspace.id,
          );
          activeConversationId = conversation.id;
          updateWorkspaceState();
          // The header shows the active Session, so switching conversations
          // has to refresh it alongside the messages.
          renderConversations();
          renderMessages();
          restoreConversationDraft();
          ensureConversationLoaded(conversation.id);
        };
        item.innerHTML = `
          <div class='item-title'>${conversation.title}</div>
          <div class='item-meta' style='direction:ltr'>${conversation.messages.length} 条消息 · ${conversation.createdAt}</div>
        `;
        const row = document.createElement('div');
        row.className = 'session-row';
        const removeSessionBtn = document.createElement('button');
        removeSessionBtn.className = 'icon-btn session-remove';
        removeSessionBtn.textContent = '×';
        removeSessionBtn.title = '移除当前会话';
        removeSessionBtn.onclick = (event) => {
          event.stopPropagation();
          removeConversation(conversation.id);
        };
        row.append(item, removeSessionBtn);
        nested.appendChild(row);
      });
    }
    group.appendChild(nested);
    list.appendChild(group);
  });
}

function rememberWorkspaceUse(workspaceId) {
  fetch(`/workspaces/${encodeURIComponent(workspaceId)}/activate`, {method: 'POST'})
    .catch(error => console.error('记录最近工作区失败', error));
}

function activateWorkspace(workspaceId) {
  persistActiveDraft();
  activeWorkspaceId = workspaceId;
  rememberWorkspaceUse(workspaceId);
  writeBrowserStorage(
    'localStorage', ACTIVE_WORKSPACE_STORAGE_KEY, workspaceId,
  );

  const workspaceConversations = conversations.filter(
    (item) => item.workspaceId === activeWorkspaceId,
  );

  activeConversationId = workspaceConversations[0]?.id || null;
  updateWorkspaceState();
  renderConversations();
  renderMessages();
  restoreConversationDraft();
  if (activeConversationId) ensureConversationLoaded(activeConversationId);
}

async function pickWorkspace() {
  clearError();
  const response = await fetch('/workspaces/pick', { method: 'POST' });
  if (response.status === 204) return;
  if (!response.ok) {
    showError('无法打开目录选择器，请稍后重试。');
    return;
  }

  const workspace = await response.json();
  const existingIndex = workspaces.findIndex((item) => item.id === workspace.id);
  const isNewWorkspace = existingIndex < 0;
  if (isNewWorkspace) {
    const lineBreak = String.fromCharCode(10);
    const trustNow = window.confirm(
      `是否信任工作区“${workspace.name}”？${lineBreak}${lineBreak}` +
      `允许 Agent 执行受控命令、构建和测试；文件写入仍会单独请求授权。${lineBreak}` +
      '选择“取消”将保持只读，之后遇到命令时仍可再次审批。',
    );
    try {
      const trustResponse = await fetch(
        `/workspaces/${encodeURIComponent(workspace.id)}/trust`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ decision: trustNow ? 'trust' : 'reject' }),
        },
      );
      if (!trustResponse.ok) {
        const detail = await trustResponse.json().catch(() => ({}));
        throw new Error(detail.detail || '工作区信任状态保存失败');
      }
      Object.assign(workspace, await trustResponse.json());
    } catch (error) {
      showError(error.message || '工作区信任状态保存失败。');
    }
  }
  if (existingIndex >= 0) {
    workspaces[existingIndex] = workspace;
  } else {
    workspaces.push(workspace);
  }
  activateWorkspace(workspace.id);
}

function createConversation() {
  persistActiveDraft();
  if (!activeWorkspaceId) {
    showError('请先选择工作区，然后再创建会话。');
    return;
  }
  const conversation = {
    id: crypto.randomUUID(),
    workspaceId: activeWorkspaceId,
    title: `会话 ${conversations.filter((item) => item.workspaceId === activeWorkspaceId).length + 1}`,
    createdAt: new Date().toLocaleTimeString(),
    messages: [],
    draft: '',
  };
  conversations.unshift(conversation);
  activeConversationId = conversation.id;
  renderConversations();
  renderMessages();
  restoreConversationDraft();
}

function getActiveConversation() {
  return getConversationById(activeConversationId);
}

function removeConversation(conversationId) {
  const conversation = getConversationById(conversationId);
  if (!conversation) return;
  const label = conversation.title || '该会话';
  if (!window.confirm(`从列表移除「${label}」？磁盘文件与运行记录不受影响。`)) return;
  const wasActive = conversation.id === activeConversationId;
  const index = conversations.findIndex((item) => item.id === conversation.id);
  if (index >= 0) conversations.splice(index, 1);
  // A draft created in this tab was never registered server-side, so a 404 is
  // the expected answer; the local entry is already gone either way.
  fetch(`/sessions/${encodeURIComponent(conversation.id)}`, {method: 'DELETE'})
    .catch((error) => console.error('移除会话失败', error));
  if (wasActive) {
    const next = conversations.find(
      (item) => item.workspaceId === conversation.workspaceId,
    );
    activeConversationId = next ? next.id : null;
    if (next) ensureConversationLoaded(next.id);
  }
  renderConversations();
  renderMessages();
  restoreConversationDraft();
}

async function removeWorkspace(workspaceId) {
  const workspace = workspaces.find((item) => item.id === workspaceId);
  if (!workspace) return;
  const owned = conversations.filter((item) => item.workspaceId === workspaceId);
  const scope = owned.length
    ? `\\n该工作区下的 ${owned.length} 个会话将一并从侧栏移除。`
    : '';
  if (!window.confirm(`移除工作区「${workspace.name}」？${scope}`)) return;
  try {
    const response = await fetch(
      `/workspaces/${encodeURIComponent(workspaceId)}`, {method: 'DELETE'},
    );
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || '移除工作区失败');
    }
  } catch (error) {
    showError(error.message || '移除工作区失败。');
    return;
  }
  const index = workspaces.findIndex((item) => item.id === workspaceId);
  if (index >= 0) workspaces.splice(index, 1);
  for (let cursor = conversations.length - 1; cursor >= 0; cursor -= 1) {
    if (conversations[cursor].workspaceId === workspaceId) {
      conversations.splice(cursor, 1);
    }
  }
  if (activeWorkspaceId === workspaceId) {
    activeWorkspaceId = workspaces[0]?.id || null;
    activeConversationId = conversations.find(
      (item) => item.workspaceId === activeWorkspaceId,
    )?.id || null;
  }
  updateWorkspaceState();
  renderConversations();
  renderMessages();
  restoreConversationDraft();
  if (activeConversationId) ensureConversationLoaded(activeConversationId);
}

let modelProfiles = [];
let defaultModelProfile = null;

async function loadModelProfiles() {
  try {
    const response = await fetch('/model-profiles');
    if (!response.ok) return;
    const data = await response.json();
    modelProfiles = data.profiles || [];
    defaultModelProfile = data.default_profile || null;
    renderModelSelector();
  } catch (error) { console.error(error); }
}

function renderModelSelector() {
  const select = document.getElementById('model-profile');
  if (!select) return;
  const conversation = getActiveConversation();
  const selected = conversation?.modelProfileId || defaultModelProfile || '';
  if (conversation && !conversation.modelSelectionPending) conversation.confirmedModelProfileId = selected;
  select.replaceChildren();
  if (!modelProfiles.length) {
    const option = document.createElement('option');
    option.value = ''; option.textContent = '跟随服务配置'; select.appendChild(option);
  }
  modelProfiles.forEach(profile => {
    const option = document.createElement('option');
    option.value = profile.id;
    const statusLabels = {limited: '部分能力受限', unverified: '待验证', blocked: '服务受限', unsupported: '协议未接入'};
    const status = profile.kind === 'image' ? '图像模型' : statusLabels[profile.status] || '';
    const suffix = status || (profile.enabled ? '' : '未启用');
    option.textContent = `${profile.label}${suffix ? ' · ' + suffix : ''}`;
    option.disabled = !profile.enabled;
    option.title = [profile.status_detail, profile.disabled_reason].filter(Boolean).join('；');
    select.appendChild(option);
  });
  if (selected && !modelProfiles.some(profile => profile.id === selected)) {
    const option = document.createElement('option');
    option.value = selected; option.textContent = `${selected} · 配置已移除`; option.disabled = true;
    select.appendChild(option);
  }
  select.value = selected;
  select.disabled = !modelProfiles.some(profile => profile.enabled);
  select.title = '切换后从下一轮生效，正在生成的回复保持原模型';
  select.onchange = () => {
    if (!conversation || select.disabled) return;
    const previous = conversation.modelProfileId || defaultModelProfile;
    conversation.modelProfileId = select.value || null;
    updateReasoningOptions();
    conversation.reasoningEffort = document.getElementById('reasoning-effort')?.value || null;
    const selectedProfile = conversation.modelProfileId;
    const generation = (conversation.modelSelectionGeneration || 0) + 1;
    conversation.modelSelectionGeneration = generation;
    conversation.modelSelectionPending = true;
    // Serialize rapid changes so the last choice also wins on the server.
    const pending = conversation.modelSelectionSave || Promise.resolve();
    conversation.modelSelectionSave = pending.catch(() => {}).then(() =>
      persistConversationModel(conversation, selectedProfile, previous, generation));
    return conversation.modelSelectionSave;
  };
  const effort = document.getElementById('reasoning-effort');
  if (effort) {
    effort.value = conversation?.reasoningEffort || '';
    effort.onchange = () => {
      if (conversation) conversation.reasoningEffort = effort.value || null;
    };
  }
  updateReasoningOptions();
}

async function persistConversationModel(conversation, selected, previous, generation) {
  try {
    const response = await fetch(`/sessions/${encodeURIComponent(conversation.id)}/model-profile`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model_profile_id: selected}),
    });
    // Empty new conversations become durable on their first submitted message.
    if (response.status === 404) return;
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '模型选择保存失败');
    conversation.confirmedModelProfileId = data.model_profile_id;
  } catch (error) {
    if (conversation.modelSelectionGeneration !== generation) return;
    conversation.modelProfileId = conversation.confirmedModelProfileId || previous;
    if (getActiveConversation()?.id === conversation.id) renderModelSelector();
    showError(error.message || '模型选择保存失败');
  } finally {
    if (conversation.modelSelectionGeneration === generation) conversation.modelSelectionPending = false;
  }
}

function updateReasoningOptions() {
  const selected = document.getElementById('model-profile')?.value;
  const profile = modelProfiles.find(item => item.id === selected);
  const effort = document.getElementById('reasoning-effort');
  if (!effort) return;
  const allowed = profile?.reasoning_efforts || [];
  Array.from(effort.options).forEach(option => {
    option.disabled = Boolean(option.value && !allowed.includes(option.value));
  });
  if (effort.selectedOptions[0]?.disabled) effort.value = '';
}

function renderConversations() {
  // Sessions render inside their workspace group, so refreshing the workspace
  // list is what keeps both in sync.
  renderWorkspaceList();
  const active = getActiveConversation();
  document.getElementById('conversation-name').textContent = active?.title || '新建会话';
  renderSessionId(active);
  renderModelSelector();
}

// The Session id is what a Task, a Turn and any log line refer back to, so it
// stays visible at the top of the conversation instead of only in the store.
function renderSessionId(active) {
  const element = document.getElementById('conversation-session-id');
  if (!element) return;
  if (!active?.id) {
    element.hidden = true;
    element.textContent = '';
    element.onclick = null;
    return;
  }
  element.hidden = false;
  element.textContent = `会话 ${active.id}`;
  element.title = '点击复制会话 id';
  element.onclick = () => copyTextToClipboard(active.id, element);
}

function copyTextToClipboard(text, element, restore = null) {
  const original = restore || element?.textContent || '';
  const done = () => {
    if (!element) return;
    element.classList.add('copied');
    element.textContent = '已复制';
    setTimeout(() => {
      element.classList.remove('copied');
      element.textContent = original;
    }, 1200);
  };
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => done());
    return;
  }
  // http:// origins are not a secure context in every browser, so the
  // clipboard API is not always available.
  const area = document.createElement('textarea');
  area.value = text;
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  try { document.execCommand('copy'); } catch (error) { console.error(error); }
  document.body.removeChild(area);
  done();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function renderInlineMarkdown(text) {
  return text
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

function extractVisibleFollowUpRequest(content) {
  if (!content.includes('[session-follow-up]')) {
    return null;
  }

  const match = content.match(/Current request:\\s*([\\s\\S]*?)(?:\\n\\n(?:Authority-free source Task|Historical outcomes|Safety boundary|Remaining work|Completed work):|$)/);
  if (!match) {
    return null;
  }

  const request = match[1].trim();
  return request || null;
}

const toolCallCollapseState = new Map();
const diffCollapseState = new Map();
const taskDetailCollapseState = new Map();

function buildStableCollapseKey(prefix, content) {
  const normalized = String(content || '').slice(0, 600);
  let hash = 0;
  for (let index = 0; index < normalized.length; index += 1) {
    hash = ((hash << 5) - hash) + normalized.charCodeAt(index);
    hash |= 0;
  }
  return `${prefix}-${Math.abs(hash)}`;
}

function renderStructuredToolCall(content) {
  const visibleFollowUpRequest = extractVisibleFollowUpRequest(content);
  if (visibleFollowUpRequest) {
    return `
      <section class="tool-call-card follow-up-card" data-tool-call-container>
        <div class="tool-call-header">
          <div class="tool-call-title-wrap">
            <div class="tool-call-title">Follow-up Request</div>
            <div class="tool-call-subtitle">仅展示当前用户请求，隐藏 Runtime 包装上下文</div>
          </div>
        </div>
        <div class="tool-call-grid">
          <div class="tool-call-item">
            <div class="tool-call-label">当前请求</div>
            <div class="tool-call-value tool-call-primary">${renderInlineMarkdown(visibleFollowUpRequest)}</div>
          </div>
        </div>
      </section>
    `;
  }

  const rows = content.split(String.fromCharCode(10)).filter((line) => line.includes('：'));
  const fieldRows = rows
    .map((line) => line.match(/^\s*(工具|操作|命令|文件|状态)\s*：\s*(.+?)\s*$/))
    .filter(Boolean);
  if (fieldRows.length < 2) return null;

  const priorityLabels = ['工具', '操作', '命令', '文件', '状态'];
  const summary = [];
  const details = [];

  fieldRows.forEach((match) => {
    const [, label, value] = match;
    const cleanLabel = label.trim();
    if (!value) return;

    const isPrimary = priorityLabels.some((item) => cleanLabel.includes(item));
    const rowHtml = `
      <div class="tool-call-item">
        <div class="tool-call-label">${renderInlineMarkdown(cleanLabel)}</div>
        <div class="tool-call-value ${isPrimary ? 'tool-call-primary' : ''}">${renderInlineMarkdown(value)}</div>
      </div>
    `;

    if (isPrimary) {
      summary.push(rowHtml);
    } else if (!cleanLabel.includes('metadata') && !cleanLabel.includes('trace')) {
      details.push(rowHtml);
    }
  });

  const contentHtml = [...summary, ...details].join('');

  const collapseId = buildStableCollapseKey('tool-call', contentHtml);
  const isCollapsed = toolCallCollapseState.has(collapseId)
    ? toolCallCollapseState.get(collapseId)
    : true;

  return `
    <section class="tool-call-card ${isCollapsed ? 'collapsed' : ''}" data-tool-call-container data-tool-call-id="${collapseId}">
      <div class="tool-call-header">
        <button class="tool-call-toggle" type="button" data-tool-call-toggle="${collapseId}" aria-expanded="${isCollapsed ? 'false' : 'true'}">
          <span class="tool-call-toggle-icon">▼</span>
          <div class="tool-call-title-wrap">
            <div class="tool-call-title">工具调用</div>
            <div class="tool-call-subtitle">聚焦关键操作与执行结果</div>
          </div>
        </button>
        <div class="tool-call-badge">Live</div>
      </div>
      <div class="tool-call-grid" data-tool-call-body="${collapseId}">${contentHtml}</div>
    </section>
  `;
}

function renderDiffBlocks(text) {
  const lines = text.split(String.fromCharCode(10));
  const sections = [];
  let current = null;

  lines.forEach((line) => {
    const normalized = line.trim();
    if (/^(原内容|Old|Before)/i.test(normalized)) {
      current = { type: 'removed', title: '修改前', lines: [] };
      sections.push(current);
      return;
    }
    if (/^(新内容|New|After)/i.test(normalized)) {
      current = { type: 'added', title: '修改后', lines: [] };
      sections.push(current);
      return;
    }
    if (/^(---|\+\+\+|@@)/.test(normalized) || normalized.includes('metadata') || normalized.includes('mutation')) {
      return;
    }
    if (!current) {
      current = { type: 'neutral', title: '变更内容', lines: [] };
      sections.push(current);
    }
    current.lines.push(line);
  });

  const meaningfulSections = sections.filter((section) => section.lines.join('').trim());
  if (!meaningfulSections.length || text.length < 120) {
    return null;
  }

  const body = meaningfulSections.map((section) => {
    const typeClass = section.type === 'added' ? 'added' : section.type === 'removed' ? 'removed' : '';
    const badge = section.type === 'added'
      ? '<span class="diff-inline-tag added">新增</span>'
      : section.type === 'removed'
        ? '<span class="diff-inline-tag removed">旧版</span>'
        : '<span class="diff-muted">结构化展示已启用</span>';

    return `
      <div class="diff-section">
        <div class="diff-section-label">
          <span>${section.title}</span>
          ${badge}
        </div>
        <div class="diff-code-block ${typeClass}">
          <pre>${escapeHtml(section.lines.join(String.fromCharCode(10)).trim())}</pre>
        </div>
      </div>
    `;
  }).join('');

  const collapseId = buildStableCollapseKey('diff', text);
  const isCollapsed = diffCollapseState.has(collapseId)
    ? diffCollapseState.get(collapseId)
    : true;

  return `
    <div class="diff-viewer">
      <div class="diff-viewer-header">
        <div>
          <div class="diff-viewer-title">Patch Diff</div>
          <div class="diff-muted">聚焦真实改动内容，弱化 metadata 与原始 dump 输出</div>
        </div>
        <div class="diff-viewer-actions">
          <button class="diff-viewer-toggle" type="button" data-diff-toggle="${collapseId}" aria-expanded="${isCollapsed ? 'false' : 'true'}">${isCollapsed ? '展开完整内容' : '收起内容'}</button>
          <div class="diff-viewer-meta">Modern Viewer</div>
        </div>
      </div>
      <div class="diff-viewer-body ${isCollapsed ? 'collapsed' : ''}" data-diff-body="${collapseId}">
        ${body}
      </div>
    </div>
  `;
}

function renderMarkdown(text) {
  const escaped = escapeHtml(text || '');
  const lines = escaped.split(String.fromCharCode(10));
  const blocks = [];
  let paragraph = [];
  let codeFence = null;
  let blankLineCount = 0;

  function flushParagraph() {
    if (!paragraph.length) return;
    const content = paragraph.join('<br />').trim();
    if (!content || content === '<br />') {
      paragraph = [];
      return;
    }
    blocks.push(`<p>${renderInlineMarkdown(content)}</p>`);
    paragraph = [];
  }

  let listItems = [];

  function flushCodeFence() {
    if (!codeFence) return;
    const language = (codeFence.language || '').trim();
    const code = codeFence.lines.join(String.fromCharCode(10));
    const isTerminal = ['bash', 'shell', 'sh', 'zsh', 'terminal'].includes(language);
    const languageBadge = language ? `<div class="code-block-header"><span>${language}</span></div>` : '';
    blocks.push(isTerminal
      ? `<div class="terminal-block"><div class="terminal-header"><span class="terminal-dot"></span><span>${language || 'terminal'}</span></div><div class="terminal-body">${code}</div></div>`
      : `<div class="code-block-shell">${languageBadge}<pre><code>${code}</code></pre></div>`);
    codeFence = null;
  }

  function flushList() {
    if (!listItems.length) return;
    blocks.push(`<ul class="markdown-list">${listItems.join('')}</ul>`);
    listItems = [];
  }

  lines.forEach((line) => {
    const fence = line.match(/^```(.*)$/);
    if (fence) {
      if (codeFence) {
        flushCodeFence();
      } else {
        flushParagraph();
        flushList();
        codeFence = { language: (fence[1] || '').trim().toLowerCase(), lines: [] };
      }
      return;
    }

    if (codeFence) {
      codeFence.lines.push(line);
      return;
    }

    if (!line.trim()) {
      blankLineCount += 1;
      if (blankLineCount >= 2) {
        flushParagraph();
        flushList();
      }
      return;
    }

    blankLineCount = 0;

    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      blocks.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      return;
    }

    if (line.startsWith('- ') || line.startsWith('* ')) {
      flushParagraph();
      listItems.push(`<li>${renderInlineMarkdown(line.slice(2))}</li>`);
      return;
    }

    if (line === '---') {
      flushParagraph();
      flushList();
      blocks.push('<hr />');
      return;
    }

    flushList();
    paragraph.push(renderInlineMarkdown(line));
  });

  const structured = renderStructuredToolCall(escaped);
  flushCodeFence();
  flushParagraph();
  flushList();

  const markdownHtml = blocks.join('');
  const structuredSection = structured
    ? `<section class="message-tool-call">${structured}</section>`
    : '';

  return `
    <article class="message-section">
      ${structuredSection}
      <div class="tool-call-markdown markdown-flow">${markdownHtml}</div>
    </article>`;
}

let shouldAutoFollowChat = true;
let lastConversationScrollTop = 0;
const CHAT_FOLLOW_THRESHOLD = 96;
const CHAT_JUMP_SCREENS = 3;

function updateChatJumpButton(chatArea) {
  const button = document.getElementById('chat-jump-bottom');
  if (!button || !chatArea) return;
  const remaining = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight;
  button.hidden = chatArea.clientHeight <= 0 || remaining < chatArea.clientHeight * CHAT_JUMP_SCREENS;
  const dock = document.getElementById('approval-dock');
  button.style.bottom = dock?.classList.contains('visible') ? `${dock.offsetHeight + 22}px` : '18px';
}

function jumpToChatBottom() {
  const chatArea = document.getElementById('chat-area');
  if (!chatArea) return;
  chatArea.scrollTop = chatArea.scrollHeight;
  syncChatFollowState(chatArea);
}

function isNearChatBottom(chatArea) {
  if (!chatArea) return true;
  const remaining = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight;
  return remaining <= CHAT_FOLLOW_THRESHOLD;
}

function syncChatFollowState(chatArea) {
  if (!chatArea) return;
  shouldAutoFollowChat = isNearChatBottom(chatArea);
  lastConversationScrollTop = chatArea.scrollTop;
  updateChatJumpButton(chatArea);
}

function ensureChatScrollTracking(chatArea) {
  if (!chatArea || chatArea.dataset.scrollTrackingBound === 'true') return;
  chatArea.dataset.scrollTrackingBound = 'true';
  chatArea.addEventListener('scroll', () => {
    syncChatFollowState(chatArea);
  }, { passive: true });
  chatArea.addEventListener('load', () => {
    if (shouldAutoFollowChat) jumpToChatBottom();
    else updateChatJumpButton(chatArea);
  }, true);
  window.addEventListener('resize', () => updateChatJumpButton(chatArea));
  if (typeof ResizeObserver !== 'undefined') {
    const observer = new ResizeObserver(() => updateChatJumpButton(chatArea));
    observer.observe(chatArea);
    const dock = document.getElementById('approval-dock');
    if (dock) observer.observe(dock);
  }
}

document.addEventListener('click', (event) => {
  const toolCallToggle = event.target.closest('[data-tool-call-toggle]');
  if (toolCallToggle) {
    const container = toolCallToggle.closest('[data-tool-call-container]');
    if (!container) {
      return;
    }

    const collapsed = container.classList.toggle('collapsed');
    const collapseId = container.dataset.toolCallId;
    if (collapseId) {
      toolCallCollapseState.set(collapseId, collapsed);
    }
    toolCallToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    return;
  }

  const diffToggle = event.target.closest('[data-diff-toggle]');
  if (diffToggle) {
    const diffId = diffToggle.dataset.diffToggle;
    const body = document.querySelector(`[data-diff-body="${diffId}"]`);
    if (!body) {
      return;
    }

    const collapsed = body.classList.toggle('collapsed');
    diffCollapseState.set(diffId, collapsed);
    diffToggle.textContent = collapsed ? '展开完整内容' : '收起内容';
    diffToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    return;
  }
});

function renderMessages(forceScrollToBottom = false) {
  const active = getActiveConversation();
  renderConversationTodo(activeWorkspaceId ? active : null);
  const chatArea = document.getElementById('chat-area');
  const approvalDock = document.getElementById('approval-dock');
  const previousScrollTop = chatArea.scrollTop;
  const shouldStickToBottom = forceScrollToBottom || shouldAutoFollowChat || isNearChatBottom(chatArea);
  ensureChatScrollTracking(chatArea);
  const jumpButton = document.getElementById('chat-jump-bottom');
  if (jumpButton) jumpButton.hidden = true;
  chatArea.innerHTML = '';
  approvalDock.innerHTML = '';
  approvalDock.classList.remove('visible');
  chatArea.classList.remove('has-approval-dock');

  if (!activeWorkspaceId) {
    chatArea.innerHTML = `
      <div class='empty-state'>
        <h2>请先添加工作区目录</h2>
        <div>会话会绑定到所选的本地目录</div>
        <button class='link-btn' onclick='pickWorkspace()'>添加目录</button>
      </div>
    `;
    renderTracePane(null);
    return;
  }

  if (!active) {
    chatArea.innerHTML = `
      <div class='empty-state'>
        <h2>当前工作区暂无会话</h2>
        <button class='link-btn' onclick='createConversation()'>新建会话</button>
      </div>
    `;
    renderTracePane(null);
    return;
  }

  if (active.messages.length === 0) {
    chatArea.innerHTML = `
      <div class='empty-state'>
        <h2>${active.title}</h2>
        <div>描述一个任务，或上传图片继续</div>
      </div>
    `;
    renderTracePane(active);
    return;
  }

  // The Turn that is running belongs directly under the question it answers,
  // above the conclusion it will produce. The Task card sits at the Task's
  // first Turn and would put this Turn's progress far above it.
  // A suspended approval is still the active Turn.  Treating WAITING as
  // "not running" made its just-created input lose the live trace until a
  // later page rebuild happened to recover the durable turn record.
  const runningTask = [...active.messages].reverse().find(
    (item) => item.role === 'task'
      && ['PREPARING', 'RUNNING', 'WAITING'].includes(item.task?.phase1State),
  );
  const lastQuestionIndex = active.messages.reduce(
    (found, item, index) => (item.role === 'user' ? index : found), -1,
  );
  const currentTurnId = active.messages[lastQuestionIndex]?.turnId;
  const durableCurrentTurn = currentTurnId
    ? active.turnCalls?.get(currentTurnId)
    : null;
  // A reloaded in-flight Turn may already have a durable event trail. Do not
  // render a second, volatile copy; before its first committed event the live
  // panel remains the immediate status signal.
  const hasDurableCurrentTrace = !durableCurrentTurn?.settled
    && Array.isArray(durableCurrentTurn?.progress)
    && durableCurrentTurn.progress.length > 0;
  let liveInserted = false;

  active.messages.forEach((message, index) => {
    const element = document.createElement('div');
    element.className = `message ${message.role}`;
    let inputTrace = null;
    const text = String(message.content ?? '').trim();
    if (message.role === 'task') {
      renderTaskCard(element, message, active.id);
    } else if (message.role === 'approval' || message.role === 'clarification') {
      if (message.approval?.request_id) {
        element.dataset.approvalRequestId = message.approval.request_id;
      }
      if (message.role === 'clarification') renderClarificationCard(element, message);
      else renderApprovalCard(element, message);
      // Approval is an operation dock, not transcript history: it remains
      // pinned at the bottom of the chat pane while the message list scrolls.
      approvalDock.appendChild(element);
      approvalDock.classList.add('visible');
      chatArea.classList.add('has-approval-dock');
      return;
    } else if (message.role === 'system') {
      element.textContent = text;
    } else {
      // Every Turn starts at its input, so the Turn id is labelled there. It is
      // what ties this exchange to its Task events and its call panel.
      if (message.role === 'user' && message.turnId) {
        chatArea.appendChild(renderTurnMarker(message.turnId));
      }
      // Follow a coding agent's transcript order: the Turn's work (what it
      // thought, which tools it used, how it ended) reads above, and the final
      // conclusion is the last thing under it.
      const callPanel = message.textStream ? null : renderTurnCallPanel(active, message);
      if (callPanel && message.role === 'assistant') element.appendChild(callPanel);

      if (message.textStream) {
        const stream = message.textStream;
        if (stream.phase === 'commentary') element.classList.add('stream-commentary');
        const label = document.createElement('div');
        label.className = 'stream-text-label';
        label.textContent = stream.status === 'committed' ? ''
          : stream.status === 'reviewing' ? '正在确认回答…'
          : stream.phase === 'commentary' ? '处理说明 · 正在生成…' : '正在生成…';
        element.appendChild(label);
      }

      const body = document.createElement('div');
      body.className = 'message-body';
      if (message.textPresentation) {
        element.dataset.textStreamId = message.textPresentation.id;
        paintTaskTextBody(body, message);
        scheduleTaskTextRender(message, active.id);
      } else {
        body.innerHTML = renderMarkdown(text);
      }
      element.appendChild(body);

      // Do not nest an in-flight trace in the user bubble: its full-width
      // panel would expand the bubble and make the input appear left-aligned.
      // It is a sibling directly below the input instead.
      if (callPanel && message.role === 'user') {
        inputTrace = document.createElement('div');
        inputTrace.className = 'turn-trace';
        inputTrace.appendChild(callPanel);
      }

      // Attachments render under the text so the prompt stays readable.
      if (message.images?.length) {
        const gallery = document.createElement('div');
        gallery.className = 'message-images';
        message.images.forEach((image) => {
          const thumb = document.createElement('img');
          thumb.src = image.image_url;
          thumb.alt = image.name || '附件图片';
          thumb.title = `${image.name || '附件图片'} · 点击放大预览`;
          thumb.tabIndex = 0;
          thumb.setAttribute('role', 'button');
          thumb.setAttribute('aria-label', `放大预览：${thumb.alt}`);
          thumb.onclick = () => openImagePreview(image.image_url, image.name);
          thumb.onkeydown = (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              openImagePreview(image.image_url, image.name);
            }
          };
          gallery.appendChild(thumb);
        });
        element.appendChild(gallery);
      }
    }
    chatArea.appendChild(element);
    if (inputTrace) chatArea.appendChild(inputTrace);
    if (
      runningTask && !hasDurableCurrentTrace && !durableCurrentTurn
      && !liveInserted && index === lastQuestionIndex
    ) {
      chatArea.appendChild(renderLiveTurnPanel(runningTask.task));
      liveInserted = true;
    }
  });
  if (runningTask && !hasDurableCurrentTrace && !durableCurrentTurn && !liveInserted) {
    chatArea.appendChild(renderLiveTurnPanel(runningTask.task));
  }

  // The trace pane is a sibling view, not a tail step of chat scrolling. It must
  // render on every pass; keeping it after the scroll branches meant the whole
  // panel stayed blank in the common "stick to bottom" case.
  renderTracePane(active);

  if (shouldStickToBottom) {
    chatArea.scrollTop = chatArea.scrollHeight;
    shouldAutoFollowChat = true;
    lastConversationScrollTop = chatArea.scrollTop;
    updateChatJumpButton(chatArea);
    return;
  }

  const maxScrollTop = Math.max(0, chatArea.scrollHeight - chatArea.clientHeight);
  chatArea.scrollTop = Math.min(lastConversationScrollTop || previousScrollTop || 0, maxScrollTop);
  updateChatJumpButton(chatArea);
}

function timelineTasksOf(conversation) {
  const turnsByTask = new Map();
  for (const turn of conversation?.turnCalls?.values() || []) {
    if (!turn?.task_id) continue;
    if (!turnsByTask.has(turn.task_id)) turnsByTask.set(turn.task_id, []);
    turnsByTask.get(turn.task_id).push(turn);
  }
  return (conversation?.messages || [])
    .filter(message => message.role === 'task' && message.task)
    .map(message => {
      const task = message.task;
      const progress = new Map();
      const add = (row, fallback) => {
        const sequence = Number(row.sequence || 0);
        const kind = row.progressData?.kind || row.progressData?.type || '';
        progress.set(sequence > 0 ? `${sequence}:${kind}` : fallback, row);
      };
      for (const turn of turnsByTask.get(task.taskId) || []) {
        (turn.progress || []).forEach((item, index) => add({
          sequence: Number(item.sequence || 0),
          text: describeProgress(item), progressData: item,
        }, `${turn.turn_id}:${index}`));
      }
      (task.progress || []).forEach((row, index) => add(row, `live:${index}`));
      return {...task, progress: [...progress.values()].sort((a, b) => a.sequence - b.sequence)};
    });
}

function renderTracePane(conversation) {
  const tracePane = document.getElementById('trace-pane');
  if (!tracePane) return;
  if (!conversation) {
    tracePane.innerHTML = '';
    return;
  }
  // The conversation stores Task messages and durable per-Turn progress.
  // The timeline consumes a derived view, never a second conversation.tasks cache.
  renderTraceView(tracePane, {tasks: timelineTasksOf(conversation)});
}

const 风险文案 = { R0: '无副作用', R1: '低风险', R2: '需授权', R3: '高风险' };

function taskStateClass(task) {
  const state = task?.projection?.display_status || task?.phase1State;
  if (state === 'completed' || state === 'DONE') return 'done';
  if (state === 'failed' || state === 'cancelled' || state === 'FAILED' || state === 'CANCELLED') return 'failed';
  if (state === 'waiting' || state === 'interrupted' || state === 'WAITING' || state === 'INTERRUPTED') return 'waiting';
  if (state === 'needs_review' || state === 'NEEDS_REVIEW') return 'waiting';
  return 'running';
}

function applyTaskProjection(task, projection) {
  if (!task || !projection) return;
  task.projection = projection;
  task.traceCursor = Number(projection.trace_cursor || 0);
  const display = projection.display_status;
  task.phase1State = {
    pending: 'PREPARING', running: 'RUNNING', waiting: 'WAITING',
    interrupted: 'INTERRUPTED', failed: 'FAILED',
    cancelled: 'CANCELLED', completed: 'DONE', needs_review: 'NEEDS_REVIEW',
  }[display] || task.phase1State;
  task.status = {
    waiting_approval: 'awaiting_approval',
    waiting_input: 'awaiting_user',
    interrupted: 'interrupted',
    stopped: display === 'completed' ? 'completed' : 'failed',
    running: 'running',
  }[projection.execution_status] || task.status;
  // A terminal snapshot is authoritative. Never let an old waiting/progress
  // record keep an already failed/completed card looking active.
  if (['failed', 'cancelled', 'completed'].includes(display)) {
    task.waiting = null;
  }
}

function applyTaskProtocol(task, result) {
  if (!task || !result) return;
  const projection = result.projection || task.projection || {};
  task.protocol = {
    latestAnswerEventRef: result.latest_answer_event_ref
      ?? projection.latest_answer_event_ref ?? null,
    conclusionClaims: result.conclusion_claims
      ?? projection.conclusion_claims ?? [],
    conclusionValidation: result.conclusion_validation
      ?? projection.conclusion_validation ?? null,
  };
}

function taskProtocol(task) {
  const protocol = task?.protocol || {};
  const projection = taskProjection(task) || {};
  return {
    latestAnswerEventRef: protocol.latestAnswerEventRef
      ?? projection.latest_answer_event_ref ?? null,
    conclusionClaims: protocol.conclusionClaims
      ?? projection.conclusion_claims ?? [],
    conclusionValidation: protocol.conclusionValidation
      ?? projection.conclusion_validation ?? null,
  };
}

function taskProjection(task) {
  return task?.projection || null;
}

function taskDisplayLabel(task) {
  const projection = taskProjection(task);
  if (projection) {
    return {
      pending: '准备中', running: '执行中', waiting: (
        projection.execution_status === 'waiting_approval' ? '等待授权' : '等待输入'
      ), interrupted: '已中断', failed: '已失败',
      cancelled: '已取消', completed: '已完成', needs_review: '待人工复核',
    }[projection.display_status] || projection.display_status;
  }
  if (task?.phase1State === 'WAITING') return taskWaitingLabel(task);
  return 状态文案[task?.phase1State] || task?.phase1State || '准备中';
}

function taskWaitingLabel(task) {
  const projection = taskProjection(task);
  if (projection?.execution_status === 'waiting_approval') return '等待授权';
  if (projection?.execution_status === 'waiting_input') return '等待输入';
  if (task?.waiting?.kind === 'APPROVAL' || task?.status === 'awaiting_approval') return '等待授权';
  if (task?.status === 'awaiting_input') return '等待下一条消息';
  if (task?.waiting || task?.status === 'awaiting_user') return '等待输入';
  return '持续执行';
}

function taskPhaseLabel(task) {
  const phase = taskProjection(task)?.phase;
  if (!phase) return taskDisplayLabel(task);
  return {
    preparing: '准备阶段', executing: '执行阶段',
    verifying: '验证阶段', finalizing: '收尾阶段',
  }[phase] || phase;
}

function taskExecutionLabel(task) {
  const projection = taskProjection(task);
  if (!projection) return taskWaitingLabel(task);
  return {
    running: '持续执行', waiting_approval: '等待授权',
    waiting_input: '等待输入', interrupted: '已中断', stopped: '已停止',
  }[projection.execution_status] || projection.execution_status;
}

function traceStateForProgress(progress) {
  const kind = progress?.kind || '';
  if (kind === 'tool_completed') {
    return progress.ok === false || progress.error_code ? 'failed' : 'done';
  }
  if (kind === 'model_completed') return 'done';
  if (kind === 'waiting') return 'waiting';
  if (kind === 'tool_started' || kind === 'model_started') return 'running';
  return progress?.ok === false ? 'failed' : 'running';
}

// 右侧“执行轨迹”：按 deepagents streaming 的三类流事件（messages / updates / custom）
// 组织成一条时间线。事件顺序取自持久化进度事件的 sequence，跨 Task 依次串联。
const TIMELINE_KIND_TEXT = {
  task: '任务',
  model: '模型',
  tool: '工具',
  approval: '授权',
  input: '输入',
  commentary: '说明',
  result: '结论',
  event: '事件',
};
const TIMELINE_STATE_TEXT = {
  running: '执行中',
  waiting: '等待',
  done: '完成',
  failed: '失败',
};

function timelineKindOf(kind, progress) {
  const name = String(kind || '');
  if (/tool/i.test(name)) return 'tool';
  if (/model|assistant|message|token/i.test(name)) return 'model';
  if (/approval|permission|consent/i.test(name)) return 'approval';
  if (/input|question|waiting/i.test(name)) return 'input';
  if (/commentary|narration|progress_note/i.test(name)) return 'commentary';
  if (/result|conclusion|answer|final/i.test(name)) return 'result';
  if (progress && (progress.tool_name || progress.toolName)) return 'tool';
  return 'event';
}

function timelineStateOf(kind, progress) {
  const name = String(kind || '');
  const status = String((progress && (progress.status || progress.state)) || '');
  const probe = name + ' ' + status;
  if (/fail|error|cancel|denied|reject|timeout|abort/i.test(probe)) return 'failed';
  if (progress && progress.ok === false) return 'failed';
  if (progress && progress.error_code) return 'failed';
  if (/waiting|approval|input_request|question/i.test(name)) return 'waiting';
  if (/start|begin|running|delta/i.test(name)) return 'running';
  return 'done';
}

function timelineTaskStateOf(task) {
  const value = String(
    (task && task.projection && task.projection.display_status) || (task && task.status) || '',
  ).toLowerCase();
  if (/fail|error|interrupt|timeout/.test(value)) return { state: 'failed', text: '失败' };
  if (/cancel|stop|paused/.test(value)) return { state: 'failed', text: '已停止' };
  if (/done|complete|success|finished/.test(value)) return { state: 'done', text: '已完成' };
  if (/wait|approval|input/.test(value)) return { state: 'waiting', text: '等待中' };
  if (/run|active|progress|executing|working/.test(value)) return { state: 'running', text: '执行中' };
  return { state: 'done', text: '已结束' };
}

function timelineEvent(patch) {
  return {
    key: patch.key,
    kind: patch.kind,
    label: TIMELINE_KIND_TEXT[patch.kind] || TIMELINE_KIND_TEXT.event,
    title: patch.title,
    body: patch.body || '',
    state: patch.state,
    stateText: patch.stateText || '',
    sequence: Number(patch.sequence || 0),
  };
}

function timelineEventsOfTask(task) {
  const events = [];
  const taskId = String((task && (task.taskId || task.task_id)) || '');
  const goal = String((task && task.goal) || '').trim();
  const taskState = timelineTaskStateOf(task);
  events.push(timelineEvent({
    key: taskId + ':start',
    kind: 'task',
    title: goal || (taskId ? '任务 ' + taskId.slice(0, 8) : '任务'),
    body: taskId ? 'task ' + taskId : '',
    state: taskState.state,
    sequence: Number.MIN_SAFE_INTEGER,
  }));
  const progressItems = task && Array.isArray(task.progress) ? task.progress : [];
  progressItems.forEach((item) => {
    const progress = (item && item.progressData) || {};
    const rawKind = String(progress.kind || progress.type || '');
    if (/^waiting$/i.test(rawKind)) return;
    const kind = timelineKindOf(rawKind, progress);
    const toolName = String(progress.tool_name || progress.toolName || progress.operation || '').trim();
    const phase = /start/i.test(rawKind) ? '开始' : (/complet|done|finish|end/i.test(rawKind) ? '结束' : '');
    const text = String((item && item.text) || progress.activity || '').trim();
    events.push(timelineEvent({
      key: taskId + ':' + String((item && item.sequence) || 0) + ':' + rawKind,
      kind,
      title: TIMELINE_KIND_TEXT[kind] + phase + (toolName ? ' · ' + toolName : ''),
      body: text,
      state: timelineStateOf(rawKind, progress),
      sequence: Number((item && item.sequence) || 0),
    }));
  });
  const waiting = task && task.waiting;
  if (waiting) {
    const kind = String(waiting.kind || '').toUpperCase() === 'APPROVAL' ? 'approval' : 'input';
    const approval = waiting.approval || {};
    events.push(timelineEvent({
      key: taskId + ':waiting',
      kind,
      title: kind === 'approval' ? '等待用户授权' : '等待用户输入',
      body: String(approval.action || waiting.question || '任务已暂停，等待用户操作。'),
      state: 'waiting',
      sequence: Number.MAX_SAFE_INTEGER - 1,
    }));
  }
  if (taskState.state !== 'running') {
    events.push(timelineEvent({
      key: taskId + ':end',
      kind: 'task',
      title: '任务结束',
      body: taskState.text,
      state: taskState.state,
      stateText: taskState.text,
      sequence: Number.MAX_SAFE_INTEGER,
    }));
  }
  return events.sort((left, right) => left.sequence - right.sequence);
}

function buildTimelineEvents(tasks) {
  const seen = new Set();
  const merged = [];
  (Array.isArray(tasks) ? tasks : []).forEach((task) => {
    timelineEventsOfTask(task).forEach((event) => {
      if (seen.has(event.key)) return;
      seen.add(event.key);
      merged.push(event);
    });
  });
  return merged;
}

function renderTimelineEvent(event) {
  const item = document.createElement('li');
  item.className = 'trace-event trace-event-' + event.state;
  item.dataset.eventKey = event.key;
  const card = document.createElement('div');
  card.className = 'trace-event-card';
  const head = document.createElement('div');
  head.className = 'trace-event-head';
  const label = document.createElement('span');
  label.className = 'trace-event-kind kind-' + event.kind;
  label.textContent = event.label;
  const title = document.createElement('span');
  title.className = 'trace-event-title';
  title.textContent = event.title;
  title.title = event.title;
  const state = document.createElement('span');
  state.className = 'trace-event-state state-' + event.state;
  state.textContent = event.stateText || TIMELINE_STATE_TEXT[event.state] || '';
  head.append(label, title, state);
  card.append(head);
  if (event.body) {
    const body = document.createElement('div');
    body.className = 'trace-event-body clamped';
    body.textContent = event.body;
    card.append(body);
  }
  item.append(card);
  return item;
}

function renderTimelineTodo(tracePane, tasks) {
  for (let index = tasks.length - 1; index >= 0; index -= 1) {
    const panel = renderTodoPanel(tasks[index] && tasks[index].todoList);
    if (panel) {
      tracePane.append(panel);
      return;
    }
  }
}

function renderTimelineTopology(tracePane) {
  const details = document.createElement('details');
  details.className = 'trace-timeline-tools';
  const summary = document.createElement('summary');
  summary.textContent = '节点与链路分析';
  details.append(summary);
  const panel = document.createElement('div');
  panel.className = 'trace-overview-panel';
  const heading = document.createElement('div');
  heading.className = 'trace-panel-heading';
  const title = document.createElement('div');
  title.className = 'trace-panel-title';
  title.textContent = '节点拓扑 / 链路追溯';
  const caption = document.createElement('div');
  caption.className = 'trace-panel-caption';
  caption.textContent = '规划中';
  heading.append(title, caption);
  const note = document.createElement('div');
  note.className = 'trace-panel-caption';
  note.textContent = '后续按节点图展示模型 / 工具 / 子任务之间的调用链路，并支持由事件反查输入输出。';
  panel.append(heading, note);
  details.append(panel);
  tracePane.append(details);
}

function renderTraceView(tracePane, conversation) {
  if (!tracePane) return;
  const tasks = Array.isArray(conversation && conversation.tasks) ? conversation.tasks : [];
  const events = buildTimelineEvents(tasks);
  const states = tasks.map((task) => timelineTaskStateOf(task).state);
  const running = states.filter((state) => state === 'running').length;
  const waiting = states.filter((state) => state === 'waiting').length;
  tracePane.replaceChildren();

  const head = document.createElement('div');
  head.className = 'trace-timeline-head';
  const heading = document.createElement('div');
  heading.className = 'trace-timeline-heading';
  heading.textContent = '执行轨迹';
  const summary = document.createElement('div');
  summary.className = 'trace-timeline-caption';
  const parts = [events.length + ' 个事件'];
  if (tasks.length) parts.push(tasks.length + ' 个任务');
  if (running) parts.push(running + ' 个执行中');
  if (waiting) parts.push(waiting + ' 个等待操作');
  summary.textContent = parts.join(' · ');
  head.append(heading, summary);
  tracePane.append(head);

  if (!events.length) {
    const empty = document.createElement('div');
    empty.className = 'trace-timeline-caption';
    empty.textContent = '暂无执行事件；发送消息后，模型、工具与任务状态会按时间顺序出现在这里。';
    tracePane.append(empty);
  } else {
    const list = document.createElement('ol');
    list.className = 'trace-timeline-list';
    list.setAttribute('aria-label', '执行轨迹时间线');
    events.forEach((event) => list.append(renderTimelineEvent(event)));
    tracePane.append(list);
  }

  if (running || waiting) {
    const hint = document.createElement('div');
    hint.className = 'trace-timeline-caption';
    hint.textContent = waiting
      ? '任务在等待你的确认或补充输入。'
      : '流式推送中，新事件到达后会追加到时间线末尾。';
    tracePane.append(hint);
  }

  renderTimelineTodo(tracePane, tasks);
  if (tasks.length) renderTimelineTopology(tracePane);
  tracePane.append(renderSourceEvaluationRunner());
  tracePane.append(renderSkillInstallButton());
}

let sourceEvaluationRunId = '';
let sourceEvaluationPoll = null;

function percent(value) {
  return typeof value === 'number' ? `${Math.round(value * 100)}%` : '—';
}

function renderSourceEvaluationRunner() {
  const panel = document.createElement('section');
  panel.className = 'trace-overview-panel';
  panel.id = 'source-evaluation-runner';
  panel.innerHTML = `
    <div class='trace-panel-heading'><div><div class='trace-panel-title'>源码行为评测</div><div class='trace-panel-caption'>真实模型样例 · 独立 Session · 不影响当前会话</div></div></div>
    <button class='new-session-btn' id='start-source-evaluation' ${activeWorkspaceId ? '' : 'disabled'}>启动 2 个源码取证样例</button>
    <div class='trace-panel-caption' id='source-evaluation-status'>不会自动开启 tool_choice=required。</div>`;
  panel.querySelector('#start-source-evaluation')?.addEventListener('click', startSourceEvaluation);
  return panel;
}

function renderSkillInstallButton() {
  const panel = document.createElement('section');
  panel.className = 'trace-overview-panel';
  panel.id = 'skill-install-runner';
  panel.innerHTML = `
    <div class='trace-panel-heading'><div><div class='trace-panel-title'>Skill 安装</div><div class='trace-panel-caption'>用户级别 · 所有项目和会话可用</div></div></div>
    <button class='new-session-btn' id='install-plan-skill' data-skill-install ${skillInstallInFlight ? 'disabled' : ''}>从本地目录安装 PLAN Skill</button>
    <input type='file' id='skill-dir-input' webkitdirectory style='display:none' />
    <div class='trace-panel-caption' id='skill-install-status' role='status'>保存到 ~/.tsm-agent/skills，自动在最近工作区新建安装会话并验证准备情况。</div>`;

  const button = panel.querySelector('#install-plan-skill');
  const input = panel.querySelector('#skill-dir-input');

  button?.addEventListener('click', () => {
    if (!input) return;
    input.value = '';
    input.click();
  });

  input?.addEventListener('change', async (event) => {
    const files = event.target?.files;
    if (!files || files.length === 0) {
      return;
    }
    await uploadSkillDirectoryAndInstall(files, event.target);
  });

  return panel;
}

let skillInstallInFlight = false;
let skillSettingsDirectories = [];
let skillDefaultDirectory = '';
let disabledBuiltinSkills = [];
const skillInstallations = new Map();
const skillInstallationStreams = new Map();
const skillInstallationAutoOpen = new Set();

function renderSkillInstallations(jobs = []) {
  for (const job of jobs) skillInstallations.set(job.id, job);
  const panel = document.getElementById('skill-installations');
  if (!panel) return;
  panel.replaceChildren();
  const labels = {
    pending: '文件已保存，准备待启动', starting: '正在创建安装会话', running: '正在准备和验证',
    ready: '安装成功，验证通过', blocked: '准备受阻', failed: '准备失败',
    awaiting_approval: '等待审批', awaiting_user: '等待补充信息',
    unverified: '尚未完成验证', interrupted: '准备已中断', needs_review: '需要复核',
  };
  for (const job of [...skillInstallations.values()].slice(-30).reverse()) {
    const row = document.createElement('div');
    row.style.cssText = 'border:1px solid #e2e8f0;border-radius:10px;padding:10px;margin-top:10px;';
    const text = document.createElement('p');
    text.textContent = `${job.skill_name} · ${labels[job.status] || job.status}：${job.summary}`;
    row.append(text);
    if (job.workspace_path) {
      const workspace = document.createElement('p');
      workspace.textContent = `安装会话工作区：${job.workspace_path}`;
      row.append(workspace);
    }
    for (const check of job.checks || []) {
      const item = document.createElement('p');
      item.textContent = check;
      row.append(item);
    }
    if (job.session_id && job.task_id) {
      const open = document.createElement('button');
      open.textContent = '打开安装会话';
      open.onclick = () => openSkillInstallationConversation(job);
      row.append(open);
    }
    if (['pending', 'failed', 'interrupted', 'unverified', 'needs_review'].includes(job.status)) {
      const retry = document.createElement('button');
      retry.textContent = '重试准备';
      retry.onclick = async () => {
        retry.disabled = true;
        try {
          const next = await readSkillResponse(await fetch(`/skills/installations/${encodeURIComponent(job.id)}/retry`, {method: 'POST'}), '启动准备失败');
          skillInstallationAutoOpen.add(next.id);
          renderSkillInstallations([next]);
        } catch (error) { setSkillInstallFeedback(error.message, true); }
        finally { retry.disabled = false; }
      };
      row.append(retry);
    }
    panel.append(row);
    if (skillInstallationAutoOpen.has(job.id) && job.task_id) {
      skillInstallationAutoOpen.delete(job.id);
      openSkillInstallationConversation(job).catch(error => setSkillInstallFeedback(`文件已保存；打开安装会话失败：${error.message}`));
    }
    if (['starting', 'running', 'awaiting_approval', 'awaiting_user'].includes(job.status)) {
      watchSkillInstallation(job.id);
    } else if (skillInstallationStreams.has(job.id)) {
      skillInstallationStreams.get(job.id).close();
      skillInstallationStreams.delete(job.id);
    }
  }
}

function watchSkillInstallation(id) {
  if (skillInstallationStreams.has(id)) return;
  const source = new EventSource(`/skills/installations/${encodeURIComponent(id)}/events`);
  skillInstallationStreams.set(id, source);
  source.onmessage = event => {
    if (skillInstallationStreams.get(id) !== source) return;
    try {
      const packet = JSON.parse(event.data);
      if (!packet.installation) return;
      renderSkillInstallations([packet.installation]);
      if (!packet.watching) {
        source.close();
        skillInstallationStreams.delete(id);
      }
    } catch (error) { console.error(error); }
  };
  source.onerror = () => {
    if (skillInstallationStreams.get(id) === source) {
      setSkillInstallFeedback('安装进度连接中断，正在重连；任务仍在后台运行。');
    }
  };
}

async function openSkillInstallationConversation(job) {
  if (!job.session_id || !job.task_id) return;
  persistActiveDraft();
  if (!workspaces.some(item => item.id === job.workspace_id)) {
    workspaces.push({id: job.workspace_id, name: job.workspace_path, path: job.workspace_path});
  }
  if (!getConversationById(job.session_id)) {
    conversations.push({id: job.session_id, workspaceId: job.workspace_id,
      title: `安装 Skill：${job.skill_name}`, createdAt: new Date(job.created_at).toLocaleTimeString(),
      messages: [], draft: '', loaded: false, latestTaskId: job.task_id});
  }
  activeWorkspaceId = job.workspace_id;
  writeBrowserStorage('localStorage', ACTIVE_WORKSPACE_STORAGE_KEY, job.workspace_id);
  activeConversationId = job.session_id;
  rememberWorkspaceUse(job.workspace_id);
  document.getElementById('skill-settings-dialog')?.close();
  updateWorkspaceState();
  renderConversations();
  renderMessages();
  restoreConversationDraft();
  const conversation = getConversationById(job.session_id);
  conversation.loaded = false;
  await ensureConversationLoaded(job.session_id);
}

const SETTINGS_TAB_IDS = ['skills', 'builtin', 'directories', 'mcp'];

function showSettingsTab(name) {
  const tab = SETTINGS_TAB_IDS.includes(name) ? name : 'skills';
  for (const key of SETTINGS_TAB_IDS) {
    const active = key === tab;
    const navItem = document.getElementById(`settings-nav-${key}`);
    if (navItem) {
      navItem.classList.toggle('active', active);
      navItem.setAttribute('aria-selected', active ? 'true' : 'false');
    }
    const panel = document.getElementById(`settings-panel-${key}`);
    if (panel) panel.classList.toggle('active', active);
  }
  const content = document.getElementById('settings-content');
  if (content) content.scrollTop = 0;
}

function refreshSettingsCounts(panelName) {
  const panel = document.getElementById(`settings-panel-${panelName}`);
  const badge = document.getElementById(`settings-tab-count-${panelName}`);
  if (!panel || !badge) return;
  const items = Array.from(panel.querySelectorAll('[data-settings-card]'));
  const shown = items.filter(item => item.style.display !== 'none').length;
  badge.textContent = shown === items.length ? String(items.length) : `${shown}/${items.length}`;
}

function refreshAllSettingsCounts() {
  for (const key of SETTINGS_TAB_IDS) refreshSettingsCounts(key);
}

function filterSettingsCards(panelName, query) {
  const panel = document.getElementById(`settings-panel-${panelName}`);
  if (!panel) return;
  const needle = String(query || '').trim().toLowerCase();
  let shown = 0;
  for (const item of panel.querySelectorAll('[data-settings-card]')) {
    const haystack = (item.dataset.searchText || item.textContent || '').toLowerCase();
    const matched = !needle || haystack.includes(needle);
    item.style.display = matched ? '' : 'none';
    if (matched) shown += 1;
  }
  const empty = document.getElementById(`settings-empty-${panelName}`);
  if (empty) empty.style.display = shown ? 'none' : '';
  refreshSettingsCounts(panelName);
}

function settingsTag(text, tone) {
  const tag = document.createElement('span');
  tag.className = tone ? `settings-tag ${tone}` : 'settings-tag';
  tag.textContent = text;
  return tag;
}

function settingsCard({glyph, title, sub, description, tags = [], meta, actions = [], searchText}) {
  const card = document.createElement('article');
  card.className = 'settings-card';
  card.dataset.settingsCard = '';
  card.dataset.searchText = searchText || [title, sub, description, meta].filter(Boolean).join(' ');
  const head = document.createElement('div');
  head.className = 'settings-card-head';
  const avatar = document.createElement('span');
  avatar.className = 'settings-avatar';
  avatar.textContent = glyph || String(title || '?').slice(0, 1);
  const headText = document.createElement('div');
  headText.className = 'settings-card-headtext';
  const titleEl = document.createElement('div');
  titleEl.className = 'settings-card-title';
  titleEl.textContent = title || '未命名';
  headText.append(titleEl);
  if (sub) {
    const subEl = document.createElement('div');
    subEl.className = 'settings-card-sub';
    subEl.textContent = sub;
    headText.append(subEl);
  }
  head.append(avatar, headText);
  card.append(head);
  if (description) {
    const desc = document.createElement('p');
    desc.className = 'settings-card-desc';
    desc.textContent = description;
    card.append(desc);
  }
  if (tags.length) {
    const tagWrap = document.createElement('div');
    tagWrap.className = 'settings-tags';
    for (const tag of tags) tagWrap.append(settingsTag(tag.text, tag.tone));
    card.append(tagWrap);
  }
  if (meta || actions.length) {
    const foot = document.createElement('div');
    foot.className = 'settings-card-foot';
    const metaEl = document.createElement('span');
    metaEl.className = 'settings-card-meta';
    metaEl.textContent = meta || '';
    foot.append(metaEl);
    if (actions.length) {
      const actionWrap = document.createElement('div');
      actionWrap.className = 'settings-actions';
      for (const action of actions) actionWrap.append(action);
      foot.append(actionWrap);
    }
    card.append(foot);
  }
  return card;
}

function renderSkillDirectories() {
  const list = document.getElementById('skill-discovery-list');
  if (!list) return;
  list.replaceChildren();
  for (const directory of skillSettingsDirectories) {
    const row = document.createElement('div');
    row.className = 'settings-list-row';
    row.dataset.settingsCard = '';
    row.dataset.searchText = directory;
    const path = document.createElement('span');
    path.className = 'settings-path';
    path.textContent = directory;
    row.append(path);
    if (directory === skillDefaultDirectory) {
      row.append(settingsTag('默认安装目录', 'accent'));
    } else {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'settings-btn ghost';
      remove.textContent = '移除';
      remove.onclick = () => {
        skillSettingsDirectories = skillSettingsDirectories.filter(item => item !== directory);
        renderSkillDirectories();
      };
      row.append(remove);
    }
    list.append(row);
  }
  if (!skillSettingsDirectories.length) {
    const empty = document.createElement('div');
    empty.className = 'settings-empty';
    empty.textContent = '还没有可发现 Skill 的目录。';
    list.append(empty);
  }
  refreshSettingsCounts('directories');
}

function renderSkillCatalog(data) {
  const catalog = document.getElementById('skill-discovered-catalog');
  if (catalog) {
    catalog.replaceChildren();
    const skills = data.skills || [];
    const caption = document.createElement('div');
    caption.className = 'settings-caption';
    caption.textContent = `已发现 ${skills.length} 个 Skill`;
    catalog.append(caption);
    for (const skill of skills) {
      const source = skill.source === 'builtin' ? '内置'
        : skill.source === 'user' ? '用户目录'
        : '代码注册';
      const tags = [{text: source, tone: skill.source === 'builtin' ? 'accent' : ''}];
      if (skill.overrides?.includes('builtin')) tags.push({text: '覆盖内置版本', tone: 'warn'});
      if (skill.safety_level) tags.push({text: `安全级别：${skill.safety_level}`, tone: ''});
      catalog.append(settingsCard({
        glyph: String(skill.name || '?').slice(0, 1),
        title: skill.name,
        sub: skill.plan_path || '代码注册',
        description: skill.description || '',
        tags,
        meta: source,
        searchText: [skill.name, skill.description, source, skill.plan_path].filter(Boolean).join(' '),
      }));
    }
  }
  const builtins = document.getElementById('skill-builtin-catalog');
  if (builtins) {
    builtins.replaceChildren();
    for (const skill of data.builtin_skills || []) {
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = !disabledBuiltinSkills.includes(skill.name);
      const state = document.createElement('span');
      state.textContent = checkbox.checked ? '已启用' : '已停用';
      checkbox.onchange = () => {
        disabledBuiltinSkills = disabledBuiltinSkills.filter(name => name !== skill.name);
        if (!checkbox.checked) disabledBuiltinSkills.push(skill.name);
        state.textContent = checkbox.checked ? '已启用' : '已停用';
      };
      const toggle = document.createElement('label');
      toggle.className = 'settings-switch';
      toggle.append(checkbox, state);
      builtins.append(settingsCard({
        glyph: String(skill.name || '?').slice(0, 1),
        title: skill.name,
        sub: '随程序提供',
        description: skill.description || '',
        tags: [{text: '内置工作流', tone: 'accent'}],
        meta: '保存后生效',
        actions: [toggle],
        searchText: [skill.name, skill.description, '内置'].filter(Boolean).join(' '),
      }));
    }
  }
  renderSkillInstallations(data.installations || []);
  refreshAllSettingsCounts();
}

function applySkillSettings(data) {
  disabledBuiltinSkills = data.disabled_builtin_skills || [];
  skillDefaultDirectory = data.install_directory;
  skillSettingsDirectories = data.discovery_directories || [];
  document.getElementById('skill-install-directory').textContent = data.install_directory;
  renderSkillDirectories();
  renderSkillCatalog(data);
  const installed = document.getElementById('installed-skills-list');
  if (installed) {
    installed.replaceChildren();
    for (const skill of data.skills || []) {
      const source = skill.source === 'builtin' ? '内置'
        : skill.source === 'user' ? '用户目录'
        : (skill.source || '其他');
      const tags = [{text: source, tone: skill.source === 'builtin' ? 'accent' : ''}];
      if (skill.safety_level) tags.push({text: `安全级别：${skill.safety_level}`, tone: ''});
      installed.append(settingsCard({
        glyph: String(skill.name || '?').slice(0, 1),
        title: skill.name,
        sub: skill.plan_path || '代码注册',
        description: skill.description || '',
        tags,
        meta: source,
        searchText: [skill.name, skill.description, source, skill.plan_path].filter(Boolean).join(' '),
      }));
    }
  }
  document.getElementById('skill-settings-status').textContent = (data.issues || []).join(String.fromCodePoint(10));
  refreshAllSettingsCounts();
  // 目录或启用状态变了，输入框的 Skill 联想缓存随之失效，下次触发时重新读取。
  skillMenuSkills = null;
  skillMenuError = '';
}

let mcpSettingsLoading = false;

function mcpAvailabilityBadge(server, enabled) {
  const badge = document.createElement('span');
  badge.style.cssText = 'display:inline-block;padding:0 8px;border-radius:999px;font-size:12px;margin-right:6px;';
  if (!enabled) {
    badge.textContent = '未启用';
    badge.style.background = 'var(--panel-muted)';
    badge.style.color = 'var(--muted)';
  } else if (server.available === true) {
    badge.textContent = '可用';
    badge.style.background = '#ecfdf5';
    badge.style.color = '#047857';
  } else if (server.available === false) {
    badge.textContent = '不可用';
    badge.style.background = '#fef2f2';
    badge.style.color = '#b91c1c';
  } else {
    badge.textContent = '未检测';
    badge.style.background = 'var(--panel-muted)';
    badge.style.color = 'var(--muted)';
  }
  return badge;
}

function renderMcpSettings(data) {
  const list = document.getElementById('mcp-server-list');
  if (!list) return;
  const status = document.getElementById('mcp-settings-status');
  list.replaceChildren();
  const enabled = data?.enabled === true;
  const servers = Array.isArray(data?.servers) ? data.servers : [];
  const issues = Array.isArray(data?.issues) ? data.issues : [];
  if (status) {
    const parts = [enabled ? 'mcp.enabled = true' : 'mcp.enabled = false', `共 ${servers.length} 个服务器`];
    if (data?.probed) {
      const usable = servers.filter((server) => server.available === true).length;
      parts.push(`可用 ${usable} / ${servers.length}`);
    } else {
      parts.push('未检测连通性');
    }
    if (!enabled) parts.push('配置不会注册到工具列表');
    status.textContent = [parts.join(' · '), ...issues.map(issue => `配置问题：${issue}`)].join(String.fromCodePoint(10));
  }
  for (const server of servers) {
    const spec = [`类型：${server.kind || '未指定'}`];
    if (server.command && server.command.length) spec.push(`命令：${server.command.join(' ')}`);
    else if (server.url) spec.push(`URL：${server.url}`);
    else if (server.module) spec.push(`模块：${server.module}`);
    if (server.option_keys && server.option_keys.length) spec.push(`options：${server.option_keys.join(', ')}`);
    const tools = Array.isArray(server.tools) ? server.tools : [];
    const availability = !enabled ? {text: '未启用', tone: ''}
      : server.available === true ? {text: '可用', tone: 'ok'}
      : server.available === false ? {text: '不可用', tone: 'bad'}
      : {text: '未检测', tone: 'warn'};
    const tags = [availability, {text: server.kind || '未指定类型', tone: 'accent'}];
    if (tools.length) tags.push({text: `${tools.length} 个工具`, tone: ''});
    list.append(settingsCard({
      glyph: String(server.id || '?').slice(0, 1),
      title: server.id,
      sub: spec.slice(1).join(' · ') || '未配置启动方式',
      description: [server.detail, tools.join(', ')].filter(Boolean).join('：'),
      tags,
      meta: spec[0],
      searchText: [server.id, server.kind, (server.command || []).join(' '), server.url, server.module, server.detail, tools.join(' ')]
        .filter(Boolean).join(' '),
    }));
  }
  const empty = document.getElementById('settings-empty-mcp');
  if (empty) empty.style.display = servers.length ? 'none' : '';
  refreshAllSettingsCounts();
}

async function loadMcpSettings(probe) {
  if (mcpSettingsLoading) return;
  const status = document.getElementById('mcp-settings-status');
  mcpSettingsLoading = true;
  if (status) status.textContent = probe ? '正在检测 MCP 服务器连通性…' : '正在读取 MCP 配置…';
  try {
    const response = await fetch(`/settings/mcp?probe=${probe ? 'true' : 'false'}`);
    const data = await readSkillResponse(response, '读取 MCP 设置失败');
    renderMcpSettings(data);
  } catch (error) {
    if (status) status.textContent = error.message;
  } finally {
    mcpSettingsLoading = false;
  }
}

async function readSkillResponse(response, action) {
  let data;
  try {
    data = await response.json();
  } catch (error) {
    throw new Error(`${action}：服务器返回非 JSON 响应（HTTP ${response.status}）`);
  }
  if (!response.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail || {});
    throw new Error(`${action}：${detail}（HTTP ${response.status}）`);
  }
  return data;
}

function setSkillInstallFeedback(message, failed = false) {
  const status = document.getElementById('skill-install-status');
  if (status) status.textContent = message;
  const settings = document.getElementById('skill-settings-status');
  if (settings) settings.textContent = message;
  if (failed) showError(message);
}

async function openSkillSettings() {
  const dialog = document.getElementById('skill-settings-dialog');
  if (!dialog.open) dialog.showModal();
  try {
    const response = await fetch('/settings/skills');
    const data = await readSkillResponse(response, '读取 Skills 设置失败');
    applySkillSettings(data);
  } catch (error) {
    document.getElementById('skill-settings-status').textContent = error.message;
  }
  // MCP 设置是只读展示，单独读取失败不影响 Skill 设置的使用；probe=true 会做真实连通性探测。
  await loadMcpSettings(true);
}

function addSkillDirectoryPath() {
  const input = document.getElementById('skill-directory-path');
  const path = input.value.trim();
  if (path && !skillSettingsDirectories.includes(path)) skillSettingsDirectories.push(path);
  input.value = '';
  renderSkillDirectories();
}

async function pickSkillDirectories() {
  try {
    const response = await fetch('/settings/skills/pick', {method: 'POST'});
    const data = await readSkillResponse(response, '选择目录失败');
    skillSettingsDirectories = [...new Set([...skillSettingsDirectories, ...(data.paths || [])])];
    renderSkillDirectories();
  } catch (error) {
    document.getElementById('skill-settings-status').textContent = error.message;
  }
}

async function saveSkillSettings() {
  const button = document.getElementById('save-skill-settings');
  button.disabled = true;
  try {
    addSkillDirectoryPath();
    const response = await fetch('/settings/skills', {
      method: 'PUT', headers: {'content-type': 'application/json'},
      body: JSON.stringify({discovery_directories: skillSettingsDirectories.filter(path => path !== skillDefaultDirectory), disabled_builtin_skills: disabledBuiltinSkills}),
    });
    const data = await readSkillResponse(response, '保存设置失败');
    applySkillSettings(data);
    const status = document.getElementById('skill-settings-status');
    status.textContent = '已保存，后续对话请求使用刷新后的 Skill 目录。' + (status.textContent ? String.fromCharCode(10) + status.textContent : '');
  } catch (error) {
    document.getElementById('skill-settings-status').textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function uploadSkillDirectoryAndInstall(fileList, input = null) {
  const files = Array.from(fileList || []).filter(file => !(
    (file.webkitRelativePath || file.name).split('/').some(part => (
      ['.DS_Store', '__MACOSX', '.git', 'Thumbs.db', 'desktop.ini'].includes(part)
    ))
  ));
  if (!files.length || skillInstallInFlight) return;
  skillInstallInFlight = true;
  document.querySelectorAll('[data-skill-install]').forEach(button => { button.disabled = true; });
  clearError();
  setSkillInstallFeedback('正在安装到用户 Skill 目录…');
  try {
    const formData = new FormData();
    files.forEach(file => formData.append('files', file, file.webkitRelativePath || file.name));
    if (typeof activeWorkspaceId !== 'undefined' && activeWorkspaceId) formData.append('workspace_id', activeWorkspaceId);
    let response;
    try {
      response = await fetch('/skills/upload', {method: 'POST', body: formData});
    } catch (error) {
      throw new Error('安装请求中断，结果尚未确认；可以重新选择同一目录重试，相同文件不会重复安装。');
    }
    const data = await readSkillResponse(response, '安装 Skill 失败');
    if (data?.installed !== true || !data.skill?.name || !data.skill?.plan_path) {
      throw new Error('安装响应缺少结果信息，请在设置中检查安装状态后重试。');
    }
    const message = data.installation
      ? `文件已保存 ${data.skill.name}：${data.skill.plan_path}。${data.installation.summary}`
      : data.preparation_required
        ? `文件已保存 ${data.skill.name}：${data.skill.plan_path}。安装准备未启动。`
        : `已安装 ${data.skill.name}：${data.skill.plan_path}`;
    const warnings = Array.isArray(data.warnings) ? [...data.warnings] : [];
    // The upload has committed. A later catalog refresh failure must not be
    // presented as a failed installation or lose the successful receipt.
    if (document.getElementById('skill-settings-dialog')?.open) {
      try {
        const refreshed = await readSkillResponse(await fetch('/settings/skills'), '刷新目录失败');
        renderSkillCatalog(refreshed);
        warnings.push(...(refreshed.issues || []));
      } catch (error) {
        warnings.push(`安装已完成，但目录列表刷新失败：${error.message}`);
      }
    }
    setSkillInstallFeedback([message, ...new Set(warnings)].join(String.fromCodePoint(10)));
    if (data.installation) {
      skillInstallationAutoOpen.add(data.installation.id);
      renderSkillInstallations([data.installation]);
    }
  } catch (error) {
    setSkillInstallFeedback(error.message || '安装 Skill 失败', true);
  } finally {
    skillInstallInFlight = false;
    document.querySelectorAll('[data-skill-install]').forEach(button => { button.disabled = false; });
    if (input) input.value = '';
  }
}

async function startSourceEvaluation() {
  if (!activeWorkspaceId) return;
  const button = document.getElementById('start-source-evaluation');
  if (button) button.disabled = true;
  try {
    const response = await fetch('/evaluations/source-analysis/runs', {
      method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({workspace_id: activeWorkspaceId}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '启动评测失败');
    sourceEvaluationRunId = data.run_id;
    await refreshSourceEvaluation();
    if (sourceEvaluationPoll) clearInterval(sourceEvaluationPoll);
    sourceEvaluationPoll = setInterval(refreshSourceEvaluation, 2500);
  } catch (error) {
    showError(error.message || '启动评测失败');
    if (button) button.disabled = false;
  }
}

async function refreshSourceEvaluation() {
  if (!sourceEvaluationRunId) return;
  try {
    const response = await fetch(`/evaluations/source-analysis/runs/${encodeURIComponent(sourceEvaluationRunId)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '读取评测失败');
    const status = document.getElementById('source-evaluation-status');
    if (!status) return;
    const metrics = data.metrics || {};
    const cases = (data.cases || []).map((item) => `${escapeHtml(item.title)}：${escapeHtml(item.status)} / ${item.source_read ? '已读源码' : '未读源码'}`).join('<br>');
    status.innerHTML = `<b>${escapeHtml(data.status || 'starting')}</b> · 样本 ${metrics.sample_count || 0}<br>首轮零工具 ${percent(metrics.first_turn_zero_tool_rate)} · 读源码 ${percent(metrics.source_read_rate)} · 仅搜索 ${percent(metrics.search_only_rate)} · Review 介入 ${percent(metrics.review_intervention_rate)} · 通过 ${percent(metrics.pass_rate)}${cases ? `<br>${cases}` : ''}`;
    if (data.status === 'completed' && sourceEvaluationPoll) {
      clearInterval(sourceEvaluationPoll); sourceEvaluationPoll = null;
      const button = document.getElementById('start-source-evaluation');
      if (button) button.disabled = false;
    }
  } catch (error) {
    const status = document.getElementById('source-evaluation-status');
    if (status) status.textContent = error.message || '读取评测失败';
  }
}

function isMarkdownProgress(text) {
  return String(text || '')
    .split(String.fromCharCode(10))
    .some((line) => {
      const normalized = line.trim();
      return /^#{1,3} /.test(normalized)
        || normalized.startsWith('- ')
        || normalized.startsWith('* ')
        || normalized === '---'
        || normalized.startsWith('```');
    });
}

function renderConversationTodo(conversation) {
  const dock = document.getElementById('todo-dock');
  if (!dock) return;
  // Use only the current Task. Never resurrect an older unfinished list when
  // the latest Task has no plan, or when its plan has been completed/cleared.
  const messages = conversation?.messages || [];
  const current = messages.find(message => message.task
    && message.task.taskId === conversation?.activeTaskId)
    || [...messages].reverse().find(message => message.task);
  const todoList = current?.task?.todoList;
  const todos = Array.isArray(todoList?.todos) ? todoList.todos : [];
  const visible = todos.length > 0 && !todos.every(todo => todo.status === 'completed');
  dock.replaceChildren();
  dock.hidden = !visible;
  if (visible) dock.appendChild(renderTodoPanel(todoList, true));
}

function renderTodoPanel(todoList, compact = false) {
  const todos = Array.isArray(todoList?.todos) ? todoList.todos : [];
  if (todos.length === 0) return null;
  const panel = document.createElement('section');
  panel.className = 'todo-panel';
  const header = document.createElement('div');
  header.className = 'todo-panel-header';
  const title = document.createElement('div');
  title.className = 'todo-panel-title';
  title.textContent = compact ? '任务清单' : '执行步骤';
  const summary = document.createElement('div');
  summary.className = 'todo-panel-summary';
  const completed = todos.filter(todo => todo.status === 'completed').length;
  const total = todos.length;
  summary.textContent = compact ? `${completed}/${total} 已完成`
    : `${completed}/${total} 已完成 · rev ${Number(todoList.revision || 0)}`;
  header.append(title, summary);
  panel.appendChild(header);

  const list = document.createElement('div');
  list.className = 'todo-list';
  const markers = {
    pending: '○', in_progress: '▶', completed: '✓', blocked: '!',
  };
  todos.forEach((todo) => {
    const status = ['pending', 'in_progress', 'completed', 'blocked'].includes(
      String(todo?.status),
    ) ? String(todo.status) : 'pending';
    const item = document.createElement('div');
    item.className = `todo-item ${status}`;
    const marker = document.createElement('div');
    marker.className = 'todo-marker';
    marker.textContent = markers[status];
    marker.setAttribute('role', 'checkbox');
    marker.setAttribute('aria-checked', String(status === 'completed'));
    marker.setAttribute('aria-label', `${String(todo?.content || todo?.id || '未命名步骤')}：${({pending: '待处理', in_progress: '进行中', completed: '已完成', blocked: '受阻'})[status]}`);
    const body = document.createElement('div');
    const content = document.createElement('div');
    content.className = 'todo-content';
    content.textContent = String(todo?.content || todo?.id || '未命名步骤');
    body.appendChild(content);
    const criteriaText = String(todo?.completion_criteria || '').trim();
    if (!compact && criteriaText && criteriaText !== content.textContent) {
      const criteria = document.createElement('div');
      criteria.className = 'todo-criteria';
      criteria.textContent = `完成条件：${criteriaText}`;
      body.appendChild(criteria);
    }
    item.append(marker, body);
    list.appendChild(item);
  });
  panel.appendChild(list);
  return panel;
}

function renderTaskCard(element, message, conversationId, mode = 'conversation') {
  const task = message.task || {};
  const card = document.createElement('div');
  card.className = 'task-card';
  const header = document.createElement('div');
  header.className = 'task-card-header';
  const title = document.createElement('div');
  title.className = 'task-card-title';
  title.textContent = task.goal || `任务 ${String(task.taskId || '').slice(0, 8)}`;
  const state = document.createElement('div');
  state.className = `task-card-state ${taskStateClass(task)}`;
  state.textContent = taskDisplayLabel(task);
  header.append(title, state);
  card.appendChild(header);

  const meta = document.createElement('div');
  meta.className = 'task-card-meta';
  const projection = taskProjection(task);
  const displayStatus = projection?.display_status || task.status || 'running';
  meta.innerHTML = `
    <span class='task-phase-badge'>${displayStatus}</span>
    <span class='task-phase-badge'>${task.taskId || 'task'}</span>
  `;
  card.appendChild(meta);

  const summary = document.createElement('div');
  summary.className = 'task-card-summary';
  const progressCount = (task.progress || []).length;
  const traceCursor = Number(projection?.trace_cursor || task.traceCursor || 0);
  const displayLabel = taskPhaseLabel(task);
  const executionLabel = taskExecutionLabel(task);
  summary.innerHTML = `
    <div class='task-summary-item'><div class='task-summary-label'>当前阶段</div><div class='task-summary-value'>${displayLabel}</div></div>
    <div class='task-summary-item'><div class='task-summary-label'>持久流程事件</div><div class='task-summary-value'>${traceCursor || progressCount}</div></div>
    <div class='task-summary-item'><div class='task-summary-label'>执行状态</div><div class='task-summary-value'>${executionLabel}</div></div>
  `;
  card.appendChild(summary);

  const protocol = taskProtocol(task);
  const protocolSummary = document.createElement('div');
  protocolSummary.className = 'task-card-summary';
  const addProtocolItem = (label, value) => {
    const item = document.createElement('div');
    item.className = 'task-summary-item';
    const name = document.createElement('div');
    name.className = 'task-summary-label';
    name.textContent = label;
    const content = document.createElement('div');
    content.className = 'task-summary-value';
    content.textContent = value;
    item.append(name, content);
    protocolSummary.appendChild(item);
  };
  const claims = Array.isArray(protocol.conclusionClaims)
    ? protocol.conclusionClaims : [];
  const modelConclusion = claims.length
    ? claims.map((claim) => {
      const kind = String(claim?.kind || 'claim');
      const summaryText = String(claim?.summary || claim?.claim_id || '未提供');
      return `${kind}: ${summaryText}`;
    }).join('；')
    : '未提供';
  const validation = protocol.conclusionValidation;
  const validationStatus = validation && typeof validation === 'object'
    ? String(validation.status || '未提供') : '未提供';
  const reference = protocol.latestAnswerEventRef || '未提供';
  addProtocolItem('执行状态', executionLabel);
  // Only rendered when a verdict exists: with no domain check in the Runtime,
  // 'not_started' is the normal value and showing it reads as a stuck state.
  const verificationStatus = projection?.verification_status;
  if (verificationStatus && verificationStatus !== 'not_started') {
    addProtocolItem('领域检查', verificationStatus);
  }
  addProtocolItem('模型结论', modelConclusion);
  addProtocolItem('引用校验', `${validationStatus} · ${reference}`);
  card.appendChild(protocolSummary);

  const todoPanel = renderTodoPanel(task.todoList);
  if (todoPanel) card.appendChild(todoPanel);

  // In conversation mode this card spans every Turn, so its buffer would stack
  // all Turns' lines at the Task's first Turn -- far above the question being
  // answered. The live Turn renders its own progress at the end of the
  // conversation, and each settled Turn keeps its durable call panel under its
  // answer, so the card carries no progress panel in that mode.
  const progress = document.createElement('div');
  progress.className = 'task-progress task-panel';
  progress.classList.add('task-collapsible');
  const lines = task.progress || [];
  if (mode !== 'conversation') {
    progress.classList.add('active');
    if (lines.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'task-progress-empty';
      empty.textContent = task.phase1State === 'WAITING'
        ? '该任务正在等待处理，不会自动重放历史执行日志。'
        : '暂无新的执行进度';
      progress.appendChild(empty);
    } else {
      lines.slice(-30).forEach((item) => {
        const line = document.createElement('div');
        line.className = 'task-progress-line';
        if (isMarkdownProgress(item.text)) {
          line.classList.add('markdown-progress');
          line.innerHTML = renderMarkdown(item.text);
        } else {
          line.textContent = item.text;
        }
        progress.appendChild(line);
      });
    }
    card.appendChild(progress);
  }

  const collapsibleSections = [progress].filter((panel) => {
    if (!panel.classList.contains('active')) return false;
    return panel.scrollHeight > 260 || panel.childElementCount > 8;
  });

  collapsibleSections.forEach((panel, index) => {
    const collapseKey = `${task.taskId || 'task'}-${mode}-${panel.className}-${index}`;
    const isCollapsed = taskDetailCollapseState.has(collapseKey)
      ? taskDetailCollapseState.get(collapseKey)
      : true;

    panel.classList.toggle('collapsed', isCollapsed);

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'task-collapse-toggle';
    toggle.textContent = isCollapsed ? '展开详情' : '收起详情';
    toggle.addEventListener('click', () => {
      const collapsed = panel.classList.toggle('collapsed');
      taskDetailCollapseState.set(collapseKey, collapsed);
      toggle.textContent = collapsed ? '展开详情' : '收起详情';
    });
    card.appendChild(toggle);
  });

  // Approval actions are deliberately never embedded in a historical Task
  // card.  The only actionable copy lives in approval-dock, which is pinned to
  // the conversation pane rather than scrolling away with this card.
  if (task.waiting?.kind === 'APPROVAL') {
    const waiting = document.createElement('div');
    waiting.className = 'task-progress-line';
    waiting.textContent = task.waiting.approval?.kind === 'project_trust'
      ? '命令执行等待底部的工作区信任确认。'
      : '写入操作等待底部的授权确认。';
    card.appendChild(waiting);
  } else if (task.waiting) {
    const waiting = document.createElement('div');
    waiting.className = 'task-progress-line';
    waiting.textContent = task.waiting.question || '该任务正在等待你的输入。';
    card.appendChild(waiting);
    // Ordinary chat text is refused while a Task awaits the user, so a
    // CONTINUATION boundary needs its own explicit channel.
    if (task.waiting.kind === 'CONTINUATION') {
      const actions = document.createElement('div');
      actions.className = 'approval-actions';
      const resume = document.createElement('button');
      resume.className = 'approval-btn approve';
      resume.textContent = '继续';
      resume.onclick = () => resumeContinuation(task.taskId, conversationId);
      actions.appendChild(resume);
      card.appendChild(actions);
    }
  }
  element.appendChild(card);
}

function taskMessage(conversationId, taskId) {
  const conversation = getConversationById(conversationId);
  return conversation?.messages.find(
    (item) => item.role === 'task' && item.task?.taskId === taskId,
  );
}

function bindLatestInputToTask(conversationId, taskId, turnId = null) {
  const conversation = getConversationById(conversationId);
  if (!conversation || !taskId) return null;
  const input = [...conversation.messages].reverse().find((item) => (
    item.role === 'user' && !item.taskId
  ));
  if (!input) return null;
  input.taskId = taskId;
  if (turnId) {
    input.turnId = turnId;
    conversation.turnCalls ||= new Map();
    if (!conversation.turnCalls.has(turnId)) {
      conversation.turnCalls.set(turnId, {
        turn_id: turnId, task_id: taskId, settled: false, progress: [],
      });
    }
    // 一旦当前输入成功绑定到具有 turnId 的 Turn，就视为“本轮已开始”。
    // 如果这是当前活动会话，则立刻根据最新的 turnCalls 视角刷新取消按钮，
    // 避免出现卡片显示“本轮进行中”但取消按钮仍然隐藏的窗口期。
    if (conversationId === activeConversationId) {
      updateComposerButtonState();
    }
  }
  return input;
}

function upsertApprovalMessage(taskId, approval, conversationId = activeConversationId) {
  const conversation = getConversationById(conversationId);
  if (!conversation || !approval?.request_id) return null;
  let message = conversation.messages.find((item) => (
    item.role === 'approval'
    && item.taskId === taskId
    && item.approval?.request_id === approval.request_id
  ));
  if (!message) {
    // This is an operation footer, so insertion order is intentionally the end
    // of the transcript. renderMessages projects it into the fixed dock.
    message = {
      role: 'approval', content: '', taskId,
      turnId: approval.turn_id || null, approval, conversationId,
    };
    conversation.messages.push(message);
  } else {
    message.approval = approval;
    message.turnId = approval.turn_id || message.turnId || null;
  }
  return message;
}

function clearApprovalMessage(taskId, conversationId = activeConversationId) {
  const conversation = getConversationById(conversationId);
  if (!conversation) return;
  conversation.messages = conversation.messages.filter((item) => !(
    item.role === 'approval' && item.taskId === taskId
  ));
}

function syncClarificationMessage(taskId, waiting, conversationId) {
  const conversation = getConversationById(conversationId);
  if (!conversation) return;
  const requestId = waiting?.request_id;
  const existing = conversation.messages.find(item => (
    item.role === 'clarification' && item.taskId === taskId
    && item.clarification?.request_id === requestId
  ));
  conversation.messages = conversation.messages.filter(item => !(
    item.role === 'clarification' && item.taskId === taskId && item !== existing
  ));
  if (!requestId) return;
  if (existing) existing.clarification = waiting;
  else conversation.messages.push({
    role: 'clarification', content: '', taskId, conversationId, clarification: waiting,
  });
}

function renderClarificationCard(element, message) {
  element.classList.add('approval');
  const request = message.clarification;
  const title = document.createElement('div');
  title.className = 'approval-title';
  title.textContent = '需要你的回答';
  const question = document.createElement('p');
  question.textContent = request.question || '请选择下一步。';
  element.append(title, question);
  const actions = document.createElement('div');
  actions.className = 'approval-actions';
  if ((request.choices || []).length) {
    request.choices.forEach(choice => {
      const button = document.createElement('button');
      button.className = 'approval-btn';
      button.textContent = choice.label;
      button.disabled = Boolean(message.submitting);
      button.onclick = () => answerClarification(message, {selected_choice: choice.value});
      actions.appendChild(button);
    });
  } else {
    const input = document.createElement('input');
    input.placeholder = '填写回答';
    input.disabled = Boolean(message.submitting);
    const button = document.createElement('button');
    button.className = 'approval-btn';
    button.textContent = '回答并继续';
    button.disabled = Boolean(message.submitting);
    button.onclick = () => { if (input.value.trim()) answerClarification(message, {answer: input.value.trim()}); };
    actions.append(input, button);
  }
  element.appendChild(actions);
}

async function answerClarification(message, reply) {
  if (message.submitting) return;
  message.submitting = true;
  clearError();
  renderMessages();
  const conversationId = message.conversationId || activeConversationId;
  try {
    const commandId = crypto.randomUUID();
    const response = await fetch('/clarifications', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task_id: message.taskId,
        request_id: message.clarification.request_id, command_id: commandId, ...reply}),
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error((await response.json()).detail || '提交失败');
    // A 202 is only acceptance. Observe the handoff before following the Turn,
    // so its old waiting snapshot cannot stop the new follower immediately.
    while (true) {
      const poll = await fetch(`/clarifications/${encodeURIComponent(commandId)}`, {signal: AbortSignal.timeout(10000)});
      if (!poll.ok) throw new Error('恢复状态不可用，请刷新会话后重试。');
      const status = await poll.json();
      if (status.status === 'failed') throw new Error(status.detail);
      if (status.status === 'resumed') {
        syncClarificationMessage(message.taskId, null, conversationId);
        renderMessages();
        setLoading(true);
        followTask(message.taskId, conversationId);
        break;
      }
      if (status.status === 'waiting') break;
      await new Promise(resolve => setTimeout(resolve, 500));
    }
  } catch (error) {
    showError(error.message || '恢复失败，请重试。');
    await refreshConversationHistory(conversationId).catch(console.error);
  } finally {
    message.submitting = false;
    renderMessages();
  }
}

function upsertTaskCard(conversationId, rawTask) {
  const conversation = getConversationById(conversationId);
  if (!conversation || !rawTask?.task_id) return null;
  let message = taskMessage(conversationId, rawTask.task_id);
  if (!message) {
    message = {
      role: 'task', content: '',
      task: {
        taskId: rawTask.task_id,
        goal: rawTask.goal || '',
        phase1State: rawTask.phase1_state || 'PREPARING',
        status: rawTask.status || 'running',
        projection: rawTask.projection || null,
        todoList: rawTask.todo_list || null,
        traceCursor: Number(rawTask.projection?.trace_cursor || rawTask.progress_cursor || 0),
        cursor: Number(rawTask.progress_cursor || 0),
        progress: [], waiting: rawTask.waiting || null,
      },
    };
    applyTaskProjection(message.task, rawTask.projection);
    applyTaskProtocol(message.task, rawTask);
    conversation.messages.push(message);
  } else {
    message.task.goal = rawTask.goal || message.task.goal;
    message.task.phase1State = rawTask.phase1_state || message.task.phase1State;
    message.task.status = rawTask.status || message.task.status;
    message.task.cursor = Math.max(
      Number(message.task.cursor || 0), Number(rawTask.progress_cursor || 0),
    );
    message.task.waiting = rawTask.waiting ?? message.task.waiting;
    if (Object.prototype.hasOwnProperty.call(rawTask, 'todo_list')) {
      message.task.todoList = rawTask.todo_list;
    }
    applyTaskProjection(message.task, rawTask.projection);
    applyTaskProtocol(message.task, rawTask);
  }

  // 当 Runtime 报告当前轮次进入终态（final 或终态 phase1_state），将对应的 Turn 标记为已结算，
  // 以便取消按钮逻辑正确识别“没有可取消的轮次”。
  const isFinal = rawTask.final === true
    || ['INTERRUPTED', 'DONE', 'FAILED', 'CANCELLED', 'NEEDS_REVIEW', 'WAITING']
      .includes(rawTask.phase1_state || '');
  const turnId = rawTask.active_turn_id || rawTask.turn_id || message.task.streamTurnId;
  if (isFinal && turnId && conversation.turnCalls instanceof Map) {
    const turn = conversation.turnCalls.get(turnId);
    if (turn && turn.settled === false) {
      turn.settled = true;
    }
  }

  if (conversationId === activeConversationId) {
    renderMessages();
    updateComposerButtonState();
  }
  return message;
}

let progressRenderPending = false;
function scheduleProgressRender(conversationId) {
  if (conversationId !== activeConversationId || progressRenderPending) return;
  progressRenderPending = true;
  requestAnimationFrame(() => {
    progressRenderPending = false;
    renderMessages();
  });
}

function appendTaskProgress(taskId, progress, conversationId) {
  const message = taskMessage(conversationId, taskId)
    || upsertTaskCard(conversationId, { task_id: taskId, phase1_state: 'RUNNING' });
  if (!message) return;
  const sequence = Number(progress?.sequence || 0);
  message.task.cursor = Math.max(Number(message.task.cursor || 0), sequence);
  const progressData = { ...(progress?.progress || {}) };
  const text = describeProgress(progressData);
  if (!text) return;
  const lines = message.task.progress;
  if (lines.some((item) => item.sequence === sequence && sequence > 0)) return;
  // Once a durable Turn panel exists renderMessages hides the volatile panel.
  // Feed every durable SSE progress item into that same panel, not just notes.
  if (progressData.kind !== 'reasoning_delta') {
    const conversation = getConversationById(conversationId);
    const turnId = progress.turn_id || message.task.streamTurnId;
    const turn = conversation?.turnCalls?.get(turnId);
    if (turn) {
      turn.progress ||= [];
      if (!sequence || !turn.progress.some((item) => Number(item.sequence) === sequence)) {
        turn.progress.push({...progressData, sequence});
      }
    }
  }
  if (progressData.kind === 'reasoning_delta') {
    const fragment = progressData.reasoning_text || '';
    const conversation = getConversationById(conversationId);
    const currentInput = [...(conversation?.messages || [])].reverse()
      .find((item) => item.role === 'user' && item.taskId === taskId && item.turnId);
    const turn = currentInput && conversation?.turnCalls?.get(currentInput.turnId);
    if (turn) {
      turn.progress ||= [];
      const last = turn.progress[turn.progress.length - 1];
      if (last?.kind === 'reasoning_delta' && last.model_call === progressData.model_call) {
        last.reasoning_text += fragment;
      } else {
        turn.progress.push({kind: 'reasoning_delta', model_call: progressData.model_call, reasoning_text: fragment});
      }
    }
    const previous = lines[lines.length - 1];
    if (previous?.progressData?.kind === 'reasoning_delta'
        && previous.progressData.model_call === progressData.model_call) {
      previous.text += fragment;
      previous.sequence = sequence;
    } else {
      lines.push({ sequence, text, progressData });
    }
    scheduleProgressRender(conversationId);
    return;
  }
  if (
    lines[lines.length - 1]?.text === text
    && lines[lines.length - 1]?.progressData?.kind === progressData.kind
  ) return;
  lines.push({ sequence, text, progressData });
  if (lines.length > 30) lines.splice(0, lines.length - 30);
  scheduleProgressRender(conversationId);
}

function renderApprovalCard(element, message) {
  const approval = message.approval || {};
  const isProjectTrust = approval.kind === 'project_trust';

  const hero = document.createElement('div');
  hero.className = 'approval-hero';

  const heading = document.createElement('div');
  // The default branch is intentionally kept as a literal compact summary:
  // heading.innerHTML = `<div class="approval-title">需要授权</div>`;
  heading.innerHTML = isProjectTrust ? `
    <div class="approval-title">需要信任工作区</div>
    <div class="approval-subtitle">允许受控命令、构建和测试；文件写入仍逐项审批。</div>
  ` : `
    <div class="approval-title">需要授权</div>
    <div class="approval-subtitle">请确认以下操作内容与影响范围。</div>
  `;

  const risk = document.createElement('div');
  risk.className = 'approval-risk';
  risk.textContent = 风险文案[approval.risk] || approval.risk || '未知风险';

  hero.appendChild(heading);
  hero.appendChild(risk);
  element.appendChild(hero);

  const makeApprovalItem = (label, value, compact = false) => {
    const item = document.createElement('div');
    item.className = 'approval-item';
    const labelElement = document.createElement('div');
    labelElement.className = 'approval-label';
    labelElement.textContent = label;
    const valueElement = document.createElement('div');
    valueElement.className = 'approval-value';
    const text = String(value ?? '未知').trim();
    valueElement.textContent = compact && text.length > 120
      ? `${text.slice(0, 117)}…` : text;
    item.append(labelElement, valueElement);
    return item;
  };

  const grid = document.createElement('div');
  grid.className = 'approval-grid';
  grid.append(
    makeApprovalItem('动作', approval.action, true),
    makeApprovalItem('目标', approval.target || '当前工作区', true),
  );

  element.appendChild(grid);

  const details = document.createElement('details');
  details.className = 'approval-details';
  const summary = document.createElement('summary');
  summary.textContent = '查看完整操作详情';
  details.appendChild(summary);
  const detailsGrid = document.createElement('div');
  detailsGrid.className = 'approval-grid';
  detailsGrid.append(
    makeApprovalItem('网络访问', approval.network_access ? '允许外网访问' : '仅本地操作'),
    makeApprovalItem('数据外发', approval.data_transmission || 'none'),
    makeApprovalItem('可回滚', approval.rollback || '未知'),
  );
  details.appendChild(detailsGrid);
  if (approval.preview) {
    const preview = document.createElement('pre');
    preview.className = 'approval-preview';
    preview.textContent = approval.preview;
    details.appendChild(preview);
  }
  element.appendChild(details);

  const actions = document.createElement('div');
  actions.className = 'approval-actions';
  if (message.resolved) {
    const done = document.createElement('div');
    done.className = 'approval-row';
    done.textContent = message.resolved === 'APPROVE' ? '已允许' : '已拒绝';
    actions.appendChild(done);
  } else if (message.submitting) {
    const pending = document.createElement('div');
    pending.className = 'approval-row';
    pending.textContent = '正在提交授权…';
    actions.appendChild(pending);
  } else {
    const approve = document.createElement('button');
    approve.className = 'approval-btn approve';
    approve.textContent = isProjectTrust ? '信任并继续' : '允许';
    approve.onclick = () => decideApproval(message, 'APPROVE');
    const deny = document.createElement('button');
    deny.className = 'approval-btn deny';
    deny.textContent = '拒绝';
    deny.onclick = () => decideApproval(message, 'DENY');
    actions.appendChild(approve);
    actions.appendChild(deny);
  }
  element.appendChild(actions);
}

async function resumeContinuation(taskId, conversationId) {
  clearError();
  try {
    const response = await fetch(`/tasks/${taskId}/resume`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command_id: crypto.randomUUID() }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || `继续失败，状态码 ${response.status}`);
    }
  } catch (error) {
    console.error(error);
    showError(error.message || '继续任务失败。');
    return;
  }
  const card = taskMessage(conversationId || activeConversationId, taskId);
  if (card) card.task.waiting = null;
  renderMessages();
  setLoading(true);
  followTask(taskId, conversationId || activeConversationId);
}

async function cancelTask(taskId, conversationId) {
  clearError();
  try {
    const response = await fetch(`/tasks/${taskId}/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command_id: crypto.randomUUID() }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || `取消失败，状态码 ${response.status}`);
    }
  } catch (error) {
    console.error(error);
    showError(error.message || '取消任务失败。');
    return;
  }
  followTask(taskId, conversationId || activeConversationId);
}

async function decideApproval(message, decision) {
  const approval = message.approval || {};
  clearError();
  try {
    const response = await fetch('/approvals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        task_id: message.taskId,
        request_id: approval.request_id,
        decision,
        command_id: crypto.randomUUID(),
      }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || `授权失败，状态码 ${response.status}`);
    }
  } catch (error) {
    console.error(error);
    showError(error.message || '提交授权决定失败。');
    return;
  }
  const card = taskMessage(message.conversationId || activeConversationId, message.taskId);
  const conversation = getConversationById(
    message.conversationId || activeConversationId,
  );
  // Restored/live conversations render approvals as standalone messages.
  // Update that message too; changing only the task card leaves the visible
  // approval dock with its old buttons forever.
  const standalone = conversation?.messages.find((item) => (
    item.role === 'approval'
    && item.taskId === message.taskId
    && item.approval?.request_id === approval.request_id
  ));
  // A 202 only says the Runtime accepted the request for background work. It
  // is not proof that the approval was committed. Keep the dock visible while
  // awaiting the durable approval.resolved event; snapshot refresh removes it
  // once pending_approval is actually cleared.
  if (standalone) standalone.submitting = true;
  if (card) {
    card.task.approvalResolution = {
      requestId: approval.request_id, decision, submitting: true,
    };
  }
  renderMessages();
  if (decision === 'APPROVE') {
    setLoading(true);
    followTask(message.taskId, message.conversationId || activeConversationId);
  }
}

function appendApproval(taskId, approval, conversationId = activeConversationId) {
  const card = taskMessage(conversationId, taskId)
    || upsertTaskCard(conversationId, {
      task_id: taskId, phase1_state: 'WAITING', status: 'awaiting_approval',
    });
  if (!card) return;
  card.task.phase1State = 'WAITING';
  card.task.status = 'awaiting_approval';
  card.task.waiting = { kind: 'APPROVAL', approval };
  if (!approvalResolutionMatches(approval, card.task.approvalResolution)) {
    card.task.approvalResolution = null;
  }
  bindLatestInputToTask(conversationId, taskId, approval.turn_id || null);
  upsertApprovalMessage(taskId, approval, conversationId);
  if (conversationId === activeConversationId) renderMessages();
}

function approvalResolutionMatches(approvalRequest, resolution) {
  return resolution?.requestId === approvalRequest.request_id;
}

function appendMessage(
  role, content, images = [], conversationId = activeConversationId, taskId = null,
) {
  const active = getConversationById(conversationId);
  if (!active) return;
  const text = String(content ?? '').trim();
  if (!text && images.length === 0) return;
  const last = active.messages[active.messages.length - 1];
  if (
    role === 'system' && last?.role === 'system'
    && last.content === text && last.taskId === taskId
  ) return;
  active.messages.push({ role, content: text, images, taskId });
  renderConversations();
  if (conversationId === activeConversationId) {
    renderMessages();
  }
}

function setLoading(isLoading) {
  document.getElementById('loading').classList.toggle('visible', isLoading);
}

function showError(message) {
  const banner = document.getElementById('error-banner');
  banner.textContent = message;
  banner.classList.add('visible');
}

function clearError() {
  document.getElementById('error-banner').classList.remove('visible');
}

function clearMessages() {
  const active = getActiveConversation();
  if (!active) return;
  active.messages = [];
  renderConversations();
  renderMessages();
}

async function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

let imagePreviewReturnFocus = null;

function openImagePreview(src, alt) {
  const modal = document.getElementById('image-preview-modal');
  const target = document.getElementById('image-preview-target');
  if (!modal.classList.contains('visible')) imagePreviewReturnFocus = document.activeElement;
  target.src = src;
  target.alt = alt || '图片预览';
  modal.classList.add('visible');
  modal.setAttribute('aria-hidden', 'false');
  document.getElementById('image-preview-close').focus();
}

function closeImagePreview() {
  const modal = document.getElementById('image-preview-modal');
  const target = document.getElementById('image-preview-target');
  modal.classList.remove('visible');
  modal.setAttribute('aria-hidden', 'true');
  target.removeAttribute('src');
  imagePreviewReturnFocus?.focus();
  imagePreviewReturnFocus = null;
}

document.addEventListener('click', (event) => {
  const modal = document.getElementById('image-preview-modal');
  if (!modal.classList.contains('visible')) return;

  if (
    event.target.id === 'image-preview-modal'
    || event.target.id === 'image-preview-close'
  ) {
    closeImagePreview();
  }
});

document.addEventListener('keydown', (event) => {
  const modal = document.getElementById('image-preview-modal');
  if (!modal.classList.contains('visible')) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    closeImagePreview();
  } else if (event.key === 'Tab') {
    event.preventDefault();
    document.getElementById('image-preview-close').focus();
  }
});

function renderPreviews() {
  const previewList = document.getElementById('preview-list');
  previewList.innerHTML = '';
  selectedImages.forEach((image, index) => {
    const item = document.createElement('div');
    item.className = 'preview-item';
    item.title = image.name;
    const thumb = document.createElement('img');
    thumb.src = image.image_url;
    thumb.alt = image.name;
    thumb.onclick = () => openImagePreview(image.image_url, image.name);
    const remove = document.createElement('button');
    remove.className = 'preview-remove';
    remove.textContent = '×';
    remove.title = '移除图片';
    remove.onclick = () => {
      selectedImages.splice(index, 1);
      renderPreviews();
    };
    item.append(thumb, remove);
    previewList.appendChild(item);
  });
}

const MAX_IMAGE_EDGE = 1024;
const IMAGE_JPEG_QUALITY = 0.65;
// Mirrors MAX_IMAGE_DATA_URL_CHARS on the server, with headroom for the JSON
// envelope, so an oversized screenshot degrades here instead of failing as 413.
const MAX_IMAGE_DATA_URL_CHARS = 380000;
// Shrinking the longest edge saves area, so it beats lowering quality: each
// step halves cost far faster than a quality drop while keeping glyphs legible.
const IMAGE_FALLBACK_STEPS = [
  { edge: 1024, quality: 0.5 },
  { edge: 896, quality: 0.5 },
  { edge: 768, quality: 0.45 },
  { edge: 640, quality: 0.4 },
];

function encodeImageToJpeg(image, edge, quality) {
  const longest = Math.max(image.width, image.height);
  const scale = longest > edge ? edge / longest : 1;
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(image.width * scale));
  canvas.height = Math.max(1, Math.round(image.height * scale));
  const context = canvas.getContext('2d');
  // JPEG carries no alpha channel: without an opaque base, transparent regions
  // of a PNG screenshot are composited onto black and become unreadable.
  context.fillStyle = '#ffffff';
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL('image/jpeg', quality);
}

async function prepareImage(dataUrl) {
  // Every attachment is re-encoded, not just oversized ones. A raw PNG
  // screenshot below the edge limit used to be sent untouched, which is the
  // worst case: base64 PNG is several times larger than the same view as JPEG.
  const image = await new Promise((resolve, reject) => {
    const element = new Image();
    element.onload = () => resolve(element);
    element.onerror = reject;
    element.src = dataUrl;
  });

  let prepared = encodeImageToJpeg(image, MAX_IMAGE_EDGE, IMAGE_JPEG_QUALITY);
  for (const step of IMAGE_FALLBACK_STEPS) {
    if (prepared.length <= MAX_IMAGE_DATA_URL_CHARS) break;
    prepared = encodeImageToJpeg(image, step.edge, step.quality);
  }

  // A small flat icon can still encode smaller as its original PNG. Prefer it
  // only when its pixel size is already within the edge budget, since pixel
  // area — not byte size — is what the provider bills for a vision input.
  const withinEdge = Math.max(image.width, image.height) <= MAX_IMAGE_EDGE;
  return withinEdge && dataUrl.length < prepared.length ? dataUrl : prepared;
}

async function addImageFiles(files) {
  const images = Array.from(files || []).filter(
    (file) => file && file.type.startsWith('image/'),
  );
  if (images.length === 0) return false;

  for (const file of images) {
    const original = await fileToDataUrl(file);
    let prepared = original;
    try {
      prepared = await prepareImage(original);
    } catch (error) {
      console.error(error);
    }
    selectedImages.push({
      name: file.name || `粘贴图片-${selectedImages.length + 1}.png`,
      media_type: prepared.startsWith('data:image/jpeg')
        ? 'image/jpeg'
        : file.type,
      image_url: prepared,
    });
  }
  renderPreviews();
  return true;
}

async function handleImages(event) {
  await addImageFiles(event.target.files);
  // Allow re-selecting the same file after it was removed.
  event.target.value = '';
}

async function handlePaste(event) {
  const items = event.clipboardData?.items;
  if (!items) return;
  const files = Array.from(items)
    .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
    .map((item) => item.getAsFile())
    .filter(Boolean);
  if (files.length === 0) return;
  // Keep the pasted image out of the textarea as binary noise.
  event.preventDefault();
  if (await addImageFiles(files)) clearError();
}

function describeFailure(reason) {
  if (/context cannot fit/.test(reason) && /current_turn_and_tool_protocol=/.test(reason)) {
    return '本轮对话或工具输出超出模型上下文上限，当前请求未发送给模型。请减少一次读取的内容，或分段处理后继续。';
  }
  if (/context cannot fit/.test(reason)) {
    return '本轮内容超出模型上下文上限，当前请求未发送给模型。请减少输入、附件或一次读取的工具内容后重试。';
  }
  return reason;
}

function toolFailureDescription(progress) {
  const raw = String(progress.detail || '');
  const timeout = raw.match(/^tool exceeded ([0-9.]+) second deadline$/);
  const detail = timeout ? `执行超过 ${timeout[1]} 秒，等待结果超时` : raw;
  if (progress.error_code === 'UNKNOWN_OUTCOME') {
    return `结果未确认（UNKNOWN_OUTCOME）${detail ? `：${detail}` : ''}。可继续查询实际状态。`;
  }
  return `失败${progress.error_code ? `（${progress.error_code}）` : ''}${detail ? `：${detail}` : ''}`;
}

function describeProgress(progress) {
  if (!progress) return '';
  const kind = progress.kind;
  if (kind === 'completion_review') {
    if (progress.review_action === 'STARTED') return '正在进行完成复核，最终答复尚未发布…';
    if (progress.review_action === 'FAILED') return `完成复核失败：${progress.review_reason_code || '未知错误'}`;
    return `完成复核：${progress.review_action || '状态更新'}`;
  }
  if (kind === 'turn_interrupted') return `本轮未完成：${progress.detail || progress.diagnostic_code || '执行已中断'}`;
  if (kind === 'waiting') {
    return progress.action || (progress.waiting_kind === 'approval' ? '等待授权' : '等待用户输入');
  }
  // 处理说明完全来自模型；工具状态由各自的结构化进度项显示。
  if (kind === 'commentary') return progress.activity || '';
  // 推理增量：保持原有中文前缀。
  if (kind === 'reasoning_delta') {
    return progress.reasoning_text ? `推理摘要：${progress.reasoning_text}` : '';
  }
  // 模型调用：用 Codex 风格的 "思考" 标签，若有计数则展示 "当前/上限"。
  if (kind === 'model_started') {
    const call = typeof progress.model_call === 'number' ? progress.model_call : null;
    const maxCalls = typeof progress.max_model_calls === 'number' ? progress.max_model_calls : null;
    const counter = call !== null && maxCalls !== null
      ? `${call}/${maxCalls}`
      : (call !== null ? String(call) : '');
    return counter ? `[思考 ${counter}] 模型正在分析…` : '正在思考…';
  }
  if (kind === 'model_completed') {
    return '已生成一轮回复';
  }
  // 工具调用开始：采用 "[操作 n] 工具名：参数" 的中文样式。
  if (kind === 'tool_started') {
    const call = typeof progress.tool_call === 'number' ? progress.tool_call : null;
    const maxCalls = typeof progress.max_tool_calls === 'number' ? progress.max_tool_calls : null;
    const counter = call !== null && maxCalls !== null
      ? `${call}/${maxCalls}`
      : (call !== null ? String(call) : '');
    const rawName = String(progress.tool_name || '未知工具');
    const isMcp = rawName.startsWith('mcp.');
    const displayName = isMcp ? `${rawName}（MCP 工具）` : rawName;
    const heading = counter
      ? `[操作 ${counter}] ${displayName}`
      : `调用工具：${displayName}`;
    const rendered = progress.operation_presentation || '';
    return rendered ? `${heading}：${rendered}` : heading;
  }
  // 工具完成：使用 "[完成 n] 工具名：状态（耗时；证据增量）" 的中文样式。
  if (kind === 'tool_completed') {
    const call = typeof progress.tool_call === 'number' ? progress.tool_call : null;
    const rawName = String(progress.tool_name || '未知工具');
    const isMcp = rawName.startsWith('mcp.');
    const displayName = isMcp ? `${rawName}（MCP 工具）` : rawName;
    const heading = call !== null
      ? `[完成 ${call}] ${displayName}`
      : `工具 ${displayName}`;
    const status = progress.ok === false
      ? toolFailureDescription(progress)
      : '完成';
    const details = [];
    if (typeof progress.elapsed_seconds === 'number') {
      details.push(`${progress.elapsed_seconds.toFixed(1)} 秒`);
    }
    if (progress.evidence_delta != null) {
      if (progress.evidence_delta) {
        details.push(`新增证据 ${progress.evidence_delta} 项`);
      } else {
        details.push('没有新增证据');
      }
    }
    const detailText = details.length ? `（${details.join('；')}）` : '';
    return `${heading}：${status}${detailText}`;
  }
  if (kind === 'model_retry' || kind === 'model_transport') {
    const attempt = progress.transport_attempt || 1;
    const maximum = progress.max_transport_attempts || 1;
    const prefix = `模型请求 ${attempt}/${maximum}`;
    const mode = {
      streaming: '流式', non_streaming: '非流式',
    }[progress.transport_mode] || '';
    const event = progress.transport_event;
    if (event === 'attempt_started') {
      return `${prefix} 开始${mode ? `（${mode}）` : ''}`;
    }
    if (event === 'attempt_completed') {
      return `${prefix} 成功${mode ? `（${mode}）` : ''}`;
    }
    if (event === 'attempt_failed') {
      const code = progress.diagnostic_code
        ? ` · ${progress.diagnostic_code}` : '';
      const detail = progress.diagnostic_detail
        ? `：${progress.diagnostic_detail}` : '';
      return `${prefix} 失败${code}${detail}`;
    }
    if (event === 'recovery_decided') {
      const action = {
        FALLBACK_TRANSPORT: '切换为非流式请求',
        RETRY_SAME_REQUEST: '重试同一请求',
        RESAMPLE: '重新采样',
        RESAMPLE_WITH_CORRECTION: '修正后重试',
        PRESERVE_AND_INTERRUPT: '保留进度并中断',
        FAIL_TERMINAL: '停止重试',
      }[progress.recovery_action] || '执行恢复策略';
      const delay = progress.retry_delay_seconds > 0
        ? `，${progress.retry_delay_seconds} 秒后执行` : '';
      return `${prefix}：${action}${delay}`;
    }
    return `${prefix} 状态更新`;
  }
  if (kind === 'focus') return progress.activity || '调整执行重点';
  if (kind === 'exploration') return progress.activity || '正在检索工作区';
  if (kind === 'wrap_up') return '正在整理结果…';
  return progress.activity || '';
}

async function handleCancelAction() {
  const button = document.getElementById('cancel-btn');
  await cancelRunningTask(button?.dataset.taskId);
}

async function cancelRunningTask(taskId) {
  clearError();
  if (!taskId) {
    showError('当前没有可取消的任务。');
    return;
  }

  try {
    const response = await fetch(`/tasks/${taskId}/stop`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });

    if (!response.ok) {
      throw new Error(`取消任务失败，状态码 ${response.status}`);
    }

    // The interrupt endpoint is accepted synchronously, while its terminal
    // state is published asynchronously. Follow the authoritative result so
    // the button cannot remain visible merely because this card is stale.
    const active = getActiveConversation();
    if (active && taskId === active.activeTaskId) {
      // 当前活动会话的 Turn 被取消，相当于本轮 Turn 已结束：
      // 直接在前端视角中结束这一轮，立刻隐藏取消按钮，避免依赖滞后的 settled 状态。
      const cancelButton = document.getElementById('cancel-btn');
      if (cancelButton) {
        cancelButton.classList.add('hidden');
        cancelButton.disabled = false;
        cancelButton.dataset.taskId = '';
        cancelButton.dataset.turnId = '';
      }

      // 继续跟随任务终态事件，保证其它状态仍然按 authoritative Session 同步。
      followTask(taskId, active.id);
    } else {
      updateComposerButtonState();
    }
  } catch (error) {
    console.error(error);
    showError(error.message || '取消任务时发生未知错误。');
  }
}

async function runTask() {
  clearError();
  if (!activeWorkspaceId) {
    showError('发送消息前请先选择工作区。');
    return;
  }

  // Read the composer before creating the first conversation.  Creating a
  // conversation restores its draft and used to clear a user's first input
  // before it was captured, producing a misleading "请输入描述" error.
  const promptElement = document.getElementById('prompt');
  const prompt = promptElement.value.trim();
  if (!prompt) {
    showError('请输入描述');
    return;
  }

  if (!getActiveConversation()) {
    createConversation();
  }

  const images = selectedImages.slice();
  const conversationId = activeConversationId;
  const workspaceId = activeWorkspaceId;
  appendMessage('user', prompt, images, conversationId);
  promptElement.value = '';
  if (conversationId) {
    const activeConversation = getConversationById(conversationId);
    if (activeConversation) {
      activeConversation.draft = '';
    }
    removeBrowserStorage('sessionStorage', draftStorageKey(conversationId));
  }
  selectedImages.length = 0;
  renderPreviews();
  setLoading(true);
  updateComposerButtonState();
  const requestId = crypto.randomUUID();
  const reasoningEffortSelect = document.getElementById('reasoning-effort');
  const reasoningEffort = (
    reasoningEffortSelect && typeof reasoningEffortSelect.value === 'string'
      ? reasoningEffortSelect.value.trim()
      : ''
  ) || null;
  const diagnosticRequested = diagnoseNextInput && (!diagnosticSessionId || diagnosticSessionId === conversationId);
  const diagnosticSessionMismatch = diagnoseNextInput && !diagnosticRequested;
  diagnoseNextInput = false;
  document.getElementById('diagnostic-toggle').textContent = '诊断下一轮';
  try {
    const activeConversation = getConversationById(conversationId);
    if (activeConversation?.modelSelectionSave) await activeConversation.modelSelectionSave;
    const activeTaskId = activeConversation?.activeTaskId || null;
    let intent = null;
    let routedText = prompt;
    if (prompt.startsWith('/steer ')) {
      intent = 'STEER';
      const trimmed = prompt.slice('/steer '.length).trim();
      // 为空时保留原始文本，避免完全丢失用户输入。
      if (trimmed) routedText = trimmed;
    }
    const response = await fetch('/session-input', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        text: routedText,
        request_id: requestId,
        workspace_id: workspaceId,
        session_id: conversationId,
        target_task_id: activeTaskId,
        intent,
        reasoning_effort: reasoningEffort,
        model_profile_id: activeConversation?.modelProfileId || document.getElementById('model-profile')?.value || null,
        diagnose_request: diagnosticRequested,
        images,
      })
    });
    if (!response.ok) {
      let detail = '';
      try {
        const errorBody = await response.json();
        detail = errorBody.detail ? `：${errorBody.detail}` : '';
      } catch (_) {
        // Keep the status-only fallback for non-JSON gateway responses.
      }
      throw new Error(`请求失败，状态码 ${response.status}${detail}`);
    }

    const data = await response.json();
    if (diagnosticSessionMismatch) showError('会话已切换，未为本轮开启诊断。');
    if (data.diagnostic_key) {
      latestDiagnostic = {key: data.diagnostic_key, sessionId: conversationId};
    }
    if (data.kind === 'answer') {
      appendMessage('assistant', data.answer || '未返回回答。', [], conversationId);
      setLoading(false);
      return;
    }
    if (data.kind === 'clarify') {
      appendMessage('assistant', data.clarification || '请补充说明相关任务。', [], conversationId);
      setLoading(false);
      return;
    }

    const task = data.task;
    if (!task?.task_id) {
      throw new Error('会话响应中没有返回任务。');
    }
    // The input is optimistically rendered before this Runtime receipt. Bind
    // it now so an immediately suspended approval still owns its Turn trace.
    // Approval carries the authoritative turn_id for this boundary.
    bindLatestInputToTask(
      conversationId, task.task_id, task.approval?.turn_id || null,
    );
    const card = upsertTaskCard(conversationId, {
      task_id: task.task_id,
      // A continuation keeps the conversation's own goal; only a first turn
      // names it.
      goal: (
        data.kind === 'continuation'
        && taskMessage(conversationId, task.task_id)
      )
        ? taskMessage(conversationId, task.task_id).task.goal
        : prompt,
      phase1_state: task.phase1_state || 'PREPARING',
      status: task.status || 'running',
      progress_cursor: 0,
      projection: task.projection || null,
      waiting: task.approval
        ? { kind: 'APPROVAL', approval: task.approval }
        : task.status === 'awaiting_user'
          ? { kind: task.clarification?.kind || 'INPUT', ...task.clarification }
          : null,
    });
    if (TERMINAL_STATES.includes(task.phase1_state)) {
      renderFinalResult(task.task_id, task, conversationId);
      setLoading(false);
      updateComposerButtonState();
    } else if (data.kind === 'continuation') {
      // A later turn on this conversation was accepted and runs in the
      // background exactly like a first turn. Its task still reads as idle
      // (AWAITING_INPUT -> WAITING) until that turn starts, so following it is
      // what shows progress and the new answer.
      // This Turn starts with an empty buffer: lines kept from the previous
      // Turn must never read as progress of the question just asked.
      if (card) card.task.progress = [];
      followTask(task.task_id, conversationId);
      updateComposerButtonState();
    } else {
      // Even an immediately suspended Task must attach its SSE follower. It
      // replays the durable trace and publishes the approval/clarification
      // payload; stopping here made the latest Turn appear to have no trace
      // and no approval dock.
      if (task.approval) {
        appendApproval(task.task_id, task.approval, conversationId);
      } else if (task.status === 'awaiting_user' && card) {
        card.task.waiting = card.task.waiting || { kind: 'INPUT' };
      }
      followTask(task.task_id, conversationId);
      updateComposerButtonState();
    }
  } catch (error) {
    console.error(error);
    showError(error.message || '发送消息时发生未知错误。');
    setLoading(false);
    updateComposerButtonState();
  }
}

const 状态文案 = {
  PREPARING: '准备中', RUNNING: '执行中', WAITING: '等待操作',
  INTERRUPTED: '已中断', DONE: '已完成', FAILED: '已失败',
  CANCELLED: '已取消', NEEDS_REVIEW: '待人工复核',
};
// INTERRUPTED is resumable at the Task level, but it is terminal for the
// current Turn: no more events will arrive until the user sends another input.
// Leaving it out kept the follower alive while renderMessages stopped treating
// the Task as live, so an interrupted Turn's trace vanished until a reload.
const TERMINAL_STATES = [
  'INTERRUPTED', 'DONE', 'FAILED', 'CANCELLED', 'NEEDS_REVIEW',
];
const MAX_STREAM_RETRIES = 6;
const followers = new Map();

// Keep received text authoritative; only its presentation is paced. A large
// network chunk must not rebuild the transcript or replace already drawn text.
const textGraphemes = typeof Intl.Segmenter === 'function'
  ? new Intl.Segmenter(undefined, {granularity: 'grapheme'}) : null;

function paintTaskTextBody(body, message) {
  const view = message.textPresentation;
  if (view && (message.textStream || view.visible !== message.content)) {
    const previous = body.textContent || '';
    if (body.classList.contains('streaming-text') && view.visible.startsWith(previous)
        && body.firstChild?.nodeType === 3) {
      body.firstChild.appendData(view.visible.slice(previous.length));
    } else {
      body.textContent = view.visible;
    }
    body.classList.add('streaming-text');
  } else {
    body.classList.remove('streaming-text');
    body.innerHTML = renderMarkdown(String(message.content || '').trim());
  }
}

function scheduleTaskTextRender(message, conversationId) {
  const view = message.textPresentation;
  if (!view || view.frame != null || conversationId !== activeConversationId) return;
  view.frame = requestAnimationFrame(now => {
    view.frame = null;
    const conversation = getConversationById(conversationId);
    // History refresh may replace the message object while keeping its view.
    const current = conversation?.messages.find(item => item.textPresentation === view);
    if (!current || conversationId !== activeConversationId) { view.lastTime = null; return; }
    const remaining = current.content.slice(view.visible.length);
    if (remaining) {
      const elapsed = view.lastTime == null ? 16 : Math.min(64, Math.max(0, now - view.lastTime));
      // Roughly one character per frame for small chunks. Catch up adaptively
      // after bursts instead of spending seconds replaying an already finished answer.
      view.credit += elapsed * Math.max(60, remaining.length / 0.35) / 1000;
      const budget = Math.floor(view.credit);
      const units = textGraphemes ? textGraphemes.segment(remaining) : remaining;
      let count = 0;
      for (const unit of units) {
        if (count >= budget) break;
        view.visible += textGraphemes ? unit.segment : unit;
        count += 1;
      }
      view.credit -= count;
    }
    view.lastTime = now;
    if (view.visible === current.content) {
      view.lastTime = null;
      view.credit = 0;
      if (view.removeAfter) {
        conversation.messages = conversation.messages.filter(item => item !== current);
        scheduleProgressRender(conversationId);
        return;
      }
    }
    const chatArea = document.getElementById('chat-area');
    const element = Array.from(chatArea?.querySelectorAll('[data-text-stream-id]') || [])
      .find(item => item.dataset.textStreamId === view.id);
    if (element) {
      const follow = shouldAutoFollowChat || isNearChatBottom(chatArea);
      paintTaskTextBody(element.querySelector('.message-body'), current);
      element.classList.toggle('stream-commentary', current.textStream?.phase === 'commentary');
      const label = element.querySelector('.stream-text-label');
      if (label) label.textContent = current.textStream?.status === 'reviewing' ? '正在确认回答…'
        : !current.textStream || current.textStream.status === 'committed' ? ''
        : current.textStream.phase === 'commentary' ? '处理说明 · 正在生成…' : '正在生成…';
      if (follow) {
        chatArea.scrollTop = chatArea.scrollHeight;
        lastConversationScrollTop = chatArea.scrollTop;
      }
      updateChatJumpButton(chatArea);
    }
    if (!current.textStream && view.visible === current.content) {
      delete current.textPresentation;
      return;
    }
    if (view.visible !== current.content) scheduleTaskTextRender(current, conversationId);
  });
}

function clearTaskTextStreams(taskId, conversationId) {
  const conversation = getConversationById(conversationId);
  if (!conversation) return;
  conversation.messages = conversation.messages.filter(
    item => !(item.taskId === taskId && item.textStream),
  );
  scheduleProgressRender(conversationId);
}

function applyTaskText(taskId, progress, conversationId, replaceText = false) {
  const conversation = getConversationById(conversationId);
  if (!conversation || !progress.text_id) return;
  const identity = progress.text_id;
  const status = progress.text_status || 'generating';
  let message = conversation.messages.find(
    item => item.taskId === taskId && item.textStream?.id === identity,
  );
  if (status === 'committed' && progress.phase === 'commentary' && message
      && message.textPresentation.visible !== message.content) {
    message.textStream.status = status;
    message.textStream.phase = 'commentary';
    message.textPresentation.removeAfter = true;
    scheduleTaskTextRender(message, conversationId);
    return;
  }
  if (['discarded', 'interrupted'].includes(status)
      || (status === 'committed' && progress.phase === 'commentary')) {
    conversation.messages = conversation.messages.filter(
      item => !(item.taskId === taskId && item.textStream?.id === identity),
    );
    scheduleProgressRender(conversationId);
    return;
  }
  const isNew = !message;
  if (!message) {
    if (!progress.text) return;
    message = {role: 'assistant', content: '', images: [], taskId,
      turnId: progress.turn_id,
      textPresentation: {id: identity, visible: '', credit: 0, lastTime: null, frame: null},
      textStream: {id: identity, sequence: 0, phase: 'pending', status}};
    conversation.messages.push(message);
  }
  const sequence = Number(progress.text_sequence || 0);
  const previousPhase = message.textStream.phase;
  const phase = progress.phase || previousPhase;
  const phaseChangedToFinal = previousPhase === 'commentary' && phase === 'final_answer';
  // When a commentary stream becomes a final answer stream, drop the commentary
  // text from the answer bubble so the final answer is not prepended with
  // processing notes. The commentary itself remains in the execution history.
  if (!replaceText && phaseChangedToFinal) {
    message.content = '';
    message.textPresentation.visible = '';
  }
  if (replaceText) {
    message.content = String(progress.text || '');
    message.textStream.sequence = sequence;
    // Recovered history is already read: do not replay it character by character.
    message.textPresentation.visible = message.content;
  } else if (progress.kind === 'text_delta' && sequence > message.textStream.sequence) {
    message.content += progress.text || '';
    message.textStream.sequence = sequence;
  }
  message.textStream.phase = phase;
  message.textStream.status = status;
  if (isNew) scheduleProgressRender(conversationId);
  scheduleTaskTextRender(message, conversationId);
}

function stopConversationFollowers(conversationId, exceptTaskId = null) {
  followers.forEach((follower, taskId) => {
    if (follower.conversationId === conversationId && taskId !== exceptTaskId) {
      follower.stop();
    }
  });
}

function appendTaskAssistantText(taskId, text, conversationId) {
  const assistantText = String(text || '').trim();
  if (!assistantText) return false;
  const conversation = getConversationById(conversationId);
  const draft = [...(conversation?.messages || [])].reverse().find(
    item => item.taskId === taskId && item.textStream
      && item.textStream.phase !== 'commentary',
  );
  if (draft) {
    if (draft.textPresentation) {
      const shown = draft.textPresentation.visible.trimStart().slice(0, assistantText.length);
      draft.textPresentation.visible = assistantText.startsWith(shown) ? shown : assistantText;
    }
    draft.content = assistantText;
    delete draft.textStream;
    return true;
  }
  const duplicate = conversation?.messages.some(
    (item) => item.role === 'assistant'
      && item.taskId === taskId
      && item.content === assistantText,
  );
  if (duplicate) return false;
  appendMessage('assistant', assistantText, [], conversationId, taskId);
  return true;
}

function renderTurnMarker(turnId) {
  const marker = document.createElement('div');
  marker.className = 'turn-marker';
  const short = String(turnId).slice(0, 13);
  marker.textContent = `轮次 ${short}`;
  marker.title = `点击复制完整轮次 id：${turnId}`;
  marker.onclick = () => copyTextToClipboard(
    turnId, marker, `轮次 ${short}`,
  );
  return marker;
}

function turnCallTarget(call) {
  const target = call.path || call.query || call.pattern
    || call.target || call.file_glob;
  return target ? String(target) : '';
}

// Live view of the Turn that is running: model calls ("thinking") and tool
// calls, in the order they happened. It disappears as soon as the Turn settles,
// where the durable per-Turn panel above takes over.
function renderLiveTurnPanel(task) {
  const lines = Array.isArray(task?.progress) ? task.progress : [];
  const wrap = document.createElement('div');
  wrap.className = 'message assistant live-turn';
  const panel = document.createElement('div');
  panel.className = 'live-turn-panel';

  const head = document.createElement('div');
  head.className = 'live-turn-head';
  const dot = document.createElement('span');
  dot.className = 'live-turn-dot';
  const heading = document.createElement('span');
  heading.textContent = '本轮进行中';
  head.append(dot, heading);
  panel.appendChild(head);

  if (lines.length === 0) {
    const pending = document.createElement('div');
    pending.className = 'live-turn-line active';
    // Before any model frame arrives the Task may still be preparing the
    // workspace, so "thinking" would overstate what is happening.
    pending.textContent = task?.phase1State === 'PREPARING'
      ? '正在准备…'
      : task?.phase1State === 'WAITING'
        ? '等待授权…'
        : '正在思考…';
    panel.appendChild(pending);
  } else {
    lines.forEach((item) => {
      const kind = item.progressData?.kind || '';
      const row = document.createElement('div');
      row.className = 'live-turn-line';
      if (kind === 'model_started' || kind === 'tool_started') {
        row.classList.add('active');
      } else if (item.progressData?.ok === false) {
        row.classList.add('failed');
      }
      row.textContent = item.text;
      panel.appendChild(row);
    });
  }
  wrap.appendChild(panel);
  return wrap;
}

// One call panel per Turn. Settled Turns render under their answer; incomplete
// Turns render under their input. Its content comes from durable Session and
// Runtime records, so a reload never turns real work into an empty transcript.
// Durable per-Turn progress items share the live stream's vocabulary, so one
// mapping describes a Turn while it runs and after it settles. Transport items
// fall through to describeProgress; tool results borrow the name of the call
// they answer.
function turnProgressLines(items) {
  const names = new Map();
  const lines = [];
  const closedApprovals = new Set((items || [])
    .filter((item) => ['approval_resolved', 'approval_invalidated'].includes(item?.kind))
    .map((item) => item.request_id).filter(Boolean));
  (items || []).forEach((item) => {
    const kind = item?.kind || '';
    if (kind === 'turn_started') {
      lines.push({ text: '本轮开始', tone: 'active' });
      return;
    }
    if (kind === 'tool_started') {
      const name = String(item.tool_name || '未知工具');
      if (item.tool_call_id) names.set(item.tool_call_id, name);
      lines.push({
        text: `调用工具：${name}${item.target ? ` · ${item.target}` : ''}`,
        tone: 'active',
      });
      return;
    }
    if (kind === 'tool_completed') {
      const name = names.get(item.tool_call_id) || '工具';
      const ok = item.ok !== false;
      lines.push({
        text: ok
          ? `${name} 完成`
          : `${name} ${toolFailureDescription(item)}`,
        tone: ok ? '' : 'failed',
      });
      return;
    }
    if (kind === 'model_completed') {
      const called = Number(item.tool_call_count || 0);
      lines.push({
        text: `模型第 ${item.model_call || 1} 轮回复完成`
          + (called ? ` · 请求 ${called} 个工具` : ''),
        tone: '',
      });
      return;
    }
    if (kind === 'turn_completed') {
      lines.push({
        text: `本轮结束 · 模型 ${item.model_calls ?? '?'} 次`
          + ` · 工具 ${item.tool_calls ?? '?'} 次`,
        tone: '',
      });
      return;
    }
    if (kind === 'turn_interrupted' || kind === 'turn_failed' || kind === 'turn_conflict') {
      const what = kind === 'turn_conflict' ? '本轮执行冲突'
        : kind === 'turn_interrupted' ? '本轮中断' : '本轮失败';
      lines.push({
        text: `${what}${item.detail ? `：${item.detail}` : ''}`,
        tone: 'failed',
      });
      return;
    }
    if (kind === 'approval_invalidated' || kind === 'approval_resolved') {
      lines.push({
        text: kind === 'approval_invalidated' ? '原授权请求已失效，未获得本次执行许可'
          : item.decision === 'approve' ? '已允许本次操作' : '已拒绝本次操作',
        tone: kind === 'approval_invalidated' ? 'failed' : '',
      });
      return;
    }
    if (kind === 'waiting') {
      const closed = closedApprovals.has(item.request_id);
      lines.push({
        text: item.waiting_kind === 'approval'
          ? `${closed ? '曾请求授权（已处理）' : '等待授权'}${item.action ? `：${item.action}` : ''}`
          : item.action || '等待用户输入',
        tone: closed ? '' : 'active',
      });
      return;
    }
    if (kind === 'completion_review') {
      if (item.review_action === 'STARTED') {
        lines.push({text: '正在进行完成复核，最终答复尚未发布…', tone: 'active'});
      } else if (item.review_action === 'FAILED') {
        lines.push({text: `完成复核失败：${item.review_reason_code || '未知错误'}`, tone: 'failed'});
      } else if (item.review_action === 'UNVERIFIED') {
        lines.push({
          text: '完成审查暂不可用；回答已展示为未核验草稿',
          tone: 'active',
        });
      } else if (item.review_action === 'SKIPPED') {
        lines.push({ text: '未启用完成审查：任务没有验收合约', tone: '' });
      } else {
        lines.push({
          text: `完成审查：${item.review_action || '已完成'}`,
          tone: item.review_action === 'CONTINUE' ? 'active' : '',
        });
      }
      return;
    }
    const described = describeProgress(item);
    if (described) {
      lines.push({ text: described, tone: item.ok === false ? 'failed' : '' });
    }
  });
  return lines;
}

function renderTurnCallPanel(conversation, message) {
  const turn = conversation?.turnCalls?.get(message.turnId);
  if (!turn) return null;
  const incomplete = turn.settled === false;
  // A completed Turn belongs to its answer. A suspended/failed-before-answer
  // Turn has no answer to anchor to, so it belongs directly below its input.
  if (
    (message.role !== 'assistant' || incomplete)
    && (message.role !== 'user' || !incomplete)
  ) return null;
  const calls = Array.isArray(turn.calls) ? turn.calls : [];
  const counts = turn.tool_counts || {};
  const recorded = turn.recorded !== false;
  const counted = Object.values(counts).reduce(
    (sum, value) => sum + Number(value || 0), 0,
  );
  // The durable counter is authoritative when present; older records only kept
  // the per-tool counts, so fall back to their sum.
  const toolCalls = Number.isInteger(turn.tool_calls) ? turn.tool_calls : counted;
  const modelCalls = Number.isInteger(turn.model_calls) ? turn.model_calls : null;
  const failed = calls.filter(
    (call) => call.ok === false || call.state === 'FAILED',
  ).length;
  const total = Math.max(toolCalls, counted);
  const progress = Array.isArray(turn.progress) ? turn.progress : [];
  const panel = document.createElement('details');
  panel.className = `turn-calls ${total > 0 ? 'has-calls' : 'no-calls'}`;
  // A Turn that called nothing is itself worth stating, but it stays collapsed
  // so a long conversation does not become a wall of empty panels. A Turn with
  // recorded activity always shows it: that record is the point.
  panel.open = incomplete || total > 0 || progress.length > 0;

  const summary = document.createElement('summary');
  const label = document.createElement('span');
  label.className = 'turn-calls-label';
  const facts = [];
  if (modelCalls !== null) facts.push(`模型 ${modelCalls} 次`);
  if (incomplete) {
    const factText = facts.length ? ` · ${facts.join(' · ')}` : '';
    label.textContent = `本轮未完成${factText}`;
  } else if (!recorded) {
    label.textContent = '本轮 · 无调用记录';
  } else {
    facts.push(`工具 ${total} 次`);
    if (failed) facts.push(`失败 ${failed} 次`);
    label.textContent = `本轮 · ${facts.join(' · ')}`;
  }
  summary.appendChild(label);
  if (total > 0) {
    const names = document.createElement('span');
    names.className = 'turn-calls-names';
    names.textContent = Object.keys(counts).join('、');
    summary.appendChild(names);
  }
  panel.appendChild(summary);

  const evaluation = buildSourceEvaluation(turn, progress);
  if (evaluation) panel.appendChild(renderSourceEvaluation(evaluation));

  // The durable lines win: they are what the Turn actually did, in order, and
  // they survive a reload. The per-call table stays for older records that
  // predate the progress events.
  const lines = turnProgressLines(progress);
  if (lines.length) {
    const list = document.createElement('div');
    list.className = 'turn-call-list turn-progress-list';
    lines.forEach((line) => {
      const row = document.createElement('div');
      row.className = `live-turn-line ${line.tone || ''}`.trim();
      row.textContent = line.text;
      list.appendChild(row);
    });
    panel.appendChild(list);
  } else if (calls.length) {
    const list = document.createElement('div');
    list.className = 'turn-call-list';
    calls.forEach((call) => {
      const failed = call.ok === false || call.state === 'FAILED';
      const row = document.createElement('div');
      row.className = 'turn-call-row';
      const name = document.createElement('span');
      name.className = 'turn-call-name';
      name.textContent = String(call.tool || '未知工具');
      const target = turnCallTarget(call);
      const status = document.createElement('span');
      status.className = `turn-call-status ${failed ? 'failed' : 'ok'}`;
      status.textContent = failed
        ? String(call.error_code || call.failure_code || 'FAILED')
        : (call.succeeded === false ? '未达成' : '成功');
      row.append(name, status);
      if (target) {
        const detail = document.createElement('span');
        detail.className = 'turn-call-target';
        detail.textContent = target;
        row.appendChild(detail);
      }
      list.appendChild(row);
    });
    // The record keeps the Turn's counts for every call but only its most
    // recent action rows, so say so rather than implying the list is complete.
    if (total > calls.length) {
      const note = document.createElement('div');
      note.className = 'turn-call-target';
      note.textContent = `另有 ${total - calls.length} 次调用未在此列出`;
      list.appendChild(note);
    }
    panel.appendChild(list);
  }
  return panel;
}

function buildSourceEvaluation(turn, progress) {
  const started = progress.find((item) => item.kind === 'turn_started');
  if (started?.tool_exposure_profile !== 'source-analysis-initial') return null;
  const toolStarts = progress.filter((item) => item.kind === 'tool_started');
  const sourceRead = toolStarts.some((item) => (
    ['source.github.get_file', 'core.read_file'].includes(item.tool_name)
  ));
  const reviews = progress.filter((item) => item.kind === 'completion_review');
  const latestReview = reviews.at(-1);
  return {
    profile: started.tool_exposure_profile,
    visibleToolCount: Number(started.visible_tool_count || 0),
    firstTool: toolStarts[0]?.tool_name || '',
    sourceRead,
    review: latestReview?.review_action || '未记录',
    reviewReason: latestReview?.review_reason_code || '',
    finalToolCalls: Number(turn.tool_calls || 0),
  };
}

function renderSourceEvaluation(evaluation) {
  const section = document.createElement('section');
  section.className = 'turn-source-evaluation';
  const first = evaluation.firstTool || '未调用';
  const recommendation = (!evaluation.firstTool || !evaluation.sourceRead)
    ? '继续收集样本；若零工具或未读源码持续偏高，再评估 required。'
    : '当前样本无需启用 required。';
  section.innerHTML = `
    <div class="turn-source-evaluation-heading">源码取证评测 <span>历史真实 Turn</span></div>
    <div class="turn-source-evaluation-grid">
      <div><small>首轮工具面</small><strong>${evaluation.visibleToolCount} 个 · 已收窄</strong></div>
      <div><small>首个动作</small><strong>${escapeHtml(first)}</strong></div>
      <div><small>实际读取源码</small><strong class="${evaluation.sourceRead ? 'good' : 'warn'}">${evaluation.sourceRead ? '是' : '否'}</strong></div>
      <div><small>Completion Review</small><strong>${escapeHtml(evaluation.review)}</strong></div>
    </div>
    <div class="turn-source-evaluation-note">${escapeHtml(recommendation)}${evaluation.reviewReason ? ` 原因：${escapeHtml(evaluation.reviewReason)}` : ''}</div>
  `;
  return section;
}

async function renderFinalResult(taskId, state, conversationId) {
  const card = taskMessage(conversationId, taskId)
    || upsertTaskCard(conversationId, { task_id: taskId });
  // Keep the live trace mounted until its durable Turn projection has been
  // loaded. Updating the card to a terminal state and rendering first creates
  // a blank interval because terminal Tasks no longer own a live panel.
  // On a conflict the SDK may still expose the previous Turn's archived
  // answer. It is not a response to this failed approval/resume.
  const assistantText = state.state === 'CONFLICT' ? '' : String(state.assistant_text || '').trim();
  if (!assistantText) clearTaskTextStreams(taskId, conversationId);
  if (state.failure_reason && conversationId === activeConversationId) {
    showError(describeFailure(state.failure_reason));
  }
  if (assistantText) {
    appendTaskAssistantText(taskId, assistantText, conversationId);
  } else if (state.clarification?.question) {
    appendMessage(
      'assistant', state.clarification.question, [], conversationId, taskId,
    );
  } else if (state.failure_reason) {
    appendTaskProgress(taskId, {
      sequence: 0,
      progress: {
        activity: `本轮已停止：${describeFailure(state.failure_reason)}`,
      },
    }, conversationId);
  }
  if (card) {
    applyTaskProjection(card.task, state.projection);
    applyTaskProtocol(card.task, state);
    card.task.phase1State = state.phase1_state || card.task.phase1State;
    if (state.turn_id
      && TERMINAL_STATES.includes(String(card.task.phase1State || '').toUpperCase())) {
      card.task.settledTurnId = state.turn_id;
    }
    card.task.status = state.status || card.task.status;
    card.task.waiting = null;
  }
  clearApprovalMessage(taskId, conversationId);
  updateComposerButtonState();

  const refreshed = await refreshConversationHistory(conversationId);
  if (assistantText) {
    // The Task state and progress can become observable a few milliseconds
    // before the Session result message. Preserve text already delivered by
    // SSE when that narrow persistence race occurs.
    const rebuilt = getConversationById(conversationId);
    const kept = (rebuilt?.messages || []).some(
      (item) => item.role === 'assistant'
        && item.taskId === taskId
        && item.content === assistantText,
    );
    if (!kept) appendTaskAssistantText(taskId, assistantText, conversationId);
  }
  if (!refreshed && conversationId === activeConversationId) renderMessages();
}

// Each follower owns exactly one Task card. Its cursor is part of that card,
// therefore reconnecting cannot replay another Task's history into the current
// conversation timeline.
function followTask(taskId, conversationId) {
  const existing = followers.get(taskId);
  if (existing) existing.stop();
  stopConversationFollowers(conversationId, taskId);
  const card = taskMessage(conversationId, taskId)
    || upsertTaskCard(conversationId, {task_id: taskId, phase1_state: 'RUNNING'});
  const follower = {
    conversationId, source: null, timer: null, attempts: 0, stopped: false,
    turnId: null, turnSequence: 0, queue: Promise.resolve(),
    stop() {
      this.stopped = true;
      if (this.source) this.source.close();
      if (this.timer) clearTimeout(this.timer);
      if (followers.get(taskId) === this) followers.delete(taskId);
    },
  };
  followers.set(taskId, follower);
  const currentFollower = () => !follower.stopped && followers.get(taskId) === follower;
  const finish = async state => {
    if (!currentFollower()) return;
    follower.stop();
    try { await renderFinalResult(taskId, state, conversationId); }
    finally { if (conversationId === activeConversationId) setLoading(false); }
  };
  const applyState = async state => {
    if (!currentFollower()) return;
    // A Task can carry several turns. Only its current snapshot may settle the
    // foreground turn; old replayed completion events are history, not commands.
    if (state.turn_id && follower.turnId && state.turn_id !== follower.turnId) return;
    const current = taskMessage(conversationId, taskId) || card;
    if (current) {
      applyTaskProjection(current.task, state.projection);
      applyTaskProtocol(current.task, state);
      current.task.todoList = state.todo_list || null;
      current.task.cursor = Math.max(Number(current.task.cursor || 0), Number(state.cursor || 0));
      current.task.phase1State = state.phase1_state;
      if (state.turn_id
        && TERMINAL_STATES.includes(String(state.phase1_state || '').toUpperCase())) {
        current.task.settledTurnId = state.turn_id;
      }
      current.task.status = state.status;
      current.task.waiting = state.waiting || (state.approval
        ? {kind: 'APPROVAL', approval: state.approval}
        : state.status === 'awaiting_user' ? {kind: 'INPUT', ...state.clarification} : null);
    }
    if (state.installations) renderSkillInstallations(state.installations);
    syncClarificationMessage(taskId, current?.task?.waiting, conversationId);
    if (state.approval) {
      bindLatestInputToTask(conversationId, taskId, state.approval.turn_id || state.turn_id || null);
      upsertApprovalMessage(taskId, state.approval, conversationId);
    } else clearApprovalMessage(taskId, conversationId);
    const submittedApprovalIsSettling = Boolean(state.approval
      && current?.task?.approvalResolution?.decision === 'APPROVE'
      && current.task.approvalResolution.requestId === state.approval.request_id);
    if (state.final || TERMINAL_STATES.includes(state.phase1_state)) {
      await finish(state);
    } else if (state.status === 'awaiting_input' && !submittedApprovalIsSettling) {
      // Ordinary completed conversations use WAITING/awaiting_input too.
      // Keep their delivered answer if the history refresh is stale or fails,
      // just as for DONE/FAILED; otherwise the refresh can erase the answer.
      await finish(state);
    } else if ((state.waiting || state.status === 'awaiting_user') && !submittedApprovalIsSettling) {
      // 澄清 / 等待用户输入视为“当前 Turn 结束但任务挂起”：
      // 在前端视角中结束这一轮 Turn，并刷新历史，同时让取消按钮跟随
      // 最新 Session turnCalls 视角收起。
      follower.stop();
      if (conversationId === activeConversationId) renderMessages();
      if (state.assistant_text && state.state !== 'CONFLICT') {
        appendTaskAssistantText(taskId, state.assistant_text, conversationId);
      }
      await refreshConversationHistory(conversationId).catch((error) => console.error(error));
      if (conversationId === activeConversationId) {
        setLoading(false);
        updateComposerButtonState();
      }
    } else if (conversationId === activeConversationId) {
      renderMessages();
      updateComposerButtonState();
    }
  };
  const accept = async payload => {
    if (!currentFollower()) return;
    const current = taskMessage(conversationId, taskId) || card;
    if (payload.type === 'stream.reset_required') {
      if (current) { current.task.cursor = 0; current.task.progress = []; }
      follower.turnId = null;
      return;
    }
    if (payload.type === 'stream.open') {
      follower.turnId = payload.active_turn_id || null;
      follower.turnSequence = Number(payload.active_turn_sequence || 0);
      if (current && current.task.streamTurnId !== follower.turnId) {
        current.task.progress = [];
        current.task.streamTurnId = follower.turnId;
      }
      return;
    }
    if (payload.type === 'text.snapshot') {
      if (payload.turn_id && follower.turnId && payload.turn_id !== follower.turnId) return;
      clearTaskTextStreams(taskId, conversationId);
      for (const stream of payload.streams || []) {
        if (!follower.turnId || stream.turn_id === follower.turnId) {
          applyTaskText(taskId, stream, conversationId, true);
        }
      }
      return;
    }
    if (payload.type === 'task.snapshot') { await applyState(payload); return; }
    const sequence = Number(payload.sequence || 0);
    if (sequence && sequence <= Number(current?.task?.cursor || 0)) return;
    if (current && sequence) current.task.cursor = sequence;
    if (sequence && sequence < follower.turnSequence) return;
    if (payload.progress?.kind === 'turn_started') {
      follower.turnId = payload.turn_id;
      if (current && current.task.streamTurnId !== follower.turnId) {
        current.task.progress = [];
        current.task.streamTurnId = follower.turnId;
      }
      bindLatestInputToTask(conversationId, taskId, payload.turn_id);
    }
    // 进度事件只属于当前跟踪的 Turn；避免旧事件影响当前按钮状态。
    if (payload.turn_id && follower.turnId && payload.turn_id !== follower.turnId) return;
    if (['text_delta', 'text_state'].includes(payload.progress?.kind)) {
      applyTaskText(taskId, {...payload.progress, turn_id: payload.turn_id}, conversationId);
      return;
    }
    if (payload.progress) appendTaskProgress(taskId, payload, conversationId);

    // 一次性绑定：只要本轮 Turn 收到终态进度事件，就在前端视角中结束它，
    // 立刻收起取消按钮，而不是完全依赖后端何时更新 turn.settled。
    if (payload.progress && ['turn_completed', 'turn_failed', 'turn_suspended'].includes(payload.progress.kind)) {
      const active = getActiveConversation();
      if (active && active.id === conversationId && active.activeTaskId === taskId) {
        const cancelButton = document.getElementById('cancel-btn');
        if (cancelButton) {
          // 有些终态事件可能不携带 turn_id，此时以当前 follower.turnId 为准，
          // 避免因为缺少 turn_id 导致按钮一直不收起。
          const effectiveTurnId = payload.turn_id || follower.turnId || '';
          const currentTurnId = cancelButton.dataset.turnId || '';
          // 仅在按钮还指向当前 Turn，或事件缺少明确 turnId（视为当前 Turn）时收起。
          if (!currentTurnId || !effectiveTurnId || currentTurnId === String(effectiveTurnId)) {
            cancelButton.classList.add('hidden');
            cancelButton.disabled = false;
            cancelButton.dataset.taskId = '';
            cancelButton.dataset.turnId = '';
          }
        }
      }
    }
  };
  const connect = () => {
    if (!currentFollower()) return;
    const current = taskMessage(conversationId, taskId) || card;
    const after = Number(current?.task?.cursor || 0);
    const source = new EventSource(`/stream/${taskId}?after=${after}`);
    follower.source = source;
    source.onmessage = event => {
      if (!currentFollower() || follower.source !== source) return;
      let payload;
      try { payload = JSON.parse(event.data); }
      catch (error) { console.error(error); return; }
      follower.attempts = 0;
      follower.queue = follower.queue.then(() => accept(payload)).catch(error => console.error(error));
    };
    source.onerror = () => {
      source.close();
      if (!currentFollower() || follower.source !== source) return;
      follower.attempts += 1;
      if (follower.attempts > MAX_STREAM_RETRIES) {
        // One recovery read after bounded reconnect failures. Healthy streams
        // never issue periodic GET /tasks requests.
        fetch(`/tasks/${taskId}`).then(response => response.ok ? response.json() : null)
          .then(state => { if (state && currentFollower()) return applyState(state); })
          .catch(error => console.error(error)).finally(() => {
            if (currentFollower()) {
              appendTaskProgress(taskId, {progress: {activity: '实时输出连接已断开；任务仍在后台运行，可刷新页面查看结果。'}}, conversationId);
              follower.stop(); if (conversationId === activeConversationId) setLoading(false);
            }
          });
        return;
      }
      const delay = Math.min(1000 * 2 ** (follower.attempts - 1), 15000);
      follower.timer = setTimeout(connect, delay);
    };
  };
  connect();
}

async function loadSessions() {
  // Conversations live in Runtime, not in this tab. Rebuilding them from the
  // durable Session records is what makes a reload or a server restart keep the
  // history instead of starting from an empty page.
  try {
    const response = await fetch('/sessions');
    if (!response.ok) return;
    const data = await response.json();
    (data.sessions || []).forEach((item) => {
      const workspace = workspaces.find(
        (candidate) => candidate.path === item.workspace_path,
      );
      if (!workspace || getConversationById(item.session_id)) return;
      conversations.push({
        id: item.session_id,
        workspaceId: workspace.id,
        title: item.title || '历史会话',
        createdAt: new Date(item.updated_at).toLocaleTimeString(),
        messages: [],
        draft: '',
        loaded: false,
        latestTaskId: item.latest_task_id,
        modelProfileId: item.model_profile_id || defaultModelProfile,
        reasoningEffort: item.reasoning_effort || null,
      });
    });
  } catch (error) {
    console.error(error);
  }
}

async function ensureConversationLoaded(conversationId) {
  const conversation = getConversationById(conversationId);
  if (!conversation || conversation.loaded) return false;
  conversation.loaded = true;
  const loadGeneration = (conversation.historyLoadGeneration || 0) + 1;
  conversation.historyLoadGeneration = loadGeneration;
  const selectionGeneration = conversation.modelSelectionGeneration || 0;
  const restoreSelection = !conversation.modelSelectionPending;
  try {
    const response = await fetch(`/sessions/${conversationId}`);
    if (!response.ok) {
      throw new Error(`历史记录接口返回 ${response.status}`);
    }
    const data = await response.json();
    // A history request started before a Turn finished may return afterwards.
    // Only the newest refresh can replace the transcript or reopen a follower.
    if (conversation.historyLoadGeneration !== loadGeneration) return false;
    if (restoreSelection && !conversation.modelSelectionPending
        && selectionGeneration === (conversation.modelSelectionGeneration || 0)) {
      conversation.modelProfileId = data.model_profile_id || defaultModelProfile;
      conversation.reasoningEffort = data.reasoning_effort || null;
    }
    const tasksById = new Map(
      (data.tasks || []).map((task) => [task.task_id, task]),
    );
    // This is the session authority for what may be stopped from the composer;
    // rendered historical cards are deliberately not an authority signal.
    // Set it before rendering, because rendering also refreshes composer state.
    conversation.activeTaskId = data.active_task_id || null;
    const turns = data.turns || [];
    // Stopped is not completed: an interrupted Turn may have no answer.
    // Preserve its Session settlement flag so its trace stays under the input.
    // Cancellation visibility follows the Task state independently.
    conversation.turnCalls = new Map(
      turns
        .filter((turn) => turn && turn.turn_id)
        .map((turn) => [turn.turn_id, turn]),
    );
    const lastTurnByTask = new Map();
    (data.turns || []).forEach((turn) => {
      if (turn?.task_id && turn.turn_id) {
        lastTurnByTask.set(turn.task_id, turn.turn_id);
      }
    });
    const timeline = [];
    const localImageFallbackRows = [];
    let insertionOrder = 0;
    const visibleAssistantTaskIds = new Set();
    (data.messages || []).forEach((message) => {
      const task = tasksById.get(message.task_id);
      if (message.role === 'assistant' && message.task_id) {
        visibleAssistantTaskIds.add(message.task_id);
      }
      // The opening input of a Task sorts at its attachment so it stays above
      // the Task card. Every later Turn's input keeps its own event position:
      // clamping them all to the attachment is what stacked every question at
      // the top of the conversation while the answers sat at the bottom.
      const sequence = message.role === 'user' && task && message.first_turn_input
        ? Math.min(
            Number(message.source_event_sequence),
            Number(task.attached_sequence),
          )
        : Number(message.source_event_sequence);
      const restoredMessage = {
        role: message.role, content: message.content,
        images: Array.isArray(message.images) ? message.images : [],
        taskId: message.task_id || null,
        turnId: message.turn_id || null,
        firstTurnInput: Boolean(message.first_turn_input),
      };
      if (!Array.isArray(message.images)) localImageFallbackRows.push(restoredMessage);
      timeline.push({
        sequence, order: message.role === 'user' ? 0 : 2,
        insertionOrder: insertionOrder++,
        message: restoredMessage,
      });
    });
    // Pending approvals are actionable session state, not transcript history.
    // Keep them at the bottom of the conversation so a user never has to find
    // a decision inside an old Task card or an earlier Turn's trace.
    const pendingApprovals = [];
    (data.tasks || []).forEach((task) => {
      timeline.push({
        sequence: Number(task.attached_sequence || 0),
        order: 1,
        insertionOrder: insertionOrder++,
        message: {
          role: 'task', content: '', task: {
            taskId: task.task_id, goal: task.goal,
            projection: task.projection || null,
            todoList: task.todo_list || null,
            phase1State: task.phase1_state, status: task.status,
            cursor: Number(task.progress_cursor || 0), progress: [],
            waiting: task.waiting || null,
          },
        },
      });
      // A Session result may be unavailable in older data or after a process
      // restart, while the Task's durable llm.completed text is still present.
      // Use it as a recovery-only assistant message; normal Session messages
      // win whenever they already exist.
      if (task.assistant_text && !visibleAssistantTaskIds.has(task.task_id)) {
        timeline.push({
          sequence: Number(task.attached_sequence || 0),
          order: 2,
          insertionOrder: insertionOrder++,
          message: {
            role: 'assistant', content: task.assistant_text, images: [],
            taskId: task.task_id,
            turnId: lastTurnByTask.get(task.task_id) || null,
          },
        });
      }
      // A Task can span many Turns. The approval is deliberately not inserted
      // in that historical timeline: it is rendered as an actionable footer
      // below every conversation item after sorting.
      if (task.waiting?.kind === 'APPROVAL' && task.waiting.approval) {
        pendingApprovals.push({
          role: 'approval', content: '', taskId: task.task_id,
          turnId: task.waiting.approval.turn_id || null,
          approval: task.waiting.approval, conversationId,
        });
      } else if (task.waiting?.request_id) {
        pendingApprovals.push({
          role: 'clarification', content: '', taskId: task.task_id,
          clarification: task.waiting, conversationId,
        });
      }
    });
    timeline.sort((left, right) => (
      left.sequence - right.sequence
      || left.order - right.order
      || left.insertionOrder - right.insertionOrder
    ));

    // Durable images survive a cold load. Only older servers that omit the
    // field need local attachment recovery; an explicit empty array wins too.
    const previousMessages = Array.isArray(conversation.messages)
      ? conversation.messages
      : [];

    const rebuiltMessages = [
      ...timeline.map((item) => item.message),
      ...pendingApprovals,
    ];

    // Prefer Turn identity. A new local input has no ids yet and belongs to
    // the newest matching row, not the oldest repeated prompt.
    previousMessages.forEach((previous) => {
      if (!previous?.images || !previous.images.length) return;
      const role = previous.role || '';
      const content = previous.content || '';
      const images = previous.images;

      const candidates = localImageFallbackRows.filter((message) => (
        message
        && (message.role || '') === role
        && (message.content || '') === content
        && (!message.images || !message.images.length)
        && (!previous.taskId || message.taskId === previous.taskId)
        && (!previous.turnId || message.turnId === previous.turnId)
      ));
      const target = previous.turnId ? candidates[0] : candidates[candidates.length - 1];

      if (target) {
        target.images = images;
      }
    });

    previousMessages.forEach(previous => {
      if (!previous.textPresentation) return;
      const target = rebuiltMessages.find(message => message.role === 'assistant'
        && message.taskId === previous.taskId && message.turnId === previous.turnId
        && message.content === previous.content);
      if (target) target.textPresentation = previous.textPresentation;
    });
    conversation.messages = rebuiltMessages;
    renderConversations();
    renderMessages();

    // Only the Session's authoritative active Task may auto-follow. Older
    // WAITING Tasks remain visible as pending cards but do not reopen an SSE
    // stream and cannot replay their historical progress into a newer Task.
    const activeTask = tasksById.get(data.active_task_id);
    if (
      activeTask
      && !activeTask.waiting
      && ['PREPARING', 'RUNNING'].includes(activeTask.phase1_state)
    ) {
      setLoading(true);
      followTask(activeTask.task_id, conversationId);
    }
    return true;
  } catch (error) {
    if (conversation.historyLoadGeneration === loadGeneration) {
      conversation.loaded = false;
      if (conversationId === activeConversationId) {
        showError(`会话历史加载失败：${error.message || '请重试'}`);
      }
    }
    console.error(error);
    return false;
  }
}

// A finished Turn is only fully described by the Session record: each answer
// gets its own Turn id and call panel there. Rebuilding from that record after
// a Turn settles is what keeps the panel visible without a manual reload.
async function refreshConversationHistory(conversationId) {
  const conversation = getConversationById(conversationId);
  if (!conversation) return false;
  conversation.loaded = false;
  return ensureConversationLoaded(conversationId);
}

async function loadWorkspaces() {
  try {
    await loadModelProfiles();
    const response = await fetch('/workspaces');
    if (!response.ok) {
      throw new Error('加载工作区列表失败');
    }

    const data = await response.json();
    workspaces.splice(0, workspaces.length, ...data.workspaces);
    await loadSessions();

    const restoredWorkspace = workspaces.find((workspace) => workspace.id === activeWorkspaceId);

    if (restoredWorkspace) {
      activateWorkspace(restoredWorkspace.id);
      return;
    }

    if (workspaces.length > 0) {
      activateWorkspace(workspaces[0].id);
      return;
    }
  } catch (error) {
    console.error(error);
    showError(error.message || '无法加载工作区列表。');
  }

  updateWorkspaceState();
  renderConversations();
  renderMessages();
}

let promptComposer = null;
let imeComposing = false;

// 简单的斜杠命令联想菜单：在输入框前缀为 "/" 时弹出可选命令列表。
// 只做前端提示与前缀填充，不改变现有 /steer 路由和 Runtime 逻辑。
const slashCommands = [
  { prefix: '/steer ', label: '/steer <补充要求>', description: '补充当前运行中的 Task 偏好或约束' },
  { prefix: '/queue ', label: '/queue <后续任务>', description: '在当前 Task 完成后排队一个新的任务' },
  { prefix: '/redirect ', label: '/redirect <新目标>', description: '替换当前 Task 目标，保留已提交修改与工具结果' },
  { prefix: '/followup ', label: '/followup steer|queue|auto', description: '固定或恢复运行中输入的默认模式' },
  { prefix: '/skill ', label: '/skill <名称>', description: '列出已发现的 Skill，选中后引用它', skillMenu: true },
];

let slashMenuElement = null;
let slashMenuActiveIndex = -1;
// 当前菜单实际展示的条目（命令或 Skill）；方向键与 Enter 以它为准，避免过滤后索引错位。
let slashMenuItems = [];

// Skill 联想的触发前缀：输入 "/skill" 或 "/skill 关键词" 时列出已发现的 Skill。
// 数据来自 /settings/skills；选中后插入一段提示模型先用 skill.load 读 SKILL.md 的文本。
const SKILL_MENU_PREFIX = '/skill';
let skillMenuSkills = null;
let skillMenuLoading = false;
let skillMenuError = '';

function isSkillMenuMode(value) {
  return value === SKILL_MENU_PREFIX || value.startsWith(`${SKILL_MENU_PREFIX} `);
}

function skillSourceLabel(source) {
  if (source === 'builtin') return '内置';
  if (source === 'user') return '用户目录';
  return '代码注册';
}

async function loadSkillMenuSkills() {
  if (skillMenuLoading || skillMenuSkills !== null) return;
  skillMenuLoading = true;
  try {
    const response = await fetch('/settings/skills');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    skillMenuSkills = (data.skills || []).map(skill => ({
      name: skill.name,
      description: skill.description || '',
      source: skill.source || '',
    }));
    skillMenuError = '';
  } catch (error) {
    skillMenuError = String((error && error.message) || error);
  } finally {
    skillMenuLoading = false;
  }
  // 加载完成时如果用户还停在 /skill 前缀，就地刷新菜单。
  if (promptComposer && isSkillMenuMode(promptComposer.value)) {
    renderSlashMenu(promptComposer);
  }
}

function resolveSkillMenuItems(query) {
  if (skillMenuSkills === null) {
    if (!skillMenuLoading) loadSkillMenuSkills();
    return [{ kind: 'info', label: '正在读取可用 Skill…', description: '首次打开会加载一次，之后使用缓存', badge: '' }];
  }
  if (skillMenuError) {
    return [{ kind: 'info', label: '读取 Skill 列表失败', description: skillMenuError, badge: '' }];
  }
  const needle = query.toLowerCase();
  const matched = skillMenuSkills.filter(skill => !needle
    || skill.name.toLowerCase().includes(needle)
    || skill.description.toLowerCase().includes(needle));
  if (matched.length === 0) {
    return [{ kind: 'info', label: '没有匹配的 Skill', description: '换个关键词，或先在设置里安装 / 启用 Skill', badge: '' }];
  }
  return matched.map(skill => ({
    kind: 'skill', label: skill.name, description: skill.description, badge: skillSourceLabel(skill.source), skill,
  }));
}

function resolveSlashMenuItems(value) {
  if (isSkillMenuMode(value)) {
    return resolveSkillMenuItems(value.slice(SKILL_MENU_PREFIX.length).trim());
  }
  const query = value.slice(1).trim().toLowerCase();
  return slashCommands
    .filter(command => !query || command.prefix.slice(1).trim().toLowerCase().startsWith(query))
    .map(command => ({ kind: 'command', label: command.label, description: command.description, badge: '', command }));
}

function ensureSlashMenu() {
  if (slashMenuElement) return slashMenuElement;
  const menu = document.createElement('div');
  menu.id = 'slash-command-menu';
  menu.style.position = 'absolute';
  menu.style.zIndex = '30';
  menu.style.background = 'white';
  menu.style.border = '1px solid #e5e7eb';
  menu.style.boxShadow = '0 4px 12px rgba(15,23,42,0.12)';
  menu.style.borderRadius = '8px';
  menu.style.padding = '4px 0';
  menu.style.fontSize = '13px';
  menu.style.minWidth = '240px';
  menu.style.maxHeight = '240px';
  menu.style.overflowY = 'auto';
  menu.style.display = 'none';

  document.body.appendChild(menu);
  slashMenuElement = menu;
  return menu;
}

function hideSlashMenu() {
  if (!slashMenuElement) return;
  slashMenuElement.style.display = 'none';
  slashMenuActiveIndex = -1;
}

function renderSlashMenu(anchorInput) {
  const menu = ensureSlashMenu();
  menu.innerHTML = '';
  slashMenuActiveIndex = -1;

  slashMenuItems = resolveSlashMenuItems(anchorInput.value);
  if (slashMenuItems.length === 0) {
    // 没有匹配项时收起菜单，避免留下空白浮层。
    menu.style.display = 'none';
    return;
  }

  slashMenuItems.forEach((entry, index) => {
    const item = document.createElement('div');
    item.style.padding = '6px 10px';
    item.style.cursor = 'pointer';
    item.style.display = 'flex';
    item.style.flexDirection = 'column';
    item.style.gap = '2px';
    item.dataset.index = String(index);

    const title = document.createElement('div');
    title.textContent = entry.label;
    title.style.fontFamily = 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace';
    title.style.fontSize = '12px';
    title.style.color = '#0f172a';

    if (entry.badge) {
      const badge = document.createElement('span');
      badge.textContent = entry.badge;
      badge.style.marginLeft = '6px';
      badge.style.fontSize = '11px';
      badge.style.color = '#94a3b8';
      title.appendChild(badge);
    }

    const desc = document.createElement('div');
    desc.textContent = entry.description;
    desc.style.color = '#64748b';

    item.appendChild(title);
    item.appendChild(desc);

    item.addEventListener('mouseenter', () => {
      highlightSlashMenuItem(index);
    });

    item.addEventListener('mousedown', (event) => {
      // 使用 mousedown 提前阻止失焦，再由 click 行为完成填充。
      event.preventDefault();
    });

    item.addEventListener('click', () => {
      applySlashMenuItem(entry, anchorInput);
    });

    menu.appendChild(item);
  });

  const rect = anchorInput.getBoundingClientRect();
  menu.style.left = `${rect.left}px`;
  // 默认在输入框下方显示菜单。
  let top = rect.bottom + 4;
  menu.style.top = `${top}px`;
  menu.style.display = 'block';

  // 如果下方空间不足，自动改为显示在输入框上方，并做简单的视口边界收紧。
  const menuRect = menu.getBoundingClientRect();
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || menuRect.bottom;
  if (top + menuRect.height > viewportHeight - 8) {
    top = rect.top - menuRect.height - 4;
    if (top < 8) {
      top = 8;
    }
    menu.style.top = `${top}px`;
  }
}

function highlightSlashMenuItem(index) {
  if (!slashMenuElement) return;
  const children = Array.from(slashMenuElement.children);
  children.forEach((child, i) => {
    const el = child;
    if (!(el instanceof HTMLElement)) return;
    if (i === index) {
      el.style.background = '#f1f5f9';
    } else {
      el.style.background = 'transparent';
    }
  });
  slashMenuActiveIndex = index;
}

function moveSlashMenuSelection(delta) {
  if (!slashMenuElement || slashMenuElement.style.display === 'none') return;
  const total = slashMenuItems.length;
  if (total === 0) return;
  let next = slashMenuActiveIndex;
  if (next < 0) {
    next = delta > 0 ? 0 : total - 1;
  } else {
    next = (next + delta + total) % total;
  }
  highlightSlashMenuItem(next);
}

function applySlashCommand(command, input) {
  const prefix = command.prefix;
  const value = input.value;
  const selectionStart = input.selectionStart ?? 0;
  const selectionEnd = input.selectionEnd ?? selectionStart;
  const isAtPrefix = selectionStart <= 1 && selectionEnd <= 1 && value.startsWith('/');

  let nextValue;
  let cursorPos;
  if (isAtPrefix) {
    // 直接用完整命令替换单个 "/"。
    nextValue = prefix;
    cursorPos = prefix.length;
  } else {
    // 在当前位置插入命令前缀。
    nextValue = value.slice(0, selectionStart) + prefix + value.slice(selectionEnd);
    cursorPos = selectionStart + prefix.length;
  }

  input.value = nextValue;
  input.selectionStart = cursorPos;
  input.selectionEnd = cursorPos;
  persistActiveDraft();
  hideSlashMenu();
}

function skillMentionText(name) {
  return `使用 Skill「${name}」：先用 skill.load 读取 SKILL.md，再按其中的步骤执行。`;
}

// 选中 Skill 后把 "/skill 关键词" 这段触发文本替换成引用句，光标停在句尾方便继续描述任务。
function applySkillMention(skill, input) {
  const mention = skillMentionText(skill.name);
  const value = input.value;
  const selectionStart = input.selectionStart ?? 0;
  const selectionEnd = input.selectionEnd ?? selectionStart;
  let nextValue;
  let cursorPos;
  if (isSkillMenuMode(value)) {
    nextValue = mention;
    cursorPos = mention.length;
  } else {
    nextValue = value.slice(0, selectionStart) + mention + value.slice(selectionEnd);
    cursorPos = selectionStart + mention.length;
  }

  input.value = nextValue;
  input.selectionStart = cursorPos;
  input.selectionEnd = cursorPos;
  persistActiveDraft();
  hideSlashMenu();
  if (typeof input.focus === 'function') input.focus();
}

function applySlashMenuItem(entry, input) {
  if (!entry) return;
  if (entry.kind === 'command') {
    applySlashCommand(entry.command, input);
    // "/skill" 命令执行后直接进入 Skill 列表模式，不必再等一次键盘输入。
    if (entry.command.skillMenu && isSkillMenuMode(input.value)) renderSlashMenu(input);
    return;
  }
  if (entry.kind === 'skill') {
    applySkillMention(entry.skill, input);
    return;
  }
  hideSlashMenu();
}
 
// 等页面 DOM 就绪后再绑定到真正的输入框，避免脚本在 textarea 挂载前执行导致 promptComposer 为空。
window.addEventListener('DOMContentLoaded', () => {
  promptComposer = document.getElementById('prompt');

  if (promptComposer) {
    promptComposer.addEventListener('input', () => {
      persistActiveDraft();

      // 文本层级触发斜杠菜单：以 "/" 开头时弹出命令列表，输入 "/skill 关键词" 时改为列出可用 Skill。
      const value = promptComposer.value;

      if (value.startsWith('/')) {
        renderSlashMenu(promptComposer);
      } else {
        // 离开斜杠前缀时自动收起菜单，避免一直占屏。
        hideSlashMenu();
      }
    });

    promptComposer.addEventListener('compositionstart', () => {
      imeComposing = true;
    });

    promptComposer.addEventListener('compositionend', () => {
      imeComposing = false;
      persistActiveDraft();
    });

    promptComposer.addEventListener('keydown', (event) => {
      const composing = imeComposing || event.isComposing || event.keyCode === 229;

      // 输入 "/" 时弹出斜杠命令联想菜单。
      if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey) {
        const value = promptComposer.value;
        const selectionStart = promptComposer.selectionStart ?? 0;
        const selectionEnd = promptComposer.selectionEnd ?? selectionStart;
        const isPrefix = selectionStart <= 1 && selectionEnd <= 1;
        const startsWithSlash = value.startsWith('/');
        if (isPrefix || startsWithSlash) {
          setTimeout(() => {
            renderSlashMenu(promptComposer);
          }, 0);
        }
        return;
      }

      // 斜杠命令补全：在输入框开头输入 "/s"，按 Tab 自动扩展成 "/steer "。
      if (event.key === 'Tab' && !composing) {
        const value = promptComposer.value;
        const selectionStart = promptComposer.selectionStart ?? 0;
        const selectionEnd = promptComposer.selectionEnd ?? selectionStart;
        const isPrefix = selectionStart <= 2 && selectionEnd <= 2;
        if (value.startsWith('/s') && isPrefix) {
          event.preventDefault();
          const completed = '/steer ';
          promptComposer.value = completed + value.slice(2);
          const cursor = completed.length;
          promptComposer.selectionStart = cursor;
          promptComposer.selectionEnd = cursor;
          persistActiveDraft();
          return;
        }
        return;
      }

      // 斜杠菜单中使用方向键与 Enter 选择命令。
      if (!composing && slashMenuElement && slashMenuElement.style.display === 'block') {
        if (event.key === 'ArrowDown') {
          event.preventDefault();
          moveSlashMenuSelection(1);
          return;
        }
        if (event.key === 'ArrowUp') {
          event.preventDefault();
          moveSlashMenuSelection(-1);
          return;
        }
        if (event.key === 'Enter' && slashMenuActiveIndex >= 0) {
          event.preventDefault();
          const entry = slashMenuItems[slashMenuActiveIndex];
          if (entry) applySlashMenuItem(entry, promptComposer);
          return;
        }
        if (event.key === 'Escape') {
          hideSlashMenu();
          return;
        }
      }

      // 保持原有 Enter 发送逻辑：在非组合态、非 Shift 的情况下发送任务。
      if (event.key !== 'Enter' || composing) {
        return;
      }
      if (event.shiftKey) {
        return;
      }

      event.preventDefault();
      persistActiveDraft();
      runTask();
    });
  } else {
    console.error('未找到消息输入框，仍继续加载工作区与会话。');
  }
});

async function loadDiagnostics() {
  try {
    const response = await fetch('/diagnostics');
    if (!response.ok) {
      return;
    }
    const data = await response.json();
    const notes = [];
    if (data.runtime_error) {
      notes.push('Runtime 启动失败：' + data.runtime_error);
    }
    for (const issue of (data.configuration_issues || [])) {
      notes.push('配置提示：' + issue);
    }
    for (const server of (data.mcp_servers || [])) {
      if (server.state === 'failed') {
        notes.push('MCP ' + server.id + ' 暂不可用（聊天仍可用）：' + server.detail);
      }
    }
    const banner = document.getElementById('config-banner');
    if (banner) {
      banner.textContent = notes.join('\\n');
      banner.classList.toggle('visible', notes.length > 0);
    }
    if ((data.mcp_servers || []).some(server => server.state === 'connecting')) {
      setTimeout(loadDiagnostics, 2000);
    }
  } catch (error) {
    console.error(error);
  }
}

setupWorkspaceResizer();
loadDiagnostics();
loadWorkspaces();
""" + """
</script>
</body>
</html>
""".replace("__WEBUI_VERSION__", WEBUI_VERSION)

runtime_server: LocalEventApiServer | None = None
runtime_error: str | None = None
configuration_issues: list[str] = []
workspace_registry: list[dict[str, str]] = []
session_workspace_bindings: dict[str, str] = {}
# Removal is an explicit deny-list rather than "absent from the index": the
# Runtime keeps owning the Session and Task records, and the startup recovery
# below rebuilds the index from them, so absence alone would resurrect entries.
removed_workspace_paths: set[str] = set()
removed_session_ids: set[str] = set()
turn_request_diagnostics: Any | None = None

# These runs are deliberately Web-local: the evaluated Agent Tasks themselves
# are durable Runtime records, while this small index only groups independent
# sample sessions for the diagnostics screen.  An evaluation never joins the
# user's active Session or contributes assistant text to its prompt context.
source_evaluation_runs: dict[str, dict[str, Any]] = {}

_SOURCE_EVALUATION_CASES: tuple[dict[str, str], ...] = (
    {
        "id": "codex-input-routing",
        "title": "Codex：输入与 turn 路径",
        "prompt": (
            "分析 openai/codex 源码：普通用户输入如何进入 turn，"
            "是否存在独立的输入意图识别提示词工程。请先定位官方仓库并读取"
            "对应实现文件，再区分源码事实与推断。"
        ),
    },
    {
        "id": "hermes-completion-loop",
        "title": "Hermes：普通 loop 与完成判断",
        "prompt": (
            "分析 NousResearch/hermes-agent 源码：普通对话 loop 如何决定继续"
            "调用工具或结束，是否有 completion review/goal judge。请先定位官方"
            "仓库并读取 loop、prompt 或 judge 的实现文件，再给出源码证据。"
        ),
    },
)


def _root_cause_message(error: BaseException) -> str:
    """Return the deepest chained message so a config fault names its variable."""
    current: BaseException = error
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, MCPServerStartupError):
            return str(current)
        cause = current.__cause__
        if cause is None and not current.__suppress_context__:
            cause = current.__context__
        if cause is None:
            break
        current = cause
    return str(current) or current.__class__.__name__


def workspace_registry_path() -> Path:
    return Path.cwd() / ".agent" / "web-workspaces.json"


def workspace_identifier(path: str) -> str:
    """Derive the id from the path itself.

    A positional id like ``str(len(registry) + 1)`` is only valid inside the
    process that produced it: after a restart the same number can name a
    different directory, so a stored client selection would silently point at
    the wrong workspace. Hashing the path keeps the id stable and collision-free
    across restarts.
    """
    return sha256(path.encode("utf-8")).hexdigest()[:12]


def load_workspace_registry() -> None:
    """Restore registered directories so client-side selections survive restart."""
    workspace_registry.clear()
    try:
        raw = json.loads(workspace_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, list):
        return
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        if not path or not Path(path).is_dir():
            # A directory that disappeared must not stay selectable.
            continue
        workspace_registry.append({
            "id": workspace_identifier(path),
            "name": str(item.get("name") or Path(path).name or path),
            "path": path,
            **({"last_used_at": item["last_used_at"]} if isinstance(item.get("last_used_at"), str) else {}),
        })


def save_workspace_registry() -> None:
    target = workspace_registry_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(workspace_registry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        # A read-only workspace still runs; it just cannot remember selections.
        return


def session_workspace_bindings_path() -> Path:
    return Path.cwd() / ".agent" / "web-session-workspaces.json"


def load_session_workspace_bindings() -> None:
    session_workspace_bindings.clear()
    try:
        raw = json.loads(
            session_workspace_bindings_path().read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    for session_id, workspace_id in raw.items():
        if isinstance(session_id, str) and isinstance(workspace_id, str):
            session_workspace_bindings[session_id] = workspace_id


def web_removals_path() -> Path:
    return Path.cwd() / ".agent" / "web-removals.json"


def load_web_removals() -> None:
    removed_workspace_paths.clear()
    removed_session_ids.clear()
    try:
        raw = json.loads(web_removals_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    for path in raw.get("workspaces") or []:
        if isinstance(path, str):
            removed_workspace_paths.add(path)
    for session_id in raw.get("sessions") or []:
        if isinstance(session_id, str):
            removed_session_ids.add(session_id)


def save_web_removals() -> None:
    target = web_removals_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "workspaces": sorted(removed_workspace_paths),
                    "sessions": sorted(removed_session_ids),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        # A read-only workspace still runs; it just cannot remember removals.
        return


def save_session_workspace_bindings() -> None:
    target = session_workspace_bindings_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(session_workspace_bindings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def bind_session_to_workspace(session_id: str, workspace_id: str) -> None:
    existing = session_workspace_bindings.get(session_id)
    if existing is not None and existing != workspace_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "当前会话已绑定其他工作区，禁止跨工作区复用 session。"
            ),
        )
    if existing is None:
        session_workspace_bindings[session_id] = workspace_id
        save_session_workspace_bindings()


def recover_workspace_registry_from_runtime() -> None:
    """Rebuild missing workspace entries from durable Task ownership.

    The JSON file is only a UI index; Runtime Task records are authoritative.
    If the file is deleted, stale, or corrupted, hiding every Session would make
    durable history appear lost even though SQLite still has it.
    """
    if runtime_server is None:
        return
    known = {item["path"] for item in workspace_registry}
    kernel = runtime_server._client.application.kernel
    try:
        sessions = runtime_server._call(kernel.list_sessions())
    except Exception:
        return
    changed = False
    for session in sorted(sessions, key=lambda item: item.updated_at, reverse=True):
        for task_id in reversed(session.task_ids):
            try:
                task = runtime_server._call(kernel.get_task(task_id))
            except (LookupError, PermissionError):
                continue
            path = str(task.workspace)
            if path in removed_workspace_paths:
                continue
            if path in known:
                existing = next(item for item in workspace_registry if item["path"] == path)
                if "last_used_at" not in existing:
                    existing["last_used_at"] = session.updated_at.isoformat()
                    changed = True
                break
            if not Path(path).is_dir():
                continue
            workspace_registry.append({
                "id": workspace_identifier(path),
                "name": Path(path).name or path,
                "path": path,
                "last_used_at": session.updated_at.isoformat(),
            })
            known.add(path)
            changed = True
            break
    if changed:
        save_workspace_registry()


@app.on_event("startup")
async def startup() -> None:
    global runtime_server, runtime_error, configuration_issues, turn_request_diagnostics
    load_workspace_registry()
    load_session_workspace_bindings()
    load_web_removals()
    runtime_server = LocalEventApiServer(Path.cwd())
    try:
        runtime_server.start()
        turn_request_diagnostics = configure_request_diagnostics(
            runtime_server._client.application
        )
        recover_workspace_registry_from_runtime()
        runtime_error = None
        configuration_issues = list(getattr(
            runtime_server._client.application, "configuration_issues", ()
        ))
    except Exception as error:
        runtime_server = None
        configuration_issues = []
        runtime_error = _root_cause_message(error)


@app.on_event("shutdown")
async def shutdown() -> None:
    global turn_request_diagnostics
    turn_request_diagnostics = None
    if runtime_server is not None:
        runtime_server.stop()


@app.get("/")
async def index() -> HTMLResponse:
    response = HTMLResponse(INDEX_HTML)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-WebUI-Version"] = WEBUI_VERSION
    return response


@app.get("/workspaces")
async def list_workspaces() -> dict:
    if runtime_server is None:
        return {
            "workspaces": [
                {**item, "trust_level": ProjectTrustLevel.UNTRUSTED.value}
                for item in workspace_registry
            ]
        }
    async def enrich_on_runtime_thread() -> list[dict[str, str]]:
        kernel = runtime_server._client.application.kernel
        enriched: list[dict[str, str]] = []
        for item in workspace_registry:
            try:
                binding = await kernel.get_project_trust(Path(item["path"]))
                enriched.append({**item, "trust_level": binding.level.value})
            except Exception as error:
                # Trust is optional display metadata. A corrupt/stale trust
                # record must never make the workspace registry unavailable,
                # because that would also hide every durable Session in UI.
                logger.warning(
                    "workspace trust lookup failed for %s: %s",
                    item["path"], error,
                )
                enriched.append({
                    **item,
                    "trust_level": ProjectTrustLevel.UNTRUSTED.value,
                })
        return enriched

    return {"workspaces": runtime_server._call(enrich_on_runtime_thread())}


@app.get("/diagnostics")
async def diagnostics() -> dict:
    """Surface configuration faults instead of showing an empty page.

    A fatal fault (for example an invalid egress mode or a missing model key)
    leaves ``runtime_error`` set; optional-search faults degrade the Runtime and
    arrive as non-fatal ``configuration_issues``.
    """
    servers = []
    if runtime_server is not None:
        # Observe the running instance; do not start extra MCP subprocesses.
        servers = await asyncio.to_thread(
            runtime_server._call, mcp_runtime_status(runtime_server._client.application),
        )
    return {
        "runtime_error": runtime_error,
        "configuration_issues": list(configuration_issues),
        "mcp_servers": servers,
    }


def register_workspace(raw_path: str) -> dict[str, str]:
    """Register one existing local directory as a canonical workspace entry."""
    candidate = raw_path.strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="工作区目录不能为空")
    try:
        resolved = Path(candidate).expanduser().resolve(strict=True)
    except OSError as error:
        raise HTTPException(
            status_code=400, detail=f"无法访问所选目录：{error}"
        ) from error
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="所选路径不是目录")

    workspace_path = str(resolved)
    # Selecting a directory is an explicit revival, so the deny-list entry has
    # to go before the early return below: after a removal the registry holds no
    # entry for this path, and keeping the path listed would hide the workspace
    # the user just picked on the very next reload.
    if workspace_path in removed_workspace_paths:
        removed_workspace_paths.discard(workspace_path)
        save_web_removals()
    existing = next(
        (item for item in workspace_registry if item["path"] == workspace_path),
        None,
    )
    if existing is not None:
        touch_workspace(existing)
        return existing

    workspace = {
        "id": workspace_identifier(workspace_path),
        "name": resolved.name or workspace_path,
        "path": workspace_path,
        "last_used_at": datetime.now(timezone.utc).isoformat(),
    }
    workspace_registry.append(workspace)
    save_workspace_registry()
    return workspace


def touch_workspace(workspace: dict) -> None:
    workspace["last_used_at"] = datetime.now(timezone.utc).isoformat()
    save_workspace_registry()


@app.post("/workspaces/{workspace_id}/activate")
async def activate_workspace(workspace_id: str) -> dict:
    workspace = next((item for item in workspace_registry if item["id"] == workspace_id), None)
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    touch_workspace(workspace)
    return workspace


@app.delete("/workspaces/{workspace_id}")
async def remove_workspace(workspace_id: str) -> dict:
    """Remove one workspace from this Web UI's sidebar index.

    The directory and its Runtime Session history are left untouched; only the
    sidebar entry disappears. Because the startup recovery rebuilds the index
    from durable Task ownership, the path is also written to the deny-list or
    it would come back on the next reload.
    """
    workspace = next((item for item in workspace_registry if item["id"] == workspace_id), None)
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    workspace_registry[:] = [
        item for item in workspace_registry if item["id"] != workspace_id
    ]
    removed_workspace_paths.add(str(workspace["path"]))
    save_workspace_registry()
    save_web_removals()
    return {"removed_workspace_id": workspace_id, "path": workspace["path"]}


MAX_IMAGE_DATA_URL_CHARS = 400_000


def _normalize_image_blocks(images: object) -> list[ImageBlock]:
    """Turn the request payload into ImageBlocks, rejecting what cannot be sent.

    Whether a source is acceptable is ``ImageBlock``'s invariant, so this only
    translates its ValueError into an HTTP status. An attachment is never dropped
    quietly: a silently discarded image leaves the model answering a request it
    cannot see, which reads to the user as the assistant ignoring the screenshot.
    """
    if not isinstance(images, list):
        return []
    blocks: list[ImageBlock] = []
    for image in images:
        if not isinstance(image, dict):
            raise HTTPException(
                status_code=400, detail="图片附件格式不正确"
            )
        image_url = str(image.get("image_url", ""))
        if len(image_url) > MAX_IMAGE_DATA_URL_CHARS:
            # An oversized attachment makes the whole model call unanswerable
            # with "context cannot fit", so reject it with an actionable error.
            raise HTTPException(
                status_code=413,
                detail="图片过大，请压缩后重试（单张建议不超过 300KB）",
            )
        try:
            blocks.append(ImageBlock(image_url=image_url))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    return blocks


def choose_workspace_directory() -> str | None:
    """Open the host's native folder chooser and return the picked directory.

    The browser cannot expose a real local absolute path, so the selection has
    to happen in this local process. ``None`` means the user cancelled.
    """
    if sys.platform != "darwin":
        raise HTTPException(
            status_code=501,
            detail="当前系统暂不支持原生目录选择，请在 macOS 上使用此功能",
        )
    try:
        completed = subprocess.run(
            [
                "osascript",
                "-e",
                'POSIX path of (choose folder with prompt "选择工作区目录")',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise HTTPException(
            status_code=503, detail=f"无法启动目录选择器：{error}"
        ) from error

    if completed.returncode != 0:
        message = completed.stderr.strip()
        # AppleScript reports an explicit cancel as error -128.
        if "-128" in message or "User canceled" in message:
            return None
        raise HTTPException(
            status_code=503,
            detail=f"目录选择器无法打开：{message or '未知错误'}",
        )

    selected = completed.stdout.strip()
    return selected or None


@app.post("/workspaces")
async def create_workspace(payload: dict) -> dict:
    return register_workspace(str(payload.get("path", "")))


@app.post("/workspaces/pick")
async def pick_workspace() -> Response:
    """Let the user choose a workspace directory from the native file dialog."""
    selected = await asyncio.to_thread(choose_workspace_directory)
    if selected is None:
        return Response(status_code=204)
    return JSONResponse(content=register_workspace(selected))


@app.post("/workspaces/{workspace_id}/trust")
async def set_workspace_trust(workspace_id: str, payload: dict) -> dict:
    """Persist an explicit workspace trust choice.

    Rejecting trust is a durable read-only choice, not an invisible failure:
    later model-originated commands can still surface a fresh approval request.
    """
    if runtime_server is None:
        raise HTTPException(status_code=503, detail=runtime_error or "runtime unavailable")
    workspace = next(
        (item for item in workspace_registry if item["id"] == workspace_id), None
    )
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    decision = str(payload.get("decision", "")).strip().lower()
    if decision not in {"trust", "reject"}:
        raise HTTPException(status_code=400, detail="decision must be trust or reject")
    level = (
        ProjectTrustLevel.TRUSTED_BUILD
        if decision == "trust" else ProjectTrustLevel.UNTRUSTED
    )
    binding = runtime_server._call(
        runtime_server._client.application.kernel.set_project_trust(
            Path(workspace["path"]), level
        )
    )
    return {**workspace, "trust_level": binding.level.value, "trust": binding.to_data()}


def _require_local_diagnostic_request(request: Request | None) -> None:
    if request is None or request.client is None:
        raise HTTPException(status_code=403, detail='diagnostics require a local request')
    try:
        peer_is_local = ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        peer_is_local = False
    if not peer_is_local or request.url.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise HTTPException(status_code=403, detail='diagnostics require loopback access')
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(status_code=403, detail='diagnostic origin mismatch')


@app.post("/turn-diagnostics/read")
async def read_turn_diagnostics(payload: dict, request: Request) -> Response:
    _require_local_diagnostic_request(request)
    key = str(payload.get('key', ''))
    session_id = str(payload.get('session_id', ''))
    data = turn_request_diagnostics.read(key, session_id) if turn_request_diagnostics else None
    if data is None:
        raise HTTPException(status_code=404, detail='diagnostic unavailable or expired')
    return JSONResponse(content=data, headers={'Cache-Control': 'no-store'})


@app.get("/model-profiles")
async def list_model_profiles() -> dict:
    if runtime_server is None:
        return {"default_profile": None, "profiles": []}
    model = runtime_server._client.application.kernel.dependencies.model
    catalog = getattr(model, "profile_catalog", None)
    return catalog() if catalog else {"default_profile": None, "profiles": []}


@app.post("/sessions/{session_id}/model-profile")
async def update_session_model_profile(session_id: str, payload: dict) -> dict:
    """Save the next Turn's model; active/resumable Turns keep their snapshot."""
    if runtime_server is None:
        raise HTTPException(status_code=503, detail=runtime_error or "runtime unavailable")
    profile_id = payload.get("model_profile_id")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise HTTPException(status_code=400, detail="model_profile_id must be a non-empty string")
    kernel = runtime_server._client.application.kernel
    try:
        snapshot = runtime_server._call(kernel.update_session_model_profile(session_id, profile_id.strip()))
    except LookupError as error:
        raise HTTPException(status_code=404, detail="session not found") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail="session access denied") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"session_id": snapshot.session_id, "model_profile_id": snapshot.model_profile_id,
            "reasoning_effort": snapshot.reasoning_effort, "applies_to": "next_turn"}


@app.post("/session-input")
async def submit_session_input(payload: dict, request: Request = None) -> dict:
    """Web's high-level normal-message ingress; `/tasks` stays low-level."""
    if runtime_server is None:
        raise HTTPException(
            status_code=503,
            detail=runtime_error or "runtime unavailable",
        )
    workspace_id = str(payload.get("workspace_id", "")).strip()
    session_id = str(payload.get("session_id", "")).strip()
    request_id = str(payload.get("request_id", "")).strip()
    text = str(payload.get("text", "")).strip()
    target_task_id_raw = payload.get("target_task_id")
    target_task_id = (
        str(target_task_id_raw).strip() or None
        if target_task_id_raw is not None
        else None
    )
    reasoning_effort_raw = payload.get("reasoning_effort")
    model_profile_id = payload.get("model_profile_id")
    if model_profile_id is not None and (not isinstance(model_profile_id, str) or not model_profile_id.strip()):
        raise HTTPException(status_code=400, detail="model_profile_id must be a non-empty string")
    reasoning_effort = (
        str(reasoning_effort_raw).strip().lower()
        if reasoning_effort_raw is not None
        else None
    ) or None
    workspace = next(
        (item for item in workspace_registry if item["id"] == workspace_id), None
    )
    if workspace is None:
        raise HTTPException(status_code=400, detail="workspace selection is required")
    if not session_id or not request_id or not text:
        raise HTTPException(
            status_code=400,
            detail="session_id, request_id, and text are required",
        )

    bind_session_to_workspace(session_id, workspace_id)
    touch_workspace(workspace)

    async def ensure_session() -> None:
        kernel = runtime_server._client.application.kernel
        try:
            await kernel.get_session(session_id)
        except LookupError:
            await kernel.create_session(text[:120], session_id=session_id)
        if hasattr(kernel, "update_session_model_profile"):
            await kernel.update_session_model_profile(session_id, model_profile_id)
        # Session-level reasoning-effort is a durable preference: when the
        # caller supplies a value, keep the Session as the single source of
        # truth and update its snapshot. Failure must not block the input.
        if "reasoning_effort" in payload:
            try:
                await kernel.update_session_reasoning_effort(
                    session_id, reasoning_effort,
                )
            except ValueError:
                raise
            except Exception:
                logger.exception(
                    "failed to update session reasoning_effort session_id=%s",
                    session_id,
                )

    try:
        runtime_server._call(ensure_session())
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    # Attachments stay structured. Inlining base64 into the text would blow
    # past the Task SPEC goal limit and fail task creation outright.
    image_blocks = _normalize_image_blocks(payload.get("images"))
    intent = _explicit_user_input_intent(payload.get("intent"))
    diagnostic_key = None
    if payload.get('diagnose_request') is True:
        _require_local_diagnostic_request(request)
        if turn_request_diagnostics is None:
            raise HTTPException(status_code=400, detail='当前模型不支持请求诊断')
        try:
            diagnostic_key = turn_request_diagnostics.start(session_id)
        except ValueError as error:
            raise HTTPException(status_code=429, detail=str(error)) from error

    async def submit_with_diagnostics():
        token = turn_request_diagnostics.activate(diagnostic_key) if diagnostic_key else None
        try:
            return await runtime_server._client.submit_user_input(
                session_id,
                text,
                input_id=request_id,
                explicit_intent=intent,
                target_task_id=target_task_id,
                images=tuple(image_blocks),
                workspace=Path(workspace['path']),
                reasoning_effort=reasoning_effort,
            )
        finally:
            if token is not None:
                turn_request_diagnostics.reset(token)

    try:
        result = runtime_server._call(submit_with_diagnostics())
    except AgentLoopLimitExceeded as error:
        if diagnostic_key:
            turn_request_diagnostics.delete(diagnostic_key)
        # A spent call budget is a bounded Runtime outcome with a known cause,
        # not an internal fault. Reporting it as 500 hides the only actionable
        # detail the user has: how much was spent and what blocked last.
        raise HTTPException(
            status_code=409, detail=_describe_loop_limit(error),
        ) from error
    except ValueError as error:
        if diagnostic_key:
            turn_request_diagnostics.delete(diagnostic_key)
        # A rejected workspace is a caller-visible conflict, most often a
        # Session whose earlier Tasks ran in a different directory.
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        if diagnostic_key:
            turn_request_diagnostics.delete(diagnostic_key)
        # Never let an unexpected runtime failure terminate the HTTP response.
        # Browsers surface a dropped response as the opaque "Failed to fetch",
        # which hides the session and task context needed to diagnose it.
        logger.exception(
            "session-input failed session_id=%s workspace_id=%s request_id=%s",
            session_id, workspace_id, request_id,
        )
        raise HTTPException(
            status_code=500,
            detail="session input failed; check server logs for the request id",
            headers={"X-Request-Id": request_id},
        ) from error
    data = dict(result.result)
    if diagnostic_key:
        data['diagnostic_key'] = diagnostic_key
    status = 202 if data.get("kind") == "task" else 200
    return JSONResponse(content=data, status_code=status)


def user_skills_store():
    return user_skills_store_for_application(
        runtime_server._client.application if runtime_server is not None else None
    )

def refresh_skill_catalog(store=None) -> tuple:
    store = store or user_skills_store()
    application = runtime_server._client.application if runtime_server is not None else None
    catalog = skill_catalog_for_application(application, store)
    if runtime_server is None:
        skills = catalog.discover()
        store.issues = list(catalog.issues)
        return skills

    async def refresh():
        registry = runtime_server._client.application.kernel._dependencies.skill_registry
        skills = catalog.refresh(registry)
        store.issues = list(catalog.issues)
        return skills

    return runtime_server._call(refresh())


def skill_data(skill) -> dict:
    return {
        "id": skill.id, "name": skill.name, "description": skill.description,
        "kind": skill.kind.value, "plan_path": skill.plan_path,
        "tags": list(skill.tags), "safety_level": skill.safety_level,
        "approval_required": skill.approval_required,
        "source": skill.source, "overrides": list(skill.overrides),
    }


def skill_install_receipt(skill, store) -> dict:
    """Disk commit is successful even if a subsequent catalog refresh fails."""
    warnings = []
    try:
        refresh_skill_catalog(store)
    except Exception:
        logger.exception("Skill installed but runtime catalog refresh failed: %s", skill.name)
        warnings.append("文件已安装，但运行时目录刷新失败；请稍后刷新设置或重启服务。")
    installation = None
    try:
        installation = start_skill_preparation(store, store.installations.create(skill))
    except Exception:
        logger.exception("Skill files saved but preparation could not start: %s", skill.name)
        warnings.append("文件已保存，但安装准备未启动；请在设置中重试准备。")
    return {
        "installed": True, "scope": "user", "skill": skill_data(skill),
        "plan_path": skill.plan_path, "skill_id": skill.id,
        "skill_name": skill.name, "description": skill.description,
        "warnings": warnings,
        "installation": installation,
        "preparation_required": True,
        "ready": bool(installation and installation["status"] == "ready"),
    }


_skill_install_launches: set[str] = set()


def start_skill_preparation(store, job: dict) -> dict:
    """Dispatch through the normal Runtime, never execute uploaded instructions here."""
    if job["status"] != "pending":
        return job
    if runtime_server is None:
        return store.installations.update(job["id"], summary="文件已保存；运行时不可用，等待重试准备。")
    candidates = [item for item in workspace_registry if Path(item["path"]).is_dir()]
    if not candidates:
        return store.installations.update(job["id"], summary="文件已保存；请先选择工作区，再重试准备。")
    # Legacy entries have no timestamp; use the last registered directory until
    # the user activates a workspace or sends a message.
    workspace = max(enumerate(candidates), key=lambda pair: (pair[1].get("last_used_at", ""), pair[0]))[1]
    session_id = job.get("session_id") or f"session-{uuid4().hex}"
    bind_session_to_workspace(session_id, workspace["id"])
    job = store.installations.update(
        job["id"], status="starting", session_id=session_id,
        workspace_id=workspace["id"], workspace_path=workspace["path"],
        summary="已创建安装会话，正在读取 Skill 并检查准备条件。",
    )
    server = runtime_server
    _skill_install_launches.add(job["id"])

    async def launch():

        try:
            kernel = server._client.application.kernel
            try:
                await kernel.get_session(session_id)
            except LookupError:
                await kernel.create_session(f"安装 Skill：{job['skill_name']}", session_id=session_id)
            result = await server._client.submit_user_input(
                session_id, skill_installation_prompt(job), input_id=job["id"],
                explicit_intent=RuntimeInputIntent.NEW_TASK, workspace=Path(workspace["path"]),
            )
            task_id = (result.result.get("task") or {}).get("task_id") or result.result.get("routed_task_id")
            if not task_id:
                raise RuntimeError("安装会话未返回任务 ID")
            # A fast model may already have reported ready during submission.
            store.installations.update(job["id"], task_id=task_id)
            store.installations.update(job["id"], expected_status="starting", status="running")
        except Exception as error:
            logger.exception("Skill preparation dispatch failed: %s", job["id"])
            store.installations.update(job["id"], status="pending", summary=f"安装会话启动失败，可重试准备：{error}")
        finally:
            _skill_install_launches.discard(job["id"])
            server._client.application.kernel.dependencies.store.notify(session_id, scope="session")

    coroutine = launch()
    try:
        server.submit_background(coroutine)
    except Exception as error:
        coroutine.close()
        _skill_install_launches.discard(job["id"])
        return store.installations.update(job["id"], status="pending", summary=f"安装会话未启动，可重试准备：{error}")
    return store.installations.get(job["id"])


def refresh_skill_installation(store, job: dict) -> dict:
    if runtime_server is None or job["status"] in {"pending", "ready", "blocked", "failed"}:
        return job

    async def status():
        task_id = job.get("task_id")
        if not task_id:
            session = await runtime_server._client.application.kernel.get_session(job["session_id"])
            task_id = session.active_task_id or (session.task_ids[-1] if session.task_ids else None)
        return await runtime_server._client.get_task_result(task_id) if task_id else None

    try:
        result = runtime_server._call(status())
    except LookupError:
        result = None
    if result is None:
        if job["id"] not in _skill_install_launches:
            return store.installations.update(job["id"], status="pending", summary="安装会话未开始，可重试准备。")
        return job
    # The reporting tool is authoritative for readiness. A stopped turn without
    # a receipt must not be mistaken for successful environment verification.
    current = store.installations.get(job["id"])
    if current["status"] in {"ready", "blocked", "failed"}:
        return current
    state = result.status
    summaries = {
        "awaiting_approval": "等待审批，请打开安装会话处理。",
        "awaiting_user": "等待登录或补充信息，请打开安装会话继续。",
        "awaiting_input": "安装会话已停止，尚未报告准备完成；请打开会话继续验证。",
        "completed": "安装会话已结束，尚未报告准备完成；请打开会话查看。",
        "failed": "安装任务失败，请打开会话查看错误或重试准备。",
        "interrupted": "安装任务已中断，请打开会话继续或重试准备。",
        "needs_review": "安装任务需要复核，请打开会话查看。",
    }
    mapped = {"completed": "unverified", "awaiting_input": "unverified"}.get(state, state)
    return store.installations.update(job["id"], expected_status=current["status"], task_id=result.task_id, status=mapped,
                                      summary=summaries.get(state, "正在按 Skill 文档检查和准备环境。"))


@app.get("/skills/installations/{installation_id}")
async def get_skill_installation(installation_id: str) -> dict:
    store = user_skills_store()
    try:
        return refresh_skill_installation(store, store.installations.get(installation_id))
    except StopIteration:
        raise HTTPException(status_code=404, detail="安装记录不存在") from None


@app.get("/skills/installations/{installation_id}/events")
async def stream_skill_installation(installation_id: str):
    from tsm_agt.sdk.ui_events import subscribe_installation
    receipts = user_skills_store().installations
    try:
        job = receipts.get(installation_id)
    except StopIteration:
        raise HTTPException(status_code=404, detail="安装记录不存在") from None
    server = runtime_server

    async def frames():
        yield ": open\n\n"
        if server is None:
            yield "data: " + json.dumps({"installation": job, "watching": False}, ensure_ascii=False) + "\n\n"
            return
        iterator = subscribe_installation(server._client, receipts, installation_id)
        async with aclosing(server.iterate_runtime(iterator)) as packets:
            async for packet in packets:
                if packet.get("type") == "stream.heartbeat":
                    yield ": heartbeat\n\n"
                else:
                    yield "data: " + json.dumps(packet, ensure_ascii=False) + "\n\n"
    return StreamingResponse(frames(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@app.post("/skills/installations/{installation_id}/retry")
async def retry_skill_preparation(installation_id: str) -> dict:
    store = user_skills_store()
    try:
        job = refresh_skill_installation(store, store.installations.get(installation_id))
    except StopIteration:
        raise HTTPException(status_code=404, detail="安装记录不存在") from None
    if job["status"] in {"starting", "running", "awaiting_approval", "awaiting_user"}:
        return job
    if job["status"] != "pending":
        job = store.installations.create(store.parse(Path(job["plan_path"])), retry=True)
    return start_skill_preparation(store, job)


@app.get("/settings/skills")
async def get_skill_settings() -> dict:
    store = user_skills_store()
    skills = refresh_skill_catalog(store)
    try:
        directories = store.discovery_directories()
    except (OSError, ValueError):
        directories = (store.install_directory.resolve(),)
    return {
        "install_directory": str(store.install_directory),
        "discovery_directories": [str(path) for path in directories],
        "skills": [skill_data(skill) for skill in skills],
        "builtin_skills": [{"name": name} for name in builtin_skill_names()],
        "disabled_builtin_skills": store.settings().get("disabled_builtin_skills", []),
        "issues": list(store.issues),
        "installations": [refresh_skill_installation(store, row) for row in store.installations.list()[-30:]],
    }


@app.get("/settings/mcp")
async def get_mcp_settings(probe: bool = True) -> dict:
    """Report configured MCP servers plus whether each one is actually usable.

    Read-only on purpose: the panel shows configuration and a real
    connectivity result, but never edits the private YAML ``mcp`` section.
    ``probe=false`` skips the connection attempt for a fast render.
    """
    try:
        configuration_file, document = load_configuration_document()
    except (OSError, ValueError) as error:
        return {
            "configuration_path": None,
            "enabled": False,
            "servers": [],
            "issues": [f"读取私有配置失败：{error}"],
            "probed": False,
        }
    enabled, servers, issues = mcp_configuration_summary(document)
    rows = await describe_mcp_servers(enabled, servers, probe=probe)
    return {
        "configuration_path": str(configuration_file),
        "enabled": enabled,
        "servers": rows,
        "issues": list(issues),
        "probed": bool(probe and enabled and servers),
    }


@app.put("/settings/skills")
async def save_skill_settings(payload: dict) -> dict:
    try:
        store = user_skills_store()
        disabled = payload.get("disabled_builtin_skills", store.settings().get("disabled_builtin_skills", []))
        if (not isinstance(disabled, list) or any(not isinstance(name, str) or name not in builtin_skill_names() for name in disabled)):
            raise ValueError("disabled_builtin_skills 包含未知内置 Skill")
        store.save_directories(payload.get("discovery_directories"))
        if "disabled_builtin_skills" in payload:
            store.save_disabled_builtin_skills(disabled)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return await get_skill_settings()


@app.post("/settings/skills/pick")
async def pick_skill_directories() -> dict:
    return {"paths": await asyncio.to_thread(choose_skill_directories)}


def choose_skill_directories() -> list[str]:
    if sys.platform != "darwin":
        raise HTTPException(status_code=501, detail="当前系统请手动输入目录路径")
    script = (
        'set chosen to choose folder with prompt "选择可发现 Skills 目录（可多选）" with multiple selections allowed\n'
        'set resultText to ""\n'
        'repeat with itemPath in chosen\n'
        'set resultText to resultText & POSIX path of itemPath & linefeed\n'
        'end repeat\n'
        'return resultText'
    )
    try:
        completed = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, check=False,
        )
    except OSError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if completed.returncode:
        if "-128" in completed.stderr or "User canceled" in completed.stderr:
            return []
        raise HTTPException(status_code=503, detail=completed.stderr.strip())
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


@app.post("/skills/upload")
async def upload_skill_directory(
    files: list[UploadFile] = File(...),
    session_id: str | None = Form(None),
    workspace_id: str | None = Form(None),
) -> dict:
    """Install one uploaded directory into the user catalog, across all chats."""
    try:
        store = user_skills_store()
        files = [file for file in files if not store.ignored_upload_file(file.filename or "")]
        uploaded: dict[str, bytes] = {}
        remaining = 20_000_000
        if len(files) > 500:
            raise ValueError("Skill 上传最多 500 个文件")
        for file in files:
            if not file.filename or file.filename in uploaded:
                raise ValueError("上传文件名为空或重复")
            content = await file.read(remaining + 1)
            remaining -= len(content)
            if remaining < 0:
                raise ValueError("Skill 上传不能超过 20 MB")
            uploaded[file.filename] = content
        skill = store.install_upload(uploaded)
        # Awaiting this upload also orders it after a workspace selection; a
        # separate browser activation request may still be in flight.
        if isinstance(workspace_id, str):
            workspace = next((row for row in workspace_registry if row["id"] == workspace_id), None)
            if workspace is not None:
                touch_workspace(workspace)
        return skill_install_receipt(skill, store)
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/skills/install")
async def install_skill_endpoint(payload: dict) -> dict:
    """Install a local manifest; metadata comes from SKILL.md, not the chat."""
    raw_path = payload.get("plan_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise HTTPException(status_code=400, detail="请提供 SKILL.md 路径")
    try:
        store = user_skills_store()
        manifest = Path(raw_path).expanduser().resolve()
        if manifest.name != "SKILL.md" or not manifest.is_file():
            raise ValueError("请选择存在的 SKILL.md 文件")
        if manifest.is_relative_to(store.install_directory.resolve()):
            skill = store.parse(manifest)
        else:
            skill = store.install_path(manifest)
        return skill_install_receipt(skill, store)
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _cancel_on_disconnect() -> bool:
    """Spec D3: by default a disconnect only releases the stream.

    Setting ``TSM_AGT_CANCEL_ON_DISCONNECT`` turns a disconnect into a cancel,
    which trades a durable session for a volatile one; it is opt-in.
    """
    return os.environ.get(
        "TSM_AGT_CANCEL_ON_DISCONNECT", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _explicit_user_input_intent(raw: object) -> RuntimeInputIntent | None:
    """Map a client's explicit command onto one unified entry intent."""
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value:
        return None
    mapping = {
        "steer": RuntimeInputIntent.STEER,
        "replace": RuntimeInputIntent.REPLACE,
        "new": RuntimeInputIntent.NEW_TASK,
        "stop": RuntimeInputIntent.INTERRUPT,
        "queue": RuntimeInputIntent.NEW_TASK_AFTER_CURRENT,
    }
    if value not in mapping:
        raise HTTPException(
            status_code=400,
            detail="intent must be one of: steer, replace, new, stop, queue",
        )
    return mapping[value]


def _source_evaluation_case_snapshot(case: Mapping[str, Any]) -> dict[str, Any]:
    """Project an isolated Task's durable events into comparable metrics."""
    events = case.get("events") if isinstance(case.get("events"), list) else []
    progress = [item for event in events if (item := _turn_progress_item(event))]
    started = next((item for item in progress if item.get("kind") == "turn_started"), {})
    tools = [item for item in progress if item.get("kind") == "tool_started"]
    read_names = {"source.github.get_file", "core.read_file"}
    search_names = {"web.search", "web.fetch_markdown", "source.github.resolve", "source.github.list_tree", "source.github.search_code"}
    reads = [item for item in tools if item.get("tool_name") in read_names]
    searches = [item for item in tools if item.get("tool_name") in search_names]
    reviews = [item for item in progress if item.get("kind") == "completion_review"]
    review = reviews[-1] if reviews else {}
    result = case.get("result") if isinstance(case.get("result"), Mapping) else {}
    status = str(result.get("status") or case.get("status") or "starting")
    terminal = status in {"completed", "failed", "incomplete", "awaiting_user"}
    return {
        "id": case.get("id"), "title": case.get("title"), "task_id": case.get("task_id"),
        "status": status, "tool_exposure_profile": started.get("tool_exposure_profile"),
        "visible_tool_count": int(started.get("visible_tool_count") or 0),
        "first_tool": tools[0].get("tool_name") if tools else None,
        "tool_call_count": len(tools), "source_read": bool(reads),
        "search_only": bool(searches) and not reads,
        "review_action": review.get("review_action"),
        "review_reason_code": review.get("review_reason_code"),
        "passed": (status == "completed" and bool(reads) and review.get("review_action") != "EXHAUSTED") if terminal else None,
        "error": case.get("error"),
    }


async def _launch_source_evaluation(run_id: str, workspace: Path) -> None:
    """Submit real work in fresh Sessions; it never touches the active chat."""
    assert runtime_server is not None
    run = source_evaluation_runs[run_id]
    run["status"] = "launching"
    kernel = runtime_server._client.application.kernel
    for case in run["cases"]:
        try:
            await kernel.create_session(case["title"], session_id=case["session_id"])
            submitted = await runtime_server._client.submit_user_input(
                case["session_id"], case["prompt"], input_id=f"evaluation-{uuid4().hex}",
                explicit_intent=RuntimeInputIntent.NEW_TASK, workspace=workspace,
            )
            case["task_id"] = str(submitted.result.get("task_id") or "")
            case["status"] = "running" if case["task_id"] else "failed"
            if not case["task_id"]:
                case["error"] = "Runtime 未返回 task_id"
        except Exception as error:
            case["status"] = "failed"
            case["error"] = str(error)
    run["status"] = "running"


async def _refresh_source_evaluation(run: dict[str, Any]) -> dict[str, Any]:
    if runtime_server is None:
        return {"status": "unavailable", "cases": [], "metrics": {}}
    kernel = runtime_server._client.application.kernel
    snapshots = []
    for case in run["cases"]:
        if task_id := str(case.get("task_id") or ""):
            try:
                case["result"] = (await runtime_server._client.get_task_result(task_id)).to_data()
                case["events"] = list(await kernel.dependencies.store.read_events(task_id, event_types=_TURN_PROGRESS_EVENT_TYPES))
            except (LookupError, PermissionError) as error:
                case["error"] = str(error)
        snapshots.append(_source_evaluation_case_snapshot(case))
    settled = snapshots and all(item["status"] in {"completed", "failed", "incomplete", "awaiting_user"} for item in snapshots)
    if settled:
        run["status"] = "completed"
    measured = [item for item in snapshots if item["tool_exposure_profile"]]
    count = len(measured)
    rate = lambda predicate: sum(predicate(item) for item in measured) / count if count else None
    return {"run_id": run["id"], "status": run["status"], "cases": snapshots, "metrics": {
        "sample_count": count, "first_turn_zero_tool_rate": rate(lambda item: not item["first_tool"]),
        "source_read_rate": rate(lambda item: item["source_read"]),
        "search_only_rate": rate(lambda item: item["search_only"]),
        "review_intervention_rate": rate(lambda item: item["review_action"] == "CONTINUE"),
        "pass_rate": rate(lambda item: item["passed"] is True),
    }}


@app.post("/evaluations/source-analysis/runs")
async def start_source_evaluation(payload: dict) -> dict:
    """Start the fixed real-model samples only after the user clicks UI."""
    if runtime_server is None:
        raise HTTPException(status_code=503, detail=runtime_error or "runtime unavailable")
    workspace_id = str(payload.get("workspace_id") or "").strip()
    workspace = next((item for item in workspace_registry if item["id"] == workspace_id), None)
    if workspace is None:
        raise HTTPException(status_code=400, detail="workspace selection is required")
    run_id = f"source-eval-{uuid4().hex}"
    source_evaluation_runs[run_id] = {"id": run_id, "status": "starting", "cases": [
        {**case, "session_id": f"evaluation-{uuid4().hex}", "status": "starting"}
        for case in _SOURCE_EVALUATION_CASES
    ]}
    asyncio.create_task(_launch_source_evaluation(run_id, Path(workspace["path"])), name=run_id)
    return {"run_id": run_id, "status": "starting", "case_count": len(_SOURCE_EVALUATION_CASES)}


@app.get("/evaluations/source-analysis/runs/{run_id}")
async def get_source_evaluation(run_id: str) -> dict:
    run = source_evaluation_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="evaluation run not found")
    return await _refresh_source_evaluation(run)


@app.post("/tasks/{task_id}/stop")
async def stop_task(task_id: str, payload: dict | None = None) -> dict:
    """Stop (recoverable interrupt) one running Task through the unified entry."""
    if runtime_server is None:
        raise HTTPException(
            status_code=503, detail=runtime_error or "runtime unavailable"
        )

    async def stop() -> Mapping[str, Any]:
        task = await runtime_server._client.application.kernel.get_task(task_id)
        result = await runtime_server._client.submit_user_input(
            task.session_id, "", input_id=f"stop-{task_id}",
            explicit_intent=RuntimeInputIntent.INTERRUPT,
            target_task_id=task_id,
        )
        return dict(result.result)

    try:
        data = runtime_server._call(stop())
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return dict(data)


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, payload: dict | None = None) -> dict:
    """Cancel one Task (terminal) through the SDK cancel command."""
    if runtime_server is None:
        raise HTTPException(
            status_code=503, detail=runtime_error or "runtime unavailable"
        )
    body = payload or {}
    command_id = str(body.get("command_id", "")).strip() or f"web-cancel-{task_id}"
    reason = str(body.get("reason", "")).strip() or "cancelled from the web UI"

    async def cancel() -> Mapping[str, Any]:
        result = await runtime_server._client.cancel(
            task_id, command_id=command_id, reason=reason,
        )
        return dict(result.result)

    try:
        data = runtime_server._call(cancel())
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return dict(data)


@app.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, payload: dict | None = None) -> JSONResponse:
    """Resume a Task suspended at a CONTINUATION boundary.

    Ordinary chat text is refused while a Task awaits the user, so this is the
    structured channel the wait banner points at. The continuation runs as
    Runtime work rather than inside this request.
    """
    if runtime_server is None:
        raise HTTPException(
            status_code=503, detail=runtime_error or "runtime unavailable"
        )
    body = payload or {}
    command_id = str(body.get("command_id", "")).strip() or f"web-resume-{task_id}"
    text = str(body.get("text", "")).strip() or "继续"
    runtime_server.submit_background(
        runtime_server._client.resume_continuation(
            task_id, command_id=command_id, text=text,
        )
    )
    return JSONResponse(
        content={"accepted": True, "task_id": task_id}, status_code=202,
    )


@app.post("/tasks/{task_id}/review")
async def review_task(task_id: str, payload: dict) -> dict:
    """Resolve a NEEDS_REVIEW Task with an explicit human decision."""
    if runtime_server is None:
        raise HTTPException(
            status_code=503, detail=runtime_error or "runtime unavailable"
        )
    decision = str(payload.get("decision", "")).strip().lower()
    reason = str(payload.get("reason", "")).strip()
    if decision not in {"accept", "return_for_revision", "cancel"}:
        raise HTTPException(
            status_code=400,
            detail="decision must be accept, return_for_revision, or cancel",
        )
    async def resolve() -> Mapping[str, Any]:
        task = await runtime_server._client.application.kernel.resolve_needs_review(
            task_id, decision, reason=reason,
        )
        return {"task_id": task.task_id, "state": task.state.value}

    try:
        data = runtime_server._call(resolve())
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return dict(data)


@app.post("/tasks/{task_id}/input")
async def submit_task_input(task_id: str, payload: dict) -> dict:
    """Apply one explicit steer/replace/queue input to an active Task."""
    if runtime_server is None:
        raise HTTPException(
            status_code=503, detail=runtime_error or "runtime unavailable"
        )
    text = str(payload.get("text", "")).strip()
    request_id = str(payload.get("request_id", "")).strip()
    if not text or not request_id:
        raise HTTPException(
            status_code=400, detail="text and request_id are required"
        )
    intent = _explicit_user_input_intent(payload.get("intent") or "steer")
    if intent is RuntimeInputIntent.INTERRUPT:
        raise HTTPException(status_code=400, detail="use /tasks/{id}/stop to stop")

    async def apply() -> Mapping[str, Any]:
        task = await runtime_server._client.application.kernel.get_task(task_id)
        result = await runtime_server._client.submit_user_input(
            task.session_id, text, input_id=request_id,
            explicit_intent=intent, target_task_id=task_id,
        )
        return dict(result.result)

    try:
        data = runtime_server._call(apply())
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return dict(data)


@app.post("/tasks")
async def create_task(payload: dict) -> dict:
    if runtime_server is None:
        raise HTTPException(
            status_code=503,
            detail=runtime_error or "runtime unavailable",
        )

    workspace_id = str(payload.get("workspace_id", "")).strip()
    workspace_path = str(payload.get("workspace_path", "")).strip()
    session_id = str(payload.get("session_id", "")).strip() or None

    if not workspace_id or not workspace_path:
        raise HTTPException(
            status_code=400,
            detail="workspace selection is required",
        )

    if session_id is not None:
        bind_session_to_workspace(session_id, workspace_id)

    goal = str(payload.get("goal", ""))
    image_blocks = _normalize_image_blocks(payload.get("images"))
    if image_blocks:
        # Record only the count; base64 payloads must never enter the goal.
        goal = f"{goal}\n\n[web-ui-attached-images: {len(image_blocks)}]"

    # The workspace is a Task parameter, not a hint inside the goal text. Naming
    # it here is what makes the Task actually run in the selected directory.
    try:
        result = runtime_server._call(runtime_server._client.submit_task(
            goal,
            command_id=f"web-ui:{workspace_id}",
            session_id=session_id,
            images=tuple(image_blocks),
            workspace=Path(workspace_path),
        ))
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return result.to_data()


@app.delete("/sessions/{session_id}")
async def remove_session(session_id: str) -> dict:
    """Hide one Session from the chat list.

    Session and Task records are durable Runtime data and the Kernel exposes no
    delete, so this is a UI-level removal: the id goes into a deny-list that
    both ``/sessions`` and the startup path honour. Removing a Session never
    removes its workspace.
    """
    if runtime_server is None:
        raise HTTPException(
            status_code=503, detail=runtime_error or "runtime unavailable",
        )
    kernel = runtime_server._client.application.kernel
    sessions = runtime_server._call(kernel.list_sessions())
    if not any(snapshot.session_id == session_id for snapshot in sessions):
        raise HTTPException(status_code=404, detail="会话不存在")
    removed_session_ids.add(session_id)
    save_web_removals()
    if session_workspace_bindings.pop(session_id, None) is not None:
        save_session_workspace_bindings()
    return {"removed_session_id": session_id}


@app.get("/sessions")
async def list_sessions(limit: int = 40) -> dict:
    """List durable Sessions so a reload or restart can rebuild the chat list.

    The browser keeps conversations in memory only. Everything shown here comes
    from the Runtime's own Session records, so history survives a page reload
    and a server restart instead of disappearing with the tab.
    """
    if runtime_server is None:
        raise HTTPException(
            status_code=503, detail=runtime_error or "runtime unavailable",
        )
    kernel = runtime_server._client.application.kernel
    sessions = runtime_server._call(kernel.list_sessions())
    items: list[dict] = []
    for snapshot in sorted(
        sessions, key=lambda item: item.updated_at, reverse=True
    ):
        if snapshot.session_id in removed_session_ids:
            continue
        if len(items) >= max(1, min(limit, 200)):
            break
        workspace_path = ""
        latest_task_id = snapshot.active_task_id or (
            snapshot.task_ids[-1] if snapshot.task_ids else None
        )
        if latest_task_id:
            try:
                task = runtime_server._call(kernel.get_task(latest_task_id))
                workspace_path = str(task.workspace)
            except (LookupError, PermissionError):
                workspace_path = ""
        items.append({
            "session_id": snapshot.session_id,
            "title": snapshot.title,
            "workspace_path": workspace_path,
            "task_count": len(snapshot.task_ids),
            "latest_task_id": latest_task_id,
            "model_profile_id": getattr(snapshot, "model_profile_id", None),
            "reasoning_effort": getattr(snapshot, "reasoning_effort", None),
            "updated_at": snapshot.updated_at.isoformat(),
        })
    return {"sessions": items}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    """Return one Session's user-visible messages and what it is waiting for."""
    if runtime_server is None:
        raise HTTPException(
            status_code=503, detail=runtime_error or "runtime unavailable",
        )
    kernel = runtime_server._client.application.kernel
    try:
        snapshot = runtime_server._call(kernel.get_session(session_id))
        projection = runtime_server._call(
            kernel.get_session_conversation(session_id)
        )
        session_events = runtime_server._call(
            kernel.dependencies.store.read_session_events(session_id)
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    task_attach_sequences = {
        str(event.payload.get("task_id")): event.sequence
        for event in session_events
        if event.event_type == "session.task_attached"
        and event.payload.get("task_id")
    }
    turns = _session_turn_calls(session_events)
    # A Turn's progress is durable fact, not a live-only view: the thinking,
    # tool and transport lines are read back for every settled Turn so the
    # conversation keeps showing what happened after the Turn ends.
    turn_progress: dict[str, list[dict]] = {}
    # Old SDK failures were sometimes recorded under the transport placeholder
    # ``sdk-background`` rather than the active Turn id.  The error still
    # belongs to the newest unfinished Turn for that Task; otherwise a reload
    # leaves its user input showing a permanently half-finished trace.
    unfinished_turn_by_task: dict[str, str] = {}
    for turn in turns:
        task_id = str(turn.get("task_id") or "")
        turn_id = str(turn.get("turn_id") or "")
        if task_id and turn_id and not turn.get("settled"):
            unfinished_turn_by_task[task_id] = turn_id
    for task_id in snapshot.task_ids:
        approval_turns: dict[str, str] = {}
        for event in runtime_server._call(
            kernel.dependencies.store.read_events(
                task_id, event_types=_TURN_PROGRESS_EVENT_TYPES,
            )
        ):
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            item = _turn_progress_item(
                event, fallback_turn_id=approval_turns.get(str(payload.get("request_id") or "")),
            )
            if item is None:
                continue
            turn_id = str(item.pop("turn_id"))
            if item.get("kind") == "waiting" and item.get("request_id"):
                approval_turns[item["request_id"]] = turn_id
            if turn_id == "sdk-background":
                turn_id = unfinished_turn_by_task.get(task_id, turn_id)
            turn_progress.setdefault(turn_id, []).append(item)
    for turn in turns:
        turn["progress"] = turn_progress.get(turn["turn_id"], [])
    # ``session.task_attached`` records the input that opened a Task without a
    # turn id, while the Turn it opened is that Task's first result event. The
    # pairing is by construction, so the first Turn's id is what labels the
    # attached input.
    first_turn_by_task: dict[str, str] = {}
    for turn in turns:
        task_id = str(turn.get("task_id") or "")
        turn_id = str(turn.get("turn_id") or "")
        if task_id and turn_id and task_id not in first_turn_by_task:
            first_turn_by_task[task_id] = turn_id
    images_by_turn = _session_input_images(session_events)
    task_items: list[dict] = []
    for task_id in snapshot.task_ids:
        try:
            task = runtime_server._call(kernel.get_task(task_id))
            result = runtime_server._call(
                runtime_server._client.get_task_result(task_id)
            )
        except (LookupError, PermissionError):
            continue
        task_items.append({
            "task_id": task_id,
            "goal": task.goal,
            "attached_sequence": task_attach_sequences.get(task_id, 0),
            "phase1_state": result.phase1_state,
            "status": result.status,
            "progress_cursor": result.cursor,
            "waiting": _waiting_payload(result),
            "failure_reason": (
                _task_failure_reason(task_id)
                if result.phase1_state in {"FAILED", "INTERRUPTED"} else None
            ),
            "assistant_text": result.assistant_text,
            "projection": dict(result.projection) if result.projection else None,
            "todo_list": (
                dict(result.todo_list) if result.todo_list is not None else None
            ),
            "is_active": task_id == snapshot.active_task_id,
        })
    latest = task_items[-1] if task_items else None
    return {
        "session_id": snapshot.session_id,
        "title": snapshot.title,
        "model_profile_id": getattr(snapshot, "model_profile_id", None),
        "reasoning_effort": getattr(snapshot, "reasoning_effort", None),
        "active_task_id": snapshot.active_task_id,
        "latest_task_id": latest["task_id"] if latest else None,
        "latest_task_state": latest["phase1_state"] if latest else None,
        "waiting": latest["waiting"] if latest else None,
        "tasks": task_items,
        # One record per Turn, in Session order. The Web renders it as the call
        # panel under that Turn's answer, so what a Turn actually called stays
        # visible after a reload instead of living only in the live progress
        # stream.
        "turns": turns,
        "messages": [
            _session_message_data(message, first_turn_by_task, images_by_turn)
            for message in projection.messages
        ],
    }


def _session_input_images(session_events) -> dict[tuple[str, str], list[dict]]:
    """Recover UI attachments without putting image bytes into model context."""
    images_by_turn: dict[tuple[str, str], list[dict]] = {}
    # Results repeat inputs; older Turns only have a result. Prefer the first
    # input record, including an explicit empty list, and never concatenate the
    # repeated result's attachments.
    for event_type in ("session.turn_input_recorded", "session.task_result_recorded"):
        for event in sorted(session_events, key=lambda item: item.sequence):
            if event.event_type != event_type:
                continue
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            task_id = str(payload.get("task_id") or "")
            turn_id = str(payload.get("turn_id") or "")
            key = (task_id, turn_id)
            raw = payload.get("user_message")
            if not task_id or not turn_id or key in images_by_turn:
                continue
            if not isinstance(raw, Mapping) or raw.get("role") != "user":
                continue
            blocks = raw.get("content")
            if not isinstance(blocks, list):
                continue
            images_by_turn[key] = [
                {"image_url": block["image_url"]}
                for block in blocks
                if isinstance(block, Mapping) and block.get("type") == "input_image"
                and isinstance(block.get("image_url"), str) and block["image_url"]
            ]
    return images_by_turn


def _session_message_data(
    message, first_turn_by_task: Mapping[str, str],
    images_by_turn: Mapping[tuple[str, str], list[dict]] | None = None,
) -> dict:
    """One Session message with the position facts the page sorts by."""

    task_id = message.task_id or ""
    turn_id = message.turn_id
    # ``session.task_attached`` records a Task's opening input without a turn
    # id; the Turn it opened is that Task's first result event.
    if turn_id is None and message.role is MessageRole.USER and task_id:
        turn_id = first_turn_by_task.get(task_id)
    return {
        "role": message.role.value,
        "content": message.text,
        "task_id": message.task_id,
        "turn_id": turn_id,
        "images": (
            (images_by_turn or {}).get((task_id, turn_id), [])
            if message.role is MessageRole.USER else []
        ),
        # The opening input belongs at its Task's attachment, above the Task
        # card. A later Turn's input keeps its own event position, which is what
        # places each question directly above its own answer instead of stacking
        # every question at the top of the conversation.
        "first_turn_input": bool(
            message.role is MessageRole.USER
            and turn_id
            and turn_id == first_turn_by_task.get(task_id)
        ),
        "source_event_sequence": message.source_event_sequence,
    }


#: The action keys a Turn's summary keeps. Anything else (command argv,
#: environment, patch bodies) is deliberately not in Session context.
_TURN_CALL_KEYS = (
    "tool", "state", "ok", "error_code", "succeeded", "failure_code",
    "path", "query", "pattern", "target", "file_glob",
)


def _session_turn_calls(session_events) -> list[dict]:
    """Return every Session Turn, including durable but unfinished Turns.

    Input is recorded before a Turn runs; a result is recorded only after it
    settles. Keeping the input record in this projection means a refresh never
    erases work paused for approval, interrupted, or still running. Settled
    records subsequently enrich that same Turn with its safe summary.
    """
    turns_by_id: dict[str, dict] = {}
    for event in session_events:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        if event.event_type == "session.turn_input_recorded":
            turn_id = str(payload.get("turn_id") or "")
            if not turn_id:
                continue
            turns_by_id.setdefault(turn_id, {
                "turn_id": turn_id,
                "task_id": str(payload.get("task_id") or ""),
                "recorded": False,
                "settled": False,
                "model_calls": None,
                "tool_calls": None,
                "tool_counts": {},
                "calls": [],
                "source_event_sequence": event.sequence,
            })
            continue
        if event.event_type != "session.task_result_recorded":
            continue
        raw_summary = payload.get("task_summary")
        summary = raw_summary if isinstance(raw_summary, Mapping) else {}
        calls = [
            {
                key: item[key]
                for key in _TURN_CALL_KEYS
                if item.get(key) is not None
            }
            for item in (summary.get("important_actions") or ())
            if isinstance(item, Mapping)
        ]
        counts = summary.get("tool_counts")
        recorded = isinstance(counts, Mapping)
        counters = payload.get("turn_counters")
        counters = counters if isinstance(counters, Mapping) else {}
        turn_id = str(payload.get("turn_id") or summary.get("turn_id") or "")
        if not turn_id:
            continue
        prior = turns_by_id.get(turn_id)
        turns_by_id[turn_id] = {
            "turn_id": turn_id,
            "task_id": str(payload.get("task_id") or ""),
            # An older record may carry no summary at all. Saying "no calls"
            # then would be a claim the record does not support, so the page
            # gets told which of the two it is looking at.
            "recorded": recorded,
            "settled": True,
            "model_calls": counters.get("model_calls"),
            "tool_calls": counters.get("tool_calls"),
            "tool_counts": (
                {str(key): value for key, value in counts.items()}
                if recorded else {}
            ),
            #: Per-call detail, newest last. ``tool_counts`` stays the
            #: authoritative total because the summary keeps only the most
            #: recent rows.
            "calls": calls,
            # The input event is the Turn's timeline anchor when present; the
            # result event is a later completion fact, not a new Turn.
            "source_event_sequence": (
                prior["source_event_sequence"] if prior is not None
                else event.sequence
            ),
        }
    return list(turns_by_id.values())


@app.post("/approvals")
async def resolve_approval(payload: dict) -> JSONResponse:
    """Carry an explicit web decision into the suspended Agent Turn.

    Only this route can approve. Ordinary chat text stays ordinary input, so a
    model-authored sentence can never read as consent. The decision is checked
    against the Task's own pending request before anything resumes, and the
    continuation then runs as Runtime work rather than inside this request.
    """
    if runtime_server is None:
        raise HTTPException(
            status_code=503,
            detail=runtime_error or "runtime unavailable",
        )
    task_id = str(payload.get("task_id", "")).strip()
    request_id = str(payload.get("request_id", "")).strip()
    command_id = str(payload.get("command_id", "")).strip()
    raw_decision = str(payload.get("decision", "")).strip().upper()
    if not task_id or not request_id or not command_id:
        raise HTTPException(
            status_code=400,
            detail="task_id, request_id, and command_id are required",
        )
    if raw_decision not in {"APPROVE", "DENY"}:
        raise HTTPException(
            status_code=400, detail="decision must be APPROVE or DENY",
        )
    decision = ApprovalDecision(raw_decision.lower())
    try:
        task = runtime_server._call(
            runtime_server._client.application.kernel.get_task(task_id)
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    pending = task.pending_approval
    if pending is None or pending.request_id != request_id:
        # A stale button must not resolve a different action than it displayed.
        raise HTTPException(
            status_code=409,
            detail="this approval request is no longer pending",
        )
    reason = str(payload.get("reason", "")).strip() or (
        "approved from the web UI after reviewing the exact action"
        if decision is ApprovalDecision.APPROVE else
        "rejected from the web UI"
    )
    runtime_server.submit_background(
        runtime_server._client.resolve_approval(
            request_id, decision, reason, command_id=command_id,
        )
    )
    return JSONResponse(
        content={
            "accepted": True, "task_id": task_id, "request_id": request_id,
            "decision": raw_decision,
        },
        status_code=202,
    )


# Bounded transport receipts, not resume credentials or execution evidence.
# Durable Task state remains the source of truth after a web service restart.
_clarification_submissions: dict[str, tuple[str, str, object]] = {}


@app.post("/clarifications")
async def answer_pending_clarification(payload: dict, request: Request) -> JSONResponse:
    _require_local_diagnostic_request(request)
    if runtime_server is None:
        raise HTTPException(status_code=503, detail=runtime_error or "runtime unavailable")
    task_id = str(payload.get("task_id") or "").strip()
    request_id = str(payload.get("request_id") or "").strip()
    command_id = str(payload.get("command_id") or "").strip()
    choice = str(payload.get("selected_choice") or "").strip() or None
    answer = str(payload.get("answer") or "").strip() or None
    if not task_id or not request_id or not command_id:
        raise HTTPException(status_code=400, detail="task_id, request_id, and command_id are required")
    try:
        task = runtime_server._call(runtime_server._client.application.kernel.get_task(task_id))
    except LookupError as error:
        raise HTTPException(status_code=404, detail="task not found") from error
    pending = task.pending_clarification
    if pending is None or pending.request_id != request_id:
        raise HTTPException(status_code=409, detail="this clarification is no longer pending")
    if ((pending.choices and (answer or choice not in {c.value for c in pending.choices}))
            or (not pending.choices and (choice or not answer))):
        raise HTTPException(status_code=400, detail="submit the displayed choice or requested answer")
    if command_id in _clarification_submissions:
        raise HTTPException(status_code=409, detail="clarification command already submitted")
    for key, (_, _, future) in list(_clarification_submissions.items()):
        if len(_clarification_submissions) < 128:
            break
        if future.done():
            del _clarification_submissions[key]
    if len(_clarification_submissions) >= 128:
        raise HTTPException(status_code=429, detail="too many pending clarification commands")
    future = runtime_server.submit_background(
        runtime_server._client.answer_pending_clarification(
            task_id, request_id, answer, selected_choice=choice, command_id=command_id,
        ),
    )
    _clarification_submissions[command_id] = (task_id, request_id, future)
    return JSONResponse(content={"accepted": True, "command_id": command_id}, status_code=202)


@app.get("/clarifications/{command_id}")
async def clarification_submission(command_id: str, request: Request) -> dict:
    _require_local_diagnostic_request(request)
    if runtime_server is None:
        raise HTTPException(status_code=503, detail="runtime unavailable")
    item = _clarification_submissions.get(command_id)
    if item is None:
        raise HTTPException(status_code=404, detail="submission unavailable; refresh pending task state")
    task_id, request_id, future = item
    if future.done() and (future.cancelled() or future.exception() is not None):
        return {"status": "failed", "detail": "恢复提交失败，请刷新任务状态后重试。"}
    task = runtime_server._call(runtime_server._client.application.kernel.get_task(task_id))
    pending = task.pending_clarification
    same_request = pending is not None and pending.request_id == request_id
    return {"status": ("waiting" if future.done() else "pending") if same_request else "resumed"}


@app.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    if runtime_server is None:
        raise HTTPException(
            status_code=503,
            detail=runtime_error or "runtime unavailable",
        )
    result = runtime_server._call(runtime_server._client.get_task_result(task_id))
    data = result.to_data()
    if result.phase1_state in {"FAILED", "INTERRUPTED"}:
        data["failure_reason"] = _task_failure_reason(task_id)
    return data


def _describe_loop_limit(error: AgentLoopLimitExceeded) -> str:
    detail = (
        "任务未能在调用预算内完成："
        f"模型回合 {error.model_calls}/{error.max_model_calls or '?'}，"
        f"工具调用 {error.tool_calls}/{error.max_tool_calls or '?'}。"
    )
    if error.last_tool_error:
        detail += f"最后一个工具问题：{error.last_tool_error}。"
        if "PERMISSION_DENIED" in error.last_tool_error:
            detail += "该动作被权限或 Project Trust 策略拒绝，并非网络失败。"
    return detail + "可以再发一条消息继续，继续时会重新分配调用预算。"


def _waiting_payload(result) -> dict | None:
    """Describe what the Task is waiting for, without inventing authority."""
    if result.state == "AWAITING_APPROVAL" and result.approval is not None:
        return {"kind": "APPROVAL", "approval": dict(result.approval)}
    if result.status == "awaiting_user":
        clarification = (
            dict(result.clarification) if result.clarification else {}
        )
        return {
            **{key: clarification[key] for key in (
                "request_id", "input_mode", "choices", "expires_at", "turn_id",
            ) if key in clarification},
            "kind": str(clarification.get("kind") or "INPUT"),
            "question": clarification.get("question"),
            "reason": clarification.get("reason"),
        }
    return None


def _task_failure_reason(task_id: str) -> str | None:
    """Read the last recorded failure message from the durable event log."""
    if runtime_server is None:
        return None
    try:
        events = runtime_server._call(
            runtime_server._client.application.kernel
            ._dependencies.store.read_events(task_id)
        )
    except Exception:
        return None
    for event in reversed(events):
        if event.event_type == "turn.started":
            break  # Never reuse an older Turn's failure for this Turn.
        if event.event_type == "checkpoint.conflict":
            return _checkpoint_conflict_reason(event.payload.get("conflicts") or [])
        if event.event_type == "turn.incomplete_boundary":
            return _incomplete_reason(event.payload)
        if event.event_type == "turn.interrupted":
            message = str(event.payload.get("message", "")).strip()
            if message:
                return _bounded_text(message)
        if event.event_type.endswith(".failed"):
            message = str(event.payload.get("message", "")).strip()
            if message:
                return message
    return None


def _checkpoint_conflict_reason(conflicts) -> str:
    keys = ", ".join(item[:80] for item in conflicts[:12] if isinstance(item, str))
    return f"执行检查点冲突（{keys or 'checkpoint_identity_conflict'}），本轮已停止，不能继续使用旧检查点或旧审批。"


@app.get("/stream/{task_id}")
async def stream(
    task_id: str, after: int = 0, request: Request = None,
):
    if runtime_server is None:
        raise HTTPException(status_code=503, detail=runtime_error or "runtime unavailable")
    if request is not None and getattr(request, "headers", {}).get("last-event-id"):
        try:
            after = int(request.headers["last-event-id"])
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid Last-Event-ID") from None
    if after < 0:
        raise HTTPException(status_code=400, detail="after must not be negative")
    server = runtime_server

    async def event_generator():
        # Preserve the explicit opt-in cancellation policy. A normal disconnect
        # only releases the subscriber and never replays or cancels execution.
        if request is not None and await request.is_disconnected():
            note_stream_released(task_id)
            if _cancel_on_disconnect():
                await server._client.cancel(task_id, command_id=f"disconnect-{task_id}", reason="client disconnected")
            return
        iterator = server._client.subscribe_task_ui(task_id, after=after)
        try:
            yield ": open\n\n"
            async with aclosing(server.iterate_runtime(iterator)) as packets:
                async for packet in packets:
                    if packet["type"] == "stream.heartbeat":
                        yield ": heartbeat\n\n"
                        continue
                    sequence = packet.get("sequence")
                    identity = f"id: {sequence}\n" if sequence is not None else ""
                    yield identity + "data: " + json.dumps(packet, ensure_ascii=False) + "\n\n"
        finally:
            note_stream_released(task_id)
            if _cancel_on_disconnect() and request is not None and await request.is_disconnected():
                server.submit_background(server._client.cancel(task_id, command_id=f"disconnect-{task_id}", reason="client disconnected"))

    return StreamingResponse(event_generator(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no",
    })


def main() -> None:
    uvicorn.run(
        "tsm_agt.web.app:app",
        host="127.0.0.1",
        port=8080,
        reload=False,
    )


if __name__ == "__main__":
    main()
