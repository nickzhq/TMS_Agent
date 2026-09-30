"""Application entry point (skeleton).

Loads and verifies configuration (config.yaml + env vars -> Settings).
Later stages wire in the orchestration graph, tools, and sub-agent nodes.
"""
from __future__ import annotations

from src.config.settings import load_settings


def main() -> None:
    settings = load_settings()

    print("=== Logistics Quotation System · bootstrap ===")
    print(f"LLM      : model={settings.llm.model or '<unset>'} base_url={settings.llm.base_url or '<unset>'}")
    print(f"DB       : {'configured' if settings.db.host else '<unset>'} (host / database / password redacted)")
    print(f"Amap     : api_key={'configured' if settings.amap.api_key else '<unset>'}")
    print(f"Memory   : enabled={settings.memory.enabled} provider={settings.memory.provider}")
    print(f"Knowledge: enabled={settings.knowledge_base.enabled} provider={settings.knowledge_base.provider} (wired, disabled)")
    print("=== configuration loaded ===")


if __name__ == "__main__":
    main()
