import logging

logger = logging.getLogger(__name__)


class FileService:
    def _resolve_hf(self, hf_token: str, hf_dataset: str):
        """
        优先使用用户提供的 token/dataset，否则 fallback 到后端默认配置
        """
        from app.services.app_settings_service import app_settings_service
        token = hf_token or app_settings_service.get_hf_token()
        dataset = hf_dataset or app_settings_service.get_hf_dataset()
        return token, dataset

    def _normalize_file_item(self, f):
        """
        统一文件列表项格式：
        {
            "path": "...",
            "name": "..."
        }
        """
        if isinstance(f, dict):
            path = f.get("path") or f.get("name") or ""
            return {
                "path": path,
                "name": f.get("name") or path.split("/")[-1]
            }

        if isinstance(f, str):
            return {
                "path": f,
                "name": f.split("/")[-1]
            }

        # 兼容 huggingface_hub 的对象类型
        path = getattr(f, 'rf_path', getattr(f, 'path', str(f)))
        return {
            "path": path,
            "name": path.split("/")[-1]
        }

    def _clear_file_cache(self, dataset_id: str, filename: str):
        """
        上传/删除后清理该文件缓存，避免返回旧内容
        """
        try:
            cache_key = cache_store.build_file_cache_key(dataset_id, filename)
            cache_store.delete(cache_key)
        except Exception as e:
            logger.warning(f"清理文件缓存失败 [{dataset_id}/{filename}]: {e}")

    def list_dataset_files(self, hf_token: str, hf_dataset: str):
        """
        列出数据集文件，统一返回格式
        """
        hf_token, hf_dataset = self._resolve_hf(hf_token, hf_dataset)
        if not hf_token or not hf_dataset:
            return []

        try:
            files = hf_dataset_service.list_files(hf_token, hf_dataset)
            result = [self._normalize_file_item(f) for f in files]
            return [f for f in result if f.get("path") and not f["path"].startswith(".")]
        except Exception as e:
            logger.error(f"列出数据集文件失败 [{hf_dataset}]: {e}")
            return []

    def list_result_files(self, hf_token: str, hf_dataset: str):
        hf_token, hf_dataset = self._resolve_hf(hf_token, hf_dataset)
        if not hf_token or not hf_dataset:
            return []

        try:
            files = hf_dataset_service.list_result_files(hf_token, hf_dataset)
            return [self._normalize_file_item(f) for f in files]
        except Exception as e:
            logger.error(f"列出结果文件失败 [{hf_dataset}]: {e}")
            return []

    def download_dataset_file(self, hf_token: str, hf_dataset: str, filename: str):
        hf_token, hf_dataset = self._resolve_hf(hf_token, hf_dataset)

        if not hf_token or not hf_dataset:
            return {"success": False, "error": "需要HF配置（token/dataset）"}
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
            if content is not None:
                cache_store.set_file_content(hf_dataset, filename, content)
                return {
                    "success": True,
                    "content": content,
                    "filename": filename.split("/")[-1],
                    "from_cache": False
                }

            return {"success": False, "error": f"文件下载失败: {filename}"}
        except Exception as e:
            logger.error(f"下载数据集文件失败 [{hf_dataset}/{filename}]: {e}")
            return {"success": False, "error": str(e)}

    def upload_text_to_dataset(self, hf_token: str, hf_dataset: str, filename: str, content: str):
        """
        供后端内部直接上传文本使用
        """
        hf_token, hf_dataset = self._resolve_hf(hf_token, hf_dataset)

        if not hf_token or not hf_dataset:
            return {"success": False, "error": "需要HF配置（token/dataset）"}
        if not filename:
            return {"success": False, "error": "缺少文件名"}

        try:
            hf_dataset_service.upload_text_file(hf_token, hf_dataset, filename, content or "")
            self._clear_file_cache(hf_dataset, filename)
            return {"success": True}
        except Exception as e:
            logger.error(f"上传文本到数据集失败 [{hf_dataset}/{filename}]: {e}")
            return {"success": False, "error": str(e)}

    def delete_dataset_file(self, hf_token: str, hf_dataset: str, filename: str):
        hf_token, hf_dataset = self._resolve_hf(hf_token, hf_dataset)

        if not hf_token or not hf_dataset:
            return {"success": False, "error": "需要HF配置（token/dataset）"}
        if not filename:
            return {"success": False, "error": "缺少文件名"}

        try:
            hf_dataset_service.delete_file(hf_token, hf_dataset, filename)
            self._clear_file_cache(hf_dataset, filename)
            return {"success": True}
        except Exception as e:
            logger.error(f"删除数据集文件失败 [{hf_dataset}/{filename}]: {e}")
            return {"success": False, "error": str(e)}

    def hf_action(self, data: dict):
        hf_token, hf_dataset = self._resolve_hf(data.get("hfToken"), data.get("hfDataset"))
        action = data.get("action")

        if not hf_token or not hf_dataset:
            return {"success": False, "error": "需要HF配置（请在设置中配置或联系管理员配置默认值）"}

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
            logger.error(f"HF 操作失败 [{action}] [{hf_dataset}]: {e}")
            return {"success": False, "error": str(e)}


# 延迟导入避免循环依赖
from app.stores.cache_store import cache_store
from app.services.hf_dataset_service import hf_dataset_service

file_service = FileService()