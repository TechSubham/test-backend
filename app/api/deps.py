from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.core.config import Settings
from app.db.database import Database
from app.services.cache_service import CacheStatus
from app.services.chat_service import ChatService
from app.services.failover import KeyPool
from app.services.session_service import SessionService


@dataclass
class AppState:
    settings: Settings
    database: Database
    key_pool: KeyPool
    cache: CacheStatus
    sessions: SessionService
    chat: ChatService


def get_state(request: Request) -> AppState:
    return request.app.state.services
