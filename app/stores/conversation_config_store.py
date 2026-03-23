import copy
import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_TEMPLATE = {
    "id": "",
    "name": "",
    "apiHost": "",
    "apiKey": "",
    "model": "",
    "systemPrompt": "",
    "batchSystemPrompt": "",
    "batchUserPromptTemplate": "",
    "temperature": "",
    "topP": "",
    "contextRounds": "",
    "maxOutputTokens": "",
    "batchSize": "",
    "hfToken": "",
    "hfDataset": "",
    "createdAt": 0,
    "updatedAt": 0,
}


def _now_ms():
    return int(time.time() * 1000)


def _safe_str(v):
    if v is None:
        return ""
    return str(v)


def _mask_secret(secret: str) -> str:
    secret = _safe_str(secret).strip()
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * (len(secret) - 8)}{secret[-4:]}"


class ConversationConfigStore:
    """
    最终版配置存储：
    - 按 user_id 存储用户配置
    - 每条配置统一结构
    - list 接口返回安全版本（掩码 apiKey）
    - get_config_full 返回完整配置
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._configs_by_user = {}

    # ==================== 内部工具 ====================

    def _ensure_user_bucket(self, user_id: str):
        user_id = user_id or "default"
        if user_id not in self._configs_by_user:
            self._configs_by_user[user_id] = {}
        return self._configs_by_user[user_id]

    def _normalize_input_config(self, config_id: str, data: dict):
        data = data or {}
        now = _now_ms()

        normalized = copy.deepcopy(DEFAULT_CONFIG_TEMPLATE)
        normalized["id"] = _safe_str(config_id or data.get("id") or str(uuid.uuid4())).strip()
        normalized["name"] = _safe_str(data.get("name")).strip() or "未命名配置"

        # 核心执行字段
        normalized["apiHost"] = _safe_str(data.get("apiHost")).strip()
        normalized["apiKey"] = _safe_str(data.get("apiKey")).strip()
        normalized["model"] = _safe_str(data.get("model")).strip()

        # Prompt
        normalized["systemPrompt"] = _safe_str(data.get("systemPrompt"))
        normalized["batchSystemPrompt"] = _safe_str(data.get("batchSystemPrompt"))
        normalized["batchUserPromptTemplate"] = _safe_str(data.get("batchUserPromptTemplate"))

        # 采样/输出参数
        normalized["temperature"] = data.get("temperature", "")
        normalized["topP"] = data.get("topP", "")
        normalized["contextRounds"] = data.get("contextRounds", "")
        normalized["maxOutputTokens"] = data.get("maxOutputTokens", "")
        normalized["batchSize"] = data.get("batchSize", "")

        # HF
        normalized["hfToken"] = _safe_str(data.get("hfToken")).strip()
        normalized["hfDataset"] = _safe_str(data.get("hfDataset")).strip()

        normalized["createdAt"] = data.get("createdAt") or now
        normalized["updatedAt"] = now

        return normalized

    def _to_safe_public_config(self, config: dict):
        if not config:
            return None

        safe_cfg = copy.deepcopy(config)
        safe_cfg["apiKeyMasked"] = _mask_secret(safe_cfg.get("apiKey", ""))
        safe_cfg["hfTokenMasked"] = _mask_secret(safe_cfg.get("hfToken", ""))

        # 列表接口不直接返回明文
        safe_cfg["apiKey"] = ""
        safe_cfg["hfToken"] = ""

        return safe_cfg

    # ==================== 对外接口 ====================

    def get_user_configs_safe(self, user_id: str):
        """
        返回给前端列表页的安全配置：
        - 包含主要字段
        - apiKey / hfToken 掩码，不返回明文
        """
        user_id = user_id or "default"
        with self._lock:
            bucket = self._ensure_user_bucket(user_id)
            items = list(bucket.values())
            items.sort(key=lambda x: (x.get("updatedAt", 0), x.get("createdAt", 0)), reverse=True)
            return [self._to_safe_public_config(item) for item in items]

    def get_config_full(self, user_id: str, config_id: str):
        """
        返回完整配置，供后端执行使用
        """
        user_id = user_id or "default"
        config_id = _safe_str(config_id).strip()
        if not config_id:
            return None

        with self._lock:
            bucket = self._ensure_user_bucket(user_id)
            item = bucket.get(config_id)
            if not item:
                return None
            return copy.deepcopy(item)

    def save_config(self, user_id: str, config_id_or_data, data: dict = None):
        """
        兼容两种调用方式：
        1. save_config(user_id, config_id, data)
        2. save_config(user_id, data)
        """
        user_id = user_id or "default"

        if isinstance(config_id_or_data, dict):
            data = config_id_or_data
            config_id = data.get("id") or str(uuid.uuid4())
        else:
            config_id = config_id_or_data

        normalized = self._normalize_input_config(config_id, data or {})

        with self._lock:
            bucket = self._ensure_user_bucket(user_id)
            old = bucket.get(normalized["id"])
            if old:
                normalized["createdAt"] = old.get("createdAt") or normalized["createdAt"]
            bucket[normalized["id"]] = normalized

        logger.info(
            f"保存配置成功: user={user_id}, id={normalized['id']}, "
            f"name={normalized['name']!r}, "
            f"apiHost={normalized.get('apiHost')!r}, "
            f"model={normalized.get('model')!r}, "
            f"batchSystemPrompt_len={len(normalized.get('batchSystemPrompt') or '')}, "
            f"systemPrompt_len={len(normalized.get('systemPrompt') or '')}"
        )

        return {
            "success": True,
            "id": normalized["id"],
            "config": self._to_safe_public_config(normalized)
        }

    def delete_config(self, user_id: str, config_id: str):
        user_id = user_id or "default"
        config_id = _safe_str(config_id).strip()
        if not config_id:
            return {"success": False, "error": "缺少配置ID"}

        with self._lock:
            bucket = self._ensure_user_bucket(user_id)
            existed = config_id in bucket
            if existed:
                del bucket[config_id]

        if existed:
            logger.info(f"删除配置成功: user={user_id}, id={config_id}")
            return {"success": True}
        return {"success": False, "error": "配置不存在"}

    def list_config_ids(self, user_id: str):
        user_id = user_id or "default"
        with self._lock:
            bucket = self._ensure_user_bucket(user_id)
            return list(bucket.keys())

    def clear_user_configs(self, user_id: str):
        user_id = user_id or "default"
        with self._lock:
            self._configs_by_user[user_id] = {}
        logger.info(f"已清空用户配置: user={user_id}")

    def export_user_configs_full(self, user_id: str):
        """
        调试/迁移用：导出用户完整配置
        """
        user_id = user_id or "default"
        with self._lock:
            bucket = self._ensure_user_bucket(user_id)
            return copy.deepcopy(list(bucket.values()))

    def import_user_configs_full(self, user_id: str, items: list):
        """
        调试/迁移用：导入用户完整配置
        """
        user_id = user_id or "default"
        items = items or []

        with self._lock:
            bucket = {}
            for item in items:
                normalized = self._normalize_input_config(item.get("id"), item)
                bucket[normalized["id"]] = normalized
            self._configs_by_user[user_id] = bucket

        logger.info(f"导入用户配置完成: user={user_id}, count={len(items)}")


conversation_config_store = ConversationConfigStore()