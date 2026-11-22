from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.config import Settings
from app.core.errors import AllProvidersUnavailable, SessionNotFound
from app.db.database import Database
from app.db.repository import persist_successful_turn
from app.main import create_app
from app.schemas.chat import ChatRequest
from app.services.failover import (
    LIVE_INFERENCE,
    LLM_CACHE,
    FailoverService,
    KeyPool,
    classify,
)
from app.services.session_service import SessionService
from app.services.simulation import parse_simulation_header


def settings() -> Settings:
    return Settings(
        _env_file=None,
        llm_api_keys="test-key-one,test-key-two",
        llm_model="test/model",
        database_url="sqlite:///:memory:",
    )


def provider_response(reply: str = "answer", *, cache_hit: bool = False):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
        usage=SimpleNamespace(prompt_tokens=18, completion_tokens=7),
        _hidden_params={"cache_hit": cache_hit},
    )


class ChatContractTests(unittest.TestCase):
    def test_production_app_exposes_only_contract_routes(self) -> None:
        app = create_app(settings())
        paths = set(app.openapi()["paths"])
        self.assertEqual(
            paths,
            {"/health", "/chat", "/sessions/{session_id}/messages"},
        )

    def test_chat_request_trims_message_and_enforces_trimmed_limit(self) -> None:
        request = ChatRequest(session_id="s-test", message="  hi  ")
        self.assertEqual(request.message, "hi")

        with self.assertRaises(ValueError):
            ChatRequest(session_id="s-test", message="   ")
        with self.assertRaises(ValueError):
            ChatRequest(session_id="s-test", message="x" * 4001)

    def test_failure_header_accepts_supported_status_and_ignores_unknown_entries(self) -> None:
        plan = parse_simulation_header(
            "*=500,key_1=429,key_7=503,key_2=418", ["key_1", "key_2"]
        )
        self.assertEqual(plan.status_for("key_1"), 429)
        self.assertEqual(plan.status_for("key_2"), 500)
        self.assertEqual(len(plan.ignored), 2)

    def test_provider_http_errors_are_classified_for_fallback(self) -> None:
        class ProviderFailure(Exception):
            def __init__(self, status_code: int) -> None:
                super().__init__("provider failed")
                self.status_code = status_code

        self.assertEqual(classify(ProviderFailure(429)), ("rate_limit", 429))
        self.assertEqual(classify(ProviderFailure(503)), ("server_error", 503))


class FailoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = settings()
        self.pool = KeyPool.from_settings(self.settings)
        self.service = FailoverService(self.pool, self.settings)
        self.messages = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "hello"},
        ]

    def test_simulated_429_falls_forward_to_next_key(self) -> None:
        async def run():
            with (
                patch(
                    "app.services.failover.litellm.acompletion",
                    new_callable=AsyncMock,
                    return_value=provider_response(),
                ) as completion,
                patch("app.services.failover.cache_service.write_through", new_callable=AsyncMock),
            ):
                result = await self.service.complete(
                    self.messages,
                    parse_simulation_header("key_1=429", self.pool.ids),
                )
                self.assertEqual(completion.await_count, 1)
                self.assertEqual(completion.await_args.kwargs["api_key"], "test-key-two")
                self.assertEqual(result.resolution_source, LIVE_INFERENCE)
                self.assertEqual(result.key_id, "key_2")
                self.assertEqual(result.fallback_attempts, 1)
                self.assertEqual(result.input_tokens, 18)
                self.assertEqual(result.output_tokens, 7)

        asyncio.run(run())

    def test_cache_hit_has_no_key_id_or_fallback_attempts(self) -> None:
        async def run():
            with patch(
                "app.services.failover.litellm.acompletion",
                new_callable=AsyncMock,
                return_value=provider_response(cache_hit=True),
            ):
                result = await self.service.complete(self.messages, parse_simulation_header(None, self.pool.ids))
                self.assertEqual(result.resolution_source, LLM_CACHE)
                self.assertIsNone(result.key_id)
                self.assertEqual(result.fallback_attempts, 0)

        asyncio.run(run())

    def test_all_simulated_failures_raise_clean_unavailable_error(self) -> None:
        async def run():
            with patch(
                "app.services.failover.litellm.acompletion", new_callable=AsyncMock
            ) as completion:
                with self.assertRaises(AllProvidersUnavailable) as caught:
                    await self.service.complete(
                        self.messages,
                        parse_simulation_header("*=429", self.pool.ids),
                    )
                self.assertEqual(caught.exception.detail, "all providers unavailable")
                completion.assert_not_awaited()

        asyncio.run(run())


class SQLiteHistoryTests(unittest.TestCase):
    def test_successful_turn_is_stored_and_sessions_are_isolated(self) -> None:
        async def run():
            config = settings()
            database = Database(config.async_database_url, config.database_path)
            await database.init_schema()
            try:
                async with database.session() as db:
                    await persist_successful_turn(
                        db,
                        session_id="s-one",
                        system_prompt=None,
                        user_content="question",
                        assistant_content="answer",
                        assistant_metadata={
                            "resolution_source": LIVE_INFERENCE,
                            "model": "test/model",
                            "latency_ms": 12,
                            "input_tokens": 4,
                            "output_tokens": 2,
                            "key_id": "key_1",
                            "fallback_attempts": 0,
                        },
                    )

                sessions = SessionService(database, config)
                history = await sessions.full_history("s-one")
                self.assertEqual([message.role for message in history], ["user", "assistant"])
                self.assertEqual(history[1].metadata["resolution_source"], LIVE_INFERENCE)
                with self.assertRaises(SessionNotFound) as caught:
                    await sessions.full_history("s-two")
                self.assertEqual(caught.exception.detail, "session not found")
            finally:
                await database.dispose()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
