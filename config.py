from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    host: str
    model: str
    api_key: str = ""


class Settings:
    def __init__(self) -> None:
        self.default_provider = os.getenv("API", "deepseek").strip().lower()
        self.providers = {
            "ollama": ProviderConfig(
                name="ollama",
                host=os.getenv("OLLAMA_HOST", "http://localhost:11434/api/chat").strip(),
                model=os.getenv("OLLAMA_MODEL", "deepseek-r1:32b").strip(),
            ),
            "deepseek": ProviderConfig(
                name="deepseek",
                host=os.getenv(
                    "DEEPSEEK_HOST", "https://api.deepseek.com/v1/chat/completions"
                ).strip(),
                model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip(),
                api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            ),
            "gemini": ProviderConfig(
                name="gemini",
                host=os.getenv(
                    "GEMINI_HOST", "https://zenmux.ai/api/v1/chat/completions"
                ).strip(),
                model=os.getenv("GEMINI_MODEL", "google/gemini-3-pro-preview").strip(),
                api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            ),
            "claude": ProviderConfig(
                name="claude",
                host=os.getenv(
                    "CLAUDE_HOST", "https://api.weiji.ai/v1/chat/completions"
                ).strip(),
                model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4.6").strip(),
                api_key=os.getenv("CLAUDE_API_KEY", "").strip(),
            ),
        }

    def get_provider(self, provider_name: str | None = None) -> ProviderConfig:
        selected = (provider_name or self.default_provider).strip().lower()
        if selected not in self.providers:
            supported = ", ".join(sorted(self.providers))
            raise ValueError(f"不支持的模型渠道：{selected}。可选值：{supported}")
        provider = self.providers[selected]
        if not provider.host or not provider.model:
            raise ValueError(f"{selected} 的 host 或 model 未配置完整。")
        if selected != "ollama" and not provider.api_key:
            raise ValueError(f"{selected} 的 API Key 未配置。")
        return provider

    def provider_options(self) -> list[dict[str, str]]:
        options = []
        for name in ("ollama", "deepseek", "gemini", "claude"):
            provider = self.providers[name]
            options.append(
                {
                    "value": provider.name,
                    "label": f"{provider.name} / {provider.model}",
                }
            )
        return options


settings = Settings()
