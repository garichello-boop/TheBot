"""
bot_config/watcher.py

Monitors bot_configs for changes between trading cycles.

ConfigWatcher is called once before each new cycle opens (never during
an active cycle — the bot works exclusively with CycleSnapshot then).

Soft-fail contract:
    If the new config is invalid, ConfigWatcher keeps the last known-good
    config and returns WatchResult(reload_failed=True). The caller
    (BotLoop) is responsible for emitting CONFIG_ERROR and deciding
    whether to skip the cycle or stop the bot.
    ConfigWatcher never raises on validation failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Optional

from .models import BotConfig, BotStatus, CycleSnapshot
from .repository import ConfigRepository, BotConfigNotFoundError
from .validator import ValidationResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WatchResult:
    """
    Returned by ConfigWatcher.check_and_reload().

    Exactly one of (config_unchanged, config_changed, reload_failed) is True.

    config        — the config to use for the upcoming cycle.
                    Always the last known-good config, never an invalid one.
    prev_version  — config_version before the check.
    curr_version  — config_version after the check (same if unchanged).
    errors        — validation errors when reload_failed=True, else empty.
    """
    config_unchanged: bool
    config_changed:   bool
    reload_failed:    bool

    config:       BotConfig
    prev_version: int
    curr_version: int
    errors:       tuple[str, ...]

    @classmethod
    def unchanged(cls, config: BotConfig) -> WatchResult:
        return cls(
            config_unchanged=True,
            config_changed=False,
            reload_failed=False,
            config=config,
            prev_version=config.config_version,
            curr_version=config.config_version,
            errors=(),
        )

    @classmethod
    def changed(cls, old: BotConfig, new: BotConfig) -> WatchResult:
        return cls(
            config_unchanged=False,
            config_changed=True,
            reload_failed=False,
            config=new,
            prev_version=old.config_version,
            curr_version=new.config_version,
            errors=(),
        )

    @classmethod
    def failed(cls, old: BotConfig, new_version: int, result: ValidationResult) -> WatchResult:
        return cls(
            config_unchanged=False,
            config_changed=False,
            reload_failed=True,
            config=old,                     # keep last known-good config
            prev_version=old.config_version,
            curr_version=new_version,
            errors=result.errors,
        )

    def __str__(self) -> str:
        if self.config_unchanged:
            return f"WatchResult(unchanged, version={self.curr_version})"
        if self.config_changed:
            return (
                f"WatchResult(changed, "
                f"{self.prev_version} -> {self.curr_version})"
            )
        return (
            f"WatchResult(reload_failed, "
            f"version={self.curr_version}, "
            f"errors=[{'; '.join(self.errors)}])"
        )


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------

class ConfigWatcher:
    """
    Detects config_version changes and reloads bot_configs before new cycles.

    Construction
    ------------
    Two patterns are supported:

    Pattern A — bot.py (auto-load at construction):
        watcher = ConfigWatcher(repo, user_id=user_id, bot_id=bot_id)
        config  = watcher.get_config()   # works immediately, no initialize() needed

        When user_id and bot_id are provided, ConfigWatcher calls
        repo.load(user_id, bot_id) in __init__, acquiring the advisory lock
        and validating the config. get_config() is immediately available.

    Pattern B — manual (tests, staged setup):
        watcher = ConfigWatcher(repo)
        watcher.initialize(config)        # set config explicitly
        config  = watcher.get_config()   # works after initialize()

    Lifecycle:
        # Before each new trading cycle:
        result = watcher.check_and_reload(user_id, bot_id)
        if result.reload_failed:
            emitter.emit(event_type="CONFIG_ERROR", ...)
        config   = result.config
        snapshot = CycleSnapshot.from_config(config)

        # Read current config without DB call:
        config = watcher.get_config()

        # Create CycleSnapshot for a new cycle:
        snapshot = watcher.create_snapshot()

        # Set CLOSE_ONLY from bot code (manual TP/DCA cancellation):
        watcher.set_close_only(user_id, bot_id)
    """

    def __init__(
        self,
        repo: ConfigRepository,
        *,
        user_id: Optional[str] = None,
        bot_id: Optional[str] = None,
    ) -> None:
        self._repo     = repo
        self._user_id  = user_id
        self._bot_id   = bot_id
        self._current: BotConfig | None = None

        # Pattern A: auto-load at construction when user_id/bot_id provided.
        # Calls repo.load() which acquires the advisory lock and validates.
        if user_id is not None and bot_id is not None:
            config = repo.load(user_id, bot_id)
            self._current = config
            logger.debug(
                "ConfigWatcher: auto-loaded config for %s/%s (version=%d).",
                user_id, bot_id, config.config_version,
            )

    # ------------------------------------------------------------------
    # Initialization (Pattern B)
    # ------------------------------------------------------------------

    def initialize(self, config: BotConfig) -> None:
        """
        Set the initial config explicitly (Pattern B).

        Must be called before check_and_reload() or get_config() when
        ConfigWatcher was constructed without user_id/bot_id.

        If user_id/bot_id were passed to __init__ (Pattern A), this method
        can still be used to override the auto-loaded config (e.g. in tests).
        """
        self._current = config
        if self._user_id is None:
            self._user_id = config.user_id
        if self._bot_id is None:
            self._bot_id = config.bot_id
        logger.debug(
            "ConfigWatcher: initialized with version=%d for %s/%s.",
            config.config_version, config.user_id, config.bot_id,
        )

    # ------------------------------------------------------------------
    # Check and reload
    # ------------------------------------------------------------------

    def check_and_reload(
        self,
        user_id: Optional[str] = None,
        bot_id: Optional[str] = None,
    ) -> WatchResult:
        """
        Compare current config_version with DB. Reload if changed.

        user_id / bot_id are optional: if omitted, the values stored at
        construction (Pattern A) or via initialize() (Pattern B) are used.

        Always returns WatchResult — never raises on validation failure.
        Raises only on unexpected DB errors (BotConfigNotFoundError, etc.)
        which the caller should treat as a critical fault.

        Call this once before each new trading cycle, never mid-cycle.
        """
        if self._current is None:
            raise RuntimeError(
                "ConfigWatcher.check_and_reload() called before config is set. "
                "Pass user_id/bot_id to __init__() or call initialize() first."
            )

        uid = user_id or self._user_id
        bid = bot_id  or self._bot_id
        if uid is None or bid is None:
            raise RuntimeError(
                "ConfigWatcher.check_and_reload() requires user_id and bot_id. "
                "Pass them as arguments or to ConfigWatcher.__init__()."
            )

        old = self._current

        # Fast check: read only config_version (avoids deserializing JSONB).
        db_version = self._fetch_version(uid, bid)

        if db_version == old.config_version:
            logger.debug(
                "ConfigWatcher: version unchanged (%d) for %s/%s.",
                db_version, uid, bid,
            )
            return WatchResult.unchanged(old)

        # Version changed — reload full config.
        logger.info(
            "ConfigWatcher: version changed %d -> %d for %s/%s, reloading.",
            old.config_version, db_version, uid, bid,
        )

        new_config, validation = self._repo.reload(uid, bid)

        if not validation.is_valid:
            logger.error(
                "ConfigWatcher: new config (version=%d) is invalid for %s/%s. "
                "Keeping version=%d. Errors: %s",
                db_version, uid, bid,
                old.config_version, "; ".join(validation.errors),
            )
            return WatchResult.failed(old, db_version, validation)

        self._current = new_config
        logger.info(
            "ConfigWatcher: config updated to version=%d for %s/%s.",
            new_config.config_version, uid, bid,
        )
        return WatchResult.changed(old, new_config)

    # ------------------------------------------------------------------
    # Read cached config (no DB call)
    # ------------------------------------------------------------------

    def get_config(self) -> BotConfig:
        """
        Return the current cached config without touching the DB.

        Available immediately after construction with user_id/bot_id (Pattern A),
        or after initialize() (Pattern B).
        """
        if self._current is None:
            raise RuntimeError(
                "ConfigWatcher.get_config() called before config is set. "
                "Pass user_id/bot_id to __init__() or call initialize() first."
            )
        return self._current

    def create_snapshot(self) -> CycleSnapshot:
        """
        Create a CycleSnapshot from the current cached config.

        Call after check_and_reload() to freeze strategy_params for the new
        cycle. The bot works exclusively with this snapshot until the cycle
        closes — WFO updates to bot_configs are invisible to it.

        Raises:
            RuntimeError: if called before config is set.
        """
        return CycleSnapshot.from_config(self.get_config())

    # ------------------------------------------------------------------
    # Status mutations
    # ------------------------------------------------------------------

    def set_close_only(self, user_id: str, bot_id: str) -> None:
        """
        Set bot status to CLOSE_ONLY in bot_configs.

        Called by BotLoop._execute_set_close_only() when a TP or DCA is
        manually cancelled (operator intervention detected).

        Updates both the DB (via ConfigRepository.set_status()) and the
        local cached config so subsequent get_config() calls reflect the
        new status without waiting for the next check_and_reload().

        Raises:
            BotConfigNotFoundError: row not found in bot_configs.
        """
        self._repo.set_status(user_id, bot_id, BotStatus.CLOSE_ONLY)

        # Optimistically update the cached config to reflect the status change.
        # config_version is not updated here — the DB increment will be detected
        # by check_and_reload() on the next cycle boundary, which will reload
        # the full config at that point.
        if self._current is not None:
            self._current = replace(self._current, status=BotStatus.CLOSE_ONLY)

        logger.info(
            "ConfigWatcher: status set to CLOSE_ONLY for %s/%s.",
            user_id, bot_id,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fetch_version(self, user_id: str, bot_id: str) -> int:
        """
        Lightweight query: read only config_version from DB.
        Avoids deserializing strategy_params JSONB on every tick boundary.
        """
        from db import transaction  # local import to keep module top clean

        with transaction() as cur:
            cur.execute(
                "SELECT config_version FROM bot_configs "
                "WHERE user_id = %s AND bot_id = %s",
                (user_id, bot_id),
            )
            row = cur.fetchone()

        if row is None:
            raise BotConfigNotFoundError(
                f"bot_configs row disappeared for {user_id}/{bot_id}."
            )

        return int(row["config_version"])
