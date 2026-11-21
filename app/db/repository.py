"""Repositories — the only place that issues SQL.

All message queries are scoped by `session_id` (session isolation, DATABASE_SCHEMA.md §4.3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import utc_now_iso
from app.db.models import MessageRow, SessionRow


@dataclass(frozen=True)
class StoredMessage:
    id: int
    role: str
    content: str
    metadata: dict[str, Any] | None
    created_at: str


def _to_stored(row: MessageRow) -> StoredMessage:
    return StoredMessage(
        id=row.id,
        role=row.role,
        content=row.content,
        metadata=json.loads(row.metadata_json) if row.metadata_json else None,
        created_at=row.created_at,
    )


class SessionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, session_id: str) -> SessionRow | None:
        return await self.db.get(SessionRow, session_id)

    async def exists(self, session_id: str) -> bool:
        result = await self.db.execute(
            select(SessionRow.session_id).where(SessionRow.session_id == session_id)
        )
        return result.scalar_one_or_none() is not None

    async def get_system_prompt(self, session_id: str) -> str | None:
        result = await self.db.execute(
            select(SessionRow.system_prompt).where(SessionRow.session_id == session_id)
        )
        return result.scalar_one_or_none()

    async def ensure(self, session_id: str, system_prompt: str | None) -> None:
        """Create the session if absent; if a non-blank prompt is given, store it."""
        stmt = (
            sqlite_insert(SessionRow)
            .values(session_id=session_id, system_prompt=system_prompt, created_at=utc_now_iso())
            .on_conflict_do_nothing(index_elements=["session_id"])
        )
        result = await self.db.execute(stmt)
        if result.rowcount == 0 and system_prompt is not None:
            await self.db.execute(
                update(SessionRow)
                .where(SessionRow.session_id == session_id)
                .values(system_prompt=system_prompt)
            )


class MessageRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def add(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        row = MessageRow(
            session_id=session_id,
            role=role,
            content=content,
            metadata_json=json.dumps(metadata, separators=(",", ":")) if metadata else None,
            created_at=utc_now_iso(),
        )
        self.db.add(row)
        await self.db.flush()
        return row.id

    async def list_for_session(self, session_id: str) -> list[StoredMessage]:
        result = await self.db.execute(
            select(MessageRow).where(MessageRow.session_id == session_id).order_by(MessageRow.id)
        )
        return [_to_stored(r) for r in result.scalars()]

    async def recent_for_session(self, session_id: str, limit: int) -> list[StoredMessage]:
        """Last `limit` messages, returned oldest first (LLM context window)."""
        if limit <= 0:
            return []
        result = await self.db.execute(
            select(MessageRow)
            .where(MessageRow.session_id == session_id)
            .order_by(MessageRow.id.desc())
            .limit(limit)
        )
        rows = list(result.scalars())
        rows.reverse()
        return [_to_stored(r) for r in rows]


async def persist_successful_turn(
    db: AsyncSession,
    *,
    session_id: str,
    system_prompt: str | None,
    user_content: str,
    assistant_content: str,
    assistant_metadata: dict[str, Any],
) -> None:
    """Write one successful chat turn atomically (session upsert + 2 messages).

    Called only after the LLM succeeded, so failed requests never touch the DB.
    """
    async with db.begin():
        await SessionRepository(db).ensure(session_id, system_prompt)
        messages = MessageRepository(db)
        await messages.add(session_id, "user", user_content)
        await messages.add(session_id, "assistant", assistant_content, assistant_metadata)
