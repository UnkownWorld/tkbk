// ============================================
// AI Workflow Assistant - Frontend
// ============================================

const $ = (id) => document.getElementById(id);

// ==================== 状态 ====================
const state = {
    currentPage: 'chat',
    conversations: [],
    currentConvId: null,
    isSending: false,
    batchFiles: [],
    tasks: {},
    taskPollTimer: null,
    serverDefaults: {},
    configs: [],
    editingConfigId: null,
    viewingTaskId: null,
    cloudFiles: [],
    threadSettings: {
        maxConcurrent: 10,
        threadPoolSize: 10
    },
    defaultBatchSize: 10,  // 默认批次大小
    batchDelayMin: 15,     // 默认最小延迟
    batchDelayMax: 45      // 默认最大延迟
};

// ==================== 工具 ====================
function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function showToast(msg, type = 'info') {
    const c = $('toastContainer');
    const t = document.createElement('div');
    t.className = `toast toast-${type}`;
    t.textContent = msg;
    c.appendChild(t);
    setTimeout(() => t.remove(), 3500);
}

function showLoading(text = '处理中...') {
    $('globalLoadingText').textContent = text;
    $('globalLoading').classList.remove('hidden');
}

function hideLoading() {
    $('globalLoading').classList.add('hidden');
}

function statusText(s) {
    return { pending: '等待中', processing: '处理中', completed: '已完成', failed: '失败', cancelled: '已取消', paused: '已暂停' }[s] || s;
}

// ==================== API ====================
async function api(path, options = {}) {
    try {
        const resp = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
        return await resp.json();
    } catch (e) {
        showToast('网络错误: ' + e.message, 'error');
        return { success: false, error: e.message };
    }
}

// ==================== 页面切换 ====================
function switchPage(page) {
    state.currentPage = page;
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    $(`page-${page}`).classList.add('active');
    document.querySelector(`.nav-item[data-page="${page}"]`).classList.add('active');
    if (page === 'tasks') refreshTasks();
    if (page === 'cloud') refreshCloudFiles();
    if (page === 'settings') { renderServerConfigStatus(); renderDefaultsForm(); refreshConfigs(); loadThreadSettings(); }
}

// ==================== 默认配置 ====================
async function loadServerSettings() {
    const data = await api('/api/settings');
    if (data.success) state.serverDefaults = data.settings;
}

function renderServerConfigStatus() {
    const c = $('serverConfigStatus');
    const items = [
        { label: 'API Key', key: 'hasApiKey' },
        { label: 'HF Token', key: 'hasHfToken' },
        { label: 'HF 数据集', key: 'hfDataset', showVal: true },
    ];
    c.innerHTML = items.map(item => {
        const val = state.serverDefaults[item.key];
        const configured = item.showVal ? !!val : !!val;
        const display = item.showVal ? (val || '未配置') : (val ? '已配置 ✓' : '未配置');
        return `<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--bg-primary);border-radius:8px;">
            <span style="font-size:14px;">${escapeHtml(item.label)}</span>
            <span class="status-badge ${configured ? 'configured' : 'not-configured'}">
                <span class="status-dot"></span>${escapeHtml(display)}
            </span>
        </div>`;
    }).join('');
}

function renderDefaultsForm() {
    const d = state.serverDefaults;
    $('defApiHost').value = d.apiHost || '';
    $('defModel').value = d.model || '';
    $('defTemperature').value = d.temperature ?? '';
    $('defTopP').value = d.topP ?? '';
    $('defContextRounds').value = d.contextRounds ?? '';
    $('defMaxOutputTokens').value = d.maxOutputTokens ?? '';
    // 聊天功能系统提示词
    $('defSystemPrompt').value = d.systemPrompt || '';
    // 批处理功能提示词
    $('defBatchSystemPrompt').value = d.batchSystemPrompt || '';
    $('defBatchUserPromptTemplate').value = d.batchUserPromptTemplate || '';
}

async function saveDefaults() {
    const data = await api('/api/settings/update', {
        method: 'POST',
        body: JSON.stringify({
            apiHost: $('defApiHost').value.trim(),
            model: $('defModel').value.trim(),
            temperature: $('defTemperature').value !== '' ? parseFloat($('defTemperature').value) : '',
            topP: $('defTopP').value !== '' ? parseFloat($('defTopP').value) : '',
            contextRounds: $('defContextRounds').value !== '' ? parseInt($('defContextRounds').value) : '',
            maxOutputTokens: $('defMaxOutputTokens').value !== '' ? parseInt($('defMaxOutputTokens').value) : '',
            systemPrompt: $('defSystemPrompt').value.trim(),
            batchSystemPrompt: $('defBatchSystemPrompt').value.trim(),
            batchUserPromptTemplate: $('defBatchUserPromptTemplate').value.trim(),
        }),
    });
    if (data.success) {
        state.serverDefaults = data.settings;
        showToast('默认配置已保存', 'success');
    } else {
        showToast('保存失败: ' + (data.error || ''), 'error');
    }
}

