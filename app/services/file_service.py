import logging

logger = logging.getLogger(__name__)

class FileService:
    def _resolve_hf(self, hf_token: str, hf_dataset: str):
        """优先使用用户提供的 token/dataset，否则 fallback 到后端默认配置"""
        from app.services.app_settings_service import app_settings_service
        token = hf_token or app_settings_service.get_hf_token()
        dataset = hf_dataset or app_settings_service.get_hf_dataset()
        return token, dataset

    def list_dataset_files(self, hf_token: str, hf_dataset: str):
        hf_token, hf_dataset = self._resolve_hf(hf_token, hf_dataset)
        files = hf_dataset_service.list_files(hf_token, hf_dataset)
        # 确保每个文件都有 name 和 path 字段
        result = []
        for f in files:
            if isinstance(f, dict):
                result.append(f)
            elif isinstance(f, str):
                result.append({"path": f, "name": f.split("/")[-1]})
            else:
                # RepoFile 对象
                path = getattr(f, 'rf_path', getattr(f, 'path', str(f)))
                result.append({"path": path, "name": path.split("/")[-1]})
        return result

    def list_result_files(self, hf_token: str, hf_dataset: str):
        hf_token, hf_dataset = self._resolve_hf(hf_token, hf_dataset)
        return hf_dataset_service.list_result_files(hf_token, hf_dataset)

    def download_dataset_file(self, hf_token: str, hf_dataset: str, filename: str):
        hf_token, hf_dataset = self._resolve_hf(hf_token, hf_dataset)

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

    def hf_action(self, data: dict):
        hf_token, hf_dataset = self._resolve_hf(data.get("hfToken"), data.get("hfDataset"))
        action = data.get("action")

        if not hf_token or not hf_dataset:
            return {"success": False, "error": "需要HF配置（请在设置中配置或联系管理员配置默认值）"}

        if action == "list":
            files = hf_dataset_service.list_files(hf_token, hf_dataset)
            return {
                "success": True,
                "files": [{"path": f, "name": f.split("/")[-1]} for f in files if not f.startswith(".")]
            }

        if action == "upload":
            hf_dataset_service.upload_text_file(hf_token, hf_dataset, data["filename"], data.get("content", ""))
            return {"success": True}

        if action == "delete":
            hf_dataset_service.delete_file(hf_token, hf_dataset, data["filename"])
            return {"success": True}

        if action == "create":
            hf_dataset_service.create_dataset(hf_token, hf_dataset, private=True)
            return {"success": True}

        return {"success": False, "error": "Invalid action"}

# 延迟导入避免循环依赖
from app.stores.cache_store import cache_store
from app.services.hf_dataset_service import hf_dataset_service

file_service = FileService()
