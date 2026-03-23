import threading
import time


class CacheStore:
    """
    最终版缓存存储：
    - 当前主要用于 HF 文本文件缓存
    - 内存级
    - 线程安全
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._data = {}

    # ==================== 基础键构建 ====================

    def build_file_cache_key(self, dataset_id: str, filename: str):
        dataset_id = dataset_id or ""
        filename = filename or ""
        return f"file::{dataset_id}::{filename}"

    # ==================== 通用接口 ====================

    def get(self, key: str):
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, value):
        with self._lock:
            self._data[key] = value

    def delete(self, key: str):
        with self._lock:
            if key in self._data:
                del self._data[key]

    def clear(self):
        with self._lock:
            self._data = {}

    # ==================== 文件缓存接口 ====================

    def get_file_content(self, dataset_id: str, filename: str):
        key = self.build_file_cache_key(dataset_id, filename)
        with self._lock:
            return self._data.get(key)

    def set_file_content(self, dataset_id: str, filename: str, content: str):
        key = self.build_file_cache_key(dataset_id, filename)
        with self._lock:
            self._data[key] = {
                "content": content or "",
                "cached_at": int(time.time() * 1000)
            }


cache_store = CacheStore()