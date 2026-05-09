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
from dataclasses import dataclass

from .models import BotConfig, CycleSnapshot
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

    Lifecycle:
        # At startup (after ConfigRepository.load()):
        watcher = ConfigWatcher(repo)
        watcher.initialize(config)

        # Before each new trading cycle:
        result = watcher.check_and_reload(user_id, bot_id)
        if result.reload_failed:
            emitter.emit(event_type="CONFIG_ERROR", ...)
            # decide: skip cycle or stop bot
        config = result.config
        snapshot = CycleSnapshot.from_config(config)

        # Read current config without triggering a reload:
        config = watcher.get_config()
    """

    def __init__(self, repo: ConfigRepository) -> None:
        self._repo = repo
        self._current: BotConfig | None = None

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def initialize(self, config: BotConfig) -> None:
        """
        Set the initial config after startup load.
        Must be called before check_and_reload() or get_config().
        """
        self._current = config
        logger.debug(
            "ConfigWatcher: initialized with version=%d for %s/%s.",
            config.config_version, config.user_id, config.bot_id,
        )

    # ------------------------------------------------------------------
    # Check and reload
    # ------------------------------------------------------------------

    def check_and_reload(self, user_id: str, bot_id: str) -> WatchResult:
        """
        Compare current config_version with DB. Reload if changed.

        Always returns WatchResult — never raises on validation failure.
        Raises only on unexpected DB errors (BotConfigNotFoundError, etc.)
        which the caller should treat as a critical fault.

        Call this once before each new trading cycle, never mid-cycle.
        """
        if self._current is None:
            raise RuntimeError(
                "ConfigWatcher.check_and_reload() called before initialize(). "
                "Call initialize(config) first."
            )

        old = self._current

        # Fast check: read only config_version from DB (lightweight query).
        db_version = self._fetch_version(user_id, bot_id)

        if db_version == old.config_version:
            logger.debug(
                "ConfigWatcher: version unchanged (%d) for %s/%s.",
                db_version, user_id, bot_id,
            )
            return WatchResult.unchanged(old)

        # Version changed — reload full config.
        logger.info(
            "ConfigWatcher: version changed %d -> %d for %s/%s, reloading.",
            old.config_version, db_version, user_id, bot_id,
        )

        new_config, validation = self._repo.reload(user_id, bot_id)

        if not validation.is_valid:
            # Soft-fail: keep old config, report failure to caller.
            logger.error(
                "ConfigWatcher: new config (version=%d) is invalid for %s/%s. "
                "Keeping version=%d. Errors: %s",
                db_version, user_id, bot_id,
                old.config_version, "; ".join(validation.errors),
            )
            return WatchResult.failed(old, db_version, validation)

        # Valid new config — update cache.
        self._current = new_config
        logger.info(
            "ConfigWatcher: config updated to version=%d for %s/%s.",
            new_config.config_version, user_id, bot_id,
        )
        return WatchResult.changed(old, new_config)

    # ------------------------------------------------------------------
    # Read cached config (no DB call)
    # ------------------------------------------------------------------

    def get_config(self) -> BotConfig:
        """
        Return the current cached config without touching the DB.
        Use during an active cycle (bot works with CycleSnapshot, but
        BotLoop may need to check status between ticks).
        """
        if self._current is None:
            raise RuntimeError(
                "ConfigWatcher.get_config() called before initialize()."
            )
        return self._current

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
