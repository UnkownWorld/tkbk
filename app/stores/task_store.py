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

    def create_task(self, task_id: str, task_data: dict):
        with self._lock:
            task_data["created_at"] = task_data.get("created_at", time.time())
            self._tasks[task_id] = deepcopy(task_data)
            return deepcopy(self._tasks[task_id])

    def get_task(self, task_id: str):
        with self._lock:
            task = self._tasks.get(task_id)
            return deepcopy(task) if task else None

    def get_task_ref(self, task_id: str):
        """获取任务引用（不拷贝），用于内部高频更新"""
        with self._lock:
            return self._tasks.get(task_id)

    def update_task(self, task_id: str, patch: dict):
        with self._lock:
            if task_id not in self._tasks:
                return None
            self._tasks[task_id].update(patch)
            return deepcopy(self._tasks[task_id])

    def append_file_result(self, task_id: str, file_idx: int, result: dict):
        """向指定文件的 results 列表追加一条结果"""
        with self._lock:
            if task_id not in self._tasks:
                return None
            files = self._tasks[task_id].get("files", [])
            if file_idx < len(files):
                files[file_idx].setdefault("results", []).append(result)
                # 更新文件级进度
                if result.get("success"):
                    files[file_idx]["completed"] = files[file_idx].get("completed", 0) + 1
                else:
                    files[file_idx]["failed"] = files[file_idx].get("failed", 0) + 1
            return True

    def get_all_tasks_summary(self):
        with self._lock:
            summary = {}
            for task_id, task in self._tasks.items():
                files_info = []
                for f in task.get("files", []):
                    files_info.append({
                        "file_name": f.get("file_name"),
                        "config_name": f.get("config_name", "默认"),
                        "total": f.get("total", 0),
                        "completed": f.get("completed", 0),
                        "failed": f.get("failed", 0),
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
                    "files": files_info,
                    "result_persisted": task.get("result_persisted"),
                    "result_persist_error": task.get("result_persist_error"),
                }
            return summary

    def delete_task(self, task_id: str):
        with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                return True
            return False

    def cleanup_expired(self):
        now = time.time()
        with self._lock:
            expired = [tid for tid, t in self._tasks.items()
                       if now - t.get("created_at", 0) > self._task_ttl]
            for tid in expired:
                del self._tasks[tid]
            if expired:
                logger.info(f"清理过期任务: {len(expired)} 个")
            return len(expired)

    def task_count(self):
        with self._lock:
            return len(self._tasks)

task_store = TaskStore()
