"""P-platform 共享异步工具：在同步与异步调用方上下文中安全运行协程。

背景：业务/平台模块里的同步函数（如 resolve_retrieval_config）内部需要执行
async DB 查询。当这些函数被 async 调用方（FastAPI async 端点）调用时，
裸 ``asyncio.run()`` 会抛 ``RuntimeError: cannot be called from a running
event loop``，若被 ``except`` 吞掉则静默返回默认值——这是慢查询的根因之一。

本模块提供 ``run_async_safe``：检测调用方是否有 running event loop——
- 无（同步 Celery task / 同步 def 端点）→ 直接 ``asyncio.run``，无额外开销；
- 有（FastAPI async 端点 / async worker）→ 在共享线程池中另起 loop 执行，
  避免与既有 loop 冲突（复用 doc/service.py `_run_async_safe` 已验证的模式）。

P-platform 层共享，无业务状态，任何模块可引入。
"""

import asyncio
import concurrent.futures

# 共享线程池：async 上下文里把协程放到独立线程的 event loop 中执行。
# 复用（而非每次新建）避免反复创建线程的开销。
_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="async-sync-bridge",
)


def run_async_safe(coro, timeout: float = 60.0):
    """在同步或异步调用方上下文中安全运行协程，返回其结果。

    Args:
        coro: 待运行的协程对象（每次调用传新建的，勿复用已 await 的）。
        timeout: async 上下文（线程池路径）下的最大等待秒数，超时抛
            TimeoutError（由调用方决定回退策略）。

    Raises:
        透传 coro 内部异常；线程池路径下 TimeoutError。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 无 running loop — 同步上下文，直接 asyncio.run
        return asyncio.run(coro)

    # 已有 running loop — 放到共享线程池的独立 loop 中执行，避免冲突
    future = _executor.submit(asyncio.run, coro)
    return future.result(timeout=timeout)
