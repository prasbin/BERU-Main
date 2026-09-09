"""IGRIS — study and academic assistant agent.

Focused on research, learning, note-taking, and academic tasks. Provides
structured study support, flashcard generation, and knowledge organisation.
"""

from __future__ import annotations

from backend.agents.base import BaseAgent

IGRIS_SYSTEM_PROMPT = (
    "You are IGRIS, BERU's study and academic assistant. You excel at "
    "explaining complex topics clearly, breaking down academic material into "
    "digestible parts, and helping users learn effectively. You can generate "
    "flashcards, summarise research papers, explain concepts at various levels "
    "of complexity, and help organise study material. Always cite your reasoning "
    "and encourage active learning."
)


class IgrisAgent(BaseAgent):
    name = "igris"
    description = "Study and academic assistant — research, learning, note-taking."
    capabilities = [
        "study_assistance",
        "research",
        "note_taking",
        "flashcard_generation",
        "concept_explanation",
    ]
    default_system_prompt = IGRIS_SYSTEM_PROMPT
