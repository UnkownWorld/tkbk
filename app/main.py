import logging
import time
from flask import request, jsonify, send_from_directory

from app.services.app_settings_service import app_settings_service
from app.services.chapter_service import chapter_service
from app.services.result_service import result_service
from app.services.task_dispatch_service import task_dispatch_service
from app.services.file_service import file_service

from app.stores.task_store import task_store
from app.stores.conversation_store import conversation_store
from app.stores.conversation_config_store import conversation_config_store

logger = logging.getLogger(__name__)


def _safe_int(value, default, min_value=None, max_value=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def register_routes(app):
    # 初始化 worker 池配置
    thread_pool_size = _safe_int(app.config.get("MAX_THREAD_WORKERS", 10), 10, min_value=1, max_value=100)
    max_concurrent = _safe_int(app.config.get("MAX_CONCURRENT_TASKS", 10), 10, min_value=1, max_value=100)

    task_dispatch_service.configure(
        thread_pool_size=thread_pool_size,
        max_concurrent=max_concurrent
    )
    task_dispatch_service.ensure_workers()

    logger.info(
        f"register_routes 初始化完成: "
        f"threadPoolSize={thread_pool_size}, maxConcurrent={max_concurrent}"
    )

    # ==================== 静态首页 ====================

    @app.route('/')
    def index():
        return send_from_directory('static', 'index.html')

    # ==================== 健康 / 状态 ====================

    @app.route('/api/health', methods=['GET'])
    def health():
        stats = task_dispatch_service.get_stats()
        return jsonify({
            "success": True,
            "status": "ok",
            "timestamp": int(time.time() * 1000),
            **stats
        })

    @app.route('/api/status', methods=['GET'])
    def status():
        stats = task_dispatch_service.get_stats()
        return jsonify({
            "success": True,
            **stats,
            "taskCount": task_store.task_count()
        })

    @app.route('/api/set-thread-pool', methods=['POST'])
    def set_thread_pool():
        data = request.json or {}
        thread_pool_size = _safe_int(data.get("threadPoolSize", 10), 10, min_value=1, max_value=100)
        max_concurrent = _safe_int(data.get("maxConcurrent", task_dispatch_service.get_stats()["maxConcurrent"]), task_dispatch_service.get_stats()["maxConcurrent"], min_value=1, max_value=100)

        task_dispatch_service.configure(
            thread_pool_size=thread_pool_size,
            max_concurrent=max_concurrent
        )
        task_dispatch_service.rebuild_workers(thread_pool_size)

        stats = task_dispatch_service.get_stats()
        return jsonify({
            "success": True,
            **stats
        })

    @app.route('/api/set-concurrent', methods=['POST'])
    def set_concurrent():
        data = request.json or {}
        max_concurrent = _safe_int(data.get("maxConcurrent", 10), 10, min_value=1, max_value=100)

        current_pool = task_dispatch_service.get_stats()["threadPoolSize"]
        task_dispatch_service.configure(
            thread_pool_size=current_pool,
            max_concurrent=max_concurrent
        )

        return jsonify({
            "success": True,
            **task_dispatch_service.get_stats()
        })

    # ==================== 系统设置 ====================

    @app.route('/api/settings', methods=['GET'])
    def get_settings():
        settings = app_settings_service.get_all_safe()
        settings.update(task_dispatch_service.get_stats())
        return jsonify({
            "success": True,
            "settings": settings
        })

    @app.route('/api/settings/update', methods=['POST'])
    def update_settings():
        data = request.json or {}
        result = app_settings_service.update_user_defaults(data)
        status_code = 200 if result.get("success") else 400
        return jsonify(result), status_code

    # ==================== 配置管理 ====================

    @app.route('/api/config/list', methods=['GET'])
    def list_configs():
        user_id = request.args.get("userId", "default")
        configs = conversation_config_store.get_user_configs_safe(user_id)
        return jsonify({
            "success": True,
            "configs": configs
        })

    @app.route('/api/config/save', methods=['POST'])
    def save_config():
        data = request.json or {}
        user_id = data.get("userId", "default")
        result = conversation_config_store.save_config(user_id, data)
        status_code = 200 if result.get("success") else 400
        return jsonify(result), status_code

    @app.route('/api/config/delete', methods=['POST'])
    def delete_config():
        data = request.json or {}
        user_id = data.get("userId", "default")
        config_id = data.get("id") or data.get("configId")
        result = conversation_config_store.delete_config(user_id, config_id)
        status_code = 200 if result.get("success") else 400
        return jsonify(result), status_code

    # ==================== 聊天 ====================

    @app.route('/api/conversations', methods=['GET'])
    def get_conversations():
        user_id = request.args.get("userId", "default")
        conversations = conversation_store.get_user_conversations(user_id)
        return jsonify({
            "success": True,
            "conversations": conversations
        })

    @app.route('/api/conversation/create', methods=['POST'])
    def create_conversation():
        import uuid

        data = request.json or {}
        user_id = data.get("userId", "default")
        conv_id = str(uuid.uuid4())

        conv_data = {
            "id": conv_id,
            "title": data.get("title", "新对话"),
            "configId": data.get("configId", ""),
            "created_at": int(time.time() * 1000),
            "messages": []
        }

        conversation_store.create_conversation(user_id, conv_id, conv_data)
        return jsonify({
            "success": True,
            "conversation": conv_data
        })

    @app.route('/api/conversation/delete', methods=['POST'])
    def delete_conversation():
        data = request.json or {}
        user_id = data.get("userId", "default")
        conv_id = data.get("id") or data.get("conversationId")
        if not conv_id:
            return jsonify({"success": False, "error": "缺少对话ID"}), 400

        conversation_store.delete_conversation(user_id, conv_id)
        return jsonify({"success": True})

    @app.route('/api/chat', methods=['POST'])
    def chat():
        from app.services.llm_service import llm_service

        data = request.json or {}
        user_id = data.get("userId", "default")
        conv_id = data.get("conversationId")
        user_message = (data.get("message", "") or "").strip()
        config_id = data.get("configId", "")

        if not conv_id:
            return jsonify({"success": False, "error": "缺少conversationId"}), 400
        if not user_message:
            return jsonify({"success": False, "error": "消息不能为空"}), 400

        resolved_config = app_settings_service.resolve_config_from_id(user_id, config_id)
        system_prompt = resolved_config.get("systemPrompt") or ""
        context_rounds = _safe_int(resolved_config.get("contextRounds", 100), 100, min_value=0)

        user_msg = {
            "role": "user",
            "content": user_message,
            "time": int(time.time() * 1000)
        }
        conversation_store.add_message(user_id, conv_id, user_msg)

        conv = conversation_store.get_conversation(user_id, conv_id)
        history = conv.get("messages", []) if conv else []

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if history and context_rounds > 0:
            max_msgs = context_rounds * 2
            recent = history[-max_msgs:-1] if len(history) > 1 else []
            for msg in recent:
                messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })

        messages.append({"role": "user", "content": user_message})

        call_result = llm_service.call_once(
            config=resolved_config,
            messages=messages,
            use_stream=False
        )

        if call_result["success"]:
            assistant_text = call_result.get("text", "")
            assistant_msg = {
                "role": "assistant",
                "content": assistant_text,
                "time": int(time.time() * 1000)
            }
            conversation_store.add_message(user_id, conv_id, assistant_msg)
            return jsonify({
                "success": True,
                "message": assistant_text
            })

        return jsonify({
            "success": False,
            "error": call_result.get("error", "调用失败")
        })

    # ==================== 章节预览 ====================

    @app.route('/api/parse-chapters', methods=['POST'])
    def parse_chapters():
        data = request.json or {}
        content = data.get("content", "") or ""
        if not content.strip():
            return jsonify({"success": False, "error": "内容不能为空"}), 400

        chapters = chapter_service.build_preview_chapters(content)
        return jsonify({
            "success": True,
            "chapters": chapters,
            "total": len(chapters)
        })

    # ==================== 批处理 ====================

    @app.route('/api/batch', methods=['POST'])
    def submit_batch():
        data = request.json or {}
        user_id = data.get("userId", "default")
        files = data.get("files", []) or []
        delay_min = _safe_int(data.get("delayMin", 15), 15, min_value=0)
        delay_max = _safe_int(data.get("delayMax", 45), 45, min_value=delay_min)

        logger.info(f"收到批处理提交: files={len(files)}, delay={delay_min}-{delay_max}")

        result = task_dispatch_service.submit_batch(
            user_id=user_id,
            files=files,
            delay_min=delay_min,
            delay_max=delay_max
        )

        status_code = 200 if result.get("success") else 400
        if result.get("duplicates"):
            status_code = 409

        return jsonify(result), status_code

    @app.route('/api/batch/cancel', methods=['POST'])
    def cancel_batch():
        data = request.json or {}
        task_id = data.get("taskId")
        if not task_id:
            return jsonify({"success": False, "error": "缺少taskId"}), 400

        result = task_dispatch_service.cancel_task(task_id)
        status_code = 200 if result.get("success") else 404
        return jsonify(result), status_code

    @app.route('/api/batch/resume', methods=['POST'])
    def resume_batch():
        return jsonify({
            "success": False,
            "error": "最终版暂不支持 resume，建议重新提交需要补跑的书/章节范围"
        }), 400

    # ==================== 任务管理 ====================

    @app.route('/api/tasks', methods=['GET'])
    def get_tasks():
        tasks = task_store.get_all_tasks_summary()
        return jsonify({
            "success": True,
            "tasks": tasks
        })

    @app.route('/api/task/<task_id>', methods=['GET'])
    def get_task(task_id):
        task = task_store.get_task(task_id)
        if not task:
            return jsonify({"success": False, "error": "任务不存在"}), 404

        return jsonify({
            "success": True,
            "task": task
        })

    @app.route('/api/task/delete', methods=['POST'])
    def delete_task():
        data = request.json or {}
        task_id = data.get("taskId")
        if not task_id:
            return jsonify({"success": False, "error": "缺少taskId"}), 400

        task = task_store.get_task(task_id)
        if not task:
            return jsonify({"success": False, "error": "任务不存在"}), 404

        task_store.delete_task(task_id)
        return jsonify({"success": True})

    @app.route('/api/task/<task_id>/download', methods=['GET'])
    def download_task(task_id):
        task = task_store.get_task(task_id)
        if not task:
            return jsonify({"success": False, "error": "任务不存在"}), 404

        return jsonify(result_service.build_download_payload_for_task(task))

    @app.route('/api/task/<task_id>/download/<int:file_idx>', methods=['GET'])
    def download_single_book(task_id, file_idx):
        task = task_store.get_task(task_id)
        if not task:
            return jsonify({"success": False, "error": "任务不存在"}), 404

        files = task.get("files", []) or []
        if file_idx < 0 or file_idx >= len(files):
            return jsonify({"success": False, "error": "文件索引无效"}), 400

        return jsonify(
            result_service.build_download_payload_for_single_book(task_id, files[file_idx])
        )

    # ==================== HF 文件管理 ====================

    @app.route('/api/hf-action', methods=['POST'])
    def hf_action():
        data = request.json or {}
        result = file_service.hf_action(data)
        return jsonify(result)

    @app.route('/api/hf-files', methods=['GET'])
    def hf_files():
        hf_token = request.args.get("hfToken", "")
        hf_dataset = request.args.get("hfDataset", "")
        files = file_service.list_dataset_files(hf_token, hf_dataset)
        return jsonify({
            "success": True,
            "files": files
        })

    @app.route('/api/hf-result-files', methods=['GET'])
    def hf_result_files():
        hf_token = request.args.get("hfToken", "")
        hf_dataset = request.args.get("hfDataset", "")
        files = file_service.list_result_files(hf_token, hf_dataset)
        return jsonify({
            "success": True,
            "files": files
        })

    @app.route('/api/hf-download', methods=['GET'])
    def hf_download():
        hf_token = request.args.get("hfToken", "")
        hf_dataset = request.args.get("hfDataset", "")
        filename = request.args.get("filename", "")
        result = file_service.download_dataset_file(hf_token, hf_dataset, filename)
        return jsonify(result)