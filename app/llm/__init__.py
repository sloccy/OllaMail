from app.llm.base import LLMProvider
from app.llm.ollama import OllamaProvider


def get_provider() -> LLMProvider:
    return OllamaProvider()