// ==================== 线程池设置 ====================
async function loadThreadSettings() {
    const data = await api('/api/status');
    if (data.success) {
        state.threadSettings.maxConcurrent = data.maxConcurrent;
        state.threadSettings.threadPoolSize = data.threadPoolSize;
        state.defaultBatchSize = data.batchSize || 10;
        state.batchDelayMin = data.batchDelayMin || 15;
        state.batchDelayMax = data.batchDelayMax || 45;
        $('maxConcurrent').value = data.maxConcurrent;
        $('threadPoolSize').value = data.threadPoolSize;
        // 更新延迟输入框
        if ($('batchDelayMin')) $('batchDelayMin').value = state.batchDelayMin;
        if ($('batchDelayMax')) $('batchDelayMax').value = state.batchDelayMax;
        // 更新批处理页面的延迟输入框
        if ($('globalDelayMin')) $('globalDelayMin').value = state.batchDelayMin;
        if ($('globalDelayMax')) $('globalDelayMax').value = state.batchDelayMax;
    }
}

async function saveThreadSettings() {
    const maxConcurrent = parseInt($('maxConcurrent').value) || 10;
    const threadPoolSize = parseInt($('threadPoolSize').value) || 10;
    
    // 保存并发数
    const data1 = await api('/api/set-concurrent', {
        method: 'POST',
        body: JSON.stringify({ maxConcurrent }),
    });
    
    // 保存线程池大小
    const data2 = await api('/api/set-thread-pool', {
        method: 'POST',
        body: JSON.stringify({ threadPoolSize }),
    });
    
    if (data1.success && data2.success) {
        state.threadSettings.maxConcurrent = maxConcurrent;
        state.threadSettings.threadPoolSize = threadPoolSize;
        $('threadStatus').textContent = '已保存 ✓';
        showToast('线程池设置已保存', 'success');
        setTimeout(() => { $('threadStatus').textContent = ''; }, 2000);
    } else {
        showToast('保存失败', 'error');
    }
}

async function saveDelaySettings() {
    const delayMin = parseInt($('batchDelayMin').value) || 15;
    const delayMax = parseInt($('batchDelayMax').value) || 45;
    
    const data = await api('/api/set-batch-delay', {
        method: 'POST',
        body: JSON.stringify({ delayMin, delayMax }),
    });
    
    if (data.success) {
        state.batchDelayMin = data.batchDelayMin;
        state.batchDelayMax = data.batchDelayMax;
        $('delayStatus').textContent = '已保存 ✓';
        showToast('延迟设置已保存', 'success');
        setTimeout(() => { $('delayStatus').textContent = ''; }, 2000);
    } else {
        showToast('保存失败', 'error');
    }
}

// ==================== 配置管理 ====================
async function refreshConfigs() {
    const data = await api('/api/config/list?userId=default');
    if (data.success) {
        state.configs = data.configs || [];
        renderConfigList();
        updateConfigSelects();
    }
}

function updateConfigSelects() {
    // 聊天页
    const sel = $('chatConfigSelect');
    const currentVal = sel.value;
    sel.innerHTML = '<option value="">默认配置</option>';
    state.configs.forEach(c => {
        sel.innerHTML += `<option value="${c.id}">${escapeHtml(c.name)}</option>`;
    });
    sel.value = currentVal;

    // 批处理文件卡片
    document.querySelectorAll('.batch-config-select').forEach(sel => {
        const fileId = sel.dataset.fileId;
        const current = sel.value;
        sel.innerHTML = '<option value="">默认配置</option>';
        state.configs.forEach(c => {
            sel.innerHTML += `<option value="${c.id}">${escapeHtml(c.name)}</option>`;
        });
        sel.value = current;
    });
}

