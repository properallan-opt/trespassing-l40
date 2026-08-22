from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger


def _config_path() -> Path:
    explicit = os.getenv("DIGLETT_PATH")
    if explicit:
        return Path(explicit)

    mounted = Path("/config/.diglett")
    if mounted.exists():
        return mounted

    return Path(".diglett")


def _read_ativar(path: Path) -> str:
    override = os.getenv("APP_ENVIRONMENT")
    if override in {"hml", "prod"}:
        return override

    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            clean = line.strip()
            if clean.startswith("_ATIVAR") and "=" in clean:
                return clean.split("=", 1)[1].strip().strip("'\"").split("#", 1)[0].strip()

    return "hml"


CONFIG_PATH = _config_path()
load_dotenv(CONFIG_PATH)
ATIVAR = _read_ativar(CONFIG_PATH)


def _env(name: str, default: str) -> str:
    if ATIVAR == "prod":
        return os.getenv(f"PROD_{name}", os.getenv(name, default))
    return os.getenv(name, default)


DIR_NORMAL = Path(_env("DIR_NORMAL", "/app/logs/normal"))
DIR_ERRO = Path(_env("DIR_ERRO", "/app/logs/erro"))
DIR_METRICAS = Path(_env("DIR_METRICAS", "/app/logs/metricas"))
MAX_FILE_SIZE_MB = int(_env("MAX_FILE_SIZE_MB", "2"))
RETENTION_SIZE = int(_env("RETENTION_SIZE", "50"))

try:
    for directory in (DIR_NORMAL, DIR_ERRO, DIR_METRICAS):
        directory.mkdir(parents=True, exist_ok=True)
except PermissionError:
    # Facilita testes fora do container quando /app não é gravável.
    DIR_NORMAL = Path("./logs/normal")
    DIR_ERRO = Path("./logs/erro")
    DIR_METRICAS = Path("./logs/metricas")
    for directory in (DIR_NORMAL, DIR_ERRO, DIR_METRICAS):
        directory.mkdir(parents=True, exist_ok=True)

logger.remove()
rotation = f"{MAX_FILE_SIZE_MB} MB"
fmt = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {module}.{function}:{line} | {message}"

logger.add(
    DIR_METRICAS / "log_metrics.log",
    filter=lambda record: record["level"].name == "DEBUG",
    rotation=rotation,
    retention=RETENTION_SIZE,
    format=fmt,
    enqueue=True,
)
logger.add(
    DIR_NORMAL / "log_normal.log",
    level="INFO",
    filter=lambda record: record["level"].name in {"INFO", "WARNING"},
    rotation=rotation,
    retention=RETENTION_SIZE,
    format=fmt,
    enqueue=True,
)
logger.add(
    DIR_ERRO / "log_erro.log",
    level="ERROR",
    rotation=rotation,
    retention=RETENTION_SIZE,
    format=fmt,
    enqueue=True,
    backtrace=True,
    diagnose=False,
)

if ATIVAR == "hml":
    logger.add(sys.stdout, level="DEBUG", format=fmt)
