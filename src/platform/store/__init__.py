"""P-STORE：对象存储模块。

封装 SeaweedFS S3 网关的文件读写操作。
接口：StorageBackend.put / get / generate_presigned_url / delete
"""

from .backend import StorageBackend

__all__ = ["StorageBackend"]
