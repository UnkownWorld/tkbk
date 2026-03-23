import logging

from app.services.app_settings_service import app_settings_service
from app.stores.cache_store import cache_store
from app.services.hf_dataset_service import hf_dataset_service

logger = logging.getLogger(__name__)


class FileService:
    """
    最终版 HF 文件服务：
    - 统一解析 HF token / dataset
    - 管理缓存
    - 提供文件列表 / 下载 / 上传 / 删除
    """

    # ==================== 内部工具 ====================

    def _resolve_hf(self, hf_token: str = "", hf_dataset: str = ""):
        """
        优先使用显式传入，其次使用系统默认配置
        """
        token = (hf_token or "").strip() or app_settings_service.get_hf_token()
        dataset = (hf_dataset or "").strip() or app_settings_service.get_hf_dataset()
        return token, dataset

    def _normalize_file_item(self, f):
        """
        统一文件项格式：
        {
            "path": "...",
            "name": "..."
        }
        """
        if isinstance(f, dict):
            path = f.get("path") or f.get("name") or ""
            return {
                "path": path,
                "name": f.get("name") or (path.split("/")[-1] if path else "")
            }

        if isinstance(f, str):
            return {
                "path": f,
                "name": f.split("/")[-1]
            }

        # 兼容 huggingface_hub 返回对象
        path = getattr(f, "rf_path", getattr(f, "path", str(f)))
        return {
            "path": path,
            "name": path.split("/")[-1]
        }

    def _clear_file_cache(self, dataset_id: str, filename: str):
        try:
            cache_key = cache_store.build_file_cache_key(dataset_id, filename)
            cache_store.delete(cache_key)
        except Exception as e:
            logger.warning(f"清理文件缓存失败 [{dataset_id}/{filename}]: {e}")

    # ==================== 列表 ====================

    def list_dataset_files(self, hf_token: str = "", hf_dataset: str = ""):
        hf_token, hf_dataset = self._resolve_hf(hf_token, hf_dataset)

        if not hf_token or not hf_dataset:
            return []

        try:
            files = hf_dataset_service.list_files(hf_token, hf_dataset)
            normalized = [self._normalize_file_item(f) for f in files]
            return [f for f in normalized if f.get("path") and not f["path"].startswith(".")]
        except Exception as e:
            logger.error(f"列出数据集文件失败 [{hf_dataset}]: {e}", exc_info=True)
            return []

    def list_result_files(self, hf_token: str = "", hf_dataset: str = ""):
        hf_token, hf_dataset = self._resolve_hf(hf_token, hf_dataset)

        if not hf_token or not hf_dataset:
            return []

        try:
            files = hf_dataset_service.list_result_files(hf_token, hf_dataset)
            return [self._normalize_file_item(f) for f in files]
        except Exception as e:
            logger.error(f"列出结果文件失败 [{hf_dataset}]: {e}", exc_info=True)
            return []

    # ==================== 下载 ====================

    def download_dataset_file(self, hf_token: str = "", hf_dataset: str = "", filename: str = ""):
        hf_token, hf_dataset = self._resolve_hf(hf_token, hf_dataset)

        if not hf_token or not hf_dataset:
            return {"success": False, "error": "需要 HF 配置（token/dataset）"}
        if not filename:
            return {"success": False, "error": "缺少文件名"}

        try:
            cached = cache_store.get_file_content(hf_dataset, filename)
            if cached:
                return {
                    "success": True,
                    "content": cached["content"],
                    "filename": filename.split("/")[-1],
                    "from_cache": True
                }

            content = hf_dataset_service.load_text_file(hf_token, hf_dataset, filename)
            if content is None:
                return {"success": False, "error": f"文件下载失败: {filename}"}

            cache_store.set_file_content(hf_dataset, filename, content)
            return {
                "success": True,
                "content": content,
                "filename": filename.split("/")[-1],
                "from_cache": False
            }

        except Exception as e:
            logger.error(f"下载数据集文件失败 [{hf_dataset}/{filename}]: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ==================== 上传 ====================

    def upload_text_to_dataset(self, hf_token: str = "", hf_dataset: str = "", filename: str = "", content: str = ""):
        hf_token, hf_dataset = self._resolve_hf(hf_token, hf_dataset)

        if not hf_token or not hf_dataset:
            return {"success": False, "error": "需要 HF 配置（token/dataset）"}
        if not filename:
            return {"success": False, "error": "缺少文件名"}

        try:
            hf_dataset_service.upload_text_file(hf_token, hf_dataset, filename, content or "")
            self._clear_file_cache(hf_dataset, filename)
            return {"success": True}
        except Exception as e:
            logger.error(f"上传文本到数据集失败 [{hf_dataset}/{filename}]: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ==================== 删除 ====================

    def delete_dataset_file(self, hf_token: str = "", hf_dataset: str = "", filename: str = ""):
        hf_token, hf_dataset = self._resolve_hf(hf_token, hf_dataset)

        if not hf_token or not hf_dataset:
            return {"success": False, "error": "需要 HF 配置（token/dataset）"}
        if not filename:
            return {"success": False, "error": "缺少文件名"}

        try:
            hf_dataset_service.delete_file(hf_token, hf_dataset, filename)
            self._clear_file_cache(hf_dataset, filename)
            return {"success": True}
        except Exception as e:
            logger.error(f"删除数据集文件失败 [{hf_dataset}/{filename}]: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ==================== 统一动作接口 ====================

    def hf_action(self, data: dict):
        data = data or {}
        hf_token, hf_dataset = self._resolve_hf(data.get("hfToken", ""), data.get("hfDataset", ""))
        action = data.get("action")

        if not hf_token or not hf_dataset:
            return {"success": False, "error": "需要 HF 配置（请在设置中配置或使用系统默认值）"}

        try:
            if action == "list":
                files = self.list_dataset_files(hf_token, hf_dataset)
                return {
                    "success": True,
                    "files": files
                }

            if action == "upload":
                filename = data.get("filename", "")
                content = data.get("content", "")
                return self.upload_text_to_dataset(hf_token, hf_dataset, filename, content)

            if action == "delete":
                filename = data.get("filename", "")
                return self.delete_dataset_file(hf_token, hf_dataset, filename)

            if action == "create":
                success = hf_dataset_service.create_dataset(hf_token, hf_dataset, private=True)
                if success:
                    return {"success": True}
                return {"success": False, "error": "创建数据集失败"}

            return {"success": False, "error": "Invalid action"}

        except Exception as e:
            logger.error(f"HF 操作失败 [{action}] [{hf_dataset}]: {e}", exc_info=True)
            return {"success": False, "error": str(e)}


file_service = FileService()