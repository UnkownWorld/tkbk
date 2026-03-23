import copy
import logging
import os

from app.stores.conversation_config_store import conversation_config_store

logger = logging.getLogger(__name__)


DEFAULT_RUNTIME_CONFIG = {
    "apiHost": "",
    "apiKey": "",
    "model": "gpt-5.4",
    "temperature": 0.7,
    "topP": 0.65,
    "contextRounds": 100,
    "maxOutputTokens": 1000000,
    "systemPrompt": "",
    "batchSystemPrompt": "",
    "batchUserPromptTemplate": "",
    "batchSize": 10,
    "hfToken": "",
    "hfDataset": "",
}


DEFAULT_BATCH_SYSTEM_PROMPT = (
    "你是一个小说节奏链条提取助手。"
    "必须严格遵守用户输入的提示词规则。"
    "你的任务仅是提取每章节的节奏链条，保留章节标题。"
    "只输出章节标题与节奏链条内容。"
    "使用“→”连接节奏点。"
    "禁止分析伏笔、意象、主题、写法、人物心理、象征、修辞。"
    "禁止总结、评价、解释、扩写。"
    "不要输出JSON，不要输出代码块。"
)

DEFAULT_BATCH_USER_PROMPT_TEMPLATE = "请严格按照系统要求，提取下面章节的节奏链条：\n\n{content}"


def _safe_str(v):
    if v is None:
        return ""
    return str(v)


