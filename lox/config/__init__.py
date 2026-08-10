import os
import shutil
import sys

import asyncclick as click
import msgspec
import requests
from platformdirs import user_config_dir

from .schema import BOOTSTRAP_ENV
from .validations import Cfg

APPNAME = "lox"

LEGACY_APPNAME = "smoked-salmon"
"""Upstream's directory name. Still read so an existing smoked-salmon install
keeps working after switching to this fork, but nothing new is written there."""

root_path = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def get_user_cfg_path():
    return os.path.join(user_config_dir(APPNAME), "config.toml")


def get_legacy_user_cfg_path():
    """Where upstream smoked-salmon keeps its config."""
    return os.path.join(user_config_dir(LEGACY_APPNAME), "config.toml")


def get_default_config_path():
    default_config_path = os.path.join(root_path, "data", "config.default.toml")

    if not os.path.exists(default_config_path):
        click.secho(f"Default config file not found at {default_config_path}", fg="yellow")
        click.secho("Downloading from GitHub...", fg="blue")

        os.makedirs(os.path.dirname(default_config_path), exist_ok=True)

        try:
            github_url = "https://raw.githubusercontent.com/MonkeyGoneWIld/lox/main/data/config.default.toml"
            response = requests.get(github_url, timeout=30)
            response.raise_for_status()

            with open(default_config_path, "w", encoding="utf-8") as f:
                f.write(response.text)

            click.secho(f"Successfully downloaded default config to {default_config_path}", fg="green")
        except requests.exceptions.RequestException as e:
            click.secho(f"Failed to download default config: {e}", fg="red")
            raise FileNotFoundError(f"Could not find or download default config file: {e}") from e
        except Exception as e:
            click.secho(f"Failed to save default config: {e}", fg="red")
            raise FileNotFoundError(f"Could not save default config file: {e}") from e

    return default_config_path


def env_overrides() -> dict:
    """Collect bootstrap settings supplied through the environment.

    Lets a container be configured entirely from compose. The environment wins
    over config.toml, so a stale mounted file cannot override the deployment.

    Returns:
        A nested dict shaped like the config, holding only what was set.
    """
    nested: dict = {}
    for name, key in BOOTSTRAP_ENV.items():
        value = os.environ.get(name)
        if value is None or not value.strip():
            continue
        node = nested
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if parts[-1] == "port":
            node[parts[-1]] = int(value)
        elif parts[-1] == "debug":
            node[parts[-1]] = value.strip().lower() not in ("0", "false", "no", "off")
        else:
            node[parts[-1]] = value
    return nested


def _merge(base: dict, overlay: dict) -> dict:
    """Deep-merge overlay into base, returning a new dict."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _parse_config(config_path):
    with open(config_path, "rb") as f:
        raw = msgspec.toml.decode(f.read())
    return msgspec.convert(_merge(raw, env_overrides()), type=Cfg, strict=False)


def _try_creating_config(src, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy(src, dest)


def find_config_path():
    """Locate config.toml.

    Checked in order: the repository root (handy for development and for
    bind-mounting a single file into a container), this fork's config directory,
    then upstream's, so an existing smoked-salmon install keeps working.

    Returns:
        Path to the config file.

    Raises:
        FileNotFoundError: If no config exists in any of those places.
    """
    candidates = [
        os.path.join(root_path, "config.toml"),
        get_user_cfg_path(),
        get_legacy_user_cfg_path(),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError("Could not find config path")


def setup_config():
    env = env_overrides()
    try:
        path = find_config_path()
    except Exception:
        # A container can be configured purely from compose. If the environment
        # supplies the bootstrap settings, run without a config file at all -
        # everything else is set in the UI and stored in settings.toml.
        missing = [
            name
            for name, key in (
                ("LOX_DOWNLOAD_DIR", ("directory", "download_directory")),
                ("LOX_TORRENTS_DIR", ("directory", "dottorrents_dir")),
            )
            if key[1] not in env.get(key[0], {})
        ]
        if not missing:
            click.secho("No config.toml; using bootstrap settings from the environment.", fg="cyan")
            return msgspec.convert(env, type=Cfg, strict=False)
        if env:
            click.secho(
                f"Partial environment bootstrap: still need {', '.join(missing)}.",
                fg="red",
            )
        cfg_path = get_user_cfg_path()
        attempted_default_cfg = os.path.join(os.path.dirname(cfg_path), "config.default.toml")

        click.secho(f"Could not find configuration path at {cfg_path}.", fg="red")
        click.secho(
            "Set the LOX_* environment variables (LOX_HOST, LOX_PORT, LOX_AUTH_TOKEN, "
            "LOX_DOWNLOAD_DIR, LOX_TORRENTS_DIR) or provide a config.toml.",
            fg="yellow",
        )
        if os.path.exists(attempted_default_cfg):
            click.secho(
                "Hint: Create a config by copying config.default.toml to config.toml.",
                fg="yellow",
            )
        elif sys.stdin.isatty():
            # Only ask when someone is there to answer. In a container this
            # prompt aborts instantly, and the abort used to mask the real
            # error behind a confusing traceback and a restart loop.
            if click.confirm(f"Do you want lox to create a default config file at {attempted_default_cfg}?"):
                _try_creating_config(get_default_config_path(), attempted_default_cfg)
        else:
            click.secho(
                "Running without a terminal, so not prompting to create one.",
                fg="yellow",
            )
        exit(-1)

    cfg = _parse_config(path)
    return cfg
