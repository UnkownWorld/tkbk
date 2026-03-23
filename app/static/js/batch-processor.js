/**
 * 高性能批处理模块
 * - 使用Web Worker并行解析
 * - 分块传输大文件
 * - 实时进度显示
 */

class BatchProcessor {
    constructor() {
        this.workers = [];
        this.maxWorkers = navigator.hardwareConcurrency || 4;
        this.pendingTasks = new Map();
        this.taskIdCounter = 0;
        
        // 创建Worker池
        this.initWorkerPool();
    }
    
    /**
     * 初始化Worker池
     */
    initWorkerPool() {
        const workerCode = `
const CHAPTER_PATTERNS = [
    /^[第]\\s*([零一二三四五六七八九十百千万\\d]+)\\s*[章节回卷集部篇]\\s*[：:．.\\s]*\\S+/,
    /^Chapter\\s*\\d+/i,
    /^CHAPTER\\s*\\d+/,
    /^\\d+[、.．]\\s*\\S+/,
];

function parseChapters(content) {
    const chapters = [];
    const lines = content.split('\\n');
    let currentChapter = null;
    let currentLines = [];
    let chapterIndex = 0;
    
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const stripped = line.trim();
        
        if (!stripped) {
            if (currentChapter !== null) currentLines.push(line);
            continue;
        }
        
        let isChapterTitle = false;
        for (const pattern of CHAPTER_PATTERNS) {
            if (pattern.test(stripped)) {
                isChapterTitle = true;
                break;
            }
        }
        
        if (isChapterTitle) {
            if (currentChapter !== null) {
                const chapterText = currentLines.join('\\n').trim();
                if (chapterText) {
                    chapterIndex++;
                    chapters.push({ title: currentChapter, content: chapterText, index: chapterIndex });
                }
            }
            currentChapter = stripped;
            currentLines = [];
        } else {
            if (currentChapter !== null) currentLines.push(line);
        }
    }
    
    if (currentChapter !== null) {
        const chapterText = currentLines.join('\\n').trim();
        if (chapterText) {
            chapterIndex++;
            chapters.push({ title: currentChapter, content: chapterText, index: chapterIndex });
        }
    }
    
    return chapters;
}

self.onmessage = function(e) {
    const { type, data, taskId } = e.data;
    
    if (type === 'parse') {
        try {
            const startTime = Date.now();
            const chapters = parseChapters(data.content);
            const elapsed = Date.now() - startTime;
            
            self.postMessage({
                type: 'result',
                taskId: taskId,
                fileName: data.fileName,
                success: true,
                chapters: chapters,
                elapsed: elapsed,
                fileSize: data.content.length
            });
        } catch (error) {
            self.postMessage({
                type: 'result',
                taskId: taskId,
                fileName: data.fileName,
                success: false,
                error: error.message
            });
        }
    }
};
`;
        
        const blob = new Blob([workerCode], { type: 'application/javascript' });
        const workerUrl = URL.createObjectURL(blob);
        
        for (let i = 0; i < this.maxWorkers; i++) {
            const worker = new Worker(workerUrl);
            worker.onmessage = (e) => this.handleWorkerMessage(e.data);
            this.workers.push({ worker, busy: false });
        }
    }
    
    /**
     * 处理Worker消息
     */
    handleWorkerMessage(data) {
        const { taskId, type } = data;
        
        if (type === 'result') {
            const task = this.pendingTasks.get(taskId);
            if (task) {
                // 释放Worker
                const workerInfo = this.workers.find(w => w.currentTaskId === taskId);
                if (workerInfo) {
                    workerInfo.busy = false;
                    workerInfo.currentTaskId = null;
                }
                
                // 调用回调
                task.callback(data);
                this.pendingTasks.delete(taskId);
                
                // 处理下一个待处理任务
                this.processNextTask();
            }
        }
    }
    
