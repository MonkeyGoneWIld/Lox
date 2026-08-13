"""Sign-in accounts: a username, a password, and a session.

The web interface used to be gated by one shared token pasted into
``docker-compose.yml``. That is a bad fit for the thing it protects -- it can
spend tracker budget, read an authenticated Deezer session and post uploads --
because a token in a compose file is in your shell history, your backups and
your terminal scrollback, it cannot be changed without editing a file and
restarting, and there is no way to tell one person from another.

So the first time lox starts with no accounts it asks you to make one, and from
then on it wants a username and a password.

Passwords are stored as scrypt hashes with a per-password salt. scrypt is in the
standard library and is memory-hard, so a stolen file does not hand over the
password. Nothing here ever writes a password to the log.
"""

import hashlib
import hmac
import os
import secrets
import time
from typing import Any

import msgspec

ACCOUNTS_FILENAME = "accounts.toml"

# scrypt parameters. n=2**15 with r=8 costs about 32 MB and ~100 ms per hash on
# a typical container -- slow enough to make guessing expensive, fast enough
# that signing in feels immediate.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_BYTES = 32
SALT_BYTES = 16

MIN_PASSWORD = 10
"""Short enough not to be annoying, long enough that scrypt does the rest."""


class AccountError(Exception):
    """Raised when an account cannot be created or changed."""


class Account(msgspec.Struct):
    """One sign-in."""

    username: str
    salt: str
    hash: str
    created: float = 0.0
    # Bumped whenever the password changes, which invalidates old sessions.
    generation: int = 1


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """Hash a password with scrypt.

    Args:
        password: The plain password.
        salt: Existing salt to reuse when verifying; a fresh one is generated
            when creating.

    Returns:
        Tuple of (salt hex, hash hex).
    """
    salt = salt or secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
        maxmem=64 * 1024 * 1024,
    )
    return salt.hex(), digest.hex()


def normalise(username: str) -> str:
    """Usernames are compared case-insensitively and without surrounding space."""
    return (username or "").strip().lower()


class AccountStore:
    """The accounts file, read and written as TOML beside settings.toml."""

    def __init__(self, directory: str) -> None:
        """Initialize the store.

        Args:
            directory: Where ``accounts.toml`` lives.
        """
        self.path = os.path.join(directory, ACCOUNTS_FILENAME)
        self._accounts: dict[str, Account] = {}
        self.load()

    def load(self) -> None:
        """Read the accounts file, tolerating its absence."""
        try:
            with open(self.path, "rb") as handle:
                raw = msgspec.toml.decode(handle.read())
        except (OSError, msgspec.DecodeError):
            self._accounts = {}
            return
        accounts: dict[str, Account] = {}
        for entry in raw.get("account") or []:
            try:
                account = msgspec.convert(entry, Account, strict=False)
            except (msgspec.ValidationError, ValueError):
                continue
            accounts[normalise(account.username)] = account
        self._accounts = accounts

    def save(self) -> None:
        """Write the accounts file atomically, readable only by the owner."""
        payload = msgspec.toml.encode(
            {"account": [msgspec.to_builtins(a) for a in self._accounts.values()]}
        )
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "wb") as handle:
            handle.write(payload)
        with contextlib_suppress_oserror():
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @property
    def empty(self) -> bool:
        """True when nobody has signed up yet, so setup is still open."""
        return not self._accounts

    def usernames(self) -> list[str]:
        """Every account name, for the settings page."""
        return sorted(a.username for a in self._accounts.values())

    def get(self, username: str) -> Account | None:
        """One account by name, or None."""
        return self._accounts.get(normalise(username))

    def create(self, username: str, password: str) -> Account:
        """Add an account.

        Args:
            username: The name to sign in with.
            password: The plain password.

        Returns:
            The created account.

        Raises:
            AccountError: If the name or password is unusable, or taken.
        """
        name = (username or "").strip()
        if len(name) < 2:
            raise AccountError("Pick a username of at least 2 characters.")
        if len(name) > 64:
            raise AccountError("That username is too long.")
        if self.get(name):
            raise AccountError(f"There is already an account called {name}.")
        self._check_password(password)

        salt, digest = hash_password(password)
        account = Account(username=name, salt=salt, hash=digest, created=time.time())
        self._accounts[normalise(name)] = account
        self.save()
        return account

    def verify(self, username: str, password: str) -> Account | None:
        """Check a sign-in.

        Compared in constant time, and an unknown username still pays the cost
        of a hash so that a wrong name and a wrong password take the same
        amount of time to reject.

        Args:
            username: Supplied name.
            password: Supplied password.

        Returns:
            The account when the password matches, otherwise None.
        """
        account = self.get(username)
        if account is None:
            hash_password(password, b"\x00" * SALT_BYTES)
            return None
        try:
            salt = bytes.fromhex(account.salt)
        except ValueError:
            return None
        _, digest = hash_password(password, salt)
        return account if hmac.compare_digest(digest, account.hash) else None

    def set_password(self, username: str, password: str) -> None:
        """Change a password, invalidating that account's existing sessions.

        Raises:
            AccountError: If the account is unknown or the password is unusable.
        """
        account = self.get(username)
        if account is None:
            raise AccountError(f"No account called {username}.")
        self._check_password(password)
        salt, digest = hash_password(password)
        account.salt, account.hash = salt, digest
        account.generation += 1
        self.save()

    def delete(self, username: str) -> None:
        """Remove an account.

        Raises:
            AccountError: If it is unknown, or the last one -- deleting that
                would lock everyone out of a running server.
        """
        account = self.get(username)
        if account is None:
            raise AccountError(f"No account called {username}.")
        if len(self._accounts) == 1:
            raise AccountError("That is the only account; removing it would lock you out.")
        del self._accounts[normalise(username)]
        self.save()

    @staticmethod
    def _check_password(password: str) -> None:
        """Reject a password too weak to be worth hashing.

        Raises:
            AccountError: If it is too short.
        """
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(f"Use a password of at least {MIN_PASSWORD} characters.")


