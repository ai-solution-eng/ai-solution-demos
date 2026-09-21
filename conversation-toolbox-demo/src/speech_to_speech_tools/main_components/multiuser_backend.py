# multi_user_backend.py - Add these to your main.py or create new file
import asyncio
import json
import logging
import uuid
from datetime import datetime
from os.path import join as pj

from fastapi import WebSocket
from utils.audio_handling import (
    SUPPORTED_AUDIO_FORMATS,
)

from main_components.constants import TRANSCRIPTS_DIR

# Supported audio formats (re-exported for backwards compat)
SUPPORTED_FORMATS = dict(SUPPORTED_AUDIO_FORMATS)
# Maximum file size (100MB)
MAX_FILE_SIZE = 100 * 1024 * 1024

logger = logging.getLogger(__name__)
__all__ = [
    "multi_user_manager",
    "return_uuid",
]


def return_uuid(input_name: str | None = None) -> str:
    return input_name or str(uuid.uuid4())


# ==============================================================================
# MULTI-USER REAL-TIME TRANSCRIPTION
# ==============================================================================
class MultiUserSession:
    """Manages multiple users in a single transcription session"""

    def __init__(self, session_id: str | None = None):
        self.session_id = return_uuid(session_id)
        self.users: dict[str, dict] = {}  # user_id -> {websocket, name, audio_buffer}
        self.transcript_path = pj(TRANSCRIPTS_DIR, f"multiuser_{self.session_id}.txt")
        self.lock = asyncio.Lock()
        self._init_transcript_file()

    def _init_transcript_file(self):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.transcript_path, "w", encoding="utf-8") as f:
            f.write(f"=== Multi-User Session Started: {self.session_id} at {timestamp} ===\n")
            f.write("=== Users will be identified as User_1, User_2, etc. ===\n\n")

    async def add_user(self, user_id: str, websocket: WebSocket, name: str | None = None):
        async with self.lock:
            user_number = len(self.users) + 1
            self.users[user_id] = {
                "websocket": websocket,
                "name": name or f"User_{user_number}",
                "user_number": user_number,
                "user_id": user_id,
                "last_activity": datetime.now(),
            }
            await self._log_event(f"{self.users[user_id]['name']} joined the session")
            return self.users[user_id]

    async def remove_user(self, user_id: str):
        async with self.lock:
            if user_id in self.users:
                user_info = self.users.pop(user_id)
                await self._log_event(f"{user_info['name']} left the session")
                return True
            return False

    async def _log_event(self, message: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] [EVENT] {message}\n"
        try:
            await asyncio.to_thread(self._write_file, self.transcript_path, entry)
        except Exception as e:
            logger.error(f"Failed to write event: {e}")

    @staticmethod
    def _write_file(path: str, content: str, mode: str = "a") -> None:
        with open(path, mode, encoding="utf-8") as f:
            f.write(content)

    async def log_transcript(self, user_id: str, text: str):
        async with self.lock:
            if user_id not in self.users:
                return
            user_info = self.users[user_id]
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            entry = f"[{timestamp}] [{user_info['name']}] {text}\n"
            user_info["last_activity"] = datetime.now()
            try:
                await asyncio.to_thread(self._write_file, self.transcript_path, entry)
            except Exception as e:
                logger.error(f"Failed to write transcript: {e}")
            # Broadcast to all users
            await self._broadcast_transcript(user_id, user_info["name"], text, timestamp)

    async def _broadcast_transcript(self, sender_id: str, sender_name: str, text: str, timestamp: str):
        message = json.dumps(
            {
                "type": "transcript",
                "sender_id": sender_id,
                "sender_name": sender_name,
                "text": text,
                "timestamp": timestamp,
            }
        )

        async def _safe_send(uid: str, ws) -> str | None:
            try:
                await ws.send_text(message)
            except Exception:
                return uid
            return None

        results = await asyncio.gather(*(_safe_send(uid, info["websocket"]) for uid, info in self.users.items()))
        for uid in results:
            if uid is not None:
                await self.remove_user(uid)

    async def broadcast_to_all(self, message: dict):
        """Broadcast a message to all users in the session"""
        msg = json.dumps(message)

        async def _safe_send(uid: str, ws) -> str | None:
            try:
                await ws.send_text(msg)
            except Exception:
                return uid
            return None

        results = await asyncio.gather(*(_safe_send(uid, info["websocket"]) for uid, info in self.users.items()))
        for uid in results:
            if uid is not None:
                await self.remove_user(uid)

    def get_user_count(self) -> int:
        return len(self.users)

    def get_user_list(self) -> list[dict]:
        return [
            {"user_id": uid, "name": info["name"], "user_number": info["user_number"]}
            for uid, info in self.users.items()
        ]


class MultiUserSessionManager:
    """Manages multiple multi-user sessions"""

    def __init__(self) -> None:
        self.sessions: dict[str, MultiUserSession] = {}
        self.user_sessions: dict[str, str] = {}  # user_id -> session_id

    def create_session(self, session_id: str | None = None) -> MultiUserSession:
        session_id = return_uuid(session_id)
        session = MultiUserSession(session_id)
        self.sessions[session.session_id] = session
        return session

    def get_session(self, session_id: str) -> MultiUserSession | None:
        return self.sessions.get(session_id)

    def remove_session(self, session_id: str):
        if session_id in self.sessions:
            del self.sessions[session_id]


# Global session manager
multi_user_manager = MultiUserSessionManager()
