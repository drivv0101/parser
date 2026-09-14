import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "app.log"
# Тесты не должны писать в боевой лог: иначе история прогонов смешивается с их запросами
LOG_TO_FILE = os.getenv("STROY_LOG_TO_FILE", "1") != "0"

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("stroyceny")
    root.setLevel(logging.INFO)

    if LOG_TO_FILE:
        LOG_DIR.mkdir(exist_ok=True)
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # apscheduler и uvicorn логируют сами по себе — понижаем их болтливость до предупреждений
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"stroyceny.{name}")
