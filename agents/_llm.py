"""
Shared Gemini helper for the shadow agent system.
Mirrors the pattern already used in agent/planner.py and actions/code_helper.py
so all agents read the same config/api_keys.json and behave consistently.
"""
import json
import re
import sys
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def generate(prompt: str, model_name: str = "gemini-2.5-flash", system_instruction: str | None = None) -> str:
    """Single-shot text generation."""
    import google.generativeai as genai

    genai.configure(api_key=get_api_key())
    model = genai.GenerativeModel(model_name=model_name, system_instruction=system_instruction)
    response = model.generate_content(prompt)
    return (response.text or "").strip()


def generate_json(prompt: str, model_name: str = "gemini-2.5-flash", system_instruction: str | None = None) -> dict:
    """Generation that expects a JSON object back. Returns {} on failure rather than raising,
    so callers can fall back gracefully."""
    try:
        raw = generate(prompt, model_name=model_name, system_instruction=system_instruction)
    except Exception as e:
        print(f"[Agents] ⚠️ generate_json call failed: {e}")
        return {}

    cleaned = _strip_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        print(f"[Agents] ⚠️ Could not parse JSON from model output: {cleaned[:200]}")
        return {}
