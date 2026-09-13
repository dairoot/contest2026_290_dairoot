"""判断 RKNN NPU 是否可用。

默认后端是 NPU（板子上的常态），但同一份代码也要能在开发机上跑起来——那里既没有
rknn-toolkit-lite2（只有 aarch64 有包），也没有几百 MB 的 .rknn。这里做一次探测，
不可用就让调用方回退到 CPU 后端，并明确打一行日志说明当前用的是什么。
"""

import importlib.util
import logging
import os

logger = logging.getLogger(__name__)


def rknn_available(model_path: str, what: str) -> bool:
    # 只查有没有这个包，不要真的 import：rknnlite 一旦在 torch 之前被导入，会把标准库
    # logging 的等级表搞坏，torch.fx 初始化 logger 时直接 ValueError: Unknown level。
    # 真正的 import 留在各自的后端模块里，那时 torch 已经加载好了。
    if importlib.util.find_spec("rknnlite") is None:
        logger.warning("[%s] 没有 rknn-toolkit-lite2，回退到 CPU 后端", what)
        return False

    if not os.path.isfile(model_path):
        logger.warning(
            "[%s] 找不到 %s，回退到 CPU 后端；模型生成见仓库 skills/speech-rknn-migration/SKILL.md",
            what,
            model_path,
        )
        return False

    return True
