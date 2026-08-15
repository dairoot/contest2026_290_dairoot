import asyncio
import logging
import time
from typing import Any, Callable

import numpy as np

logger = logging.getLogger(__name__)
BatchItem = tuple[np.ndarray, asyncio.Future]
InferBatch = Callable[[list[np.ndarray]], list[dict[str, Any]]]

_batch_queues: dict[str, asyncio.Queue[BatchItem]] = {}
_batch_tasks: dict[str, asyncio.Task[None]] = {}


async def _batch_worker(
    model_type: str,
    queue: asyncio.Queue[BatchItem],
    infer_batch: InferBatch,
    batch_size: int,
) -> None:
    loop = asyncio.get_running_loop()
    while True:
        first = await queue.get()
        batch: list[BatchItem] = [first]
        while len(batch) < batch_size:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        wavs = [item[0] for item in batch]
        if len(wavs) > 1:
            logger.info(f"[asr] batch_size={len(wavs)}")

        futures = [item[1] for item in batch]
        try:
            t0 = time.perf_counter()
            results = await loop.run_in_executor(None, infer_batch, wavs)
            elapsed = time.perf_counter() - t0
            logger.info(f"[asr] model_type={model_type} batch_size={len(wavs)} elapsed={elapsed * 1000:.3f}ms")
        except Exception as exc:
            for fut in futures:
                if not fut.done():
                    fut.set_exception(exc)
            continue

        for fut, res in zip(futures, results):
            if not fut.done():
                fut.set_result(res)


def _ensure_batch_worker(worker: object) -> "asyncio.Queue[BatchItem]":
    model_type = worker.model_type
    loop = asyncio.get_running_loop()
    queue = _batch_queues.get(model_type)
    if queue is None:
        queue = asyncio.Queue()
        _batch_queues[model_type] = queue

    task = _batch_tasks.get(model_type)
    if task is None or task.done():
        _batch_tasks[model_type] = loop.create_task(
            _batch_worker(model_type, queue, worker.infer_batch, worker._BATCH_SIZE)
        )
    return queue
