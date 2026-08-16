"""解析 modelscope 模型的本地缓存目录。

modelscope 1.38 的缓存布局是 `models/<owner>--<name>/snapshots/master`，老版本是
`hub/models/<owner>/<name>`。两种都查一遍；都没命中就把 model id 原样交给 hub 解析
（会联网）。板子上离线或网络不稳时，命中本地缓存才能正常启动。
"""

import os

_CACHE_ROOT = os.path.expanduser(
    os.environ.get("MODELSCOPE_CACHE", "~/.cache/modelscope")
)


def resolve_model_path(model_id: str, revision: str = "master") -> str:
    """revision 要和加载时传的 model_revision 一致，缓存目录是按 revision 分的。"""
    for path in _candidates(model_id, revision):
        if os.path.isdir(path):
            return path
    return model_id


def ensure_model_dir(model_id: str, revision: str = "master") -> str:
    """返回本地目录；没有就下载。

    onnxruntime 只认真实路径，不像 funasr / modelscope 能接受 model id，
    所以这里必须落到目录。
    """
    for path in _candidates(model_id, revision):
        if os.path.isdir(path):
            return path

    from modelscope.hub.snapshot_download import snapshot_download

    return snapshot_download(model_id)


def _candidates(model_id: str, revision: str) -> tuple[str, ...]:
    return (
        os.path.join(_CACHE_ROOT, "models", model_id.replace("/", "--"), "snapshots", revision),
        os.path.join(_CACHE_ROOT, "hub", "models", model_id),
    )
