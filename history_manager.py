#!/usr/bin/env python3
"""
history_manager.py - 本地会话历史与对话持久化数据库
基于 SQLite 构建，存储多会话列表、消息流、时间戳与元数据。
"""

import os
import sqlite3
import time
import uuid
import json
from typing import List, Dict, Optional, Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_DIR = os.path.join(BASE_DIR, "memory")
os.makedirs(MEMORY_DIR, exist_ok=True)
DB_PATH = os.path.join(MEMORY_DIR, "chat_history.db")


class HistoryManager:
    """SQLite 本地对话历史管理器"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._active_session_id = None
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    metadata TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)")
            conn.commit()
        os.chmod(self.db_path, 0o600)

    def create_session(self, title: Optional[str] = None) -> Dict[str, Any]:
        """创建新会话"""
        session_id = str(uuid.uuid4())
        now = time.time()
        if not title:
            title = "新对话"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, title, now, now)
            )
            conn.commit()

        self._active_session_id = session_id
        return {
            "id": session_id,
            "title": title,
            "created_at": now,
            "updated_at": now
        }

    def get_or_create_active_session(self) -> Dict[str, Any]:
        """获取当前活跃会话，若无则新建"""
        if self._active_session_id:
            session = self.get_session(self._active_session_id)
            if session:
                return session

        # 尝试读取最近的一个会话
        sessions = self.list_sessions()
        if sessions:
            self._active_session_id = sessions[0]["id"]
            return sessions[0]

        return self.create_session()

    def set_active_session(self, session_id: str) -> bool:
        if not self.get_session(session_id):
            return False
        self._active_session_id = session_id
        return True

    def get_active_session_id(self) -> str:
        return self.get_or_create_active_session()["id"]

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

    def list_sessions(self) -> List[Dict[str, Any]]:
        """获取所有会话列表（按更新时间倒序）"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sessions ORDER BY updated_at DESC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def rename_session(self, session_id: str, new_title: str) -> bool:
        """重命名会话"""
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (new_title.strip(), now, session_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_session(self, session_id: str) -> bool:
        """删除指定会话及其所有消息"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            if self._active_session_id == session_id:
                self._active_session_id = None
            return cursor.rowcount > 0

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """向会话添加一条消息，并自动刷新会话更新时间与首句标题"""
        message_id = str(uuid.uuid4())
        now = time.time()
        meta_str = json.dumps(metadata, ensure_ascii=False) if metadata else None

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO messages (id, session_id, role, content, created_at, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                (message_id, session_id, role, content, now, meta_str)
            )
            cursor.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id)
            )
            conn.commit()

        # 如果是用户发出的第一条消息且标题还是默认标题，自动生成智能简短标题
        if role == "user":
            self._maybe_update_session_title(session_id, content)

        return {
            "id": message_id,
            "session_id": session_id,
            "role": role,
            "content": content,
            "created_at": now,
            "metadata": metadata
        }

    def _maybe_update_session_title(self, session_id: str, first_query: str):
        session = self.get_session(session_id)
        if session and session.get("title") in ["新对话", "未命名对话", "New Chat", ""]:
            # 截取前 18 个字符作为清晰标题
            clean_title = first_query.replace("\n", " ").strip()
            if len(clean_title) > 20:
                clean_title = clean_title[:20] + "..."
            if clean_title:
                self.rename_session(session_id, clean_title)

    def get_session_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """获取指定会话的所有历史消息"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,)
            )
            rows = cursor.fetchall()
            messages = []
            for r in rows:
                item = dict(r)
                if item.get("metadata"):
                    try:
                        item["metadata"] = json.loads(item["metadata"])
                    except Exception:
                        pass
                messages.append(item)
            return messages

    def clear_all(self):
        """清空所有历史数据"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM messages")
            cursor.execute("DELETE FROM sessions")
            conn.commit()
            self._active_session_id = None


history_manager = HistoryManager()


if __name__ == "__main__":
    print("Testing HistoryManager...")
    s = history_manager.create_session("测试会话 1")
    print("Created session:", s)
    m1 = history_manager.add_message(s["id"], "user", "2026年最新发布的AI模型有哪些？")
    m2 = history_manager.add_message(s["id"], "assistant", "Anthropic 发布了 Claude 3.7，OpenAI 发布了 GPT-5.6。")
    print("Messages in session:", history_manager.get_session_messages(s["id"]))
    print("All sessions:", history_manager.list_sessions())
    print("✅ HistoryManager test passed!")
