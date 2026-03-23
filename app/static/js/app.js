// ============================================
// AI Workflow Assistant - Frontend
// Final Refactor Version (Aligned to current index.html)
// ============================================

const $ = (id) => document.getElementById(id);

// ==================== 全局状态 ====================
const state = {
    currentPage: 'chat',

    // 聊天
    conversations: [],
    currentConvId: null,
    currentConvMessages: [],
    isSending: false,

    // 配置
    configs: [],
    editingConfigId: null,

    // 批处理
    batchFiles: [],

    // 任务
    tasks: [],
    taskPollTimer: null,
    viewingTaskId: null,
    viewingTaskData: null,

    // 云端文件
    cloudFiles: [],

    // 设置/服务端状态
    serverDefaults: {},
    threadSettings: {
        maxConcurrent: 10,
        threadPoolSize: 10
    },
    defaultBatchSize: 10,
    batchDelayMin: 15,
    batchDelayMax: 45
};

// ==================== 工具函数 ====================
function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function normalizeText(text) {
    return (text || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
}

function clampInt(value, defaultValue, minValue = null, maxValue = null) {
    let n = parseInt(value, 10);
    if (Number.isNaN(n)) n = defaultValue;
    if (minValue !== null) n = Math.max(minValue, n);
    if (maxValue !== null) n = Math.min(maxValue, n);
    return n;
}

function getVal(id, defaultValue = '') {
    const el = $(id);
    return el ? el.value : defaultValue;
}

function setVal(id, value) {
    const el = $(id);
    if (!el) return false;
    el.value = value ?? '';
    return true;
}

function setText(id, text) {
    const el = $(id);
    if (!el) return false;
    el.textContent = text ?? '';
    return true;
}

function formatTime(ts) {
    if (!ts) return '-';
    const d = new Date(ts > 9999999999 ? ts : ts * 1000);
    if (Number.isNaN(d.getTime())) return '-';
    return d.toLocaleString();
}

function showToast(msg, type = 'info') {
    const c = $('toastContainer');
    if (!c) {
        try { alert(msg); } catch (_) {}
        return;
    }
    const t = document.createElement('div');
    t.className = `toast toast-${type}`;
    t.textContent = msg;
    c.appendChild(t);
    setTimeout(() => t.remove(), 3500);
}

function showLoading(text = '处理中...') {
    const textEl = $('globalLoadingText');
    const loadingEl = $('globalLoading');
    if (textEl) textEl.textContent = text;
    if (loadingEl) loadingEl.classList.remove('hidden');
}

function hideLoading() {
    const loadingEl = $('globalLoading');
    if (loadingEl) loadingEl.classList.add('hidden');
}

function safeAddEvent(id, event, handler) {
    const el = $(id);
    if (!el) return;
    el.addEventListener(event, handler);
}

function downloadText(filename, content) {
    const blob = new Blob([content || ''], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'download.txt';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
}

function statusText(s) {
    return {
        pending: '等待中',
        queued: '排队中',
        processing: '处理中',
        completed: '已完成',
        partial_failed: '部分失败',
        failed: '失败',
        cancelled: '已取消',
        paused: '已暂停'
    }[s] || s;
}

function getConfigNameById(configId) {
    if (!configId) return '默认配置';
    const cfg = state.configs.find(c => c.id === configId);
    return cfg ? cfg.name : '默认配置';
}

// ==================== API ====================
async function api(url, options = {}) {
    const opts = {
        headers: {
            'Content-Type': 'application/json',
            ...(options.headers || {})
        },
        ...options
    };

    const resp = await fetch(url, opts);
    const text = await resp.text();

    let data;
    try {
        data = text ? JSON.parse(text) : {};
    } catch (e) {
        throw new Error(`服务器返回非JSON：${text.slice(0, 300)}`);
    }

    if (!resp.ok && !data.success) {
        throw new Error(data.error || `请求失败：${resp.status}`);
    }

    return data;
}

// ==================== 页面切换 ====================
function setActiveNav(page) {
    document.querySelectorAll('.nav-item').forEach(el => {
        if (el.dataset.page === page) el.classList.add('active');
        else el.classList.remove('active');
    });
}

function showPage(page) {
    state.currentPage = page;

    document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
    const pageEl = $(`page-${page}`);
    if (pageEl) pageEl.classList.add('active');

    setActiveNav(page);

    if (page === 'tasks') {
        loadTasks(false);
        startTaskPolling();
    } else {
        stopTaskPolling();
    }

    if (page === 'cloud') {
        refreshCloudFiles();
    }
}

// ==================== 本地章节预览解析 ====================
function isStrongChapterTitle(line) {
    if (!line) return false;
    const s = line.trim().replace(/\u3000/g, ' ').replace(/\s+/g, ' ');
    if (!s) return false;

    const patterns = [
        /^第\s*[零一二三四五六七八九十百千万两〇\d]+\s*[章节回卷集部篇册幕]\s*(?:[：:·\-—.．、]\s*.*)?$/i,
        /^第\s*[零一二三四五六七八九十百千万两〇\d]+\s*[章节回卷集部篇册幕]\s+.*$/i,
        /^(序章|序|楔子|引子|前言|正文|终章|尾声|后记|番外|番外篇|附录|完结感言)$/i,
        /^(序章|序|楔子|引子|前言|正文|终章|尾声|后记|番外|番外篇|附录|完结感言)\s*[：:·\-—.．、]\s*.*$/i,
        /^chapter\s*\d+\s*(?:[:：.\-—]\s*.*)?$/i,
        /^chapter\s*[ivxlcdm]+\s*(?:[:：.\-—]\s*.*)?$/i
    ];

    return patterns.some(p => p.test(s));
}

function isWeakChapterTitle(line) {
    if (!line) return false;
    const s = line.trim().replace(/\u3000/g, ' ').replace(/\s+/g, ' ');
    if (!s || s.length > 60) return false;

    return /^\d+\s*[、.．\-—]\s*.+$/i.test(s) || /^\d+\s+.+$/i.test(s);
}

function parseChaptersLocal(content) {
    content = normalizeText(content || '').replace(/\ufeff/g, '');
    const lines = content.split('\n');

    const items = lines.map(line => {
        const text = (line || '').trim().replace(/\u3000/g, ' ').replace(/\s+/g, ' ');
        return {
            raw: line,
            text,
            strong: isStrongChapterTitle(text),
            weak: isWeakChapterTitle(text)
        };
    });

    const strongCount = items.filter(x => x.strong).length;
    const weakCount = items.filter(x => x.weak).length;

    function followingTextLength(startIndex, maxLookahead = 8) {
        let total = 0;
        for (let i = startIndex + 1; i < Math.min(items.length, startIndex + 1 + maxLookahead); i++) {
            const t = items[i].text;
            if (!t) continue;
            if (items[i].strong) break;
            total += t.length;
        }
        return total;
    }

    function isTitleAt(index) {
        const item = items[index];
        if (!item.text) return false;
        if (item.strong) return true;
        if (!item.weak) return false;

        const fLen = followingTextLength(index, 8);
        if (fLen >= 12) return true;
        if (weakCount >= 3) return true;
        if (/^\d+\s*[、.．\-—]\s*.+$/i.test(item.text)) return true;
        if (strongCount + weakCount >= 2 && /^\d+\s+.+$/i.test(item.text)) return true;

        return false;
    }

    const chapters = [];
    for (let i = 0; i < items.length; i++) {
        if (isTitleAt(i)) {
            chapters.push({
                index: chapters.length + 1,
                title: items[i].text
            });
        }
    }
    return chapters;
}

// ==================== 配置管理 ====================
function renderConfigOptions() {
    const selects = [
        $('chatConfigSelect'),
        ...document.querySelectorAll('.batch-config-select')
    ].filter(Boolean);

    selects.forEach(select => {
        const current = select.value;
        select.innerHTML =
            `<option value="">默认配置</option>` +
            state.configs.map(cfg => `<option value="${cfg.id}">${escapeHtml(cfg.name)}</option>`).join('');
        select.value = current || '';
    });
}

function renderConfigList() {
    const box = $('configList');
    if (!box) return;

    if (!state.configs.length) {
        box.innerHTML = `
            <div class="empty-state">
                <div class="icon">⚙️</div>
                <p>暂无配置</p>
            </div>
        `;
        return;
    }

    box.innerHTML = state.configs.map(cfg => `
        <div class="task-item">
            <div>
                <div style="font-weight:600;">${escapeHtml(cfg.name || '未命名配置')}</div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
                    model: ${escapeHtml(cfg.model || '默认')}
                    ${cfg.apiHost ? ` | host: ${escapeHtml(cfg.apiHost)}` : ''}
                    ${cfg.apiKeyMasked ? ` | key: ${escapeHtml(cfg.apiKeyMasked)}` : ''}
                </div>
            </div>
            <div style="display:flex;gap:8px;">
                <button class="btn btn-sm btn-secondary" onclick="editConfig('${cfg.id}')">编辑</button>
                <button class="btn btn-sm btn-danger" onclick="deleteConfig('${cfg.id}')">删除</button>
            </div>
        </div>
    `).join('');

    renderConfigOptions();
}

async function loadConfigs() {
    try {
        const data = await api('/api/config/list?userId=default');
        state.configs = data.configs || [];
        renderConfigList();
        renderConfigOptions();
    } catch (e) {
        console.error('loadConfigs error:', e);
        showToast(`加载配置失败：${e.message || e}`, 'error');
    }
}

function openConfigModal(cfg = null) {
    state.editingConfigId = cfg ? cfg.id : null;

    setVal('cfgName', cfg?.name || '');
    setVal('cfgSystemPrompt', cfg?.systemPrompt || '');
    setVal('cfgContextRounds', cfg?.contextRounds || '');
    setVal('cfgMaxOutputTokens', cfg?.maxOutputTokens || '');

    setVal('cfgBatchSystemPrompt', cfg?.batchSystemPrompt || '');
    setVal('cfgBatchUserPromptTemplate', cfg?.batchUserPromptTemplate || '');
    setVal('cfgBatchSize', cfg?.batchSize || '');

    setVal('cfgApiHost', cfg?.apiHost || '');
    setVal('cfgApiKey', '');
    setVal('cfgModel', cfg?.model || '');
    setVal('cfgTemperature', cfg?.temperature ?? '');
    setVal('cfgTopP', cfg?.topP ?? '');

    setVal('cfgHfToken', '');
    setVal('cfgHfDataset', cfg?.hfDataset || '');

    setText('configModalTitle', cfg ? '编辑配置' : '新建配置');

    const btnDelete = $('btnDeleteConfig');
    if (btnDelete) btnDelete.style.display = cfg ? 'inline-flex' : 'none';

    const modal = $('configModal');
    if (modal) modal.classList.remove('hidden');
}

function closeConfigModal() {
    const modal = $('configModal');
    if (modal) modal.classList.add('hidden');
    state.editingConfigId = null;
}

async function saveConfig() {
    const payload = {
        userId: 'default',
        id: state.editingConfigId || undefined,
        name: getVal('cfgName', '').trim(),
        systemPrompt: getVal('cfgSystemPrompt', ''),
        contextRounds: getVal('cfgContextRounds', ''),
        maxOutputTokens: getVal('cfgMaxOutputTokens', ''),
        batchSystemPrompt: getVal('cfgBatchSystemPrompt', ''),
        batchUserPromptTemplate: getVal('cfgBatchUserPromptTemplate', ''),
        batchSize: getVal('cfgBatchSize', ''),
        apiHost: getVal('cfgApiHost', '').trim(),
        apiKey: getVal('cfgApiKey', '').trim(),
        model: getVal('cfgModel', '').trim(),
        temperature: getVal('cfgTemperature', ''),
        topP: getVal('cfgTopP', ''),
        hfToken: getVal('cfgHfToken', '').trim(),
        hfDataset: getVal('cfgHfDataset', '').trim()
    };

    if (!payload.name) {
        showToast('请输入配置名称', 'warning');
        return;
    }

    try {
        const data = await api('/api/config/save', {
            method: 'POST',
            body: JSON.stringify(payload)
        });

        if (data.success) {
            showToast('配置保存成功', 'success');
            closeConfigModal();
            await loadConfigs();
        } else {
            showToast(data.error || '配置保存失败', 'error');
        }
    } catch (e) {
        console.error('saveConfig error:', e);
        showToast(`配置保存失败：${e.message || e}`, 'error');
    }
}

async function deleteConfig(configId) {
    if (!configId) return;
    if (!confirm('确定删除该配置吗？')) return;

    try {
        const data = await api('/api/config/delete', {
            method: 'POST',
            body: JSON.stringify({
                userId: 'default',
                id: configId
            })
        });

        if (data.success) {
            showToast('配置已删除', 'success');
            await loadConfigs();
        } else {
            showToast(data.error || '删除失败', 'error');
        }
    } catch (e) {
        console.error('deleteConfig error:', e);
        showToast(`删除失败：${e.message || e}`, 'error');
    }
}

function editConfig(configId) {
    const cfg = state.configs.find(c => c.id === configId);
    if (!cfg) {
        showToast('配置不存在', 'warning');
        return;
    }
    openConfigModal(cfg);
}

// ==================== 聊天 ====================
async function loadConversations() {
    try {
        const data = await api('/api/conversations?userId=default');
        state.conversations = data.conversations || [];
        renderConversationList();
    } catch (e) {
        console.error('loadConversations error:', e);
        showToast(`加载对话失败：${e.message || e}`, 'error');
    }
}

function renderConversationList() {
    const box = $('conversationList');
    if (!box) return;

    if (!state.conversations.length) {
        box.innerHTML = `
            <div class="empty-state">
                <div class="icon">💬</div>
                <p>暂无对话</p>
            </div>
        `;
        return;
    }

    box.innerHTML = state.conversations.map(conv => `
        <div class="task-item" onclick="selectConversation('${conv.id}')" style="cursor:pointer;">
            <div>
                <div style="font-weight:600;">${escapeHtml(conv.title || '新对话')}</div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
                    ${conv.lastMessagePreview ? escapeHtml(conv.lastMessagePreview) : '暂无消息'}
                </div>
            </div>
            <div style="display:flex;gap:8px;">
                <button class="btn btn-sm btn-danger" onclick="event.stopPropagation(); deleteConversation('${conv.id}')">删除</button>
            </div>
        </div>
    `).join('');
}

function renderChatMessages(messages = []) {
    const box = $('chatMessages');
    if (!box) return;

    if (!messages.length) {
        box.innerHTML = `
            <div class="empty-state">
                <div class="icon">🤖</div>
                <p>开始一个新的对话吧</p>
            </div>
        `;
        return;
    }

    box.innerHTML = messages.map(msg => `
        <div class="chat-message ${msg.role === 'user' ? 'user' : 'assistant'}">
            <div class="chat-avatar">${msg.role === 'user' ? '你' : 'AI'}</div>
            <div class="chat-bubble">${escapeHtml(msg.content || '')}</div>
        </div>
    `).join('');

    box.scrollTop = box.scrollHeight;
}

async function createConversation() {
    try {
        const configId = getVal('chatConfigSelect', '');
        const data = await api('/api/conversation/create', {
            method: 'POST',
            body: JSON.stringify({
                userId: 'default',
                title: '新对话',
                configId
            })
        });

        if (data.success) {
            state.currentConvId = data.conversation.id;
            state.currentConvMessages = [];
            renderChatMessages([]);
            await loadConversations();
        } else {
            showToast(data.error || '创建对话失败', 'error');
        }
    } catch (e) {
        console.error('createConversation error:', e);
        showToast(`创建对话失败：${e.message || e}`, 'error');
    }
}

function selectConversation(convId) {
    state.currentConvId = convId;
    state.currentConvMessages = [];
    renderChatMessages([]);
}

async function deleteConversation(convId) {
    if (!confirm('确定删除该对话吗？')) return;

    try {
        const data = await api('/api/conversation/delete', {
            method: 'POST',
            body: JSON.stringify({
                userId: 'default',
                id: convId
            })
        });

        if (data.success) {
            if (state.currentConvId === convId) {
                state.currentConvId = null;
                state.currentConvMessages = [];
                renderChatMessages([]);
            }
            await loadConversations();
            showToast('对话已删除', 'success');
        } else {
            showToast(data.error || '删除失败', 'error');
        }
    } catch (e) {
        console.error('deleteConversation error:', e);
        showToast(`删除失败：${e.message || e}`, 'error');
    }
}

async function sendChat() {
    if (state.isSending) return;

    const input = $('chatInput');
    if (!input) return;

    const message = (input.value || '').trim();
    if (!message) return;

    if (!state.currentConvId) {
        await createConversation();
        if (!state.currentConvId) {
            showToast('创建对话失败，无法发送消息', 'error');
            return;
        }
    }

    state.currentConvMessages.push({
        role: 'user',
        content: message
    });
    renderChatMessages(state.currentConvMessages);

    state.isSending = true;

    try {
        const data = await api('/api/chat', {
            method: 'POST',
            body: JSON.stringify({
                userId: 'default',
                conversationId: state.currentConvId,
                configId: getVal('chatConfigSelect', ''),
                message
            })
        });

        if (data.success) {
            state.currentConvMessages.push({
                role: 'assistant',
                content: data.message || ''
            });
            renderChatMessages(state.currentConvMessages);
            input.value = '';
            await loadConversations();
        } else {
            showToast(data.error || '发送失败', 'error');
        }
    } catch (e) {
        console.error('sendChat error:', e);
        showToast(`发送失败：${e.message || e}`, 'error');
    } finally {
        state.isSending = false;
    }
}

// ==================== 设置页 ====================
async function loadSettings() {
    try {
        const data = await api('/api/settings');
        const settings = data.settings || {};

        state.serverDefaults = settings;
        state.threadSettings.maxConcurrent = settings.maxConcurrent || 10;
        state.threadSettings.threadPoolSize = settings.threadPoolSize || 10;
        state.defaultBatchSize = settings.batchSize || 10;
        state.batchDelayMin = settings.batchDelayMin ?? 15;
        state.batchDelayMax = settings.batchDelayMax ?? 45;

        // 默认配置区域
        setVal('defApiHost', settings.apiHost || '');
        setVal('defModel', settings.model || 'gpt-5.4');
        setVal('defTemperature', settings.temperature ?? 0.7);
        setVal('defTopP', settings.topP ?? 0.65);
        setVal('defContextRounds', settings.contextRounds ?? 100);
        setVal('defMaxOutputTokens', settings.maxOutputTokens ?? 1000000);
        setVal('defSystemPrompt', settings.systemPrompt || '');
        setVal('defBatchSystemPrompt', settings.batchSystemPrompt || '');
        setVal('defBatchUserPromptTemplate', settings.batchUserPromptTemplate || '');

        // 并发/线程
        setVal('maxConcurrent', state.threadSettings.maxConcurrent);
        setVal('threadPoolSize', state.threadSettings.threadPoolSize);

        // 延迟
        setVal('batchDelayMin', state.batchDelayMin);
        setVal('batchDelayMax', state.batchDelayMax);

        // 批处理全局默认值
        setVal('globalBatchSize', state.defaultBatchSize);
        setVal('globalDelayMin', state.batchDelayMin);
        setVal('globalDelayMax', state.batchDelayMax);

        renderServerStatus();
    } catch (e) {
        console.error('loadSettings error:', e);
        showToast(`加载设置失败：${e.message || e}`, 'error');
    }
}

function renderServerStatus() {
    const serverStatus = $('serverConfigStatus');
    const threadStatus = $('threadStatus');
    const delayStatus = $('delayStatus');

    if (serverStatus) {
        serverStatus.textContent =
            `默认模型：${state.serverDefaults.model || '-'} | ` +
            `默认批次：${state.serverDefaults.batchSize || 10} | ` +
            `默认API Host：${state.serverDefaults.apiHost || '(未配置)'}`;
    }

    if (threadStatus) {
        threadStatus.textContent =
            `线程池：${state.threadSettings.threadPoolSize} | 最大并发：${state.threadSettings.maxConcurrent}`;
    }

    if (delayStatus) {
        delayStatus.textContent =
            `当前延迟：${state.batchDelayMin}-${state.batchDelayMax} 秒`;
    }
}

async function saveDefaultSettings() {
    const payload = {
        apiHost: getVal('defApiHost', '').trim(),
        model: getVal('defModel', '').trim(),
        temperature: getVal('defTemperature', ''),
        topP: getVal('defTopP', ''),
        contextRounds: getVal('defContextRounds', ''),
        maxOutputTokens: getVal('defMaxOutputTokens', ''),
        systemPrompt: getVal('defSystemPrompt', ''),
        batchSystemPrompt: getVal('defBatchSystemPrompt', ''),
        batchUserPromptTemplate: getVal('defBatchUserPromptTemplate', '')
    };

    try {
        const data = await api('/api/settings/update', {
            method: 'POST',
            body: JSON.stringify(payload)
        });

        if (data.success) {
            showToast('默认设置保存成功', 'success');
            await loadSettings();
        } else {
            showToast(data.error || '保存失败', 'error');
        }
    } catch (e) {
        console.error('saveDefaultSettings error:', e);
        showToast(`保存失败：${e.message || e}`, 'error');
    }
}

async function saveThreadSettings() {
    const threadPoolSize = clampInt(getVal('threadPoolSize', 10), 10, 1, 100);
    const maxConcurrent = clampInt(getVal('maxConcurrent', 10), 10, 1, 100);

    try {
        const data = await api('/api/set-thread-pool', {
            method: 'POST',
            body: JSON.stringify({
                threadPoolSize,
                maxConcurrent
            })
        });

        if (data.success) {
            state.threadSettings.threadPoolSize = data.threadPoolSize || threadPoolSize;
            state.threadSettings.maxConcurrent = data.maxConcurrent || maxConcurrent;
            renderServerStatus();
            showToast('线程设置已更新', 'success');
        } else {
            showToast(data.error || '线程设置失败', 'error');
        }
    } catch (e) {
        console.error('saveThreadSettings error:', e);
        showToast(`线程设置失败：${e.message || e}`, 'error');
    }
}

async function saveDelaySettings() {
    const delayMin = clampInt(getVal('batchDelayMin', 15), 15, 0, 3600);
    const delayMax = clampInt(getVal('batchDelayMax', 45), 45, delayMin, 3600);

    state.batchDelayMin = delayMin;
    state.batchDelayMax = delayMax;

    renderServerStatus();
    showToast('批次延迟设置已更新（提交任务时生效）', 'success');
}

// ==================== 批处理页 ====================
function buildLocalBatchFileFingerprint(fileName, content) {
    return `${fileName}::${(content || '').length}::${(content || '').slice(0, 200)}`;
}

function renderBatchSubmitArea() {
    const card = $('batchSubmitArea');
    const btnSubmit = $('btnSubmitBatch');
    const btnClear = $('btnClearBatch');

    if (btnSubmit) btnSubmit.disabled = state.batchFiles.length === 0;
    if (btnClear) btnClear.disabled = state.batchFiles.length === 0;

    if (!card) return;

    let info = card.querySelector('.batch-submit-info');
    if (!info) {
        info = document.createElement('div');
        info.className = 'batch-submit-info';
        info.style.fontSize = '13px';
        info.style.color = 'var(--text-secondary)';
        info.style.marginBottom = '12px';

        // 插到 card-title 后面、按钮区前面
        const title = card.querySelector('.card-title');
        if (title && title.nextSibling) {
            card.insertBefore(info, title.nextSibling);
        } else if (title) {
            card.appendChild(info);
        } else {
            card.insertBefore(info, card.firstChild);
        }
    }

    if (!state.batchFiles.length) {
        info.textContent = '上传 TXT 后即可提交批处理';
        return;
    }

    const totalPreview = state.batchFiles.reduce((sum, f) => {
        return sum + Math.max(1, (f.previewChapters || []).length || 1);
    }, 0);

    info.innerHTML = `当前待提交：<strong>${state.batchFiles.length}</strong> 本，总预览章节 <strong>${totalPreview}</strong>`;
}


async function handleBatchFiles(files) {
    const txtFiles = Array.from(files).filter(f => f.name.toLowerCase().endsWith('.txt'));
    if (!txtFiles.length) {
        showToast('请选择 .txt 文件', 'warning');
        return;
    }

    showLoading('正在解析文件...');
    let successCount = 0;
    let duplicateCount = 0;

    for (const file of txtFiles) {
        const result = await new Promise((resolve) => {
            const reader = new FileReader();

            reader.onload = (e) => {
                const content = normalizeText(e.target.result || '');
                const previewChapters = parseChaptersLocal(content);

                resolve({
                    fileName: file.name,
                    content,
                    previewChapters,
                    success: !!content
                });
            };

            reader.onerror = () => resolve({
                fileName: file.name,
                content: '',
                previewChapters: [],
                success: false
            });

            reader.readAsText(file);
        });

        if (!result.success || !result.content) continue;

        const localFingerprint = buildLocalBatchFileFingerprint(result.fileName, result.content);
        const exists = state.batchFiles.some(f => f.localFingerprint === localFingerprint);
        if (exists) {
            duplicateCount++;
            continue;
        }

        const totalChapters = Math.max(1, (result.previewChapters || []).length || 1);

        state.batchFiles.push({
            fileId: `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
            localFingerprint,
            fileName: result.fileName,
            content: result.content,
            previewChapters: result.previewChapters,
            configId: '',
            batchSize: state.defaultBatchSize || 10,
            startChapter: 1,
            endChapter: totalChapters
        });

        successCount++;
    }

    hideLoading();
    renderBatchFiles();
    renderBatchSubmitArea();

    let msg = `成功解析 ${successCount}/${txtFiles.length} 个文件`;
    if (duplicateCount > 0) msg += `，跳过重复 ${duplicateCount} 个`;
    showToast(msg, successCount > 0 ? 'success' : 'warning');
}

function clearBatchFiles() {
    state.batchFiles = [];
    renderBatchFiles();
    renderBatchSubmitArea();
}

function removeBatchFile(fileId) {
    state.batchFiles = state.batchFiles.filter(f => f.fileId !== fileId);
    renderBatchFiles();
    renderBatchSubmitArea();
}

function updateBatchFileField(fileId, field, value) {
    const item = state.batchFiles.find(f => f.fileId === fileId);
    if (!item) return;

    item[field] = value;

    const total = Math.max(1, (item.previewChapters || []).length || 1);

    if (field === 'batchSize') {
        item.batchSize = clampInt(value, state.defaultBatchSize || 10, 1, 100);
    }
    if (field === 'startChapter' || field === 'endChapter') {
        item.startChapter = clampInt(item.startChapter, 1, 1, total);
        item.endChapter = clampInt(item.endChapter, total, 1, total);
        if (item.startChapter > item.endChapter) {
            const t = item.startChapter;
            item.startChapter = item.endChapter;
            item.endChapter = t;
        }
    }
}

function renderBatchFiles() {
    const box = $('batchFileList');
    if (!box) return;

    if (!state.batchFiles.length) {
        box.innerHTML = `
            <div class="empty-state">
                <div class="icon">📚</div>
                <p>暂无待处理文件，请上传 TXT 小说</p>
            </div>
        `;
        return;
    }

    box.innerHTML = state.batchFiles.map(file => {
        const previewCount = (file.previewChapters || []).length || 0;
        const total = Math.max(1, previewCount || 1);

        const previewTitles = (file.previewChapters || []).slice(0, 5).map(ch => ch.title).join(' / ');

        return `
            <div class="batch-file-card">
                <div class="batch-file-header">
                    <div>
                        <div class="batch-file-name">📄 ${escapeHtml(file.fileName)}</div>
                        <div class="batch-file-meta">
                            本地预览识别 ${previewCount} 章 | 文件长度 ${(file.content || '').length} 字符
                        </div>
                    </div>
                    <button class="btn btn-sm btn-danger" onclick="removeBatchFile('${file.fileId}')">移除</button>
                </div>

                ${previewTitles ? `
                    <div style="font-size:12px;color:var(--text-muted);margin-bottom:10px;">
                        章节预览：${escapeHtml(previewTitles)}${previewCount > 5 ? ' ...' : ''}
                    </div>
                ` : ''}

                <div class="batch-file-config">
                    <label>配置</label>
                    <select class="form-input batch-config-select"
                            onchange="updateBatchFileField('${file.fileId}', 'configId', this.value)">
                        <option value="">默认配置</option>
                        ${state.configs.map(cfg => `
                            <option value="${cfg.id}" ${cfg.id === file.configId ? 'selected' : ''}>
                                ${escapeHtml(cfg.name)}
                            </option>
                        `).join('')}
                    </select>
                </div>

                <div class="batch-file-config">
                    <label>批次章数</label>
                    <input class="form-input"
                           type="number"
                           min="1"
                           max="100"
                           value="${file.batchSize}"
                           onchange="updateBatchFileField('${file.fileId}', 'batchSize', this.value)">
                </div>

                <div class="batch-file-config">
                    <label>开始章节</label>
                    <input class="form-input"
                           type="number"
                           min="1"
                           max="${total}"
                           value="${file.startChapter}"
                           onchange="updateBatchFileField('${file.fileId}', 'startChapter', this.value)">
                </div>

                <div class="batch-file-config">
                    <label>结束章节</label>
                    <input class="form-input"
                           type="number"
                           min="1"
                           max="${total}"
                           value="${file.endChapter}"
                           onchange="updateBatchFileField('${file.fileId}', 'endChapter', this.value)">
                </div>
            </div>
        `;
    }).join('');

    renderConfigOptions();
}

async function submitBatch() {
    if (!state.batchFiles.length) {
        showToast('请先上传文件', 'warning');
        return;
    }

    showLoading('正在提交批处理任务...');

    try {
        const globalBatchSize = clampInt(
            getVal('globalBatchSize', state.defaultBatchSize || 10),
            state.defaultBatchSize || 10,
            1,
            100
        );
        const globalDelayMin = clampInt(
            getVal('globalDelayMin', state.batchDelayMin || 15),
            state.batchDelayMin || 15,
            0,
            3600
        );
        const globalDelayMax = clampInt(
            getVal('globalDelayMax', state.batchDelayMax || 45),
            state.batchDelayMax || 45,
            globalDelayMin,
            3600
        );

        const files = state.batchFiles.map(file => {
            const total = Math.max(1, (file.previewChapters || []).length || 1);
            return {
                fileName: file.fileName,
                content: file.content || '',
                configId: file.configId || '',
                batchSize: clampInt(file.batchSize || globalBatchSize, globalBatchSize, 1, 100),
                startChapter: clampInt(file.startChapter || 1, 1, 1, total),
                endChapter: clampInt(file.endChapter || total, total, 1, total)
            };
        });

        console.log('[提交批处理 payload]', files.map(f => ({
            fileName: f.fileName,
            configId: f.configId,
            batchSize: f.batchSize,
            startChapter: f.startChapter,
            endChapter: f.endChapter,
            contentLen: (f.content || '').length
        })));

        const data = await api('/api/batch', {
            method: 'POST',
            body: JSON.stringify({
                userId: 'default',
                files,
                delayMin: globalDelayMin,
                delayMax: globalDelayMax
            })
        });

        hideLoading();

        if (data.success) {
            let msg = `任务已提交：入队 ${data.queuedFiles || 0} 本，共 ${data.totalChapters || 0} 章`;
            if (data.duplicateFiles && data.duplicateFiles.length > 0) {
                msg += `，重复跳过 ${data.duplicateFiles.length} 本`;
            }
            showToast(msg, 'success');

            state.batchFiles = [];
            renderBatchFiles();
            renderBatchSubmitArea();

            showPage('tasks');
            await loadTasks(false);
        } else {
            showToast(data.error || '提交失败', 'error');
        }
    } catch (e) {
        hideLoading();
        console.error('submitBatch error:', e);
        showToast(`提交失败：${e.message || e}`, 'error');
    }
}

// ==================== 任务页 ====================
function startTaskPolling() {
    stopTaskPolling();
    state.taskPollTimer = setInterval(async () => {
        if (state.currentPage === 'tasks') {
            await loadTasks(false);
            if (state.viewingTaskId) {
                await loadTaskDetail(state.viewingTaskId, false);
            }
        }
    }, 5000);
}

function stopTaskPolling() {
    if (state.taskPollTimer) {
        clearInterval(state.taskPollTimer);
        state.taskPollTimer = null;
    }
}

async function loadTasks(showError = true) {
    try {
        const data = await api('/api/tasks');
        state.tasks = data.tasks || [];
        renderTaskList();
    } catch (e) {
        console.error('loadTasks error:', e);
        if (showError) {
            showToast(`加载任务失败：${e.message || e}`, 'error');
        }
    }
}

function renderTaskList() {
    const box = $('taskList');
    if (!box) return;

    if (!state.tasks.length) {
        box.innerHTML = `
            <div class="empty-state">
                <div class="icon">🗂️</div>
                <p>暂无任务</p>
            </div>
        `;
        return;
    }

    box.innerHTML = state.tasks.map(task => `
        <div class="task-item">
            <div>
                <div style="font-weight:600;">任务 ${escapeHtml((task.task_id || '').slice(0, 8))}</div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
                    状态：${statusText(task.status)} | 进度：${escapeHtml(task.progress || '')} |
                    文件数：${task.file_count || 0} | 创建时间：${formatTime(task.created_at)}
                </div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
                    ${escapeHtml(task.message || '')}
                </div>
            </div>

            <div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;">
                <button class="btn btn-sm btn-secondary" onclick="openTaskDetail('${task.task_id}')">详情</button>
                <button class="btn btn-sm btn-success" onclick="downloadTask('${task.task_id}')">下载</button>
                <button class="btn btn-sm btn-danger" onclick="cancelTask('${task.task_id}')">取消</button>
                <button class="btn btn-sm btn-secondary" onclick="deleteTask('${task.task_id}')">删除</button>
            </div>
        </div>
    `).join('');
}

function closeTaskView() {
    state.viewingTaskId = null;
    state.viewingTaskData = null;
    const modal = $('taskViewModal');
    if (modal) modal.classList.add('hidden');
}

async function openTaskDetail(taskId) {
    state.viewingTaskId = taskId;
    await loadTaskDetail(taskId, true);
}

async function loadTaskDetail(taskId, showError = true) {
    try {
        const data = await api(`/api/task/${taskId}`);
        if (!data.success) {
            if (showError) showToast(data.error || '加载任务详情失败', 'error');
            return;
        }

        state.viewingTaskData = data.task;
        renderTaskDetailModal(data.task);
    } catch (e) {
        console.error('loadTaskDetail error:', e);
        if (showError) {
            showToast(`加载任务详情失败：${e.message || e}`, 'error');
        }
    }
}

function renderTaskDetailModal(task) {
    const modal = $('taskViewModal');
    const title = $('taskViewTitle');
    const body = $('taskViewBody');
    const downloadBtn = $('btnDownloadFromView');

    if (!modal || !title || !body) return;

    title.textContent = `任务详情：${(task.task_id || '').slice(0, 8)}`;

    const files = task.files || [];

    body.innerHTML = `
        <div class="card">
            <div class="card-title">任务概览</div>
            <div style="font-size:13px;color:var(--text-secondary);line-height:1.8;">
                <div>状态：${statusText(task.status)}</div>
                <div>进度：${escapeHtml(task.progress || '')}</div>
                <div>说明：${escapeHtml(task.message || '')}</div>
                <div>创建时间：${formatTime(task.created_at)}</div>
                <div>总章节：${task.total_chapters || 0}</div>
                <div>成功章节：${task.completed_chapters || 0}</div>
                <div>失败章节：${task.failed_chapters || 0}</div>
            </div>
        </div>

        ${files.map((file, idx) => `
            <div class="card">
                <div class="card-title">
                    <span>📘 ${escapeHtml(file.file_name || '未命名')}</span>
                    <div style="display:flex;gap:8px;">
                        <button class="btn btn-sm btn-success" onclick="downloadSingleBook('${task.task_id}', ${idx})">下载本书</button>
                    </div>
                </div>

                <div style="font-size:13px;color:var(--text-secondary);line-height:1.8;margin-bottom:12px;">
                    <div>配置：${escapeHtml(file.config_name || '默认配置')}</div>
                    <div>状态：${statusText(file.status)}</div>
                    <div>章节范围：第 ${file.start_chapter || '-'} - ${file.end_chapter || '-'} 章</div>
                    <div>批次大小：${file.batch_size || '-'}</div>
                    <div>章节进度：成功 ${file.completed_chapters || 0} / 失败 ${file.failed_chapters || 0} / 总计 ${file.total_chapters || 0}</div>
                    <div>结果上传：${file.result_uploaded ? '已上传' : (file.result_upload_error ? `失败：${escapeHtml(file.result_upload_error)}` : '未上传')}</div>
                </div>

                ${(file.batches || []).length ? `
                    <div style="display:flex;flex-direction:column;gap:10px;">
                        ${(file.batches || []).map(batch => `
                            <div style="border:1px solid var(--border);border-radius:10px;padding:12px;background:var(--bg-secondary);">
                                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                                    <div style="font-weight:600;">
                                        批次 ${batch.batch_index}：第${batch.chapter_start}-${batch.chapter_end}章
                                    </div>
                                    <div style="font-size:12px;color:${batch.success ? 'var(--success)' : 'var(--error)'};">
                                        ${batch.success ? '成功' : '失败'}
                                    </div>
                                </div>

                                ${batch.chapter_titles && batch.chapter_titles.length ? `
                                    <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;">
                                        ${escapeHtml(batch.chapter_titles.join(' | '))}
                                    </div>
                                ` : ''}

                                ${batch.success ? `
                                    <div style="font-size:13px;line-height:1.7;white-space:pre-wrap;word-break:break-word;">
                                        ${escapeHtml(batch.result || batch.preview || '')}
                                    </div>
                                ` : `
                                    <div style="font-size:13px;color:var(--error);white-space:pre-wrap;">
                                        ${escapeHtml(batch.error || '失败')}
                                    </div>
                                `}
                            </div>
                        `).join('')}
                    </div>
                ` : `
                    <div class="empty-state"><p>暂无批次结果</p></div>
                `}
            </div>
        `).join('')}
    `;

    if (downloadBtn) {
        downloadBtn.onclick = () => downloadTask(task.task_id);
    }

    modal.classList.remove('hidden');
}

async function cancelTask(taskId) {
    if (!taskId) return;
    if (!confirm('确定取消该任务吗？')) return;

    try {
        const data = await api('/api/batch/cancel', {
            method: 'POST',
            body: JSON.stringify({ taskId })
        });

        if (data.success) {
            showToast('任务已取消', 'success');
            await loadTasks(false);
            if (state.viewingTaskId === taskId) {
                await loadTaskDetail(taskId, false);
            }
        } else {
            showToast(data.error || '取消失败', 'error');
        }
    } catch (e) {
        console.error('cancelTask error:', e);
        showToast(`取消失败：${e.message || e}`, 'error');
    }
}

async function deleteTask(taskId) {
    if (!taskId) return;
    if (!confirm('确定删除该任务记录吗？')) return;

    try {
        const data = await api('/api/task/delete', {
            method: 'POST',
            body: JSON.stringify({ taskId })
        });

        if (data.success) {
            showToast('任务已删除', 'success');
            if (state.viewingTaskId === taskId) {
                closeTaskView();
            }
            await loadTasks(false);
        } else {
            showToast(data.error || '删除失败', 'error');
        }
    } catch (e) {
        console.error('deleteTask error:', e);
        showToast(`删除失败：${e.message || e}`, 'error');
    }
}

async function downloadTask(taskId) {
    try {
        const data = await api(`/api/task/${taskId}/download`);
        if (data.success) {
            downloadText(data.filename || `batch_${taskId.slice(0, 8)}.txt`, data.content || '');
        } else {
            showToast(data.error || '下载失败', 'error');
        }
    } catch (e) {
        console.error('downloadTask error:', e);
        showToast(`下载失败：${e.message || e}`, 'error');
    }
}

async function downloadSingleBook(taskId, fileIdx) {
    try {
        const data = await api(`/api/task/${taskId}/download/${fileIdx}`);
        if (data.success) {
            downloadText(data.filename || 'book.txt', data.content || '');
        } else {
            showToast(data.error || '下载失败', 'error');
        }
    } catch (e) {
        console.error('downloadSingleBook error:', e);
        showToast(`下载失败：${e.message || e}`, 'error');
    }
}

// ==================== 云端文件页 ====================
async function refreshCloudFiles() {
    try {
        const hfToken = getVal('cloudHfToken', '').trim();
        const hfDataset = getVal('cloudHfDataset', '').trim();

        const query = new URLSearchParams({
            hfToken,
            hfDataset
        }).toString();

        const data = await api(`/api/hf-files?${query}`);
        state.cloudFiles = data.files || [];
        renderCloudFiles();
    } catch (e) {
        console.error('refreshCloudFiles error:', e);
        showToast(`加载云端文件失败：${e.message || e}`, 'error');
    }
}

function renderCloudFiles() {
    const box = $('cloudFileList');
    if (!box) return;

    if (!state.cloudFiles.length) {
        box.innerHTML = `
            <div class="empty-state">
                <div class="icon">☁️</div>
                <p>暂无云端文件</p>
            </div>
        `;
        return;
    }

    box.innerHTML = state.cloudFiles.map(file => `
        <div class="task-item">
            <div>
                <div style="font-weight:600;">${escapeHtml(file.name || file.path || '未命名')}</div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
                    ${escapeHtml(file.path || '')}
                </div>
            </div>
            <div style="display:flex;gap:8px;">
                <button class="btn btn-sm btn-success" onclick="downloadCloudFile('${encodeURIComponent(file.path || file.name || '')}')">下载</button>
                <button class="btn btn-sm btn-danger" onclick="deleteCloudFile('${encodeURIComponent(file.path || file.name || '')}')">删除</button>
            </div>
        </div>
    `).join('');
}

async function downloadCloudFile(encodedPath) {
    try {
        const filename = decodeURIComponent(encodedPath);
        const hfToken = getVal('cloudHfToken', '').trim();
        const hfDataset = getVal('cloudHfDataset', '').trim();

        const query = new URLSearchParams({
            hfToken,
            hfDataset,
            filename
        }).toString();

        const data = await api(`/api/hf-download?${query}`);
        if (data.success) {
            downloadText(data.filename || filename.split('/').pop() || 'download.txt', data.content || '');
        } else {
            showToast(data.error || '下载失败', 'error');
        }
    } catch (e) {
        console.error('downloadCloudFile error:', e);
        showToast(`下载失败：${e.message || e}`, 'error');
    }
}

async function deleteCloudFile(encodedPath) {
    const filename = decodeURIComponent(encodedPath);
    if (!confirm(`确定删除云端文件：${filename} 吗？`)) return;

    try {
        const data = await api('/api/hf-action', {
            method: 'POST',
            body: JSON.stringify({
                action: 'delete',
                hfToken: getVal('cloudHfToken', '').trim(),
                hfDataset: getVal('cloudHfDataset', '').trim(),
                filename
            })
        });

        if (data.success) {
            showToast('云端文件已删除', 'success');
            await refreshCloudFiles();
        } else {
            showToast(data.error || '删除失败', 'error');
        }
    } catch (e) {
        console.error('deleteCloudFile error:', e);
        showToast(`删除失败：${e.message || e}`, 'error');
    }
}

// ==================== 初始化绑定 ====================
function bindNav() {
    document.querySelectorAll('.nav-item').forEach(el => {
        el.addEventListener('click', () => {
            const page = el.dataset.page;
            if (page) showPage(page);
        });
    });
}

function bindBatchUpload() {
    const input = $('batchFileInput');
    const area = $('batchUploadArea');

    if (input) {
        input.addEventListener('change', (e) => {
            const files = e.target.files;
            if (files && files.length) {
                handleBatchFiles(files);
                input.value = '';
            }
        });
    }

    if (area && input) {
        area.addEventListener('click', () => input.click());

        area.addEventListener('dragover', (e) => {
            e.preventDefault();
            area.classList.add('dragover');
        });

        area.addEventListener('dragleave', () => {
            area.classList.remove('dragover');
        });

        area.addEventListener('drop', (e) => {
            e.preventDefault();
            area.classList.remove('dragover');
            const files = e.dataTransfer?.files;
            if (files && files.length) {
                handleBatchFiles(files);
            }
        });
    }
}

function bindActions() {
    // 聊天
    safeAddEvent('btnNewConv', 'click', createConversation);
    safeAddEvent('btnSendMessage', 'click', sendChat);

    const chatInput = $('chatInput');
    if (chatInput) {
        chatInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendChat();
            }
        });
    }

    // 配置
    safeAddEvent('btnNewConfig', 'click', () => openConfigModal(null));
    safeAddEvent('btnCloseConfigModal', 'click', closeConfigModal);
    safeAddEvent('btnCancelConfig', 'click', closeConfigModal);
    safeAddEvent('btnSaveConfig', 'click', saveConfig);
    safeAddEvent('btnDeleteConfig', 'click', () => {
        if (state.editingConfigId) deleteConfig(state.editingConfigId);
    });

    // 设置
    safeAddEvent('btnSaveDefaults', 'click', saveDefaultSettings);
    safeAddEvent('btnSaveThreadSettings', 'click', saveThreadSettings);
    safeAddEvent('btnSaveDelaySettings', 'click', saveDelaySettings);

    // 批处理
    safeAddEvent('btnSubmitBatch', 'click', submitBatch);
    safeAddEvent('btnClearBatch', 'click', clearBatchFiles);

    // 任务
    safeAddEvent('btnRefreshTasks', 'click', () => loadTasks(true));
    safeAddEvent('btnCloseTaskView', 'click', closeTaskView);
    safeAddEvent('btnCloseTaskView2', 'click', closeTaskView);

    const taskModal = $('taskViewModal');
    if (taskModal) {
        taskModal.addEventListener('click', (e) => {
            if (e.target === taskModal) closeTaskView();
        });
    }

    // 云端
    safeAddEvent('btnRefreshCloudFiles', 'click', refreshCloudFiles);

    // 配置弹窗背景点击关闭
    const configModal = $('configModal');
    if (configModal) {
        configModal.addEventListener('click', (e) => {
            if (e.target === configModal) closeConfigModal();
        });
    }
}

async function initApp() {
    try {
        bindNav();
        bindBatchUpload();
        bindActions();

        await Promise.all([
            loadSettings(),
            loadConfigs(),
            loadTasks(false),
            loadConversations()
        ]);

        renderBatchFiles();
        renderBatchSubmitArea();
        renderTaskList();
        renderConversationList();
        renderChatMessages([]);
        renderCloudFiles();

        showPage('chat');
    } catch (e) {
        console.error('initApp error:', e);
        showToast(`初始化失败：${e.message || e}`, 'error');
    }
}

document.addEventListener('DOMContentLoaded', initApp);

// ==================== 导出到 window，供 HTML onclick 使用 ====================
window.showPage = showPage;

window.openConfigModal = openConfigModal;
window.closeConfigModal = closeConfigModal;
window.editConfig = editConfig;
window.deleteConfig = deleteConfig;

window.removeBatchFile = removeBatchFile;
window.updateBatchFileField = updateBatchFileField;
window.submitBatch = submitBatch;
window.clearBatchFiles = clearBatchFiles;

window.openTaskDetail = openTaskDetail;
window.closeTaskView = closeTaskView;
window.cancelTask = cancelTask;
window.deleteTask = deleteTask;
window.downloadTask = downloadTask;
window.downloadSingleBook = downloadSingleBook;

window.downloadCloudFile = downloadCloudFile;
window.deleteCloudFile = deleteCloudFile;

window.selectConversation = selectConversation;
window.deleteConversation = deleteConversation;