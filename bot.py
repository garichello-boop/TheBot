"""
bot.py вЂ” С‚РѕС‡РєР° РІС…РѕРґР° С‚РѕСЂРіРѕРІРѕРіРѕ Р±РѕС‚Р°.

Р—Р°РїСѓСЃРє:
  python bot.py --user alex --bot-id phor_dca
  python bot.py --user alex --bot-id btc_mean_rev

Startup sequence:
  1. РџР°СЂСЃРёРЅРі Р°СЂРіСѓРјРµРЅС‚РѕРІ.
  2. Р—Р°РіСЂСѓР·РєР° AppSettings (Pydantic, РёР· .env / ENV).
  3. Р Р°СЃС€РёС„СЂРѕРІРєР° СЃРµРєСЂРµС‚РѕРІ С‡РµСЂРµР· KeyManager (РјР°СЃС‚РµСЂ-РїР°СЂРѕР»СЊ РІРІРѕРґРёС‚СЃСЏ РѕРґРёРЅ СЂР°Р·).
  4. РќР°СЃС‚СЂРѕР№РєР° Observability (EventEmitter, РІСЃРµ sinks).
  5. РРЅРёС†РёР°Р»РёР·Р°С†РёСЏ РїРѕРґСЃРёСЃС‚РµРј (broker, market, state).
  6. РџСЂРѕРІРµСЂРєР° bot_registry вЂ” Р·Р°С‰РёС‚Р° РѕС‚ РґРІРѕР№РЅРѕРіРѕ Р·Р°РїСѓСЃРєР°.
  7. StateRecovery.reconcile() вЂ” СЃРІРµСЂРєР° СЃ Р±РёСЂР¶РµР№ РїРѕСЃР»Рµ Р»СЋР±РѕРіРѕ СЂРµСЃС‚Р°СЂС‚Р°.
  8. BotLoop.run() вЂ” Р±РµСЃРєРѕРЅРµС‡РЅС‹Р№ tick-loop.

РџСЂРё Ctrl+C вЂ” РєРѕСЂСЂРµРєС‚РЅР°СЏ РѕСЃС‚Р°РЅРѕРІРєР° СЃ emit BOT_STOPPED.
РџСЂРё Р»СЋР±РѕРј РЅРµРѕР±СЂР°Р±РѕС‚Р°РЅРЅРѕРј РёСЃРєР»СЋС‡РµРЅРёРё вЂ” emit BOT_CRASHED, exit(1).

РџСЂРµРґРїРѕР»РѕР¶РµРЅРёСЏ РѕР± РёРЅС‚РµСЂС„РµР№СЃР°С… Рџ6 (СЃРІРµСЂРёС‚СЊ РїСЂРё РёРЅС‚РµРіСЂР°С†РёРё):
  - StateRecovery.startup(user_id, bot_id, broker) в†’ BotState
  - StateRepository(db_pool) в†’ repo
  - RegistryRepository(db_pool) в†’ repo
  - StateManager(db_pool, emitter) в†’ manager

РџСЂРµРґРїРѕР»РѕР¶РµРЅРёСЏ РѕР± РёРЅС‚РµСЂС„РµР№СЃР°С… Рџ5:
  - ConfigRepository(db_pool) в†’ repo
  - ConfigWatcher(repo, user_id, bot_id) в†’ watcher
  - ConfigWatcher.create_snapshot() в†’ CycleSnapshot
  - ConfigWatcher.set_close_only(user_id, bot_id) в†’ None
  - ConfigWatcher.get_config() в†’ BotConfig

РџСЂРµРґРїРѕР»РѕР¶РµРЅРёСЏ РѕР± РёРЅС‚РµСЂС„РµР№СЃР°С… Рџ3:
  - setup_observability(settings, bot_id, ticker, tg_token, tg_chat_id) в†’ EventEmitter

РџСЂРµРґРїРѕР»РѕР¶РµРЅРёСЏ РѕР± РёРЅС‚РµСЂС„РµР№СЃР°С… Рџ2:
  - ProviderFactory.create(settings.market) в†’ MarketDataProvider

РџСЂРµРґРїРѕР»РѕР¶РµРЅРёСЏ РѕР± РёРЅС‚РµСЂС„РµР№СЃР°С… Рџ4:
  - BrokerFactory.create(settings.broker, token=...) в†’ IBroker
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="РђР»РіРѕС‚СЂРµР№РґРёРЅРіРѕРІС‹Р№ Р±РѕС‚",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "РџСЂРёРјРµСЂС‹:\n"
            "  python bot.py --user alex --bot-id phor_dca\n"
            "  python bot.py --user alex --bot-id btc_mean_rev\n"
        ),
    )
    parser.add_argument(
        "--user",
        required=True,
        help="РРґРµРЅС‚РёС„РёРєР°С‚РѕСЂ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ (СЃРѕРІРїР°РґР°РµС‚ СЃ user_id РІ bot_configs).",
    )
    parser.add_argument(
        "--bot-id",
        required=True,
        dest="bot_id",
        help="РРґРµРЅС‚РёС„РёРєР°С‚РѕСЂ Р±РѕС‚Р° (СЃРѕРІРїР°РґР°РµС‚ СЃ bot_id РІ bot_configs).",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="РџРµСЂРµРѕРїСЂРµРґРµР»РёС‚СЊ СѓСЂРѕРІРµРЅСЊ Р»РѕРіРёСЂРѕРІР°РЅРёСЏ РёР· РєРѕРЅС„РёРіР° (DEBUG/INFO/WARNING).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    user_id = args.user
    bot_id  = args.bot_id

    # ------------------------------------------------------------------
    # 1. Р‘Р°Р·РѕРІС‹Р№ logging РґРѕ РёРЅРёС†РёР°Р»РёР·Р°С†РёРё Observability
    #    (С‡С‚РѕР±С‹ РІРёРґРµС‚СЊ РѕС€РёР±РєРё РґРѕ СЃС‚Р°СЂС‚Р° EventEmitter)
    # ------------------------------------------------------------------
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    logger.info("РЎС‚Р°СЂС‚ Р±РѕС‚Р°: user=%s, bot_id=%s", user_id, bot_id)

    # ------------------------------------------------------------------
    # 2. AppSettings (Рџ0)
    # ------------------------------------------------------------------
    try:
        from config.settings import AppSettings          # noqa: PLC0415
        from config.bot_loop_settings import BotLoopSettings  # noqa: PLC0415
        settings          = AppSettings()
        bot_loop_settings = BotLoopSettings()
    except Exception as exc:
        logger.critical("РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РєРѕРЅС„РёРі: %s", exc)
        sys.exit(1)

    # РџРµСЂРµРѕРїСЂРµРґРµР»РµРЅРёРµ СѓСЂРѕРІРЅСЏ Р»РѕРіРёСЂРѕРІР°РЅРёСЏ РёР· Р°СЂРіСѓРјРµРЅС‚Р° CLI
    if args.log_level:
        logging.getLogger().setLevel(args.log_level.upper())

    # ------------------------------------------------------------------
    # 3. KeyManager (Рџ1) вЂ” РјР°СЃС‚РµСЂ-РїР°СЂРѕР»СЊ РІРІРѕРґРёС‚СЃСЏ РѕРґРёРЅ СЂР°Р·
    # ------------------------------------------------------------------
    try:
        from keys.key_manager import KeyManager  # noqa: PLC0415
        km = KeyManager()
        master_password = os.environ.get("BOT_MASTER_PASSWORD") or getpass.getpass("Р’РІРµРґРёС‚Рµ РјР°СЃС‚РµСЂ-РїР°СЂРѕР»СЊ: ")
        km.load(master_password)
        del master_password  # РЅРµ РґРµСЂР¶Р°С‚СЊ РІ РїР°РјСЏС‚Рё РґРѕР»СЊС€Рµ С‡РµРј РЅСѓР¶РЅРѕ
    except Exception as exc:
        logger.critical("РћС€РёР±РєР° Р·Р°РіСЂСѓР·РєРё РєР»СЋС‡РµР№: %s", exc)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 4. Observability (Рџ3)
    #    Р­РјРёС‚С‚РµСЂ СЃС‚Р°СЂС‚СѓРµС‚ СЂР°РЅРѕ вЂ” РІСЃРµ РѕС€РёР±РєРё СЃС‚Р°СЂС‚Р° РёРґСѓС‚ РІ Telegram.
    #    РўРёРєРµСЂ СѓС‚РѕС‡РЅСЏРµС‚СЃСЏ СЃСЂР°Р·Сѓ РїРѕСЃР»Рµ Р·Р°РіСЂСѓР·РєРё BotConfig (С€Р°Рі 6).
    # ------------------------------------------------------------------
    try:
        from observability import setup_observability  # noqa: PLC0415

        emitter = setup_observability(
            settings=settings,
            bot_id=bot_id,
            ticker="",   # СѓС‚РѕС‡РЅСЏРµС‚СЃСЏ РІ С€Р°РіРµ 6 С‡РµСЂРµР· emitter.set_ticker()
            tg_token=km.get("TG_BOT_TOKEN"),
            tg_chat_id=km.get("TG_CHAT_ID"),
        )
    except Exception as exc:
        logger.critical("РќРµ СѓРґР°Р»РѕСЃСЊ РёРЅРёС†РёР°Р»РёР·РёСЂРѕРІР°С‚СЊ Observability: %s", exc)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 5. Р‘Р°Р·Р° РґР°РЅРЅС‹С… вЂ” РїСѓР» СЃРѕРµРґРёРЅРµРЅРёР№
    # ------------------------------------------------------------------
    try:
        from db import init_pool as create_pool  # noqa: PLC0415
        db_pool = create_pool(settings.database.url)
    except Exception as exc:
        logger.critical("РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРґРєР»СЋС‡РёС‚СЊСЃСЏ Рє PostgreSQL: %s", exc)
        emitter.emit(
            event_type="PG_CONNECTION_FAILED",
            level="CRITICAL",
            message=f"PostgreSQL РЅРµРґРѕСЃС‚СѓРїРµРЅ РїСЂРё СЃС‚Р°СЂС‚Рµ: {exc}",
            payload={"error": str(exc)},
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # 6. РљРѕРЅС„РёРіСѓСЂР°С†РёСЏ Р±РѕС‚Р° (Рџ5)
    # ------------------------------------------------------------------
    try:
        from bot_config import ConfigRepository, ConfigWatcher  # noqa: PLC0415
        config_repo    = ConfigRepository(db_pool)
        config_watcher = ConfigWatcher(config_repo, user_id=user_id, bot_id=bot_id)

        # РџРµСЂРІРёС‡РЅР°СЏ Р·Р°РіСЂСѓР·РєР° РєРѕРЅС„РёРіР° вЂ” SELECT FOR UPDATE (Р·Р°С‰РёС‚Р° РѕС‚ РґРІРѕР№РЅРѕРіРѕ Р·Р°РїСѓСЃРєР°)
        bot_config = config_watcher.get_config()
        ticker     = bot_config.ticker

        # РћР±РЅРѕРІР»СЏРµРј С‚РёРєРµСЂ РІ СЌРјРёС‚С‚РµСЂРµ вЂ” РІСЃРµ РїРѕСЃР»РµРґСѓСЋС‰РёРµ СЃРѕР±С‹С‚РёСЏ Р±СѓРґСѓС‚ СЃ РїСЂР°РІРёР»СЊРЅС‹Рј С‚РёРєРµСЂРѕРј
        emitter.set_ticker(ticker)

        logger.info(
            "РљРѕРЅС„РёРі Р·Р°РіСЂСѓР¶РµРЅ: ticker=%s, strategy=%s, status=%s, version=%d",
            ticker, bot_config.strategy_name, bot_config.status, bot_config.config_version,
        )
    except Exception as exc:
        logger.critical("РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РєРѕРЅС„РёРі Р±РѕС‚Р°: %s", exc)
        emitter.emit(
            event_type="CONFIG_VALIDATION_FAILED",
            level="CRITICAL",
            message=f"РћС€РёР±РєР° Р·Р°РіСЂСѓР·РєРё bot_config: {exc}",
            payload={"user_id": user_id, "bot_id": bot_id, "error": str(exc)},
        )
        sys.exit(1)

    # РџСЂРѕРІРµСЂСЏРµРј СЃС‚Р°С‚СѓСЃ вЂ” РµСЃР»Рё STOPPED, РЅРµ СЃС‚Р°СЂС‚СѓРµРј
    if bot_config.status == "STOPPED":
        logger.info("bot_configs.status=STOPPED вЂ” Р±РѕС‚ РЅРµ Р·Р°РїСѓСЃРєР°РµС‚СЃСЏ")
        sys.exit(0)

    # ------------------------------------------------------------------
    # 7. Р‘СЂРѕРєРµСЂ (Рџ4)
    # ------------------------------------------------------------------
    try:
        from broker.broker_factory import BrokerFactory
        bundle = BrokerFactory.create(
            settings=settings,
            key_manager=km,
            emitter=emitter,
            trade_repo=None,
            bot_id=bot_id,
        )
        broker = bundle.broker
        tracker = bundle.tracker
        bundle.start()
        logger.info("Р‘СЂРѕРєРµСЂ РёРЅРёС†РёР°Р»РёР·РёСЂРѕРІР°РЅ: %s", settings.broker.broker_type)
    except Exception as exc:
        logger.critical("РќРµ СѓРґР°Р»РѕСЃСЊ РёРЅРёС†РёР°Р»РёР·РёСЂРѕРІР°С‚СЊ Р±СЂРѕРєРµСЂ: %s", exc)
        emitter.emit(
            event_type="CREDENTIALS_MISSING",
            level="CRITICAL",
            message=f"РћС€РёР±РєР° РёРЅРёС†РёР°Р»РёР·Р°С†РёРё Р±СЂРѕРєРµСЂР°: {exc}",
            payload={"broker_type": settings.broker.broker_type, "error": str(exc)},
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # 8. Р С‹РЅРѕС‡РЅС‹Рµ РґР°РЅРЅС‹Рµ (Рџ2)
    # ------------------------------------------------------------------
    try:
        from market_data import ProviderFactory  # noqa: PLC0415
        market = ProviderFactory.create(settings.market)
        market.start()
        market.subscribe(ticker)
        logger.info("MarketDataProvider Р·Р°РїСѓС‰РµРЅ: ticker=%s", ticker)
    except Exception as exc:
        logger.critical("РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РїСѓСЃС‚РёС‚СЊ MarketDataProvider: %s", exc)
        emitter.emit(
            event_type="MARKET_WATCHER_ERROR",
            level="CRITICAL",
            message=f"РћС€РёР±РєР° Р·Р°РїСѓСЃРєР° СЂС‹РЅРѕС‡РЅС‹С… РґР°РЅРЅС‹С…: {exc}",
            payload={"ticker": ticker, "error": str(exc)},
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # 9. State (Рџ6): registry, repository, manager
    # ------------------------------------------------------------------
    try:
        from bot_state import (  # noqa: PLC0415
            StateRepository,
            StateManager,
            RegistryRepository,
            StateRecovery,
        )
        state_repo    = StateRepository(db_pool)
        state_manager = StateManager(db_pool, emitter=emitter)
        registry_repo = RegistryRepository(db_pool)
    except Exception as exc:
        logger.critical("РќРµ СѓРґР°Р»РѕСЃСЊ РёРЅРёС†РёР°Р»РёР·РёСЂРѕРІР°С‚СЊ State-РїРѕРґСЃРёСЃС‚РµРјСѓ: %s", exc)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 10. РџСЂРѕРІРµСЂРєР° bot_registry + StateRecovery.reconcile() (Рџ6)
    # ------------------------------------------------------------------
    emitter.emit(
        event_type="BOT_STARTING",
        level="INFO",
        message=f"Р—Р°РїСѓСЃРє Р±РѕС‚Р°: {bot_id}",
        payload={"user_id": user_id, "bot_id": bot_id, "ticker": ticker},
    )

    try:
        # StateRecovery РІС‹РїРѕР»РЅСЏРµС‚:
        #   - РїСЂРѕРІРµСЂРєСѓ bot_registry (Р·Р°С‰РёС‚Р° РѕС‚ РґРІРѕР№РЅРѕРіРѕ Р·Р°РїСѓСЃРєР°)
        #   - РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёРµ bot_state РёР· PostgreSQL
        #   - reconciliation СЃ Р±РёСЂР¶РµР№ (Р°РєС‚РёРІРЅС‹Рµ РѕСЂРґРµСЂР°, fills)
        #   - 8-С€Р°РіРѕРІС‹Р№ Startup/Restart Recovery РёР· РўР— 7
        # РџРѕСЃР»Рµ СЌС‚РѕРіРѕ РІС‹Р·РѕРІР° Р±РѕС‚ РјРѕР¶РµС‚ С‚РѕСЂРіРѕРІР°С‚СЊ.
        StateRecovery.startup(
            user_id=user_id,
            bot_id=bot_id,
            ticker=ticker,
            broker=broker,
            state_repo=state_repo,
            state_manager=state_manager,
            registry_repo=registry_repo,
            emitter=emitter,
            virtual_balance=bot_config.virtual_balance,
            market=market,  # for OHLCV playback on PaperBroker restart
        )

        logger.info("StateRecovery Р·Р°РІРµСЂС€С‘РЅ вЂ” РіРѕС‚РѕРІ Рє С‚РѕСЂРіРѕРІР»Рµ")
    except RuntimeError as exc:
        # RuntimeError РѕС‚ StateRecovery вЂ” РЅР°РїСЂРёРјРµСЂ, Р±РѕС‚ СѓР¶Рµ Р·Р°РїСѓС‰РµРЅ
        logger.critical("StateRecovery РѕС‚РєР°Р·Р°Р»: %s", exc)
        emitter.emit(
            event_type="BOT_CRASHED",
            level="CRITICAL",
            message=str(exc),
            payload={"user_id": user_id, "bot_id": bot_id},
        )
        sys.exit(1)
    except Exception as exc:
        logger.critical("РћС€РёР±РєР° РїСЂРё РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёРё СЃРѕСЃС‚РѕСЏРЅРёСЏ: %s", exc)
        emitter.emit(
            event_type="BOT_CRASHED",
            level="CRITICAL",
            message=f"StateRecovery failed: {exc}",
            payload={"user_id": user_id, "bot_id": bot_id, "error": str(exc)},
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # 11. РЎС‚СЂР°С‚РµРіРёСЏ
    # ------------------------------------------------------------------
    try:
        from business_logic import create_strategy  # noqa: PLC0415
        strategy = create_strategy(bot_config.strategy_name)
        logger.info("РЎС‚СЂР°С‚РµРіРёСЏ: %s", strategy.name())
    except Exception as exc:
        logger.critical("РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕР·РґР°С‚СЊ СЃС‚СЂР°С‚РµРіРёСЋ '%s': %s", bot_config.strategy_name, exc)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 12. BotLoop
    # ------------------------------------------------------------------
    from business_logic import BotLoop  # noqa: PLC0415

    bot = BotLoop(
        market=market,
        broker=broker,
        state_manager=state_manager,
        state_repo=state_repo,
        registry_repo=registry_repo,
        config_watcher=config_watcher,
        strategy=strategy,
        emitter=emitter,
        settings=bot_loop_settings,
        bot_id=bot_id,
        user_id=user_id,
    )

    # ------------------------------------------------------------------
    # 13. Р—Р°РїСѓСЃРє вЂ” Р±Р»РѕРєРёСЂСѓРµС‚ РїРѕС‚РѕРє РґРѕ РѕСЃС‚Р°РЅРѕРІРєРё
    # ------------------------------------------------------------------
    try:
        bot.run()
    except KeyboardInterrupt:
        logger.info("РџРѕР»СѓС‡РµРЅ Ctrl+C вЂ” РєРѕСЂСЂРµРєС‚РЅР°СЏ РѕСЃС‚Р°РЅРѕРІРєР°")
        emitter.emit(
            event_type="BOT_STOPPED",
            level="INFO",
            message="РћСЃС‚Р°РЅРѕРІР»РµРЅ РѕРїРµСЂР°С‚РѕСЂРѕРј (Ctrl+C)",
            payload={"bot_id": bot_id},
        )
    except Exception as exc:
        logger.critical("РќРµРѕР±СЂР°Р±РѕС‚Р°РЅРЅРѕРµ РёСЃРєР»СЋС‡РµРЅРёРµ РІ BotLoop: %s", exc, exc_info=True)
        emitter.emit(
            event_type="BOT_CRASHED",
            level="CRITICAL",
            message=f"РќРµРѕР±СЂР°Р±РѕС‚Р°РЅРЅРѕРµ РёСЃРєР»СЋС‡РµРЅРёРµ: {exc}",
            payload={"error": str(exc), "bot_id": bot_id},
        )
        sys.exit(1)
    finally:
        # РљРѕСЂСЂРµРєС‚РЅРѕРµ Р·Р°РІРµСЂС€РµРЅРёРµ РїРѕРґСЃРёСЃС‚РµРј
        try:
            registry_repo.update_status(user_id, bot_id, operational_status="STOPPED")
        except Exception:
            pass
        try:
            market.stop()
        except Exception:
            pass
        try:
            db_pool.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()