import logging
from huggingface_hub import HfApi, hf_hub_download, create_repo

logger = logging.getLogger(__name__)

class HFDatasetService:
    def __init__(self):
        self._api = None
        self._token = None

    def _get_api(self, token: str):
        if not self._api or self._token != token:
            self._api = HfApi(token=token)
            self._token = token
        return self._api

    def list_files(self, token: str, dataset: str):
        try:
            api = self._get_api(token)
            files = api.list_repo_files(repo_id=dataset, repo_type="dataset", token=token)
            return [f for f in files if not f.startswith(".")]
        except Exception as e:
            logger.error(f"列出文件失败 [{dataset}]: {e}")
            return []

    def list_result_files(self, token: str, dataset: str):
        files = self.list_files(token, dataset)
        return [f for f in files if f.startswith("results/")]

    def load_text_file(self, token: str, dataset: str, filename: str):
        try:
            local_path = hf_hub_download(
                repo_id=dataset,
                filename=filename,
                repo_type="dataset",
                token=token
            )
            with open(local_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.error(f"加载文件失败 [{dataset}/{filename}]: {e}")
            return None

    def upload_text_file(self, token: str, dataset: str, filename: str, content: str):
        api = self._get_api(token)
        api.upload_file(
            path_or_fileobj=content.encode("utf-8"),
            path_in_repo=filename,
            repo_id=dataset,
            repo_type="dataset",
            token=token
        )

    def delete_file(self, token: str, dataset: str, filename: str):
        api = self._get_api(token)
        api.delete_file(
            path_in_repo=filename,
            repo_id=dataset,
            repo_type="dataset",
            token=token
        )

    def create_dataset(self, token: str, dataset: str, private: bool = True):
        try:
            api = self._get_api(token)
            create_repo(repo_id=dataset, repo_type="dataset", private=private, token=token, exist_ok=True)
            return True
        except Exception as e:
            logger.error(f"创建数据集失败 [{dataset}]: {e}")
            return False

hf_dataset_service = HFDatasetService()
