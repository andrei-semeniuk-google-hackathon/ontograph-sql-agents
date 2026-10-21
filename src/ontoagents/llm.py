"""The one seam between the agents and a model provider.

Agents ask for a typed object and get a validated pydantic instance back. Tests swap
in ScriptedLLM, so the orchestration logic is tested without network or flakiness.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, cast

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, SecretStr


class StructuredLLM(Protocol):
    async def generate[T: BaseModel](self, schema: type[T], *, system: str, user: str) -> T: ...


class AnthropicLLM:
    def __init__(self, model: str, api_key: SecretStr | None = None, max_tokens: int = 2048):
        kwargs: dict[str, Any] = {"model": model, "temperature": 0, "max_tokens": max_tokens}
        if api_key is not None:
            kwargs["api_key"] = api_key
        self._chat = ChatAnthropic(**kwargs)

    async def generate[T: BaseModel](self, schema: type[T], *, system: str, user: str) -> T:
        runnable = self._chat.with_structured_output(schema)
        result = await runnable.ainvoke([SystemMessage(system), HumanMessage(user)])
        return cast(T, result)


Responder = BaseModel | Callable[[str], BaseModel]


class ScriptedLLM:
    """Deterministic stand-in: a queue of responses per output schema.

    A response can be a model instance or a function of the user prompt, which lets a
    test react to feedback the validator sent back on a previous attempt.
    """

    def __init__(self, script: dict[type[BaseModel], list[Responder]]):
        self._script = {schema: list(responses) for schema, responses in script.items()}
        self.calls: list[tuple[str, str]] = []

    async def generate[T: BaseModel](self, schema: type[T], *, system: str, user: str) -> T:
        self.calls.append((schema.__name__, user))
        queue = self._script.get(schema)
        if not queue:
            raise AssertionError(f"no scripted response left for {schema.__name__}")
        responder = queue.pop(0)
        result = responder(user) if callable(responder) else responder
        if not isinstance(result, schema):
            raise AssertionError(f"scripted {type(result).__name__}, expected {schema.__name__}")
        return result
