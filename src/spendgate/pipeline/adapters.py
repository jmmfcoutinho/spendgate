"""The one place that knows every provider adapter and builds them from `Settings`.

Gateway and library mode both call this, so the two front ends cannot disagree about which
adapters exist or how they are configured. It lives in `pipeline` rather than `providers`
because it reads `Settings` and the layer contracts keep `providers` and `config` apart.
"""

from spendgate.config import Settings
from spendgate.providers.anthropic import AnthropicConfig, AnthropicProvider
from spendgate.providers.azure_openai import AzureOpenAIProvider
from spendgate.providers.azure_speech import AzureSpeech
from spendgate.providers.bedrock import Bedrock
from spendgate.providers.elevenlabs import ElevenLabs
from spendgate.providers.google import GoogleProvider
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.port import Provider


def build_providers(settings: Settings) -> dict[str, Provider]:
    """One adapter per provider slug, configured from `settings`.

    `Pipeline` refuses to start while a configured route has no adapter.
    """
    cfg = settings.config.anthropic
    remote_ok = settings.config.fail_open.allow_remote_media
    anthropic = AnthropicConfig(
        allowed_versions=tuple(cfg.allowed_versions), allowed_betas=frozenset(cfg.allowed_betas)
    )
    return {
        "openai": OpenAIProvider(remote_ok=remote_ok),
        "anthropic": AnthropicProvider(anthropic),
        "bedrock": Bedrock(),
        "google": GoogleProvider(),
        "azure_openai": AzureOpenAIProvider(remote_ok=remote_ok),
        "azure_speech": AzureSpeech(),
        "elevenlabs": ElevenLabs(),
    }
