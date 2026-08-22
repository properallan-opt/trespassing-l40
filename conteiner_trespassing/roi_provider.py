from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class ROIError(ValueError):
    """Base error for invalid or unavailable ROI data."""


class MissingROIError(ROIError):
    """Raised when a camera has no ROI available from any enabled source."""


@dataclass(frozen=True)
class ResolvedROI:
    points: list[list[float]]
    source: str
    coordinate_space: str = "pixels"


class CameraROIRegistry:
    """
    JSON-backed camera -> ROI registry with automatic reload when the file changes.

    Supported root shapes:
      {"cameras": {"CAM01": {...}}}
      {"CAM01": {...}}
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._mtime_ns: int | None = None
        self._data: dict[str, Any] = {}

    def _reload_if_needed(self) -> None:
        if not self.path.exists():
            self._data = {}
            self._mtime_ns = None
            return

        mtime_ns = self.path.stat().st_mtime_ns
        if self._mtime_ns == mtime_ns:
            return

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ROIError(f"ROI registry must be a JSON object: {self.path}")

        cameras = raw.get("cameras", raw)
        if not isinstance(cameras, dict):
            raise ROIError("The 'cameras' field in ROI registry must be an object.")

        self._data = {str(key): value for key, value in cameras.items()}
        self._mtime_ns = mtime_ns

    def get(self, camera_id: Any) -> Any | None:
        self._reload_if_needed()
        if camera_id is None:
            return None
        return self._data.get(str(camera_id))


class ROIResolver:
    """
    Resolves the ROI without coupling inference code to its storage/transport.

    Priority:
      1) ROI in Rabbit message metadata/header (future production path)
      2) local JSON registry keyed by CameraId (temporary/test fallback)
    """

    def __init__(
        self,
        registry_path: str | Path,
        *,
        message_enabled: bool = True,
        local_enabled: bool = True,
        message_keys: Iterable[str] = (
            "TrespassingROI",
            "perimeter_polygon",
            "PerimeterPolygon",
            "AreaOfInterest",
        ),
    ):
        self.message_enabled = message_enabled
        self.local_enabled = local_enabled
        self.message_keys = tuple(message_keys)
        self.registry = CameraROIRegistry(registry_path)

    def resolve(
        self,
        camera_id: Any,
        metadata: Mapping[str, Any] | None,
        image_shape: Sequence[int],
    ) -> ResolvedROI:
        height, width = int(image_shape[0]), int(image_shape[1])

        if self.message_enabled and metadata:
            payload, key = self._find_message_roi(metadata)
            if payload is not None:
                points, space = self._parse_roi(payload, width=width, height=height)
                return ResolvedROI(points=points, source=f"message:{key}", coordinate_space=space)

        if self.local_enabled:
            payload = self.registry.get(camera_id)
            if payload is not None:
                points, space = self._parse_roi(payload, width=width, height=height)
                return ResolvedROI(points=points, source="local_json", coordinate_space=space)

        raise MissingROIError(f"No ROI found for CameraId={camera_id!r}")

    def _find_message_roi(self, metadata: Mapping[str, Any]) -> tuple[Any | None, str | None]:
        for key in self.message_keys:
            if key in metadata and metadata[key] not in (None, ""):
                return metadata[key], key

        nested = metadata.get("Trespassing")
        if isinstance(nested, Mapping):
            for key in ("roi", "ROI", "perimeter_polygon", "polygon"):
                if key in nested and nested[key] not in (None, ""):
                    return nested[key], f"Trespassing.{key}"

        return None, None

    @classmethod
    def _parse_roi(cls, payload: Any, *, width: int, height: int) -> tuple[list[list[float]], str]:
        payload = cls._decode_json_if_needed(payload)

        coordinate_space = "pixels"
        points_payload = payload

        if isinstance(payload, Mapping):
            coordinate_space = str(
                payload.get("coordinate_space", payload.get("space", "pixels"))
            ).strip().lower()
            points_payload = (
                payload.get("perimeter_polygon")
                or payload.get("polygon")
                or payload.get("points")
                or payload.get("roi")
            )

            if payload.get("normalized") is True:
                coordinate_space = "normalized"

        points = cls._validate_points(points_payload)

        if coordinate_space in {"normalized", "relative", "0-1", "01"}:
            points = [[x * width, y * height] for x, y in points]
            coordinate_space = "normalized"
        elif coordinate_space not in {"pixels", "pixel", "px"}:
            raise ROIError(f"Unsupported ROI coordinate_space={coordinate_space!r}")
        else:
            coordinate_space = "pixels"

        return points, coordinate_space

    @staticmethod
    def _decode_json_if_needed(payload: Any) -> Any:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            try:
                return json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ROIError("ROI string must contain valid JSON.") from exc
        return payload

    @staticmethod
    def _validate_points(points: Any) -> list[list[float]]:
        if not isinstance(points, (list, tuple)) or len(points) < 3:
            raise ROIError("ROI must contain at least three [x, y] points.")

        normalized: list[list[float]] = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                raise ROIError("Each ROI point must contain [x, y].")
            normalized.append([float(point[0]), float(point[1])])

        return normalized
