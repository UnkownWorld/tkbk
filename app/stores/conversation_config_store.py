import threading
import logging

logger = logging.getLogger(__name__)

class ConversationConfigStore:
    """
    配置存储：每个配置是一个完整的 profile，包含提示词、API设置、HF设置。
    密钥(apiKey, hfToken)存储在服务端，绝不返回给前端。
    
    提示词配置分为：
    - 聊天功能：systemPrompt（系统提示词）
    - 批处理功能：batchSystemPrompt（系统提示词）+ batchUserPromptTemplate（用户提示词模板）+ batchSize（批次大小）
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._configs = {}  # {user_id: {config_id: config_data}}

    def get_user_configs_safe(self, user_id: str) -> list:
        """返回配置列表（不含密钥）"""
        with self._lock:
            configs = list(self._configs.get(user_id, {}).values())
            return [self._strip_secrets(c) for c in configs]

    def get_config_full(self, user_id: str, config_id: str) -> dict:
        """返回完整配置（含密钥），仅供服务端内部使用"""
        with self._lock:
            return self._configs.get(user_id, {}).get(config_id)

    def save_config(self, user_id: str, config_id: str, config_data: dict):
        """保存配置。如果 apiKey/hfToken 为空，保留旧值"""
        with self._lock:
            if user_id not in self._configs:
                self._configs[user_id] = {}

            existing = self._configs[user_id].get(config_id, {})

            # 密钥字段：空值表示不修改，保留旧值
            for secret_key in ("apiKey", "hfToken"):
                if not config_data.get(secret_key):
                    config_data[secret_key] = existing.get(secret_key, "")

            config_data["id"] = config_id
            self._configs[user_id][config_id] = config_data

    def delete_config(self, user_id: str, config_id: str):
        with self._lock:
            if user_id in self._configs and config_id in self._configs[user_id]:
                del self._configs[user_id][config_id]

    def _strip_secrets(self, config: dict) -> dict:
        """移除密钥，替换为 hasXxx 标志"""
        safe = {k: v for k, v in config.items() if k not in ("apiKey", "hfToken")}
        safe["hasApiKey"] = bool(config.get("apiKey"))
        safe["hasHfToken"] = bool(config.get("hfToken"))
        return safe


conversation_config_store = ConversationConfigStore()
