"""
Coletor de bilhetagem usado pelos containers de processamento.

A inferência apenas chama ``record(headers)`` para cada mensagem recebida.
Uma thread dedicada publica, em uma conexão RabbitMQ separada, um snapshot
cumulativo por dia. O snapshot cumulativo permite ao centralizador tratar
reentregas de mensagens de forma idempotente.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pika


class BillingReporter:
    def __init__(
        self,
        *,
        enabled: bool,
        environment: str,
        rabbit_host: str,
        rabbit_port: int,
        rabbit_user: str,
        rabbit_password: str,
        exchange: str,
        queue_name: str,
        report_interval_seconds: int = 3600,
        timezone_name: str = "America/Sao_Paulo",
        logger=None,
    ) -> None:
        self.enabled = enabled
        self.environment = environment
        self.rabbit_host = rabbit_host
        self.rabbit_port = int(rabbit_port)
        self.rabbit_user = rabbit_user
        self.rabbit_password = rabbit_password
        self.exchange = exchange
        self.queue_name = queue_name
        self.routing_key = f"{queue_name}-routing-key"
        self.report_interval_seconds = max(60, int(report_interval_seconds))
        self.local_tz = ZoneInfo(timezone_name)
        self.logger = logger

        self.hostname = socket.gethostname()
        # Novo UUID a cada processo. Um restart passa a ser uma nova fonte
        # cumulativa e não é confundido com snapshots antigos.
        self.instance_id = f"{self.hostname}:{uuid.uuid4().hex}"

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # {YYYY-MM-DD: {"cameras": {identifier: {...}}, "invalid_messages": int}}
        self._days: dict[str, dict] = {}

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        try:
            getattr(self.logger, level)(message)
        except Exception:
            pass

    @staticmethod
    def _normalize_id(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        value = str(value).strip()
        return value or None

    @classmethod
    def _header_id(cls, headers: dict, keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = cls._normalize_id(headers.get(key))
            if value is not None:
                return value
        return None

    @staticmethod
    def _camera_identifier(angel_id: str | None, camera_id: str) -> str:
        return f"{angel_id}:{camera_id}" if angel_id is not None else camera_id

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return

        self._thread = threading.Thread(
            target=self._run,
            name="billing-reporter",
            daemon=True,
        )
        self._thread.start()
        self._log(
            "info",
            (
                "Bilhetagem local iniciada | "
                f"hostname={self.hostname} | instance_id={self.instance_id} | "
                f"intervalo={self.report_interval_seconds}s"
            ),
        )

    def record(self, headers: dict | None) -> None:
        """Contabiliza uma mensagem recebida sem bloquear a inferência."""
        if not self.enabled:
            return

        headers = headers or {}
        angel_id = self._header_id(
            headers,
            (
                "AngelId", "AngelID", "angelId", "angelID", "angel_id",
                "AngleId", "AngleID", "angleId", "angleID", "angle_id",
            ),
        )
        camera_id = self._header_id(
            headers,
            ("CameraId", "cameraId", "cameraID", "camera_id"),
        )

        now_utc = datetime.now(timezone.utc)
        day = now_utc.astimezone(self.local_tz).date().isoformat()
        now_iso = now_utc.isoformat()

        with self._lock:
            day_state = self._days.setdefault(
                day,
                {"cameras": {}, "invalid_messages": 0},
            )

            # CameraId is the only mandatory identifier. AngelId/AngleId is
            # optional for backward-compatible producers.
            if camera_id is None:
                day_state["invalid_messages"] += 1
                return

            key = self._camera_identifier(angel_id, camera_id)
            camera = day_state["cameras"].get(key)

            if camera is None:
                day_state["cameras"][key] = {
                    "identifier": key,
                    "angel_id": angel_id,
                    "camera_id": camera_id,
                    "cumulative_frames": 1,
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                }
            else:
                camera["cumulative_frames"] += 1
                camera["last_seen"] = now_iso

    def _snapshot(self) -> list[dict]:
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._lock:
            reports = []
            for day, day_state in sorted(self._days.items()):
                cameras = [
                    dict(camera)
                    for _, camera in sorted(day_state["cameras"].items())
                ]
                if not cameras and day_state["invalid_messages"] == 0:
                    continue

                reports.append(
                    {
                        "schema_version": 1,
                        "type": "camera_usage_snapshot",
                        "environment": self.environment,
                        "hostname": self.hostname,
                        "instance_id": self.instance_id,
                        "day": day,
                        "generated_at": now_iso,
                        "invalid_messages": day_state["invalid_messages"],
                        "cameras": cameras,
                    }
                )

            return reports

    def _publish_report(self, report: dict) -> None:
        credentials = pika.PlainCredentials(
            self.rabbit_user,
            self.rabbit_password,
        )
        parameters = pika.ConnectionParameters(
            host=self.rabbit_host,
            port=self.rabbit_port,
            virtual_host="/",
            credentials=credentials,
            heartbeat=30,
            blocked_connection_timeout=30,
            connection_attempts=3,
            retry_delay=2,
        )

        connection = None
        try:
            connection = pika.BlockingConnection(parameters)
            channel = connection.channel()

            # O exchange já existe no sistema. Declaramos apenas a fila de
            # bilhetagem e fazemos o bind para não assumir o tipo do exchange.
            channel.queue_declare(queue=self.queue_name, durable=True)
            channel.queue_bind(
                queue=self.queue_name,
                exchange=self.exchange,
                routing_key=self.routing_key,
            )
            channel.confirm_delivery()

            body = json.dumps(
                report,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")

            channel.basic_publish(
                exchange=self.exchange,
                routing_key=self.routing_key,
                body=body,
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,  # mensagem persistente
                    timestamp=int(time.time()),
                    type="camera_usage_snapshot",
                ),
                mandatory=False,
            )
        finally:
            if connection is not None and connection.is_open:
                connection.close()

    def publish(self) -> None:
        """Publica todos os dias ainda mantidos em memória."""
        if not self.enabled:
            return

        for report in self._snapshot():
            try:
                self._publish_report(report)
                self._log(
                    "debug",
                    (
                        "Snapshot de bilhetagem publicado | "
                        f"day={report['day']} | cameras={len(report['cameras'])} | "
                        f"instance_id={self.instance_id}"
                    ),
                )
            except Exception as exc:
                # Não derruba a inferência. Como o contador é cumulativo, o
                # próximo snapshot recupera automaticamente os frames que não
                # conseguiram ser enviados nesta tentativa.
                self._log(
                    "error",
                    (
                        "Falha ao publicar snapshot de bilhetagem | "
                        f"day={report['day']} | erro={exc!r}"
                    ),
                )

        self._prune_old_days()

    def _prune_old_days(self) -> None:
        """Mantém no máximo o dia local atual e o anterior na RAM."""
        current_day = datetime.now(timezone.utc).astimezone(self.local_tz).date()

        with self._lock:
            for day in list(self._days):
                try:
                    parsed = datetime.strptime(day, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if (current_day - parsed).days > 1:
                    self._days.pop(day, None)

    def _run(self) -> None:
        while not self._stop_event.wait(self.report_interval_seconds):
            self.publish()

    def close(self) -> None:
        if not self.enabled:
            return

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

        # Flush final em encerramentos normais.
        self.publish()