function renderConfigList() {
    const c = $('configList');
    if (state.configs.length === 0) {
        c.innerHTML = '<p style="color:var(--text-muted);font-size:13px;">暂无配置，点击右上角新建</p>';
        return;
    }
    c.innerHTML = state.configs.map(cfg => {
        const badges = [];
        if (cfg.hasApiKey) badges.push('🔑 API');
        if (cfg.hasHfToken) badges.push('☁️ HF');
        if (cfg.hfDataset) badges.push('📦 ' + cfg.hfDataset);
        if (cfg.systemPrompt || cfg.batchSystemPrompt) badges.push('📝 提示词');
        if (cfg.batchSize) badges.push('📚 批次:' + cfg.batchSize);
        const diff = [];
        if (cfg.model && cfg.model !== state.serverDefaults.model) diff.push('模型');
        if (cfg.temperature !== '' && cfg.temperature !== state.serverDefaults.temperature) diff.push('Temp');
        if (diff.length) badges.push('⚙️ ' + diff.join(','));
        return `<div class="config-item" onclick="editConfig('${cfg.id}')">
            <div>
                <div class="name">${escapeHtml(cfg.name)}</div>
                <div class="desc">${badges.join(' · ') || '使用默认配置'}</div>
            </div>
            <span style="color:var(--text-muted);font-size:12px;">点击编辑</span>
        </div>`;
    }).join('');
}

function openConfigModal(configId = null) {
    state.editingConfigId = configId;
    $('configModalTitle').textContent = configId ? '编辑配置' : '新建配置';
    $('btnDeleteConfig').style.display = configId ? 'inline-flex' : 'none';

    // 默认值（从 serverDefaults 取）
    const d = state.serverDefaults;

    if (configId) {
        const cfg = state.configs.find(c => c.id === configId);
        if (cfg) {
            $('cfgName').value = cfg.name || '';
            // 聊天功能提示词
            $('cfgSystemPrompt').value = cfg.systemPrompt || '';
            $('cfgContextRounds').value = cfg.contextRounds || '';
            $('cfgMaxOutputTokens').value = cfg.maxOutputTokens || '';
            // 批处理功能提示词
            $('cfgBatchSystemPrompt').value = cfg.batchSystemPrompt || '';
            $('cfgBatchUserPromptTemplate').value = cfg.batchUserPromptTemplate || '';
            $('cfgBatchSize').value = cfg.batchSize || '';
            // API配置
            $('cfgApiHost').value = cfg.apiHost || '';
            $('cfgApiKey').value = '';
            $('cfgModel').value = cfg.model || '';
            $('cfgTemperature').value = cfg.temperature ?? '';
            $('cfgTopP').value = cfg.topP ?? '';
            // HF配置
            $('cfgHfToken').value = '';
            $('cfgHfDataset').value = cfg.hfDataset || '';
            $('configModal').classList.remove('hidden');
            return;
        }
    }

    // 新建配置：预填默认值（除敏感字段）
    $('cfgName').value = '';
    // 聊天功能提示词
    $('cfgSystemPrompt').value = d.systemPrompt || '';
    $('cfgContextRounds').value = d.contextRounds || '';
    $('cfgMaxOutputTokens').value = d.maxOutputTokens || '';
    // 批处理功能提示词
    $('cfgBatchSystemPrompt').value = d.batchSystemPrompt || '';
    $('cfgBatchUserPromptTemplate').value = d.batchUserPromptTemplate || '';
    $('cfgBatchSize').value = state.defaultBatchSize || 10;
    // API配置
    $('cfgApiHost').value = '';
    $('cfgApiKey').value = '';
    $('cfgModel').value = d.model || '';
    $('cfgTemperature').value = d.temperature ?? '';
    $('cfgTopP').value = d.topP ?? '';
    // HF配置
    $('cfgHfToken').value = '';
    $('cfgHfDataset').value = '';
    $('configModal').classList.remove('hidden');
}

function editConfig(id) { openConfigModal(id); }

