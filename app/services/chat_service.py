"""Chat service — one turn end to end (ARCHITECTURE §3.2).

lock(session) → load stored prompt + history → LiteLLM via failover → measure latency
→ persist session + both messages atomically → response.
If the LLM step raises (AllProvidersUnavailable → 503) nothing is written.
"""

from __future__ import annotations

import logging
import time

from app.core.config import Settings
from app.db.database import Database
from app.db.repository import persist_successful_turn
from app.schemas.chat import ChatMetadata, ChatRequest, ChatResponse
from app.services.llm_service import LLMService, resolve_system_prompt, to_metadata
from app.services.session_service import SessionService
from app.services.simulation import SimulationPlan

logger = logging.getLogger("app.chat")


class ChatService:
    def __init__(
        self, settings: Settings, database: Database, sessions: SessionService, llm: LLMService
    ) -> None:
        self.settings = settings
        self.database = database
        self.sessions = sessions
        self.llm = llm

    async def handle(
        self, request: ChatRequest, plan: SimulationPlan, started: float
    ) -> ChatResponse:
        async with self.sessions.lock(request.session_id):
            context = await self.sessions.load_context(request.session_id)
            system_prompt = resolve_system_prompt(
                request.system_prompt,
                context.stored_system_prompt,
                self.settings.default_system_prompt,
            )
            result, _ = await self.llm.generate(
                system_prompt=system_prompt,
                history=context.history,
                user_message=request.message,
                plan=plan,
            )
            latency_ms = int(round((time.perf_counter() - started) * 1000))
            metadata = to_metadata(result, latency_ms)
            async with self.database.session() as db:
                await persist_successful_turn(
                    db,
                    session_id=request.session_id,
                    # only an explicit, non-blank prompt is persisted / overrides
                    system_prompt=request.system_prompt,
                    user_content=request.message,
                    assistant_content=result.reply,
                    assistant_metadata=metadata,
                )
        logger.info(
            "chat turn stored session=%s new_session=%s source=%s latency_ms=%d",
            request.session_id,
            not context.exists,
            result.resolution_source,
            latency_ms,
        )
        return ChatResponse(
            session_id=request.session_id,
            reply=result.reply,
            metadata=ChatMetadata(**metadata),
        )
