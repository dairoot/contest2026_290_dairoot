"""具名声纹的本地存储，不加载识别模型。"""

import json
import os
import tempfile
from pathlib import Path

import numpy as np


MIN_ENROLLMENT_SPEECH_MS = 1000
MAX_SAMPLES_PER_NAME = 10


class VoiceprintFileError(ValueError):
    pass


def default_profile_path():
    default = Path(__file__).resolve().parent / "speaker_profiles" / "voiceprints.json"
    return Path(os.environ.get("SPEAKER_PROFILE_PATH", str(default))).expanduser()


class VoiceprintStore:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self._speakers: dict[str, list[np.ndarray]] = {}

    @staticmethod
    def validate_name(name):
        if not isinstance(name, str):
            raise ValueError("请填写姓名")
        name = name.strip()
        if not name or len(name) > 40 or any(ord(char) < 32 for char in name):
            raise ValueError("姓名须为 1～40 个字符，不能包含换行等控制字符")
        return name

    @staticmethod
    def prepare_embedding(embedding):
        try:
            vector = np.asarray(embedding, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise ValueError("该消息没有有效声纹") from exc
        if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
            raise ValueError("该消息没有有效声纹")
        norm = np.linalg.norm(vector)
        if not np.isfinite(norm) or norm < 1e-8:
            raise ValueError("该消息没有有效声纹")
        return vector / norm

    @property
    def samples(self):
        return [
            {"name": name, "embedding": embedding}
            for name, embeddings in self._speakers.items()
            for embedding in embeddings
        ]

    def summary(self):
        return [
            {"name": name, "samples": len(embeddings)}
            for name, embeddings in self._speakers.items()
        ]

    def load(self):
        if not self.path.exists():
            self._speakers = {}
            return
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if document["version"] != 1 or not isinstance(document["speakers"], list):
                raise ValueError("不支持的声纹文件格式")
            speakers = {}
            dimension = None
            for item in document["speakers"]:
                name = self.validate_name(item["name"])
                embeddings = item["embeddings"]
                if name in speakers or not isinstance(embeddings, list) or not 1 <= len(embeddings) <= MAX_SAMPLES_PER_NAME:
                    raise ValueError("声纹文件包含重复姓名或无效样本数量")
                vectors = [self.prepare_embedding(embedding) for embedding in embeddings]
                for vector in vectors:
                    if dimension is not None and vector.size != dimension:
                        raise ValueError("声纹文件的向量维度不一致")
                    dimension = vector.size
                speakers[name] = vectors
        except (KeyError, TypeError, ValueError) as exc:
            raise VoiceprintFileError(f"无效的声纹文件格式: {exc}") from exc
        self._speakers = speakers

    def add(self, name, embedding):
        name = self.validate_name(name)
        vector = self.prepare_embedding(embedding)
        for sample in self.samples:
            if sample["embedding"].size != vector.size:
                raise ValueError("声纹维度与已有资料不一致，请使用同一声纹模型")
            if np.allclose(sample["embedding"], vector, rtol=1e-5, atol=1e-6):
                if sample["name"] == name:
                    return False
                raise ValueError(f"该条声纹已录入“{sample['name']}”，请选择其他消息")
        previous = self._speakers.get(name, [])
        if len(previous) >= MAX_SAMPLES_PER_NAME:
            raise ValueError(f"“{name}”已录入 {MAX_SAMPLES_PER_NAME} 条声纹")
        updated = {**self._speakers, name: [*previous, vector]}
        self._save(updated)
        self._speakers = updated
        return True

    def _save(self, speakers):
        document = {
            "version": 1,
            "speakers": [
                {"name": name, "embeddings": [vector.tolist() for vector in embeddings]}
                for name, embeddings in speakers.items()
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as file:
                temporary_path = Path(file.name)
                json.dump(document, file, ensure_ascii=False, allow_nan=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
