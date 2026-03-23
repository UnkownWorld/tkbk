import json
import time
import uuid
import threading
import logging
import re
import random
import hashlib
import queue
import requests as http_requests
from flask import request, jsonify, send_from_directory

from app.stores.task_store import task_store
from app.stores.cache_store import cache_store
from app.stores.conversation_store import conversation_store
from app.stores.conversation_config_store import conversation_config_store
from app.services.file_service import file_service
from app.services.app_settings_service import app_settings_service

logger = logging.getLogger(__name__)

# 全局配置
DEFAULT_THREAD_POOL_SIZE = 10       # 工作线程数：同时最多处理多少本书
DEFAULT_BATCH_SIZE = 10             # 默认每批次10章
MAX_CONCURRENT_TASKS = 10           # 兼容保留，实际以工作线程池并发为主
BATCH_DELAY_MIN = 15
BATCH_DELAY_MAX = 45

_http_session = http_requests.Session()

# ==================== 全局队列 / Worker ====================

_book_queue = queue.Queue()
_worker_threads = []
_worker_lock = threading.RLock()
_worker_stop_event = threading.Event()

# 去重索引：fingerprint -> metadata
# metadata: {"task_id": ..., "file_name": ..., "status": queued/processing}
_book_dedup_index = {}
_book_dedup_lock = threading.RLock()


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


def _extract_assistant_content(result: dict) -> str:
    try:
        return result.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    except Exception:
        return ""


def _normalize_text_for_hash(text: str) -> str:
    if text is None:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(_normalize_text_for_hash(text).encode("utf-8")).hexdigest()


def _sanitize_filename(name: str) -> str:
    return (name or "未命名").replace(" ", "_").replace("/", "_").replace("\\", "_").replace("\n", "").replace("\r", "")[:80]


def _get_worker_stats():
    with _worker_lock:
        alive = sum(1 for t in _worker_threads if t.is_alive())
    return {
        "workerCount": len(_worker_threads),
        "aliveWorkers": alive,
        "queueSize": _book_queue.qsize(),
    }


def _random_delay_seconds(delay_min: int, delay_max: int) -> int:
    delay_min = max(0, _safe_int(delay_min, BATCH_DELAY_MIN, min_value=0))
    delay_max = max(delay_min, _safe_int(delay_max, BATCH_DELAY_MAX, min_value=0))
    return random.randint(delay_min, delay_max) if delay_max > 0 else 0


def _sleep_with_cancel_check(task_id: str, seconds: int) -> bool:
    for _ in range(max(0, seconds)):
        task = task_store.get_task_ref(task_id)
        if not task or task.get("status") == "cancelled":
            return False
        time.sleep(1)
    return True


# ==================== Worker 生命周期 ====================

def _ensure_workers():
    global _worker_threads
    with _worker_lock:
        desired = max(1, DEFAULT_THREAD_POOL_SIZE)
        alive_threads = [t for t in _worker_threads if t.is_alive()]
        _worker_threads = alive_threads

        missing = desired - len(_worker_threads)
        for i in range(missing):
            worker = threading.Thread(
                target=_book_worker_loop,
                name=f"book-worker-{len(_worker_threads) + i + 1}",
                daemon=True
            )
            worker.start()
            _worker_threads.append(worker)

        logger.info(f"Worker池已就绪: desired={desired}, alive={len(_worker_threads)}")


def _restart_workers(new_size: int):
    global DEFAULT_THREAD_POOL_SIZE
    DEFAULT_THREAD_POOL_SIZE = max(1, new_size)
    _ensure_workers()


