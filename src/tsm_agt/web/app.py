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
:root{color-scheme:dark;--bg:#111213;--panel:#191a1c;--border:#2a2c30;--text:#e8eaed;--muted:#9aa0a6;--accent:#5b8cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
button{font:inherit;color:inherit;background:none;border:none;cursor:pointer;border-radius:6px}
button:disabled{opacity:.4;cursor:not-allowed}
.layout{display:grid;grid-template-columns:260px 1fr;height:100vh;overflow:hidden}
.sidebar{border-right:1px solid var(--border);padding:16px 12px;display:flex;flex-direction:column;gap:16px;min-height:0;height:100vh;overflow-y:auto;overscroll-behavior:contain}
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
.list-item:hover{background:#202225}
.list-item.active{background:#26282c}
.item-title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item-meta{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl;text-align:left}
.hint{font-size:12px;color:var(--muted);padding:6px}
.main{display:flex;flex-direction:column;min-width:0;min-height:0;height:100vh;overflow:hidden}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 20px;border-bottom:1px solid var(--border)}
#conversation-name{font-weight:500}
.topbar .icon-btn{font-size:12px}
.chat-shell{flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden}
.view-switcher{display:flex;align-items:center;justify-content:space-between;padding:12px 20px 0;gap:12px}
.view-tabs{display:flex;gap:8px}
.view-tab{padding:8px 14px;border:1px solid var(--border);color:var(--muted);background:#16181b}
.view-tab.active{background:#273246;border-color:#3d5d92;color:#fff}
.view-summary{font-size:12px;color:var(--muted)}
.chat-area{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;padding:20px;display:flex;flex-direction:column;gap:10px}
.trace-board{display:grid;grid-template-columns:minmax(0,1.2fr) 320px;gap:18px;align-items:start}
.trace-main{display:flex;flex-direction:column;gap:14px}
.trace-sidebar{position:sticky;top:0;display:flex;flex-direction:column;gap:12px}
.trace-summary-card,.trace-legend{background:#16181b;border:1px solid var(--border);border-radius:12px;padding:14px}
.trace-summary-title{font-size:13px;font-weight:600;margin-bottom:10px}
.trace-metric-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.trace-metric{background:#1d2025;border-radius:10px;padding:10px}
.trace-metric-label{font-size:11px;color:var(--muted)}
.trace-metric-value{font-size:18px;font-weight:600;margin-top:4px}
.trace-legend-item{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}
.trace-dot{width:10px;height:10px;border-radius:50%}
.trace-dot.running{background:#8ab4f8}.trace-dot.waiting{background:#f0c674}.trace-dot.done{background:#81c995}.trace-dot.failed{background:#f4837a}
.message{max-width:70%;word-break:break-word}
.message-body{white-space:pre-wrap}
.message-images{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.message-images img{width:56px;height:56px;object-fit:cover;border-radius:6px;display:block;cursor:zoom-in;border:1px solid rgba(255,255,255,.12)}
.message.user{align-self:flex-end;background:#2f3a4f;padding:8px 12px;border-radius:12px 12px 2px 12px}
.message.assistant{align-self:flex-start;background:var(--panel);padding:8px 12px;border-radius:12px 12px 12px 2px}
.message.system{align-self:flex-start;max-width:100%;color:var(--muted);font-size:12px;padding:0 2px}
.message.task{align-self:stretch;max-width:100%;padding:0}
.task-card{border:1px solid var(--border);border-radius:14px;background:#151719;padding:14px;display:flex;flex-direction:column;gap:12px;box-shadow:0 10px 30px rgba(0,0,0,.18)}
.task-card-header{display:flex;justify-content:space-between;gap:12px;align-items:center}
.task-card-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.task-phase-badge{padding:3px 8px;border-radius:999px;font-size:11px;background:#22262c;color:var(--muted);border:1px solid #31353c}
.task-card-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.task-summary-item{background:#1c1f24;border-radius:10px;padding:10px}
.task-summary-label{font-size:11px;color:var(--muted)}
.task-summary-value{margin-top:4px;font-size:13px;font-weight:600}
.task-view-tabs{display:flex;gap:8px;padding-bottom:4px;border-bottom:1px solid var(--border)}
.task-view-tab{padding:6px 12px;border:1px solid var(--border);border-radius:999px;color:var(--muted)}
.task-view-tab.active{background:#253246;border-color:#40639f;color:#fff}
.task-panel{display:none}
.task-panel.active{display:flex;flex-direction:column}
.task-progress{display:flex;flex-direction:column;gap:8px}
.task-progress-line{font-size:12px;color:var(--muted);white-space:pre-wrap;word-break:break-word;background:#1a1d21;border:1px solid #252932;border-radius:10px;padding:10px 12px}
.task-progress-empty{font-size:12px;color:#686d75}
.trace-timeline{position:relative;display:flex;flex-direction:column;gap:12px;padding-left:18px}
.trace-timeline::before{content:'';position:absolute;left:5px;top:4px;bottom:4px;width:1px;background:#31353c}
.trace-step{position:relative;padding:12px 14px;border-radius:12px;background:#1a1d21;border:1px solid #2b3038}
.trace-step::before{content:'';position:absolute;left:-18px;top:18px;width:10px;height:10px;border-radius:50%;background:#5b8cff;border:2px solid #111213}
.trace-step.done::before{background:#81c995}.trace-step.waiting::before{background:#f0c674}.trace-step.failed::before{background:#f4837a}
.trace-step-header{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
.trace-step-title{font-size:13px;font-weight:600}
.trace-step-meta{font-size:11px;color:var(--muted)}
.trace-step-body{margin-top:8px;font-size:12px;color:#c7cbd1;white-space:pre-wrap;word-break:break-word}
.task-card-header{display:flex;justify-content:space-between;gap:12px;align-items:center}
.task-card-title{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task-card-state{font-size:12px;color:var(--muted);flex:none}
.task-card-state.running{color:#8ab4f8}.task-card-state.waiting{color:#f0c674}.task-card-state.failed{color:#f4837a}.task-card-state.done{color:#81c995}
.task-progress{display:flex;flex-direction:column;gap:3px;border-top:1px solid var(--border);padding-top:7px}
.task-progress-line{font-size:12px;color:var(--muted);white-space:pre-wrap;word-break:break-word}
.task-progress-empty{font-size:12px;color:#686d75}
.task-card .message.approval{max-width:100%;align-self:stretch;margin-top:2px}
.message.approval{align-self:flex-start;max-width:85%;background:var(--panel);border:1px solid #6b4f1d;border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:6px}
.approval-title{font-weight:600;color:#f0c674}
.approval-row{font-size:12px;color:var(--muted);word-break:break-all}
.approval-preview{margin:4px 0 0;padding:8px;background:#101113;border:1px solid var(--border);border-radius:6px;font-size:12px;max-height:220px;overflow:auto;white-space:pre-wrap}
.approval-actions{display:flex;gap:8px;margin-top:4px;align-items:center}
.approval-btn{padding:6px 14px;border:1px solid var(--border)}
.approval-btn.approve{background:#2f5d3a;border-color:#3c7a4b}
.approval-btn.deny{background:#5d2f2f;border-color:#7a3c3c}
.empty-state{margin:auto;text-align:center;color:var(--muted);display:flex;flex-direction:column;gap:12px;align-items:center}
.empty-state h2{margin:0;font-size:15px;font-weight:500;color:var(--text)}
.link-btn{color:var(--accent)}
.composer{padding:12px 20px 20px;display:flex;flex-direction:column;gap:8px}
.composer-box{border:1px solid var(--border);border-radius:10px;padding:10px 12px;background:var(--panel)}
.composer-box:focus-within{border-color:#3c4046}
textarea{width:100%;min-height:48px;max-height:180px;background:none;border:none;color:var(--text);font:inherit;resize:none;outline:none}
.composer-actions{display:flex;align-items:center;justify-content:space-between;margin-top:6px}
.send-btn{background:var(--accent);color:#fff;padding:6px 16px}
.send-btn:disabled{background:#3a4560}
.preview-list{display:flex;gap:8px;overflow:auto}
.preview-item{position:relative;flex:none}
.preview-item img{width:40px;height:40px;object-fit:cover;border-radius:6px;display:block;border:1px solid var(--border)}
.preview-remove{position:absolute;top:-6px;right:-6px;width:18px;height:18px;line-height:1;border-radius:50%;background:#3a3d42;color:var(--text);font-size:12px}
.preview-remove:hover{background:#4a4e55}
.loading,.error-banner{display:none;font-size:13px;color:var(--muted);padding:0 20px}
.loading.visible,.error-banner.visible{display:block}
.error-banner{color:#f4837a}
@media(max-width:900px){.layout{grid-template-columns:1fr;height:auto}.sidebar{border-right:none;border-bottom:1px solid var(--border)}.trace-board{grid-template-columns:1fr}.trace-sidebar{position:static}.task-card-summary{grid-template-columns:1fr}}
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
      <div class=\"view-switcher\">
        <div class=\"view-tabs\">
          <button id=\"conversation-tab\" class=\"view-tab active\" onclick=\"switchPrimaryView('conversation')\">Conversation</button>
          <button id=\"trace-tab\" class=\"view-tab\" onclick=\"switchPrimaryView('trace')\">Trace</button>
        </div>
        <div id=\"view-summary\" class=\"view-summary\">聚焦当前对话与执行过程</div>
      </div>
      <section id=\"chat-area\" class=\"chat-area\"></section>
    </div>

    <div class=\"composer\">
      <div id=\"preview-list\" class=\"preview-list\"></div>
      <div class=\"composer-box\">
        <textarea id=\"prompt\" placeholder=\"请输入希望助手完成的任务，可直接粘贴图片…\" onpaste=\"handlePaste(event)\"></textarea>
        <div class=\"composer-actions\">
          <button class=\"icon-btn\" onclick=\"document.getElementById('image-input').click()\" title=\"上传图片\">＋ 图片</button>
          <input id=\"image-input\" type=\"file\" accept=\"image/*\" multiple onchange=\"handleImages(event)\" style=\"display:none\" />
          <button class=\"send-btn\" onclick=\"runTask()\">发送</button>
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
let activeConversationId = null;
let activeWorkspaceId = localStorage.getItem(ACTIVE_WORKSPACE_STORAGE_KEY);
let activePrimaryView = 'conversation';

function getConversationById(conversationId) {
  return conversations.find((item) => item.id === conversationId);
}

function persistActiveDraft() {
  const active = getActiveConversation();
  if (!active) return;
  active.draft = document.getElementById('prompt').value;
}

function restoreConversationDraft() {
  const active = getActiveConversation();
  document.getElementById('prompt').value = active?.draft || '';
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

function switchPrimaryView(view) {
  activePrimaryView = view;
  document.getElementById('conversation-tab').classList.toggle('active', view === 'conversation');
  document.getElementById('trace-tab').classList.toggle('active', view === 'trace');
  document.getElementById('view-summary').textContent = view === 'trace'
    ? '按阶段查看 Agent 执行链路、状态与关键事件'
    : '聚焦用户对话、结果输出与上下文交流';
  renderMessages();
}

function renderMessages() {
  const active = getActiveConversation();
  const chatArea = document.getElementById('chat-area');
  chatArea.innerHTML = '';

  if (!activeWorkspaceId) {
    chatArea.innerHTML = `
      <div class='empty-state'>
        <h2>请先添加工作区目录</h2>
        <div>会话会绑定到所选的本地目录</div>
        <button class='link-btn' onclick='pickWorkspace()'>添加目录</button>
      </div>
    `;
    return;
  }

  if (!active) {
    chatArea.innerHTML = `
      <div class='empty-state'>
        <h2>当前工作区暂无会话</h2>
        <button class='link-btn' onclick='createConversation()'>新建会话</button>
      </div>
    `;
    return;
  }

  if (active.messages.length === 0) {
    chatArea.innerHTML = `
      <div class='empty-state'>
        <h2>${active.title}</h2>
        <div>描述一个任务，或上传图片继续</div>
      </div>
    `;
    return;
  }

  if (activePrimaryView === 'trace') {
    renderTraceView(chatArea, active);
    chatArea.scrollTop = 0;
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
      body.textContent = text;
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

  chatArea.scrollTop = chatArea.scrollHeight;
}

const 风险文案 = { R0: '无副作用', R1: '低风险', R2: '需授权', R3: '高风险' };

function taskStateClass(phase1State) {
  if (phase1State === 'DONE') return 'done';
  if (phase1State === 'FAILED' || phase1State === 'CANCELLED') return 'failed';
  if (phase1State === 'WAITING' || phase1State === 'INTERRUPTED') return 'waiting';
  return 'running';
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
        step.body = `${step.body}\n${text}`;
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
      step.body = `${step.body}\n${text}`;
      step.meta = `#${step.startSequence || sequence}–#${sequence}`;
      pendingModel = null;
      return;
    }
    steps.push({
      title: kind === 'waiting' ? '等待用户操作' : '运行事件',
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
  return steps.slice(-12);
}

function renderTraceView(chatArea, conversation) {
  const tasks = conversation.messages.filter((item) => item.role === 'task');
  const board = document.createElement('div');
  board.className = 'trace-board';

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
  const summary = document.createElement('div');
  summary.className = 'trace-summary-card';
  const taskCount = tasks.length;
  const runningCount = tasks.filter((item) => item.task?.phase1State === 'RUNNING').length;
  const waitingCount = tasks.filter((item) => item.task?.phase1State === 'WAITING').length;
  summary.innerHTML = `
    <div class='trace-summary-title'>执行流程总览</div>
    <div class='trace-metric-grid'>
      <div class='trace-metric'><div class='trace-metric-label'>任务节点</div><div class='trace-metric-value'>${taskCount}</div></div>
      <div class='trace-metric'><div class='trace-metric-label'>运行中</div><div class='trace-metric-value'>${runningCount}</div></div>
      <div class='trace-metric'><div class='trace-metric-label'>等待确认</div><div class='trace-metric-value'>${waitingCount}</div></div>
      <div class='trace-metric'><div class='trace-metric-label'>会话消息</div><div class='trace-metric-value'>${conversation.messages.length}</div></div>
    </div>
  `;
  const legend = document.createElement('div');
  legend.className = 'trace-legend';
  legend.innerHTML = `
    <div class='trace-summary-title'>状态图例</div>
    <div class='trace-legend-item'><span class='trace-dot running'></span>执行中</div>
    <div class='trace-legend-item'><span class='trace-dot waiting'></span>等待用户操作</div>
    <div class='trace-legend-item'><span class='trace-dot done'></span>已完成阶段</div>
    <div class='trace-legend-item'><span class='trace-dot failed'></span>失败或中断</div>
  `;
  sidebar.append(summary, legend);
  board.append(main, sidebar);
  chatArea.appendChild(board);
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
  state.className = `task-card-state ${taskStateClass(task.phase1State)}`;
  state.textContent = 状态文案[task.phase1State] || task.phase1State || '准备中';
  header.append(title, state);
  card.appendChild(header);

  const meta = document.createElement('div');
  meta.className = 'task-card-meta';
  meta.innerHTML = `
    <span class='task-phase-badge'>${task.status || 'running'}</span>
    <span class='task-phase-badge'>${task.taskId || 'task'}</span>
  `;
  card.appendChild(meta);

  const summary = document.createElement('div');
  summary.className = 'task-card-summary';
  const progressCount = (task.progress || []).length;
  summary.innerHTML = `
    <div class='task-summary-item'><div class='task-summary-label'>当前阶段</div><div class='task-summary-value'>${状态文案[task.phase1State] || task.phase1State || '准备中'}</div></div>
    <div class='task-summary-item'><div class='task-summary-label'>流程事件</div><div class='task-summary-value'>${progressCount}</div></div>
    <div class='task-summary-item'><div class='task-summary-label'>执行状态</div><div class='task-summary-value'>${task.waiting ? '等待输入' : '持续执行'}</div></div>
  `;
  card.appendChild(summary);

  const tabBar = document.createElement('div');
  tabBar.className = 'task-view-tabs';
  tabBar.innerHTML = `
    <button class='task-view-tab ${mode === 'conversation' ? 'active' : ''}'>Conversation</button>
    <button class='task-view-tab ${mode === 'trace' ? 'active' : ''}'>Trace Timeline</button>
  `;
  card.appendChild(tabBar);

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
    tracePanel.appendChild(timeline);
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
        cursor: Number(rawTask.progress_cursor || 0),
        progress: [], waiting: rawTask.waiting || null,
      },
    };
    conversation.messages.push(message);
  } else {
    message.task.goal = rawTask.goal || message.task.goal;
    message.task.phase1State = rawTask.phase1_state || message.task.phase1State;
    message.task.status = rawTask.status || message.task.status;
    message.task.cursor = Math.max(
      Number(message.task.cursor || 0), Number(rawTask.progress_cursor || 0),
    );
    message.task.waiting = rawTask.waiting ?? message.task.waiting;
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
  const title = document.createElement('div');
  title.className = 'approval-title';
  title.textContent = `需要你授权后才会执行（${风险文案[approval.risk] || approval.risk || '未知风险'}）`;
  element.appendChild(title);

  const rows = [
    ['动作', approval.action],
    ['目标', approval.target],
    ['网络访问', approval.network_access ? '是' : '否'],
    ['数据外发', approval.data_transmission],
    ['可回滚', approval.rollback],
  ];
  rows.forEach(([label, value]) => {
    if (!value && value !== false) return;
    const row = document.createElement('div');
    row.className = 'approval-row';
    row.textContent = `${label}：${value}`;
    element.appendChild(row);
  });

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

async function downscaleImage(dataUrl) {
  // A full-resolution screenshot is counted as input tokens and can exceed the
  // model's context window on its own, so bound the longest edge first.
  const image = await new Promise((resolve, reject) => {
    const element = new Image();
    element.onload = () => resolve(element);
    element.onerror = reject;
    element.src = dataUrl;
  });

  const longest = Math.max(image.width, image.height);
  if (longest <= MAX_IMAGE_EDGE) return dataUrl;

  const scale = MAX_IMAGE_EDGE / longest;
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(image.width * scale);
  canvas.height = Math.round(image.height * scale);
  canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL('image/jpeg', 0.82);
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
      prepared = await downscaleImage(original);
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
  if (kind === 'model_retry') return '模型调用重试中…';
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
  PREPARING: '准备中', RUNNING: '执行中', WAITING: '等待输入',
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

function renderFinalResult(taskId, state, conversationId) {
  const card = taskMessage(conversationId, taskId)
    || upsertTaskCard(conversationId, { task_id: taskId });
  if (card) {
    card.task.phase1State = state.phase1_state || card.task.phase1State;
    card.task.status = state.status || card.task.status;
    card.task.waiting = null;
  }
  if (state.assistant_text) {
    const conversation = getConversationById(conversationId);
    const duplicate = conversation?.messages.some(
      (item) => item.role === 'assistant'
        && item.taskId === taskId
        && item.content === state.assistant_text,
    );
    if (!duplicate) {
      appendMessage('assistant', state.assistant_text, [], conversationId, taskId);
    }
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
    if (current) {
      current.task.phase1State = state.phase1_state;
      current.task.status = state.status;
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
        const currentCard = taskMessage(conversationId, taskId);
        if (currentCard) {
          currentCard.task.phase1State = 'WAITING';
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
    (data.messages || []).forEach((message) => {
      const task = tasksById.get(message.task_id);
      // Task user text is persisted when a result is recorded, but belongs at
      // task attachment time. Direct Session chat keeps its own event sequence.
      const sequence = Number(
        message.role === 'user' && task
          ? task.attached_sequence
          : message.source_event_sequence,
      );
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
            phase1State: task.phase1_state, status: task.status,
            cursor: Number(task.progress_cursor || 0), progress: [],
            waiting: task.waiting || null,
          },
        },
      });
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

document.getElementById('prompt').addEventListener('input', () => {
  persistActiveDraft();
});

loadWorkspaces();
</script>
</body>
</html>
""".replace("__WEBUI_VERSION__", WEBUI_VERSION)

runtime_server: LocalEventApiServer | None = None
runtime_error: str | None = None
workspace_registry: list[dict[str, str]] = []


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


def _normalize_image_blocks(images: object) -> list[dict[str, str]]:
    """Keep only inline data-URL images the Kernel can consume."""
    if not isinstance(images, list):
        return []
    blocks: list[dict[str, str]] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        image_url = str(image.get("image_url", ""))
        if not image_url.startswith("data:image/"):
            continue
        if len(image_url) > MAX_IMAGE_DATA_URL_CHARS:
            # An oversized attachment makes the whole model call unanswerable
            # with "context cannot fit", so reject it with an actionable error.
            raise HTTPException(
                status_code=413,
                detail="图片过大，请压缩后重试（单张建议不超过 300KB）",
            )
        blocks.append({
            "type": "input_image",
            "image_url": image_url,
            "media_type": str(image.get("media_type", "image/png")),
        })
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
        ))
    except AgentLoopLimitExceeded as error:
        # A spent call budget is a bounded Runtime outcome with a known cause,
        # not an internal fault. Reporting it as 500 hides the only actionable
        # detail the user has: how much was spent and what blocked last.
        raise HTTPException(
            status_code=409, detail=_describe_loop_limit(error),
        ) from error
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

    goal = str(payload.get("goal", ""))
    image_blocks = _normalize_image_blocks(payload.get("images"))

    if image_blocks:
        # Record only the count; base64 payloads must never enter the goal.
        goal = f"{goal}\n\n[web-ui-attached-images: {len(image_blocks)}]"

    goal = (
        f"[workspace-context]\n"
        f"workspace_id={workspace_id}\n"
        f"workspace_path={workspace_path}\n\n"
        f"{goal}"
    )

    result = runtime_server._call(runtime_server._client.submit_task(
        goal,
        command_id=f"web-ui:{workspace_id}",
        session_id=session_id,
        images=tuple(
            ImageBlock(image_url=block["image_url"]) for block in image_blocks
        ),
    ))
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
            signature = json.dumps(waiting, ensure_ascii=False, sort_keys=True)
            if waiting is not None and signature != announced_wait:
                announced_wait = signature
                yield (
                    "data: "
                    + json.dumps(
                        {"task_id": task_id, "waiting": waiting},
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
