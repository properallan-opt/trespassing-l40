from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
import pika
from dotenv import load_dotenv

from conteiner_log.loguru_config import logger
from conteiner_oclusao.oclusion import OclusionDetector
from billing_reporter import BillingReporter


@dataclass
class OclusionSettings:
    ativar: str
    enabled: bool
    model_path: str
    confidence_threshold: float
    limite_fila_rabbitmq: int
    nome_da_fila_entrada_oclusao: str
    nome_da_fila_entrada_trespassing: str
    nome_da_fila_saida: str
    nome_da_fila_erros: str
    credentials_usuario: str
    credentials_senha: str
    rabbit_servidor: str
    rabbit_porta: int
    nome_anjos_exchange: str
    mensagens_prefetch: int
    billing_enabled: bool
    billing_queue: str
    billing_report_interval_seconds: int
    billing_timezone: str


def _config_path() -> Path:
    explicit = os.getenv("DIGLETT_PATH")
    if explicit:
        return Path(explicit)
    mounted = Path("/config/.diglett")
    return mounted if mounted.exists() else Path(".diglett")


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


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"true", "1", "yes", "y", "sim"}


def load_settings(path: str | Path | None = None) -> OclusionSettings:
    config_path = Path(path) if path else _config_path()
    load_dotenv(config_path)
    ativar = _read_ativar(config_path)

    def env(name: str, default: str | None = None) -> str | None:
        if ativar == "prod":
            return os.getenv(f"PROD_{name}", os.getenv(name, default))
        return os.getenv(name, default)

    return OclusionSettings(
        ativar=ativar,
        enabled=_bool(env("oclusao_chave", "True"), True),
        model_path=str(env("model_oclusion_path", "./modelos_prod/T4S_model_oclusion.pt")),
        confidence_threshold=float(env("FATOR_OCLUSAO_T4S", "0.90")),
        limite_fila_rabbitmq=int(env("limite_fila_rabbitmq", "150")),
        nome_da_fila_entrada_oclusao=str(env("nome_da_fila_entrada_oclusao", "")),
        nome_da_fila_entrada_trespassing=str(env("nome_da_fila_entrada_trespassing", "")),
        nome_da_fila_saida=str(env("nome_da_fila_saida", "")),
        nome_da_fila_erros=str(env("nome_da_fila_erros", "")),
        credentials_usuario=str(env("credentials_usuario", "")),
        credentials_senha=str(env("credentials_senha", "")),
        rabbit_servidor=str(env("rabbit_servidor", "")),
        rabbit_porta=int(env("rabbit_porta", "5672")),
        nome_anjos_exchange=str(env("nome_anjos_exchange", "anjosexchange")),
        mensagens_prefetch=int(env("mensagens_prefetch", "1")),
        billing_enabled=_bool(env("BILLING_ENABLED", "False")),
        billing_queue=str(env("BILLING_QUEUE", f"gtower_Trespassing_billing_{ativar}")),
        billing_report_interval_seconds=int(env("BILLING_REPORT_INTERVAL_SECONDS", "3600")),
        billing_timezone=str(env("BILLING_TIMEZONE", "America/Sao_Paulo")),
    )


def validate_settings(settings: OclusionSettings, *, require_rabbit: bool = True) -> None:
    if settings.enabled and not Path(settings.model_path).exists():
        raise RuntimeError(f"Modelo de oclusão não encontrado: {settings.model_path}")

    if require_rabbit:
        required = {
            "nome_da_fila_entrada_oclusao": settings.nome_da_fila_entrada_oclusao,
            "nome_da_fila_entrada_trespassing": settings.nome_da_fila_entrada_trespassing,
            "nome_da_fila_saida": settings.nome_da_fila_saida,
            "nome_da_fila_erros": settings.nome_da_fila_erros,
            "credentials_usuario": settings.credentials_usuario,
            "credentials_senha": settings.credentials_senha,
            "rabbit_servidor": settings.rabbit_servidor,
            "nome_anjos_exchange": settings.nome_anjos_exchange,
        }
        missing = [name for name, value in required.items() if not value or value == "CHANGE_ME"]
        if missing:
            raise RuntimeError(f"Configuração Rabbit ausente no .diglett: {missing}")


def decode_image_bytes(body: bytes) -> np.ndarray:
    encoded = np.frombuffer(body, dtype=np.uint8)
    if encoded.size == 0:
        raise ValueError("Body da imagem vazio.")
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("cv2.imdecode não conseguiu decodificar a imagem.")
    return image


def is_camera_problem(class_id: int, confidence: float, threshold: float) -> bool:
    # Mantém a mesma regra do smokefire-l40: classe 0 = câmera normal;
    # qualquer outra classe acima do limiar = problema/oclusão.
    return int(class_id) != 0 and float(confidence) >= float(threshold)


def build_occlusion_headers(
    headers_in: dict[str, Any],
    *,
    class_id: int,
    class_name: str,
    confidence: float,
    blocked: bool,
) -> dict[str, Any]:
    detection = {
        "detection": bool(blocked),
        "class_id": int(class_id),
        "class_name": class_name,
        "confidence": float(confidence),
    }
    return {
        **headers_in,
        "OclusionDetection": json.dumps(detection),
        "CameraProblem": str(class_id),
        "CameraProblemPercentage": str(float(confidence)),
        "OclusionClass": class_name,
    }


