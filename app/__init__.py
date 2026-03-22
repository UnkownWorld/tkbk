import os
import logging
from flask import Flask
from flask_cors import CORS

def create_app():
    app = Flask(__name__, static_folder="static", static_url_path="/static")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )
    app.logger.setLevel(logging.INFO)
    logger = logging.getLogger(__name__)

    # 基础配置
    app.config["APP_NAME"] = os.environ.get("APP_NAME", "AI Workflow Assistant")
    app.config["MAX_CONCURRENT_TASKS"] = int(os.environ.get("APP_MAX_CONCURRENT_TASKS", "10"))
    app.config["MAX_THREAD_WORKERS"] = int(os.environ.get("APP_MAX_THREAD_WORKERS", "10"))

    # HF 配置（敏感，绝不返回前端）
    app.config["HF_TOKEN"] = os.environ.get("APP_HF_TOKEN", "")
    app.config["HF_DATASET"] = os.environ.get("APP_HF_DATASET", "")
    app.config["HF_USERNAME"] = ""

    # API 配置（敏感，绝不返回前端）
    app.config["DEFAULT_API_HOST"] = os.environ.get("APP_API_HOST", "https://ai.wsocket.xyz/v1")
    app.config["DEFAULT_API_KEY"] = os.environ.get("APP_API_KEY", "")
    app.config["DEFAULT_MODEL"] = os.environ.get("APP_MODEL", "gpt-5.4")
    app.config["DEFAULT_TEMPERATURE"] = float(os.environ.get("APP_TEMPERATURE", "0.7"))
    app.config["DEFAULT_TOP_P"] = float(os.environ.get("APP_TOP_P", "0.65"))
    app.config["DEFAULT_MAX_TOKENS"] = int(os.environ.get("APP_MAX_TOKENS", "1000000"))
    app.config["DEFAULT_MAX_OUTPUT_TOKENS"] = int(os.environ.get("APP_MAX_OUTPUT_TOKENS", "50000"))
    
    # 聊天功能系统提示词
    app.config["DEFAULT_SYSTEM_PROMPT"] = os.environ.get("APP_SYSTEM_PROMPT", "You are a helpful AI assistant.")
    
    # 批处理功能提示词（独立配置）
    app.config["DEFAULT_BATCH_SYSTEM_PROMPT"] = os.environ.get("APP_BATCH_SYSTEM_PROMPT", "")
    app.config["DEFAULT_BATCH_USER_PROMPT_TEMPLATE"] = os.environ.get("APP_BATCH_USER_PROMPT_TEMPLATE", "")
    
    app.config["DEFAULT_CONTEXT_ROUNDS"] = int(os.environ.get("APP_CONTEXT_ROUNDS", "100"))

    CORS(app)

    # 自动检测 HF 用户名并创建默认数据集
    _init_hf_dataset(app)

    from app.main import register_routes
    register_routes(app)

    return app


def _init_hf_dataset(app):
    """启动时自动检测 HF 用户名，创建 username/bk1 数据集"""
    hf_token = app.config.get("HF_TOKEN", "")
    if not hf_token:
        logging.getLogger(__name__).info("未配置 APP_HF_TOKEN，跳过数据集初始化")
        return

    try:
        from huggingface_hub import HfApi, create_repo
        api = HfApi()
        user_info = api.whoami(token=hf_token)
        username = user_info.get("name", "")
        if not username:
            return

        app.config["HF_USERNAME"] = username
        default_dataset = f"{username}/bk1"

        # 如果没有手动指定数据集，使用默认
        if not app.config.get("HF_DATASET"):
            app.config["HF_DATASET"] = default_dataset

        # 尝试创建数据集（private）
        try:
            create_repo(
                repo_id=default_dataset,
                repo_type="dataset",
                private=True,
                token=hf_token,
                exist_ok=True
            )
            logging.getLogger(__name__).info(f"数据集已就绪: {default_dataset} (private)")
        except Exception as e:
            logging.getLogger(__name__).warning(f"创建数据集失败 [{default_dataset}]: {e}")

    except Exception as e:
        logging.getLogger(__name__).warning(f"获取 HF 用户信息失败: {e}")
