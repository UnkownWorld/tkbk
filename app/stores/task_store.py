import threading
import time
import logging
from copy import deepcopy

logger = logging.getLogger(__name__)


class TaskStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._tasks = {}
        self._task_ttl = 86400  # 24小时过期

    # ==================== 基础操作 ====================

    def create_task(self, task_id: str, task_data: dict):
        with self._lock:
            task_data = deepcopy(task_data)
            task_data["created_at"] = task_data.get("created_at", time.time())
            self._tasks[task_id] = task_data
            return deepcopy(self._tasks[task_id])

    def get_task(self, task_id: str):
        with self._lock:
            task = self._tasks.get(task_id)
            return deepcopy(task) if task else None

    def get_task_ref(self, task_id: str):
        """
        获取任务引用（不拷贝），仅供后端内部高频更新使用。
        使用方必须遵循：
        - 只在明确知道自己在做什么时使用
        - 不要把返回对象长期缓存到外部
        """
        with self._lock:
            return self._tasks.get(task_id)

    def update_task(self, task_id: str, patch: dict):
        with self._lock:
            if task_id not in self._tasks:
                return None
            self._tasks[task_id].update(deepcopy(patch))
            return deepcopy(self._tasks[task_id])

    def delete_task(self, task_id: str):
        with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                return True
            return False

    def task_count(self):
        with self._lock:
            return len(self._tasks)

    # ==================== 文件级操作 ====================

    def get_file(self, task_id: str, file_idx: int):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            files = task.get("files", [])
            if not (0 <= file_idx < len(files)):
                return None
            return deepcopy(files[file_idx])

    def update_file(self, task_id: str, file_idx: int, patch: dict):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            files = task.get("files", [])
            if not (0 <= file_idx < len(files)):
                return None
            files[file_idx].update(deepcopy(patch))
            return deepcopy(files[file_idx])

    def append_file_result(self, task_id: str, file_idx: int, result: dict):
        """
        向指定文件追加处理结果。
        注意：
        - completed / failed 按章节数统计
        - result 应包含 chapter_count
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None

            files = task.get("files", [])
            if not (0 <= file_idx < len(files)):
                return None

            file_info = files[file_idx]
            result = deepcopy(result)
            file_info.setdefault("results", []).append(result)

            chapter_count = result.get("chapter_count", 0)
            try:
                chapter_count = int(chapter_count)
            except (TypeError, ValueError):
                chapter_count = 0

            if result.get("success"):
                file_info["completed"] = file_info.get("completed", 0) + chapter_count
            else:
                file_info["failed"] = file_info.get("failed", 0) + chapter_count

            return True

    def clear_file_results(self, task_id: str, file_idx: int):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            files = task.get("files", [])
            if not (0 <= file_idx < len(files)):
                return False

            files[file_idx]["results"] = []
            files[file_idx]["completed"] = 0
            files[file_idx]["failed"] = 0
            return True

    def replace_file_results(self, task_id: str, file_idx: int, results: list):
        """
        用新的 results 全量替换某文件结果，并重新计算 completed/failed（按章节数）
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            files = task.get("files", [])
            if not (0 <= file_idx < len(files)):
                return False

            safe_results = deepcopy(results or [])
            completed = 0
            failed = 0

            for r in safe_results:
                chapter_count = r.get("chapter_count", 0)
                try:
                    chapter_count = int(chapter_count)
                except (TypeError, ValueError):
                    chapter_count = 0

                if r.get("success"):
                    completed += chapter_count
                else:
                    failed += chapter_count

            files[file_idx]["results"] = safe_results
            files[file_idx]["completed"] = completed
            files[file_idx]["failed"] = failed
            return True

    def remove_file_results_from_batch(self, task_id: str, file_idx: int, start_batch: int):
        """
        删除某文件指定批次及之后的结果，便于未来做 resume / 重跑
        返回：
        {
            "removed_success": x,
            "removed_failed": y,
            "remaining_results": n
        }
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None

            files = task.get("files", [])
            if not (0 <= file_idx < len(files)):
                return None

            file_info = files[file_idx]
            old_results = file_info.get("results", [])

            keep_results = []
            removed_success = 0
            removed_failed = 0

            for r in old_results:
                batch_num = r.get("batch", 0)
                chapter_count = r.get("chapter_count", 0)
                try:
                    chapter_count = int(chapter_count)
                except (TypeError, ValueError):
                    chapter_count = 0

                if batch_num < start_batch:
                    keep_results.append(r)
                else:
                    if r.get("success"):
                        removed_success += chapter_count
                    else:
                        removed_failed += chapter_count

            file_info["results"] = keep_results
            file_info["completed"] = max(0, file_info.get("completed", 0) - removed_success)
            file_info["failed"] = max(0, file_info.get("failed", 0) - removed_failed)

            return {
                "removed_success": removed_success,
                "removed_failed": removed_failed,
                "remaining_results": len(keep_results)
            }

    # ==================== 统计 / 摘要 ====================

    def recalc_task_progress(self, task_id: str):
        """
        根据 files 重算任务总进度
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None

            total_chapters = 0
            completed_chapters = 0
            failed_chapters = 0

            for f in task.get("files", []):
                total_chapters += f.get("total", 0)
                completed_chapters += f.get("completed", 0)
                failed_chapters += f.get("failed", 0)

            task["total_chapters"] = total_chapters
            task["completed_chapters"] = completed_chapters
            task["failed_chapters"] = failed_chapters

            if task.get("status") != "completed":
                task["progress"] = f"{completed_chapters}/{total_chapters}"

            return {
                "total_chapters": total_chapters,
                "completed_chapters": completed_chapters,
                "failed_chapters": failed_chapters,
                "progress": task.get("progress")
            }

    def get_all_tasks_summary(self):
        """
        返回任务摘要，避免把超大章节正文 / 全量结果都返回给前端任务列表。
        """
        with self._lock:
            summary = {}

            for task_id, task in self._tasks.items():
                files_info = []
                queued_files = 0
                processing_files = 0
                completed_files = 0
                failed_files = 0

                for f in task.get("files", []):
                    file_status = f.get("status", "queued")
                    if file_status == "queued":
                        queued_files += 1
                    elif file_status == "processing":
                        processing_files += 1
                    elif file_status == "completed":
                        completed_files += 1
                    elif file_status in ("failed", "partial_failed", "cancelled"):
                        failed_files += 1

                    files_info.append({
                        "file_name": f.get("file_name"),
                        "config_name": f.get("config_name", "默认"),
                        "total": f.get("total", 0),
                        "completed": f.get("completed", 0),
                        "failed": f.get("failed", 0),
                        "status": file_status,
                        "message": f.get("message", ""),
                        "batch_size": f.get("batch_size"),
                        "start_chapter": f.get("start_chapter"),
                        "end_chapter": f.get("end_chapter"),
                    })

                summary[task_id] = {
                    "task_id": task_id,
                    "status": task.get("status"),
                    "progress": task.get("progress"),
                    "message": task.get("message"),
                    "created_at": task.get("created_at"),
                    "total_chapters": task.get("total_chapters", 0),
                    "completed_chapters": task.get("completed_chapters", 0),
                    "failed_chapters": task.get("failed_chapters", 0),
                    "queued_files": queued_files,
                    "processing_files": processing_files,
                    "completed_files": completed_files,
                    "failed_files": failed_files,
                    "files": files_info,
                    "result_persisted": task.get("result_persisted"),
                    "result_persist_error": task.get("result_persist_error"),
                }

            return summary

    # ==================== 清理 ====================

    def cleanup_expired(self):
        now = time.time()
        with self._lock:
            expired = [
                tid for tid, t in self._tasks.items()
                if now - t.get("created_at", 0) > self._task_ttl
            ]
            for tid in expired:
                del self._tasks[tid]
            if expired:
                logger.info(f"清理过期任务: {len(expired)} 个")
            return len(expired)


task_store = TaskStore()