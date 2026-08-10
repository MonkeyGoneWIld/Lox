import os

import asyncclick as click

from salmon.config import Cfg, find_config_path, get_user_cfg_path, setup_config
from salmon.config.store import SettingsStore


def _config_location() -> str:
    """Describe where the config came from, for error messages."""
    try:
        return find_config_path()
    except Exception:
        return "the environment (no config.toml found)"


try:
    cfg: Cfg = setup_config()
except SystemExit:
    raise
except Exception as e:
    click.secho(f"Configuration error: {_config_location()}", fg="yellow")
    click.secho(str(e), fg="red")
    exit(-1)


def _settings_dir() -> str:
    """Where settings.toml lives.

    Beside config.toml when there is one. With an environment-only bootstrap
    there is no config file, so fall back to LOX_SETTINGS_DIR and then the user
    config directory, which in a container is a mounted volume.
    """
    override = os.environ.get("LOX_SETTINGS_DIR")
    if override:
        return override
    try:
        return os.path.dirname(find_config_path())
    except Exception:
        return os.path.dirname(get_user_cfg_path())


# Everything the UI can edit lives in settings.toml, layered on top of the
# bootstrap here. The UI never rewrites config.toml.
settings = SettingsStore(_settings_dir())
_unapplied = settings.apply_to(cfg)
if _unapplied:
    click.secho(
        f"Ignored {len(_unapplied)} saved setting(s) with no matching config section: {', '.join(_unapplied)}",
        fg="yellow",
    )
