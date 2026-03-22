import threading
from copy import deepcopy

class ConversationStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._conversations = {}

    def get_user_conversations(self, user_id: str):
        with self._lock:
            return deepcopy(self._conversations.get(user_id, {}))

    def get_conversation(self, user_id: str, conv_id: str):
        with self._lock:
            user_convs = self._conversations.get(user_id, {})
            conv = user_convs.get(conv_id)
            return deepcopy(conv) if conv else None

    def create_conversation(self, user_id: str, conv_id: str, conv_data: dict):
        with self._lock:
            if user_id not in self._conversations:
                self._conversations[user_id] = {}
            self._conversations[user_id][conv_id] = deepcopy(conv_data)
            return deepcopy(self._conversations[user_id][conv_id])

    def update_conversation(self, user_id: str, conv_id: str, patch: dict):
        with self._lock:
            if user_id not in self._conversations:
                return None
            if conv_id not in self._conversations[user_id]:
                return None
            self._conversations[user_id][conv_id].update(patch)
            return deepcopy(self._conversations[user_id][conv_id])

    def delete_conversation(self, user_id: str, conv_id: str):
        with self._lock:
            if user_id in self._conversations and conv_id in self._conversations[user_id]:
                del self._conversations[user_id][conv_id]
                return True
            return False

    def add_message(self, user_id: str, conv_id: str, message: dict):
        with self._lock:
            if user_id not in self._conversations:
                return None
            if conv_id not in self._conversations[user_id]:
                return None
            if "messages" not in self._conversations[user_id][conv_id]:
                self._conversations[user_id][conv_id]["messages"] = []
            self._conversations[user_id][conv_id]["messages"].append(message)
            return deepcopy(self._conversations[user_id][conv_id])

conversation_store = ConversationStore()