def _publish(ch, settings: OclusionSettings, properties, *, queue: str, body: bytes, headers: dict[str, Any]) -> None:
    properties.headers = headers
    properties.delivery_mode = properties.delivery_mode or 2
    ch.basic_publish(
        exchange=settings.nome_anjos_exchange,
        routing_key=queue + "-routing-key",
        body=body,
        properties=properties,
    )


def _publish_error(ch, settings: OclusionSettings, properties, *, error: Exception) -> None:
    headers = {
        **(properties.headers or {}),
        "Mensagem": "Erro no estágio de oclusão",
        "Contexto": "conteiner_oclusao.t4s_oclusao",
        "Error": str(error),
    }
    _publish(ch, settings, properties, queue=settings.nome_da_fila_erros, body=b"", headers=headers)


def make_callback(
    settings: OclusionSettings,
    detector: OclusionDetector | None,
    billing_reporter: BillingReporter,
) -> Callable:
    def callback(ch, method, properties, body):
        start = time.time()
        headers_in = dict(properties.headers or {})
        camera_id = headers_in.get("CameraId")

        # Mesmo ponto de contabilização do smokefire: entrada do primeiro consumer.
        try:
            billing_reporter.record(headers_in)
        except Exception:
            logger.exception("Erro ao contabilizar frame para bilhetagem")

        try:
            if settings.enabled:
                image = decode_image_bytes(body)
                assert detector is not None
                class_id, class_name, confidence = detector.predict(image)
            else:
                class_id, class_name, confidence = 0, "disabled", 0.0

            blocked = settings.enabled and is_camera_problem(
                class_id,
                confidence,
                settings.confidence_threshold,
            )
            headers_out = build_occlusion_headers(
                headers_in,
                class_id=class_id,
                class_name=class_name,
                confidence=confidence,
                blocked=blocked,
            )

            if blocked:
                # Mesmo comportamento do smokefire-l40: se há problema de câmera,
                # não executa o segundo modelo e já encaminha à fila final.
                destination = settings.nome_da_fila_saida
            else:
                # Headers recebidos (inclusive TrespassingROI) são preservados.
                destination = settings.nome_da_fila_entrada_trespassing

            _publish(ch, settings, properties, queue=destination, body=body, headers=headers_out)
            ch.basic_ack(delivery_tag=method.delivery_tag)

            logger.debug(
                "Oclusão processada | CameraId={} | class_id={} | class={} | conf={:.4f} | blocked={} | destino={} | {:.4f}s",
                camera_id,
                class_id,
                class_name,
                confidence,
                blocked,
                destination,
                time.time() - start,
            )
        except Exception as exc:
            try:
                _publish_error(ch, settings, properties, error=exc)
            finally:
                ch.basic_ack(delivery_tag=method.delivery_tag)
            logger.exception("Erro no estágio de oclusão | CameraId={}", camera_id)

    return callback


def setup_rabbitmq(settings: OclusionSettings):
    validate_settings(settings, require_rabbit=True)
    credentials = pika.PlainCredentials(settings.credentials_usuario, settings.credentials_senha)
    parameters = pika.ConnectionParameters(
        host=settings.rabbit_servidor,
        port=settings.rabbit_porta,
        virtual_host="/",
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
    )
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    args = {"x-max-length": settings.limite_fila_rabbitmq}

    for queue in (
        settings.nome_da_fila_entrada_oclusao,
        settings.nome_da_fila_entrada_trespassing,
        settings.nome_da_fila_saida,
        settings.nome_da_fila_erros,
    ):
        channel.queue_declare(queue=queue, durable=True, arguments=args)
        channel.queue_bind(
            queue=queue,
            exchange=settings.nome_anjos_exchange,
            routing_key=queue + "-routing-key",
        )

    channel.basic_qos(prefetch_count=settings.mensagens_prefetch)
    return connection, channel


def run_consumer(settings: OclusionSettings) -> None:
    validate_settings(settings, require_rabbit=True)
    detector = OclusionDetector(settings.model_path) if settings.enabled else None
    billing_reporter = BillingReporter(
        enabled=settings.billing_enabled,
        environment=settings.ativar,
        rabbit_host=settings.rabbit_servidor,
        rabbit_port=settings.rabbit_porta,
        rabbit_user=settings.credentials_usuario,
        rabbit_password=settings.credentials_senha,
        exchange=settings.nome_anjos_exchange,
        queue_name=settings.billing_queue,
        report_interval_seconds=settings.billing_report_interval_seconds,
        timezone_name=settings.billing_timezone,
        logger=logger,
    )
    billing_reporter.start()
    connection = None
    try:
        connection, channel = setup_rabbitmq(settings)
        channel.basic_consume(
            queue=settings.nome_da_fila_entrada_oclusao,
            auto_ack=False,
            on_message_callback=make_callback(settings, detector, billing_reporter),
        )
        logger.info(
            "Oclusão Rabbit consumer iniciado | env={} | queue={} | next={} | enabled={} | model={}",
            settings.ativar,
            settings.nome_da_fila_entrada_oclusao,
            settings.nome_da_fila_entrada_trespassing,
            settings.enabled,
            settings.model_path,
        )
        channel.start_consuming()
    finally:
        try:
            billing_reporter.close()
        except Exception:
            logger.exception("Erro ao finalizar bilhetagem")
        if connection is not None and connection.is_open:
            connection.close()


def main() -> None:
    settings = load_settings()
    run_consumer(settings)


if __name__ == "__main__":
    main()