async function saveConfig() {
    const name = $('cfgName').value.trim();
    if (!name) { showToast('请输入配置名称', 'warning'); return; }

    const data = await api('/api/config/save', {
        method: 'POST',
        body: JSON.stringify({
            userId: 'default',
            id: state.editingConfigId,
            name,
            // 聊天功能提示词
            systemPrompt: $('cfgSystemPrompt').value.trim(),
            contextRounds: $('cfgContextRounds').value !== '' ? parseInt($('cfgContextRounds').value) : '',
            maxOutputTokens: $('cfgMaxOutputTokens').value !== '' ? parseInt($('cfgMaxOutputTokens').value) : '',
            // 批处理功能提示词
            batchSystemPrompt: $('cfgBatchSystemPrompt').value.trim(),
            batchUserPromptTemplate: $('cfgBatchUserPromptTemplate').value.trim(),
            batchSize: $('cfgBatchSize').value !== '' ? parseInt($('cfgBatchSize').value) : '',
            // API配置
            apiHost: $('cfgApiHost').value.trim(),
            apiKey: $('cfgApiKey').value.trim(),
            model: $('cfgModel').value.trim(),
            temperature: $('cfgTemperature').value !== '' ? parseFloat($('cfgTemperature').value) : '',
            topP: $('cfgTopP').value !== '' ? parseFloat($('cfgTopP').value) : '',
            // HF配置
            hfToken: $('cfgHfToken').value.trim(),
            hfDataset: $('cfgHfDataset').value.trim(),
        }),
    });

    if (data.success) {
        showToast('配置已保存', 'success');
        $('configModal').classList.add('hidden');
        await refreshConfigs();
    } else {
        showToast('保存失败: ' + (data.error || ''), 'error');
    }
}

async function deleteConfig() {
    if (!state.editingConfigId) return;
    if (!confirm('确定删除此配置？')) return;
    const data = await api('/api/config/delete', {
        method: 'POST',
        body: JSON.stringify({ userId: 'default', id: state.editingConfigId }),
    });
    if (data.success) {
        showToast('已删除', 'success');
        $('configModal').classList.add('hidden');
        await refreshConfigs();
    }
}

// ==================== 聊天 ====================
async function loadConversations() {
    const data = await api('/api/conversations?userId=default');
    if (data.success) state.conversations = data.conversations || [];
}

async function createConversation() {
    const configId = $('chatConfigSelect').value;
    const configName = $('chatConfigSelect').selectedOptions[0]?.textContent || '默认配置';
    const data = await api('/api/conversation/create', {
        method: 'POST',
        body: JSON.stringify({ userId: 'default', title: configName, configId }),
    });
    if (data.success) {
        state.currentConvId = data.conversation.id;
        await loadConversations();
        renderChatMessages();
    }
}

async function sendMessage() {
    const input = $('chatInput');
    const message = input.value.trim();
    if (!message || state.isSending) return;

    if (!state.currentConvId) {
        await createConversation();
        if (!state.currentConvId) return;
    }

    state.isSending = true;
    $('btnSendMessage').disabled = true;
    input.value = '';
    input.style.height = 'auto';

    appendChatBubble('user', message);
    appendChatBubble('assistant', '思考中...', true);

    const configId = $('chatConfigSelect').value;
    const data = await api('/api/chat', {
        method: 'POST',
        body: JSON.stringify({
            userId: 'default',
            conversationId: state.currentConvId,
            message,
            configId,
        }),
    });

    const thinking = document.querySelector('.chat-message.assistant .chat-bubble.loading-bubble');
    if (thinking) thinking.closest('.chat-message').remove();

    if (data.success) {
        appendChatBubble('assistant', data.message);
    } else {
        appendChatBubble('assistant', '❌ ' + (data.error || '请求失败'));
    }

    state.isSending = false;
    $('btnSendMessage').disabled = false;
    input.focus();
}

