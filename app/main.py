import os
import json
import time
import uuid
import threading
import logging
import re
import random
import requests as http_requests
from concurrent.futures import ThreadPoolExecutor
from flask import request, jsonify, send_from_directory

from app.stores.task_store import task_store
from app.stores.cache_store import cache_store
from app.stores.conversation_store import conversation_store
from app.stores.conversation_config_store import conversation_config_store
from app.services.file_service import file_service
from app.services.app_settings_service import app_settings_service

logger = logging.getLogger(__name__)

# 全局配置
DEFAULT_THREAD_POOL_SIZE = 10
DEFAULT_BATCH_SIZE = 10  # 默认每批次10章
MAX_CONCURRENT_TASKS = 10
# 批次间延迟配置（秒）- 默认值，可通过API调整
BATCH_DELAY_MIN = 15  # 默认最小延迟15秒
BATCH_DELAY_MAX = 45  # 默认最大延迟45秒

task_semaphore = threading.Semaphore(MAX_CONCURRENT_TASKS)
executor = ThreadPoolExecutor(max_workers=DEFAULT_THREAD_POOL_SIZE)
_http_session = http_requests.Session()


# ==================== 通用工具 ====================

def _safe_int(value, default, min_value=None, max_value=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default

    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def _safe_float(value, default, min_value=None, max_value=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default

    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def _get_available_slots():
    return getattr(task_semaphore, "_value", 0)


def _extract_assistant_content(result: dict) -> str:
    try:
        return result.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception:
        return ""


# ==================== LLM 调用 ====================

def _call_llm_api(config: dict, messages: list, use_stream: bool = False):
    """
    调用LLM API

    Args:
        config: 配置字典
        messages: 消息列表
        use_stream: 是否使用流式响应

    Returns:
        (success, result) - 成功时result是响应JSON，失败时result是错误信息
    """
    api_host = (config.get("apiHost", "") or "").rstrip("/")
    api_key = config.get("apiKey", "")
    model = config.get("model", "gpt-5.4")
    temperature = _safe_float(config.get("temperature", 0.7), 0.7)
    top_p = _safe_float(config.get("topP", 0.65), 0.65)
    # max_tokens设置为1M (1000000)，除非用户明确指定较小值
    max_tokens = _safe_int(config.get("maxOutputTokens", 1000000), 1000000, min_value=1)
    if max_tokens <= 0:
        max_tokens = 1000000

    if not api_host:
        return False, "未配置 apiHost"

    url = f"{api_host}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stream": use_stream,  # 启用流式响应
    }

    # 批处理使用更长的超时时间（30分钟），聊天使用较短超时（10分钟）
    timeout = 1800 if use_stream else 600

    try:
        if use_stream:
            # 流式响应处理
            resp = _http_session.post(url, headers=headers, json=payload, timeout=timeout, stream=True)
            if resp.status_code != 200:
                return False, f"API错误: {resp.status_code} - {resp.text[:200]}"

            # 收集流式响应
            full_content = ""
            for line in resp.iter_lines():
                if line:
                    line = line.decode('utf-8', errors='ignore')
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            if chunk.get("choices") and chunk["choices"][0].get("delta", {}).get("content"):
                                full_content += chunk["choices"][0]["delta"]["content"]
                        except json.JSONDecodeError:
                            continue

            return True, {"choices": [{"message": {"content": full_content}}]}
        else:
            # 非流式响应（聊天功能使用）
            resp = _http_session.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 200:
                return True, resp.json()
            return False, f"API错误: {resp.status_code} - {resp.text[:200]}"
    except Exception as e:
        return False, str(e)


def _build_messages(system_prompt: str, history: list, user_message: str, context_rounds: int = 100):
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history and context_rounds > 0:
        max_msgs = context_rounds * 2
        recent = history[-max_msgs:]
        for msg in recent:
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})
    return messages


# ==================== 章节解析 ====================

_CHAPTER_SPECIAL_TITLES = {
    "序章", "序", "楔子", "引子", "前言", "正文",
    "终章", "尾声", "后记", "番外", "番外篇", "完结感言"
}

_STRONG_CHAPTER_PATTERNS = [
    # 第15章 / 第 15 章 / 第十五章 / 第 十五 章 / 第十五章：归来 / 第十五卷
    r'^第\s*[零一二三四五六七八九十百千万两〇\d]+\s*[章节回卷集部篇册]\s*(?:[：:·\-—.．、]\s*.*)?$',
    r'^第\s*[零一二三四五六七八九十百千万两〇\d]+\s*[章节回卷集部篇册]\s+.*$',

    # chapter 15 / CHAPTER 15 / chapter iv
    r'^chapter\s*\d+\s*(?:[:：.\-—]\s*.*)?$',
    r'^chapter\s*[ivxlcdm]+\s*(?:[:：.\-—]\s*.*)?$',
]

_WEAK_CHAPTER_PATTERNS = [
    # 1、这一天 / 1. 这一天 / 1-这一天 / 1 — 这一天
    r'^\d+\s*[、.．\-—]\s*.+$',
    # 1 这一天 / 001 重生
    r'^\d+\s+.+$',
]


def _normalize_chapter_line(line: str) -> str:
    if line is None:
        return ""
    line = line.replace("\ufeff", "").replace("\u3000", " ")
    line = line.strip()
    line = re.sub(r"\s+", " ", line)
    return line


