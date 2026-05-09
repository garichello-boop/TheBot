"""
Тесты KeyManager.
Запуск: python test_key_manager.py
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from key_manager import KeyManager, _encrypt, _decrypt, _derive_key


class TestEncryptDecrypt(unittest.TestCase):

    def test_roundtrip(self):
        plaintext = b'{"API_KEY": "secret123"}'
        password = b"strongpassword"
        encrypted = _encrypt(plaintext, password)
        decrypted = _decrypt(encrypted, password)
        self.assertEqual(plaintext, decrypted)

    def test_wrong_password_raises(self):
        plaintext = b'{"KEY": "value"}'
        encrypted = _encrypt(plaintext, b"correct")
        with self.assertRaises(Exception):
            _decrypt(encrypted, b"wrong")

    def test_different_salts_each_time(self):
        plaintext = b'{"KEY": "value"}'
        password = b"pass"
        enc1 = _encrypt(plaintext, password)
        enc2 = _encrypt(plaintext, password)
        # Соль и nonce случайные → разные файлы
        self.assertNotEqual(enc1, enc2)
        # Но оба расшифровываются
        self.assertEqual(_decrypt(enc1, password), plaintext)
        self.assertEqual(_decrypt(enc2, password), plaintext)

    def test_tampered_ciphertext_raises(self):
        plaintext = b'{"KEY": "value"}'
        encrypted = bytearray(_encrypt(plaintext, b"pass"))
        # Портим один байт в шифртексте
        encrypted[30] ^= 0xFF
        with self.assertRaises(Exception):
            _decrypt(bytes(encrypted), b"pass")


class TestKeyManager(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.keys_path = Path(self.tmpdir) / "keys.enc"
        self.password = "test_master_password"
        self.store = {
            "BYBIT_API_KEY": "api_key_value",
            "BYBIT_API_SECRET": "api_secret_value",
            "TG_BOT_TOKEN": "123456:ABC",
        }
        # Создаём тестовый keys.enc
        plaintext = json.dumps(self.store).encode()
        self.keys_path.write_bytes(_encrypt(plaintext, self.password.encode()))

    # ── load() ────────────────────────────────────────────────────────────────

    def test_load_success(self):
        km = KeyManager(str(self.keys_path))
        km.load(self.password)   # не должно бросить

    def test_load_wrong_password(self):
        km = KeyManager(str(self.keys_path))
        with self.assertRaises(ValueError) as ctx:
            km.load("wrongpassword")
        self.assertIn("Неверный мастер-пароль", str(ctx.exception))

    def test_load_missing_file(self):
        km = KeyManager("/nonexistent/keys.enc")
        with self.assertRaises(FileNotFoundError) as ctx:
            km.load(self.password)
        self.assertIn("keys.enc", str(ctx.exception))
        self.assertIn("setup", str(ctx.exception))

    def test_load_corrupted_file(self):
        self.keys_path.write_bytes(b"\x00" * 50)
        km = KeyManager(str(self.keys_path))
        with self.assertRaises(ValueError):
            km.load(self.password)

    def test_load_too_short_file(self):
        self.keys_path.write_bytes(b"\x00" * 10)
        km = KeyManager(str(self.keys_path))
        with self.assertRaises(ValueError) as ctx:
            km.load(self.password)
        self.assertIn("повреждён", str(ctx.exception))

    # ── get() ─────────────────────────────────────────────────────────────────

    def test_get_existing_key(self):
        km = KeyManager(str(self.keys_path))
        km.load(self.password)
        self.assertEqual(km.get("BYBIT_API_KEY"), "api_key_value")
        self.assertEqual(km.get("TG_BOT_TOKEN"), "123456:ABC")

    def test_get_missing_key(self):
        km = KeyManager(str(self.keys_path))
        km.load(self.password)
        with self.assertRaises(KeyError) as ctx:
            km.get("NONEXISTENT_KEY")
        self.assertIn("NONEXISTENT_KEY", str(ctx.exception))
        self.assertIn("setup", str(ctx.exception))

    def test_get_before_load(self):
        km = KeyManager(str(self.keys_path))
        with self.assertRaises(RuntimeError) as ctx:
            km.get("BYBIT_API_KEY")
        self.assertIn("load()", str(ctx.exception))

    def test_get_all_keys(self):
        km = KeyManager(str(self.keys_path))
        km.load(self.password)
        for name, value in self.store.items():
            self.assertEqual(km.get(name), value)

    # ── Unicode и спецсимволы ─────────────────────────────────────────────────

    def test_unicode_values(self):
        store = {"KEY": "значение_с_кириллицей_и_символами_!@#$%"}
        plaintext = json.dumps(store, ensure_ascii=False).encode()
        path = Path(self.tmpdir) / "unicode.enc"
        path.write_bytes(_encrypt(plaintext, b"pass"))
        km = KeyManager(str(path))
        km.load("pass")
        self.assertEqual(km.get("KEY"), "значение_с_кириллицей_и_символами_!@#$%")


class TestArgon2Determinism(unittest.TestCase):
    """Один пароль + одна соль → всегда один ключ."""

    def test_derive_key_deterministic(self):
        password = b"deterministic_test"
        salt = os.urandom(16)
        key1 = _derive_key(password, salt)
        key2 = _derive_key(password, salt)
        self.assertEqual(key1, key2)

    def test_derive_key_length(self):
        key = _derive_key(b"pass", os.urandom(16))
        self.assertEqual(len(key), 32)  # 256 бит

    def test_derive_key_different_salts(self):
        password = b"same_password"
        key1 = _derive_key(password, os.urandom(16))
        key2 = _derive_key(password, os.urandom(16))
        self.assertNotEqual(key1, key2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
