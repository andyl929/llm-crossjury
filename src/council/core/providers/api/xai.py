"""xAI (Grok) via the vendor's official SDK or documented API."""

from council.core.providers.api._common import ApiProviderBase


class XaiApiProvider(ApiProviderBase):
    """Grok models through the xAI API."""

    name = "xai"
    api_key_env = "XAI_API_KEY"
