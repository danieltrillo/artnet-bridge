"""Process supervisor: config file, engine lifetime, reloads and the web UI."""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import signal
import tempfile
import time

from . import __version__
from .config import Config, ConfigError, load_config, parse_yaml
from .engine import Engine

log = logging.getLogger(__name__)


class RingLogHandler(logging.Handler):
    """Keeps the last few hundred log lines for the web UI."""

    def __init__(self, size: int = 400) -> None:
        super().__init__()
        self.records: collections.deque[dict] = collections.deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(
                {
                    "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                    "level": record.levelname.lower(),
                    "logger": record.name.replace("artbridge.", ""),
                    "message": self.format(record),
                }
            )
        except Exception:
            pass

    def to_list(self) -> list[dict]:
        return list(self.records)


class Supervisor:
    def __init__(self, config_path: str, *, web_override: tuple[str, int] | None = None) -> None:
        self.config_path = config_path
        self.web_override = web_override
        self.config: Config | None = None
        self.config_text = ""
        self.config_error = ""
        self.engine: Engine | None = None
        self.ring = RingLogHandler()
        self.ring.setFormatter(logging.Formatter("%(message)s"))
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._web_runner = None

    # -- config -----------------------------------------------------------

    def read_text(self) -> str:
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def _write_text(self, text: str) -> None:
        directory = os.path.dirname(os.path.abspath(self.config_path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".yaml", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.config_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async def apply_text(self, text: str) -> Config:
        """Validate, save and apply a new config. Raises ConfigError."""
        config = parse_yaml(text)
        async with self._lock:
            self._write_text(text)
            self.config_text = text
            await self._swap_engine(config)
        return config

    async def reload_from_disk(self) -> None:
        text = self.read_text()
        try:
            config = parse_yaml(text)
        except ConfigError as exc:
            self.config_error = str(exc)
            log.error("config reload failed: %s", exc)
            return
        async with self._lock:
            self.config_text = text
            await self._swap_engine(config)

    async def _swap_engine(self, config: Config) -> None:
        old = self.engine
        if old is not None:
            await old.stop()
        self.config = config
        self.config_error = ""
        logging.getLogger("artbridge").setLevel(config.log_level.upper())
        engine = Engine(config)
        try:
            await engine.start()
        except OSError as exc:
            self.config_error = f"cannot start: {exc}"
            log.error("engine failed to start: %s", exc)
            await engine.stop()
            self.engine = None
            return
        self.engine = engine

    # -- lifetime -----------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> int:
        logging.getLogger().addHandler(self.ring)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            loop.add_signal_handler(signal.SIGHUP, lambda: asyncio.ensure_future(self.reload_from_disk()))
        except (NotImplementedError, RuntimeError, AttributeError):
            pass

        log.info("ArtBridge %s starting, config %s", __version__, self.config_path)
        self.config_text = self.read_text()
        try:
            self.config = load_config(self.config_path)
        except ConfigError as exc:
            self.config_error = str(exc)
            log.error("config error: %s", exc)
            log.error("the web UI is still available so you can fix it")

        if self.web_override is not None:
            await self._start_web(*self.web_override)
        elif self.config is None:
            await self._start_web("0.0.0.0", 8080)
        elif self.config.web.enabled:
            await self._start_web(self.config.web.bind, self.config.web.port)

        if self.config is not None:
            await self._swap_engine(self.config)

        await self._stop.wait()
        log.info("shutting down")
        if self.engine is not None:
            await self.engine.stop()
        if self._web_runner is not None:
            await self._web_runner.cleanup()
        return 0

    async def _start_web(self, bind: str, port: int) -> None:
        try:
            from .web.server import make_runner
        except ImportError as exc:
            log.error("web UI disabled: %s (install python3-aiohttp)", exc)
            return
        try:
            self._web_runner = await make_runner(self, bind, port)
            log.info("web UI listening on http://%s:%d", bind, port)
        except OSError as exc:
            log.error("web UI could not bind %s:%d: %s", bind, port, exc)

    # -- status for the UI -------------------------------------------------

    def status(self) -> dict:
        if self.engine is not None:
            data = self.engine.status()
        else:
            data = {"version": __version__, "running": False, "networks": [], "routes": [], "nodes": [], "unrouted": []}
        data["config_path"] = self.config_path
        data["config_error"] = self.config_error
        return data
