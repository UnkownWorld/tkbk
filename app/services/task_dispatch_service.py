import copy
import hashlib
import json
import logging
import queue
import threading
import time
import uuid

from app.services.app_settings_service import app_settings_service
from app.services.batch_build_service import batch_build_service
from app.services.llm_service import llm_service
from app.services.result_service import result_service
from app.stores.task_store import task_store
from app.stores.conversation_config_store import conversation_config_store

logger = logging.getLogger(__name__)


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


def _normalize_text_for_hash(text: str) -> str:
    if text is None:
        return ""
    return str(text).replace("\r\n", "\n").replace("\r", "\n")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(_normalize_text_for_hash(text).encode("utf-8")).hexdigest()


class TaskDispatchService:
    """
    最终版任务调度服务：
    - 一本书一个队列项
    - worker 并行处理多本书
    - 书内批次串行
    - 后端统一解析章节/构建批次
    """

    def __init__(self):
        self._queue = queue.Queue()
        self._worker_threads = []
        self._worker_lock = threading.RLock()
        self._worker_generation = 0

        self._dedup_lock = threading.RLock()
        self._dedup_index = {}

        self._thread_pool_size = 10
        self._max_concurrent = 10

    # ==================== worker 池 ====================

    def configure(self, thread_pool_size: int = None, max_concurrent: int = None):
        if thread_pool_size is not None:
            self._thread_pool_size = _safe_int(thread_pool_size, self._thread_pool_size, min_value=1, max_value=100)
        if max_concurrent is not None:
            self._max_concurrent = _safe_int(max_concurrent, self._max_concurrent, min_value=1, max_value=100)

    def get_stats(self):
        with self._worker_lock:
            alive = sum(1 for t in self._worker_threads if t.is_alive())
            generation = self._worker_generation
            worker_count = len(self._worker_threads)

        return {
            "threadPoolSize": self._thread_pool_size,
            "maxConcurrent": self._max_concurrent,
            "workerCount": worker_count,
            "aliveWorkers": alive,
            "queueSize": self._queue.qsize(),
            "generation": generation,
        }

    def rebuild_workers(self, new_size: int = None):
        if new_size is not None:
            self._thread_pool_size = _safe_int(new_size, self._thread_pool_size, min_value=1, max_value=100)

        with self._worker_lock:
            self._worker_generation += 1
            generation = self._worker_generation
            self._worker_threads = []

            for idx in range(self._thread_pool_size):
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(generation, idx + 1),
                    daemon=True,
                    name=f"book-worker-g{generation}-{idx + 1}"
                )
                t.start()
                self._worker_threads.append(t)

        logger.info(f"Worker池已重建: generation={generation}, size={self._thread_pool_size}")

    def ensure_workers(self):
        stats = self.get_stats()
        if stats["aliveWorkers"] != self._thread_pool_size:
            self.rebuild_workers(self._thread_pool_size)

    def _worker_loop(self, generation: int, worker_index: int):
        logger.info(f"[worker {worker_index}] 启动 generation={generation}")

        while True:
            with self._worker_lock:
                if generation != self._worker_generation:
                    logger.info(f"[worker {worker_index}] generation 过期，退出")
                    return

            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            try:
                task_id = item.get("task_id")
                file_idx = item.get("file_idx")
                self._process_book_item(task_id, file_idx)
            except Exception as e:
                logger.error(f"[worker {worker_index}] 处理书籍异常: {e}", exc_info=True)
            finally:
                self._queue.task_done()

    # ==================== 去重 ====================

    def build_book_fingerprint(self, file_name: str, content: str, config_id: str, batch_size: int, start_chapter: int, end_chapter: int):
        payload = {
            "file_name": file_name or "",
            "content_hash": _sha256_text(content or ""),
            "config_id": config_id or "__default__",
            "batch_size": batch_size,
            "start_chapter": start_chapter,
            "end_chapter": end_chapter,
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def check_duplicate(self, fingerprint: str):
        with self._dedup_lock:
            item = self._dedup_index.get(fingerprint)
            if item and item.get("status") in ("queued", "processing"):
                return copy.deepcopy(item)
            return None

    def register_fingerprint(self, fingerprint: str, task_id: str, file_name: str, status: str = "queued"):
        with self._dedup_lock:
            self._dedup_index[fingerprint] = {
                "task_id": task_id,
                "file_name": file_name,
                "status": status,
                "updated_at": time.time(),
            }

    def set_fingerprint_status(self, fingerprint: str, status: str):
        if not fingerprint:
            return
        with self._dedup_lock:
            item = self._dedup_index.get(fingerprint)
            if item:
                item["status"] = status
                item["updated_at"] = time.time()

    # ==================== 任务构建 ====================

    def _resolve_config_name(self, user_id: str, config_id: str):
        if not config_id:
            return "默认配置"
        cfg = conversation_config_store.get_config_full(user_id, config_id)
        if not cfg:
            return "默认配置"
        return cfg.get("name", "默认配置")

    def _create_book_task(self, user_id: str, file_payload: dict):
        file_name = file_payload.get("fileName", "未命名")
        content = file_payload.get("content", "") or ""
        config_id = file_payload.get("configId", "") or ""
        start_chapter = file_payload.get("startChapter")
        end_chapter = file_payload.get("endChapter")
        batch_size = _safe_int(file_payload.get("batchSize", 10), 10, min_value=1)

        resolved_config = app_settings_service.resolve_book_config(user_id, config_id)
        config_name = self._resolve_config_name(user_id, config_id)

        if resolved_config.get("batchSize"):
            batch_size = _safe_int(resolved_config.get("batchSize"), batch_size, min_value=1)

        execution_plan = batch_build_service.build_book_execution_plan(
            file_name=file_name,
            content=content,
            config_id=config_id,
            batch_size=batch_size,
            start_chapter=start_chapter,
            end_chapter=end_chapter
        )

        total_chapters = execution_plan["total_chapters"]
        fingerprint = self.build_book_fingerprint(
            file_name=file_name,
            content=content,
            config_id=config_id,
            batch_size=batch_size,
            start_chapter=execution_plan["start_chapter"],
            end_chapter=execution_plan["end_chapter"]
        )

        return {
            "file_id": str(uuid.uuid4()),
            "file_name": file_name,
            "content_hash": _sha256_text(content),
            "raw_content": content,
            "config_id": config_id,
            "config_name": config_name,
            "resolved_config": resolved_config,
            "batch_size": batch_size,
            "start_chapter": execution_plan["start_chapter"],
            "end_chapter": execution_plan["end_chapter"],
            "all_chapters": execution_plan["all_chapters"],
            "selected_chapters": execution_plan["selected_chapters"],
            "total_chapters": total_chapters,
            "batches_plan": execution_plan["batches"],
            "batches": [],
            "completed_chapters": 0,
            "failed_chapters": 0,
            "status": "queued",
            "message": "排队中...",
            "fingerprint": fingerprint,
            "result_uploaded": False,
            "result_upload_error": "",
        }

    def submit_batch(self, user_id: str, files: list, delay_min: int = 15, delay_max: int = 45):
        user_id = user_id or "default"
        files = files or []

        if not files:
            return {"success": False, "error": "没有文件可处理"}

        task_id = str(uuid.uuid4())
        task_files = []
        duplicate_files = []
        total_chapters = 0

        default_config = app_settings_service.get_full_config()

        for file_payload in files:
            content = file_payload.get("content", "") or ""
            if not content.strip():
                continue

            book_task = self._create_book_task(user_id, file_payload)

            if not book_task["selected_chapters"]:
                logger.warning(f"[submit_batch] 文件={book_task['file_name']} 未识别到章节，跳过")
                continue

            duplicate = self.check_duplicate(book_task["fingerprint"])
            if duplicate:
                duplicate_files.append({
                    "fileName": book_task["file_name"],
                    "taskId": duplicate.get("task_id"),
                    "status": duplicate.get("status"),
                    "message": f"《{book_task['file_name']}》已在队列或处理中"
                })
                continue

            book_task["delay_min"] = _safe_int(delay_min, 15, min_value=0)
            book_task["delay_max"] = _safe_int(delay_max, 45, min_value=book_task["delay_min"])

            task_files.append(book_task)
            total_chapters += book_task["total_chapters"]

        if not task_files:
            if duplicate_files:
                return {
                    "success": False,
                    "error": "提交内容全部重复，未进入队列",
                    "duplicates": duplicate_files
                }
            return {"success": False, "error": "没有可处理文件"}

        task_data = {
            "task_id": task_id,
            "user_id": user_id,
            "status": "pending",
            "files": task_files,
            "total_chapters": total_chapters,
            "completed_chapters": 0,
            "failed_chapters": 0,
            "progress": f"0/{total_chapters}",
            "message": "等待排队...",
            "created_at": time.time(),
            "default_config": default_config,
            "result_files": []
        }

        task_store.create_task(task_id, task_data)

        for idx, book_task in enumerate(task_files):
            self.register_fingerprint(book_task["fingerprint"], task_id, book_task["file_name"], "queued")
            self._queue.put({
                "task_id": task_id,
                "file_idx": idx
            })

        self._update_task_aggregate_progress(task_id)

        return {
            "success": True,
            "taskId": task_id,
            "queuedFiles": len(task_files),
            "totalChapters": total_chapters,
            "duplicateFiles": duplicate_files,
        }

    # ==================== 状态更新 ====================

    def _mark_book_processing(self, task_id: str, file_idx: int):
        task = task_store.get_task_ref(task_id)
        if not task:
            return
        files = task.get("files", [])
        if 0 <= file_idx < len(files):
            files[file_idx]["status"] = "processing"
            files[file_idx]["message"] = "处理中..."

    def _mark_book_terminal(self, task_id: str, file_idx: int, status: str, message: str = ""):
        task = task_store.get_task_ref(task_id)
        if not task:
            return
        files = task.get("files", [])
        if 0 <= file_idx < len(files):
            files[file_idx]["status"] = status
            files[file_idx]["message"] = message

    def _append_batch_result(self, task_id: str, file_idx: int, batch_result: dict):
        task = task_store.get_task_ref(task_id)
        if not task:
            return

        files = task.get("files", [])
        if not (0 <= file_idx < len(files)):
            return

        book_task = files[file_idx]
        book_task.setdefault("batches", []).append(batch_result)

        chapter_count = _safe_int(batch_result.get("chapter_count", 0), 0, min_value=0)
        if batch_result.get("success"):
            book_task["completed_chapters"] = book_task.get("completed_chapters", 0) + chapter_count
        else:
            book_task["failed_chapters"] = book_task.get("failed_chapters", 0) + chapter_count

    def _update_task_aggregate_progress(self, task_id: str):
        task = task_store.get_task_ref(task_id)
        if not task:
            return

        total = 0
        completed = 0
        failed = 0
        queued = 0
        processing = 0

        for f in task.get("files", []):
            total += f.get("total_chapters", 0)
            completed += f.get("completed_chapters", 0)
            failed += f.get("failed_chapters", 0)
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

    def _update_task_status_if_finished(self, task_id: str):
        task = task_store.get_task_ref(task_id)
        if not task:
            return

        files = task.get("files", [])
        if not files:
            task["status"] = "completed"
            task["progress"] = "完成"
            task["message"] = "无文件"
            return

        statuses = [f.get("status", "queued") for f in files]
        terminal = {"completed", "failed", "partial_failed", "cancelled"}

        self._update_task_aggregate_progress(task_id)

        if task.get("status") == "cancelled":
            return

        if all(s in terminal for s in statuses):
            completed = task.get("completed_chapters", 0)
            failed = task.get("failed_chapters", 0)
            total = task.get("total_chapters", 0)

            if failed > 0:
                task["status"] = "partial_failed"
                task["message"] = f"批处理完成，但存在失败章节: 成功 {completed}/{total}，失败 {failed}"
            else:
                task["status"] = "completed"
                task["progress"] = "完成"
                task["message"] = f"批处理完成: {completed}/{total}"

    # ==================== 执行 ====================

    def _build_batch_messages(self, resolved_config: dict, batch_content: str):
        system_prompt = (
            resolved_config.get("batchSystemPrompt")
            or resolved_config.get("systemPrompt")
            or ""
        )
        user_template = resolved_config.get("batchUserPromptTemplate") or "{content}"
        user_message = user_template.replace("{content}", batch_content)

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

    def _is_cancelled(self, task_id: str):
        task = task_store.get_task_ref(task_id)
        return (not task) or task.get("status") == "cancelled"

    def _process_book_item(self, task_id: str, file_idx: int):
        task = task_store.get_task(task_id)
        if not task:
            return

        files = task.get("files", [])
        if not (0 <= file_idx < len(files)):
            return

        book_task = files[file_idx]
        fingerprint = book_task.get("fingerprint")

        if self._is_cancelled(task_id):
            self._mark_book_terminal(task_id, file_idx, "cancelled", "任务已取消")
            self.set_fingerprint_status(fingerprint, "cancelled")
            self._update_task_status_if_finished(task_id)
            return

        self._mark_book_processing(task_id, file_idx)
        self.set_fingerprint_status(fingerprint, "processing")

        file_name = book_task.get("file_name", "未命名")
        resolved_config = book_task.get("resolved_config", {}) or {}
        batches_plan = book_task.get("batches_plan", []) or []
        delay_min = _safe_int(book_task.get("delay_min", 15), 15, min_value=0)
        delay_max = _safe_int(book_task.get("delay_max", 45), 45, min_value=delay_min)

        logger.info(
            f"[{task_id}] 开始处理书籍: {file_name}, "
            f"config_id={book_task.get('config_id')!r}, "
            f"config_name={book_task.get('config_name')!r}, "
            f"model={resolved_config.get('model')!r}, "
            f"apiHost={resolved_config.get('apiHost')!r}, "
            f"batchSystemPrompt_len={len(resolved_config.get('batchSystemPrompt') or '')}, "
            f"systemPrompt_len={len(resolved_config.get('systemPrompt') or '')}"
        )

        try:
            for batch in batches_plan:
                if self._is_cancelled(task_id):
                    self._mark_book_terminal(task_id, file_idx, "cancelled", "任务已取消")
                    self.set_fingerprint_status(fingerprint, "cancelled")
                    self._update_task_status_if_finished(task_id)
                    return

                batch_index = batch["batch_index"]
                chapter_start = batch["chapter_start"]
                chapter_end = batch["chapter_end"]
                chapter_titles = batch.get("chapter_titles", [])
                batch_content = batch.get("content", "") or ""

                logger.info(
                    f"[{task_id}] [{file_name}] 批次 {batch_index} 开始: "
                    f"第{chapter_start}-{chapter_end}章, "
                    f"chapter_count={batch.get('chapter_count')}, "
                    f"batch_content_len={len(batch_content)}"
                )

                started_at = time.time()
                messages = self._build_batch_messages(resolved_config, batch_content)

                call_result = llm_service.call_with_retry(
                    config=resolved_config,
                    messages=messages,
                    max_retries=3,
                    delay_min=delay_min,
                    delay_max=delay_max,
                    cancel_check=lambda: self._is_cancelled(task_id),
                )
                finished_at = time.time()

                if call_result["success"]:
                    batch_result = result_service.build_batch_result(
                        batch_index=batch_index,
                        chapter_start=chapter_start,
                        chapter_end=chapter_end,
                        chapter_titles=chapter_titles,
                        success=True,
                        result_text=call_result.get("text", ""),
                        error="",
                        started_at=started_at,
                        finished_at=finished_at,
                    )
                else:
                    batch_result = result_service.build_batch_result(
                        batch_index=batch_index,
                        chapter_start=chapter_start,
                        chapter_end=chapter_end,
                        chapter_titles=chapter_titles,
                        success=False,
                        result_text="",
                        error=call_result.get("error", "未知错误"),
                        started_at=started_at,
                        finished_at=finished_at,
                    )

                self._append_batch_result(task_id, file_idx, batch_result)
                self._update_task_aggregate_progress(task_id)

            # 单书处理结束
            current_task = task_store.get_task_ref(task_id)
            if not current_task or current_task.get("status") == "cancelled":
                self._mark_book_terminal(task_id, file_idx, "cancelled", "任务已取消")
                self.set_fingerprint_status(fingerprint, "cancelled")
                self._update_task_status_if_finished(task_id)
                return

            # 上传结果
            upload_result = result_service.upload_single_book_result(
                task_id=task_id,
                book_task=book_task,
                default_config=task.get("default_config", {})
            )

            if upload_result.get("success"):
                book_task["result_uploaded"] = True
                book_task["result_upload_error"] = ""
                uploaded_name = upload_result.get("filename")
                if uploaded_name:
                    task.setdefault("result_files", [])
                    if uploaded_name not in task["result_files"]:
                        task["result_files"].append(uploaded_name)
            else:
                book_task["result_uploaded"] = False
                book_task["result_upload_error"] = upload_result.get("error", "")

            failed = book_task.get("failed_chapters", 0)
            final_status = "completed" if failed == 0 else "partial_failed"
            self._mark_book_terminal(task_id, file_idx, final_status, "处理完成")
            self.set_fingerprint_status(fingerprint, final_status)
            self._update_task_status_if_finished(task_id)

        except Exception as e:
            logger.error(f"[{task_id}] [{file_name}] 处理书籍异常: {e}", exc_info=True)
            self._mark_book_terminal(task_id, file_idx, "failed", str(e))
            self.set_fingerprint_status(fingerprint, "failed")
            self._update_task_status_if_finished(task_id)

    # ==================== 对外任务控制 ====================

    def cancel_task(self, task_id: str):
        task = task_store.get_task_ref(task_id)
        if not task:
            return {"success": False, "error": "任务不存在"}

        task["status"] = "cancelled"
        task["message"] = "任务已取消"

        for f in task.get("files", []):
            if f.get("status") in ("queued", "processing"):
                f["status"] = "cancelled"
                self.set_fingerprint_status(f.get("fingerprint"), "cancelled")

        self._update_task_status_if_finished(task_id)
        return {"success": True}


task_dispatch_service = TaskDispatchService()