function appendChatBubble(role, content, isLoading = false) {
    const container = $('chatMessages');
    const empty = container.querySelector('.empty-state');
    if (empty) empty.remove();

    const div = document.createElement('div');
    div.className = `chat-message ${role}`;
    div.innerHTML = `
        <div class="chat-avatar">${role === 'user' ? '👤' : '🤖'}</div>
        <div class="chat-bubble${isLoading ? ' loading-bubble' : ''}">${escapeHtml(content)}</div>
    `;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function renderChatMessages() {
    const container = $('chatMessages');
    container.innerHTML = '';
    if (!state.currentConvId) {
        container.innerHTML = '<div class="empty-state"><div class="icon">💬</div><p>选择配置，开始新对话</p></div>';
        return;
    }
    const conv = state.conversations.find(c => c.id === state.currentConvId);
    if (!conv || !conv.messages || conv.messages.length === 0) {
        container.innerHTML = '<div class="empty-state"><div class="icon">💬</div><p>开始新对话吧</p></div>';
        return;
    }
    conv.messages.forEach(msg => appendChatBubble(msg.role, msg.content));
    container.scrollTop = container.scrollHeight;
}

// ==================== 批处理（多文件） ====================
function handleBatchFiles(files) {
    Array.from(files).forEach(file => {
        if (!file.name.endsWith('.txt')) return;
        const reader = new FileReader();
        reader.onload = async (e) => {
            const content = e.target.result;
            showLoading(`正在解析 ${file.name} ...`);
            const data = await api('/api/parse-chapters', {
                method: 'POST',
                body: JSON.stringify({ content }),
            });
            hideLoading();

            if (data.success && data.chapters.length > 0) {
                const fileId = Date.now() + '_' + Math.random().toString(36).substr(2, 6);
                state.batchFiles.push({
                    fileId,
                    fileName: file.name,
                    chapters: data.chapters,
                    configId: '',
                    batchSize: state.defaultBatchSize,
                });
                renderBatchFiles();
                showToast(`${file.name}: ${data.chapters.length} 章`, 'success');
            } else {
                showToast(`${file.name}: 未识别到章节`, 'warning');
            }
        };
        reader.readAsText(file);
    });
}

function renderBatchFiles() {
    const container = $('batchFileList');
    const submitArea = $('batchSubmitArea');

    if (state.batchFiles.length === 0) {
        container.innerHTML = '';
        submitArea.classList.add('hidden');
        return;
    }

    submitArea.classList.remove('hidden');
    const totalChapters = state.batchFiles.reduce((sum, f) => sum + f.chapters.length, 0);

    container.innerHTML = state.batchFiles.map(f => `
        <div class="batch-file-card" data-file-id="${f.fileId}">
            <div class="batch-file-header">
                <div>
                    <div class="batch-file-name">📄 ${escapeHtml(f.fileName)}</div>
                    <div class="batch-file-meta">${f.chapters.length} 个章节</div>
                </div>
                <button class="btn btn-sm btn-danger" onclick="removeBatchFile('${f.fileId}')">✕ 移除</button>
            </div>
            <div class="batch-file-config">
                <label>使用配置:</label>
                <select class="form-input batch-config-select" data-file-id="${f.fileId}" onchange="updateBatchFileConfig('${f.fileId}', this.value)">
                    <option value="">默认配置</option>
                    ${state.configs.map(c => `<option value="${c.id}" ${f.configId === c.id ? 'selected' : ''}>${escapeHtml(c.name)}</option>`).join('')}
                </select>
            </div>
            <div class="batch-file-config">
                <label>批次大小:</label>
                <input type="number" class="form-input batch-size-input" data-file-id="${f.fileId}" 
                    value="${f.batchSize || state.defaultBatchSize}" min="1" max="50" 
                    onchange="updateBatchFileSize('${f.fileId}', this.value)" style="width:80px;">
                <span style="font-size:12px;color:var(--text-muted);">章/批次</span>
            </div>
        </div>
    `).join('') + `<p style="font-size:13px;color:var(--text-muted);margin-top:8px;">共 ${state.batchFiles.length} 个文件，${totalChapters} 个章节</p>`;
}

function removeBatchFile(fileId) {
    state.batchFiles = state.batchFiles.filter(f => f.fileId !== fileId);
    renderBatchFiles();
}

function updateBatchFileConfig(fileId, configId) {
    const f = state.batchFiles.find(f => f.fileId === fileId);
    if (f) f.configId = configId;
}

function updateBatchFileSize(fileId, batchSize) {
    const f = state.batchFiles.find(f => f.fileId === fileId);
    if (f) f.batchSize = parseInt(batchSize) || state.defaultBatchSize;
}

async function submitBatch() {
    if (state.batchFiles.length === 0) return;

    showLoading('正在提交批处理任务...');
    
    // 获取全局批次大小和延迟配置
    const globalBatchSize = parseInt($('globalBatchSize')?.value || state.defaultBatchSize);
    const delayMin = parseInt($('globalDelayMin')?.value || state.batchDelayMin);
    const delayMax = parseInt($('globalDelayMax')?.value || state.batchDelayMax);
    
    const files = state.batchFiles.map(f => ({
        fileName: f.fileName,
        chapters: f.chapters,
        configId: f.configId,
        batchSize: f.batchSize || globalBatchSize,
        delayMin: delayMin,
        delayMax: delayMax
    }));

    const data = await api('/api/batch', {
        method: 'POST',
        body: JSON.stringify({ 
            userId: 'default', 
            files,
            batchSize: globalBatchSize,
            delayMin: delayMin,
            delayMax: delayMax
        }),
    });
    hideLoading();

    if (data.success) {
        showToast(`任务已提交: ${data.totalChapters} 个章节`, 'success');
        state.batchFiles = [];
        renderBatchFiles();
        switchPage('tasks');
    } else {
        showToast('提交失败: ' + (data.error || ''), 'error');
    }
}

// ==================== 任务管理 ====================
async function refreshTasks() {
    const data = await api('/api/tasks');
    if (data.success) {
        state.tasks = data.tasks || {};
        renderTaskList();
    }
}

function renderTaskList() {
    const container = $('taskList');
    const tasks = Object.values(state.tasks);
    if (tasks.length === 0) {
        container.innerHTML = '<div class="empty-state"><div class="icon">📋</div><p>暂无任务</p></div>';
        return;
    }
    tasks.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));

    container.innerHTML = tasks.map(task => {
        const progress = task.total_chapters > 0 ? (task.completed_chapters / task.total_chapters * 100) : 0;
        const isRunning = task.status === 'processing' || task.status === 'pending';
        // 已取消、失败、或已完成但有失败章节的任务可以恢复
        const canResume = task.status === 'cancelled' || task.status === 'failed' || 
            (task.status === 'completed' && task.failed_chapters > 0);
        const filesBrief = (task.files || []).map(f => `${f.file_name}(${f.config_name})`).join(', ');
        return `<div class="task-item">
            <div class="task-info">
                <div class="task-name">${escapeHtml(filesBrief || '批处理任务')}</div>
                <div class="task-meta">${escapeHtml(task.message || '')} ${task.result_persisted ? '· ☁️已保存' : ''}</div>
                <div class="task-files-brief">${task.completed_chapters || 0}/${task.total_chapters || 0} 章 (失败: ${task.failed_chapters || 0})</div>
                <div class="progress-bar"><div class="progress-fill" style="width:${progress}%"></div></div>
            </div>
            <div class="task-actions">
                <span class="task-status ${task.status}">${statusText(task.status)}</span>
                <button class="btn btn-sm btn-secondary" onclick="viewTask('${task.task_id}')">👁 查看</button>
                <button class="btn btn-sm btn-secondary" onclick="downloadTask('${task.task_id}')">📥 下载</button>
                ${isRunning ? `<button class="btn btn-sm btn-danger" onclick="cancelTask('${task.task_id}')">取消</button>` : ''}
                ${canResume ? `<button class="btn btn-sm btn-success" onclick="resumeTask('${task.task_id}')">▶ 恢复</button>` : ''}
                <button class="btn btn-sm btn-danger" onclick="deleteTask('${task.task_id}')">🗑️</button>
            </div>
        </div>`;
    }).join('');
}