def _book_worker_loop():
    logger.info(f"[{threading.current_thread().name}] 启动")
    while not _worker_stop_event.is_set():
        try:
            item = _book_queue.get(timeout=1)
        except queue.Empty:
            continue

        try:
            task_id = item["task_id"]
            file_idx = item["file_idx"]

            task = task_store.get_task(task_id)
            if not task:
                _book_queue.task_done()
                continue

            file_info = task["files"][file_idx]
            fingerprint = file_info.get("fingerprint")

            # 任务已取消，跳过
            if task.get("status") == "cancelled":
                _mark_file_terminal(task_id, file_idx, "cancelled", "任务已取消")
                _release_dedup_if_terminal(fingerprint, "cancelled")
                _update_task_status_if_finished(task_id)
                _book_queue.task_done()
                continue

            # 标记处理中
            _mark_file_processing(task_id, file_idx)
            _set_dedup_status(fingerprint, task_id, file_info.get("file_name", ""), "processing")

            logger.info(f"[{threading.current_thread().name}] 开始处理 task={task_id} file_idx={file_idx} file={file_info.get('file_name')}")
            _process_single_book(task_id, file_idx)

            # 单本结束后更新任务聚合状态
            _update_task_status_if_finished(task_id)

        except Exception as e:
            logger.error(f"[{threading.current_thread().name}] 处理书籍任务异常: {e}", exc_info=True)
        finally:
            _book_queue.task_done()


# ==================== 去重索引 ====================

def _build_book_fingerprint(file_name: str, chapters: list, config_id: str, batch_size: int, start_chapter: int, end_chapter: int) -> str:
    chapter_digest_parts = []
    for ch in chapters:
        title = ch.get("title", "")
        content = ch.get("content", "")
        chapter_digest_parts.append(f"{title}\n{_sha256_text(content)}")

    payload = {
        "file_name": file_name or "",
        "chapters_digest": chapter_digest_parts,
        "config_id": config_id or "__default__",
        "batch_size": batch_size,
        "start_chapter": start_chapter,
        "end_chapter": end_chapter,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _check_duplicate_fingerprint(fingerprint: str):
    with _book_dedup_lock:
        item = _book_dedup_index.get(fingerprint)
        if item and item.get("status") in ("queued", "processing"):
            return item
        return None


def _register_fingerprint(fingerprint: str, task_id: str, file_name: str, status: str = "queued"):
    with _book_dedup_lock:
        _book_dedup_index[fingerprint] = {
            "task_id": task_id,
            "file_name": file_name,
            "status": status,
            "updated_at": time.time(),
        }


def _set_dedup_status(fingerprint: str, task_id: str, file_name: str, status: str):
    if not fingerprint:
        return
    with _book_dedup_lock:
        _book_dedup_index[fingerprint] = {
            "task_id": task_id,
            "file_name": file_name,
            "status": status,
            "updated_at": time.time(),
        }


def _release_dedup_if_terminal(fingerprint: str, status: str):
    if not fingerprint:
        return
    with _book_dedup_lock:
        item = _book_dedup_index.get(fingerprint)
        if not item:
            return
        item["status"] = status
        item["updated_at"] = time.time()


# ==================== LLM 调用 ====================

def _call_llm_api(config: dict, messages: list, use_stream: bool = False):
    api_host = (config.get("apiHost", "") or "").rstrip("/")
    api_key = config.get("apiKey", "")
    model = config.get("model", "gpt-5.4")
    temperature = _safe_float(config.get("temperature", 0.7), 0.7)
    top_p = _safe_float(config.get("topP", 0.65), 0.65)
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
        "stream": use_stream,
    }

    timeout = 1800 if use_stream else 600

    try:
        if use_stream:
            resp = _http_session.post(url, headers=headers, json=payload, timeout=timeout, stream=True)
            if resp.status_code != 200:
                return False, f"API错误: {resp.status_code} - {resp.text[:200]}"

            full_content = ""
            for line in resp.iter_lines():
                if line:
                    line = line.decode("utf-8", errors="ignore")
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
            resp = _http_session.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 200:
                return True, resp.json()
            return False, f"API错误: {resp.status_code} - {resp.text[:200]}"
    except Exception as e:
        return False, str(e)


