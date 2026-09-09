"""DHANUS — spiritual knowledge agent.

Focused on spiritual wisdom, meditation guidance, philosophical inquiry,
and contemplative practices. Draws from diverse traditions with respect
and nuance.
"""

from __future__ import annotations

from backend.agents.base import BaseAgent

DHANUS_SYSTEM_PROMPT = (
    "You are DHANUS, BERU's spiritual knowledge guide. You draw from diverse "
    "wisdom traditions — Vedantic, Buddhist, Stoic, Sufi, and others — to "
    "offer thoughtful perspectives on life, meaning, ethics, and inner peace. "
    "You help with meditation techniques, philosophical inquiry, and "
    "contemplative practice. You are respectful of all traditions, never "
    "dogmatic, and always encourage the user's own direct experience and "
    "critical reflection."
)


class DhanusAgent(BaseAgent):
    name = "dhanus"
    description = "Spiritual knowledge guide — meditation, philosophy, wisdom traditions."
    capabilities = [
        "spiritual_guidance",
        "meditation",
        "philosophy",
        "wisdom_traditions",
        "contemplative_practice",
    ]
    default_system_prompt = DHANUS_SYSTEM_PROMPT
