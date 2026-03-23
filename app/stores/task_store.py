import copy
import threading


class TaskStore:
    """
    最终版任务存储：
    - 内存存储
    - 线程安全
    - 支持摘要和详情读取
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._tasks = {}

    # ==================== 基础操作 ====================

    def create_task(self, task_id: str, task_data: dict):
        with self._lock:
            self._tasks[task_id] = task_data

    def get_task(self, task_id: str):
        with self._lock:
            task = self._tasks.get(task_id)
            return copy.deepcopy(task) if task else None

    def get_task_ref(self, task_id: str):
        """
        返回原对象引用，供 worker 线程内部更新状态
        """
        with self._lock:
            return self._tasks.get(task_id)

    def delete_task(self, task_id: str):
        with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]

    def task_count(self):
        with self._lock:
            return len(self._tasks)

    def clear(self):
        with self._lock:
            self._tasks = {}

    # ==================== 列表摘要 ====================

    def _build_task_summary(self, task: dict):
        files = task.get("files", []) or []

        return {
            "task_id": task.get("task_id"),
            "user_id": task.get("user_id", "default"),
            "status": task.get("status", "pending"),
            "progress": task.get("progress", ""),
            "message": task.get("message", ""),
            "created_at": task.get("created_at"),
            "total_chapters": task.get("total_chapters", 0),
            "completed_chapters": task.get("completed_chapters", 0),
            "failed_chapters": task.get("failed_chapters", 0),
            "file_count": len(files),
            "files": [
                {
                    "file_id": f.get("file_id"),
                    "file_name": f.get("file_name"),
                    "config_id": f.get("config_id"),
                    "config_name": f.get("config_name"),
                    "status": f.get("status"),
                    "message": f.get("message", ""),
                    "start_chapter": f.get("start_chapter"),
                    "end_chapter": f.get("end_chapter"),
                    "total_chapters": f.get("total_chapters", 0),
                    "completed_chapters": f.get("completed_chapters", 0),
                    "failed_chapters": f.get("failed_chapters", 0),
                    "batch_size": f.get("batch_size"),
                    "result_uploaded": f.get("result_uploaded", False),
                }
                for f in files
            ]
        }

    def get_all_tasks_summary(self):
        with self._lock:
            tasks = list(self._tasks.values())

        tasks.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return [self._build_task_summary(task) for task in tasks]


task_store = TaskStore()