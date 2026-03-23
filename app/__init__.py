import logging
import os

from flask import Flask

from app.main import register_routes


def _get_env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def create_app():
    app = Flask(
        __name__,
        static_folder="../static",
        static_url_path="/static"
    )

    # ==================== Flask / 运行时配置 ====================
    app.config["JSON_AS_ASCII"] = False
    app.config["MAX_THREAD_WORKERS"] = _get_env_int("MAX_THREAD_WORKERS", 10)
    app.config["MAX_CONCURRENT_TASKS"] = _get_env_int("MAX_CONCURRENT_TASKS", 10)

    # 可选：保留一份环境配置快照，便于调试
    app.config["ENV_SNAPSHOT"] = {
        "LOG_LEVEL": os.getenv("LOG_LEVEL", "INFO"),
        "MAX_THREAD_WORKERS": app.config["MAX_THREAD_WORKERS"],
        "MAX_CONCURRENT_TASKS": app.config["MAX_CONCURRENT_TASKS"],
        "APP_API_HOST": os.getenv("APP_API_HOST", ""),
        "APP_MODEL": os.getenv("APP_MODEL", "gpt-5.4"),
        "APP_BATCH_SIZE": os.getenv("APP_BATCH_SIZE", "10"),
        "APP_HF_DATASET": os.getenv("APP_HF_DATASET", ""),
        "HAS_APP_API_KEY": bool(os.getenv("APP_API_KEY")),
        "HAS_APP_HF_TOKEN": bool(os.getenv("APP_HF_TOKEN")),
        "HAS_APP_BATCH_SYSTEM_PROMPT": bool(os.getenv("APP_BATCH_SYSTEM_PROMPT")),
        "HAS_APP_BATCH_USER_PROMPT_TEMPLATE": bool(os.getenv("APP_BATCH_USER_PROMPT_TEMPLATE")),
    }

    # ==================== 日志 ====================
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    logger = logging.getLogger(__name__)
    logger.info(
        "应用初始化完成: "
        f"MAX_THREAD_WORKERS={app.config['MAX_THREAD_WORKERS']}, "
        f"MAX_CONCURRENT_TASKS={app.config['MAX_CONCURRENT_TASKS']}, "
        f"APP_API_HOST={app.config['ENV_SNAPSHOT']['APP_API_HOST']!r}, "
        f"APP_MODEL={app.config['ENV_SNAPSHOT']['APP_MODEL']!r}, "
        f"APP_HF_DATASET={app.config['ENV_SNAPSHOT']['APP_HF_DATASET']!r}, "
        f"HAS_APP_API_KEY={app.config['ENV_SNAPSHOT']['HAS_APP_API_KEY']}, "
        f"HAS_APP_HF_TOKEN={app.config['ENV_SNAPSHOT']['HAS_APP_HF_TOKEN']}"
    )

    register_routes(app)
    return app