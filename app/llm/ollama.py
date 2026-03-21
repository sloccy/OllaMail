import json
import requests
from app import db

_session = requests.Session()
from app.llm.base import LLMProvider
from app.config import (OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_TIMEOUT,
                        OLLAMA_NUM_CTX, OLLAMA_NUM_PREDICT, OLLAMA_GENERATE_NUM_PREDICT)


class OllamaProvider(LLMProvider):
    def ensure_model_pulled(self) -> None:
        try:
            resp = _session.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
            models = [m["name"] for m in resp.json().get("models", [])]
            model_base = OLLAMA_MODEL.split(":")[0]
            already_present = any(m.startswith(model_base) for m in models)
            if not already_present:
                print(f"Pulling model {OLLAMA_MODEL} from Ollama... (this may take a while)")
                _session.post(
                    f"{OLLAMA_HOST}/api/pull",
                    json={"name": OLLAMA_MODEL, "stream": False},
                    timeout=OLLAMA_TIMEOUT,
                )
                print(f"Model {OLLAMA_MODEL} ready.")
        except Exception as e:
            print(f"Warning: could not check/pull Ollama model: {e}")

    def classify_email_batch(self, email: dict, prompts: list) -> dict:
        if not prompts:
            return {}

        rules_text = "\n".join(
            f"{i+1}. {p['name']}: {p['instructions']}"
            for i, p in enumerate(prompts)
        )

        example = ", ".join(f'"{i+1}": false' for i in range(min(2, len(prompts))))
        prompt = f"""You are an email classification assistant. You will be given an email and a list of labeling rules. For each rule, decide if the label should be applied to this email.

Rules:
{rules_text}

Email:
From: {email['sender']}
Subject: {email['subject']}
Body:
{email['body'] or email['snippet']}

Respond with ONLY a JSON object where each key is the rule's number (1, 2, 3...) and the value is true or false.
Example: {{{example}}}
No explanation, no markdown, just the JSON object."""

        try:
            response = _session.post(
                f"{OLLAMA_HOST}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are an email classification assistant. Respond only with a JSON object mapping rule numbers to true/false. No explanation, no markdown.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "think": False,
                    "format": "json",
                    "options": {
                        "temperature": 0,
                        "num_predict": max(50, len(prompts) * 20),
                        "num_ctx": OLLAMA_NUM_CTX,
                    },
                },
                timeout=OLLAMA_TIMEOUT,
            )
            response.raise_for_status()

            raw = response.json().get("message", {}).get("content", "").strip()

            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            try:
                result = json.loads(raw)
                parsed = {}
                for k, v in result.items():
                    idx = int(k) - 1
                    if 0 <= idx < len(prompts):
                        parsed[prompts[idx]["id"]] = bool(v)
                db.add_log("DEBUG", f"LLM raw response: {raw}")
                db.add_log("DEBUG", f"LLM parsed: { {p['name']: parsed.get(p['id'], False) for p in prompts} }")
                return parsed
            except Exception as e:
                db.add_log("ERROR", f"LLM parse error: {e!r} | raw: {raw!r}")
                print(f"Warning: could not parse LLM batch response: {e!r} | raw: {raw!r}")
                return {p["id"]: False for p in prompts}
        except requests.exceptions.RequestException as e:
            db.add_log("ERROR", f"LLM request failed: {e!r}")
            print(f"Warning: LLM request failed: {e!r}")
            return {p["id"]: False for p in prompts}
        except Exception as e:
            db.add_log("ERROR", f"LLM unexpected error: {e!r}")
            print(f"Warning: LLM unexpected error: {e!r}")
            return {p["id"]: False for p in prompts}

    def _build_generate_request(self, description: str) -> dict:
        system_prompt = (
            "You write email filter rules for an AI classifier. "
            "The classifier reads email content and infers meaning, intent, and context — "
            "it is NOT limited to keywords or sender addresses. "
            "Rules should describe what an email is about, its purpose, and tone. "
            "Be specific about what should match and what should not.\n"
            "Output only the rule text. No preamble, no quotes, no explanation."
        )
        user_prompt = (
            f'A user wants to automatically label certain emails. They described:\n\n"{description}"\n\n'
            "Write a precise classifier instruction (2-5 sentences). "
            "Focus on the meaning and context of the email — what it is about, why it was sent, "
            "and who it is intended for. Describe what distinguishes matching emails from "
            "similar-but-different ones based on content and intent, not just surface signals "
            "like sender address or keywords.\n\n"
            "Respond with ONLY the instruction text."
        )
        return {
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {
                "temperature": 0.7,
                "num_predict": OLLAMA_GENERATE_NUM_PREDICT,
                "num_ctx": OLLAMA_NUM_CTX,
            },
        }

    def _filter_think_chunks(self, buffer: str, in_think: bool, chunk: str):
        """Append chunk to buffer, flush safe content, return (events, new_buffer, in_think).
        Events are (type, text) tuples where type is 'think' or 'content'."""
        buffer += chunk
        events = []
        while True:
            tag = "</think>" if in_think else "<think>"
            idx = buffer.find(tag)
            if idx == -1:
                # No complete tag — keep up to len(tag)-1 chars buffered
                safe = max(0, len(buffer) - (len(tag) - 1))
                if safe > 0:
                    events.append(("think" if in_think else "content", buffer[:safe]))
                    buffer = buffer[safe:]
                break
            else:
                if idx > 0:
                    events.append(("think" if in_think else "content", buffer[:idx]))
                buffer = buffer[idx + len(tag):]
                in_think = not in_think
        return events, buffer, in_think

    def stream_generate_prompt_instruction(self, description: str):
        """Generator that yields {"type": "think"|"content", "text": str} dicts."""
        payload = self._build_generate_request(description)
        payload["stream"] = True
        response = _session.post(
            f"{OLLAMA_HOST}/api/chat",
            json=payload,
            stream=True,
            timeout=OLLAMA_TIMEOUT,
        )
        response.raise_for_status()
        in_think = False
        buffer = ""
        for line in response.iter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            token = data.get("message", {}).get("content", "")
            if not token:
                continue
            events, buffer, in_think = self._filter_think_chunks(buffer, in_think, token)
            for evt_type, evt_text in events:
                if evt_text:
                    yield {"type": evt_type, "text": evt_text}
        # Flush remaining buffer
        if buffer:
            yield {"type": "think" if in_think else "content", "text": buffer}

    def generate_prompt_instruction(self, description: str) -> str:
        payload = self._build_generate_request(description)
        payload["stream"] = False
        response = _session.post(
            f"{OLLAMA_HOST}/api/chat",
            json=payload,
            timeout=OLLAMA_TIMEOUT,
        )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "").strip()
        # Strip think blocks in case model includes them despite stream=False
        import re
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        return content
