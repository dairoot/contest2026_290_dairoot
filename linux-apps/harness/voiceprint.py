"""harness 本地声纹库与 ASR 接入；embedding 不放进对话消息或浏览器状态。"""

from collections import OrderedDict
from datetime import datetime, timezone
from inspect import signature
import logging
from pathlib import Path

import numpy as np
from sqlalchemy import JSON, Text, URL, create_engine, delete, inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from aichat_sdk import ChatBot
from session_store import Base, _read_database

logger = logging.getLogger("web_console")
MIN_SPEECH_MS = 1500
MAX_SAMPLES = 10
MAX_UTTERANCES = 200


class Voiceprint(Base):
    __tablename__ = "voiceprints"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    embeddings: Mapped[list] = mapped_column(JSON)
    updated_at: Mapped[str] = mapped_column(Text)


def normalize(embedding) -> np.ndarray:
    try:
        vector = np.asarray(embedding, dtype=np.float64)
    except (TypeError, ValueError):
        raise ValueError("声纹 embedding 格式无效") from None
    if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
        raise ValueError("声纹 embedding 必须是非空的有限数值向量")
    norm = np.linalg.norm(vector)
    if not np.isfinite(norm) or norm <= 0:
        raise ValueError("声纹 embedding 不能为零向量")
    return vector / norm


class VoiceprintStore:
    def __init__(self, path: str):
        self.path = path
        self._profiles: dict[str, list[np.ndarray]] | None = None
        self.error = ""

    def _load(self):
        if self._profiles is not None:
            return
        profiles = {}
        try:
            with _read_database(self.path) as session:
                # 兼容升级前只有聊天记录的数据库；读取不创建目录或表。
                if session is not None and inspect(session.bind).has_table("voiceprints"):
                    for record in session.scalars(select(Voiceprint)):
                        vectors = [normalize(value) for value in record.embeddings]
                        if not vectors or len({v.size for v in vectors}) != 1:
                            raise ValueError("声纹样本维度不一致或为空")
                        profiles[record.name] = vectors
        except (OSError, SQLAlchemyError, ValueError, TypeError):
            self.error = "读取声纹数据库失败，请检查 data/history.sqlite3"
            logger.exception(self.error)
        self._profiles = profiles

    def summary(self) -> list[dict]:
        self._load()
        return [{"name": name, "samples": len(vectors)} for name, vectors in sorted(self._profiles.items())]

    def add(self, name: str, vector: np.ndarray):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
            raise ValueError("姓名需为 1～40 个字符")
        name = name.strip()
        vector = normalize(vector)
        self._load()
        if self.error:
            raise ValueError(self.error)
        for other_name, vectors in self._profiles.items():
            for sample in vectors:
                if sample.size != vector.size:
                    raise ValueError("声纹模型维度已变化，请先删除旧声纹再录入")
                if np.allclose(sample, vector, atol=1e-6, rtol=0):
                    if other_name == name:
                        return name  # 同一句话重复点录入，不重复计数。
                    raise ValueError(f"这条声纹已录入为“{other_name}”")
        vectors = [*self._profiles.get(name, []), vector][-MAX_SAMPLES:]
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(URL.create("sqlite+pysqlite", database=self.path))
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session, session.begin():
                session.merge(Voiceprint(
                    name=name, embeddings=[sample.tolist() for sample in vectors],
                    updated_at=datetime.now(timezone.utc).isoformat(),
                ))
        finally:
            engine.dispose()
        self._profiles[name] = vectors  # 事务成功后才更新内存。
        return name

    def remove(self, name: str):
        self._load()
        if self.error:
            raise ValueError(self.error)
        if name not in self._profiles:
            raise ValueError("声纹不存在")
        engine = create_engine(URL.create("sqlite+pysqlite", database=self.path))
        try:
            with Session(engine) as session, session.begin():
                session.execute(delete(Voiceprint).where(Voiceprint.name == name))
        finally:
            engine.dispose()
        del self._profiles[name]


class VoiceprintClient:
    """只暂存本轮最近 200 句的 embedding；点录入才写入本地数据库。"""

    def __init__(self, path: str):
        self.store = VoiceprintStore(path)
        self.utterances = OrderedDict()

    def capture(self, payload: dict):
        audio_url = payload.get("audio_url")
        if not isinstance(audio_url, str) or not audio_url:
            return
        vector, error = None, ""
        try:
            speech_ms = float(payload.get("speech_ms") or 0)
            if not np.isfinite(speech_ms) or speech_ms < MIN_SPEECH_MS:
                raise ValueError("需要至少 1.5 秒有效语音，请重新说一句长一些的话")
            vector = normalize(payload.get("embedding"))
        except (ValueError, TypeError):
            error = "语音不足 1.5 秒或无法提取声纹，请重新说一句长一些的话"
        self.utterances[audio_url] = {
            "vector": vector,
            "speaker": payload.get("speaker") if isinstance(payload.get("speaker"), str) else "",
            "enrolled_name": "", "error": error,
        }
        self.utterances.move_to_end(audio_url)
        while len(self.utterances) > MAX_UTTERANCES:
            self.utterances.popitem(last=False)

    def status(self) -> dict:
        return {
            "profiles": self.store.summary(), "error": self.store.error,
            "utterances": {
                url: {key: value for key, value in item.items() if key != "vector"}
                for url, item in self.utterances.items()
            },
        }

    def enroll(self, audio_url: str, name: str):
        item = self.utterances.get(audio_url)
        if item is None:
            raise ValueError("这条消息的声纹已失效，请在当前会话重新说一句话")
        if item["error"]:
            raise ValueError(item["error"])
        item["enrolled_name"] = self.store.add(name, item["vector"])

    def remove(self, name: str):
        self.store.remove(name)
        for item in self.utterances.values():
            if item["enrolled_name"] == name:
                item["enrolled_name"] = ""


class VoiceprintChatBot(ChatBot):
    """说话人由 SDK 原样透传；这里只缓存语音样本供手动录入。"""

    def __init__(self, *, voiceprints: VoiceprintClient, **kwargs):
        if "speaker" not in signature(ChatBot.send_text).parameters:
            raise RuntimeError("当前 aichat-sdk 不支持 speaker 参数，请更新 SDK；本地联调可用 PYTHONPATH 指向 SDK 源码目录")
        super().__init__(**kwargs)
        self.voiceprints = voiceprints
        self.voiceprints.utterances.clear()
        # 先于 SDK 的 reg_asr 注册，只保留供手动录入的 embedding，不改写 speaker。
        self.asr.on("stt")(voiceprints.capture)
