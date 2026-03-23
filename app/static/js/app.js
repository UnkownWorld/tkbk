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
    defaultBatchSize: 10,
    batchDelayMin: 15,
    batchDelayMax: 45
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

function clampInt(value, defaultValue, minValue = null, maxValue = null) {
    let n = parseInt(value, 10);
    if (Number.isNaN(n)) n = defaultValue;
    if (minValue !== null) n = Math.max(minValue, n);
    if (maxValue !== null) n = Math.min(maxValue, n);
    return n;
}

function normalizeText(text) {
    return (text || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
}

function getConfigNameById(configId) {
    if (!configId) return '默认配置';
    const cfg = state.configs.find(c => c.id === configId);
    return cfg ? cfg.name : '默认配置';
}

// ==================== API ====================
async function api(path, options = {}) {
    try {
        const resp = await fetch(path, {
            headers: { 'Content-Type': 'application/json' },
            ...options
        });
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

    const pageEl = $(`page-${page}`);
    if (pageEl) pageEl.classList.add('active');

    const navEl = document.querySelector(`.nav-item[data-page="${page}"]`);
    if (navEl) navEl.classList.add('active');

    if (page === 'tasks') refreshTasks();
    if (page === 'cloud') refreshCloudFiles();
    if (page === 'settings') {
        renderServerConfigStatus();
        renderDefaultsForm();
        refreshConfigs();
        loadThreadSettings();
    }
}

// ==================== 默认配置 ====================
async function loadServerSettings() {
    const data = await api('/api/settings');
    if (data.success) {
        state.serverDefaults = data.settings || {};
        state.defaultBatchSize = clampInt(state.serverDefaults.batchSize || 10, 10, 1, 100);
        state.batchDelayMin = clampInt(state.serverDefaults.batchDelayMin || 15, 15, 0, 3600);
        state.batchDelayMax = clampInt(state.serverDefaults.batchDelayMax || 45, 45, state.batchDelayMin, 3600);
    }
}

function renderServerConfigStatus() {
    const c = $('serverConfigStatus');
    if (!c) return;

    const items = [
        { label: 'API Key', key: 'hasApiKey' },
        { label: 'HF Token', key: 'hasHfToken' },
        { label: 'HF 数据集', key: 'hfDataset', showVal: true },
    ];

    c.innerHTML = items.map(item => {
        const val = state.serverDefaults[item.key];
        const display = item.showVal ? (val || '未配置') : (val ? '已配置 ✓' : '未配置');
        return `
            <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--bg-primary);border-radius:8px;">
                <span style="font-size:13px;color:var(--text-secondary);">${item.label}</span>
                <span style="font-size:13px;">${escapeHtml(display)}</span>
            </div>
        `;
    }).join('');
}

function renderDefaultsForm() {
    if ($('defaultApiHost')) $('defaultApiHost').value = state.serverDefaults.apiHost || '';
    if ($('defaultModel')) $('defaultModel').value = state.serverDefaults.model || '';
    if ($('defaultTemperature')) $('defaultTemperature').value = state.serverDefaults.temperature ?? 0.7;
    if ($('defaultTopP')) $('defaultTopP').value = state.serverDefaults.topP ?? 0.65;
    if ($('defaultMaxOutputTokens')) $('defaultMaxOutputTokens').value = state.serverDefaults.maxOutputTokens ?? 50000;
    if ($('defaultContextRounds')) $('defaultContextRounds').value = state.serverDefaults.contextRounds ?? 100;
    if ($('defaultSystemPrompt')) $('defaultSystemPrompt').value = state.serverDefaults.systemPrompt || '';
    if ($('defaultBatchSystemPrompt')) $('defaultBatchSystemPrompt').value = state.serverDefaults.batchSystemPrompt || '';
    if ($('defaultBatchUserPromptTemplate')) $('defaultBatchUserPromptTemplate').value = state.serverDefaults.batchUserPromptTemplate || '';
    if ($('globalBatchSize')) $('globalBatchSize').value = state.defaultBatchSize;
    if ($('globalDelayMin')) $('globalDelayMin').value = state.batchDelayMin;
    if ($('globalDelayMax')) $('globalDelayMax').value = state.batchDelayMax;
}

async function saveDefaults() {
    const payload = {
        apiHost: $('defaultApiHost')?.value || '',
        model: $('defaultModel')?.value || '',
        temperature: $('defaultTemperature')?.value || '',
        topP: $('defaultTopP')?.value || '',
        maxOutputTokens: $('defaultMaxOutputTokens')?.value || '',
        contextRounds: $('defaultContextRounds')?.value || '',
        systemPrompt: $('defaultSystemPrompt')?.value || '',
        batchSystemPrompt: $('defaultBatchSystemPrompt')?.value || '',
        batchUserPromptTemplate: $('defaultBatchUserPromptTemplate')?.value || '',
    };

    const data = await api('/api/settings/update', {
        method: 'POST',
        body: JSON.stringify(payload)
    });

    if (data.success) {
        state.serverDefaults = data.settings || {};
        showToast('默认配置已保存', 'success');
        renderServerConfigStatus();
    } else {
        showToast('保存失败: ' + (data.error || ''), 'error');
    }
}

// ==================== 线程设置 ====================
async function loadThreadSettings() {
    const data = await api('/api/status');
    if (data.success) {
        state.threadSettings.maxConcurrent = data.maxConcurrent || 10;
        state.threadSettings.threadPoolSize = data.threadPoolSize || 10;
        if ($('maxConcurrentInput')) $('maxConcurrentInput').value = state.threadSettings.maxConcurrent;
        if ($('threadPoolSizeInput')) $('threadPoolSizeInput').value = state.threadSettings.threadPoolSize;
        if ($('globalBatchSize')) $('globalBatchSize').value = data.batchSize || state.defaultBatchSize;
        if ($('globalDelayMin')) $('globalDelayMin').value = data.batchDelayMin || state.batchDelayMin;
        if ($('globalDelayMax')) $('globalDelayMax').value = data.batchDelayMax || state.batchDelayMax;
    }
}

async function saveThreadSettings() {
    const maxConcurrent = clampInt($('maxConcurrentInput')?.value || 10, 10, 1, 50);
    const threadPoolSize = clampInt($('threadPoolSizeInput')?.value || 10, 10, 1, 100);
    const delayMin = clampInt($('globalDelayMin')?.value || state.batchDelayMin, state.batchDelayMin, 0, 3600);
    const delayMax = clampInt($('globalDelayMax')?.value || state.batchDelayMax, state.batchDelayMax, delayMin, 3600);

    const r1 = await api('/api/set-concurrent', {
        method: 'POST',
        body: JSON.stringify({ maxConcurrent })
    });

    const r2 = await api('/api/set-thread-pool', {
        method: 'POST',
        body: JSON.stringify({ threadPoolSize })
    });

    const r3 = await api('/api/set-batch-delay', {
        method: 'POST',
        body: JSON.stringify({ delayMin, delayMax })
    });

    if (r1.success && r2.success && r3.success) {
        state.threadSettings.maxConcurrent = maxConcurrent;
        state.threadSettings.threadPoolSize = threadPoolSize;
        state.batchDelayMin = delayMin;
        state.batchDelayMax = delayMax;
        showToast('线程与批处理设置已保存', 'success');
    } else {
        showToast('设置保存失败', 'error');
    }
}

// ==================== 配置管理 ====================
async function refreshConfigs() {
    const data = await api('/api/config/list?userId=default');
    if (data.success) {
        state.configs = data.configs || [];
        renderConfigList();
        renderBatchFiles();
        renderConversationConfigOptions();
    }
}

function renderConfigList() {
    const c = $('configList');
    if (!c) return;

    if (state.configs.length === 0) {
        c.innerHTML = `<div style="font-size:13px;color:var(--text-muted);">暂无配置</div>`;
        return;
    }

    c.innerHTML = state.configs.map(cfg => `
        <div class="task-item">
            <div>
                <div style="font-weight:600;">${escapeHtml(cfg.name || '未命名配置')}</div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
                    model: ${escapeHtml(cfg.model || '')}
                    ${cfg.hasApiKey ? ' · API Key ✓' : ''}
                    ${cfg.hasHfToken ? ' · HF Token ✓' : ''}
                </div>
            </div>
            <div style="display:flex;gap:8px;">
                <button class="btn btn-sm btn-secondary" onclick="editConfig('${cfg.id}')">编辑</button>
                <button class="btn btn-sm btn-danger" onclick="deleteConfig('${cfg.id}')">删除</button>
            </div>
        </div>
    `).join('');
}

function clearConfigForm() {
    state.editingConfigId = null;
    if ($('configName')) $('configName').value = '';
    if ($('configSystemPrompt')) $('configSystemPrompt').value = '';
    if ($('configBatchSystemPrompt')) $('configBatchSystemPrompt').value = '';
    if ($('configBatchUserPromptTemplate')) $('configBatchUserPromptTemplate').value = '';
    if ($('configBatchSize')) $('configBatchSize').value = '';
    if ($('configModel')) $('configModel').value = '';
    if ($('configTemperature')) $('configTemperature').value = '';
    if ($('configTopP')) $('configTopP').value = '';
    if ($('configContextRounds')) $('configContextRounds').value = '';
    if ($('configApiHost')) $('configApiHost').value = '';
    if ($('configApiKey')) $('configApiKey').value = '';
    if ($('configMaxOutputTokens')) $('configMaxOutputTokens').value = '';
    if ($('configHfToken')) $('configHfToken').value = '';
    if ($('configHfDataset')) $('configHfDataset').value = '';
}

function editConfig(configId) {
    const cfg = state.configs.find(c => c.id === configId);
    if (!cfg) return;
    state.editingConfigId = configId;

    if ($('configName')) $('configName').value = cfg.name || '';
    if ($('configSystemPrompt')) $('configSystemPrompt').value = cfg.systemPrompt || '';
    if ($('configBatchSystemPrompt')) $('configBatchSystemPrompt').value = cfg.batchSystemPrompt || '';
    if ($('configBatchUserPromptTemplate')) $('configBatchUserPromptTemplate').value = cfg.batchUserPromptTemplate || '';
    if ($('configBatchSize')) $('configBatchSize').value = cfg.batchSize || '';
    if ($('configModel')) $('configModel').value = cfg.model || '';
    if ($('configTemperature')) $('configTemperature').value = cfg.temperature || '';
    if ($('configTopP')) $('configTopP').value = cfg.topP || '';
    if ($('configContextRounds')) $('configContextRounds').value = cfg.contextRounds || '';
    if ($('configApiHost')) $('configApiHost').value = cfg.apiHost || '';
    if ($('configApiKey')) $('configApiKey').value = '';
    if ($('configMaxOutputTokens')) $('configMaxOutputTokens').value = cfg.maxOutputTokens || '';
    if ($('configHfToken')) $('configHfToken').value = '';
    if ($('configHfDataset')) $('configHfDataset').value = cfg.hfDataset || '';

    showToast('已加载配置到编辑区', 'info');
}

async function saveConfig() {
    const payload = {
        userId: 'default',
        id: state.editingConfigId || '',
        name: $('configName')?.value || '未命名配置',
        systemPrompt: $('configSystemPrompt')?.value || '',
        batchSystemPrompt: $('configBatchSystemPrompt')?.value || '',
        batchUserPromptTemplate: $('configBatchUserPromptTemplate')?.value || '',
        batchSize: $('configBatchSize')?.value || '',
        model: $('configModel')?.value || '',
        temperature: $('configTemperature')?.value || '',
        topP: $('configTopP')?.value || '',
        contextRounds: $('configContextRounds')?.value || '',
        apiHost: $('configApiHost')?.value || '',
        apiKey: $('configApiKey')?.value || '',
        maxOutputTokens: $('configMaxOutputTokens')?.value || '',
        hfToken: $('configHfToken')?.value || '',
        hfDataset: $('configHfDataset')?.value || '',
    };

    const data = await api('/api/config/save', {
        method: 'POST',
        body: JSON.stringify(payload)
    });

    if (data.success) {
        showToast('配置保存成功', 'success');
        clearConfigForm();
        refreshConfigs();
    } else {
        showToast('保存配置失败: ' + (data.error || ''), 'error');
    }
}

async function deleteConfig(configId) {
    const data = await api('/api/config/delete', {
        method: 'POST',
        body: JSON.stringify({ userId: 'default', id: configId })
    });

    if (data.success) {
        showToast('配置已删除', 'success');
        refreshConfigs();
    } else {
        showToast('删除失败: ' + (data.error || ''), 'error');
    }
}

// ==================== 对话 ====================
async function loadConversations() {
    const data = await api('/api/conversations?userId=default');
    if (data.success) {
        state.conversations = Object.values(data.conversations || {});
        renderConversationList();
        if (!state.currentConvId && state.conversations.length > 0) {
            state.currentConvId = state.conversations[0].id;
            renderCurrentConversation();
        }
    }
}

function renderConversationList() {
    const c = $('conversationList');
    if (!c) return;

    c.innerHTML = state.conversations.map(conv => `
        <div class="task-item" style="cursor:pointer;${state.currentConvId === conv.id ? 'border-color:var(--primary);' : ''}" onclick="selectConversation('${conv.id}')">
            <div style="flex:1;min-width:0;">
                <div style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(conv.title || '新对话')}</div>
            </div>
            <button class="btn btn-sm btn-danger" onclick="event.stopPropagation();deleteConversation('${conv.id}')">删除</button>
        </div>
    `).join('');
}

function renderConversationConfigOptions() {
    const select = $('chatConfigSelect');
    if (!select) return;

    select.innerHTML = `
        <option value="">默认配置</option>
        ${state.configs.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join('')}
    `;
}

function selectConversation(convId) {
    state.currentConvId = convId;
    renderConversationList();
    renderCurrentConversation();
}

function renderCurrentConversation() {
    const wrap = $('chatMessages');
    if (!wrap) return;

    const conv = state.conversations.find(c => c.id === state.currentConvId);
    if (!conv) {
        wrap.innerHTML = `<div style="color:var(--text-muted);font-size:14px;">暂无对话</div>`;
        return;
    }

    const msgs = conv.messages || [];
    wrap.innerHTML = msgs.map(msg => `
        <div class="chat-message ${msg.role}">
            <div class="chat-avatar">${msg.role === 'user' ? '我' : 'AI'}</div>
            <div class="chat-bubble">${escapeHtml(msg.content || '')}</div>
        </div>
    `).join('');

    wrap.scrollTop = wrap.scrollHeight;
}

async function createConversation() {
    const data = await api('/api/conversation/create', {
        method: 'POST',
        body: JSON.stringify({
            userId: 'default',
            title: '新对话',
            configId: $('chatConfigSelect')?.value || ''
        })
    });

    if (data.success) {
        await loadConversations();
        state.currentConvId = data.conversation.id;
        renderConversationList();
        renderCurrentConversation();
    }
}

async function deleteConversation(convId) {
    const data = await api('/api/conversation/delete', {
        method: 'POST',
        body: JSON.stringify({ userId: 'default', id: convId })
    });

    if (data.success) {
        if (state.currentConvId === convId) state.currentConvId = null;
        await loadConversations();
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
    }

    state.isSending = true;
    input.value = '';

    const data = await api('/api/chat', {
        method: 'POST',
        body: JSON.stringify({
            userId: 'default',
            conversationId: state.currentConvId,
            message,
            configId: $('chatConfigSelect')?.value || ''
        })
    });

    state.isSending = false;

    if (data.success) {
        await loadConversations();
        renderCurrentConversation();
    } else {
        showToast('发送失败: ' + (data.error || ''), 'error');
    }
}

// ==================== 章节解析（本地） ====================

const SPECIAL_CHAPTER_TITLES = new Set([
    '序章', '序', '楔子', '引子', '前言', '正文',
    '终章', '尾声', '后记', '番外', '番外篇', '完结感言'
]);

const STRONG_CHAPTER_PATTERNS = [
    /^第\s*[零一二三四五六七八九十百千万两〇\d]+\s*[章节回卷集部篇册]\s*(?:[：:·\-—.．、]\s*.*)?$/i,
    /^第\s*[零一二三四五六七八九十百千万两〇\d]+\s*[章节回卷集部篇册]\s+.*$/i,
    /^chapter\s*\d+\s*(?:[:：.\-—]\s*.*)?$/i,
    /^chapter\s*[ivxlcdm]+\s*(?:[:：.\-—]\s*.*)?$/i,
];

const WEAK_CHAPTER_PATTERNS = [
    /^\d+\s*[、.．\-—]\s*.+$/i,
    /^\d+\s+.+$/i,
];

function normalizeChapterLine(line) {
    return (line || '')
        .replace(/\uFEFF/g, '')
        .replace(/\u3000/g, ' ')
        .trim()
        .replace(/\s+/g, ' ');
}

function isSpecialChapterTitle(line) {
    return SPECIAL_CHAPTER_TITLES.has(normalizeChapterLine(line));
}

function isStrongChapterTitle(line) {
    const normalized = normalizeChapterLine(line);
    if (!normalized) return false;
    if (isSpecialChapterTitle(normalized)) return true;
    if (normalized.length > 80) return false;
    return STRONG_CHAPTER_PATTERNS.some(pattern => pattern.test(normalized));
}

function isWeakChapterTitle(line) {
    const normalized = normalizeChapterLine(line);
    if (!normalized) return false;
    if (normalized.length > 60) return false;
    return WEAK_CHAPTER_PATTERNS.some(pattern => pattern.test(normalized));
}

function collectFollowingTextLength(lines, startIndex, maxLookahead = 8) {
    let totalLen = 0;
    for (let i = startIndex + 1; i < Math.min(lines.length, startIndex + 1 + maxLookahead); i++) {
        const text = normalizeChapterLine(lines[i]);
        if (!text) continue;
        if (isStrongChapterTitle(text)) break;
        totalLen += text.length;
    }
    return totalLen;
}

function countChapterSignalLines(lines) {
    let strongCount = 0;
    let weakCount = 0;
    for (const line of lines) {
        const normalized = normalizeChapterLine(line);
        if (!normalized) continue;
        if (isStrongChapterTitle(normalized)) strongCount++;
        else if (isWeakChapterTitle(normalized)) weakCount++;
    }
    return { strongCount, weakCount };
}

function isChapterTitleWithContext(lines, index, strongCount, weakCount) {
    const normalized = normalizeChapterLine(lines[index]);
    if (!normalized) return false;

    if (isStrongChapterTitle(normalized)) return true;
    if (!isWeakChapterTitle(normalized)) return false;

    const followingLen = collectFollowingTextLength(lines, index, 8);
    if (followingLen >= 12) return true;
    if (weakCount >= 3) return true;
    if (/^\d+\s*[、.．\-—]\s*.+$/i.test(normalized)) return true;
    if (strongCount + weakCount >= 2 && /^\d+\s+.+$/i.test(normalized)) return true;

    return false;
}

/**
 * 本地解析章节
 * 当前版本在主线程中解析，规则已与后端尽量保持一致。
 */
function parseChaptersLocal(content) {
    const safeContent = normalizeText(content);
    if (!safeContent) return [];

    const lines = safeContent.split('\n');
    const { strongCount, weakCount } = countChapterSignalLines(lines);

    const chapters = [];
    let currentChapter = null;
    let currentLines = [];

    for (let i = 0; i < lines.length; i++) {
        const rawLine = lines[i];
        const stripped = normalizeChapterLine(rawLine);

        if (!stripped) {
            if (currentChapter !== null) currentLines.push(rawLine);
            continue;
        }

        if (isChapterTitleWithContext(lines, i, strongCount, weakCount)) {
            if (currentChapter !== null) {
                const chapterText = currentLines.join('\n').trim();
                if (chapterText) {
                    chapters.push({
                        title: currentChapter,
                        content: chapterText,
                        index: chapters.length + 1
                    });
                }
            }
            currentChapter = stripped;
            currentLines = [];
        } else {
            if (currentChapter !== null) currentLines.push(rawLine);
        }
    }

    if (currentChapter !== null) {
        const chapterText = currentLines.join('\n').trim();
        if (chapterText) {
            chapters.push({
                title: currentChapter,
                content: chapterText,
                index: chapters.length + 1
            });
        }
    }

    return chapters;
}

// ==================== 批处理文件 ====================

function buildLocalBatchFileFingerprint(fileName, chapters) {
    return `${fileName}::${chapters.length}`;
}

/**
 * 批处理文件解析
 * 当前版本使用主线程 FileReader + 本地章节解析。
 */
async function handleBatchFiles(files) {
    const txtFiles = Array.from(files).filter(f => f.name.toLowerCase().endsWith('.txt'));
    if (txtFiles.length === 0) {
        showToast('请选择.txt文件', 'warning');
        return;
    }

    const progressContainer = document.createElement('div');
    progressContainer.id = 'batchProgress';
    progressContainer.innerHTML = `
        <div class="progress-overlay">
            <div class="progress-card">
                <div class="progress-title">正在解析文件...</div>
                <div class="progress-bar-container">
                    <div class="progress-bar" id="batchProgressBar"></div>
                </div>
                <div class="progress-text" id="batchProgressText">0/${txtFiles.length}</div>
                <div class="progress-files" id="batchProgressFiles"></div>
            </div>
        </div>
    `;
    document.body.appendChild(progressContainer);

    const progressBar = $('batchProgressBar');
    const progressText = $('batchProgressText');
    const progressFiles = $('batchProgressFiles');

    let completed = 0;

    const parsePromises = txtFiles.map((file) => {
        return new Promise((resolve) => {
            const reader = new FileReader();

            reader.onload = (e) => {
                const content = e.target.result || '';
                const chapters = parseChaptersLocal(content);
                completed++;

                const percent = Math.round((completed / txtFiles.length) * 100);
                progressBar.style.width = percent + '%';
                progressText.textContent = `${completed}/${txtFiles.length}`;
                progressFiles.innerHTML = `<div class="progress-file">✓ ${escapeHtml(file.name)}: ${chapters.length} 章</div>` + progressFiles.innerHTML;

                resolve({
                    fileName: file.name,
                    chapters,
                    success: chapters.length > 0
                });
            };

            reader.onerror = () => {
                completed++;
                progressBar.style.width = Math.round((completed / txtFiles.length) * 100) + '%';
                progressText.textContent = `${completed}/${txtFiles.length}`;
                progressFiles.innerHTML = `<div class="progress-file error">✗ ${escapeHtml(file.name)}: 读取失败</div>` + progressFiles.innerHTML;
                resolve({
                    fileName: file.name,
                    chapters: [],
                    success: false
                });
            };

            reader.readAsText(file);
        });
    });

    const allResults = await Promise.all(parsePromises);

    if (progressContainer.parentNode) {
        document.body.removeChild(progressContainer);
    }

    let successCount = 0;
    let duplicateCount = 0;

    allResults.forEach(result => {
        if (!result.success || result.chapters.length === 0) return;

        const localFingerprint = buildLocalBatchFileFingerprint(result.fileName, result.chapters);
        const exists = state.batchFiles.some(f => f.localFingerprint === localFingerprint);

        if (exists) {
            duplicateCount++;
            return;
        }

        const fileId = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
        state.batchFiles.push({
            fileId,
            localFingerprint,
            fileName: result.fileName,
            chapters: result.chapters,
            configId: '',
            batchSize: state.defaultBatchSize,
            startChapter: 1,
            endChapter: result.chapters.length
        });
        successCount++;
    });

    renderBatchFiles();

    let msg = `成功解析 ${successCount}/${txtFiles.length} 个文件`;
    if (duplicateCount > 0) msg += `，跳过本地重复 ${duplicateCount} 个`;
    showToast(msg, successCount > 0 ? 'success' : 'warning');
}

function renderBatchFiles() {
    const container = $('batchFileList');
    const submitArea = $('batchSubmitArea');
    if (!container || !submitArea) return;

    if (state.batchFiles.length === 0) {
        container.innerHTML = '';
        submitArea.classList.add('hidden');
        return;
    }

    submitArea.classList.remove('hidden');

    const totalChapters = state.batchFiles.reduce((sum, f) => sum + (f.chapters?.length || 0), 0);

    container.innerHTML = state.batchFiles.map(f => {
        const total = f.chapters.length;
        const start = clampInt(f.startChapter || 1, 1, 1, Math.max(1, total));
        const end = clampInt(f.endChapter || total, total, 1, Math.max(1, total));
        const rangeCount = Math.max(0, Math.abs(end - start) + 1);

        return `
        <div class="batch-file-card" data-file-id="${f.fileId}">
            <div class="batch-file-header">
                <div>
                    <div class="batch-file-name">📄 ${escapeHtml(f.fileName)}</div>
                    <div class="batch-file-meta">
                        总章节：${total} · 当前范围：${start} ~ ${end}（${rangeCount}章）
                    </div>
                </div>
                <button class="btn btn-sm btn-danger" onclick="removeBatchFile('${f.fileId}')">✕ 移除</button>
            </div>

            <div class="batch-file-config">
                <label>使用配置:</label>
                <select class="form-input" onchange="updateBatchFileConfig('${f.fileId}', this.value)">
                    <option value="">默认配置</option>
                    ${state.configs.map(c => `<option value="${c.id}" ${f.configId === c.id ? 'selected' : ''}>${escapeHtml(c.name)}</option>`).join('')}
                </select>
            </div>

            <div class="batch-file-config">
                <label>批次大小:</label>
                <input type="number"
                       class="form-input"
                       value="${f.batchSize || state.defaultBatchSize}"
                       min="1"
                       max="100"
                       onchange="updateBatchFileSize('${f.fileId}', this.value)"
                       style="width:90px;">
                <span style="font-size:12px;color:var(--text-muted);">章/批次</span>
            </div>

            <div class="batch-file-config">
                <label>起始章节:</label>
                <input type="number"
                       class="form-input"
                       value="${start}"
                       min="1"
                       max="${Math.max(1, total)}"
                       onchange="updateBatchFileStartChapter('${f.fileId}', this.value)"
                       style="width:100px;">
                <span style="font-size:12px;color:var(--text-muted);">从第几章开始</span>
            </div>

            <div class="batch-file-config">
                <label>结束章节:</label>
                <input type="number"
                       class="form-input"
                       value="${end}"
                       min="1"
                       max="${Math.max(1, total)}"
                       onchange="updateBatchFileEndChapter('${f.fileId}', this.value)"
                       style="width:100px;">
                <span style="font-size:12px;color:var(--text-muted);">处理到第几章</span>
            </div>

            <div style="margin-top:8px;font-size:12px;color:var(--text-muted);">
                当前配置：${escapeHtml(getConfigNameById(f.configId))}
            </div>
        </div>
        `;
    }).join('') + `
        <p style="font-size:13px;color:var(--text-muted);margin-top:8px;">
            共 ${state.batchFiles.length} 个文件，${totalChapters} 个解析章节
        </p>
    `;
}

function removeBatchFile(fileId) {
    state.batchFiles = state.batchFiles.filter(f => f.fileId !== fileId);
    renderBatchFiles();
}

function updateBatchFileConfig(fileId, configId) {
    const f = state.batchFiles.find(x => x.fileId === fileId);
    if (f) {
        f.configId = configId;
        renderBatchFiles();
    }
}

function updateBatchFileSize(fileId, batchSize) {
    const f = state.batchFiles.find(x => x.fileId === fileId);
    if (f) {
        f.batchSize = clampInt(batchSize, state.defaultBatchSize, 1, 100);
        renderBatchFiles();
    }
}

function updateBatchFileStartChapter(fileId, startChapter) {
    const f = state.batchFiles.find(x => x.fileId === fileId);
    if (!f) return;

    const total = f.chapters.length;
    f.startChapter = clampInt(startChapter, 1, 1, total);
    if (f.startChapter > f.endChapter) {
        f.endChapter = f.startChapter;
    }
    renderBatchFiles();
}

function updateBatchFileEndChapter(fileId, endChapter) {
    const f = state.batchFiles.find(x => x.fileId === fileId);
    if (!f) return;

    const total = f.chapters.length;
    f.endChapter = clampInt(endChapter, total, 1, total);
    if (f.endChapter < f.startChapter) {
        f.startChapter = f.endChapter;
    }
    renderBatchFiles();
}

async function submitBatch() {
    if (state.batchFiles.length === 0) return;

    showLoading('正在提交批处理任务...');

    const globalBatchSize = clampInt($('globalBatchSize')?.value || state.defaultBatchSize, state.defaultBatchSize, 1, 100);
    const delayMin = clampInt($('globalDelayMin')?.value || state.batchDelayMin, state.batchDelayMin, 0, 3600);
    const delayMax = clampInt($('globalDelayMax')?.value || state.batchDelayMax, state.batchDelayMax, delayMin, 3600);

    const files = state.batchFiles.map(f => ({
        fileName: f.fileName,
        chapters: f.chapters,
        configId: f.configId,
        configName: getConfigNameById(f.configId),
        batchSize: clampInt(f.batchSize || globalBatchSize, globalBatchSize, 1, 100),
        startChapter: clampInt(f.startChapter || 1, 1, 1, Math.max(1, f.chapters.length)),
        endChapter: clampInt(f.endChapter || f.chapters.length, f.chapters.length, 1, Math.max(1, f.chapters.length))
    }));

    const data = await api('/api/batch', {
        method: 'POST',
        body: JSON.stringify({
            userId: 'default',
            files,
            batchSize: globalBatchSize,
            delayMin,
            delayMax
        }),
    });

    hideLoading();

    if (data.success) {
        let msg = `任务已提交：${data.totalChapters} 个章节`;
        if (data.queuedFiles !== undefined) {
            msg += `，入队 ${data.queuedFiles} 本`;
        }
        if (data.duplicateFiles && data.duplicateFiles.length > 0) {
            msg += `，重复跳过 ${data.duplicateFiles.length} 本`;
        }

        showToast(msg, 'success');

        if (data.duplicateFiles && data.duplicateFiles.length > 0) {
            const duplicateNames = data.duplicateFiles.map(x => x.fileName).join('、');
            showToast(`以下文件已在队列或处理中，已跳过：${duplicateNames}`, 'warning');
        }

        state.batchFiles = [];
        renderBatchFiles();
        switchPage('tasks');
        refreshTasks();
    } else {
        if (data.duplicates && data.duplicates.length > 0) {
            const names = data.duplicates.map(x => x.fileName).join('、');
            showToast(`重复提交，未进入队列：${names}`, 'warning');
        } else {
            showToast('提交失败: ' + (data.error || ''), 'error');
        }
    }
}

// ==================== 任务 ====================
async function refreshTasks() {
    const data = await api('/api/tasks');
    if (data.success) {
        state.tasks = data.tasks || {};
        renderTaskList();
    }
}

function renderTaskList() {
    const container = $('taskList');
    if (!container) return;

    const tasks = Object.values(state.tasks || {}).sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    if (tasks.length === 0) {
        container.innerHTML = `<div style="font-size:13px;color:var(--text-muted);">暂无任务</div>`;
        return;
    }

    container.innerHTML = tasks.map(task => `
        <div class="task-item">
            <div style="flex:1;min-width:0;">
                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                    <span style="font-weight:600;">任务 ${escapeHtml((task.task_id || '').slice(0, 8))}</span>
                    <span style="font-size:12px;padding:2px 8px;border-radius:999px;background:var(--bg-primary);">${statusText(task.status)}</span>
                </div>
                <div style="font-size:13px;color:var(--text-secondary);margin-top:4px;">
                    ${escapeHtml(task.progress || '')} · ${escapeHtml(task.message || '')}
                </div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
                    文件：${task.files?.length || 0}
                    · 排队：${task.queued_files || 0}
                    · 处理中：${task.processing_files || 0}
                    · 已完成：${task.completed_files || 0}
                </div>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
                <button class="btn btn-sm btn-secondary" onclick="viewTask('${task.task_id}')">查看</button>
                <button class="btn btn-sm btn-secondary" onclick="downloadTask('${task.task_id}')">导出</button>
                ${task.status === 'pending' || task.status === 'processing'
                    ? `<button class="btn btn-sm btn-danger" onclick="cancelTask('${task.task_id}')">取消</button>`
                    : ''
                }
                <button class="btn btn-sm btn-danger" onclick="deleteTask('${task.task_id}')">删除</button>
            </div>
        </div>
    `).join('');
}

async function viewTask(taskId) {
    const data = await api(`/api/task/${taskId}`);
    if (!data.success) {
        showToast('获取任务详情失败', 'error');
        return;
    }

    state.viewingTaskId = taskId;
    const task = data.task;
    const modal = $('taskDetailModal');
    const body = $('taskDetailBody');
    if (!modal || !body) return;

    body.innerHTML = `
        <div style="margin-bottom:12px;">
            <div><strong>任务ID：</strong>${escapeHtml(task.task_id || '')}</div>
            <div><strong>状态：</strong>${escapeHtml(statusText(task.status || ''))}</div>
            <div><strong>进度：</strong>${escapeHtml(task.progress || '')}</div>
            <div><strong>说明：</strong>${escapeHtml(task.message || '')}</div>
        </div>
        ${(task.files || []).map((f, idx) => `
            <div class="card" style="margin-bottom:12px;">
                <div class="card-title">
                    <span>${escapeHtml(f.file_name || '未命名')}</span>
                    <span style="font-size:12px;color:var(--text-muted);">${escapeHtml(statusText(f.status || 'queued'))}</span>
                </div>
                <div style="font-size:13px;color:var(--text-secondary);margin-bottom:8px;">
                    配置：${escapeHtml(f.config_name || '默认配置')}
                    · 批次：${escapeHtml(String(f.batch_size || ''))}
                    · 范围：${escapeHtml(String(f.start_chapter || 1))} ~ ${escapeHtml(String(f.end_chapter || f.total || 0))}
                    · 完成：${escapeHtml(String(f.completed || 0))}/${escapeHtml(String(f.total || 0))}
                    · 失败：${escapeHtml(String(f.failed || 0))}
                </div>
                <div style="display:flex;gap:8px;margin-bottom:8px;">
                    <button class="btn btn-sm btn-secondary" onclick="downloadSingleNovel('${task.task_id}', ${idx})">导出本书</button>
                </div>
                <div style="max-height:240px;overflow:auto;background:var(--bg-primary);border-radius:8px;padding:10px;">
                    ${(f.results || []).map(r => `
                        <div style="padding:8px 0;border-bottom:1px solid var(--border);">
                            <div style="font-size:13px;font-weight:600;">
                                批次 ${escapeHtml(String(r.batch || '?'))}
                                · 第${escapeHtml(String(r.chapter_start || '?'))}-${escapeHtml(String(r.chapter_end || '?'))}章
                                · ${r.success ? '成功' : '失败'}
                            </div>
                            <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
                                ${escapeHtml(r.preview || r.error || '')}
                            </div>
                        </div>
                    `).join('') || '<div style="font-size:12px;color:var(--text-muted);">暂无结果</div>'}
                </div>
            </div>
        `).join('')}
    `;

    modal.classList.remove('hidden');
}

function closeTaskDetail() {
    const modal = $('taskDetailModal');
    if (modal) modal.classList.add('hidden');
}

async function cancelTask(taskId) {
    const data = await api('/api/batch/cancel', {
        method: 'POST',
        body: JSON.stringify({ taskId })
    });

    if (data.success) {
        showToast('任务已取消', 'success');
        refreshTasks();
    } else {
        showToast('取消失败: ' + (data.error || ''), 'error');
    }
}

async function deleteTask(taskId) {
    const data = await api('/api/task/delete', {
        method: 'POST',
        body: JSON.stringify({ taskId })
    });

    if (data.success) {
        showToast('任务已删除', 'success');
        refreshTasks();
    } else {
        showToast('删除失败: ' + (data.error || ''), 'error');
    }
}

async function downloadTask(taskId) {
    const data = await api(`/api/task/${taskId}/download`);
    if (data.success) {
        downloadTextFile(data.filename || 'task.txt', data.content || '');
    } else {
        showToast('导出失败', 'error');
    }
}

async function downloadSingleNovel(taskId, fileIdx) {
    const data = await api(`/api/task/${taskId}/download/${fileIdx}`);
    if (data.success) {
        downloadTextFile(data.filename || 'novel.txt', data.content || '');
    } else {
        showToast('导出失败', 'error');
    }
}

function downloadTextFile(filename, content) {
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'download.txt';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
}

// ==================== 云端文件 ====================
async function refreshCloudFiles() {
    const hfDataset = state.serverDefaults.hfDataset || '';
    const data = await api(`/api/hf-files?hfToken=&hfDataset=${encodeURIComponent(hfDataset)}`);
    if (data.success) {
        state.cloudFiles = data.files || [];
        renderCloudFiles();
    }
}

function renderCloudFiles() {
    const container = $('cloudFileList');
    if (!container) return;

    if (!state.cloudFiles.length) {
        container.innerHTML = `<div style="font-size:13px;color:var(--text-muted);">暂无云端文件</div>`;
        return;
    }

    container.innerHTML = state.cloudFiles.map(f => `
        <div class="task-item">
            <div>${escapeHtml(f.name || f.path || '')}</div>
            <div style="display:flex;gap:8px;">
                <button class="btn btn-sm btn-secondary" onclick="downloadCloudFile('${encodeURIComponent(f.path || '')}')">下载</button>
            </div>
        </div>
    `).join('');
}

async function downloadCloudFile(encodedPath) {
    const filename = decodeURIComponent(encodedPath);
    const data = await api('/api/hf-download', {
        method: 'POST',
        body: JSON.stringify({
            hfToken: '',
            hfDataset: state.serverDefaults.hfDataset || '',
            filename
        })
    });

    if (data.success) {
        downloadTextFile(data.filename || 'cloud.txt', data.content || '');
    } else {
        showToast('云端下载失败: ' + (data.error || ''), 'error');
    }
}

// ==================== 初始化 ====================
function bindEvents() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => switchPage(item.dataset.page));
    });

    if ($('btnCreateConversation')) $('btnCreateConversation').addEventListener('click', createConversation);
    if ($('btnSendChat')) $('btnSendChat').addEventListener('click', sendChat);
    if ($('chatInput')) {
        $('chatInput').addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendChat();
            }
        });
    }

    if ($('batchFileInput')) {
        $('batchFileInput').addEventListener('change', (e) => {
            handleBatchFiles(e.target.files || []);
            e.target.value = '';
        });
    }

    if ($('batchUploadArea')) {
        $('batchUploadArea').addEventListener('click', () => $('batchFileInput')?.click());
        $('batchUploadArea').addEventListener('dragover', (e) => {
            e.preventDefault();
        });
        $('batchUploadArea').addEventListener('drop', (e) => {
            e.preventDefault();
            handleBatchFiles(e.dataTransfer.files || []);
        });
    }

    if ($('btnSubmitBatch')) $('btnSubmitBatch').addEventListener('click', submitBatch);
    if ($('btnSaveDefaults')) $('btnSaveDefaults').addEventListener('click', saveDefaults);
    if ($('btnSaveThreadSettings')) $('btnSaveThreadSettings').addEventListener('click', saveThreadSettings);
    if ($('btnSaveConfig')) $('btnSaveConfig').addEventListener('click', saveConfig);
    if ($('btnClearConfigForm')) $('btnClearConfigForm').addEventListener('click', clearConfigForm);
    if ($('btnCloseTaskDetail')) $('btnCloseTaskDetail').addEventListener('click', closeTaskDetail);
}

