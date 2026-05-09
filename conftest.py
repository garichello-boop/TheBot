"""
conftest.py — корневой конфиг pytest.
Добавляет корень проекта в sys.path чтобы пакеты (broker, config, ...)
были доступны при запуске тестов из любой директории.
"""
import sys
from pathlib import Path

# TheBot/ → в начало пути, перекрывает любые системные пакеты с тем же именем
sys.path.insert(0, str(Path(__file__).parent))