    /**
     * 处理下一个待处理任务
     */
    processNextTask() {
        const freeWorker = this.workers.find(w => !w.busy);
        if (!freeWorker) return;
        
        // 找到第一个待处理的任务
        for (const [taskId, task] of this.pendingTasks) {
            if (!task.started) {
                task.started = true;
                freeWorker.busy = true;
                freeWorker.currentTaskId = taskId;
                freeWorker.worker.postMessage({
                    type: 'parse',
                    data: task.data,
                    taskId: taskId
                });
                break;
            }
        }
    }
    
    /**
     * 解析单个文件
     * @param {File} file - 文件对象
     * @param {Function} callback - 回调函数
     * @returns {Promise}
     */
    parseFile(file, callback) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            
            reader.onload = (e) => {
                const content = e.target.result;
                const taskId = ++this.taskIdCounter;
                
                this.pendingTasks.set(taskId, {
                    data: { content, fileName: file.name },
                    callback: (result) => {
                        if (callback) callback(result);
                        if (result.success) {
                            resolve(result);
                        } else {
                            reject(new Error(result.error));
                        }
                    },
                    started: false
                });
                
                // 尝试立即处理
                this.processNextTask();
            };
            
            reader.onerror = () => reject(new Error('文件读取失败'));
            reader.readAsText(file);
        });
    }
    
    /**
     * 批量解析文件 - 并行处理
     * @param {FileList} files - 文件列表
     * @param {Function} onProgress - 进度回调
     * @param {Function} onFileComplete - 单文件完成回调
     * @returns {Promise<Array>}
     */
    async parseFiles(files, onProgress, onFileComplete) {
        const results = [];
        const total = files.length;
        let completed = 0;
        
        // 创建所有任务的Promise
        const tasks = Array.from(files).map((file, index) => {
            return this.parseFile(file, (result) => {
                completed++;
                if (onProgress) {
                    onProgress({
                        completed,
                        total,
                        fileName: file.name,
                        progress: Math.round((completed / total) * 100)
                    });
                }
                if (onFileComplete) {
                    onFileComplete(result);
                }
            }).catch(error => ({
                success: false,
                fileName: file.name,
                error: error.message
            }));
        });
        
        // 并行执行所有任务
        const allResults = await Promise.all(tasks);
        return allResults;
    }
    
    /**
     * 提交批处理任务 - 优化传输
     * @param {Object} payload - 任务数据
     * @param {Function} onProgress - 进度回调
     * @returns {Promise}
     */
    async submitBatch(payload, onProgress) {
        // 压缩数据（如果支持）
        const data = JSON.stringify(payload);
        
        // 分块传输大文件
        const CHUNK_SIZE = 1024 * 1024; // 1MB
        
        if (data.length > CHUNK_SIZE) {
            return this.submitBatchChunked(data, onProgress);
        } else {
            // 小文件直接传输
            const response = await fetch('/api/batch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: data
            });
            return response.json();
        }
    }
    
    /**
     * 分块传输大文件
     */
    async submitBatchChunked(data, onProgress) {
        const CHUNK_SIZE = 1024 * 1024; // 1MB
        const totalChunks = Math.ceil(data.length / CHUNK_SIZE);
        const taskId = Date.now().toString(36) + Math.random().toString(36).substr(2);
        
        // 发送分块
        for (let i = 0; i < totalChunks; i++) {
            const start = i * CHUNK_SIZE;
            const end = Math.min(start + CHUNK_SIZE, data.length);
            const chunk = data.slice(start, end);
            
            const response = await fetch('/api/batch/chunk', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    taskId,
                    chunkIndex: i,
                    totalChunks,
                    data: chunk,
                    isLast: i === totalChunks - 1
                })
            });
            
            if (onProgress) {
                onProgress({
                    uploaded: end,
                    total: data.length,
                    progress: Math.round((end / data.length) * 100)
                });
            }
            
            // 如果是最后一块，返回结果
            if (i === totalChunks - 1) {
                return response.json();
            }
        }
    }
    
    /**
     * 销毁Worker池
     */
    destroy() {
        this.workers.forEach(w => w.worker.terminate());
        this.workers = [];
    }
}

// 导出单例
window.batchProcessor = new BatchProcessor();