def contextlib_suppress_oserror() -> Any:
    """chmod is not meaningful on every filesystem; failing it is not an error."""
    import contextlib

    return contextlib.suppress(OSError, NotImplementedError)


# ----------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------
#
# The cookie carries who you are and is signed, rather than being a random id
# looked up in a table. That keeps sessions working across a restart without
# persisting anything, and the signature is what stops the browser editing the
# username in it.
#
# The signing key is derived from the account file itself, so it changes when a
# password does -- which is what makes "change your password" actually end the
# other sessions rather than only appearing to.

SESSION_TTL = 60 * 60 * 24 * 30


def _signing_key(store: "AccountStore") -> bytes:
    """A key derived from every stored hash.

    Any password change alters it, so tokens signed with the old key stop
    verifying. There is nothing to store and nothing to rotate by hand.
    """
    material = "|".join(
        f"{a.username}:{a.hash}:{a.generation}" for a in sorted(store._accounts.values(), key=lambda a: a.username)  # noqa: SLF001
    )
    return hashlib.sha256(("lox-session/" + material).encode("utf-8")).digest()


def issue_session(store: "AccountStore", username: str, remember: bool = True) -> str:
    """Mint a signed session token for an account."""
    expires = int(time.time()) + (SESSION_TTL if remember else 60 * 60 * 12)
    payload = f"{normalise(username)}.{expires}"
    signature = hmac.new(_signing_key(store), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{signature}"


def read_session(store: "AccountStore", token: str | None) -> str | None:
    """The username a session token proves, or None if it proves nothing.

    Args:
        store: The account store, for the signing key and the account itself.
        token: The cookie value.

    Returns:
        The username, or None when the token is malformed, expired, signed with
        a superseded key, or names an account that no longer exists.
    """
    if not token or token.count(".") != 2:
        return None
    username, expires, signature = token.split(".")
    expected = hmac.new(
        _signing_key(store), f"{username}.{expires}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        if int(expires) < time.time():
            return None
    except ValueError:
        return None
    account = store.get(username)
    return account.username if account else None
