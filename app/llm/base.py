from abc import ABC, abstractmethod


class LLMProvider(ABC):
    @abstractmethod
    def classify_email_batch(self, email: dict, prompts: list) -> dict:
        """Classify an email against multiple rules in a single call.
        Returns a dict mapping prompt id (int) -> bool."""
        ...

    @abstractmethod
    def generate_prompt_instruction(self, description: str) -> str:
        """Generate a classifier instruction from a plain-language description.
        Raises on failure so callers can surface the error."""
        ...

    @abstractmethod
    def stream_generate_prompt_instruction(self, description: str):
        """Stream a classifier instruction as (type, text) events.
        Yields dicts with keys 'type' ('think'|'content') and 'text'."""
        ...

    def ensure_model_pulled(self) -> None:
        """Pull/warm up the model if needed. No-op by default."""
        pass
