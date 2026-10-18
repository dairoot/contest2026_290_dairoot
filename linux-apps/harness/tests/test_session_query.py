"""历史会话 HTTP 查询回归；使用真实临时 SQLite，不启动音频或 ChatBot。"""

from contextlib import closing
import copy
import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.exc import OperationalError
from starlette.testclient import TestClient

with patch("dotenv.load_dotenv"):
    import main

from server import create_app
from session_store import _read_database, save_session


STARTED_AT = datetime.datetime(2026, 10, 18, 3, 4, 5, tzinfo=datetime.timezone.utc)


class SessionQueryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / "history.sqlite3"
        db_path = patch.object(main, "SESSION_DB_PATH", str(self.path))
        db_path.start()
        self.addCleanup(db_path.stop)
        self.client = main.AIClient(copy.deepcopy(main.DEFAULT_CONFIG))
        self.web = TestClient(create_app(self.client))
        self.web.__enter__()
        self.addCleanup(self.web.__exit__, None, None, None)

    def save(self, session_id, messages, started_at=STARTED_AT):
        save_session(str(self.path), session_id, started_at, started_at + datetime.timedelta(minutes=2), messages)

    def seed(self):
        self.save("older", [{"role": "user", "content": "旧会话"}], STARTED_AT - datetime.timedelta(days=1))
        self.save("newer-a", [{"role": "user", "content": "看摄像头"}])
        self.save("newer-b", [
            {"role": "system", "content": "系统提示词不入库"},
            {"role": "user", "content": json.dumps({"text": "把客厅灯调暗", "round": 1}, ensure_ascii=False)},
            {"role": "assistant", "content": None},
            {"role": "tool", "content": "亮度设置成功"},
            {"role": "assistant", "content": "已调到 50%", "usage": {"total_tokens": 42}},
        ])

    def test_missing_database_is_empty_without_creating_files(self):
        response = self.web.get("/api/sessions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "sessions": [], "total": 0, "limit": 20, "offset": 0})
        self.assertEqual(self.web.get("/api/sessions/unknown").status_code, 404)
        self.assertFalse(self.path.parent.exists())

    def test_list_is_paginated_and_ordered_with_counts_and_user_preview(self):
        self.seed()
        response = self.web.get("/api/sessions", params={"limit": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        data = response.json()
        self.assertEqual((data["total"], data["limit"], data["offset"]), (3, 2, 0))
        self.assertEqual([row["id"] for row in data["sessions"]], ["newer-b", "newer-a"])
        latest = data["sessions"][0]
        self.assertEqual(latest["preview"], "把客厅灯调暗")
        self.assertEqual(latest["message_count"], 4)
        self.assertEqual(latest["started_at"], STARTED_AT.isoformat())
        self.assertNotIn("messages", latest)
        page = self.web.get("/api/sessions", params={"limit": 2, "offset": 2}).json()
        self.assertEqual([row["id"] for row in page["sessions"]], ["older"])
        self.assertEqual(page["total"], 3)
        self.assertEqual(self.web.get("/api/sessions", params={"offset": 100}).json()["sessions"], [])

    def test_search_matches_ids_and_any_message_without_duplicate_sessions(self):
        self.seed()
        for query in ("newer-b", "客厅", "亮度设置成功", "50%", "  客厅  "):
            with self.subTest(query=query):
                data = self.web.get("/api/sessions", params={"q": query}).json()
                self.assertEqual(data["total"], 1)
                self.assertEqual([row["id"] for row in data["sessions"]], ["newer-b"])
        self.assertEqual(self.web.get("/api/sessions", params={"q": "不存在"}).json()["total"], 0)
        self.assertEqual(self.web.get("/api/sessions", params={"q": "系统提示词"}).json()["total"], 0)
        self.assertEqual(self.web.get("/api/sessions", params={"q": "newer", "limit": 1}).json()["total"], 2)

    def test_search_treats_sql_wildcards_and_quotes_as_literal_text(self):
        self.save("literal", [{"role": "user", "content": "测试 100% a_b / 路径 ' OR 1=1 --"}])
        self.save("plain", [{"role": "user", "content": "普通消息"}])
        for query in ("%", "_", "/", "' OR 1=1 --"):
            with self.subTest(query=query):
                data = self.web.get("/api/sessions", params={"q": query}).json()
                self.assertEqual(data["total"], 1)
                self.assertEqual(data["sessions"][0]["id"], "literal")

    def test_detail_keeps_content_usage_and_message_order(self):
        self.seed()
        response = self.web.get("/api/sessions/newer-b")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        record = response.json()["session"]
        self.assertEqual(record["id"], "newer-b")
        self.assertEqual(record["started_at"], STARTED_AT.isoformat())
        self.assertEqual(record["ended_at"], (STARTED_AT + datetime.timedelta(minutes=2)).isoformat())
        self.assertEqual([message["position"] for message in record["messages"]], [0, 1, 2, 3])
        self.assertEqual([message["role"] for message in record["messages"]], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(record["messages"][0]["content"], "把客厅灯调暗")
        self.assertIsNone(record["messages"][1]["content"])
        self.assertIsNone(record["messages"][1]["usage"])
        self.assertEqual(record["messages"][-1]["usage"], {"total_tokens": 42})
        self.assertIsNone(self.client.chat_bot)

    def test_detail_preserves_structured_empty_and_null_content(self):
        content = [{"type": "text", "text": "你好"}]
        self.save("structured", [
            {"role": "user", "content": content},
            {"role": "assistant", "content": "", "usage": {}},
            {"role": None, "content": None},
        ])
        messages = self.web.get("/api/sessions/structured").json()["session"]["messages"]
        self.assertEqual(json.loads(messages[0]["content"]), content)
        self.assertEqual(messages[1]["content"], "")
        self.assertEqual(messages[1]["usage"], {})
        self.assertIsNone(messages[2]["role"])
        self.assertIsNone(messages[2]["content"])

    def test_detail_returns_speaker_and_message_send_time_with_original_timezone(self):
        sent_at = "2026-10-18T13:09:44.584297+08:00"
        reply_at = "2026-10-18T13:09:45.123456+08:00"
        content = json.dumps({"text": "你好", "time": sent_at, "speaker": "张三"}, ensure_ascii=False)
        self.save("times", [
            {"role": "user", "content": content},
            {"role": "assistant", "content": "你好", "time": reply_at, "speaker": "助手"},
            {"role": "tool", "content": None},
        ])
        response = self.web.get("/api/sessions/times")
        self.assertEqual(response.status_code, 200)
        messages = response.json()["session"]["messages"]
        self.assertEqual([message["time"] for message in messages], [sent_at, reply_at, None])
        self.assertEqual([message["speaker"] for message in messages], ["张三", "助手", None])
        self.assertEqual(messages[0]["content"], "你好")

    def test_search_matches_user_text_without_matching_content_metadata(self):
        self.save("weather", [{"role": "user", "content": json.dumps({
            "text": "查一下天气。", "time": "2026-10-18T18:43:21.916778+08:00",
            "round": 1, "speaker": "unknown",
        }, ensure_ascii=False)}])
        result = self.web.get("/api/sessions", params={"q": "天气"}).json()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["sessions"][0]["preview"], "查一下天气。")
        self.assertEqual(self.web.get("/api/sessions", params={"q": "unknown"}).json()["total"], 0)

    def test_user_text_that_looks_like_json_is_not_parsed_again_in_history(self):
        text = '{"text":"正文里的 JSON", "speaker":"字面值"}'
        self.save("json-text", [{"role": "user", "content": json.dumps({
            "text": text, "time": "2026-10-18T18:43:21.916778+08:00", "speaker": "张三",
        }, ensure_ascii=False)}])
        listing = self.web.get("/api/sessions").json()
        self.assertEqual(listing["sessions"][0]["preview"], text)
        message = self.web.get("/api/sessions/json-text").json()["session"]["messages"][0]
        self.assertEqual(message["content"], text)
        self.assertEqual(message["speaker"], "张三")

    def test_unknown_session_returns_404(self):
        self.seed()
        response = self.web.get("/api/sessions/unknown")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["ok"])

    def test_invalid_pagination_and_long_query_return_400(self):
        for params in ({"limit": "abc"}, {"limit": 0}, {"limit": 101}, {"offset": -1},
                       {"offset": "1.5"}, {"q": "长" * 201}):
            with self.subTest(params=params):
                response = self.web.get("/api/sessions", params=params)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["ok"])
        self.assertFalse(self.path.parent.exists())

    def test_corrupt_database_returns_503_without_affecting_live_state(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"not a sqlite database")
        for url in ("/api/sessions", "/api/sessions/unknown"):
            with self.subTest(url=url), self.assertLogs("web_console", level="ERROR"):
                response = self.web.get(url)
                self.assertEqual(response.status_code, 503)
                self.assertFalse(response.json()["ok"])
        self.assertEqual(self.web.get("/api/state").status_code, 200)

    def test_queries_do_not_modify_database_and_connection_is_read_only(self):
        self.seed()
        before = self.path.read_bytes()
        self.web.get("/api/sessions")
        self.web.get("/api/sessions/newer-b")
        self.assertEqual(self.path.read_bytes(), before)
        with _read_database(str(self.path)) as session:
            with self.assertRaises(OperationalError):
                session.connection().exec_driver_sql("DELETE FROM sessions")
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM sessions").fetchone()[0], 3)
        self.assertEqual(list(self.path.parent.glob("*.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