def _safe_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class AppSettingsService:
    """
    最终版设置服务：
    - 统一系统默认配置
    - 提供 resolve_config_from_id
    - 统一环境变量 fallback
    """

    def __init__(self):
        self._runtime_defaults = copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
        self._load_env_defaults()

    # ==================== 环境变量 ====================

    def _load_env_defaults(self):
        self._runtime_defaults["apiHost"] = _safe_str(os.getenv("APP_API_HOST", self._runtime_defaults["apiHost"])).strip()
        self._runtime_defaults["apiKey"] = _safe_str(os.getenv("APP_API_KEY", self._runtime_defaults["apiKey"])).strip()
        self._runtime_defaults["model"] = _safe_str(os.getenv("APP_MODEL", self._runtime_defaults["model"])).strip() or "gpt-5.4"

        self._runtime_defaults["temperature"] = _safe_float(
            os.getenv("APP_TEMPERATURE", self._runtime_defaults["temperature"]),
            self._runtime_defaults["temperature"]
        )
        self._runtime_defaults["topP"] = _safe_float(
            os.getenv("APP_TOP_P", self._runtime_defaults["topP"]),
            self._runtime_defaults["topP"]
        )
        self._runtime_defaults["contextRounds"] = _safe_int(
            os.getenv("APP_CONTEXT_ROUNDS", self._runtime_defaults["contextRounds"]),
            self._runtime_defaults["contextRounds"]
        )
        self._runtime_defaults["maxOutputTokens"] = _safe_int(
            os.getenv("APP_MAX_OUTPUT_TOKENS", self._runtime_defaults["maxOutputTokens"]),
            self._runtime_defaults["maxOutputTokens"]
        )
        self._runtime_defaults["batchSize"] = _safe_int(
            os.getenv("APP_BATCH_SIZE", self._runtime_defaults["batchSize"]),
            self._runtime_defaults["batchSize"]
        )

        self._runtime_defaults["systemPrompt"] = _safe_str(
            os.getenv("APP_SYSTEM_PROMPT", self._runtime_defaults["systemPrompt"])
        )
        self._runtime_defaults["batchSystemPrompt"] = _safe_str(
            os.getenv("APP_BATCH_SYSTEM_PROMPT", DEFAULT_BATCH_SYSTEM_PROMPT)
        )
        self._runtime_defaults["batchUserPromptTemplate"] = _safe_str(
            os.getenv("APP_BATCH_USER_PROMPT_TEMPLATE", DEFAULT_BATCH_USER_PROMPT_TEMPLATE)
        )

        self._runtime_defaults["hfToken"] = _safe_str(
            os.getenv("APP_HF_TOKEN", self._runtime_defaults["hfToken"])
        ).strip()
        self._runtime_defaults["hfDataset"] = _safe_str(
            os.getenv("APP_HF_DATASET", self._runtime_defaults["hfDataset"])
        ).strip()

        logger.info(
            "已加载环境默认配置: "
            f"apiHost={self._runtime_defaults.get('apiHost')!r}, "
            f"model={self._runtime_defaults.get('model')!r}, "
            f"batchSystemPrompt_len={len(self._runtime_defaults.get('batchSystemPrompt') or '')}, "
            f"batchUserPromptTemplate_len={len(self._runtime_defaults.get('batchUserPromptTemplate') or '')}"
        )

    # ==================== 默认配置 ====================

    def get_default_runtime_config(self):
        return copy.deepcopy(self._runtime_defaults)

    def get_full_config(self):
        """
        兼容旧代码命名：返回完整系统默认配置
        """
        return self.get_default_runtime_config()

    def get_hf_token(self):
        return _safe_str(self._runtime_defaults.get("hfToken")).strip()

    def get_hf_dataset(self):
        return _safe_str(self._runtime_defaults.get("hfDataset")).strip()

    # ==================== 用户默认配置更新 ====================

    def update_user_defaults(self, data: dict):
        """
        更新系统运行默认值（当前版本为内存级）。
        若以后需要持久化，可在这里接入文件或数据库。
        """
        data = data or {}

        updated = copy.deepcopy(self._runtime_defaults)

        if "apiHost" in data:
            updated["apiHost"] = _safe_str(data.get("apiHost")).strip()
        if "apiKey" in data:
            updated["apiKey"] = _safe_str(data.get("apiKey")).strip()
        if "model" in data:
            updated["model"] = _safe_str(data.get("model")).strip() or updated["model"]

        if "temperature" in data:
            updated["temperature"] = _safe_float(data.get("temperature"), updated["temperature"])
        if "topP" in data:
            updated["topP"] = _safe_float(data.get("topP"), updated["topP"])
        if "contextRounds" in data:
            updated["contextRounds"] = _safe_int(data.get("contextRounds"), updated["contextRounds"])
        if "maxOutputTokens" in data:
            updated["maxOutputTokens"] = _safe_int(data.get("maxOutputTokens"), updated["maxOutputTokens"])
        if "batchSize" in data:
            updated["batchSize"] = _safe_int(data.get("batchSize"), updated["batchSize"])

        if "systemPrompt" in data:
            updated["systemPrompt"] = _safe_str(data.get("systemPrompt"))
        if "batchSystemPrompt" in data:
            updated["batchSystemPrompt"] = _safe_str(data.get("batchSystemPrompt"))
        if "batchUserPromptTemplate" in data:
            updated["batchUserPromptTemplate"] = _safe_str(data.get("batchUserPromptTemplate"))

        if "hfToken" in data:
            updated["hfToken"] = _safe_str(data.get("hfToken")).strip()
        if "hfDataset" in data:
            updated["hfDataset"] = _safe_str(data.get("hfDataset")).strip()

        self._runtime_defaults = updated

        logger.info(
            "更新系统默认配置成功: "
            f"apiHost={updated.get('apiHost')!r}, "
            f"model={updated.get('model')!r}, "
            f"batchSystemPrompt_len={len(updated.get('batchSystemPrompt') or '')}, "
            f"systemPrompt_len={len(updated.get('systemPrompt') or '')}"
        )

        return {
            "success": True,
            "settings": self.get_default_runtime_config()
        }

    # ==================== 配置解析 ====================

    def _merge_with_defaults(self, config: dict):
        base = self.get_default_runtime_config()
        config = config or {}

        merged = copy.deepcopy(base)

        for key, value in config.items():
            if value is None:
                continue

            # 对字符串字段，允许空串保留，但后续 prompt fallback 另行处理
            merged[key] = value

        # 统一类型修正
        merged["temperature"] = _safe_float(merged.get("temperature"), base["temperature"])
        merged["topP"] = _safe_float(merged.get("topP"), base["topP"])
        merged["contextRounds"] = _safe_int(merged.get("contextRounds"), base["contextRounds"])
        merged["maxOutputTokens"] = _safe_int(merged.get("maxOutputTokens"), base["maxOutputTokens"])
        merged["batchSize"] = _safe_int(merged.get("batchSize"), base["batchSize"])

        merged["apiHost"] = _safe_str(merged.get("apiHost")).strip()
        merged["apiKey"] = _safe_str(merged.get("apiKey")).strip()
        merged["model"] = _safe_str(merged.get("model")).strip() or base["model"]

        merged["systemPrompt"] = _safe_str(merged.get("systemPrompt"))
        merged["batchSystemPrompt"] = _safe_str(merged.get("batchSystemPrompt"))
        merged["batchUserPromptTemplate"] = _safe_str(merged.get("batchUserPromptTemplate"))

        merged["hfToken"] = _safe_str(merged.get("hfToken")).strip()
        merged["hfDataset"] = _safe_str(merged.get("hfDataset")).strip()

        # 关键：批处理 prompt fallback 规则
        if not merged["batchSystemPrompt"]:
            merged["batchSystemPrompt"] = merged["systemPrompt"] or DEFAULT_BATCH_SYSTEM_PROMPT

        if not merged["batchUserPromptTemplate"]:
            merged["batchUserPromptTemplate"] = DEFAULT_BATCH_USER_PROMPT_TEMPLATE

        return merged

    def resolve_config_from_id(self, user_id: str, config_id: str):
        """
        最终执行配置解析入口：
        - 没有 configId：返回系统默认配置
        - 有 configId：从 store 取完整配置，再与默认配置合并
        """
        user_id = user_id or "default"
        config_id = _safe_str(config_id).strip()

        if not config_id:
            resolved = self._merge_with_defaults({})
            logger.info(
                f"resolve_config_from_id: user={user_id}, config_id=<default>, "
                f"model={resolved.get('model')!r}, "
                f"batchSystemPrompt_len={len(resolved.get('batchSystemPrompt') or '')}, "
                f"systemPrompt_len={len(resolved.get('systemPrompt') or '')}"
            )
            return resolved

        full_cfg = conversation_config_store.get_config_full(user_id, config_id)
        if not full_cfg:
            logger.warning(f"resolve_config_from_id: user={user_id}, config_id={config_id!r} 不存在，回退默认配置")
            resolved = self._merge_with_defaults({})
            return resolved

        resolved = self._merge_with_defaults(full_cfg)

        logger.info(
            f"resolve_config_from_id: user={user_id}, config_id={config_id!r}, "
            f"name={full_cfg.get('name')!r}, "
            f"apiHost={resolved.get('apiHost')!r}, "
            f"model={resolved.get('model')!r}, "
            f"batchSystemPrompt_len={len(resolved.get('batchSystemPrompt') or '')}, "
            f"systemPrompt_len={len(resolved.get('systemPrompt') or '')}, "
            f"batchUserPromptTemplate_len={len(resolved.get('batchUserPromptTemplate') or '')}"
        )
        return resolved

    def resolve_book_config(self, user_id: str, config_id: str):
        """
        语义化别名：一本书执行时解析配置
        """
        return self.resolve_config_from_id(user_id, config_id)

    def get_all_safe(self):
        """
        兼容旧接口：返回前端可看的安全设置
        """
        cfg = self.get_default_runtime_config()
        safe_cfg = copy.deepcopy(cfg)

        if safe_cfg.get("apiKey"):
            safe_cfg["apiKeyMasked"] = f"{safe_cfg['apiKey'][:4]}****" if len(safe_cfg["apiKey"]) >= 4 else "****"
        else:
            safe_cfg["apiKeyMasked"] = ""

        if safe_cfg.get("hfToken"):
            safe_cfg["hfTokenMasked"] = f"{safe_cfg['hfToken'][:4]}****" if len(safe_cfg["hfToken"]) >= 4 else "****"
        else:
            safe_cfg["hfTokenMasked"] = ""

        # 不直接回明文
        safe_cfg["apiKey"] = ""
        safe_cfg["hfToken"] = ""

        return safe_cfg


app_settings_service = AppSettingsService()