import logging
from huggingface_hub import HfApi, hf_hub_download, create_repo

logger = logging.getLogger(__name__)


class HFDatasetService:
    def _get_api(self, token: str):
        """
        每次按 token 创建独立 HfApi，避免共享状态带来的潜在线程安全问题。
        """
        return HfApi(token=token)

    def list_files(self, token: str, dataset: str):
        if not token or not dataset:
            logger.warning("list_files 缺少 token 或 dataset")
            return []

        try:
            api = self._get_api(token)
            files = api.list_repo_files(
                repo_id=dataset,
                repo_type="dataset",
                token=token
            )
            return [f for f in files if not str(f).startswith(".")]
        except Exception as e:
            logger.error(f"列出文件失败 [{dataset}]: {e}")
            return []

    def list_result_files(self, token: str, dataset: str):
        files = self.list_files(token, dataset)
        return [f for f in files if str(f).startswith("results/")]

    def load_text_file(self, token: str, dataset: str, filename: str):
        if not token or not dataset or not filename:
            logger.warning("load_text_file 缺少 token/dataset/filename")
            return None

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
        if not token or not dataset:
            raise ValueError("上传失败：缺少 token 或 dataset")
        if not filename:
            raise ValueError("上传失败：缺少 filename")

        try:
            api = self._get_api(token)
            api.upload_file(
                path_or_fileobj=(content or "").encode("utf-8"),
                path_in_repo=filename,
                repo_id=dataset,
                repo_type="dataset",
                token=token
            )
            logger.info(f"上传文件成功 [{dataset}/{filename}]")
            return True
        except Exception as e:
            logger.error(f"上传文件失败 [{dataset}/{filename}]: {e}")
            raise

    def delete_file(self, token: str, dataset: str, filename: str):
        if not token or not dataset:
            raise ValueError("删除失败：缺少 token 或 dataset")
        if not filename:
            raise ValueError("删除失败：缺少 filename")

        try:
            api = self._get_api(token)
            api.delete_file(
                path_in_repo=filename,
                repo_id=dataset,
                repo_type="dataset",
                token=token
            )
            logger.info(f"删除文件成功 [{dataset}/{filename}]")
            return True
        except Exception as e:
            logger.error(f"删除文件失败 [{dataset}/{filename}]: {e}")
            raise

    def create_dataset(self, token: str, dataset: str, private: bool = True):
        if not token or not dataset:
            logger.warning("create_dataset 缺少 token 或 dataset")
            return False

        try:
            create_repo(
                repo_id=dataset,
                repo_type="dataset",
                private=private,
                token=token,
                exist_ok=True
            )
            logger.info(f"创建/确认数据集成功 [{dataset}]")
            return True
        except Exception as e:
            logger.error(f"创建数据集失败 [{dataset}]: {e}")
            return False


hf_dataset_service = HFDatasetService()