async function viewTask(taskId) {
    showLoading('加载任务详情...');
    const data = await api(`/api/task/${taskId}`);
    hideLoading();

    if (!data.success) {
        showToast(data.error || '加载失败', 'error');
        return;
    }

    state.viewingTaskId = taskId;
    const task = data.task;
    $('taskViewTitle').textContent = `任务结果 - ${statusText(task.status)}`;

    let html = '';
    (task.files || []).forEach((file, fi) => {
        // 每本小说一个独立区块
        const totalBatches = (file.results || []).length;
        const completedChapters = file.completed || 0;
        const totalChapters = file.total || 0;

        html += `<div class="result-file-section">
            <div class="result-file-title">📖 ${escapeHtml(file.file_name)} · ${completedChapters}/${totalChapters} 章 · ${totalBatches} 批次
            <button class="btn btn-sm btn-secondary" style="float:right;margin-top:-4px;" onclick="downloadSingleNovel('${taskId}', ${fi})">📥 下载此小说</button>
            </div>`;

        // 按批次分组展示
        (file.results || []).forEach((r, ri) => {
            const batchNum = r.batch || ri + 1;
            const totalB = r.total_batches || totalBatches;
            const chStart = r.chapter_start || '?';
            const chEnd = r.chapter_end || '?';
            const chCount = r.chapter_count || '?';
            const isSuccess = r.success;

            // 章节标题列表
            const titles = (r.chapter_titles || []).map(t => escapeHtml(t)).join('、');

            html += `<div class="result-batch">
                <div class="result-batch-header" onclick="toggleBatchResult(this)">
                    <span class="result-batch-title">📦 批次 ${batchNum}/${totalB}: 第${chStart}-${chEnd}章 (共${chCount}章)</span>
                    <span class="result-batch-status ${isSuccess ? 'success' : 'fail'}">${isSuccess ? '✓ 成功' : '✗ 失败'}</span>
                </div>
                <div class="result-batch-titles">${titles}</div>
                <div class="result-batch-preview">${isSuccess ? escapeHtml(r.preview || '') : escapeHtml(r.error || '未知错误')}</div>
                <div class="result-batch-full" id="batch-${fi}-${ri}">${isSuccess ? escapeHtml(r.result || '') : escapeHtml(r.error || '')}</div>
            </div>`;
        });

        if (!file.results || file.results.length === 0) {
            html += '<p style="color:var(--text-muted);font-size:13px;">暂无结果</p>';
        }
        html += '</div>';
    });

    $('taskViewBody').innerHTML = html;
    $('taskViewModal').classList.remove('hidden');
}

