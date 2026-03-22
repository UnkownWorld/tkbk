import threading
import time
import logging

logger = logging.getLogger(__name__)

class CacheStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._data = {}
        self._summary_cache = None
        # 缓存大小限制
        self._max_size = 200
        # 缓存过期时间（秒）
        self._file_ttl = 300  # 文件缓存5分钟

    def get(self, key: str):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            # 检查是否过期
            if "expires_at" in entry and time.time() > entry["expires_at"]:
                del self._data[key]
                return None
            return entry.get("value")

    def set(self, key: str, value, ttl: int = None):
        with self._lock:
            # 如果超过大小限制，清理最旧的条目
            if len(self._data) >= self._max_size and key not in self._data:
                self._evict_oldest()
            entry = {"value": value, "time": time.time()}
            if ttl:
                entry["expires_at"] = time.time() + ttl
            self._data[key] = entry

    def delete(self, key: str):
        with self._lock:
            if key in self._data:
                del self._data[key]

    def _evict_oldest(self):
        """清理最旧的缓存条目（清理20%）"""
        if not self._data:
            return
        sorted_keys = sorted(self._data.keys(), key=lambda k: self._data[k].get("time", 0))
        evict_count = max(1, len(sorted_keys) // 5)
        for k in sorted_keys[:evict_count]:
            del self._data[k]
        logger.debug(f"缓存清理: 移除了 {evict_count} 个旧条目")

    def build_file_cache_key(self, dataset_id: str, filename: str) -> str:
        return f"{dataset_id}:{filename}"

    def set_file_content(self, dataset_id: str, filename: str, content: str):
        cache_key = self.build_file_cache_key(dataset_id, filename)
        self.set(cache_key, {"content": content}, ttl=self._file_ttl)

    def get_file_content(self, dataset_id: str, filename: str):
        cache_key = self.build_file_cache_key(dataset_id, filename)
        return self.get(cache_key)

    def set_tasks_summary(self, data: dict):
        with self._lock:
            self._summary_cache = {"data": data, "time": time.time()}

    def get_tasks_summary(self, ttl_seconds: int = 2):
        with self._lock:
            if not self._summary_cache:
                return None
            age = time.time() - self._summary_cache["time"]
            if age > ttl_seconds:
                return None
            return {"data": self._summary_cache["data"], "age": age}

    def clear_tasks_summary(self):
        with self._lock:
            self._summary_cache = None

    def clear_all(self):
        with self._lock:
            self._data.clear()
            self._summary_cache = None

    def size(self):
        with self._lock:
            return len(self._data)

cache_store = CacheStore()
