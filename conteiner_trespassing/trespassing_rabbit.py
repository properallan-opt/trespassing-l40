from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
import pika
from dotenv import load_dotenv

from conteiner_log.loguru_config import logger
from conteiner_trespassing.detector import TrespassingDetector
from conteiner_trespassing.roi_provider import (
    MissingROIError,
    ROIError,
    ROIResolver,
    camera_identifier,
)


@dataclass
class AppSettings:
    ativar: str
    model_path: str
    image_size: int
    confidence_threshold: float
    iou_threshold: float
    device: str
    person_class_id: int
    verbose_model: bool

    roi_registry_path: str
    roi_local_enabled: bool
    roi_message_enabled: bool
    roi_message_keys: tuple[str, ...]
    missing_roi_policy: str
    include_roi_in_output_headers: bool

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

    log_erro: bool = True
    janela_metricas: int = 10


@dataclass
class MetricsState:
    count_msgs: int = 0
    count_detections: int = 0
    count_missing_roi: int = 0
    process_times: list[float] = field(default_factory=list)
    window_start: float = field(default_factory=time.time)


@dataclass
class RuntimeContext:
    settings: AppSettings
    detector: TrespassingDetector
    roi_resolver: ROIResolver
    metrics: MetricsState = field(default_factory=MetricsState)


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


def load_settings(path: str | Path | None = None) -> AppSettings:
    config_path = Path(path) if path else _config_path()
    load_dotenv(config_path)
    ativar = _read_ativar(config_path)

    def env(name: str, default: str | None = None) -> str | None:
        if ativar == "prod":
            return os.getenv(f"PROD_{name}", os.getenv(name, default))
        return os.getenv(name, default)

    return AppSettings(
        ativar=ativar,
        model_path=str(env("MODEL_PATH", "./modelos_prod/yolo26n_ncnn_model")),
        image_size=int(env("IMAGE_SIZE", "320")),
        confidence_threshold=float(env("CONFIDENCE_THRESHOLD", "0.25")),
        iou_threshold=float(env("IOU_THRESHOLD", "0.70")),
        device=str(env("DEVICE", "cpu")),
        person_class_id=int(env("PERSON_CLASS_ID", "0")),
        verbose_model=_bool(env("VERBOSE_MODEL", "False")),
        roi_registry_path=str(env("ROI_REGISTRY_PATH", "/config/camera_rois.json")),
        roi_local_enabled=_bool(env("ROI_LOCAL_ENABLED", "True"), True),
        roi_message_enabled=_bool(env("ROI_MESSAGE_ENABLED", "True"), True),
        roi_message_keys=tuple(
            key.strip()
            for key in str(env("ROI_MESSAGE_KEYS", "TrespassingROI,perimeter_polygon,PerimeterPolygon,AreaOfInterest")).split(",")
            if key.strip()
        ),
        missing_roi_policy=str(env("MISSING_ROI_POLICY", "skip")).strip().lower(),
        include_roi_in_output_headers=_bool(env("INCLUDE_ROI_IN_OUTPUT_HEADERS", "False")),
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
        log_erro=_bool(env("LOG_ERRO", "True"), True),
        janela_metricas=int(env("JANELA_METRICAS", "10")),
    )


