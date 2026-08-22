from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from ultralytics import YOLO


class OclusionDetector:
    """Mesmo classificador de oclusão usado no smokefire-l40."""

    def __init__(self, model_path: str | Path):
        self.model_path = str(model_path)
        self.model = YOLO(self.model_path)

    def predict(self, image: np.ndarray) -> tuple[int, str, float]:
        if image is None or image.size == 0:
            raise ValueError("Imagem vazia para o detector de oclusão.")

        result: Any = self.model.predict(image, verbose=False)[0]
        if result.probs is None:
            raise RuntimeError("O modelo de oclusão não retornou probabilidades de classificação.")

        class_id = int(result.probs.top1)
        confidence = float(result.probs.top1conf)
        class_name = str(self.model.names[class_id])
        return class_id, class_name, confidence
