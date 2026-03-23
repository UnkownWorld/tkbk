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

# 全局配置（会在 register_routes(app) 时用 app.config 覆盖）
DEFAULT_THREAD_POOL_SIZE = 10
DEFAULT_BATCH_SIZE = 10
MAX_CONCURRENT_TASKS = 10
BATCH_DELAY_MIN = 15
BATCH_DELAY_MAX = 45

_http_session = http_requests.Session()

# ==================== 全局队列 / Worker ====================

_book_queue = queue.Queue()
_worker_threads = []
_worker_lock = threading.RLock()
_worker_generation = 0

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
        generation = _worker_generation
    return {
        "workerCount": len(_worker_threads),
        "aliveWorkers": alive,
        "queueSize": _book_queue.qsize(),
        "generation": generation,
    }


def _random_delay_seconds(delay_min: int, delay_max: int) -> int:
    delay_min = max(0, _safe_int(delay_min, BATCH_DELAY_MIN, min_value=0))
    delay_max = max(delay_min, _safe_int(delay_max, BATCH_DELAY_MAX, min_value=delay_min))
    if delay_max <= delay_min:
        return delay_min
    return random.randint(delay_min, delay_max)


def _sleep_with_cancel_check(task_id: str, seconds: int) -> bool:
    seconds = max(0, int(seconds))
    for _ in range(seconds):
        task = task_store.get_task_ref(task_id)
        if not task or task.get("status") == "cancelled":
            return False
        time.sleep(1)
    return True


# ==================== Worker 管理 ====================

def _worker_loop(worker_generation: int, worker_index: int):
    thread_name = threading.current_thread().name
    logger.info(f"[{thread_name}] Worker启动，generation={worker_generation}, index={worker_index}")

    while True:
        with _worker_lock:
            if worker_generation != _worker_generation:
                logger.info(f"[{thread_name}] Worker退出：generation已变更 {worker_generation} -> {_worker_generation}")
                return

        try:
            item = _book_queue.get(timeout=1)
        except queue.Empty:
            continue

        try:
            task_id = item.get("task_id")
            file_idx = item.get("file_idx")

            task = task_store.get_task_ref(task_id)
            if not task:
                _book_queue.task_done()
                continue

            files = task.get("files", [])
            if not (0 <= file_idx < len(files)):
                _book_queue.task_done()
                continue

            file_info = files[file_idx]
            fingerprint = file_info.get("fingerprint")

            if task.get("status") == "cancelled":
                _mark_file_terminal(task_id, file_idx, "cancelled", "任务已取消")
                _release_dedup_if_terminal(fingerprint, "cancelled")
                _update_task_status_if_finished(task_id)
                _book_queue.task_done()
                continue

            _mark_file_processing(task_id, file_idx)
            _set_dedup_status(fingerprint, task_id, file_info.get("file_name", ""), "processing")

            logger.info(
                f"[{thread_name}] 开始处理 "
                f"task={task_id} file_idx={file_idx} file={file_info.get('file_name')} "
                f"config_id={file_info.get('config_id')!r} config_name={file_info.get('config_name')!r}"
            )

            _process_single_book(task_id, file_idx)
            _update_task_status_if_finished(task_id)

        except Exception as e:
            logger.error(f"[{thread_name}] 处理书籍任务异常: {e}", exc_info=True)
        finally:
            _book_queue.task_done()


def _rebuild_workers(target_size: int):
    global _worker_threads, _worker_generation

    target_size = _safe_int(target_size, DEFAULT_THREAD_POOL_SIZE, min_value=1, max_value=100)

    with _worker_lock:
        _worker_generation += 1
        current_generation = _worker_generation
        _worker_threads = []

        for i in range(target_size):
            t = threading.Thread(
                target=_worker_loop,
                args=(current_generation, i),
                daemon=True,
                name=f"book-worker-{current_generation}-{i + 1}"
            )
            _worker_threads.append(t)
            t.start()

    logger.info(f"Worker池已重建：size={target_size}, generation={current_generation}")


def _ensure_workers():
    stats = _get_worker_stats()
    if stats["aliveWorkers"] != DEFAULT_THREAD_POOL_SIZE:
        _rebuild_workers(DEFAULT_THREAD_POOL_SIZE)


# ==================== 去重索引 ====================

def _build_book_fingerprint(file_name: str, chapters: list, config_id: str, batch_size: int, start_chapter: int, end_chapter: int) -> str:
    chapter_digest_parts = []
    for ch in chapters:
        title = ch.get("title", "")
        raw_slice = ch.get("raw_slice", "")
        content = raw_slice or ch.get("content", "")
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

def _normalize_chapter_line(line: str) -> str:
    if line is None:
        return ""
    line = str(line).replace("\ufeff", "")
    line = line.replace("\u3000", " ")
    line = re.sub(r"[ \t]+", " ", line)
    return line.strip()


