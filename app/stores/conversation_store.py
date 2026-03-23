import copy
import threading


class ConversationStore:
    """
    最终版聊天会话存储：
    - 按 user_id 管理会话
    - 线程安全
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._conversations_by_user = {}

    # ==================== 内部工具 ====================

    def _ensure_user_bucket(self, user_id: str):
        user_id = user_id or "default"
        if user_id not in self._conversations_by_user:
            self._conversations_by_user[user_id] = {}
        return self._conversations_by_user[user_id]

    def _build_summary(self, conv: dict):
        messages = conv.get("messages", []) or []
        last_message = messages[-1] if messages else None

        return {
            "id": conv.get("id"),
            "title": conv.get("title", "新对话"),
            "configId": conv.get("configId", ""),
            "created_at": conv.get("created_at"),
            "messageCount": len(messages),
            "lastMessageTime": last_message.get("time") if last_message else None,
            "lastMessagePreview": (last_message.get("content", "")[:80] if last_message else ""),
        }

    # ==================== 基础接口 ====================

    def create_conversation(self, user_id: str, conv_id: str, conv_data: dict):
        user_id = user_id or "default"
        with self._lock:
            bucket = self._ensure_user_bucket(user_id)
            bucket[conv_id] = conv_data

    def get_conversation(self, user_id: str, conv_id: str):
        user_id = user_id or "default"
        with self._lock:
            bucket = self._ensure_user_bucket(user_id)
            conv = bucket.get(conv_id)
            return copy.deepcopy(conv) if conv else None

    def get_conversation_ref(self, user_id: str, conv_id: str):
        user_id = user_id or "default"
        with self._lock:
            bucket = self._ensure_user_bucket(user_id)
            return bucket.get(conv_id)

    def delete_conversation(self, user_id: str, conv_id: str):
        user_id = user_id or "default"
        with self._lock:
            bucket = self._ensure_user_bucket(user_id)
            if conv_id in bucket:
                del bucket[conv_id]
                return True
            return False

    def add_message(self, user_id: str, conv_id: str, message: dict):
        user_id = user_id or "default"
        with self._lock:
            bucket = self._ensure_user_bucket(user_id)
            conv = bucket.get(conv_id)
            if not conv:
                return False
            conv.setdefault("messages", []).append(message)
            return True

    # ==================== 列表接口 ====================

    def get_user_conversations(self, user_id: str):
        user_id = user_id or "default"
        with self._lock:
            bucket = self._ensure_user_bucket(user_id)
            items = list(bucket.values())

        items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return [self._build_summary(item) for item in items]

    # ==================== 兼容旧命名 ====================

    def get_conversations(self, user_id: str):
        return self.get_user_conversations(user_id)

    # ==================== 调试辅助 ====================

    def clear_user_conversations(self, user_id: str):
        user_id = user_id or "default"
        with self._lock:
            self._conversations_by_user[user_id] = {}

    def clear_all(self):
        with self._lock:
            self._conversations_by_user = {}


conversation_store = ConversationStore()