"""B-CHAT：对话编排模块。

提供：
- POST /conversations/{id}/query   对话查询入口
- retrieve_and_generate_task       检索 + 生成 Celery 任务
- 流式回传（Redis Pub/Sub → SSE）

独占数据：conversation / conversation_turn
"""

from .service import retrieve_and_generate_task

__all__ = ["retrieve_and_generate_task"]
