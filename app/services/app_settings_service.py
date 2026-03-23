import os
import json
import logging

logger = logging.getLogger(__name__)

# 默认兜底路径（当 current_app 不可用时使用）
_FALLBACK_USER_DEFAULTS_FILE = "/app/data/user_defaults.json"


class AppSettingsService:
    def get_app_name(self):
        from flask import current_app
        return current_app.config.get("APP_NAME", "AI Workflow Assistant")

    def get_hf_token(self):
        from flask import current_app
        return current_app.config.get("HF_TOKEN", "")

    def get_hf_dataset(self):
        from flask import current_app
        return current_app.config.get("HF_DATASET", "")

    def get_hf_username(self):
        from flask import current_app
        return current_app.config.get("HF_USERNAME", "")

    def get_max_concurrent_tasks(self):
        from flask import current_app
        return current_app.config.get("MAX_CONCURRENT_TASKS", 10)

    def _get_user_defaults_file(self):
        """
        优先从 Flask 配置中读取持久化路径，便于 HF Space / Docker 环境调整。
        """
        try:
            from flask import current_app
            return current_app.config.get("USER_DEFAULTS_FILE", _FALLBACK_USER_DEFAULTS_FILE)
        except Exception:
            return _FALLBACK_USER_DEFAULTS_FILE

    def _load_user_defaults(self):
        """
        加载用户修改过的默认配置（非敏感字段）
        """
        path = self._get_user_defaults_file()
        try:
            if not os.path.exists(path):
                return {}
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"加载用户默认配置失败 [{path}]: {e}")
            return {}

    def _save_user_defaults(self, data: dict):
        """
        保存用户修改过的默认配置
        返回：
            {"success": True}
            或
            {"success": False, "error": "..."}
        """
        path = self._get_user_defaults_file()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return {"success": True}
        except Exception as e:
            logger.error(f"保存用户默认配置失败 [{path}]: {e}")
            return {"success": False, "error": str(e)}

    def _get_cfg(self):
        from flask import current_app
        return current_app.config

    def _build_runtime_config(self, include_secrets: bool = False):
        """
        统一构造运行配置
        """
        cfg = self._get_cfg()
        user_overrides = self._load_user_defaults()

        result = {
            "apiHost": user_overrides.get("apiHost", cfg.get("DEFAULT_API_HOST", "")),
            "model": user_overrides.get("model", cfg.get("DEFAULT_MODEL", "gpt-5.4")),
            "temperature": user_overrides.get("temperature", cfg.get("DEFAULT_TEMPERATURE", 0.7)),
            "topP": user_overrides.get("topP", cfg.get("DEFAULT_TOP_P", 0.65)),
            # 统一以 maxOutputTokens 为主，maxTokens 仅兼容保留
            "maxOutputTokens": user_overrides.get(
                "maxOutputTokens",
                cfg.get("DEFAULT_MAX_OUTPUT_TOKENS", 50000)
            ),
            "maxTokens": user_overrides.get(
                "maxTokens",
                cfg.get("DEFAULT_MAX_TOKENS", 1000000)
            ),
            "contextRounds": user_overrides.get("contextRounds", cfg.get("DEFAULT_CONTEXT_ROUNDS", 100)),
            "systemPrompt": user_overrides.get("systemPrompt", cfg.get("DEFAULT_SYSTEM_PROMPT", "")),
            "batchSystemPrompt": user_overrides.get("batchSystemPrompt", cfg.get("DEFAULT_BATCH_SYSTEM_PROMPT", "")),
            "batchUserPromptTemplate": user_overrides.get(
                "batchUserPromptTemplate",
                cfg.get("DEFAULT_BATCH_USER_PROMPT_TEMPLATE", "")
            ),
            "hfDataset": cfg.get("HF_DATASET", ""),
            "hasHfDataset": bool(cfg.get("HF_DATASET", "")),
        }

        if include_secrets:
            result.update({
                "apiKey": cfg.get("DEFAULT_API_KEY", ""),
                "hfToken": cfg.get("HF_TOKEN", ""),
                "hfDataset": cfg.get("HF_DATASET", ""),
            })
        else:
            result.update({
                "hasApiKey": bool(cfg.get("DEFAULT_API_KEY", "")),
                "hasHfToken": bool(cfg.get("HF_TOKEN", "")),
            })

        return result

    def get_default_runtime_config(self):
        """
        返回默认配置给前端：
        - 非敏感字段：返回实际值
        - 敏感字段：返回 hasXxx
        """
        return self._build_runtime_config(include_secrets=False)

    def update_user_defaults(self, updates: dict):
        """
        更新用户修改的默认配置（仅允许非敏感字段）
        返回：
            {"success": True, "settings": ...}
            或
            {"success": False, "error": "...", "settings": ...}
        """
        allowed = {
            "model",
            "temperature",
            "topP",
            "maxTokens",
            "maxOutputTokens",
            "contextRounds",
            "systemPrompt",
            "apiHost",
            "batchSystemPrompt",
            "batchUserPromptTemplate",
        }

        user_defaults = self._load_user_defaults()

        for key in allowed:
            if key in updates and updates[key] is not None and updates[key] != "":
                user_defaults[key] = updates[key]
            elif key in updates and (updates[key] is None or updates[key] == ""):
                user_defaults.pop(key, None)

        save_result = self._save_user_defaults(user_defaults)
        logger.info(f"用户默认配置已更新: {list(user_defaults.keys())}")

        if save_result.get("success"):
            return {
                "success": True,
                "settings": self.get_default_runtime_config()
            }

        return {
            "success": False,
            "error": save_result.get("error", "保存失败"),
            "settings": self.get_default_runtime_config()
        }

    def get_full_config(self):
        """
        内部使用，返回完整配置（含 apiKey / hfToken）
        """
        return self._build_runtime_config(include_secrets=True)

    def resolve_config(self, user_config: dict = None):
        """
        合并用户配置和默认配置，用户配置优先
        """
        default = self.get_full_config()
        if not user_config:
            return default

        merged = default.copy()
        for key in [
            "apiHost",
            "apiKey",
            "model",
            "temperature",
            "topP",
            "maxTokens",
            "maxOutputTokens",
            "contextRounds",
            "systemPrompt",
            "batchSystemPrompt",
            "batchUserPromptTemplate",
            "batchSize",
            "hfToken",
            "hfDataset",
        ]:
            if key in user_config and user_config[key]:
                merged[key] = user_config[key]
        return merged

    def resolve_config_from_id(self, user_id: str, config_id: str = None):
        """
        根据 config_id 解析完整配置（含密钥），用于内部调用
        """
        from app.stores.conversation_config_store import conversation_config_store

        default = self.get_full_config()

        if not config_id or config_id == "__default__":
            return default

        config = conversation_config_store.get_config_full(user_id, config_id)
        if not config:
            logger.warning(f"配置 {config_id} 不存在，使用默认配置")
            return default

        merged = default.copy()
        for key in [
            "apiHost",
            "apiKey",
            "model",
            "temperature",
            "topP",
            "maxTokens",
            "maxOutputTokens",
            "contextRounds",
            "systemPrompt",
            "batchSystemPrompt",
            "batchUserPromptTemplate",
            "batchSize",
            "hfToken",
            "hfDataset",
        ]:
            if key in config and config[key]:
                merged[key] = config[key]
        return merged


app_settings_service = AppSettingsService()