def _is_special_chapter_title(line: str) -> bool:
    normalized = _normalize_chapter_line(line)
    return normalized in _CHAPTER_SPECIAL_TITLES


def _is_strong_chapter_title(line: str) -> bool:
    normalized = _normalize_chapter_line(line)
    if not normalized:
        return False

    if _is_special_chapter_title(normalized):
        return True

    if len(normalized) > 80:
        return False

    for pattern in _STRONG_CHAPTER_PATTERNS:
        if re.match(pattern, normalized, re.IGNORECASE):
            return True
    return False


def _is_weak_chapter_title(line: str) -> bool:
    normalized = _normalize_chapter_line(line)
    if not normalized:
        return False

    if len(normalized) > 60:
        return False

    for pattern in _WEAK_CHAPTER_PATTERNS:
        if re.match(pattern, normalized, re.IGNORECASE):
            return True
    return False


def _collect_following_text_length(lines: list, start_index: int, max_lookahead: int = 8) -> int:
    """
    从 start_index 之后向下看几行，统计像正文的总长度
    用于辅助判断弱标题是否可信
    """
    total_len = 0
    for i in range(start_index + 1, min(len(lines), start_index + 1 + max_lookahead)):
        text = _normalize_chapter_line(lines[i])
        if not text:
            continue
        if _is_strong_chapter_title(text):
            break
        total_len += len(text)
    return total_len


def _count_chapter_signal_lines(lines: list):
    strong_count = 0
    weak_count = 0
    for line in lines:
        normalized = _normalize_chapter_line(line)
        if not normalized:
            continue
        if _is_strong_chapter_title(normalized):
            strong_count += 1
        elif _is_weak_chapter_title(normalized):
            weak_count += 1
    return strong_count, weak_count


def _is_chapter_title_with_context(lines: list, index: int, strong_count: int, weak_count: int) -> bool:
    """
    带上下文的章节判断：
    - 强标题：直接认
    - 弱标题：尽量匹配，但增加少量上下文判断，避免太离谱的误判
    """
    raw_line = lines[index]
    normalized = _normalize_chapter_line(raw_line)
    if not normalized:
        return False

    if _is_strong_chapter_title(normalized):
        return True

    if not _is_weak_chapter_title(normalized):
        return False

    # 尽可能匹配：只做轻度约束，不做过强拦截
    # 1) 如果全文完全是弱章节模式小说，就允许匹配
    # 2) 如果全文同时有强章节模式，也仍允许弱标题出现（有些小说混用）
    # 3) 但如果后面几乎没有正文，也不像章节
    following_len = _collect_following_text_length(lines, index, max_lookahead=8)
    if following_len >= 12:
        return True

    # 如果弱章节信号很多，也接受
    if weak_count >= 3:
        return True

    # 如果这一行形式非常典型，也接受
    if re.match(r'^\d+\s*[、.．\-—]\s*.+$', normalized, re.IGNORECASE):
        return True

    # 最后的兜底：纯数字+空格标题，如果全文至少有一本像小说章节的结构，也接受
    if strong_count + weak_count >= 2 and re.match(r'^\d+\s+.+$', normalized, re.IGNORECASE):
        return True

    return False


def _parse_chapters(content: str) -> list:
    if not content:
        return []

    lines = content.splitlines()
    strong_count, weak_count = _count_chapter_signal_lines(lines)

    chapters = []
    current_chapter = None
    current_lines = []

    for idx, raw_line in enumerate(lines):
        stripped = _normalize_chapter_line(raw_line)

        if not stripped:
            if current_chapter is not None:
                current_lines.append(raw_line)
            continue

        if _is_chapter_title_with_context(lines, idx, strong_count, weak_count):
            if current_chapter is not None:
                chapter_text = '\n'.join(current_lines).strip()
                if chapter_text:
                    chapters.append({
                        'title': current_chapter,
                        'content': chapter_text,
                        'index': len(chapters) + 1
                    })
            current_chapter = stripped
            current_lines = []
        else:
            if current_chapter is not None:
                current_lines.append(raw_line)

    if current_chapter is not None:
        chapter_text = '\n'.join(current_lines).strip()
        if chapter_text:
            chapters.append({
                'title': current_chapter,
                'content': chapter_text,
                'index': len(chapters) + 1
            })

    return chapters


# ==================== 批处理核心 ====================

# LLM API 调用重试配置
LLM_MAX_RETRIES = 3
LLM_RETRY_DELAY = 5  # 秒


