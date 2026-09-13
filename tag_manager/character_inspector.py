from __future__ import annotations

import re
from pathlib import Path

from .gallery import find_checkpoints_and_loras, find_text_inputs, parse_json, read_image_metadata

LORA_RE = re.compile(r"<lora:([^:>]+)(?::([0-9.]+))?>", re.I)

def inspect_image(path: Path) -> dict:
    meta = read_image_metadata(path)
    raw = meta.get("parameters", "")
    positive, negative = raw, ""
    match = re.search(r"\nNegative prompt:\s*(.*?)(?:\nSteps:|\Z)", raw, re.S | re.I)
    if match:
        positive = raw[:match.start()].strip()
        negative = match.group(1).strip()
    workflow = parse_json(meta.get("prompt", ""))
    if isinstance(workflow, dict):
        def trace(node_id: str, seen: set[str] | None = None) -> str:
            seen = seen or set()
            if node_id in seen or node_id not in workflow: return ""
            seen.add(node_id); node = workflow[node_id]; inputs = node.get("inputs", {})
            value = inputs.get("text_0")
            if isinstance(value, str) and value.strip(): return value.strip()
            value = inputs.get("text")
            if isinstance(value, str) and value.strip(): return value.strip()
            if isinstance(value, list) and value: return trace(str(value[0]), seen)
            return ""
        texts = []
        positive_text = negative_text = ""
        for node_id, node in workflow.items():
            if "CLIPTextEncode" in node.get("class_type", ""):
                text = trace(node_id)
                title = str(node.get("_meta", {}).get("title", ""))
                if "负" in title: negative_text = text
                elif text: positive_text = text
                if text: texts.append(text)
        if not texts: texts = find_text_inputs(workflow)
        for node in workflow.values():
            if "KSampler" in node.get("class_type", ""):
                inputs = node.get("inputs", {})
                if isinstance(inputs.get("positive"), list): positive_text = trace(str(inputs["positive"][0])) or positive_text
                if isinstance(inputs.get("negative"), list): negative_text = trace(str(inputs["negative"][0])) or negative_text
        if texts:
            positive = positive_text or texts[0]
            negative = negative_text or (texts[1] if len(texts) > 1 else negative)
    lora = LORA_RE.search(raw)
    if isinstance(workflow, dict):
        _, loras = find_checkpoints_and_loras(workflow)
        if loras:
            match = re.search(r"<lora:([^:>]+)(?::([0-9.]+))?>", raw)
            item = loras[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            name, _, weight = item.rpartition(":")
            lora_name = name.rsplit(".safetensors", 1)[0]
            lora = match or True
            lora_result = (lora_name, float(weight or 1.0))
        else:
            lora_result = (lora.group(1), float(lora.group(2) or 1.0)) if lora else ("", 1.0)
    else:
        lora_result = (lora.group(1), float(lora.group(2) or 1.0)) if lora else ("", 1.0)
    return {"name": path.stem, "positive_prompt": positive, "negative_prompt": negative, "lora": lora_result[0], "lora_weight": lora_result[1], "metadata": meta.get("_metadata_json", ""), "source": meta.get("_metadata_source", "")}
