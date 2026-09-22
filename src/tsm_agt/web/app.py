from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    HTMLResponse, JSONResponse, Response, StreamingResponse,
)

from tsm_agt.core import AgentLoopLimitExceeded, ApprovalDecision
from tsm_agt.local_api import LocalEventApiServer
from tsm_agt.ports import ImageBlock

app = FastAPI(title="tsm-agt-web")
WEBUI_VERSION = "deepseek-cn-v20260921-session-timeline-order"

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
body{margin:0;background:linear-gradient(180deg,#fbfdff 0%,#f6f9fc 100%);color:var(--text);font:14px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
body::before{content:"";position:fixed;inset:0;background:radial-gradient(circle at top,rgba(22,119,255,.08),transparent 30%);pointer-events:none}
button{font:inherit;color:inherit;background:none;border:none;cursor:pointer;border-radius:10px}
button:disabled{opacity:.4;cursor:not-allowed}
.layout{display:grid;grid-template-columns:260px minmax(0,1fr);height:100vh;overflow:hidden}
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
.workspace-entry{flex:1;min-width:0}
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
.main{display:flex;flex-direction:column;min-width:0;min-height:0;height:100vh;overflow:hidden;background:transparent}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:18px 32px;border-bottom:1px solid var(--border);background:rgba(255,255,255,.9);backdrop-filter:blur(12px)}
#conversation-name{font-weight:500}
.topbar .icon-btn{font-size:12px}
.chat-shell{flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;padding:18px 22px 14px}
.workspace-stage{flex:1;min-height:0;width:100%;display:grid;grid-template-columns:minmax(0,1.18fr) minmax(320px,.82fr);gap:14px;align-items:stretch}
.pane-shell{min-height:0;display:flex;flex-direction:column;background:rgba(255,255,255,.9);border:1px solid rgba(255,255,255,.72);border-radius:24px;overflow:hidden;backdrop-filter:blur(10px);box-shadow:none}
.pane-header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:14px 18px 10px;border-bottom:1px solid rgba(237,242,247,.75);background:rgba(255,255,255,.78)}
.pane-title{font-size:15px;font-weight:700;color:#111827}
.pane-subtitle{margin-top:4px;font-size:12px;color:#64748b}
.pane-badge{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:999px;background:#eff6ff;color:#2563eb;font-size:12px;font-weight:600}
.chat-area,.trace-pane{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;padding:10px;display:flex;flex-direction:column;gap:8px;background:transparent}
.trace-pane{gap:8px;background:rgba(255,255,255,.2)}
.trace-live-board{display:flex;flex-direction:column;gap:6px}
.trace-future-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
.trace-future-card{border:1px dashed #dde6f0;border-radius:10px;padding:7px 9px;background:rgba(255,255,255,.72)}
.trace-future-title{font-size:12px;font-weight:700;color:#334155;margin-bottom:4px}
.trace-future-copy{font-size:11px;color:#64748b;line-height:1.5}
.trace-board{display:grid;grid-template-columns:minmax(0,1fr) 208px;gap:8px;align-items:start}
.trace-main{display:flex;flex-direction:column;gap:6px}
.trace-sidebar{position:sticky;top:8px;display:flex;flex-direction:column;gap:6px;opacity:.94}
.trace-overview-panel{background:linear-gradient(180deg,rgba(255,255,255,.78),rgba(248,251,255,.88));border:1px solid rgba(226,232,240,.72);border-radius:14px;padding:9px;display:flex;flex-direction:column;gap:6px}
.trace-panel-heading{display:flex;align-items:center;justify-content:space-between;gap:10px}
.trace-panel-title{font-size:12px;font-weight:700;color:#0f172a}
.trace-panel-caption{font-size:11px;color:#64748b}
.trace-summary-strip{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
.trace-metric{background:rgba(255,255,255,.88);border:1px solid #e2e8f0;border-radius:9px;padding:7px 8px;min-height:44px}
.trace-metric-label{font-size:9px;color:#6b7280;text-transform:uppercase;letter-spacing:.04em}
.trace-metric-value{font-size:14px;font-weight:700;margin-top:2px;color:#111827}
.trace-legend-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px}
.trace-legend-item{display:flex;align-items:center;gap:5px;color:var(--text-soft);font-size:10px;padding:4px 6px;border-radius:8px;background:rgba(251,253,255,.82);border:1px solid #e7edf5}
.trace-dot{width:10px;height:10px;border-radius:50%;box-shadow:0 0 0 4px rgba(255,255,255,.06)}
.trace-dot.running{background:#1677ff}.trace-dot.waiting{background:#faad14}.trace-dot.done{background:#52c41a}.trace-dot.failed{background:#ff4d4f}
.message{display:flex;flex-direction:column;position:relative;max-width:min(920px,100%);word-break:break-word;gap:8px}
.message.assistant{align-self:flex-start;width:100%;padding-inline:0;background:rgba(255,255,255,.82);border:1px solid #e6edf5;border-radius:20px;padding:16px 18px;box-shadow:none}
.message.user{display:inline-flex;align-self:flex-end !important;margin-left:auto !important;margin-right:0 !important;width:fit-content;max-width:min(720px,calc(100% - 24px));background:transparent;color:#334155;border:none !important;outline:none !important;box-shadow:none !important;border-radius:16px;padding:12px 14px;text-align:left}
.message-body{width:min(96ch,100%);white-space:normal;font-size:15px;line-height:1.72;letter-spacing:0;color:var(--text);font-family:Inter,"SF Pro Display","Segoe UI",sans-serif}
.message-body > *:first-child{margin-top:0}
.message-body > *:last-child{margin-bottom:0}
.message-body p{margin:.45em 0;color:var(--text-soft);max-width:96ch}
.message-body h1,.message-body h2,.message-body h3{line-height:1.3;font-weight:600;letter-spacing:-.01em;color:var(--text)}
.message-body h1{margin:1.2em 0 .55em;font-size:28px}
.message-body h2{margin:1em 0 .45em;font-size:22px}
.message-body h3{margin:.9em 0 .35em;font-size:18px}
.message-body ul,.message-body ol{margin:.45em 0 .9em;padding-left:1.3em;color:var(--text-soft);max-width:96ch}
.message-body li{margin:.2em 0}
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
.message-body blockquote{margin:1rem 0;padding:12px 16px;border-left:3px solid #7b8798;background:#343d49;border-radius:0 12px 12px 0;color:#eef2f7;max-width:86ch}
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
.message.user{justify-content:flex-end;align-self:flex-end !important;margin-left:auto !important;margin-right:0 !important;background:transparent;border:none !important;outline:none !important;box-shadow:none !important;border-bottom-right-radius:8px;color:#334155;text-align:left}
.message.assistant{align-self:flex-start;background:#ffffff;border:1px solid var(--border);border-bottom-left-radius:8px;color:#1f2937;width:100%}
.message-role{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#64748b}
.message-role::before{content:'';width:8px;height:8px;border-radius:999px;background:currentColor;opacity:.85}
.message.assistant .message-role{color:#4b5563}
.message.user .message-role{color:#d1d5db}
.message-body{font-size:15px;line-height:1.72;color:inherit}
.message-body p:first-child,.message-body h1:first-child,.message-body h2:first-child,.message-body h3:first-child{margin-top:0}
.message-body p:last-child{margin-bottom:0}
.message.system{align-self:flex-start;max-width:100%;color:var(--muted);font-size:12px;padding:0 2px}
.message.task{align-self:stretch;max-width:100%;padding:0}
.task-card{border:1px solid #d9e1ec;border-radius:18px;background:linear-gradient(180deg,#ffffff,#f8fafc);padding:14px 16px;display:flex;flex-direction:column;gap:10px;box-shadow:0 2px 6px rgba(15,23,42,.04)}
.task-card-header{display:flex;justify-content:space-between;gap:10px;align-items:center}
.task-card-meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.task-phase-badge{padding:3px 8px;border-radius:999px;font-size:10px;background:#dbeafe;color:#2563eb;border:1px solid #93c5fd;font-weight:600}
.task-card-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;align-items:stretch}
.task-summary-item{background:#f8fafc;border:1px solid #d9e1ec;border-radius:10px;padding:8px 10px;min-height:0}
.task-summary-label{font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.06em}
.task-summary-value{margin-top:3px;font-size:13px;font-weight:700;color:#111827;line-height:1.2}
.task-panel{display:none}
.task-panel.active{display:flex;flex-direction:column;gap:8px}
.task-progress{display:flex;flex-direction:column;gap:6px}
.task-progress-line{font-size:12px;color:#334155;white-space:pre-wrap;word-break:break-word;background:#f8fafc;border:1px solid #d9e1ec;border-left:2px solid #91caff;border-radius:10px;padding:8px 10px}
.task-progress-empty{font-size:12px;color:#94a3b8}
.trace-timeline-scroll{min-height:0;max-height:min(72vh,1400px);overflow-y:auto;overscroll-behavior:contain;padding-right:4px}
.trace-timeline{position:relative;display:flex;flex-direction:column;gap:8px;padding-left:18px;padding-bottom:6px}
.trace-timeline::before{content:'';position:absolute;left:6px;top:6px;bottom:6px;width:2px;background:linear-gradient(180deg,#cbd5e1,#dbe5f0)}
.trace-step{position:relative;padding:7px 10px;border-radius:10px;background:#ffffff;border:1px solid #d9e1ec;box-shadow:0 1px 1px rgba(15,23,42,.03)}
.trace-step::before{content:'';position:absolute;left:-12px;top:10px;width:8px;height:8px;border-radius:50%;background:#8b95a7;border:2px solid #f8fafc;box-shadow:none}
.trace-step.done::before{background:#52c41a}.trace-step.waiting::before{background:#faad14}.trace-step.failed::before{background:#ff4d4f}
.trace-step-header{display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:nowrap}
.trace-step-header > div:first-child{display:flex;align-items:center;gap:8px;min-width:0;flex:1}
.trace-step-title{font-size:12px;font-weight:700;color:#111827;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trace-step-meta{font-size:10px;color:#64748b;background:#f8fafc;border-radius:999px;padding:1px 6px;border:1px solid #dbe4ee;white-space:nowrap;flex-shrink:0}
.trace-step-body{margin-top:2px;font-size:11px;color:#64748b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.35}
.task-card-header{display:flex;justify-content:space-between;gap:12px;align-items:center}
.task-card-title{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task-card-state{font-size:12px;color:var(--muted);flex:none}
.task-card-state.running{color:#8ab4f8}.task-card-state.waiting{color:#f0c674}.task-card-state.failed{color:#f4837a}.task-card-state.done{color:#81c995}
.task-progress{display:flex;flex-direction:column;gap:3px;border-top:1px solid var(--border);padding-top:7px}
.task-progress-line{font-size:12px;color:var(--muted);white-space:pre-wrap;word-break:break-word}
.task-progress-empty{font-size:12px;color:#686d75}
.task-card .message.approval{max-width:100%;align-self:stretch;margin-top:4px}
.message.approval{align-self:flex-start;max-width:min(920px,100%);background:linear-gradient(180deg,#ffffff,#f8fafc);border:1px solid #d9e1ec;border-radius:18px;padding:10px 12px;display:flex;flex-direction:column;gap:8px;box-shadow:0 6px 14px rgba(15,23,42,.05);position:relative;overflow:hidden}
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
.markdown-list{margin:14px 0;padding-left:18px;color:#475569;display:flex;flex-direction:column;gap:10px;line-height:1.7}
.markdown-list li::marker{color:#94a3b8}
.markdown-table{width:100%;border-collapse:separate;border-spacing:0;margin:18px 0;background:#ffffff;border:1px solid #d9e1ec;border-radius:18px;overflow:hidden;box-shadow:0 10px 30px rgba(15,23,42,.06)}
.markdown-table th,.markdown-table td{padding:12px 14px;border-bottom:1px solid #e8edf5;text-align:left;font-size:13px;color:#475569;vertical-align:top}
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
.tool-call-markdown{background:linear-gradient(180deg,#fbfdff 0%,#f8fafc 100%);border:1px solid #dbe3ee;border-radius:18px;padding:18px;display:flex;flex-direction:column;gap:14px;overflow:hidden}
.tool-call-markdown p{margin:0 0 12px;color:#475569;line-height:1.75}
.tool-call-markdown p:last-child{margin-bottom:0}
.tool-call-markdown h1,.tool-call-markdown h2,.tool-call-markdown h3{margin:0 0 10px;color:#0f172a;letter-spacing:-.01em}
.tool-call-markdown code{background:#eef2ff;color:#4338ca;padding:2px 6px;border-radius:6px;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.tool-call-markdown hr{border:none;border-top:1px solid #dbe3ee;margin:16px 0}
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
.change-list{display:flex;flex-direction:column;gap:12px;margin-top:8px}
.change-list{display:flex;flex-direction:column;gap:4px;margin:2px 0}
.change-item{padding:8px 10px;background:#fff;border:1px solid #e2e8f0;border-radius:10px}
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
.composer{padding:8px 20px 10px;display:flex;flex-direction:column;gap:6px;background:#f7f9fc;border-top:1px solid #e2e8f0}
.composer-box{border:1px solid #dbe3ee;border-radius:16px;padding:10px 12px;background:#ffffff;box-shadow:none}
.composer-box:focus-within{border-color:#91caff;box-shadow:0 0 0 2px rgba(22,119,255,.08)}
.composer-meta{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px}
.composer-title{font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#94a3b8}
.composer-hint{font-size:11px;color:var(--muted);white-space:nowrap;display:flex;align-items:center;min-height:36px}
.composer-input-row{display:flex;align-items:center;gap:10px;min-width:0}
.textarea-wrap{flex:1;min-width:0;display:flex;align-items:center}
textarea{width:100%;min-height:48px;max-height:140px;background:#ffffff;border:1px solid #e2e8f0;color:#0f172a;font:inherit;font-size:14px;line-height:1.45;resize:none;outline:none;padding:10px 12px;border-radius:12px;transition:border-color .2s ease,background .2s ease,box-shadow .2s ease}
textarea:focus{border-color:#91caff;background:#ffffff;box-shadow:0 0 0 3px rgba(22,119,255,.08)}
textarea::placeholder{color:#94a3b8}
.composer-actions{display:flex;align-items:center;justify-content:flex-end;gap:10px;flex-shrink:0;min-height:48px}
.send-btn{display:inline-flex;align-items:center;justify-content:center;min-height:40px;background:linear-gradient(135deg,#4096ff,#1677ff);color:#fff;padding:9px 16px;border-radius:999px;font-weight:600;letter-spacing:.01em;box-shadow:0 6px 14px rgba(22,119,255,.16)}
.send-btn:disabled{background:#3a4560}
.preview-list{display:flex;gap:8px;overflow:auto}
.preview-item{position:relative;flex:none}
.preview-item img{width:40px;height:40px;object-fit:cover;border-radius:6px;display:block;border:1px solid var(--border)}
.preview-remove{position:absolute;top:-6px;right:-6px;width:18px;height:18px;line-height:1;border-radius:50%;background:#3a3d42;color:var(--text);font-size:12px}
.preview-remove:hover{background:#4a4e55}
.loading,.error-banner{display:none;font-size:13px;color:var(--muted);padding:0 20px}
.loading.visible,.error-banner.visible{display:block}
.error-banner{color:#f4837a}
@media(max-width:1500px){.workspace-stage{grid-template-columns:minmax(0,1fr) 380px}.trace-board{grid-template-columns:minmax(0,1fr) 220px}}
@media(max-width:1280px){.workspace-stage{grid-template-columns:1fr}.trace-board{grid-template-columns:1fr}.trace-sidebar{position:static;order:-1;opacity:.72}.task-card-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.trace-future-grid{grid-template-columns:1fr}}
@media(max-width:1024px){.layout{grid-template-columns:1fr;height:auto}.sidebar{display:none}.topbar{padding-inline:16px}.chat-shell{padding:16px 16px 14px}.chat-area,.trace-pane{padding:14px}.message.user{max-width:100%}.composer{padding:8px 14px 10px}.composer-box{padding:9px 10px}.textarea{min-height:44px}}
@media(max-width:720px){.trace-board{gap:8px}.task-card-summary{grid-template-columns:1fr}.chat-area,.trace-pane{padding:10px 12px 18px}.task-card{padding:9px 10px}.trace-step{padding-block:6px}.composer{padding:8px 10px}.composer-meta{align-items:flex-start;flex-direction:column;gap:3px}.composer-input-row{align-items:stretch;flex-wrap:wrap}.composer-actions{width:100%;justify-content:space-between}.send-btn{padding:8px 14px}.composer-hint{white-space:normal}}
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
      <div id=\"conversation-name\">新建会话</div>
      <div>
        <span id=\"workspace-status\" class=\"section-title\"></span>
        <button class=\"icon-btn\" onclick=\"clearMessages()\">清空</button>
      </div>
    </div>

    <div id=\"error-banner\" class=\"error-banner\"></div>
    <div id=\"loading\" class=\"loading\">正在等待运行结果…</div>
    <div class=\"chat-shell\">
      <div class=\"workspace-stage\">
        <section class=\"pane-shell\">
          <div class=\"pane-header\">
            <div>
              <div class=\"pane-title\">实时对话</div>
              <div class=\"pane-subtitle\">持续展示会话上下文、tool call 与 patch diff 输出</div>
            </div>
            <div class=\"pane-badge\">Conversation Live</div>
          </div>
          <section id=\"chat-area\" class=\"chat-area\"></section>
        </section>
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
      <div id=\"preview-list\" class=\"preview-list\"></div>
      <div class=\"composer-box\">
        <div class=\"composer-meta\">
          <div>
            <div class=\"composer-title\">当前任务</div>
            <div class=\"composer-hint\">Enter 发送消息，Shift + Enter 换行</div>
          </div>
          <div class=\"composer-hint\">支持粘贴截图与 markdown</div>
        </div>
        <div class=\"composer-input-row\">
          <div class=\"textarea-wrap\">
            <textarea id=\"prompt\" placeholder=\"描述你希望完成的任务、修复的问题或需要分析的内容…\" onpaste=\"handlePaste(event)\"></textarea>
          </div>
          <div class=\"composer-actions\">
            <button class=\"icon-btn\" onclick=\"document.getElementById('image-input').click()\" title=\"上传图片\">＋ 图片</button>
            <input id=\"image-input\" type=\"file\" accept=\"image/*\" multiple onchange=\"handleImages(event)\" style=\"display:none\" />
            <button class=\"send-btn\" onclick=\"runTask()\">发送</button>
          </div>
        </div>
      </div>
    </div>
  </main>
</div>

<script>
const selectedImages = [];
const workspaces = [];
const conversations = [];
const ACTIVE_WORKSPACE_STORAGE_KEY = 'tsm-agt.active-workspace';
const DRAFT_STORAGE_PREFIX = 'tsm-agt.draft.';
let activeConversationId = null;
let activeWorkspaceId = localStorage.getItem(ACTIVE_WORKSPACE_STORAGE_KEY);
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
  sessionStorage.setItem(draftStorageKey(active.id), value);
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

  const persistedDraft = sessionStorage.getItem(draftStorageKey(active.id));
  const draft = resolveConversationDraft(active, persistedDraft);

  active.draft = draft;
  document.getElementById('prompt').value = draft;
}

function getActiveWorkspace() {
  return workspaces.find((item) => item.id === activeWorkspaceId);
}

function updateWorkspaceState() {
  const activeWorkspace = getActiveWorkspace();
  const disabled = !activeWorkspace;

  document.getElementById('new-conversation-btn').disabled = disabled;
  document.getElementById('prompt').disabled = disabled;
  document.getElementById('image-input').disabled = disabled;
  document.querySelector('.send-btn').disabled = disabled;
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
    entry.innerHTML = `
      <div class='item-title'>${workspace.name}</div>
      <div class='item-meta'>${workspace.path}</div>
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
          localStorage.setItem(ACTIVE_WORKSPACE_STORAGE_KEY, workspace.id);
          activeConversationId = conversation.id;
          updateWorkspaceState();
          renderMessages();
          restoreConversationDraft();
          ensureConversationLoaded(conversation.id);
        };
        item.innerHTML = `
          <div class='item-title'>${conversation.title}</div>
          <div class='item-meta' style='direction:ltr'>${conversation.messages.length} 条消息 · ${conversation.createdAt}</div>
        `;
        nested.appendChild(item);
      });
    }
    group.appendChild(nested);
    list.appendChild(group);
  });
}

function activateWorkspace(workspaceId) {
  persistActiveDraft();
  activeWorkspaceId = workspaceId;
  localStorage.setItem(ACTIVE_WORKSPACE_STORAGE_KEY, workspaceId);

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

function renderConversations() {
  // Sessions render inside their workspace group, so refreshing the workspace
  // list is what keeps both in sync.
  renderWorkspaceList();
  const active = getActiveConversation();
  document.getElementById('conversation-name').textContent = active?.title || '新建会话';
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

function renderStructuredToolCall(content) {
  const rows = content.split('\\n').filter((line) => line.includes('：'));
  if (rows.length < 2) return null;

  const priorityLabels = ['工具', '操作', '命令', '文件', '状态'];
  const summary = [];
  const details = [];

  rows.forEach((line) => {
    const [label, ...rest] = line.split('：');
    const cleanLabel = label.trim();
    const value = rest.join('：').trim();
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

  const collapseId = `tool-call-${Math.random().toString(36).slice(2, 10)}`;

  return `
    <section class="tool-call-card collapsed" data-tool-call-container>
      <div class="tool-call-header">
        <button class="tool-call-toggle" type="button" data-tool-call-toggle="${collapseId}" aria-expanded="false">
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
  const lines = text.split('\\n');
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
          <pre>${escapeHtml(section.lines.join('\\n').trim())}</pre>
        </div>
      </div>
    `;
  }).join('');

  const collapseId = `diff-${Math.random().toString(36).slice(2, 10)}`;

  return `
    <div class="diff-viewer">
      <div class="diff-viewer-header">
        <div>
          <div class="diff-viewer-title">Patch Diff</div>
          <div class="diff-muted">聚焦真实改动内容，弱化 metadata 与原始 dump 输出</div>
        </div>
        <div class="diff-viewer-actions">
          <button class="diff-viewer-toggle" type="button" data-diff-toggle="${collapseId}" aria-expanded="false">展开完整内容</button>
          <div class="diff-viewer-meta">Modern Viewer</div>
        </div>
      </div>
      <div class="diff-viewer-body collapsed" data-diff-body="${collapseId}">
        ${body}
      </div>
    </div>
  `;
}

function renderMarkdown(text) {
  const escaped = escapeHtml(text || '');
  const lines = escaped.split('\\n');
  const blocks = [];
  let paragraph = [];
  let codeFence = null;

  function flushParagraph() {
    if (!paragraph.length) return;
    const content = paragraph.join('<br />');
    blocks.push(`<p>${renderInlineMarkdown(content)}</p>`);
    paragraph = [];
  }

  let listItems = [];

  function flushCodeFence() {
    if (!codeFence) return;
    const language = codeFence.language || 'code';
    const code = codeFence.lines.join('\\n');
    const isTerminal = ['bash', 'shell', 'sh', 'zsh', 'terminal'].includes(language);
    blocks.push(isTerminal
      ? `<div class="terminal-block"><div class="terminal-header"><span class="terminal-dot"></span><span>${language}</span></div><div class="terminal-body">${code}</div></div>`
      : `<div class="code-block-shell"><div class="code-block-header"><span>${language}</span></div><pre><code>${code}</code></pre></div>`);
    codeFence = null;
  }

  function flushList() {
    if (!listItems.length) return;
    const collapseId = `change-list-${Math.random().toString(36).slice(2, 8)}`;
    blocks.push(`
      <div class="change-list">
        <div class="change-item">
          <div class="change-item-header">
            <span class="change-item-title">改动清单</span>
            <span class="change-item-meta">${listItems.length} 项</span>
          </div>
          <div class="change-item-body">
            <ul class="markdown-list grouped-list">${listItems.join('')}</ul>
          </div>
        </div>
      </div>`);
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
      flushParagraph();
      flushList();
      return;
    }

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

  const markdownHtml = blocks.join('')
    .replace(/<li>/g, '<div class="change-bullet"><div>')
    .replace(/<\/li>/g, '</div></div>');

  const diffHtml = renderDiffBlocks(text || '');

  return `<div class="message-section"><div class="message-body">${structured || ''}<div class="tool-call-markdown">${diffHtml || ''}${markdownHtml}</div></div></div>`;
}

let shouldAutoFollowChat = true;
let lastConversationScrollTop = 0;
const CHAT_FOLLOW_THRESHOLD = 96;

function isNearChatBottom(chatArea) {
  if (!chatArea) return true;
  const remaining = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight;
  return remaining <= CHAT_FOLLOW_THRESHOLD;
}

function syncChatFollowState(chatArea) {
  if (!chatArea) return;
  shouldAutoFollowChat = isNearChatBottom(chatArea);
  lastConversationScrollTop = chatArea.scrollTop;
}

function ensureChatScrollTracking(chatArea) {
  if (!chatArea || chatArea.dataset.scrollTrackingBound === 'true') return;
  chatArea.dataset.scrollTrackingBound = 'true';
  chatArea.addEventListener('scroll', () => {
    syncChatFollowState(chatArea);
  }, { passive: true });
}

document.addEventListener('click', (event) => {
  const toolCallToggle = event.target.closest('[data-tool-call-toggle]');
  if (toolCallToggle) {
    const container = toolCallToggle.closest('[data-tool-call-container]');
    if (!container) {
      return;
    }

    const collapsed = container.classList.toggle('collapsed');
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
    diffToggle.textContent = collapsed ? '展开完整内容' : '收起内容';
    diffToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    return;
  }
});

function renderMessages(forceScrollToBottom = false) {
  const active = getActiveConversation();
  const chatArea = document.getElementById('chat-area');
  const previousScrollTop = chatArea.scrollTop;
  const shouldStickToBottom = forceScrollToBottom || shouldAutoFollowChat || isNearChatBottom(chatArea);
  ensureChatScrollTracking(chatArea);
  chatArea.innerHTML = '';

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

  active.messages.forEach((message) => {
    const element = document.createElement('div');
    element.className = `message ${message.role}`;
    const text = String(message.content ?? '').trim();
    if (message.role === 'task') {
      renderTaskCard(element, message, active.id);
    } else if (message.role === 'approval') {
      renderApprovalCard(element, message);
    } else if (message.role === 'system') {
      element.textContent = text;
    } else {
      const body = document.createElement('div');
      body.className = 'message-body';
      body.innerHTML = renderMarkdown(text);
      element.appendChild(body);

      // Attachments render under the text so the prompt stays readable.
      if (message.images?.length) {
        const gallery = document.createElement('div');
        gallery.className = 'message-images';
        message.images.forEach((image) => {
          const thumb = document.createElement('img');
          thumb.src = image.image_url;
          thumb.alt = image.name || '附件图片';
          thumb.title = image.name || '附件图片';
          thumb.onclick = () => window.open(image.image_url, '_blank');
          gallery.appendChild(thumb);
        });
        element.appendChild(gallery);
      }
    }
    chatArea.appendChild(element);
  });

  // The trace pane is a sibling view, not a tail step of chat scrolling. It must
  // render on every pass; keeping it after the scroll branches meant the whole
  // panel stayed blank in the common "stick to bottom" case.
  renderTracePane(active);

  if (shouldStickToBottom) {
    chatArea.scrollTop = chatArea.scrollHeight;
    shouldAutoFollowChat = true;
    lastConversationScrollTop = chatArea.scrollTop;
    return;
  }

  const maxScrollTop = Math.max(0, chatArea.scrollHeight - chatArea.clientHeight);
  chatArea.scrollTop = Math.min(lastConversationScrollTop || previousScrollTop || 0, maxScrollTop);
}

function renderTracePane(conversation) {
  const tracePane = document.getElementById('trace-pane');
  if (!tracePane) return;
  if (!conversation) {
    tracePane.innerHTML = '';
    return;
  }
  renderTraceView(tracePane, conversation);
}

const 风险文案 = { R0: '无副作用', R1: '低风险', R2: '需授权', R3: '高风险' };

function taskStateClass(task) {
  const state = task?.projection?.display_status || task?.phase1State;
  if (state === 'completed' || state === 'DONE') return 'done';
  if (state === 'failed' || state === 'cancelled' || state === 'FAILED' || state === 'CANCELLED') return 'failed';
  if (state === 'waiting' || state === 'interrupted' || state === 'WAITING' || state === 'INTERRUPTED') return 'waiting';
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
    cancelled: 'CANCELLED', completed: 'DONE',
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
      cancelled: '已取消', completed: '已完成',
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

function buildTraceSteps(task) {
  const sortTraceSteps = (items) => items.sort((left, right) => {
    const leftSequence = Number(left.startSequence || 0);
    const rightSequence = Number(right.startSequence || 0);
    return leftSequence - rightSequence;
  });

  const projectedTrace = taskProjection(task)?.trace;
  if (Array.isArray(projectedTrace) && projectedTrace.length) {
    const state = {
      pending: 'running', running: 'running', completed: 'done',
      failed: 'failed', cancelled: 'failed', unknown: 'failed',
      waiting_approval: 'waiting', waiting_input: 'waiting', skipped: 'done',
    };
    return sortTraceSteps(projectedTrace.map((item) => ({
      title: item.title,
      body: item.wait_reason || item.kind,
      state: state[item.status] || 'running',
      meta: item.sequence_end
        ? `#${item.sequence_start}–#${item.sequence_end}`
        : `#${item.sequence_start}`,
      startSequence: Number(item.sequence_start || 0),
    })));
  }
  const steps = [];
  const pendingTools = [];
  let pendingModel = null;
  (task.progress || []).forEach((item) => {
    const progress = item.progressData || {};
    const kind = progress.kind || '';
    const toolName = progress.tool_name || progress.operation || '';
    const sequence = Number(item.sequence || 0);
    const text = item.text || '执行中';
    if (kind === 'tool_started') {
      steps.push({
        title: toolName ? `调用工具：${toolName}` : '调用工具',
        body: text, state: 'running',
        meta: `#${sequence || steps.length + 1}`,
        startSequence: sequence, toolName,
      });
      pendingTools.push(steps.length - 1);
      return;
    }
    if (kind === 'tool_completed') {
      const index = pendingTools.findLastIndex((candidate) => {
        const step = steps[candidate];
        return step.state === 'running'
          && (!toolName || !step.toolName || step.toolName === toolName);
      });
      const state = traceStateForProgress(progress);
      if (index >= 0) {
        const stepIndex = pendingTools.splice(index, 1)[0];
        const step = steps[stepIndex];
        step.state = state;
        step.body = `${step.body}\\n${text}`;
        step.meta = `#${step.startSequence || sequence}–#${sequence}`;
      } else {
        steps.push({
          title: toolName ? `工具结果：${toolName}` : '工具结果',
          body: text, state,
          meta: `#${sequence || steps.length + 1}`,
          startSequence: sequence, toolName,
        });
      }
      return;
    }
    if (kind === 'model_started') {
      steps.push({
        title: '模型调用', body: text, state: 'running',
        meta: `#${sequence || steps.length + 1}`,
        startSequence: sequence,
      });
      pendingModel = steps.length - 1;
      return;
    }
    if (kind === 'model_completed' && pendingModel !== null) {
      const step = steps[pendingModel];
      step.state = 'done';
      step.body = `${step.body}\\n${text}`;
      step.meta = `#${step.startSequence || sequence}–#${sequence}`;
      pendingModel = null;
      return;
    }
    if (kind === 'waiting') {
      // The authoritative current waiting payload below has richer structure
      // (approval versus text input). Do not preserve an old generic progress
      // line that would otherwise claim every wait is "等待输入".
      return;
    }
    steps.push({
      title: '运行事件',
      body: text,
      state: traceStateForProgress(progress),
      meta: `#${sequence || steps.length + 1}`,
      startSequence: sequence,
    });
  });
  if (task.waiting) {
    const approval = task.waiting.approval;
    steps.push({
      title: task.waiting.kind === 'APPROVAL'
        ? '等待用户授权' : '等待用户输入',
      body: approval?.action || task.waiting.question || '任务已暂停，等待用户操作。',
      state: 'waiting',
      meta: approval?.request_id || 'pending',
      startSequence: 0,
    });
  }
  return sortTraceSteps(steps);
}

function renderTraceView(tracePane, conversation) {
  tracePane.innerHTML = '';
  const tasks = conversation.messages.filter((item) => item.role === 'task');

  const futureBoard = document.createElement('div');
  futureBoard.className = 'trace-future-grid';
  futureBoard.innerHTML = `
    <div class='trace-future-card'>
      <div class='trace-future-title'>流程节点图容器</div>
      <div class='trace-future-copy'>兼容 node / edge / lane 数据结构，可升级为真实 Agent Graph 与工具调用链路。</div>
    </div>
    <div class='trace-future-card'>
      <div class='trace-future-title'>实时 Timeline 容器</div>
      <div class='trace-future-copy'>兼容 sequence、cursor 与阶段聚合，可扩展为时间轴回放与事件过滤。</div>
    </div>
  `;
  tracePane.appendChild(futureBoard);

  const board = document.createElement('div');
  board.className = 'trace-live-board';

  const traceGraphMeta = {
    nodeCount: tasks.length,
    edgeCount: Math.max(tasks.length - 1, 0),
    liveCursor: tasks.reduce((max, item) => Math.max(max, Number(item.task?.traceCursor || 0)), 0),
  };
  traceVisualizationState.graph.nodes = tasks.map((item) => ({
    id: item.task?.taskId,
    type: 'task',
    status: item.task?.status,
  }));
  traceVisualizationState.timeline.liveCursor = traceGraphMeta.liveCursor;

  const main = document.createElement('div');
  main.className = 'trace-main';

  if (tasks.length === 0) {
    main.innerHTML = `<div class='empty-state'><h2>暂无执行链路</h2><div>发送任务后会生成 Agent Trace Timeline</div></div>`;
  } else {
    tasks.forEach((message) => {
      const wrapper = document.createElement('div');
      wrapper.className = 'message task';
      renderTaskCard(wrapper, message, conversation.id, 'trace');
      main.appendChild(wrapper);
    });
  }

  const sidebar = document.createElement('div');
  sidebar.className = 'trace-sidebar';
  const overview = document.createElement('div');
  overview.className = 'trace-overview-panel';
  const taskCount = tasks.length;
  const runningCount = tasks.filter((item) => (
    item.task?.projection?.execution_status === 'running'
    || (!item.task?.projection && item.task?.phase1State === 'RUNNING')
  )).length;
  const waitingCount = tasks.filter((item) => (
    item.task?.projection?.display_status === 'waiting'
    || (!item.task?.projection && item.task?.phase1State === 'WAITING')
  )).length;
  overview.innerHTML = `
    <div class='trace-panel-heading'>
      <div>
        <div class='trace-panel-title'>Trace Overview</div>
        <div class='trace-panel-caption'>聚合关键状态、统计与执行阶段摘要</div>
      </div>
      <div class='pane-badge'>Live Trace</div>
    </div>
    <div class='trace-summary-strip'>
      <div class='trace-metric'><div class='trace-metric-label'>任务节点</div><div class='trace-metric-value'>${taskCount}</div></div>
      <div class='trace-metric'><div class='trace-metric-label'>运行中</div><div class='trace-metric-value'>${runningCount}</div></div>
      <div class='trace-metric'><div class='trace-metric-label'>等待确认</div><div class='trace-metric-value'>${waitingCount}</div></div>
      <div class='trace-metric'><div class='trace-metric-label'>会话消息</div><div class='trace-metric-value'>${conversation.messages.length}</div></div>
    </div>
    <div class='trace-legend-grid'>
      <div class='trace-legend-item'><span class='trace-dot running'></span>执行中</div>
      <div class='trace-legend-item'><span class='trace-dot waiting'></span>等待操作</div>
      <div class='trace-legend-item'><span class='trace-dot done'></span>已完成</div>
      <div class='trace-legend-item'><span class='trace-dot failed'></span>失败/中断</div>
    </div>
  `;
  sidebar.append(overview);
  board.append(main, sidebar);
  tracePane.appendChild(board);
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

  const progress = document.createElement('div');
  progress.className = `task-progress task-panel ${mode === 'conversation' ? 'active' : ''}`;
  const lines = task.progress || [];
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
      line.textContent = item.text;
      progress.appendChild(line);
    });
  }
  card.appendChild(progress);

  const tracePanel = document.createElement('div');
  tracePanel.className = `task-panel ${mode === 'trace' ? 'active' : ''}`;
  const timelineScroll = document.createElement('div');
  timelineScroll.className = 'trace-timeline-scroll';
  const timeline = document.createElement('div');
  timeline.className = 'trace-timeline';
  const steps = buildTraceSteps(task);
  if (steps.length === 0) {
    const emptyTrace = document.createElement('div');
    emptyTrace.className = 'task-progress-empty';
    emptyTrace.textContent = '等待 Agent 产生执行事件后，这里会展示完整 Timeline。';
    tracePanel.appendChild(emptyTrace);
  } else {
    steps.forEach((step) => {
      const item = document.createElement('div');
      item.className = `trace-step ${step.state}`;
      item.innerHTML = `
        <div class='trace-step-header'>
          <div>
            <div class='trace-step-title'>${step.title}</div>
            <div class='trace-step-meta'>${step.meta}</div>
          </div>
          <div class='task-phase-badge'>${step.state}</div>
        </div>
        <div class='trace-step-body'>${step.body}</div>
      `;
      timeline.appendChild(item);
    });
    timelineScroll.appendChild(timeline);
    tracePanel.appendChild(timelineScroll);
    requestAnimationFrame(() => {
      timelineScroll.scrollTop = timelineScroll.scrollHeight;
    });
  }
  card.appendChild(tracePanel);

  if (task.waiting?.kind === 'APPROVAL' && task.waiting.approval) {
    const approval = document.createElement('div');
    approval.className = 'message approval';
    const approvalRequest = task.waiting.approval;
    const resolution = task.approvalResolution;
    renderApprovalCard(approval, {
      approval: approvalRequest,
      taskId: task.taskId,
      resolved: (
        resolution?.requestId === approvalRequest.request_id
          ? resolution.decision : null
      ),
      conversationId,
    });
    card.appendChild(approval);
  } else if (task.waiting) {
    const waiting = document.createElement('div');
    waiting.className = 'task-progress-line';
    waiting.textContent = task.waiting.question || '该任务正在等待你的输入。';
    card.appendChild(waiting);
  }
  element.appendChild(card);
}

function taskMessage(conversationId, taskId) {
  const conversation = getConversationById(conversationId);
  return conversation?.messages.find(
    (item) => item.role === 'task' && item.task?.taskId === taskId,
  );
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
        traceCursor: Number(rawTask.projection?.trace_cursor || rawTask.progress_cursor || 0),
        cursor: Number(rawTask.progress_cursor || 0),
        progress: [], waiting: rawTask.waiting || null,
      },
    };
    applyTaskProjection(message.task, rawTask.projection);
    conversation.messages.push(message);
  } else {
    message.task.goal = rawTask.goal || message.task.goal;
    message.task.phase1State = rawTask.phase1_state || message.task.phase1State;
    message.task.status = rawTask.status || message.task.status;
    message.task.cursor = Math.max(
      Number(message.task.cursor || 0), Number(rawTask.progress_cursor || 0),
    );
    message.task.waiting = rawTask.waiting ?? message.task.waiting;
    applyTaskProjection(message.task, rawTask.projection);
  }
  if (conversationId === activeConversationId) renderMessages();
  return message;
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
  if (
    lines[lines.length - 1]?.text === text
    && lines[lines.length - 1]?.progressData?.kind === progressData.kind
  ) return;
  lines.push({ sequence, text, progressData });
  if (lines.length > 30) lines.splice(0, lines.length - 30);
  if (conversationId === activeConversationId) renderMessages();
}

function renderApprovalCard(element, message) {
  const approval = message.approval || {};

  const hero = document.createElement('div');
  hero.className = 'approval-hero';

  const heading = document.createElement('div');
  heading.innerHTML = `
    <div class="approval-title">授权确认</div>
    <div class="approval-subtitle">请确认以下操作内容与影响范围。新版授权面板会展示风险等级、目标与变更预览，避免误操作。</div>
  `;

  const risk = document.createElement('div');
  risk.className = 'approval-risk';
  risk.textContent = 风险文案[approval.risk] || approval.risk || '未知风险';

  hero.appendChild(heading);
  hero.appendChild(risk);
  element.appendChild(hero);

  const grid = document.createElement('div');
  grid.className = 'approval-grid';

  const rows = [
    ['动作', approval.action],
    ['目标', approval.target],
    ['网络访问', approval.network_access ? '允许外网访问' : '仅本地操作'],
    ['数据外发', approval.data_transmission || 'none'],
    ['可回滚', approval.rollback || '未知'],
  ];

  rows.forEach(([label, value]) => {
    if (!value && value !== false) return;
    const item = document.createElement('div');
    item.className = 'approval-item';
    item.innerHTML = `
      <div class="approval-label">${label}</div>
      <div class="approval-value">${renderInlineMarkdown(String(value))}</div>
    `;
    grid.appendChild(item);
  });

  element.appendChild(grid);

  if (approval.preview) {
    const preview = document.createElement('pre');
    preview.className = 'approval-preview';
    preview.textContent = approval.preview;
    element.appendChild(preview);
  }

  const actions = document.createElement('div');
  actions.className = 'approval-actions';
  if (message.resolved) {
    const done = document.createElement('div');
    done.className = 'approval-row';
    done.textContent = message.resolved === 'APPROVE' ? '已允许' : '已拒绝';
    actions.appendChild(done);
  } else {
    const approve = document.createElement('button');
    approve.className = 'approval-btn approve';
    approve.textContent = '允许';
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
  if (card) {
    card.task.approvalResolution = {
      requestId: approval.request_id, decision,
    };
    card.task.waiting = decision === 'APPROVE' ? null : card.task.waiting;
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
  if (card.task.approvalResolution?.requestId !== approval.request_id) {
    card.task.approvalResolution = null;
  }
  if (conversationId === activeConversationId) renderMessages();
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
  if (/context cannot fit/.test(reason)) {
    return '输入内容超出模型上下文上限，请缩小图片尺寸或减少附件后重试。';
  }
  return reason;
}

function describeProgress(progress) {
  if (!progress) return '';
  const kind = progress.kind;
  if (kind === 'model_started') return '正在思考…';
  if (kind === 'model_completed') return '已生成一轮回复';
  if (kind === 'tool_started') {
    return `调用工具：${progress.tool_name}${progress.operation_presentation ? ' · ' + progress.operation_presentation : ''}`;
  }
  if (kind === 'tool_completed') {
    const status = progress.ok === false
      ? `失败${progress.error_code ? '（' + progress.error_code + '）' : ''}`
      : '完成';
    return `工具 ${progress.tool_name} ${status}`;
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

async function runTask() {
  clearError();
  if (!activeWorkspaceId) {
    showError('发送消息前请先选择工作区。');
    return;
  }
  if (!getActiveConversation()) {
    createConversation();
  }

  const promptElement = document.getElementById('prompt');
  const prompt = promptElement.value.trim();
  if (!prompt) {
    showError('请输入内容后再发送。');
    return;
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
    sessionStorage.removeItem(draftStorageKey(conversationId));
  }
  selectedImages.length = 0;
  renderPreviews();
  setLoading(true);
  const requestId = crypto.randomUUID();
  try {
    const response = await fetch('/session-input', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        text: prompt,
        request_id: requestId,
        workspace_id: workspaceId,
        session_id: conversationId,
        images,
      })
    });
    if (!response.ok) {
      throw new Error(`请求失败，状态码 ${response.status}`);
    }

    const data = await response.json();
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
    const card = upsertTaskCard(conversationId, {
      task_id: task.task_id,
      goal: prompt,
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
    } else if (
      task.phase1_state === 'WAITING'
      || task.status === 'awaiting_user'
      || task.status === 'awaiting_approval'
    ) {
      appendTaskAssistantText(task.task_id, task.assistant_text, conversationId);
      if (card) card.task.waiting = card.task.waiting || { kind: 'INPUT' };
      setLoading(false);
      renderMessages();
    } else {
      followTask(task.task_id, conversationId);
    }
  } catch (error) {
    console.error(error);
    showError(error.message || '发送消息时发生未知错误。');
    setLoading(false);
  }
}

const 状态文案 = {
  PREPARING: '准备中', RUNNING: '执行中', WAITING: '等待操作',
  INTERRUPTED: '已中断', DONE: '已完成', FAILED: '已失败',
  CANCELLED: '已取消',
};
const TERMINAL_STATES = ['DONE', 'FAILED', 'CANCELLED'];
const MAX_STREAM_RETRIES = 6;
const followers = new Map();

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
  const duplicate = conversation?.messages.some(
    (item) => item.role === 'assistant'
      && item.taskId === taskId
      && item.content === assistantText,
  );
  if (duplicate) return false;
  appendMessage('assistant', assistantText, [], conversationId, taskId);
  return true;
}

function renderFinalResult(taskId, state, conversationId) {
  const card = taskMessage(conversationId, taskId)
    || upsertTaskCard(conversationId, { task_id: taskId });
  if (card) {
    applyTaskProjection(card.task, state.projection);
    card.task.phase1State = state.phase1_state || card.task.phase1State;
    card.task.status = state.status || card.task.status;
    card.task.waiting = null;
  }
  if (state.assistant_text) {
    appendTaskAssistantText(taskId, state.assistant_text, conversationId);
  } else if (state.clarification?.question) {
    appendMessage(
      'assistant', state.clarification.question, [], conversationId, taskId,
    );
  } else if (state.failure_reason) {
    if (conversationId === activeConversationId) {
      showError(describeFailure(state.failure_reason));
    }
    appendTaskProgress(taskId, {
      sequence: 0,
      progress: {
        activity: `任务失败：${describeFailure(state.failure_reason)}`,
      },
    }, conversationId);
  }
  if (conversationId === activeConversationId) renderMessages();
}

// Each follower owns exactly one Task card. Its cursor is part of that card,
// therefore reconnecting cannot replay another Task's history into the current
// conversation timeline.
function followTask(taskId, conversationId) {
  const existing = followers.get(taskId);
  if (existing) existing.stop();
  stopConversationFollowers(conversationId, taskId);
  const card = taskMessage(conversationId, taskId)
    || upsertTaskCard(conversationId, { task_id: taskId, phase1_state: 'RUNNING' });
  const follower = {
    conversationId,
    attempts: 0, stopped: false, source: null, timer: null,
    stop() {
      this.stopped = true;
      if (this.source) this.source.close();
      if (this.timer) clearInterval(this.timer);
      followers.delete(taskId);
    },
  };
  followers.set(taskId, follower);

  const finish = (state) => {
    renderFinalResult(taskId, state, conversationId);
    follower.stop();
    setLoading(false);
  };

  const reconcile = async () => {
    const response = await fetch(`/tasks/${taskId}`);
    if (!response.ok) return false;
    const state = await response.json();
    const current = taskMessage(conversationId, taskId) || card;
    appendTaskAssistantText(taskId, state.assistant_text, conversationId);
    if (current) {
      applyTaskProjection(current.task, state.projection);
      if (!state.projection) {
        current.task.phase1State = state.phase1_state;
        current.task.status = state.status;
      }
      current.task.waiting = state.approval
        ? { kind: 'APPROVAL', approval: state.approval }
        : state.status === 'awaiting_user'
          ? { kind: state.clarification?.kind || 'INPUT', ...state.clarification }
          : null;
    }
    if (TERMINAL_STATES.includes(state.phase1_state)) {
      finish(state);
      return true;
    }
    const submittedApprovalIsSettling = Boolean(
      state.approval
      && current?.task?.approvalResolution?.decision === 'APPROVE'
      && current.task.approvalResolution.requestId === state.approval.request_id
    );
    if (
      (state.phase1_state === 'WAITING' || state.status === 'awaiting_user')
      && !submittedApprovalIsSettling
    ) {
      if (conversationId === activeConversationId) renderMessages();
      follower.stop();
      setLoading(false);
      return true;
    }
    if (conversationId === activeConversationId) renderMessages();
    return false;
  };

  const connect = () => {
    if (follower.stopped) return;
    const current = taskMessage(conversationId, taskId) || card;
    const after = Number(current?.task?.cursor || 0);
    const source = new EventSource(`/stream/${taskId}?after=${after}`);
    follower.source = source;
    source.onopen = () => { follower.attempts = 0; };
    source.onmessage = (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (error) {
        console.error(error);
        return;
      }
      const sequence = Number(event.lastEventId || payload.sequence || 0);
      if (sequence > 0) {
        const currentCard = taskMessage(conversationId, taskId);
        if (currentCard) {
          currentCard.task.cursor = Math.max(
            Number(currentCard.task.cursor || 0), sequence,
          );
        }
      }
      if (payload.waiting) {
        appendTaskAssistantText(taskId, payload.assistant_text, conversationId);
        const currentCard = taskMessage(conversationId, taskId);
        if (currentCard) {
          applyTaskProjection(currentCard.task, payload.projection);
          if (!payload.projection) currentCard.task.phase1State = 'WAITING';
          currentCard.task.waiting = payload.waiting;
        }
        if (conversationId === activeConversationId) renderMessages();
        const submittedApprovalIsSettling = Boolean(
          payload.waiting.kind === 'APPROVAL'
          && currentCard?.task?.approvalResolution?.decision === 'APPROVE'
          && currentCard.task.approvalResolution.requestId
            === payload.waiting.approval?.request_id
        );
        if (!submittedApprovalIsSettling) {
          follower.stop();
          setLoading(false);
        }
        return;
      }
      if (payload.final) {
        finish(payload);
        return;
      }
      appendTaskProgress(taskId, payload, conversationId);
    };
    source.onerror = async () => {
      source.close();
      if (follower.stopped) return;
      if (await reconcile()) return;
      follower.attempts += 1;
      if (follower.attempts > MAX_STREAM_RETRIES) {
        appendTaskProgress(taskId, {
          sequence: 0,
          progress: {
            activity: '实时输出连接已断开；任务仍在后台运行，可刷新页面查看结果。',
          },
        }, conversationId);
        follower.stop();
        setLoading(false);
        return;
      }
      setTimeout(connect, Math.min(1000 * follower.attempts, 5000));
    };
  };

  follower.timer = setInterval(() => {
    reconcile().catch((error) => console.error(error));
  }, 2500);
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
      });
    });
  } catch (error) {
    console.error(error);
  }
}

async function ensureConversationLoaded(conversationId) {
  const conversation = getConversationById(conversationId);
  if (!conversation || conversation.loaded) return;
  conversation.loaded = true;
  try {
    const response = await fetch(`/sessions/${conversationId}`);
    if (!response.ok) return;
    const data = await response.json();
    const tasksById = new Map(
      (data.tasks || []).map((task) => [task.task_id, task]),
    );
    const timeline = [];
    let insertionOrder = 0;
    const visibleAssistantTaskIds = new Set();
    (data.messages || []).forEach((message) => {
      const task = tasksById.get(message.task_id);
      if (message.role === 'assistant' && message.task_id) {
        visibleAssistantTaskIds.add(message.task_id);
      }
      // A Task records its input at attachment time, so a user message already
      // sorts correctly. Older records only kept it on the result event, which
      // lands after the Task card, so fall back to the attachment position.
      const sequence = message.role === 'user' && task
        ? Math.min(
            Number(message.source_event_sequence),
            Number(task.attached_sequence),
          )
        : Number(message.source_event_sequence);
      timeline.push({
        sequence, order: message.role === 'user' ? 0 : 2,
        insertionOrder: insertionOrder++,
        message: {
          role: message.role, content: message.content, images: [],
          taskId: message.task_id || null,
        },
      });
    });
    (data.tasks || []).forEach((task) => {
      timeline.push({
        sequence: Number(task.attached_sequence || 0),
        order: 1,
        insertionOrder: insertionOrder++,
        message: {
          role: 'task', content: '', task: {
            taskId: task.task_id, goal: task.goal,
            projection: task.projection || null,
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
          },
        });
      }
    });
    timeline.sort((left, right) => (
      left.sequence - right.sequence
      || left.order - right.order
      || left.insertionOrder - right.insertionOrder
    ));
    conversation.messages = timeline.map((item) => item.message);
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
  } catch (error) {
    conversation.loaded = false;
    console.error(error);
  }
}

async function loadWorkspaces() {
  try {
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

const promptComposer = document.getElementById('prompt');
let imeComposing = false;

promptComposer.addEventListener('input', () => {
  persistActiveDraft();
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

loadWorkspaces();
</script>
</body>
</html>
""".replace("__WEBUI_VERSION__", WEBUI_VERSION)

runtime_server: LocalEventApiServer | None = None
runtime_error: str | None = None
workspace_registry: list[dict[str, str]] = []
session_workspace_bindings: dict[str, str] = {}


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
    for session in sessions:
        for task_id in reversed(session.task_ids):
            try:
                task = runtime_server._call(kernel.get_task(task_id))
            except (LookupError, PermissionError):
                continue
            path = str(task.workspace)
            if path in known:
                break
            if not Path(path).is_dir():
                continue
            workspace_registry.append({
                "id": workspace_identifier(path),
                "name": Path(path).name or path,
                "path": path,
            })
            known.add(path)
            changed = True
            break
    if changed:
        save_workspace_registry()


@app.on_event("startup")
async def startup() -> None:
    global runtime_server, runtime_error
    load_workspace_registry()
    load_session_workspace_bindings()
    runtime_server = LocalEventApiServer(Path.cwd())
    try:
        runtime_server.start()
        recover_workspace_registry_from_runtime()
        runtime_error = None
    except Exception as error:
        runtime_server = None
        runtime_error = str(error)


@app.on_event("shutdown")
async def shutdown() -> None:
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
    return {"workspaces": workspace_registry}


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
    existing = next(
        (item for item in workspace_registry if item["path"] == workspace_path),
        None,
    )
    if existing is not None:
        return existing

    workspace = {
        "id": workspace_identifier(workspace_path),
        "name": resolved.name or workspace_path,
        "path": workspace_path,
    }
    workspace_registry.append(workspace)
    save_workspace_registry()
    return workspace


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


@app.post("/session-input")
async def submit_session_input(payload: dict) -> dict:
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

    async def ensure_session() -> None:
        try:
            await runtime_server._client.application.kernel.get_session(session_id)
        except LookupError:
            await runtime_server._client.application.kernel.create_session(
                text[:120], session_id=session_id
            )

    runtime_server._call(ensure_session())
    # Attachments stay structured. Inlining base64 into the text would blow
    # past the Task SPEC goal limit and fail task creation outright.
    image_blocks = _normalize_image_blocks(payload.get("images"))
    try:
        result = runtime_server._call(runtime_server._client.submit_session_text(
            session_id, text, command_id=request_id,
            images=tuple(image_blocks),
            workspace=Path(workspace["path"]),
        ))
    except AgentLoopLimitExceeded as error:
        # A spent call budget is a bounded Runtime outcome with a known cause,
        # not an internal fault. Reporting it as 500 hides the only actionable
        # detail the user has: how much was spent and what blocked last.
        raise HTTPException(
            status_code=409, detail=_describe_loop_limit(error),
        ) from error
    except ValueError as error:
        # A rejected workspace is a caller-visible conflict, most often a
        # Session whose earlier Tasks ran in a different directory.
        raise HTTPException(status_code=409, detail=str(error)) from error
    data = dict(result.result)
    status = 202 if data.get("kind") == "task" else 200
    return JSONResponse(content=data, status_code=status)


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
    )[:max(1, min(limit, 200))]:
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
            "progress_cursor": (
                runtime_server._client.latest_progress_sequence(task_id)
            ),
            "waiting": _waiting_payload(result),
            "assistant_text": result.assistant_text,
            "projection": dict(result.projection) if result.projection else None,
            "is_active": task_id == snapshot.active_task_id,
        })
    latest = task_items[-1] if task_items else None
    return {
        "session_id": snapshot.session_id,
        "title": snapshot.title,
        "active_task_id": snapshot.active_task_id,
        "latest_task_id": latest["task_id"] if latest else None,
        "latest_task_state": latest["phase1_state"] if latest else None,
        "waiting": latest["waiting"] if latest else None,
        "tasks": task_items,
        "messages": [
            {
                "role": message.role.value,
                "content": message.text,
                "task_id": message.task_id,
                "source_event_sequence": message.source_event_sequence,
            }
            for message in projection.messages
        ],
    }


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


@app.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    if runtime_server is None:
        raise HTTPException(
            status_code=503,
            detail=runtime_error or "runtime unavailable",
        )
    result = runtime_server._call(runtime_server._client.get_task_result(task_id))
    data = result.to_data()
    if result.phase1_state == "FAILED":
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
    if result.approval is not None:
        return {"kind": "APPROVAL", "approval": dict(result.approval)}
    if result.status == "awaiting_user":
        clarification = (
            dict(result.clarification) if result.clarification else {}
        )
        return {
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
        if event.event_type.endswith(".failed"):
            message = str(event.payload.get("message", "")).strip()
            if message:
                return message
    return None


@app.get("/stream/{task_id}")
async def stream(task_id: str, after: int = 0):
    if runtime_server is None:
        raise HTTPException(
            status_code=503,
            detail=runtime_error or "runtime unavailable",
        )
    if after < 0:
        raise HTTPException(status_code=400, detail="after must not be negative")

    async def event_generator():
        cursor = after
        announced_wait: str | None = None
        while True:
            items = runtime_server._client.read_progress(task_id, after=cursor)
            for item in items:
                cursor = max(cursor, item.sequence)
                yield (
                    f"id: {item.sequence}\n"
                    f"data: {json.dumps(item.to_data(), ensure_ascii=False)}\n\n"
                )

            result = runtime_server._call(
                runtime_server._client.get_task_result(task_id)
            )
            # A suspended Turn produces no further progress until the user acts,
            # so the wait itself has to reach the page; otherwise the UI shows a
            # spinner over a Task that is asking for a decision.
            waiting = _waiting_payload(result)
            signature = json.dumps(
                {"waiting": waiting, "assistant_text": result.assistant_text},
                ensure_ascii=False, sort_keys=True,
            )
            if waiting is not None and signature != announced_wait:
                announced_wait = signature
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "task_id": task_id,
                            "waiting": waiting,
                            "projection": result.projection,
                            # A Task may generate a useful response and then
                            # suspend for clarification or continuation. Waiting
                            # must not hide that already durable response.
                            "assistant_text": result.assistant_text,
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
            elif waiting is None:
                announced_wait = None
            if result.phase1_state in {"DONE", "FAILED", "CANCELLED"}:
                # Progress is ephemeral; the final answer only lives on the
                # Task result, so the stream has to deliver it before closing.
                # A failed Task has no answer at all, so surface the recorded
                # reason instead of an empty bubble.
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "task_id": task_id,
                            "final": True,
                            "projection": result.projection,
                            "phase1_state": result.phase1_state,
                            "assistant_text": result.assistant_text,
                            "clarification": result.clarification,
                            "failure_reason": _task_failure_reason(task_id)
                            if result.phase1_state == "FAILED" else None,
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                return
            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def main() -> None:
    uvicorn.run(
        "tsm_agt.web.app:app",
        host="127.0.0.1",
        port=8080,
        reload=False,
    )


if __name__ == "__main__":
    main()
