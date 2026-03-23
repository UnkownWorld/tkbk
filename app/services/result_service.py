import logging
import time

from app.services.file_service import file_service

logger = logging.getLogger(__name__)


def _safe_str(v):
    if v is None:
        return ""
    return str(v)


def _sanitize_filename(name: str) -> str:
    return _safe_str(name or "未命名").replace(" ", "_").replace("/", "_").replace("\\", "_").replace("\n", "").replace("\r", "")[:80]


class ResultService:
    """
    最终版结果服务：
    - 构建批次结果
    - 构建单书导出文本
    - 构建整任务导出文本
    - 上传 HF
    """

    # ==================== 批次结果 ====================

    def build_batch_result(
        self,
        batch_index: int,
        chapter_start: int,
        chapter_end: int,
        chapter_titles: list,
        success: bool,
        result_text: str = "",
        error: str = "",
        started_at: float = None,
        finished_at: float = None,
    ):
        result_text = _safe_str(result_text)
        error = _safe_str(error)

        return {
            "batch_index": batch_index,
            "chapter_start": chapter_start,
            "chapter_end": chapter_end,
            "chapter_count": max(0, int(chapter_end) - int(chapter_start) + 1),
            "chapter_titles": chapter_titles or [],
            "status": "success" if success else "failed",
            "success": bool(success),
            "result": result_text,
            "error": error,
            "preview": result_text[:300] + ("..." if len(result_text) > 300 else ""),
            "started_at": started_at,
            "finished_at": finished_at,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ==================== 导出文本 ====================

    def build_single_book_export_text(self, task_id: str, book_task: dict):
        file_name = book_task.get("file_name", "未命名")
        batches = book_task.get("batches", []) or []

        text_parts = [
            f"小说: {file_name}",
            f"任务ID: {task_id}",
            f"导出时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"{'=' * 60}",
            ""
        ]

        for batch in batches:
            batch_index = batch.get("batch_index", "?")
            chapter_start = batch.get("chapter_start", "?")
            chapter_end = batch.get("chapter_end", "?")
            chapter_titles = batch.get("chapter_titles", []) or []

            text_parts.append(f"--- 批次 {batch_index}: 第{chapter_start}-{chapter_end}章 ---")
            if chapter_titles:
                text_parts.append("章节标题: " + " | ".join(chapter_titles))
            text_parts.append("")

            if batch.get("success"):
                text_parts.append(batch.get("result", "") or "")
            else:
                text_parts.append(f"❌ 失败: {batch.get('error', '')}")

            text_parts.append("")
            text_parts.append("")

        return "\n".join(text_parts).rstrip() + "\n"

    def build_task_export_text(self, task: dict):
        task_id = task.get("task_id", "")
        files = task.get("files", []) or []

        text_parts = [
            f"任务ID: {task_id}",
            f"导出时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"{'=' * 60}",
            ""
        ]

        for book_task in files:
            text_parts.append(self.build_single_book_export_text(task_id, book_task))
            text_parts.append("")
            text_parts.append("")

        return "\n".join(text_parts).rstrip() + "\n"

    # ==================== HF 上传 ====================

    def upload_single_book_result(self, task_id: str, book_task: dict, default_config: dict):
        resolved_config = book_task.get("resolved_config", {}) or {}
        file_name = book_task.get("file_name", "未命名")

        hf_token = (
            resolved_config.get("hfToken")
            or (default_config or {}).get("hfToken", "")
            or ""
        )
        hf_dataset = (
            resolved_config.get("hfDataset")
            or (default_config or {}).get("hfDataset", "")
            or ""
        )

        if not hf_token or not hf_dataset:
            logger.warning(f"[{task_id}] [{file_name}] 未配置 HF Token/Dataset，跳过上传")
            return {
                "success": False,
                "error": "未配置 HF Token/Dataset"
            }

        download_payload = self.build_download_payload_for_single_book(task_id, book_task)
        export_text = download_payload.get("content", "")
        upload_filename = download_payload.get("filename") or f"{_sanitize_filename(file_name)}-节奏.txt"

        result = file_service.upload_text_to_dataset(
            hf_token=hf_token,
            hf_dataset=hf_dataset,
            filename=upload_filename,
            content=export_text
        )

        if result.get("success"):
            logger.info(f"[{task_id}] [{file_name}] 结果已上传到 HF: {upload_filename}")
            return {
                "success": True,
                "filename": upload_filename
            }

        logger.error(f"[{task_id}] [{file_name}] 上传 HF 失败: {result.get('error')}")
        return result



    # ==================== 下载输出 ====================

    def build_download_payload_for_task(self, task: dict):
        task_id = task.get("task_id", "")
        return {
            "success": True,
            "content": self.build_task_export_text(task),
            "filename": f"batch_{task_id[:8]}.txt"
        }

    def build_download_payload_for_single_book(self, task_id: str, book_task: dict):
        file_name = book_task.get("file_name", "未命名")
        safe_name = _sanitize_filename(file_name)
        return {
            "success": True,
            "content": self.build_single_book_export_text(task_id, book_task),
            "filename": f"{safe_name}-节奏.txt"
        }


result_service = ResultService()