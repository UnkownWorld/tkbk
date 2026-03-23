/**
 * 高性能章节解析器 - Web Worker版本
 * 使用正则表达式快速匹配章节标题
 */

// 章节标题匹配模式
const CHAPTER_PATTERNS = [
    /^[第]\s*([零一二三四五六七八九十百千万\d]+)\s*[章节回卷集部篇]\s*[：:．.\s]*\S+/,
    /^Chapter\s*\d+/i,
    /^CHAPTER\s*\d+/,
    /^\d+[、.．]\s*\S+/,
];

/**
 * 解析章节 - 高性能版本
 * @param {string} content - 文本内容
 * @returns {Array} 章节列表
 */
function parseChapters(content) {
    const chapters = [];
    const lines = content.split('\n');
    let currentChapter = null;
    let currentLines = [];
    let chapterIndex = 0;
    
    // 预编译正则
    const patterns = CHAPTER_PATTERNS.map(p => new RegExp(p.source, p.flags));
    
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const stripped = line.trim();
        
        // 跳过空行
        if (!stripped) {
            if (currentChapter !== null) {
                currentLines.push(line);
            }
            continue;
        }
        
        // 检查是否是章节标题
        let isChapterTitle = false;
        for (const pattern of patterns) {
            if (pattern.test(stripped)) {
                isChapterTitle = true;
                break;
            }
        }
        
        if (isChapterTitle) {
            // 保存上一章
            if (currentChapter !== null) {
                const chapterText = currentLines.join('\n').trim();
                if (chapterText) {
                    chapterIndex++;
                    chapters.push({
                        title: currentChapter,
                        content: chapterText,
                        index: chapterIndex
                    });
                }
            }
            currentChapter = stripped;
            currentLines = [];
        } else {
            if (currentChapter !== null) {
                currentLines.push(line);
            }
        }
    }
    
    // 保存最后一章
    if (currentChapter !== null) {
        const chapterText = currentLines.join('\n').trim();
        if (chapterText) {
            chapterIndex++;
            chapters.push({
                title: currentChapter,
                content: chapterText,
                index: chapterIndex
            });
        }
    }
    
    return chapters;
}

/**
 * Web Worker消息处理
 */
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
                elapsed: elapsed
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