def _call_llm_api_with_retry(config: dict, messages: list, max_retries: int = LLM_MAX_RETRIES, use_stream: bool = True):
    """
    带重试机制的 LLM API 调用

    Args:
        config: 配置字典
        messages: 消息列表
        max_retries: 最大重试次数
        use_stream: 是否使用流式响应（批处理默认使用流式）

    Returns:
        (success, result) - 成功时result是响应JSON，失败时result是错误信息
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        success, result = _call_llm_api(config, messages, use_stream=use_stream)
        if success:
            return True, result
        last_error = result
        logger.warning(f"LLM API 调用失败 (第 {attempt}/{max_retries} 次): {last_error}")
        if attempt < max_retries:
            time.sleep(LLM_RETRY_DELAY * attempt)  # 递增等待
    return False, last_error


def process_single_novel(task_id: str, file_idx: int, file_info: dict, config: dict, start_batch: int = 0, delay_min: int = 15, delay_max: int = 45):
    """
    在独立线程中处理一本小说文档
    - 不同线程处理不同小说文档
    - 每本小说按批次处理章节（如每批次10章）
    - 每个批次：系统提示词 + 批次章节内容 → 发送给 LLM API
    - 处理完所有批次后 → 上传 HF 数据集
    - 支持从指定批次开始恢复运行
    - 批次间随机延迟避免限速
    """
    try:
        logger.info(f"[线程{file_idx}] 开始执行 process_single_novel, task_id={task_id}")
        task = task_store.get_task_ref(task_id)
        logger.info(f"[线程{file_idx}] 任务状态: {task.get('status') if task else 'None'}")
        if not task or task.get("status") in ("cancelled", "completed"):
            logger.warning(f"[线程{file_idx}] 任务不存在或已完成，跳过")
            return

        # 获取章节列表（深拷贝，避免共享引用问题）
        chapters = list(file_info.get("chapters", []))
        batch_size = max(1, _safe_int(file_info.get("batch_size", DEFAULT_BATCH_SIZE), DEFAULT_BATCH_SIZE, min_value=1))
        file_name = file_info.get("file_name", "未命名")

        # 获取提示词配置
        system_prompt = config.get("batchSystemPrompt", config.get("systemPrompt", "You are a helpful AI assistant."))
        user_prompt_template = config.get("batchUserPromptTemplate", "")

        # 确保延迟范围有效
        delay_min = max(0, _safe_int(delay_min, 15, min_value=0))
        delay_max = max(delay_min, _safe_int(delay_max, 45, min_value=0))

        # 上下文历史
        context_messages = []

        total_chapters = len(chapters)
        total_batches = (total_chapters + batch_size - 1) // batch_size if total_chapters > 0 else 0

        logger.info(f"[线程{file_idx}] 开始处理 [{file_name}] 共 {total_chapters} 章, 分 {total_batches} 批次, 每批 {batch_size} 章, 从批次 {start_batch + 1} 开始, 延迟 {delay_min}-{delay_max}秒")

        if total_chapters == 0:
            logger.warning(f"[线程{file_idx}] [{file_name}] 没有章节可处理，跳过")
            return

        # 按批次处理
        for batch_index in range(start_batch, total_batches):
            # 检查任务是否被取消
            task = task_store.get_task_ref(task_id)
            if not task or task.get("status") == "cancelled":
                task_store.update_task(task_id, {"status": "cancelled", "message": "任务已取消"})
                return

            # 计算当前批次的章节范围
            batch_start = batch_index * batch_size
            batch_end = min(batch_start + batch_size, total_chapters)
            batch_chapters = chapters[batch_start:batch_end]
            batch_num = batch_index + 1

            logger.info(f"[线程{file_idx}] [{file_name}] 处理批次 {batch_num}/{total_batches}: 章节 {batch_start+1}-{batch_end} (共 {len(batch_chapters)} 章)")

            # 构建当前批次的章节内容
            batch_content_parts = []
            for ch in batch_chapters:
                title = ch.get("title", "未命名章节")
                content = ch.get("content", "")
                batch_content_parts.append(f"\n\n=== {title} ===\n\n{content}")
            batch_content = "".join(batch_content_parts)

            # 构建用户消息
            if user_prompt_template:
                user_message = user_prompt_template.replace("{content}", batch_content)
            else:
                user_message = batch_content

            # 构建消息列表
            messages = [{"role": "system", "content": system_prompt}]

            # 添加上一个批次的上下文（只保留 n-1 批次的 user+assistant）
            max_context_batches = 1
            recent_context = context_messages[-(max_context_batches * 2):]
            messages.extend(recent_context)

            # 添加当前批次的用户消息
            messages.append({"role": "user", "content": user_message})

            logger.info(f"[线程{file_idx}] 批次 {batch_num}/{total_batches} 发送给 LLM: {len(messages)} 条消息, 用户消息长度: {len(user_message)} 字符")

            # 调用 LLM API（带重试）
            success, result = _call_llm_api_with_retry(config, messages)

            if success:
                assistant_message = _extract_assistant_content(result)

                # 保存上下文
                context_messages.append({"role": "user", "content": user_message})
                context_messages.append({"role": "assistant", "content": assistant_message})

                # 整个批次存为一条结果
                batch_result = {
                    "batch": batch_num,
                    "total_batches": total_batches,
                    "chapter_start": batch_start + 1,
                    "chapter_end": batch_end,
                    "chapter_count": len(batch_chapters),
                    "chapter_titles": [ch.get("title", "未命名章节") for ch in batch_chapters],
                    "success": True,
                    "result": assistant_message,
                    "preview": assistant_message[:300] + ("..." if len(assistant_message) > 300 else ""),
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                task_store.append_file_result(task_id, file_idx, batch_result)

                # 更新进度
                task = task_store.get_task(task_id)
                if task:
                    completed = task.get("completed_chapters", 0) + len(batch_chapters)
                    total = task.get("total_chapters", 0)
                    task_store.update_task(task_id, {
                        "completed_chapters": completed,
                        "progress": f"{completed}/{total}",
                        "message": f"[{file_name}] 批次 {batch_num}/{total_batches} 完成 ({completed}/{total})"
                    })

                logger.info(f"[线程{file_idx}] [{file_name}] 批次 {batch_num}/{total_batches} 处理成功")

                # 批次间随机延迟（最后一个批次不延迟）
                if batch_index < total_batches - 1 and delay_max > 0:
                    delay = random.randint(delay_min, delay_max)
                    logger.info(f"[线程{file_idx}] [{file_name}] 批次 {batch_num} 完成，等待 {delay} 秒后继续...")
                    time.sleep(delay)
            else:
                # 处理失败 - 整个批次存为一条失败记录
                logger.error(f"[线程{file_idx}] [{file_name}] 批次 {batch_num}/{total_batches} 处理失败: {result}")
                batch_result = {
                    "batch": batch_num,
                    "total_batches": total_batches,
                    "chapter_start": batch_start + 1,
                    "chapter_end": batch_end,
                    "chapter_count": len(batch_chapters),
                    "chapter_titles": [ch.get("title", "未命名章节") for ch in batch_chapters],
                    "success": False,
                    "error": str(result),
                    "preview": "",
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                task_store.append_file_result(task_id, file_idx, batch_result)

                task = task_store.get_task(task_id)
                if task:
                    failed = task.get("failed_chapters", 0) + len(batch_chapters)
                    task_store.update_task(task_id, {"failed_chapters": failed})

        logger.info(f"[线程{file_idx}] [{file_name}] 全部 {total_batches} 批次处理完成")

    except Exception as e:
        logger.error(f"[线程{file_idx}] 书籍处理异常 [{task_id}] [{file_info.get('file_name', 'unknown')}]: {e}")
        import traceback
        traceback.print_exc()


def _run_batch_task(task_id: str, resume: bool = False, delay_min: int = None, delay_max: int = None):
    """
    执行批处理任务（多文件并行）
    - 每本书一个线程
    - 线程池控制并发
    - 支持从失败处恢复运行
    """
    try:
        task = task_store.get_task(task_id)
        if not task:
            return

        files = task.get("files", [])

        # 使用传入的延迟配置，如果没有则使用默认值
        actual_delay_min = delay_min if delay_min is not None else BATCH_DELAY_MIN
        actual_delay_max = delay_max if delay_max is not None else BATCH_DELAY_MAX

        if not resume:
            # 新任务：初始化计数
            total_chapters = sum(f.get("total", 0) for f in files)
            task_store.update_task(task_id, {
                "status": "processing",
                "total_chapters": total_chapters,
                "completed_chapters": 0,
                "failed_chapters": 0,
                "progress": f"0/{total_chapters}",
                "message": "开始处理..."
            })
        else:
            # 恢复任务：保持已有进度，只更新状态
            task_store.update_task(task_id, {
                "status": "processing",
                "message": "从失败处恢复运行..."
            })

        # 为每本书创建独立线程
        threads = []
        for file_idx, file_info in enumerate(files):
            # 检查任务是否被取消
            task = task_store.get_task_ref(task_id)
            if not task or task.get("status") == "cancelled":
                task_store.update_task(task_id, {"status": "cancelled", "message": "任务已取消"})
                return

            # 获取预解析的配置
            config = file_info.get("resolved_config", {})

            # 计算该文件的起始批次（恢复模式）
            start_batch = 0
            if resume:
                results = file_info.get("results", [])
                # 找到第一个失败或未处理的批次
                processed_batches = set()
                for r in results:
                    if r.get("success"):
                        processed_batches.add(r.get("batch", 0))

                # 找到第一个未成功处理的批次
                file_batch_size = max(1, _safe_int(file_info.get("batch_size", DEFAULT_BATCH_SIZE), DEFAULT_BATCH_SIZE, min_value=1))
                total_batches = (file_info.get("total", 0) + file_batch_size - 1) // file_batch_size
                for b in range(1, total_batches + 1):
                    if b not in processed_batches:
                        start_batch = b - 1  # 转换为0-based索引
                        break

                if start_batch > 0:
                    logger.info(f"[恢复] 文件 {file_info.get('file_name')} 从批次 {start_batch + 1} 继续")

            # 创建线程 - 每个线程独立处理一本小说文档
            thread = threading.Thread(
                target=process_single_novel,
                args=(task_id, file_idx, file_info, config, start_batch, actual_delay_min, actual_delay_max)
            )
            thread.daemon = True
            threads.append(thread)
            thread.start()
            logger.info(f"启动线程 {file_idx} 处理小说: {file_info.get('file_name', 'unknown')}")

        # 等待所有线程完成
        for thread in threads:
            thread.join()

        # 每本小说处理完后立即上传结果到 HF 数据集
        task = task_store.get_task(task_id)
        if not task:
            return

        for file_idx, file_info in enumerate(task.get("files", [])):
            _upload_single_novel_result(task_id, file_idx, file_info)

        # 完成状态判定
        task = task_store.get_task(task_id)
        if not task:
            return

        completed = task.get("completed_chapters", 0)
        failed = task.get("failed_chapters", 0)
        total = task.get("total_chapters", 0)

        if task.get("status") == "cancelled":
            return

        final_status = "completed" if failed == 0 else "partial_failed"
        final_progress = "完成" if failed == 0 else f"{completed}/{total}"
        final_message = (
            f"批处理完成: {completed}/{total}"
            if failed == 0
            else f"批处理完成，但存在失败章节: 成功 {completed}/{total}，失败 {failed}"
        )

        task_store.update_task(task_id, {
            "status": final_status,
            "progress": final_progress,
            "message": final_message
        })

    except Exception as e:
        logger.error(f"批处理任务异常 [{task_id}]: {e}")
        import traceback
        traceback.print_exc()
        task_store.update_task(task_id, {"status": "failed", "message": str(e)})


def _upload_single_novel_result(task_id: str, file_idx: int, file_info: dict):
    """单本小说处理完成后，立即上传结果到 HF 数据集（小说名-节奏.txt）"""
    task = task_store.get_task(task_id)
    if not task:
        return

    # 从文件配置或默认配置获取 HF token/dataset
    config = file_info.get("resolved_config", {})
    hf_token = config.get("hfToken", "")
    hf_dataset = config.get("hfDataset", "")

    if not hf_token or not hf_dataset:
        default_config = task.get("default_config", {})
        hf_token = hf_token or default_config.get("hfToken", "")
        hf_dataset = hf_dataset or default_config.get("hfDataset", "")

    if not hf_token or not hf_dataset:
        logger.warning(f"[{task_id}] 未配置 HF Token/Dataset，跳过上传")
        task_store.update_task(task_id, {"result_persist_error": "未配置HF存储"})
        return

    file_name = file_info.get("file_name", "未命名")
    # 清理文件名
    safe_name = file_name.replace(" ", "_").replace("/", "_").replace("\\", "_").replace("\n", "").replace("\r", "")
    safe_name = safe_name[:80]

    try:
        from app.services.hf_dataset_service import hf_dataset_service

        # 构建纯文本内容（不是 JSON）
        text_parts = []
        text_parts.append(f"小说: {file_name}")
        text_parts.append(f"任务ID: {task_id}")
        text_parts.append(f"处理时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        text_parts.append(f"{'='*60}\n")

        results = file_info.get("results", [])
        for r in results:
            batch_num = r.get("batch", "?")
            chapter_start = r.get("chapter_start", "?")
            chapter_end = r.get("chapter_end", "?")
            text_parts.append(f"--- 批次 {batch_num}: 第{chapter_start}-{chapter_end}章 ---\n")
            if r.get("success"):
                text_parts.append(r.get("result", ""))
            else:
                text_parts.append(f"❌ 失败: {r.get('error', '')}")
            text_parts.append("\n")

        content = "\n".join(text_parts)
        filename = f"{safe_name}-节奏.txt"

        hf_dataset_service.upload_text_file(hf_token, hf_dataset, filename, content)

        # 记录上传成功
        uploaded = task.get("result_files", [])
        uploaded.append(filename)
        task_store.update_task(task_id, {
            "result_persisted": True,
            "result_files": uploaded
        })
        logger.info(f"小说 [{file_name}] 结果已上传到 HF 数据集: {filename}")

    except Exception as e:
        logger.error(f"上传小说结果失败 [{file_name}]: {e}")
        import traceback
        traceback.print_exc()
        task_store.update_task(task_id, {"result_persist_error": str(e)})


# ==================== 路由注册 ====================

def register_routes(app):

    @app.route('/')
    def index():
        return send_from_directory('static', 'index.html')

    @app.route('/api/health')
    def health():
        return jsonify({
            'status': 'ok',
            'timestamp': int(time.time() * 1000),
            'max_concurrent': MAX_CONCURRENT_TASKS,
            'thread_pool_size': DEFAULT_THREAD_POOL_SIZE,
            'batch_size': DEFAULT_BATCH_SIZE,
            'batch_delay_min': BATCH_DELAY_MIN,
            'batch_delay_max': BATCH_DELAY_MAX,
            'available_slots': _get_available_slots()
        })

    # ==================== 设置 ====================

    @app.route('/api/settings', methods=['GET'])
    def get_settings():
        settings = app_settings_service.get_default_runtime_config()
        # 添加延迟配置
        settings['batchDelayMin'] = BATCH_DELAY_MIN
        settings['batchDelayMax'] = BATCH_DELAY_MAX
        return jsonify({
            'success': True,
            'settings': settings
        })

    @app.route('/api/settings/update', methods=['POST'])
    def update_settings():
        """更新默认配置（仅非敏感字段）"""
        data = request.json or {}
        app_settings_service.update_user_defaults(data)
        return jsonify({'success': True, 'settings': app_settings_service.get_default_runtime_config()})

    @app.route('/api/set-concurrent', methods=['POST'])
    def set_concurrent():
        global MAX_CONCURRENT_TASKS, task_semaphore
        data = request.json or {}
        value = _safe_int(data.get('maxConcurrent', 10), 10, min_value=1, max_value=50)
        MAX_CONCURRENT_TASKS = value
        task_semaphore = threading.Semaphore(value)
        logger.info(f"并发数调整为: {value}")
        return jsonify({'success': True, 'maxConcurrent': value})

    @app.route('/api/set-thread-pool', methods=['POST'])
    def set_thread_pool():
        """设置线程池大小"""
        global DEFAULT_THREAD_POOL_SIZE, executor
        data = request.json or {}
        value = _safe_int(data.get('threadPoolSize', 10), 10, min_value=1, max_value=100)
        DEFAULT_THREAD_POOL_SIZE = value
        # 重新创建线程池
        executor = ThreadPoolExecutor(max_workers=value)
        logger.info(f"线程池大小调整为: {value}")
        return jsonify({'success': True, 'threadPoolSize': value})

    @app.route('/api/set-batch-delay', methods=['POST'])
    def set_batch_delay():
        """设置批次间延迟范围（秒）"""
        global BATCH_DELAY_MIN, BATCH_DELAY_MAX
        data = request.json or {}
        min_val = _safe_int(data.get('delayMin', BATCH_DELAY_MIN), BATCH_DELAY_MIN, min_value=0)
        max_val = _safe_int(data.get('delayMax', BATCH_DELAY_MAX), BATCH_DELAY_MAX, min_value=min_val)
        BATCH_DELAY_MIN = min_val
        BATCH_DELAY_MAX = max_val
        logger.info(f"批次延迟调整为: {min_val}-{max_val} 秒")
        return jsonify({
            'success': True,
            'batchDelayMin': BATCH_DELAY_MIN,
            'batchDelayMax': BATCH_DELAY_MAX
        })

    @app.route('/api/status', methods=['GET'])
    def get_status():
        return jsonify({
            'success': True,
            'maxConcurrent': MAX_CONCURRENT_TASKS,
            'threadPoolSize': DEFAULT_THREAD_POOL_SIZE,
            'batchSize': DEFAULT_BATCH_SIZE,
            'batchDelayMin': BATCH_DELAY_MIN,
            'batchDelayMax': BATCH_DELAY_MAX,
            'availableSlots': _get_available_slots(),
            'taskCount': task_store.task_count()
        })

    # ==================== 配置管理 ====================

    @app.route('/api/config/list', methods=['GET'])
    def list_configs():
        user_id = request.args.get('userId', 'default')
        configs = conversation_config_store.get_user_configs_safe(user_id)
        return jsonify({'success': True, 'configs': configs})

    @app.route('/api/config/save', methods=['POST'])
    def save_config():
        data = request.json or {}
        user_id = data.get('userId', 'default')
        config_id = data.get('id') or str(uuid.uuid4())
        config_data = {
            "name": data.get('name', '未命名配置'),
            # 聊天功能提示词
            "systemPrompt": data.get('systemPrompt', ''),
            # 批处理功能提示词（独立配置）
            "batchSystemPrompt": data.get('batchSystemPrompt', ''),
            "batchUserPromptTemplate": data.get('batchUserPromptTemplate', ''),
            # 批次大小
            "batchSize": data.get('batchSize', ''),
            # 模型配置
            "model": data.get('model', ''),
            "temperature": data.get('temperature', ''),
            "topP": data.get('topP', ''),
            "contextRounds": data.get('contextRounds', ''),
            "maxOutputTokens": data.get('maxOutputTokens', ''),
            # API配置
            "apiHost": data.get('apiHost', ''),
            "apiKey": data.get('apiKey', ''),
            # HF配置
            "hfToken": data.get('hfToken', ''),
            "hfDataset": data.get('hfDataset', ''),
        }
        conversation_config_store.save_config(user_id, config_id, config_data)
        return jsonify({'success': True, 'id': config_id})

    @app.route('/api/config/delete', methods=['POST'])
    def delete_config():
        data = request.json or {}
        user_id = data.get('userId', 'default')
        config_id = data.get('id')
        if not config_id:
            return jsonify({'success': False, 'error': '缺少配置ID'}), 400
        conversation_config_store.delete_config(user_id, config_id)
        return jsonify({'success': True})

    # ==================== 聊天 ====================

    @app.route('/api/conversations', methods=['GET'])
    def get_conversations():
        user_id = request.args.get('userId', 'default')
        convs = conversation_store.get_user_conversations(user_id)
        return jsonify({'success': True, 'conversations': convs})

    @app.route('/api/conversation/create', methods=['POST'])
    def create_conversation():
        data = request.json or {}
        user_id = data.get('userId', 'default')
        conv_id = str(uuid.uuid4())
        conv_data = {
            'id': conv_id,
            'title': data.get('title', '新对话'),
            'configId': data.get('configId', ''),
            'created_at': int(time.time() * 1000),
            'messages': []
        }
        conversation_store.create_conversation(user_id, conv_id, conv_data)
        return jsonify({'success': True, 'conversation': conv_data})

    @app.route('/api/conversation/delete', methods=['POST'])
    def delete_conversation():
        data = request.json or {}
        user_id = data.get('userId', 'default')
        conv_id = data.get('id')
        if not conv_id:
            return jsonify({'success': False, 'error': '缺少对话ID'}), 400
        conversation_store.delete_conversation(user_id, conv_id)
        return jsonify({'success': True})

    @app.route('/api/chat', methods=['POST'])
    def chat():
        data = request.json or {}
        user_id = data.get('userId', 'default')
        conv_id = data.get('conversationId')
        user_message = data.get('message', '').strip()
        config_id = data.get('configId', '')

        if not user_message:
            return jsonify({'success': False, 'error': '消息不能为空'}), 400

        # 解析配置（在请求上下文中解析）
        config = app_settings_service.resolve_config_from_id(user_id, config_id)
        # 聊天功能使用 systemPrompt
        system_prompt = config.get('systemPrompt', '')
        context_rounds = _safe_int(config.get('contextRounds', 100), 100, min_value=0)

        # 添加用户消息
        user_msg = {'role': 'user', 'content': user_message, 'time': int(time.time() * 1000)}
        conversation_store.add_message(user_id, conv_id, user_msg)

        # 获取历史
        conv = conversation_store.get_conversation(user_id, conv_id)
        history = conv.get('messages', []) if conv else []
        messages = _build_messages(system_prompt, history[:-1], user_message, context_rounds)

        success, result = _call_llm_api(config, messages)

        if success:
            assistant_message = _extract_assistant_content(result)
            assistant_msg = {'role': 'assistant', 'content': assistant_message, 'time': int(time.time() * 1000)}
            conversation_store.add_message(user_id, conv_id, assistant_msg)
            return jsonify({'success': True, 'message': assistant_message})
        else:
            return jsonify({'success': False, 'error': result})

    # ==================== 批处理 ====================

    @app.route('/api/parse-chapters', methods=['POST'])
    def parse_chapters():
        data = request.json or {}
        content = data.get('content', '')
        if not content:
            return jsonify({'success': False, 'error': '内容不能为空'}), 400
        chapters = _parse_chapters(content)
        return jsonify({'success': True, 'chapters': chapters, 'total': len(chapters)})

    @app.route('/api/batch', methods=['POST'])
    def submit_batch():
        """
        提交多文件批处理任务
        - 在请求上下文中预解析配置
        - 每本书将独立线程处理
        """
        data = request.json or {}
        user_id = data.get('userId', 'default')
        files = data.get('files', [])
        batch_size = _safe_int(data.get('batchSize', DEFAULT_BATCH_SIZE), DEFAULT_BATCH_SIZE, min_value=1)
        # 获取延迟配置
        delay_min = _safe_int(data.get('delayMin', BATCH_DELAY_MIN), BATCH_DELAY_MIN, min_value=0)
        delay_max = _safe_int(data.get('delayMax', BATCH_DELAY_MAX), BATCH_DELAY_MAX, min_value=delay_min)

        if not files:
            return jsonify({'success': False, 'error': '没有文件可处理'}), 400

        task_id = str(uuid.uuid4())
        task_files = []
        total_chapters = 0

        # 获取默认配置（用于持久化）
        default_config = app_settings_service.get_full_config()

        for f in files:
            chapters = f.get('chapters', [])
            config_id = f.get('configId', '')
            config_name = f.get('configName', '默认配置')
            file_batch_size = _safe_int(f.get('batchSize', batch_size), batch_size, min_value=1)

            # 在请求上下文中解析配置（关键：避免后台线程中的应用上下文问题）
            resolved_config = app_settings_service.resolve_config_from_id(user_id, config_id)

            # 如果配置中有批次大小，使用配置中的
            if resolved_config.get('batchSize'):
                file_batch_size = _safe_int(resolved_config['batchSize'], file_batch_size, min_value=1)

            # 获取配置名（如果提供了 configId）
            if config_id and config_id != "__default__":
                cfg = conversation_config_store.get_config_full(user_id, config_id)
                if cfg:
                    config_name = cfg.get('name', config_name)

            task_files.append({
                "file_name": f.get('fileName', '未命名'),
                "config_id": config_id,
                "config_name": config_name,
                "chapters": chapters,
                "batch_size": file_batch_size,
                "results": [],
                "completed": 0,
                "failed": 0,
                "total": len(chapters),
                # 预解析的配置（包含所有必要字段）
                "resolved_config": resolved_config,
            })
            total_chapters += len(chapters)

        task_data = {
            'task_id': task_id,
            'user_id': user_id,
            'status': 'pending',
            'files': task_files,
            'total_chapters': total_chapters,
            'completed_chapters': 0,
            'failed_chapters': 0,
            'progress': f'0/{total_chapters}',
            'message': '等待处理...',
            'created_at': time.time(),
            # 保存默认配置用于持久化
            'default_config': default_config,
        }
        task_store.create_task(task_id, task_data)

        # 启动批处理任务
        thread = threading.Thread(target=_run_batch_task, args=(task_id, False, delay_min, delay_max))
        thread.daemon = True
        thread.start()

        return jsonify({'success': True, 'taskId': task_id, 'totalChapters': total_chapters})

    @app.route('/api/batch/cancel', methods=['POST'])
    def batch_cancel():
        data = request.json or {}
        task_id = data.get('taskId')
        if not task_id:
            return jsonify({'success': False, 'error': '缺少taskId'}), 400
        task_store.update_task(task_id, {'status': 'cancelled'})
        return jsonify({'success': True})

    @app.route('/api/batch/resume', methods=['POST'])
    def batch_resume():
        """从失败处恢复运行任务"""
        data = request.json or {}
        task_id = data.get('taskId')
        if not task_id:
            return jsonify({'success': False, 'error': '缺少taskId'}), 400

        task = task_store.get_task(task_id)
        if not task:
            return jsonify({'success': False, 'error': '任务不存在'}), 404

        # 只有已取消、失败、部分失败的任务可以恢复
        if task.get('status') not in ('cancelled', 'failed', 'partial_failed'):
            return jsonify({'success': False, 'error': '只有已取消、失败或部分失败的任务可以恢复'}), 400

        # 获取延迟配置
        delay_min = _safe_int(data.get('delayMin', BATCH_DELAY_MIN), BATCH_DELAY_MIN, min_value=0)
        delay_max = _safe_int(data.get('delayMax', BATCH_DELAY_MAX), BATCH_DELAY_MAX, min_value=delay_min)

        # 启动恢复任务
        thread = threading.Thread(target=_run_batch_task, args=(task_id, True, delay_min, delay_max))
        thread.daemon = True
        thread.start()

        logger.info(f"任务 {task_id} 从失败处恢复运行")
        return jsonify({'success': True, 'message': '任务已恢复运行'})

    # ==================== 任务管理 ====================

    @app.route('/api/tasks', methods=['GET'])
    def get_tasks():
        summary = task_store.get_all_tasks_summary()
        return jsonify({'success': True, 'tasks': summary})

    @app.route('/api/task/<task_id>', methods=['GET'])
    def get_task_detail(task_id):
        """获取任务详情（含结果，用于查看）"""
        task = task_store.get_task(task_id)
        if not task:
            return jsonify({'success': False, 'error': '任务不存在'}), 404
        return jsonify({'success': True, 'task': task})

    @app.route('/api/task/delete', methods=['POST'])
    def delete_task():
        data = request.json or {}
        task_id = data.get('taskId')
        if not task_id:
            return jsonify({'success': False, 'error': '缺少taskId'}), 400
        task_store.delete_task(task_id)
        return jsonify({'success': True})

    @app.route('/api/task/<task_id>/download', methods=['GET'])
    def download_task_results(task_id):
        """下载任务结果为文本 - 按小说分组，按批次展示"""
        task = task_store.get_task(task_id)
        if not task:
            return jsonify({'success': False, 'error': '任务不存在'}), 404

        text_parts = []
        for f in task.get("files", []):
            file_name = f.get('file_name', '未命名')
            text_parts.append(f"{'='*60}")
            text_parts.append(f"小说: {file_name}")
            text_parts.append(f"{'='*60}\n")

            for r in f.get("results", []):
                batch_num = r.get('batch', '?')
                chapter_start = r.get('chapter_start', '?')
                chapter_end = r.get('chapter_end', '?')
                text_parts.append(f"--- 批次 {batch_num}: 第{chapter_start}-{chapter_end}章 ---\n")
                if r.get("success"):
                    text_parts.append(r.get("result", ""))
                else:
                    text_parts.append(f"❌ 失败: {r.get('error', '')}")
                text_parts.append("\n")

        content = '\n'.join(text_parts)
        return jsonify({'success': True, 'content': content, 'filename': f"batch_{task_id[:8]}.txt"})

    @app.route('/api/task/<task_id>/download/<int:file_idx>', methods=['GET'])
    def download_single_novel(task_id, file_idx):
        """下载单本小说的处理结果"""
        task = task_store.get_task(task_id)
        if not task:
            return jsonify({'success': False, 'error': '任务不存在'}), 404

        files = task.get("files", [])
        if file_idx < 0 or file_idx >= len(files):
            return jsonify({'success': False, 'error': '文件索引无效'}), 400

        f = files[file_idx]
        file_name = f.get('file_name', '未命名')
        safe_name = file_name.replace(" ", "_").replace("/", "_").replace("\\", "_").replace("\n", "").replace("\r", "")[:80]

        text_parts = []
        text_parts.append(f"小说: {file_name}")
        text_parts.append(f"任务ID: {task_id}")
        text_parts.append(f"处理时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        text_parts.append(f"{'='*60}\n")

        for r in f.get("results", []):
            batch_num = r.get('batch', '?')
            chapter_start = r.get('chapter_start', '?')
            chapter_end = r.get('chapter_end', '?')
            text_parts.append(f"--- 批次 {batch_num}: 第{chapter_start}-{chapter_end}章 ---\n")
            if r.get("success"):
                text_parts.append(r.get("result", ""))
            else:
                text_parts.append(f"❌ 失败: {r.get('error', '')}")
            text_parts.append("\n")

        content = '\n'.join(text_parts)
        return jsonify({'success': True, 'content': content, 'filename': f"{safe_name}-节奏.txt"})

    # ==================== HF 数据集 ====================

    @app.route('/api/hf-action', methods=['POST'])
    def hf_action():
        data = request.json or {}
        result = file_service.hf_action(data)
        status_code = 200 if result.get('success') else 400
        return jsonify(result), status_code

    @app.route('/api/hf-files', methods=['GET'])
    def hf_files():
        hf_token = request.args.get('hfToken', '')
        hf_dataset = request.args.get('hfDataset', '')
        files = file_service.list_dataset_files(hf_token, hf_dataset)
        return jsonify({'success': True, 'files': files})

    @app.route('/api/hf-download', methods=['POST'])
    def hf_download():
        data = request.json or {}
        hf_token = data.get('hfToken', '')
        hf_dataset = data.get('hfDataset', '')
        filename = data.get('filename', '')
        if not filename:
            return jsonify({'success': False, 'error': '缺少文件名'}), 400
        result = file_service.download_dataset_file(hf_token, hf_dataset, filename)
        status_code = 200 if result.get('success') else 400
        return jsonify(result), status_code

    @app.route('/api/hf-create-dataset', methods=['POST'])
    def hf_create_dataset():
        """前端请求创建 private 数据集"""
        data = request.json or {}
        hf_token = data.get('hfToken', '')
        dataset_name = data.get('datasetName', '')
        if not hf_token or not dataset_name:
            return jsonify({'success': False, 'error': '需要 HF Token 和数据集名称'}), 400
        from app.services.hf_dataset_service import hf_dataset_service
        success = hf_dataset_service.create_dataset(hf_token, dataset_name, private=True)
        if success:
            return jsonify({'success': True, 'message': f'数据集 {dataset_name} 已创建 (private)'})
        return jsonify({'success': False, 'error': '创建失败'})