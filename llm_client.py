from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

import requests

from config import Settings


logger = logging.getLogger("golden_opening.llm")


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()

    def chat(
        self,
        provider_name: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.85,
        max_tokens: int = 2400,
        purpose: str = "模型调用",
    ) -> str:
        provider = self.settings.get_provider(provider_name)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        started_at = perf_counter()
        logger.info(
            "开始%s provider=%s model=%s host=%s prompt_chars=%s",
            purpose,
            provider.name,
            provider.model,
            provider.host,
            len(system_prompt) + len(user_prompt),
        )
        try:
            if provider.name == "ollama":
                content = self._chat_ollama(provider.host, provider.model, messages, temperature)
            else:
                content = self._chat_openai_compatible(
                    provider.host,
                    provider.model,
                    provider.api_key,
                    messages,
                    temperature,
                    max_tokens,
                )
        except Exception:
            logger.exception("失败%s provider=%s model=%s", purpose, provider.name, provider.model)
            raise

        logger.info(
            "完成%s provider=%s model=%s elapsed_ms=%s output_chars=%s",
            purpose,
            provider.name,
            provider.model,
            int((perf_counter() - started_at) * 1000),
            len(content),
        )
        return content

    def _chat_ollama(
        self,
        host: str,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
    ) -> str:
        response = self.session.post(
            host,
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": temperature},
            },
            timeout=300,
        )
        self._raise_for_status(response)
        data = response.json()
        content = data.get("message", {}).get("content", "")
        if not content:
            raise RuntimeError("Ollama 没有返回可用内容。")
        return content

    def _chat_openai_compatible(
        self,
        host: str,
        model: str,
        api_key: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> str:
        response = self.session.post(
            host,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=300,
        )
        self._raise_for_status(response)
        data = response.json()
        content = self._extract_openai_content(data)
        if not content:
            raise RuntimeError("模型接口返回成功，但没有拿到正文内容。")
        return content

    @staticmethod
    def _extract_openai_content(data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return "\n".join(part for part in parts if part)
        return content if isinstance(content, str) else ""

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        if response.ok:
            return
        message = response.text
        try:
            data = response.json()
            if isinstance(data, dict):
                error_data = data.get("error")
                if isinstance(error_data, dict):
                    message = error_data.get("message") or message
                else:
                    message = data.get("message") or message
        except ValueError:
            pass
        raise RuntimeError(f"模型调用失败（HTTP {response.status_code}）：{message}")
