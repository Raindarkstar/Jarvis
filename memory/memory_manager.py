import json
import os
import time
import uuid
import tempfile
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from runtime_paths import memory_path

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows.
    fcntl = None
    import msvcrt


MEMORY_FILE = str(memory_path())


class MemoryManager:
    """用户长期个性化记忆管理系统。"""

    def __init__(self, storage_path: str = MEMORY_FILE):
        self.storage_path = os.path.abspath(storage_path)
        self.lock_path = f"{self.storage_path}.lock"
        self._thread_lock = threading.RLock()
        self._load()

    @staticmethod
    def _default_data() -> Dict[str, Any]:
        return {
            "profile": {
                "nickname": "",
                "home_city": "深圳",
                "preferences": [],
            },
            "memories": [],
        }

    @contextmanager
    def _file_lock(self, exclusive: bool):
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as lock_file:
            try:
                os.chmod(self.lock_path, 0o600)
            except OSError:
                pass
            if fcntl is not None:
                mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(lock_file.fileno(), mode)
            else:
                # msvcrt locks a byte range from the current file position.
                lock_file.seek(0)
                lock_file.write("0")
                lock_file.flush()
                lock_file.seek(0)
                mode = msvcrt.LK_LOCK if exclusive else msvcrt.LK_RLCK
                msvcrt.locking(lock_file.fileno(), mode, 1)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                else:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)

    def _read_unlocked(self) -> Dict[str, Any]:
        if not os.path.exists(self.storage_path):
            return self._default_data()
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("invalid memory root")
            data.setdefault("profile", self._default_data()["profile"])
            data.setdefault("memories", [])
            return data
        except (OSError, ValueError, json.JSONDecodeError):
            return self._default_data()

    def _write_unlocked(self, data: Dict[str, Any]):
        directory = os.path.dirname(self.storage_path)
        fd, temp_path = tempfile.mkstemp(prefix=".user_memory.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, self.storage_path)
            try:
                os.chmod(self.storage_path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def _load(self):
        with self._thread_lock, self._file_lock(exclusive=True):
            existed = os.path.exists(self.storage_path)
            self.data = self._read_unlocked()
            if not existed:
                self._write_unlocked(self.data)
            else:
                try:
                    os.chmod(self.storage_path, 0o600)
                except OSError:
                    pass

    def _refresh(self):
        with self._thread_lock, self._file_lock(exclusive=False):
            self.data = self._read_unlocked()

    def add_memory(self, content: str, category: str = "general") -> str:
        """记住一条新事实、偏好或待办事项。"""
        content_clean = str(content or "").strip()
        if not content_clean:
            return "记忆内容不能为空。"

        with self._thread_lock, self._file_lock(exclusive=True):
            self.data = self._read_unlocked()
            for m in self.data["memories"]:
                if m.get("content") == content_clean:
                    return f"已存在相同记忆：【{content_clean}】。"

            new_item = {
                "id": f"mem_{uuid.uuid4().hex}",
                "category": category,
                "content": content_clean,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.data["memories"].append(new_item)
            self._write_unlocked(self.data)
        return f"🧠 已成功为您记住：【{content_clean}】"

    def search_memory(self, query: str = "") -> str:
        """查询检索已保存的记忆。"""
        q = query.strip().lower()
        self._refresh()
        memories = self.data.get("memories", [])
        
        if not memories and not self.data.get("profile", {}).get("preferences"):
            return "目前尚未保存任何个性化记忆或备忘事项。"

        results = []
        for m in memories:
            text = m.get("content", "")
            cat = m.get("category", "")
            if not q or q in text.lower() or q in cat.lower():
                results.append(f"- [{cat}] {text} (记录于 {m.get('created_at', '')})")

        if not results:
            return f"未找到与 '{query}' 相关的记忆记录。"

        return "🧠 检索到以下相关记忆：\n" + "\n".join(results)

    def delete_memory(self, target: str) -> str:
        """删除或遗忘指定的记忆内容。"""
        target_clean = target.strip().lower()
        if not target_clean:
            return "请指定要遗忘或删除的关键词。"

        with self._thread_lock, self._file_lock(exclusive=True):
            self.data = self._read_unlocked()
            orig_len = len(self.data["memories"])
            self.data["memories"] = [
                m for m in self.data["memories"]
                if target_clean not in m.get("content", "").lower()
                and target_clean != m.get("id", "").lower()
            ]
            deleted_count = orig_len - len(self.data["memories"])

            if deleted_count > 0:
                self._write_unlocked(self.data)
                return f"🗑️ 已成功遗忘并删除了 {deleted_count} 条相关记忆。"
        return f"未找到包含 '{target}' 的记忆。"

    def list_memories(self) -> List[Dict[str, Any]]:
        """Return a copy of saved memories for desktop UI rendering."""
        self._refresh()
        return [dict(item) for item in self.data.get("memories", [])]

    def forget(self, target: str) -> str:
        """Compatibility alias used by the desktop client."""
        return self.delete_memory(target)

    def get_system_prompt_context(self) -> str:
        """生成用于注入大模型 System Prompt 的记忆上下文。"""
        self._refresh()
        memories = self.data.get("memories", [])
        if not memories:
            return ""

        lines = ["\n【关于用户的长期记忆与个人偏好】:"]
        for m in memories[-15:]:
            lines.append(f"- {m.get('content')}")
        return "\n".join(lines)


# 全局单例管理器
memory_manager = MemoryManager()
