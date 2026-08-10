import os
import shutil

import asyncclick as click
import msgspec
import requests
from platformdirs import user_config_dir

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


def _parse_config(config_path):
    with open(config_path, "rb") as f:
        cfg_string = f.read()
        return msgspec.toml.decode(cfg_string, type=Cfg)


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
    try:
        path = find_config_path()
    except Exception:
        cfg_path = get_user_cfg_path()
        attempted_default_cfg = os.path.join(os.path.dirname(cfg_path), "config.default.toml")

        click.secho(f"Could not find configuration path at {cfg_path}.", fg="red")
        if os.path.exists(attempted_default_cfg):
            click.secho(
                "Hint: Create a config by copying config.default.toml to config.toml.",
                fg="yellow",
            )
        else:
            user_choice = click.confirm(
                f"Do you want lox to create a default config file at {attempted_default_cfg}?"
            )
            if user_choice:
                default_cfg = get_default_config_path()
                _try_creating_config(default_cfg, attempted_default_cfg)
        exit(-1)

    cfg = _parse_config(path)
    return cfg
