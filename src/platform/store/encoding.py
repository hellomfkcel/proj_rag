"""文件编码自动检测模块。

提供 detect_and_decode() 函数，按优先级尝试多种编码检测策略：
1. BOM 检测 (UTF-8-SIG / UTF-16-LE / UTF-16-BE)
2. UTF-8 严格解码
3. chardet 高置信度检测 (>= 0.8)
4. GB18030 回退（中国国标，兼容 GBK/GB2312）
5. chardet 低置信度检测
6. UTF-8 with replace（最后兜底）
"""

from __future__ import annotations

import logging
from typing import Tuple

import chardet

_log = logging.getLogger("encoding")


def detect_and_decode(raw_bytes: bytes) -> Tuple[str, str]:
    """检测编码并解码，返回 (decoded_text, encoding_name)。

    永远不会抛出异常 — 最坏情况回退到 UTF-8 with replace。
    """
    if not raw_bytes:
        return "", "empty"

    # ── 1. BOM 检测 ──
    if raw_bytes[:3] == b"\xef\xbb\xbf":
        return raw_bytes[3:].decode("utf-8"), "utf-8-sig"
    if raw_bytes[:2] == b"\xff\xfe":
        try:
            return raw_bytes[2:].decode("utf-16-le"), "utf-16-le"
        except UnicodeDecodeError:
            pass
    if raw_bytes[:2] == b"\xfe\xff":
        try:
            return raw_bytes[2:].decode("utf-16-be"), "utf-16-be"
        except UnicodeDecodeError:
            pass

    # ── 2. UTF-8 严格解码 ──
    try:
        return raw_bytes.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    # ── 3. chardet 检测（始终优先于 GB18030 盲猜） ──
    detected = chardet.detect(raw_bytes)
    detected_enc = detected.get("encoding", "")
    detected_conf = detected.get("confidence", 0.0) or 0.0

    if detected_enc:
        try:
            text = raw_bytes.decode(detected_enc)
            if "�" not in text:
                if detected_conf < 0.8:
                    _log.info(
                        "encoding_detected_low_confidence",
                        extra={
                            "encoding": detected_enc,
                            "confidence": round(detected_conf, 2),
                        },
                    )
                return text, detected_enc
        except (UnicodeDecodeError, LookupError):
            pass

    # ── 4. GB18030 回退（中国国标，兼容 GBK/GB2312） ──
    try:
        text = raw_bytes.decode("gb18030")
        if "�" not in text:
            return text, "gb18030"
    except (UnicodeDecodeError, LookupError):
        pass

    # ── 6. 其他常见编码 ──
    for enc in ["big5", "euc-jp", "shift_jis", "euc-kr", "latin-1"]:
        try:
            text = raw_bytes.decode(enc)
            if "�" not in text:
                return text, enc
        except (UnicodeDecodeError, LookupError):
            continue

    # ── 7. 最后兜底 ──
    _log.warning("encoding_fallback_replace", extra={"chardet_encoding": detected_enc})
    return raw_bytes.decode("utf-8", errors="replace"), "utf-8 (fallback)"
