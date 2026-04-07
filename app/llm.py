import json
import logging
import re

from openai import OpenAI

from app import db
from app.config import (
    DEBUG_LOGGING,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_NUM_PREDICT,
    LLM_TIMEOUT,
)

_logger = logging.getLogger("ollamail.llm")
_client = OpenAI(base_url=LLM_BASE_URL, api_key="not-needed", timeout=LLM_TIMEOUT)

# Safety net: strip <think> tags in case llama.cpp embeds them in content
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(Exception):
    """Raised when the LLM fails to produce a usable classification."""


def ensure_model_pulled() -> None:
    try:
        _client.models.list()
        db.add_log("INFO", f"Model runner is reachable, model: {LLM_MODEL}")
    except Exception as e:
        db.add_log("WARNING", f"Could not reach model runner: {e}")


def classify_email_batch(email: dict, prompts: list) -> tuple:
    """Returns (parsed_results: dict[int, bool], raw_response: str)."""
    if not prompts:
        return {}, ""

    rules_text = "\n".join(f"{i + 1}. {p['name']}: {p['instructions']}" for i, p in enumerate(prompts))
    example = ", ".join(f'"{i + 1}": false' for i in range(min(2, len(prompts))))
    prompt = f"""You are an email classification assistant. You will be given an email and a list of labeling rules. For each rule, decide if the label should be applied to this email.

Rules:
{rules_text}

Email:
From: {email["sender"]}
Subject: {email["subject"]}
Body:
{email["body"] or email["snippet"]}

Respond with ONLY a JSON object where each key is the rule's number (1, 2, 3...) and the value is true or false.
Example: {{{example}}}
No explanation, no markdown, just the JSON object."""

    db.add_log("INFO", f"LLM classifying '{email.get('subject', '?')[:60]}' against {len(prompts)} rule(s)")
    try:
        response = _client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are an email classification assistant. Respond only with a JSON object mapping rule numbers to true/false. No explanation, no markdown.",
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=max(50, len(prompts) * 20),
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        raw = response.choices[0].message.content or ""
        db.add_log("INFO", f"LLM classify response: content={len(raw)} chars")
        if raw:
            db.add_log("INFO", f"LLM raw content: {raw[:500]}")
        raw_response = raw  # save original for history before stripping
        raw = _THINK_RE.sub("", raw).strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        result = json.loads(raw)
        parsed = {}
        for k, v in result.items():
            idx = int(k) - 1
            if 0 <= idx < len(prompts):
                parsed[prompts[idx]["id"]] = bool(v)
        if DEBUG_LOGGING:
            db.add_log("DEBUG", f"LLM raw response: {raw}")
            db.add_log("DEBUG", f"LLM parsed: { {p['name']: parsed.get(p['id'], False) for p in prompts} }")
        return parsed, raw_response
    except json.JSONDecodeError as e:
        db.add_log("ERROR", f"LLM parse error: {e!r} | raw: {raw!r}")
        raise LLMError(f"LLM parse error: {e!r}") from e
    except Exception as e:
        db.add_log("ERROR", f"LLM request failed: {e!r}")
        raise LLMError(f"LLM request failed: {e!r}") from e


def stream_generate_prompt_instruction(description: str):
    """Generator that yields {"type": "think"|"content", "text": str} dicts."""
    stream = _client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You write email filter rules for an AI classifier. "
                    "Output only the rule text. No preamble, no drafts, no self-critique, no quotes, no explanation."
                ),
            },
            {
                "role": "user",
                "content": (
                    f'Write a 2-4 sentence classifier instruction for emails matching: "{description}"\n\n'
                    "The instruction must describe: what the email is about, its purpose/intent, "
                    "and what distinguishes it from similar-but-non-matching emails. "
                    "Do not use keywords or sender addresses as criteria — focus on meaning and context.\n\n"
                    "Output ONLY the instruction text."
                ),
            },
        ],
        stream=True,
        temperature=0.7,
        max_tokens=LLM_NUM_PREDICT,
        extra_body={"chat_template_kwargs": {"enable_thinking": True}},
    )
    for chunk in stream:
        delta = chunk.choices[0].delta
        reasoning = getattr(delta, "reasoning_content", None)
        content = delta.content or ""
        if reasoning:
            yield {"type": "think", "text": reasoning}
        if content:
            yield {"type": "content", "text": content}
    _logger.info("stream_generate_prompt_instruction finished")
