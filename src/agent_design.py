import os
from typing import AsyncGenerator
from openai import AsyncOpenAI
from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from pathlib import Path

SYSTEM_PROMPT = (Path(__file__).parent / "prompts/system_prompt.md").read_text()

load_dotenv()

AI_SERVER_API_KEY = os.getenv("AI_SERVER_API_KEY")


def _resolve_base_url() -> str | None:
    raw = os.getenv("AI_BASE_URL") or os.getenv("AI_SERVER_URL")
    if not raw:
        return None

    raw = raw.strip().strip('"').strip("'")
    normalized = raw.rstrip("/")

    # If a completions endpoint is provided, convert to API base URL.
    for suffix in ("/v1/chat/completions", "/chat/completions"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break

    return normalized.rstrip("/")


AI_BASE_URL = _resolve_base_url()


def create_agent() -> Agent:
    if not AI_BASE_URL:
        raise RuntimeError("AI_BASE_URL (or AI_SERVER_URL) is not configured")
    if not AI_SERVER_API_KEY:
        raise RuntimeError("AI_SERVER_API_KEY is not configured")

    client = AsyncOpenAI(base_url=AI_BASE_URL, api_key=AI_SERVER_API_KEY)
    model = OpenAIChatModel(
        model_name="unsloth/gemma-4-E2B-it-GGUF",
        provider=OpenAIProvider(openai_client=client),
    )
    return Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
    )


async def ask(
    agent: Agent, prompt: str, history: list = []
) -> AsyncGenerator[str, None]:
    async with agent.run_stream(prompt, message_history=history) as response:
        async for text in response.stream_output(debounce_by=0.01):
            yield text


if __name__ == "__main__":
    import asyncio

    agent = create_agent()
    print(f"Connecting to AI server at: {AI_BASE_URL}...")

    async def main():
        test_questions = [
            "Who is Kevin?",
            "What are Kevin's main skills?",
            "Tell me about Kevin's capstone project.",
            "How can I contact Kevin?",
        ]

        for question in test_questions:
            print(f"\n{'=' * 50}")
            print(f"Q: {question}")
            print("A: ", end="", flush=True)
            async for chunk in ask(agent, question):
                print(chunk, end="", flush=True)
            print()  # newline after each answer

    asyncio.run(main())
