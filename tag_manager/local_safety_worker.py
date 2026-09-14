from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image


EXPLICIT = {"nude", "completely nude", "nipples", "pussy", "penis", "vagina", "anus", "sex", "genitals"}
SUSPICIOUS = {"bikini", "swimsuit", "underwear", "panties", "bra", "lingerie", "cleavage", "sideboob", "underboob", "suggestive"}


def level_from_tags(tags: set[str], ratings: dict[str, float]) -> str:
    if ratings.get("explicit", 0.0) >= 0.35 or tags & EXPLICIT:
        return "nsfw"
    if ratings.get("sensitive", 0.0) >= 0.35 or ratings.get("questionable", 0.0) >= 0.35 or tags & SUSPICIOUS:
        return "suspicious"
    return "normal"


def main() -> None:
    payload = json.load(sys.stdin)
    paths = [Path(path) for path in payload.get("paths", [])]
    model_dir = Path(payload["model_dir"])
    model_name = payload.get("model_name", "wd-swinv2-tagger-v3")
    model_path = model_dir / f"{model_name}.onnx"
    csv_path = model_dir / f"{model_name}.csv"
    providers = [provider for provider in ("CUDAExecutionProvider", "CPUExecutionProvider") if provider in ort.get_available_providers()]
    session = ort.InferenceSession(model_path, providers=providers)
    input_info = session.get_inputs()[0]
    height = int(input_info.shape[1])

    tags: list[str] = []
    categories: list[str] = []
    with csv_path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            tags.append(row["name"].replace("_", " "))
            categories.append(row["category"])

    result: dict[str, dict[str, object]] = {}
    for path in paths:
        try:
            with Image.open(path) as source:
                image = source.convert("RGB")
            ratio = height / max(image.size)
            resized = image.resize(tuple(int(side * ratio) for side in image.size), Image.Resampling.LANCZOS)
            square = Image.new("RGB", (height, height), "white")
            square.paste(resized, ((height - resized.width) // 2, (height - resized.height) // 2))
            array = np.expand_dims(np.asarray(square, dtype=np.float32)[:, :, ::-1], 0)
            output = session.run(None, {input_info.name: array})[0][0]
            ratings = {tags[index]: float(score) for index, score in enumerate(output) if categories[index] == "9"}
            matched = {tags[index] for index, score in enumerate(output) if categories[index] == "0" and score >= 0.35}
            level = level_from_tags(matched, ratings)
            evidence = sorted((matched & EXPLICIT) | (matched & SUSPICIOUS))
            result[str(path)] = {"level": level, "evidence": evidence, "ratings": ratings}
        except Exception as exc:
            result[str(path)] = {"error": str(exc)}
    json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
