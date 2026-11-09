"""Filesystem-backed LLM provider settings (data/llm.json).

Which LLM the app calls is the one piece of server configuration meant
to change at runtime — env vars can't (they are read once at startup),
so the choice lives in a JSON file instead, the same file-is-truth
style as users.json: atomic temp+rename writes (a half-written settings
file would silently turn the LLM off), reload on mtime change so a
hand-edit takes effect without a restart.

Shape:

    {
      "provider": "ollama",         # the active one ("none" = off)
      "providers": {                 # remembered per provider, so
        "openai": {...},            # switching never loses a key
        "anthropic": {...},
        "ollama": {...},
        "openai-compatible": {...}
      }
    }

Per-provider fields (plain strings, all optional): api_key, model,
base_url. The store persists; services/llm.py interprets (resolves
base_url against the registry default, builds the wire request).

An absent file means {"provider": "none"} — no LLM, no outbound calls,
the deployment unchanged until an admin turns a provider on in the
Settings UI (the opt-in stance the terminal takes).

SCRIPTENGINE_LLM_* env vars seed the file on first boot only
(seed_from_env, called from the app lifespan): after that the UI owns
it, exactly like the admin bootstrap.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ..config import DATA_DIR

# What can be configured, and enough metadata to render it. Two wire
# formats cover the whole market: everything OpenAI-compatible (one
# adapter: OpenAI itself, Ollama, vLLM, LM Studio, OpenRouter, DeepSeek,
# Groq, …) plus Anthropic's native one. "none" (the default, not listed
# here) means the feature is off.
PROVIDERS: dict[str, dict] = {
    "openai": {
        "label": "OpenAI",
        "wire": "openai",
        "default_base_url": "https://api.openai.com/v1",
        "hint": "The OpenAI API — needs an api key and a model name",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "wire": "anthropic",
        "default_base_url": "https://api.anthropic.com",
        "hint": "Claude models via the native Anthropic API",
    },
    "ollama": {
        "label": "Ollama (local)",
        "wire": "openai",
        "default_base_url": "http://localhost:11434/v1",
        "hint": "Local models through Ollama's OpenAI-compatible endpoint — "
                "no key, nothing leaves the machine",
    },
    "openai-compatible": {
        "label": "OpenAI-compatible (custom URL)",
        "wire": "openai",
        "default_base_url": "",
        "hint": "Any endpoint speaking the OpenAI chat API: vLLM, LM Studio, "
                "OpenRouter, DeepSeek, Groq, … — set its base URL",
    },
}

# Fields each provider carries. api_key may stay empty (Ollama and most
# self-hosted endpoints want none).
FIELDS = ("api_key", "model", "base_url")


def _blank() -> dict:
    return {f: "" for f in FIELDS}


class LlmStore:
    def __init__(self, path: Path = DATA_DIR / "llm.json") -> None:
        self._path = path
        self._data: dict = {"provider": "none", "providers": {}}
        self._stamp: tuple[int, int] | None = None  # (mtime_ns, size)
        self._load()

    # -- persistence -----------------------------------------------------
    def _load(self) -> None:
        try:
            stat = self._path.stat()
        except OSError:
            self._data = {"provider": "none", "providers": self._defaults()}
            self._stamp = None
            return
        if self._stamp == (stat.st_mtime_ns, stat.st_size):
            return  # unchanged since the last look
        try:
            self._data = self._normalize(json.loads(
                self._path.read_text(encoding="utf-8")))
            self._stamp = (stat.st_mtime_ns, stat.st_size)
        except (OSError, ValueError):
            # unreadable/corrupt settings: treat as "off" rather than crash
            # the server; the admin can fix or delete the file by hand.
            self._data = {"provider": "none", "providers": self._defaults()}
            self._stamp = None

    @staticmethod
    def _defaults() -> dict:
        return {key: _blank() for key in PROVIDERS}

    @staticmethod
    def _normalize(raw: dict) -> dict:
        """Whatever the file says, the in-memory shape is complete and
        typed — unknown keys and wrong types drop out, not in."""
        provider = raw.get("provider", "none")
        if provider not in ("none", *PROVIDERS):
            provider = "none"
        providers = {}
        stored = raw.get("providers") or {}
        for key in PROVIDERS:
            entry = stored.get(key) or {}
            providers[key] = {f: str(entry.get(f) or "").strip()
                              for f in FIELDS}
        return {"provider": provider, "providers": providers}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, indent=2) + "\n"
        # atomic: a partial llm.json would silently flip the LLM off
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".llm-")
        try:
            with open(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            Path(tmp).replace(self._path)
        finally:
            Path(tmp).unlink(missing_ok=True)  # no-op after a successful rename
        stat = self._path.stat()
        self._stamp = (stat.st_mtime_ns, stat.st_size)

    # -- queries ---------------------------------------------------------
    def settings(self) -> dict:
        """A deep copy — callers may mutate the result freely without
        touching what the store holds."""
        self._load()
        return json.loads(json.dumps(self._data))

    # -- mutations -------------------------------------------------------
    def update(self, provider: str, values: dict | None = None) -> None:
        """Set the active provider and merge per-provider fields.

        An empty api_key in `values` keeps the stored one — the password-
        change pattern, so a masked key never needs to round-trip through
        a browser. model/base_url overwrite as given.
        """
        self._load()
        if provider not in ("none", *PROVIDERS):
            raise ValueError(f"Unknown LLM provider: {provider}")
        for key, fields in (values or {}).items():
            if key not in PROVIDERS:
                raise ValueError(f"Unknown LLM provider: {key}")
            for name in FIELDS:
                value = str((fields or {}).get(name) or "").strip()
                if name == "api_key" and not value:
                    continue  # blank key = keep the stored one
                self._data["providers"][key][name] = value
        self._data["provider"] = provider
        self._save()


store = LlmStore()


def seed_from_env() -> bool:
    """First-boot seeding from SCRIPTENGINE_LLM_* (config.py): write
    data/llm.json when it doesn't exist yet and a provider is named.
    Returns True when a file was written. Afterwards the UI owns the
    file — the env vars are ignored (the ADMIN_BOOTSTRAP pattern).
    """
    from .. import config  # at call time: monkeypatch-friendly in tests

    provider = (config.LLM_PROVIDER or "").strip()
    if not provider or store._path.exists():
        return False
    if provider not in PROVIDERS:
        print(f"llm: SCRIPTENGINE_LLM_PROVIDER={provider!r} is unknown "
              f"(one of {', '.join(PROVIDERS)}) — ignored")
        return False
    fields = {
        "api_key": (config.LLM_API_KEY or "").strip(),
        "model": (config.LLM_MODEL or "").strip(),
        "base_url": (config.LLM_BASE_URL or "").strip(),
    }
    store.update(provider, {provider: {k: v for k, v in fields.items() if v}})
    print(f"llm: seeded data/llm.json from SCRIPTENGINE_LLM_* "
          f"(first boot only — manage providers in the Settings UI)")
    return True
