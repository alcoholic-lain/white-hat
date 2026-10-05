from bot.config import Settings
from bot.limits import Cooldown


def settings(**over) -> Settings:
    env = {"DISCORD_TOKEN": "x", "ALLOWED_USER_IDS": "1"}
    env.update(over)
    return Settings.from_env(env)


def test_default_is_allowlist_only():
    s = settings()
    assert s.is_permitted(1, 10)
    assert not s.is_permitted(2, 10)
    assert not s.is_permitted(2, None)


def test_allow_all_opens_everything():
    s = settings(ALLOW_ALL_USERS="true")
    assert s.is_permitted(2, 10) and s.is_permitted(2, None)


def test_allow_all_restricted_to_guilds_also_blocks_stranger_dms():
    s = settings(ALLOW_ALL_USERS="true", ALLOWED_GUILD_IDS="10, 11")
    assert s.is_permitted(2, 10)
    assert not s.is_permitted(2, 99)
    assert not s.is_permitted(2, None)
    assert s.is_permitted(1, None)  # trusted users always pass


def test_cooldown():
    now = [100.0]
    c = Cooldown(5, clock=lambda: now[0])
    assert c.check(7) == 0
    now[0] = 102
    assert round(c.check(7)) == 3
    assert c.check(8) == 0  # other users unaffected
    now[0] = 106
    assert c.check(7) == 0


def test_admins_default_to_allowlist_and_can_be_split():
    s = settings(ALLOWED_USER_IDS="1, 2")
    assert s.admin_user_ids == {1, 2} and s.is_admin(2)
    s = settings(ALLOWED_USER_IDS="1, 2", ADMIN_USER_IDS="1")
    assert s.is_admin(1) and not s.is_admin(2)
    assert s.is_trusted(2) and s.is_permitted(2, None)  # allowlisted, just not admin


def test_admin_only_config_is_enough_and_always_permitted():
    s = settings(ALLOWED_USER_IDS="", ADMIN_USER_IDS="9")
    assert s.is_admin(9) and s.is_permitted(9, None) and not s.is_permitted(3, None)


def test_both_empty_is_an_error():
    import pytest

    from bot.config import ConfigError

    with pytest.raises(ConfigError):
        settings(ALLOWED_USER_IDS="", ADMIN_USER_IDS="")


def test_allow_mentions_flag():
    assert settings().allow_mentions is False
    assert settings(ALLOW_MENTIONS="true").allow_mentions is True