function startTaskPolling() {
    if (state.taskPollTimer) clearInterval(state.taskPollTimer);
    state.taskPollTimer = setInterval(() => {
        refreshTasks();
    }, 3000);
}

async function init() {
    bindEvents();
    await loadServerSettings();
    renderServerConfigStatus();
    renderDefaultsForm();
    await refreshConfigs();
    await loadConversations();
    await loadThreadSettings();
    await refreshTasks();
    startTaskPolling();
    switchPage('chat');
}

window.switchPage = switchPage;
window.removeBatchFile = removeBatchFile;
window.updateBatchFileConfig = updateBatchFileConfig;
window.updateBatchFileSize = updateBatchFileSize;
window.updateBatchFileStartChapter = updateBatchFileStartChapter;
window.updateBatchFileEndChapter = updateBatchFileEndChapter;
window.submitBatch = submitBatch;
window.editConfig = editConfig;
window.deleteConfig = deleteConfig;
window.selectConversation = selectConversation;
window.deleteConversation = deleteConversation;
window.viewTask = viewTask;
window.closeTaskDetail = closeTaskDetail;
window.cancelTask = cancelTask;
window.deleteTask = deleteTask;
window.downloadTask = downloadTask;
window.downloadSingleNovel = downloadSingleNovel;
window.downloadCloudFile = downloadCloudFile;

document.addEventListener('DOMContentLoaded', init);