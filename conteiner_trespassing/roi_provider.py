from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class ROIError(ValueError):
    """Base error for invalid or unavailable ROI data."""


class MissingROIError(ROIError):
    """Legacy error kept for compatibility with older callers/policies."""


def _normalize_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = str(value).strip()
    return value or None


def camera_identifier(camera_id: Any, angel_id: Any = None) -> str | None:
    """
    Build the canonical camera identifier used by trespassing and billing.

    - AngelId + CameraId -> "<AngelId>:<CameraId>"
    - CameraId only      -> "<CameraId>"
    """
    camera = _normalize_id(camera_id)
    angel = _normalize_id(angel_id)
    if camera is None:
        return None
    return f"{angel}:{camera}" if angel is not None else camera


@dataclass(frozen=True)
class ResolvedROI:
    points: list[list[float]]
    source: str
    coordinate_space: str = "pixels"
    identifier: str | None = None


class CameraROIRegistry:
    """
    JSON-backed camera -> ROI registry with automatic reload when the file changes.

    Supported root shapes:
      {"cameras": {"18:31723": {...}, "31723": {...}}}
      {"18:31723": {...}, "31723": {...}}

    Lookup rule:
      - when AngelId and CameraId are available, first try "AngelId:CameraId";
      - for backward compatibility, also try the legacy CameraId-only key;
      - when only CameraId is available, first try "CameraId";
      - if that key is absent, a single unambiguous "*:CameraId" entry is accepted.
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

    @staticmethod
    def candidate_keys(camera_id: Any, angel_id: Any = None) -> list[str]:
        camera = _normalize_id(camera_id)
        angel = _normalize_id(angel_id)
        if camera is None:
            return []

        keys: list[str] = []
        if angel is not None:
            keys.append(f"{angel}:{camera}")
        # Compatibility with old camera_rois.json files keyed only by CameraId.
        if camera not in keys:
            keys.append(camera)
        return keys

    def get_with_key(self, camera_id: Any, angel_id: Any = None) -> tuple[Any | None, str | None]:
        self._reload_if_needed()
        camera = _normalize_id(camera_id)
        angel = _normalize_id(angel_id)

        for key in self.candidate_keys(camera, angel):
            if key in self._data:
                return self._data[key], key

        # Compatibility with a registry already migrated to composite keys when
        # an older producer still sends CameraId only. This fallback is used
        # only when the CameraId maps to exactly one composite key; if there are
        # multiple angles for the same CameraId, selecting one would be unsafe.
        if camera is not None and angel is None:
            suffix = f":{camera}"
            matches = [key for key in self._data if key.endswith(suffix)]
            if len(matches) == 1:
                matched_key = matches[0]
                return self._data[matched_key], matched_key

        return None, None

    def get(self, camera_id: Any, angel_id: Any = None) -> Any | None:
        payload, _ = self.get_with_key(camera_id, angel_id)
        return payload


class ROIResolver:
    """
    Resolves the ROI without coupling inference code to its storage/transport.

    Priority:
      1) ROI in Rabbit message metadata/header
      2) local JSON registry using the canonical camera identifier
      3) full image when no ROI is registered
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
        *,
        angel_id: Any = None,
    ) -> ResolvedROI:
        height, width = int(image_shape[0]), int(image_shape[1])
        identifier = camera_identifier(camera_id, angel_id)

        if self.message_enabled and metadata:
            payload, key = self._find_message_roi(metadata)
            if payload is not None:
                points, space = self._parse_roi(payload, width=width, height=height)
                return ResolvedROI(
                    points=points,
                    source=f"message:{key}",
                    coordinate_space=space,
                    identifier=identifier,
                )

        if self.local_enabled:
            payload, matched_key = self.registry.get_with_key(camera_id, angel_id)
            if payload is not None:
                points, space = self._parse_roi(payload, width=width, height=height)
                return ResolvedROI(
                    points=points,
                    source="local_json",
                    coordinate_space=space,
                    identifier=identifier,
                )

        # No registered ROI: process the whole image. The polygon is expanded
        # slightly beyond the image because the detector intentionally uses
        # Polygon.contains(), which excludes points exactly on the boundary.
        points = [
            [-1.0, -1.0],
            [float(width) + 1.0, -1.0],
            [float(width) + 1.0, float(height) + 1.0],
            [-1.0, float(height) + 1.0],
        ]
        return ResolvedROI(
            points=points,
            source="full_image_missing_roi",
            coordinate_space="pixels",
            identifier=identifier,
        )

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
