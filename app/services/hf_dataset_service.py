import logging
from io import BytesIO

import requests
from huggingface_hub import HfApi, hf_hub_download

logger = logging.getLogger(__name__)


class HFDatasetService:
    """
    最终版 HF Dataset 服务：
    - 创建数据集
    - 列文件
    - 列结果文件
    - 下载文本
    - 上传文本
    - 删除文件
    """

    def __init__(self):
        self._api = HfApi()

    # ==================== 数据集管理 ====================

    def create_dataset(self, hf_token: str, hf_dataset: str, private: bool = True):
        try:
            self._api.create_repo(
                repo_id=hf_dataset,
                token=hf_token,
                repo_type="dataset",
                private=private,
                exist_ok=True
            )
            return True
        except Exception as e:
            logger.error(f"创建 HF 数据集失败 [{hf_dataset}]: {e}", exc_info=True)
            return False

    # ==================== 文件列表 ====================

    def list_files(self, hf_token: str, hf_dataset: str):
        return self._api.list_repo_files(
            repo_id=hf_dataset,
            repo_type="dataset",
            token=hf_token
        )

    def list_result_files(self, hf_token: str, hf_dataset: str):
        files = self.list_files(hf_token, hf_dataset)
        result = []
        for f in files:
            if str(f).lower().endswith(".txt"):
                result.append(f)
        return result

    # ==================== 下载 ====================

    def load_text_file(self, hf_token: str, hf_dataset: str, filename: str):
        try:
            local_path = hf_hub_download(
                repo_id=hf_dataset,
                filename=filename,
                repo_type="dataset",
                token=hf_token
            )
            with open(local_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.error(f"加载 HF 文本文件失败 [{hf_dataset}/{filename}]: {e}", exc_info=True)
            return None

    # ==================== 上传 ====================

    def upload_text_file(self, hf_token: str, hf_dataset: str, filename: str, content: str):
        content = content or ""
        binary = content.encode("utf-8")

        self._api.upload_file(
            path_or_fileobj=BytesIO(binary),
            path_in_repo=filename,
            repo_id=hf_dataset,
            repo_type="dataset",
            token=hf_token
        )

    # ==================== 删除 ====================

    def delete_file(self, hf_token: str, hf_dataset: str, filename: str):
        self._api.delete_file(
            path_in_repo=filename,
            repo_id=hf_dataset,
            repo_type="dataset",
            token=hf_token
        )


hf_dataset_service = HFDatasetService()