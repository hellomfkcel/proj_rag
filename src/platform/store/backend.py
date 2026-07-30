"""P-STORE：对象存储后端。

封装 SeaweedFS S3 网关的文件操作。
接口：StorageBackend — put / get / generate_presigned_url / delete
"""

import io
from datetime import timedelta
from typing import Optional

import boto3
from botocore.exceptions import ClientError


class StorageBackend:
    """S3 兼容对象存储后端。"""

    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
    ):
        self.endpoint_url = endpoint_url
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """确保存储桶存在。"""
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self.bucket)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """上传文件，返回 storage_path。"""
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return f"s3://{self.bucket}/{key}"

    def _strip_prefix(self, key: str) -> str:
        """去除 storage_path 中的 s3://bucket/ 前缀，提取纯 S3 key。

        put() 返回 s3://{bucket}/{key} 格式作为 storage_path，
        但 boto3 get_object/delete_object 的 Key 参数只需要 {key} 部分。
        """
        prefix = f"s3://{self.bucket}/"
        if key.startswith(prefix):
            return key[len(prefix):]
        return key

    def get(self, key: str) -> bytes:
        """下载文件内容。"""
        resp = self._client.get_object(Bucket=self.bucket, Key=self._strip_prefix(key))
        return resp["Body"].read()

    def get_stream(self, key: str) -> io.BytesIO:
        """以流形式获取文件内容。"""
        resp = self._client.get_object(Bucket=self.bucket, Key=self._strip_prefix(key))
        return io.BytesIO(resp["Body"].read())

    def generate_presigned_url(self, key: str, expires_in: int = 300) -> str:
        """生成临时签名下载 URL。"""
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._strip_prefix(key)},
            ExpiresIn=expires_in,
        )

    def delete(self, key: str) -> None:
        """删除文件。"""
        self._client.delete_object(Bucket=self.bucket, Key=self._strip_prefix(key))
