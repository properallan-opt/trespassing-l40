from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np
from shapely.geometry import Point, Polygon
from ultralytics import YOLO


class TrespassingDetector:
    """YOLO person detector + polygon trespassing rule."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        image_size: int = 320,
        confidence_threshold: float = 0.25,
        iou_threshold: float = 0.70,
        device: str = "cpu",
        person_class_id: int = 0,
        verbose: bool = False,
    ):
        self.model_path = str(model_path)
        self.image_size = image_size
        self.confidence_threshold = confidence_threshold
        self.iou_threshold = iou_threshold
        self.device = device
        self.person_class_id = person_class_id
        self.verbose = verbose
        self.model = YOLO(self.model_path, task="detect")

    @staticmethod
    def _polygon(points: Sequence[Sequence[float]]) -> Polygon:
        if not isinstance(points, (list, tuple)) or len(points) < 3:
            raise ValueError("perimeter_polygon must contain at least three [x, y] points.")

        polygon = Polygon([(float(point[0]), float(point[1])) for point in points])
        if not polygon.is_valid or polygon.area == 0:
            raise ValueError("perimeter_polygon must form a valid non-degenerate polygon.")
        return polygon

    def predict(self, image: np.ndarray, perimeter_polygon: Sequence[Sequence[float]]) -> dict:
        polygon = self._polygon(perimeter_polygon)

        result = self.model.predict(
            source=image,
            imgsz=self.image_size,
            conf=self.confidence_threshold,
            iou=self.iou_threshold,
            device=self.device,
            classes=[self.person_class_id],
            verbose=self.verbose,
        )[0]

        boxes: list[list[float | str]] = []
        max_confidence = 0.0

        if result.boxes is not None:
            for xyxy, confidence, class_id in zip(
                result.boxes.xyxy.cpu().tolist(),
                result.boxes.conf.cpu().tolist(),
                result.boxes.cls.cpu().tolist(),
            ):
                x1, y1, x2, y2 = map(float, xyxy)
                confidence = float(confidence)
                max_confidence = max(max_confidence, confidence)

                # Mantém exatamente o critério do trespassing-bento original:
                # o ponto inferior-central da bbox precisa estar estritamente dentro da ROI.
                feet_center = ((x1 + x2) / 2.0, y2)
                if not polygon.contains(Point(feet_center)):
                    continue

                boxes.append(
                    [
                        x1,
                        y1,
                        x2,
                        y2,
                        confidence,
                        result.names[int(class_id)],
                    ]
                )

        return {
            "detection": bool(boxes),
            "bbox": boxes,
            "max_confidence": max_confidence,
        }
