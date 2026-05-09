"""
Пункт 1: Хранение и шифрование API-ключей.

Схема:
    Мастер-пароль → Argon2id → 256-битный ключ → AES-256-GCM → keys.enc

Формат keys.enc:
    [16 байт соль][12 байт nonce][шифртекст][16 байт GCM-тег]
"""

import json
import os
import sys
import getpass
from pathlib import Path

from argon2.low_level import hash_secret_raw, Type
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ── Константы Argon2id ────────────────────────────────────────────────────────
# Привязаны к формату файла. При изменении необходимо заново запустить setup.

_ARGON2_MEMORY_KIB   = 65_536   # 64 MB
_ARGON2_ITERATIONS   = 3
_ARGON2_PARALLELISM  = 1
_ARGON2_HASH_LEN     = 32       # 256 бит → ключ AES-256

_SALT_LEN  = 16
_NONCE_LEN = 12


# ── KeyManager ────────────────────────────────────────────────────────────────

class KeyManager:
    """
    Загружает зашифрованное хранилище ключей и предоставляет доступ по имени.

    Использование:
        km = KeyManager()
        km.load(master_password)
        api_key = km.get("BYBIT_API_KEY")
    """

    def __init__(self, keys_path: str = "keys.enc") -> None:
        self._path = Path(keys_path)
        self._store: dict[str, str] | None = None

    # ── Публичный интерфейс ───────────────────────────────────────────────────

    def load(self, password: str) -> None:
        """
        Расшифровывает keys.enc и держит результат в памяти.

        Raises:
            FileNotFoundError: файл не найден.
            ValueError:        неверный пароль или файл повреждён.
        """
        if not self._path.exists():
            raise FileNotFoundError(
                f"Файл ключей не найден: {self._path}\n"
                f"Запустите: python key_manager.py setup"
            )

        raw = self._path.read_bytes()

        min_len = _SALT_LEN + _NONCE_LEN + 1
        if len(raw) < min_len:
            raise ValueError("Файл ключей повреждён: слишком короткий.")

        salt  = raw[:_SALT_LEN]
        nonce = raw[_SALT_LEN:_SALT_LEN + _NONCE_LEN]
        ciphertext_with_tag = raw[_SALT_LEN + _NONCE_LEN:]

        key = _derive_key(password.encode(), salt)

        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext_with_tag, None)
        except Exception:
            # Намеренно единое сообщение: не даём различить неверный пароль
            # от повреждённого файла — это утечка информации.
            raise ValueError(
                "Неверный мастер-пароль или файл ключей повреждён."
            )

        self._store = json.loads(plaintext.decode())

    def get(self, name: str) -> str:
        """
        Возвращает значение ключа по имени.

        Raises:
            RuntimeError: load() ещё не был вызван.
            KeyError:     ключ с таким именем не найден.
        """
        if self._store is None:
            raise RuntimeError(
                "KeyManager не инициализирован. Вызовите load() перед использованием."
            )
        if name not in self._store:
            raise KeyError(
                f"Ключ '{name}' не найден в хранилище. Проверьте setup."
            )
        return self._store[name]


# ── Шифрование / дешифрование ────────────────────────────────────────────────

def _derive_key(password: bytes, salt: bytes) -> bytes:
    """Argon2id: пароль + соль → 256-битный ключ AES."""
    return hash_secret_raw(
        secret=password,
        salt=salt,
        time_cost=_ARGON2_ITERATIONS,
        memory_cost=_ARGON2_MEMORY_KIB,
        parallelism=_ARGON2_PARALLELISM,
        hash_len=_ARGON2_HASH_LEN,
        type=Type.ID,
    )


def _encrypt(plaintext: bytes, password: bytes) -> bytes:
    """Шифрует plaintext. Возвращает соль + nonce + шифртекст + GCM-тег."""
    salt  = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key   = _derive_key(password, salt)
    ciphertext_with_tag = AESGCM(key).encrypt(nonce, plaintext, None)
    return salt + nonce + ciphertext_with_tag


