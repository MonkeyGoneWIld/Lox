import shutil
import sqlite3
from os import listdir, makedirs, path

import asyncclick as click
from platformdirs import user_data_dir

from lox.common import commandgroup
from lox.config import APPNAME, LEGACY_APPNAME


DB_FILENAME = "lox.db"
LEGACY_DB_FILENAME = "smoked.db"


def _db_dir() -> str:
    """Return the data directory, preferring one that already holds a database.

    Renaming the app would otherwise orphan an existing install, so an upstream
    directory with a database in it wins over a fresh lox one.
    """
    legacy = user_data_dir(appname=LEGACY_APPNAME)
    if any(path.exists(path.join(legacy, name)) for name in (DB_FILENAME, LEGACY_DB_FILENAME)):
        return legacy
    return user_data_dir(appname=APPNAME)


DB_DIR = _db_dir()
DB_PATH = path.join(DB_DIR, DB_FILENAME)
# Upstream kept the database beside the package before it moved to a data dir.
OLD_DB_PATH = path.abspath(path.join(path.dirname(path.dirname(__file__)), LEGACY_DB_FILENAME))
MIG_DIR = path.abspath(path.join(path.dirname(path.dirname(__file__)), "data", "migrations"))


def adopt_legacy_db() -> str | None:
    """Move a pre-rename smoked.db onto the current path, if one exists.

    Renaming the file would otherwise silently start a fresh database and lose
    the upload history. Checked in both the data directory and the old
    beside-the-package location.

    Returns:
        The path the database was moved from, or None if nothing was adopted.
    """
    if path.exists(DB_PATH):
        return None
    for candidate in (path.join(DB_DIR, LEGACY_DB_FILENAME), OLD_DB_PATH):
        if path.exists(candidate):
            makedirs(DB_DIR, exist_ok=True)
            shutil.move(candidate, DB_PATH)
            return candidate
    return None


@commandgroup.command()
@click.option("--list", "-l", is_flag=True, help="List migrations instead of migrating.")
def migrate(list):
    """Migrate database to newest version"""
    if list:
        list_migrations()
        return

    current_version = get_current_version()
    ran_once = False
    makedirs(DB_DIR, exist_ok=True)
    adopted = adopt_legacy_db()
    if adopted:
        click.secho(f"Moved existing database from {adopted} to {DB_PATH}...", fg="yellow")
    else:
        click.secho(f"Connecting to database at {DB_PATH}...", fg="yellow")
    with sqlite3.connect(DB_PATH) as conn:
        for migration in sorted(f for f in listdir(MIG_DIR) if f.endswith(".sql")):
            try:
                mig_version = int(migration[:4])
            except TypeError:
                click.secho(
                    f"\n{migration} is improperly named. It must start with a four digit integer.",
                    fg="red",
                )
                raise click.Abort from None

            if mig_version > current_version:
                ran_once = True
                click.secho(f"Running {migration}...")
                cursor = conn.cursor()
                with open(path.join(MIG_DIR, migration)) as mig_file:
                    cursor.executescript(mig_file.read())
                    cursor.execute("INSERT INTO version (id) VALUES (?)", (mig_version,))
                conn.commit()
                cursor.close()

    if not ran_once:
        click.secho("You are already caught up with all migrations.", fg="green")


def list_migrations():
    """List migration history and current status"""
    current_version = get_current_version()
    for migration in sorted(f for f in listdir(MIG_DIR) if f.endswith(".sql")):
        try:
            mig_version = int(migration[:4])
        except TypeError:
            click.secho(
                f"\n{migration} is improperly named. It must start with a four digit integer.",
                fg="red",
            )
            raise click.Abort from None

        if mig_version == current_version:
            click.secho(f"{migration} (CURRENT)", fg="cyan", bold=True)
        else:
            click.echo(migration)

    if not current_version:
        click.secho(
            "\nYou have not yet ran a migration. Catch your database up with ./run.py migrate",
            fg="magenta",
            bold=True,
        )


def get_current_version():
    current_path = DB_PATH
    if not path.isfile(current_path):
        for candidate in (path.join(DB_DIR, LEGACY_DB_FILENAME), OLD_DB_PATH):
            if path.isfile(candidate):
                current_path = candidate
                break
        else:
            return 0
    with sqlite3.connect(current_path) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT MAX(id) from version")
        except sqlite3.OperationalError:
            return 0
        return cursor.fetchone()[0]


def migration_pending() -> bool:
    """True when the newest migration is ahead of the database."""
    try:
        newest = sorted(f for f in listdir(MIG_DIR) if f.endswith(".sql"))[-1]
        return int(newest[:4]) > get_current_version()
    except (IndexError, ValueError, OSError):
        return False


def run_migrations() -> bool:
    """Apply any outstanding migrations without prompting.

    Called at web-UI startup: a container has no shell to run `lox migrate` in,
    and the database holds only lox's own bookkeeping.

    Returns:
        True if anything was applied.
    """
    adopt_legacy_db()
    if not migration_pending():
        return False
    current_version = get_current_version()
    makedirs(DB_DIR, exist_ok=True)
    applied = False
    with sqlite3.connect(DB_PATH) as conn:
        for migration in sorted(f for f in listdir(MIG_DIR) if f.endswith(".sql")):
            mig_version = int(migration[:4])
            if mig_version <= current_version:
                continue
            cursor = conn.cursor()
            with open(path.join(MIG_DIR, migration)) as mig_file:
                cursor.executescript(mig_file.read())
                cursor.execute("INSERT INTO version (id) VALUES (?)", (mig_version,))
            conn.commit()
            cursor.close()
            applied = True
    return applied


def check_if_migration_is_needed():
    current_version = get_current_version()
    most_recent_mig = sorted(f for f in listdir(MIG_DIR) if f.endswith(".sql"))[-1:][0]
    if path.exists(OLD_DB_PATH):
        click.secho(
            f"The database needs to be moved to the new directory ({DB_PATH}). Please run `lox migrate`.\n",
            fg="red",
            bold=True,
        )
    try:
        mig_version = int(most_recent_mig[:4])
    except TypeError:
        click.secho(
            f"\n{most_recent_mig} is improperly named. It must start with a four digit integer.",
            fg="red",
        )
        raise click.Abort from None
    if mig_version > current_version:
        click.secho(
            "The database needs updating. Please run `lox migrate`.\n",
            fg="red",
            bold=True,
        )


check_if_migration_is_needed()