function toggleBatchResult(header) {
    const batch = header.closest('.result-batch');
    const full = batch.querySelector('.result-batch-full');
    const preview = batch.querySelector('.result-batch-preview');
    const titles = batch.querySelector('.result-batch-titles');
    if (full.classList.contains('expanded')) {
        full.classList.remove('expanded');
        preview.style.display = '';
        titles.style.display = '';
    } else {
        full.classList.add('expanded');
        preview.style.display = 'none';
        titles.style.display = 'none';
    }
}

async function downloadTask(taskId) {
    showLoading('准备下载...');
    const data = await api(`/api/task/${taskId}/download`);
    hideLoading();
    if (data.success) {
        const blob = new Blob([data.content], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = data.filename || 'batch_result.txt';
        a.click();
        URL.revokeObjectURL(url);
        showToast('下载成功', 'success');
    } else {
        showToast(data.error || '下载失败', 'error');
    }
}

async function downloadSingleNovel(taskId, fileIdx) {
    showLoading('准备下载单本小说...');
    const data = await api(`/api/task/${taskId}/download/${fileIdx}`);
    hideLoading();
    if (data.success) {
        const blob = new Blob([data.content], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = data.filename || 'novel_result.txt';
        a.click();
        URL.revokeObjectURL(url);
        showToast('下载成功', 'success');
    } else {
        showToast(data.error || '下载失败', 'error');
    }
}

async function cancelTask(taskId) {
    await api('/api/batch/cancel', { method: 'POST', body: JSON.stringify({ taskId }) });
    showToast('任务已取消', 'success');
    refreshTasks();
}

async function resumeTask(taskId) {
    // 获取延迟配置
    const delayMin = parseInt($('globalDelayMin')?.value || state.batchDelayMin);
    const delayMax = parseInt($('globalDelayMax')?.value || state.batchDelayMax);
    
    showLoading('正在恢复任务...');
    const data = await api('/api/batch/resume', { 
        method: 'POST', 
        body: JSON.stringify({ 
            taskId,
            delayMin: delayMin,
            delayMax: delayMax
        }) 
    });
    hideLoading();
    
    if (data.success) {
        showToast('任务已恢复运行', 'success');
        refreshTasks();
    } else {
        showToast('恢复失败: ' + (data.error || ''), 'error');
    }
}

async function deleteTask(taskId) {
    if (!confirm('确定删除此任务？')) return;
    await api('/api/task/delete', { method: 'POST', body: JSON.stringify({ taskId }) });
    showToast('已删除', 'success');
    refreshTasks();
}

// ==================== 云端文件 ====================
function getCloudHfConfig() {
    return {
        hfToken: ($('cloudHfToken') ? $('cloudHfToken').value : '') || '',
        hfDataset: ($('cloudHfDataset') ? $('cloudHfDataset').value : '') || ''
    };
}

async function refreshCloudFiles() {
    showLoading('加载文件列表...');
    const { hfToken, hfDataset } = getCloudHfConfig();
    const data = await api(`/api/hf-files?hfToken=${encodeURIComponent(hfToken)}&hfDataset=${encodeURIComponent(hfDataset)}`);
    hideLoading();
    if (data.success) {
        state.cloudFiles = data.files || [];
        renderCloudFiles();
    } else {
        $('cloudFileList').innerHTML = `<div class="empty-state"><div class="icon">⚠️</div><p>${escapeHtml(data.error || '加载失败，请检查 HF 配置')}</p></div>`;
    }
}

function renderCloudFiles() {
    const c = $('cloudFileList');
    if (state.cloudFiles.length === 0) {
        c.innerHTML = '<div class="empty-state"><div class="icon">☁️</div><p>暂无文件</p></div>';
        return;
    }
    c.innerHTML = state.cloudFiles.map(f =>
        `<div class="file-item">
            <span>📄 ${escapeHtml(f.name || f.path)}</span>
            <div style="display:flex;gap:6px;">
                <button class="btn btn-sm btn-secondary" onclick="downloadCloudFile('${escapeHtml(f.path)}')">下载</button>
                <button class="btn btn-sm btn-danger" onclick="deleteCloudFile('${escapeHtml(f.path)}')">删除</button>
            </div>
        </div>`
    ).join('');
}

async function downloadCloudFile(path) {
    showLoading('下载中...');
    const { hfToken, hfDataset } = getCloudHfConfig();
    const data = await api('/api/hf-download', {
        method: 'POST',
        body: JSON.stringify({ hfToken, hfDataset, filename: path }),
    });
    hideLoading();
    if (data.success) {
        const blob = new Blob([data.content], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = data.filename || 'download.txt';
        a.click();
        URL.revokeObjectURL(url);
    } else {
        showToast(data.error || '下载失败', 'error');
    }
}

async function deleteCloudFile(path) {
    if (!confirm(`确定删除 ${path}？`)) return;
    const { hfToken, hfDataset } = getCloudHfConfig();
    const data = await api('/api/hf-action', {
        method: 'POST',
        body: JSON.stringify({ hfToken, hfDataset, action: 'delete', filename: path }),
    });
    if (data.success) {
        showToast('已删除', 'success');
        refreshCloudFiles();
    } else {
        showToast(data.error || '删除失败', 'error');
    }
}

// ==================== 任务轮询 ====================
function startTaskPolling() {
    if (state.taskPollTimer) clearInterval(state.taskPollTimer);
    state.taskPollTimer = setInterval(() => {
        if (state.currentPage === 'tasks') refreshTasks();
    }, 3000);
}

// ==================== 事件绑定 ====================
function bindEvents() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => switchPage(item.dataset.page));
    });

    // 聊天
    $('btnSendMessage').addEventListener('click', sendMessage);
    $('chatInput').addEventListener('keydown', e => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) sendMessage();
    });
    $('chatInput').addEventListener('input', e => {
        e.target.style.height = 'auto';
        e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px';
    });
    $('btnNewConv').addEventListener('click', createConversation);

    // 批处理
    $('batchUploadArea').addEventListener('click', () => $('batchFileInput').click());
    $('batchFileInput').addEventListener('change', e => {
        if (e.target.files.length) handleBatchFiles(e.target.files);
        e.target.value = '';
    });
    $('batchUploadArea').addEventListener('dragover', e => { e.preventDefault(); e.stopPropagation(); });
    $('batchUploadArea').addEventListener('drop', e => {
        e.preventDefault(); e.stopPropagation();
        if (e.dataTransfer.files.length) handleBatchFiles(e.dataTransfer.files);
    });
    $('btnSubmitBatch').addEventListener('click', submitBatch);
    $('btnClearBatch').addEventListener('click', () => { state.batchFiles = []; renderBatchFiles(); });

    // 任务
    $('btnRefreshTasks').addEventListener('click', refreshTasks);

    // 云端文件
    $('btnRefreshCloudFiles').addEventListener('click', refreshCloudFiles);

    // 设置 - 默认配置
    $('btnSaveDefaults').addEventListener('click', saveDefaults);
    
    // 设置 - 线程池
    $('btnSaveThreadSettings').addEventListener('click', saveThreadSettings);
    
    // 设置 - 延迟
    $('btnSaveDelaySettings').addEventListener('click', saveDelaySettings);

    // 配置管理
    $('btnNewConfig').addEventListener('click', () => openConfigModal());
    $('btnCloseConfigModal').addEventListener('click', () => $('configModal').classList.add('hidden'));
    $('btnCancelConfig').addEventListener('click', () => $('configModal').classList.add('hidden'));
    $('btnSaveConfig').addEventListener('click', saveConfig);
    $('btnDeleteConfig').addEventListener('click', deleteConfig);

    // 任务查看模态框
    $('btnCloseTaskView').addEventListener('click', () => $('taskViewModal').classList.add('hidden'));
    $('btnCloseTaskView2').addEventListener('click', () => $('taskViewModal').classList.add('hidden'));
    $('btnDownloadFromView').addEventListener('click', () => {
        if (state.viewingTaskId) downloadTask(state.viewingTaskId);
    });

    // 密钥可见性切换
    document.querySelectorAll('.toggle-visibility').forEach(btn => {
        btn.addEventListener('click', () => {
            const target = $(btn.dataset.target);
            target.type = target.type === 'password' ? 'text' : 'password';
        });
    });
}

// ==================== 初始化 ====================
async function init() {
    bindEvents();
    await loadServerSettings();
    await refreshConfigs();
    await loadConversations();
    renderChatMessages();
    startTaskPolling();
}

document.addEventListener('DOMContentLoaded', init);
