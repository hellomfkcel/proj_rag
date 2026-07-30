"""P-STORE：对外服务门面。

提供简洁的 read_file / delete_file 等高层接口，
内部委托 StorageBackend 执行 S3 操作。
"""

from typing import Optional

from src.config import Settings
from src.platform.store.backend import StorageBackend


def _get_backend() -> StorageBackend:
    s = Settings()
    return StorageBackend(
        endpoint_url=s.s3_endpoint_url,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key,
        bucket=s.s3_bucket,
    )


def read_file(storage_path: str) -> Optional[str]:
    """从对象存储读取文件内容，返回文本字符串。

    storage_path 格式：s3://bucket/key 或直接的 key 路径。
    读取失败返回 None。
    """
    try:
        backend = _get_backend()
        key = storage_path
        if key.startswith("s3://"):
            # Strip s3://bucket/ prefix
            parts = key.split("/", 3)
            if len(parts) >= 4:
                key = parts[3]
        content_bytes = backend.get(key)
        return content_bytes.decode("utf-8", errors="replace")
    except Exception:
        return None


def delete_file(storage_path: str) -> bool:
    """从对象存储删除文件。成功返回 True。"""
    try:
        backend = _get_backend()
        key = storage_path
        if key.startswith("s3://"):
            parts = key.split("/", 3)
            if len(parts) >= 4:
                key = parts[3]
        backend.delete(key)
        return True
    except Exception:
        return False