def validate_settings(settings: AppSettings, *, require_rabbit: bool = True) -> None:
    if settings.missing_roi_policy not in {"skip", "error"}:
        raise RuntimeError("MISSING_ROI_POLICY must be 'skip' or 'error'.")

    if not Path(settings.model_path).exists():
        raise RuntimeError(f"Model path does not exist: {settings.model_path}")

    if require_rabbit:
        required = {
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
            raise RuntimeError(f"Rabbit configuration missing in .diglett: {missing}")


def build_context(settings: AppSettings, *, require_rabbit: bool = True) -> RuntimeContext:
    validate_settings(settings, require_rabbit=require_rabbit)
    detector = TrespassingDetector(
        settings.model_path,
        image_size=settings.image_size,
        confidence_threshold=settings.confidence_threshold,
        iou_threshold=settings.iou_threshold,
        device=settings.device,
        person_class_id=settings.person_class_id,
        verbose=settings.verbose_model,
    )
    resolver = ROIResolver(
        settings.roi_registry_path,
        message_enabled=settings.roi_message_enabled,
        local_enabled=settings.roi_local_enabled,
        message_keys=settings.roi_message_keys,
    )
    return RuntimeContext(settings=settings, detector=detector, roi_resolver=resolver)


def decode_image_bytes(body: bytes) -> np.ndarray:
    encoded = np.frombuffer(body, dtype=np.uint8)
    if encoded.size == 0:
        raise ValueError("Empty image body.")
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("cv2.imdecode could not decode the image.")
    return image


def _header_id(headers: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = headers.get(key)
        if value not in (None, ""):
            return value
    return None


def _camera_id(headers: dict[str, Any]) -> Any:
    return _header_id(headers, ("CameraId", "cameraId", "cameraID", "camera_id"))


def _angel_id(headers: dict[str, Any]) -> Any:
    # AngelId is the legacy/canonical field used by the project. AngleId and
    # common casing variants are accepted to keep producers compatible.
    return _header_id(
        headers,
        (
            "AngelId", "AngelID", "angelId", "angelID", "angel_id",
            "AngleId", "AngleID", "angleId", "angleID", "angle_id",
        ),
    )


def _properties_with_headers(properties: pika.BasicProperties, headers: dict[str, Any]) -> pika.BasicProperties:
    properties.headers = headers
    properties.delivery_mode = properties.delivery_mode or 2
    return properties


def _publish_error(ch, settings: AppSettings, properties, *, message: str, context: str, error: Exception | None = None) -> None:
    headers = {
        **(properties.headers or {}),
        "Mensagem": message,
        "Contexto": context,
        "Error": str(error) if error else None,
    }
    _properties_with_headers(properties, headers)
    ch.basic_publish(
        exchange=settings.nome_anjos_exchange,
        routing_key=settings.nome_da_fila_erros + "-routing-key",
        body=b"",
        properties=properties,
    )


def _output_headers(headers_in: dict[str, Any], result: dict, roi, settings: AppSettings, status: str = "ok") -> dict[str, Any]:
    detection = {
        "detection": bool(result.get("detection", False)),
        "bbox": result.get("bbox", []),
    }
    headers = {
        **headers_in,
        "TrespassingDetection": json.dumps(detection),
        "TrespassingMaxConfidence": str(float(result.get("max_confidence", 0.0))),
        "TrespassingRoiSource": roi.source if roi is not None else "none",
        "TrespassingStatus": status,
    }
    if roi is not None and settings.include_roi_in_output_headers:
        headers["TrespassingROIResolved"] = json.dumps(roi.points)
    return headers


def _publish_output(ch, settings: AppSettings, properties, headers: dict[str, Any], body: bytes) -> None:
    _properties_with_headers(properties, headers)
    ch.basic_publish(
        exchange=settings.nome_anjos_exchange,
        routing_key=settings.nome_da_fila_saida + "-routing-key",
        body=body,
        properties=properties,
    )


def _update_metrics(metrics: MetricsState, settings: AppSettings, elapsed: float, *, detected: bool, missing_roi: bool = False) -> None:
    metrics.count_msgs += 1
    metrics.count_detections += int(detected)
    metrics.count_missing_roi += int(missing_roi)
    metrics.process_times.append(elapsed)

    window_seconds = max(settings.janela_metricas, 1) * 60
    now = time.time()
    if now - metrics.window_start < window_seconds:
        return

    avg = sum(metrics.process_times) / len(metrics.process_times) if metrics.process_times else 0.0
    logger.debug(
        "METRICAS {} MIN | msgs={} | detections={} | missing_roi={} | avg_process_s={:.4f}",
        settings.janela_metricas,
        metrics.count_msgs,
        metrics.count_detections,
        metrics.count_missing_roi,
        avg,
    )
    metrics.count_msgs = 0
    metrics.count_detections = 0
    metrics.count_missing_roi = 0
    metrics.process_times.clear()
    metrics.window_start = now


def make_callback(context: RuntimeContext) -> Callable:
    settings = context.settings

    def callback(ch, method, properties, body):
        start = time.time()
        headers_in = dict(properties.headers or {})
        camera_id = _camera_id(headers_in)
        angel_id = _angel_id(headers_in)
        identifier = camera_identifier(camera_id, angel_id)

        try:
            image = decode_image_bytes(body)
            roi = context.roi_resolver.resolve(
                camera_id,
                headers_in,
                image.shape,
                angel_id=angel_id,
            )
            missing_roi = roi.source == "full_image_missing_roi"
            if missing_roi:
                logger.warning(
                    "ROI nao encontrada no camera_rois.json | identifier={} | "
                    "AngelId={} | CameraId={} | processando imagem inteira",
                    identifier,
                    angel_id,
                    camera_id,
                )

            result = context.detector.predict(image, roi.points)

            status = "ok_full_image_missing_roi" if missing_roi else "ok"
            headers_out = _output_headers(headers_in, result, roi, settings, status=status)
            body_out = body if result["detection"] else b""
            _publish_output(ch, settings, properties, headers_out, body_out)
            ch.basic_ack(delivery_tag=method.delivery_tag)

            _update_metrics(
                context.metrics,
                settings,
                time.time() - start,
                detected=bool(result["detection"]),
                missing_roi=missing_roi,
            )

            logger.debug(
                "Mensagem processada | identifier={} | AngelId={} | CameraId={} | "
                "detection={} | roi_source={} | bbox_count={}",
                identifier,
                angel_id,
                camera_id,
                result["detection"],
                roi.source,
                len(result["bbox"]),
            )

        except MissingROIError as exc:
            if settings.missing_roi_policy == "skip":
                result = {"detection": False, "bbox": [], "max_confidence": 0.0}
                headers_out = _output_headers(headers_in, result, None, settings, status="skipped_missing_roi")
                _publish_output(ch, settings, properties, headers_out, b"")
                ch.basic_ack(delivery_tag=method.delivery_tag)
                logger.warning("Mensagem sem ROI ignorada | identifier={} | CameraId={} | {}", identifier, camera_id, exc)
            else:
                _publish_error(
                    ch,
                    settings,
                    properties,
                    message="ROI de trespassing não encontrada para a câmera",
                    context=f"identifier={identifier}; AngelId={angel_id}; CameraId={camera_id}",
                    error=exc,
                )
                ch.basic_ack(delivery_tag=method.delivery_tag)
                logger.error("ROI ausente | identifier={} | CameraId={} | {}", identifier, camera_id, exc)

            _update_metrics(
                context.metrics,
                settings,
                time.time() - start,
                detected=False,
                missing_roi=True,
            )

        except (ROIError, ValueError) as exc:
            _publish_error(
                ch,
                settings,
                properties,
                message="ROI ou imagem inválida no trespassing",
                context=f"CameraId={camera_id}",
                error=exc,
            )
            ch.basic_ack(delivery_tag=method.delivery_tag)
            logger.exception("Erro de entrada | CameraId={}", camera_id)

        except Exception as exc:
            _publish_error(
                ch,
                settings,
                properties,
                message="Erro ao executar o modelo de trespassing",
                context=f"CameraId={camera_id}",
                error=exc,
            )
            ch.basic_ack(delivery_tag=method.delivery_tag)
            logger.exception("Erro no processamento | CameraId={}", camera_id)

    return callback


def setup_rabbitmq(settings: AppSettings):
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


def run_consumer(context: RuntimeContext) -> None:
    connection = None
    try:
        connection, channel = setup_rabbitmq(context.settings)
        channel.basic_consume(
            queue=context.settings.nome_da_fila_entrada_trespassing,
            auto_ack=False,
            on_message_callback=make_callback(context),
        )
        logger.info(
            "Trespassing Rabbit consumer iniciado | env={} | queue={} | model={} | roi_registry={}",
            context.settings.ativar,
            context.settings.nome_da_fila_entrada_trespassing,
            context.settings.model_path,
            context.settings.roi_registry_path,
        )
        channel.start_consuming()
    finally:
        if connection is not None and connection.is_open:
            connection.close()


def main() -> None:
    settings = load_settings()
    context = build_context(settings, require_rabbit=True)
    run_consumer(context)


if __name__ == "__main__":
    main()