def _decrypt(data: bytes, password: bytes) -> bytes:
    """Расшифровывает данные из формата keys.enc."""
    salt  = data[:_SALT_LEN]
    nonce = data[_SALT_LEN:_SALT_LEN + _NONCE_LEN]
    ciphertext_with_tag = data[_SALT_LEN + _NONCE_LEN:]
    key   = _derive_key(password, salt)
    return AESGCM(key).decrypt(nonce, ciphertext_with_tag, None)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cmd_setup(keys_path: Path) -> None:
    """
    Интерактивный ввод ключей. Создаёт/перезаписывает keys.enc.
    """
    print("=== KeyManager Setup ===")
    print("Введите пары ключ/значение. Пустое имя — завершить ввод.\n")

    store: dict[str, str] = {}

    while True:
        name = input("Имя ключа (Enter для завершения): ").strip()
        if not name:
            break
        value = getpass.getpass(f"Значение для {name}: ")
        if not value:
            print(f"  Пропущено: пустое значение.")
            continue
        store[name] = value
        print(f"  Добавлено: {name}")

    if not store:
        print("\nНи одного ключа не введено. Файл не создан.")
        sys.exit(1)

    print()
    password = getpass.getpass("Мастер-пароль: ")
    confirm  = getpass.getpass("Подтвердите мастер-пароль: ")

    if password != confirm:
        print("Пароли не совпадают. Файл не создан.")
        sys.exit(1)

    if not password:
        print("Пустой пароль не допускается.")
        sys.exit(1)

    plaintext = json.dumps(store, ensure_ascii=False).encode()
    encrypted = _encrypt(plaintext, password.encode())
    keys_path.write_bytes(encrypted)

    print(f"\n✓ Сохранено {len(store)} ключ(ей) в {keys_path}")


def _cmd_list(keys_path: Path) -> None:
    """Показывает имена ключей (значения скрыты)."""
    _require_file(keys_path)
    password = getpass.getpass("Мастер-пароль: ")
    store = _load_store(keys_path, password)
    print(f"\nКлючи в {keys_path}:")
    for name in sorted(store):
        print(f"  • {name}")
    print(f"\nВсего: {len(store)}")


def _cmd_get(keys_path: Path) -> None:
    """Показывает все ключи с именами и значениями (только для отладки)."""
    _require_file(keys_path)
    print("⚠️  Внимание: значения будут показаны в открытом виде.")
    password = getpass.getpass("Мастер-пароль: ")
    store = _load_store(keys_path, password)
    print(f"\nКлючи в {keys_path}:")
    for name, value in sorted(store.items()):
        print(f"  {name} = {value}")


def _require_file(keys_path: Path) -> None:
    if not keys_path.exists():
        print(f"Файл не найден: {keys_path}")
        print("Запустите: python key_manager.py setup")
        sys.exit(1)


def _load_store(keys_path: Path, password: str) -> dict[str, str]:
    try:
        raw = keys_path.read_bytes()
        plaintext = _decrypt(raw, password.encode())
        return json.loads(plaintext.decode())
    except Exception:
        print("Неверный мастер-пароль или файл повреждён.")
        sys.exit(1)


# ── Entrypoint ───────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="KeyManager — управление зашифрованными API-ключами"
    )
    parser.add_argument(
        "command",
        choices=["setup", "list", "get"],
        help="setup: создать хранилище | list: имена ключей | get: показать все (отладка)"
    )
    parser.add_argument(
        "--keys-path",
        default="keys.enc",
        help="Путь к файлу keys.enc (по умолчанию: keys.enc)"
    )

    args = parser.parse_args()
    keys_path = Path(args.keys_path)

    if args.command == "setup":
        _cmd_setup(keys_path)
    elif args.command == "list":
        _cmd_list(keys_path)
    elif args.command == "get":
        _cmd_get(keys_path)


if __name__ == "__main__":
    main()
