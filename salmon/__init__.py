import os

import asyncclick as click

from salmon.config import Cfg, find_config_path, setup_config
from salmon.config.store import SettingsStore

try:
    cfg: Cfg = setup_config()
except Exception as e:
    click.secho(f"Configuration error: {find_config_path()}", fg="yellow")
    click.secho(e, fg="red")
    exit(-1)

# Everything the UI can edit lives in settings.toml beside config.toml, layered
# on top of it here. config.toml stays the bootstrap; the UI never rewrites it.
settings = SettingsStore(os.path.dirname(find_config_path()))
_unapplied = settings.apply_to(cfg)
if _unapplied:
    click.secho(
        f"Ignored {len(_unapplied)} saved setting(s) with no matching config section: {', '.join(_unapplied)}",
        fg="yellow",
    )
