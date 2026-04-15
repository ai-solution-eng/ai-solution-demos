from openai import AsyncOpenAI


def make_client(base_url: str, api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=base_url or "", api_key=api_key or "")
