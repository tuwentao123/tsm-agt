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
WEBUI_VERSION = "deepseek-cn-v20260920-restore"

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
.chat-area{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;padding:20px;display:flex;flex-direction:column;gap:10px}
.message{max-width:70%;word-break:break-word}
.message-body{white-space:pre-wrap}
.message-images{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.message-images img{width:56px;height:56px;object-fit:cover;border-radius:6px;display:block;cursor:zoom-in;border:1px solid rgba(255,255,255,.12)}
.message.user{align-self:flex-end;background:#2f3a4f;padding:8px 12px;border-radius:12px 12px 2px 12px}
.message.assistant{align-self:flex-start;background:var(--panel);padding:8px 12px;border-radius:12px 12px 12px 2px}
.message.system{align-self:flex-start;max-width:100%;color:var(--muted);font-size:12px;padding:0 2px}
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
@media(max-width:900px){.layout{grid-template-columns:1fr;height:auto}.sidebar{border-right:none;border-bottom:1px solid var(--border)}}
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
    <section id=\"chat-area\" class=\"chat-area\"></section>

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

  active.messages.forEach((message) => {
    const element = document.createElement('div');
    element.className = `message ${message.role}`;
    const text = String(message.content ?? '').trim();
    if (message.role === 'approval') {
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
  message.resolved = decision;
  renderMessages();
  appendMessage('system', decision === 'APPROVE' ? '已允许，继续执行…' : '已拒绝该操作。');
  if (decision === 'APPROVE') setLoading(true);
}

function appendApproval(taskId, approval, conversationId = activeConversationId) {
  const active = getConversationById(conversationId);
  if (!active) return;
  const existing = active.messages.find(
    (item) => item.role === 'approval' && item.approval?.request_id === approval.request_id,
  );
  if (existing) return;
  active.messages.push({
    role: 'approval', content: '', approval, taskId, resolved: null,
  });
  renderConversations();
  if (conversationId === activeConversationId) {
    renderMessages();
  }
}

function appendMessage(role, content, images = [], conversationId = activeConversationId) {
  const active = getConversationById(conversationId);
  if (!active) return;
  const text = String(content ?? '').trim();
  if (!text && images.length === 0) return;
  const last = active.messages[active.messages.length - 1];
  // Progress notices repeat often; collapse identical consecutive lines.
  if (role === 'system' && last?.role === 'system' && last.content === text) return;
  active.messages.push({ role, content: text, images });
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
    appendMessage('system', '已创建任务，正在执行…', [], conversationId);
    followTask(task.task_id, conversationId);
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

function renderFinalResult(state, conversationId) {
  if (state.assistant_text) {
    appendMessage('assistant', state.assistant_text, [], conversationId);
  } else if (state.clarification?.question) {
    appendMessage('assistant', state.clarification.question, [], conversationId);
  } else if (state.failure_reason) {
    showError(describeFailure(state.failure_reason));
    appendMessage('system', `任务失败：${describeFailure(state.failure_reason)}`, [], conversationId);
  } else {
    appendMessage('system', '本次任务没有返回文本结果。', [], conversationId);
  }
}

// The progress stream is ephemeral and dies with its connection: a server
// restart, a sleeping laptop, or a proxy timeout all end it. Closing the
// EventSource there left the page silent even when the Task went on to finish,
// so reconnect and reconcile against the durable Task result instead.
function followTask(taskId, conversationId) {
  const existing = followers.get(taskId);
  if (existing) {
    existing.stop();
  }
  const follower = {
    attempts: 0, lastState: null, stopped: false,
    source: null, timer: null,
    stop() {
      this.stopped = true;
      if (this.source) this.source.close();
      if (this.timer) clearInterval(this.timer);
      followers.delete(taskId);
    },
  };
  followers.set(taskId, follower);

  const finish = (state) => {
    renderFinalResult(state, conversationId);
    follower.stop();
    setLoading(false);
  };

  const reconcile = async () => {
    // Called on every (re)connect: anything missed while disconnected is still
    // readable from the Task itself.
    const response = await fetch(`/tasks/${taskId}`);
    if (!response.ok) return false;
    const state = await response.json();
    if (state.phase1_state !== follower.lastState) {
      follower.lastState = state.phase1_state;
      appendMessage('system', `任务状态：${状态文案[state.phase1_state] || '未知'}`, [], conversationId);
    }
    if (TERMINAL_STATES.includes(state.phase1_state)) {
      finish(state);
      return true;
    }
    if (state.approval) {
      setLoading(false);
      appendApproval(taskId, state.approval, conversationId);
    }
    return false;
  };

  const connect = () => {
    if (follower.stopped) return;
    const source = new EventSource(`/stream/${taskId}`);
    follower.source = source;
    source.onopen = () => {
      follower.attempts = 0;
    };
    source.onmessage = (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (error) {
        appendMessage('system', event.data, [], conversationId);
        return;
      }
      if (payload.waiting) {
        setLoading(false);
        if (payload.waiting.kind === 'APPROVAL' && payload.waiting.approval) {
          appendApproval(payload.task_id || taskId, payload.waiting.approval, conversationId);
        } else if (payload.waiting.question) {
          appendMessage('assistant', payload.waiting.question, [], conversationId);
        }
        return;
      }
      if (payload.final) {
        finish(payload);
        return;
      }
      const text = describeProgress(payload.progress);
      if (text) appendMessage('system', text, [], conversationId);
    };
    source.onerror = async () => {
      source.close();
      if (follower.stopped) return;
      if (await reconcile()) return;
      follower.attempts += 1;
      if (follower.attempts > MAX_STREAM_RETRIES) {
        appendMessage('system', '实时输出连接已断开；任务仍在后台运行，可刷新页面查看结果。', [], conversationId);
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
    conversation.messages = (data.messages || []).map((message) => ({
      role: message.role, content: message.content, images: [],
    }));
    // An unanswered approval has to come back as a live control, not as text:
    // the Task stays suspended until someone decides.
    if (data.waiting?.kind === 'APPROVAL' && data.waiting.approval) {
      conversation.messages.push({
        role: 'approval', content: '', approval: data.waiting.approval,
        taskId: data.latest_task_id, resolved: null,
      });
    } else if (data.waiting) {
      conversation.messages.push({
        role: 'system', content: '该会话正在等待你的输入。', images: [],
      });
    }
    renderConversations();
    renderMessages();
    // Still executing: re-attach the stream so output continues here instead of
    // finishing invisibly in the background.
    if (
      data.latest_task_id
      && !data.waiting
      && !TERMINAL_STATES.includes(data.latest_task_state)
    ) {
      setLoading(true);
      followTask(data.latest_task_id, conversationId);
    }
  } catch (error) {
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


@app.on_event("startup")
async def startup() -> None:
    global runtime_server, runtime_error
    load_workspace_registry()
    runtime_server = LocalEventApiServer(Path.cwd())
    try:
        runtime_server.start()
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
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    latest_task_id = snapshot.active_task_id or (
        snapshot.task_ids[-1] if snapshot.task_ids else None
    )
    waiting = None
    latest_task_state = None
    if latest_task_id:
        try:
            result = runtime_server._call(
                runtime_server._client.get_task_result(latest_task_id)
            )
            waiting = _waiting_payload(result)
            latest_task_state = result.phase1_state
        except (LookupError, PermissionError):
            waiting = None
    return {
        "session_id": snapshot.session_id,
        "title": snapshot.title,
        "latest_task_id": latest_task_id,
        # A Task that is still executing needs its stream re-attached, so the
        # page has to know the difference between running and already finished.
        "latest_task_state": latest_task_state,
        # An unanswered approval is part of the Session's live state: after a
        # reload the button has to come back, or the Task waits forever.
        "waiting": waiting,
        "messages": [
            {
                "role": message.role.value,
                "content": message.text,
                "task_id": message.task_id,
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
    return result.to_data()


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
        if event.event_type in {"turn.failed", "llm.failed"}:
            message = str(event.payload.get("message", "")).strip()
            if message:
                return message
    return None


@app.get("/stream/{task_id}")
async def stream(task_id: str):
    if runtime_server is None:
        raise HTTPException(
            status_code=503,
            detail=runtime_error or "runtime unavailable",
        )

    async def event_generator():
        cursor = 0
        announced_wait: str | None = None
        while True:
            items = runtime_server._client.read_progress(task_id, after=cursor)
            for item in items:
                cursor = max(cursor, item.sequence)
                yield f"data: {json.dumps(item.to_data(), ensure_ascii=False)}\n\n"

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
