"""Every secret a builder asks `providers.secrets` for must be on the allowlist that loads it."""

from ai_agent_adapters.stream import obs, twitch, youtube
from ai_agent_adapters.telegram.bot import PROXY_SETTING, TOKEN_SETTING
from ai_agent_adapters.voice.soniox import KEY_SETTING

from ai_agent_core.local_host.provider import SERVICE_SECRETS


def test_the_allowlist_covers_every_secret_the_builders_read():
    wanted = {
        KEY_SETTING, TOKEN_SETTING, PROXY_SETTING, obs.PASSWORD_SETTING,
        twitch.TOKEN_SETTING, twitch.NICK_SETTING, youtube.CLIENT_ID_SETTING,
        youtube.CLIENT_SECRET_SETTING, youtube.REFRESH_TOKEN_SETTING,
    }  # fmt: skip
    assert wanted <= set(SERVICE_SECRETS)
    assert len(SERVICE_SECRETS) == len(set(SERVICE_SECRETS))