def _is_strong_chapter_title(normalized: str) -> bool:
    if not normalized:
        return False

    patterns = [
        r"^第\s*[0-9零一二三四五六七八九十百千万两〇]+
           \s*[章回卷节集部篇册幕季]
           (?:\s*[:：\-—\.、]\s*.*)?$",
        r"^(序章|楔子|引子|终章|尾声|后记|番外|附录|卷首语|卷尾语)$",
        r"^(序章|楔子|引子|终章|尾声|后记|番外|附录|卷首语|卷尾语)
           \s*[:：\-—\.、]\s*.*$",
    ]

    for p in patterns:
        if re.match(p, normalized, re.IGNORECASE | re.VERBOSE):
            return True
    return False


def _is_weak_chapter_title(normalized: str) -> bool:
    if not normalized:
        return False

    patterns = [
        r"^\d+\s*[、.．\-—]\s*.+$",
        r"^\d+\s+.+$",
    ]
    for p in patterns:
        if re.match(p, normalized, re.IGNORECASE):
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

    normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized_content.split("\n")
    strong_count, weak_count = _count_chapter_signal_lines(lines)

    chapters = []
    current_title = None
    current_start_line = None

    for idx, raw_line in enumerate(lines):
        stripped = _normalize_chapter_line(raw_line)
        if not stripped:
            continue

        if _is_chapter_title_with_context(lines, idx, strong_count, weak_count):
            if current_title is not None and current_start_line is not None:
                end_line = idx
                chapter_lines = lines[current_start_line:end_line]
                chapter_text = "\n".join(chapter_lines).strip("\n")
                chapters.append({
                    "title": current_title,
                    "content": chapter_text,
                    "raw_slice": chapter_text,
                    "index": len(chapters) + 1,
                    "start_line": current_start_line,
                    "end_line": end_line,
                })

            current_title = stripped
            current_start_line = idx

    if current_title is not None and current_start_line is not None:
        chapter_lines = lines[current_start_line:]
        chapter_text = "\n".join(chapter_lines).strip("\n")
        chapters.append({
            "title": current_title,
            "content": chapter_text,
            "raw_slice": chapter_text,
            "index": len(chapters) + 1,
            "start_line": current_start_line,
            "end_line": len(lines),
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


def _build_batch_content_from_raw_lines(raw_lines: list, all_selected_chapters: list, batch_start: int, batch_end: int) -> str:
    if not all_selected_chapters or batch_start >= batch_end:
        return ""

    batch_chapters = all_selected_chapters[batch_start:batch_end]
    if not batch_chapters:
        return ""

    start_line = _safe_int(batch_chapters[0].get("start_line", 0), 0, min_value=0, max_value=len(raw_lines))

    if batch_end < len(all_selected_chapters):
        next_start_line = _safe_int(all_selected_chapters[batch_end].get("start_line", len(raw_lines)), len(raw_lines), min_value=0, max_value=len(raw_lines))
        end_line = next_start_line
    else:
        end_line = len(raw_lines)

    if end_line < start_line:
        end_line = start_line

    return "\n".join(raw_lines[start_line:end_line]).strip("\n")


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
    raw_content = file_info.get("raw_content", "") or ""
    raw_lines = raw_content.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    if not chapters:
        _mark_file_terminal(task_id, file_idx, "failed", "没有可处理章节")
        fingerprint = file_info.get("fingerprint")
        _release_dedup_if_terminal(fingerprint, "failed")
        _update_task_status_if_finished(task_id)
        return

    system_prompt = (
        config.get("batchSystemPrompt")
        or config.get("systemPrompt")
        or "You are a helpful AI assistant."
    )
    user_prompt_template = config.get("batchUserPromptTemplate") or ""

    logger.info(
        f"[{task_id}] [{file_name}] 配置检查: "
        f"config_id={file_info.get('config_id')!r}, "
        f"config_name={file_info.get('config_name')!r}, "
        f"model={config.get('model')!r}, "
        f"batchSystemPrompt_len={len(config.get('batchSystemPrompt') or '')}, "
        f"systemPrompt_len={len(config.get('systemPrompt') or '')}, "
        f"batchUserPromptTemplate_len={len(config.get('batchUserPromptTemplate') or '')}"
    )
    logger.info(
        f"[{task_id}] [{file_name}] 实际使用system_prompt前120字符: {repr(system_prompt[:120])}"
    )

    context_messages = []

    total_chapters = len(chapters)
    total_batches = (total_chapters + batch_size - 1) // batch_size

    logger.info(
        f"[{task_id}] 开始处理单本小说 [{file_name}]，"
        f"共 {total_chapters} 章，{total_batches} 批，每批 {batch_size} 章，"
        f"当前worker总数={DEFAULT_THREAD_POOL_SIZE}"
    )

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

            batch_content = _build_batch_content_from_raw_lines(
                raw_lines=raw_lines,
                all_selected_chapters=chapters,
                batch_start=batch_start,
                batch_end=batch_end
            )

            if not batch_content.strip():
                logger.warning(
                    f"[{task_id}] [{file_name}] 批次 {batch_num}/{total_batches} 内容为空，"
                    f"章节 {batch_start + 1}-{batch_end}"
                )

            if user_prompt_template:
                user_message = user_prompt_template.replace("{content}", batch_content)
            else:
                user_message = batch_content

            messages = [{"role": "system", "content": system_prompt}]
            recent_context = context_messages[-2:]
            messages.extend(recent_context)
            messages.append({"role": "user", "content": user_message})

            logger.info(
                f"[{task_id}] [{file_name}] 处理批次 {batch_num}/{total_batches}，"
                f"章节 {batch_start + 1}-{batch_end}，"
                f"batch_content_len={len(batch_content)}"
            )

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
    global DEFAULT_THREAD_POOL_SIZE, MAX_CONCURRENT_TASKS

    # 关键修复：启动时同步 app.config，而不是永远写死10
    DEFAULT_THREAD_POOL_SIZE = _safe_int(
        app.config.get("MAX_THREAD_WORKERS", DEFAULT_THREAD_POOL_SIZE),
        DEFAULT_THREAD_POOL_SIZE,
        min_value=1,
        max_value=100
    )
    MAX_CONCURRENT_TASKS = _safe_int(
        app.config.get("MAX_CONCURRENT_TASKS", MAX_CONCURRENT_TASKS),
        MAX_CONCURRENT_TASKS,
        min_value=1,
        max_value=100
    )

    logger.info(
        f"register_routes 初始化: "
        f"DEFAULT_THREAD_POOL_SIZE={DEFAULT_THREAD_POOL_SIZE}, "
        f"MAX_CONCURRENT_TASKS={MAX_CONCURRENT_TASKS}"
    )

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
            'generation': worker_stats['generation'],
        })

    @app.route('/api/settings', methods=['GET'])
    def get_settings():
        data = app_settings_service.get_all_safe()
        data.update(_get_worker_stats())
        data["threadPoolSize"] = DEFAULT_THREAD_POOL_SIZE
        data["maxConcurrent"] = MAX_CONCURRENT_TASKS
        return jsonify({'success': True, 'settings': data})

    @app.route('/api/settings', methods=['POST'])
    def save_settings():
        data = request.json or {}
        result = app_settings_service.save_settings(data)
        return jsonify(result)

    @app.route('/api/set-thread-pool', methods=['POST'])
    def set_thread_pool():
        global DEFAULT_THREAD_POOL_SIZE, MAX_CONCURRENT_TASKS
        data = request.json or {}

        new_thread_pool = _safe_int(
            data.get('threadPoolSize', DEFAULT_THREAD_POOL_SIZE),
            DEFAULT_THREAD_POOL_SIZE,
            min_value=1,
            max_value=100
        )
        new_max_concurrent = _safe_int(
            data.get('maxConcurrent', MAX_CONCURRENT_TASKS),
            MAX_CONCURRENT_TASKS,
            min_value=1,
            max_value=100
        )

        DEFAULT_THREAD_POOL_SIZE = new_thread_pool
        MAX_CONCURRENT_TASKS = new_max_concurrent

        _rebuild_workers(DEFAULT_THREAD_POOL_SIZE)

        logger.info(
            f"线程池设置更新：threadPoolSize={DEFAULT_THREAD_POOL_SIZE}, "
            f"maxConcurrent={MAX_CONCURRENT_TASKS}"
        )

        return jsonify({
            'success': True,
            'threadPoolSize': DEFAULT_THREAD_POOL_SIZE,
            'maxConcurrent': MAX_CONCURRENT_TASKS,
            **_get_worker_stats()
        })

    @app.route('/api/conversations', methods=['GET'])
    def get_conversations():
        user_id = request.args.get('userId', 'default')
        conversations = conversation_store.get_conversations(user_id)
        return jsonify({'success': True, 'conversations': conversations})

    @app.route('/api/conversation/new', methods=['POST'])
    def create_conversation():
        data = request.json or {}
        user_id = data.get('userId', 'default')
        title = data.get('title', '新对话')
        conv = conversation_store.create_conversation(user_id, title)
        return jsonify({'success': True, 'conversation': conv})

    @app.route('/api/conversation/<conv_id>', methods=['GET'])
    def get_conversation(conv_id):
        user_id = request.args.get('userId', 'default')
        conv = conversation_store.get_conversation(user_id, conv_id)
        if not conv:
            return jsonify({'success': False, 'error': '对话不存在'}), 404
        return jsonify({'success': True, 'conversation': conv})

    @app.route('/api/conversation/delete', methods=['POST'])
    def delete_conversation():
        data = request.json or {}
        user_id = data.get('userId', 'default')
        conv_id = data.get('conversationId')
        if not conv_id:
            return jsonify({'success': False, 'error': '缺少conversationId'}), 400
        ok = conversation_store.delete_conversation(user_id, conv_id)
        return jsonify({'success': ok})

    @app.route('/api/configs', methods=['GET'])
    def get_configs():
        user_id = request.args.get('userId', 'default')
        configs = conversation_config_store.get_user_configs_safe(user_id)
        return jsonify({'success': True, 'configs': configs})

    @app.route('/api/config/save', methods=['POST'])
    def save_config():
        data = request.json or {}
        user_id = data.get('userId', 'default')
        result = conversation_config_store.save_config(user_id, data)
        return jsonify(result)

    @app.route('/api/config/delete', methods=['POST'])
    def delete_config():
        data = request.json or {}
        user_id = data.get('userId', 'default')
        config_id = data.get('configId')
        if not config_id:
            return jsonify({'success': False, 'error': '缺少configId'}), 400
        result = conversation_config_store.delete_config(user_id, config_id)
        return jsonify(result)

    @app.route('/api/chat', methods=['POST'])
    def chat():
        data = request.json or {}
        user_id = data.get('userId', 'default')
        conv_id = data.get('conversationId')
        user_message = data.get('message', '')
        config_id = data.get('configId', '')

        if not conv_id:
            return jsonify({'success': False, 'error': '缺少conversationId'}), 400
        if not user_message:
            return jsonify({'success': False, 'error': '消息不能为空'}), 400

        config = app_settings_service.resolve_config_from_id(user_id, config_id)
        system_prompt = config.get('systemPrompt') or ''
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

            raw_content = f.get('content', '') or f.get('rawContent', '') or ''
            if not raw_content:
                logger.warning(f"[submit_batch] 文件={f.get('fileName')} 未携带原始content，批次切片可能不准确")

            config_id = f.get('configId', '')
            config_name = f.get('configName', '默认配置')
            file_batch_size = _safe_int(f.get('batchSize', batch_size), batch_size, min_value=1)
            start_chapter = _safe_int(f.get('startChapter', 1), 1, min_value=1)
            end_chapter = f.get('endChapter')
            end_chapter = _safe_int(end_chapter, len(chapters), min_value=1, max_value=max(1, len(chapters)))

            logger.info(
                f"[submit_batch] 文件={f.get('fileName')} 收到 "
                f"configId={config_id!r}, configName={config_name!r}, "
                f"batchSize={file_batch_size}, start={start_chapter}, end={end_chapter}"
            )

            resolved_config = app_settings_service.resolve_config_from_id(user_id, config_id)

            logger.info(
                f"[submit_batch] 文件={f.get('fileName')} resolved_config: "
                f"model={resolved_config.get('model')!r}, "
                f"batchSystemPrompt_len={len(resolved_config.get('batchSystemPrompt') or '')}, "
                f"systemPrompt_len={len(resolved_config.get('systemPrompt') or '')}"
            )

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
                "raw_content": raw_content,
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

        text_parts = [
            f"小说: {file_name}",
            f"任务ID: {task_id}",
            f"导出时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"{'='*60}\n"
        ]

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
        safe_name = _sanitize_filename(file_name)
        return jsonify({'success': True, 'content': content, 'filename': f"{safe_name}-节奏.txt"})

    @app.route('/api/hf/action', methods=['POST'])
    def hf_action():
        data = request.json or {}
        result = file_service.hf_action(data)
        return jsonify(result)

    @app.route('/api/hf/files', methods=['GET'])
    def hf_files():
        hf_token = request.args.get('hfToken', '')
        hf_dataset = request.args.get('hfDataset', '')
        files = file_service.list_dataset_files(hf_token, hf_dataset)
        return jsonify({'success': True, 'files': files})

    @app.route('/api/hf/result-files', methods=['GET'])
    def hf_result_files():
        hf_token = request.args.get('hfToken', '')
        hf_dataset = request.args.get('hfDataset', '')
        files = file_service.list_result_files(hf_token, hf_dataset)
        return jsonify({'success': True, 'files': files})

    @app.route('/api/hf/download', methods=['GET'])
    def hf_download():
        hf_token = request.args.get('hfToken', '')
        hf_dataset = request.args.get('hfDataset', '')
        filename = request.args.get('filename', '')
        result = file_service.download_dataset_file(hf_token, hf_dataset, filename)
        return jsonify(result)