def _call_llm_batch_with_retry(config: dict, messages: list, task_id: str, delay_min: int, delay_max: int, max_retries: int = 3):
    last_error = None
    for attempt in range(1, max_retries + 1):
        success, result = _call_llm_api(config, messages, use_stream=True)
        if success:
            return True, result

        last_error = result
        logger.warning(f"[{task_id}] LLM批次调用失败 (第 {attempt}/{max_retries} 次): {last_error}")

        if attempt < max_retries:
            delay = _random_delay_seconds(delay_min, delay_max)
            logger.info(f"[{task_id}] 批次失败，{delay} 秒后重试")
            if not _sleep_with_cancel_check(task_id, delay):
                return False, "任务已取消"

    return False, last_error


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
    r'^第\s*[零一二三四五六七八九十百千万两〇\d]+\s*[章节回卷集部篇册]\s*(?:[：:·\-—.．、]\s*.*)?$',
    r'^第\s*[零一二三四五六七八九十百千万两〇\d]+\s*[章节回卷集部篇册]\s+.*$',
    r'^chapter\s*\d+\s*(?:[:：.\-—]\s*.*)?$',
    r'^chapter\s*[ivxlcdm]+\s*(?:[:：.\-—]\s*.*)?$',
]

_WEAK_CHAPTER_PATTERNS = [
    r'^\d+\s*[、.．\-—]\s*.+$',
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
    return _normalize_chapter_line(line) in _CHAPTER_SPECIAL_TITLES


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
    raw_line = lines[index]
    normalized = _normalize_chapter_line(raw_line)
    if not normalized:
        return False

    if _is_strong_chapter_title(normalized):
        return True

    if not _is_weak_chapter_title(normalized):
        return False

    following_len = _collect_following_text_length(lines, index, max_lookahead=8)
    if following_len >= 12:
        return True

    if weak_count >= 3:
        return True

    if re.match(r'^\d+\s*[、.．\-—]\s*.+$', normalized, re.IGNORECASE):
        return True

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
                chapter_text = "\n".join(current_lines).strip()
                if chapter_text:
                    chapters.append({
                        "title": current_chapter,
                        "content": chapter_text,
                        "index": len(chapters) + 1
                    })
            current_chapter = stripped
            current_lines = []
        else:
            if current_chapter is not None:
                current_lines.append(raw_line)

    if current_chapter is not None:
        chapter_text = "\n".join(current_lines).strip()
        if chapter_text:
            chapters.append({
                "title": current_chapter,
                "content": chapter_text,
                "index": len(chapters) + 1
            })

    return chapters


def _slice_chapters(chapters: list, start_chapter: int = None, end_chapter: int = None):
    total = len(chapters)
    if total == 0:
        return [], 0, 0

    start = _safe_int(start_chapter, 1, min_value=1, max_value=total)
    end = _safe_int(end_chapter, total, min_value=1, max_value=total)
    if start > end:
        start, end = end, start

    selected = chapters[start - 1:end]
    return selected, start, end


# ==================== 任务 / 文件状态辅助 ====================

def _mark_file_processing(task_id: str, file_idx: int):
    task = task_store.get_task_ref(task_id)
    if not task:
        return
    files = task.get("files", [])
    if 0 <= file_idx < len(files):
        files[file_idx]["status"] = "processing"
        files[file_idx]["message"] = "处理中..."


def _mark_file_terminal(task_id: str, file_idx: int, status: str, message: str = ""):
    task = task_store.get_task_ref(task_id)
    if not task:
        return
    files = task.get("files", [])
    if 0 <= file_idx < len(files):
        files[file_idx]["status"] = status
        files[file_idx]["message"] = message


def _append_file_result(task_id: str, file_idx: int, result: dict):
    task = task_store.get_task_ref(task_id)
    if not task:
        return False

    files = task.get("files", [])
    if not (0 <= file_idx < len(files)):
        return False

    file_info = files[file_idx]
    file_info.setdefault("results", []).append(result)

    chapter_count = _safe_int(result.get("chapter_count", 0), 0, min_value=0)
    if result.get("success"):
        file_info["completed"] = file_info.get("completed", 0) + chapter_count
    else:
        file_info["failed"] = file_info.get("failed", 0) + chapter_count

    return True


def _update_task_aggregate_progress(task_id: str):
    task = task_store.get_task_ref(task_id)
    if not task:
        return

    total = 0
    completed = 0
    failed = 0
    queued = 0
    processing = 0

    for f in task.get("files", []):
        total += f.get("total", 0)
        completed += f.get("completed", 0)
        failed += f.get("failed", 0)
        status = f.get("status")
        if status == "queued":
            queued += 1
        elif status == "processing":
            processing += 1

    task["total_chapters"] = total
    task["completed_chapters"] = completed
    task["failed_chapters"] = failed

    if task.get("status") == "cancelled":
        task["progress"] = f"{completed}/{total}"
        task["message"] = f"任务已取消: {completed}/{total}"
        return

    task["progress"] = f"{completed}/{total}"

    if processing > 0:
        task["status"] = "processing"
        task["message"] = f"处理中：完成 {completed}/{total}，排队文件 {queued}"
    elif queued > 0:
        task["status"] = "pending"
        task["message"] = f"等待处理：完成 {completed}/{total}，排队文件 {queued}"


def _update_task_status_if_finished(task_id: str):
    task = task_store.get_task_ref(task_id)
    if not task:
        return

    files = task.get("files", [])
    if not files:
        task_store.update_task(task_id, {"status": "completed", "progress": "完成", "message": "无文件"})
        return

    file_statuses = [f.get("status", "queued") for f in files]
    terminal = {"completed", "failed", "partial_failed", "cancelled"}

    _update_task_aggregate_progress(task_id)

    if task.get("status") == "cancelled":
        return

    if all(s in terminal for s in file_statuses):
        completed = task.get("completed_chapters", 0)
        failed = task.get("failed_chapters", 0)
        total = task.get("total_chapters", 0)

        if failed > 0:
            task["status"] = "partial_failed"
            task["progress"] = f"{completed}/{total}"
            task["message"] = f"批处理完成，但存在失败章节: 成功 {completed}/{total}，失败 {failed}"
        else:
            task["status"] = "completed"
            task["progress"] = "完成"
            task["message"] = f"批处理完成: {completed}/{total}"


# ==================== 单本小说处理 ====================

def _process_single_book(task_id: str, file_idx: int):
    task = task_store.get_task(task_id)
    if not task:
        return

    files = task.get("files", [])
    if not (0 <= file_idx < len(files)):
        return

    file_info = files[file_idx]
    config = file_info.get("resolved_config", {})
    chapters = list(file_info.get("chapters", []))
    batch_size = max(1, _safe_int(file_info.get("batch_size", DEFAULT_BATCH_SIZE), DEFAULT_BATCH_SIZE, min_value=1))
    delay_min = _safe_int(file_info.get("delay_min", BATCH_DELAY_MIN), BATCH_DELAY_MIN, min_value=0)
    delay_max = _safe_int(file_info.get("delay_max", BATCH_DELAY_MAX), BATCH_DELAY_MAX, min_value=delay_min)
    file_name = file_info.get("file_name", "未命名")

    if not chapters:
        _mark_file_terminal(task_id, file_idx, "failed", "没有可处理章节")
        fingerprint = file_info.get("fingerprint")
        _release_dedup_if_terminal(fingerprint, "failed")
        _update_task_status_if_finished(task_id)
        return

    system_prompt = config.get("batchSystemPrompt", config.get("systemPrompt", "You are a helpful AI assistant."))
    user_prompt_template = config.get("batchUserPromptTemplate", "")
    context_messages = []

    total_chapters = len(chapters)
    total_batches = (total_chapters + batch_size - 1) // batch_size

    logger.info(f"[{task_id}] 开始处理单本小说 [{file_name}]，共 {total_chapters} 章，{total_batches} 批，每批 {batch_size} 章")

    try:
        for batch_index in range(total_batches):
            current_task = task_store.get_task_ref(task_id)
            if not current_task or current_task.get("status") == "cancelled":
                _mark_file_terminal(task_id, file_idx, "cancelled", "任务已取消")
                fingerprint = file_info.get("fingerprint")
                _release_dedup_if_terminal(fingerprint, "cancelled")
                return

            batch_start = batch_index * batch_size
            batch_end = min(batch_start + batch_size, total_chapters)
            batch_num = batch_index + 1
            batch_chapters = chapters[batch_start:batch_end]

            batch_content_parts = []
            for ch in batch_chapters:
                title = ch.get("title", "未命名章节")
                content = ch.get("content", "")
                batch_content_parts.append(f"\n\n=== {title} ===\n\n{content}")
            batch_content = "".join(batch_content_parts)

            if user_prompt_template:
                user_message = user_prompt_template.replace("{content}", batch_content)
            else:
                user_message = batch_content

            messages = [{"role": "system", "content": system_prompt}]
            recent_context = context_messages[-2:]
            messages.extend(recent_context)
            messages.append({"role": "user", "content": user_message})

            logger.info(f"[{task_id}] [{file_name}] 处理批次 {batch_num}/{total_batches}，章节 {batch_start + 1}-{batch_end}")

            success, result = _call_llm_batch_with_retry(
                config=config,
                messages=messages,
                task_id=task_id,
                delay_min=delay_min,
                delay_max=delay_max,
                max_retries=_safe_int(config.get("batchRetryCount", 3), 3, min_value=1, max_value=10)
            )

            if success:
                assistant_message = _extract_assistant_content(result)
                context_messages.append({"role": "user", "content": user_message})
                context_messages.append({"role": "assistant", "content": assistant_message})

                _append_file_result(task_id, file_idx, {
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
                })

                _update_task_aggregate_progress(task_id)

                if batch_index < total_batches - 1:
                    delay = _random_delay_seconds(delay_min, delay_max)
                    logger.info(f"[{task_id}] [{file_name}] 批次 {batch_num} 完成，等待 {delay} 秒处理下一批")
                    if not _sleep_with_cancel_check(task_id, delay):
                        _mark_file_terminal(task_id, file_idx, "cancelled", "任务已取消")
                        fingerprint = file_info.get("fingerprint")
                        _release_dedup_if_terminal(fingerprint, "cancelled")
                        return
            else:
                logger.error(f"[{task_id}] [{file_name}] 批次 {batch_num} 最终失败: {result}")
                _append_file_result(task_id, file_idx, {
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
                })
                _update_task_aggregate_progress(task_id)

        # 单本完成后上传
        _upload_single_novel_result(task_id, file_idx)
        current_task = task_store.get_task_ref(task_id)
        if current_task and current_task.get("status") != "cancelled":
            file_status = "completed" if files[file_idx].get("failed", 0) == 0 else "partial_failed"
            _mark_file_terminal(task_id, file_idx, file_status, "处理完成")
            fingerprint = file_info.get("fingerprint")
            _release_dedup_if_terminal(fingerprint, file_status)

        _update_task_status_if_finished(task_id)

    except Exception as e:
        logger.error(f"[{task_id}] 单本书处理异常 [{file_name}]: {e}", exc_info=True)
        _mark_file_terminal(task_id, file_idx, "failed", str(e))
        fingerprint = file_info.get("fingerprint")
        _release_dedup_if_terminal(fingerprint, "failed")
        _update_task_status_if_finished(task_id)


def _upload_single_novel_result(task_id: str, file_idx: int):
    task = task_store.get_task(task_id)
    if not task:
        return

    files = task.get("files", [])
    if not (0 <= file_idx < len(files)):
        return

    file_info = files[file_idx]
    config = file_info.get("resolved_config", {})
    default_config = task.get("default_config", {})

    hf_token = config.get("hfToken") or default_config.get("hfToken", "")
    hf_dataset = config.get("hfDataset") or default_config.get("hfDataset", "")

    if not hf_token or not hf_dataset:
        logger.warning(f"[{task_id}] 未配置 HF Token/Dataset，跳过上传")
        task_store.update_task(task_id, {"result_persist_error": "未配置HF存储"})
        return

    file_name = file_info.get("file_name", "未命名")
    safe_name = _sanitize_filename(file_name)
    filename = f"{safe_name}-节奏.txt"

    try:
        text_parts = [
            f"小说: {file_name}",
            f"任务ID: {task_id}",
            f"处理时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"{'=' * 60}\n"
        ]

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
        from app.services.hf_dataset_service import hf_dataset_service
        hf_dataset_service.upload_text_file(hf_token, hf_dataset, filename, content)

        uploaded = task.get("result_files", [])
        if filename not in uploaded:
            uploaded.append(filename)
        task_store.update_task(task_id, {
            "result_persisted": True,
            "result_files": uploaded
        })
        logger.info(f"[{task_id}] 小说 [{file_name}] 结果已上传到 HF 数据集: {filename}")

    except Exception as e:
        logger.error(f"[{task_id}] 上传小说结果失败 [{file_name}]: {e}", exc_info=True)
        task_store.update_task(task_id, {"result_persist_error": str(e)})


# ==================== 路由注册 ====================

def register_routes(app):
    _ensure_workers()

    @app.route('/')
    def index():
        return send_from_directory('static', 'index.html')

    @app.route('/api/health')
    def health():
        worker_stats = _get_worker_stats()
        return jsonify({
            'status': 'ok',
            'timestamp': int(time.time() * 1000),
            'max_concurrent': MAX_CONCURRENT_TASKS,
            'thread_pool_size': DEFAULT_THREAD_POOL_SIZE,
            'batch_size': DEFAULT_BATCH_SIZE,
            'batch_delay_min': BATCH_DELAY_MIN,
            'batch_delay_max': BATCH_DELAY_MAX,
            'workerCount': worker_stats['workerCount'],
            'aliveWorkers': worker_stats['aliveWorkers'],
            'queueSize': worker_stats['queueSize'],
        })

    # ==================== 设置 ====================

    @app.route('/api/settings', methods=['GET'])
    def get_settings():
        settings = app_settings_service.get_default_runtime_config()
        settings['batchDelayMin'] = BATCH_DELAY_MIN
        settings['batchDelayMax'] = BATCH_DELAY_MAX
        return jsonify({
            'success': True,
            'settings': settings
        })

    @app.route('/api/settings/update', methods=['POST'])
    def update_settings():
        data = request.json or {}
        app_settings_service.update_user_defaults(data)
        return jsonify({'success': True, 'settings': app_settings_service.get_default_runtime_config()})

    @app.route('/api/set-concurrent', methods=['POST'])
    def set_concurrent():
        global MAX_CONCURRENT_TASKS
        data = request.json or {}
        value = _safe_int(data.get('maxConcurrent', 10), 10, min_value=1, max_value=50)
        MAX_CONCURRENT_TASKS = value
        logger.info(f"maxConcurrent 调整为: {value}")
        return jsonify({'success': True, 'maxConcurrent': value})

    @app.route('/api/set-thread-pool', methods=['POST'])
    def set_thread_pool():
        data = request.json or {}
        value = _safe_int(data.get('threadPoolSize', 10), 10, min_value=1, max_value=100)
        _restart_workers(value)
        logger.info(f"工作线程数调整为: {value}")
        return jsonify({'success': True, 'threadPoolSize': value})

    @app.route('/api/set-batch-delay', methods=['POST'])
    def set_batch_delay():
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
        worker_stats = _get_worker_stats()
        return jsonify({
            'success': True,
            'maxConcurrent': MAX_CONCURRENT_TASKS,
            'threadPoolSize': DEFAULT_THREAD_POOL_SIZE,
            'batchSize': DEFAULT_BATCH_SIZE,
            'batchDelayMin': BATCH_DELAY_MIN,
            'batchDelayMax': BATCH_DELAY_MAX,
            'taskCount': task_store.task_count(),
            'queueSize': worker_stats['queueSize'],
            'aliveWorkers': worker_stats['aliveWorkers'],
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
            "systemPrompt": data.get('systemPrompt', ''),
            "batchSystemPrompt": data.get('batchSystemPrompt', ''),
            "batchUserPromptTemplate": data.get('batchUserPromptTemplate', ''),
            "batchSize": data.get('batchSize', ''),
            "model": data.get('model', ''),
            "temperature": data.get('temperature', ''),
            "topP": data.get('topP', ''),
            "contextRounds": data.get('contextRounds', ''),
            "maxOutputTokens": data.get('maxOutputTokens', ''),
            "apiHost": data.get('apiHost', ''),
            "apiKey": data.get('apiKey', ''),
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

        config = app_settings_service.resolve_config_from_id(user_id, config_id)
        system_prompt = config.get('systemPrompt', '')
        context_rounds = _safe_int(config.get('contextRounds', 100), 100, min_value=0)

        user_msg = {'role': 'user', 'content': user_message, 'time': int(time.time() * 1000)}
        conversation_store.add_message(user_id, conv_id, user_msg)

        conv = conversation_store.get_conversation(user_id, conv_id)
        history = conv.get('messages', []) if conv else []
        messages = _build_messages(system_prompt, history[:-1], user_message, context_rounds)

        success, result = _call_llm_api(config, messages, use_stream=False)

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
        data = request.json or {}
        user_id = data.get('userId', 'default')
        files = data.get('files', [])
        batch_size = _safe_int(data.get('batchSize', DEFAULT_BATCH_SIZE), DEFAULT_BATCH_SIZE, min_value=1)
        delay_min = _safe_int(data.get('delayMin', BATCH_DELAY_MIN), BATCH_DELAY_MIN, min_value=0)
        delay_max = _safe_int(data.get('delayMax', BATCH_DELAY_MAX), BATCH_DELAY_MAX, min_value=delay_min)

        if not files:
            return jsonify({'success': False, 'error': '没有文件可处理'}), 400

        task_id = str(uuid.uuid4())
        task_files = []
        total_chapters = 0
        duplicate_files = []

        default_config = app_settings_service.get_full_config()

        for f in files:
            chapters = f.get('chapters', []) or []
            if not chapters:
                continue

            config_id = f.get('configId', '')
            config_name = f.get('configName', '默认配置')
            file_batch_size = _safe_int(f.get('batchSize', batch_size), batch_size, min_value=1)
            start_chapter = _safe_int(f.get('startChapter', 1), 1, min_value=1)
            end_chapter = f.get('endChapter')
            end_chapter = _safe_int(end_chapter, len(chapters), min_value=1, max_value=max(1, len(chapters)))

            resolved_config = app_settings_service.resolve_config_from_id(user_id, config_id)
            if resolved_config.get('batchSize'):
                file_batch_size = _safe_int(resolved_config['batchSize'], file_batch_size, min_value=1)

            if config_id and config_id != "__default__":
                cfg = conversation_config_store.get_config_full(user_id, config_id)
                if cfg:
                    config_name = cfg.get('name', config_name)

            selected_chapters, actual_start, actual_end = _slice_chapters(chapters, start_chapter, end_chapter)
            if not selected_chapters:
                continue

            file_name = f.get('fileName', '未命名')
            fingerprint = _build_book_fingerprint(
                file_name=file_name,
                chapters=selected_chapters,
                config_id=config_id,
                batch_size=file_batch_size,
                start_chapter=actual_start,
                end_chapter=actual_end
            )

            duplicate = _check_duplicate_fingerprint(fingerprint)
            if duplicate:
                duplicate_files.append({
                    "fileName": file_name,
                    "taskId": duplicate.get("task_id"),
                    "status": duplicate.get("status"),
                    "message": f"《{file_name}》已在队列或处理中"
                })
                continue

            task_files.append({
                "file_name": file_name,
                "config_id": config_id,
                "config_name": config_name,
                "chapters": selected_chapters,
                "batch_size": file_batch_size,
                "delay_min": delay_min,
                "delay_max": delay_max,
                "results": [],
                "completed": 0,
                "failed": 0,
                "total": len(selected_chapters),
                "status": "queued",
                "message": "排队中...",
                "start_chapter": actual_start,
                "end_chapter": actual_end,
                "fingerprint": fingerprint,
                "resolved_config": resolved_config,
            })
            total_chapters += len(selected_chapters)

        if not task_files:
            if duplicate_files:
                return jsonify({
                    'success': False,
                    'error': '提交内容全部重复，未进入队列',
                    'duplicates': duplicate_files
                }), 409
            return jsonify({'success': False, 'error': '没有可处理文件'}), 400

        task_data = {
            'task_id': task_id,
            'user_id': user_id,
            'status': 'pending',
            'files': task_files,
            'total_chapters': total_chapters,
            'completed_chapters': 0,
            'failed_chapters': 0,
            'progress': f'0/{total_chapters}',
            'message': '等待排队...',
            'created_at': time.time(),
            'default_config': default_config,
            'result_files': []
        }
        task_store.create_task(task_id, task_data)

        for idx, file_info in enumerate(task_files):
            _register_fingerprint(file_info["fingerprint"], task_id, file_info["file_name"], "queued")
            _book_queue.put({
                "task_id": task_id,
                "file_idx": idx
            })

        _update_task_aggregate_progress(task_id)

        return jsonify({
            'success': True,
            'taskId': task_id,
            'totalChapters': total_chapters,
            'queuedFiles': len(task_files),
            'duplicateFiles': duplicate_files
        })

    @app.route('/api/batch/cancel', methods=['POST'])
    def batch_cancel():
        data = request.json or {}
        task_id = data.get('taskId')
        if not task_id:
            return jsonify({'success': False, 'error': '缺少taskId'}), 400

        task = task_store.get_task_ref(task_id)
        if not task:
            return jsonify({'success': False, 'error': '任务不存在'}), 404

        task["status"] = "cancelled"
        task["message"] = "任务已取消"

        for f in task.get("files", []):
            if f.get("status") in ("queued", "processing"):
                f["status"] = "cancelled"
                fingerprint = f.get("fingerprint")
                _release_dedup_if_terminal(fingerprint, "cancelled")

        _update_task_status_if_finished(task_id)
        return jsonify({'success': True})

    @app.route('/api/batch/resume', methods=['POST'])
    def batch_resume():
        return jsonify({
            'success': False,
            'error': '当前重构版本暂未开放 resume，请重新提交需要重跑的章节范围'
        }), 400

    # ==================== 任务管理 ====================

    @app.route('/api/tasks', methods=['GET'])
    def get_tasks():
        summary = task_store.get_all_tasks_summary()
        return jsonify({'success': True, 'tasks': summary})

    @app.route('/api/task/<task_id>', methods=['GET'])
    def get_task_detail(task_id):
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
        task = task_store.get_task(task_id)
        if not task:
            return jsonify({'success': False, 'error': '任务不存在'}), 404

        files = task.get("files", [])
        if file_idx < 0 or file_idx >= len(files):
            return jsonify({'success': False, 'error': '文件索引无效'}), 400

        f = files[file_idx]
        file_name = f.get('file_name', '未命名')
        safe_name = _sanitize_filename(file_name)

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