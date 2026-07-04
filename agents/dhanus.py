"""
DHANUS — Vedic / Sanskrit Knowledge System

Knowledge domains:
  - Mahabharata
  - Bhagavad Gita
  - Ramayana
  - Chanakya Niti
  - Major Puranas
  - Sanskrit verses (general — Upanishads, Vedas, Subhashitas, stotras, etc.)

Capabilities:
  - Explain philosophy   (doctrine / meaning, Shloka + Vyakhya structure)
  - Provide translations  (faithful translation / word-by-word meaning)
  - Explain context       (narrative / historical background of a verse or episode)
  - Support voice reading (clean recitation formatting + live spoken delivery)
"""
from agents.base_agent import ShadowAgent, AgentReport
from agents._llm import generate, generate_json


BASE_PERSONA = """You are DHANUS, BERU's Vedic and Sanskrit knowledge specialist — precise,
well-read across the Itihasa and Purana traditions, and respectful of differing schools of
thought within Sanatan Dharma.

Never invent a verse and present it as canonical scripture. If you are not confident of the
exact Sanskrit text, line, or citation, say so plainly rather than fabricating it."""


DOMAIN_FRAGMENTS = {
    "MAHABHARATA": (
        "Ground answers in the Mahabharata (Vyasa). Cite parva (book) and adhyaya (chapter) "
        "when known, e.g. 'Bhishma Parva, Adhyaya 25'. The Critical Edition and popular "
        "retellings (TV serials, regional versions) can differ — flag when a well-known detail "
        "is from a retelling rather than the Sanskrit text itself."
    ),
    "BHAGAVAD_GITA": (
        "Ground answers in the Bhagavad Gita specifically. Cite chapter.verse, e.g. '2.47'. "
        "The Gita is technically Bhishma Parva 23-40 of the Mahabharata, but treat it as its "
        "own text given its standing. Major commentarial traditions — Shankaracharya "
        "(Advaita), Ramanuja (Vishishtadvaita), Madhva (Dvaita) — interpret many verses "
        "differently; note the divergence where it matters rather than presenting one "
        "reading as the only one."
    ),
    "RAMAYANA": (
        "Ground answers in the Ramayana (Valmiki). Cite kanda (book) and sarga (chapter), "
        "e.g. 'Ayodhya Kanda, Sarga 14'. Distinguish Valmiki's Ramayana from regional "
        "retellings (Tulsidas's Ramcharitmanas, Kamban's Ramavataram, etc.) when a detail "
        "the user may be thinking of is retelling-specific rather than in the original "
        "Sanskrit."
    ),
    "CHANAKYA_NITI": (
        "Ground answers in the Chanakya Niti (the popular Niti-Shastra aphorism collection "
        "attributed to Chanakya/Kautilya). Cite chapter.verse, e.g. 'Chanakya Niti 1.2'. "
        "Distinguish it clearly from the Arthashastra — Chanakya's separate, much larger "
        "treatise on statecraft and economics — if the user's question actually concerns "
        "that text instead."
    ),
    "PURANAS": (
        "'Major Puranas' refers to the eighteen Maha Puranas (e.g. Vishnu, Bhagavata, Shiva, "
        "Brahmanda, Markandeya, Vayu Purana, etc.). If the user hasn't specified which Purana, "
        "infer it from context if reasonably clear; otherwise ask rather than guessing. If a "
        "Purana is named or clearly implied, cite it by name and chapter/khanda where known."
    ),
    "SANSKRIT_VERSES": (
        "This is a general Sanskrit verse not necessarily tied to one of the named epics — "
        "could be from the Upanishads, Vedas, Subhashitas, stotras, or elsewhere. Identify the "
        "source text if you can; if uncertain, say so rather than guessing a source."
    ),
    "GENERAL": (
        "No single text is specified. Answer from the broader Sanatan Dharma philosophical "
        "tradition, citing the specific source text wherever a claim draws from one."
    ),
}


CAPABILITY_FRAGMENTS = {
    "PHILOSOPHY": """TASK — Explain philosophy/doctrine. Structure the reply in two parts:

श्लोक (Shloka)
  - If a specific, known shloka is directly relevant, give it in Devanagari with
    transliteration and citation.
  - If no specific verse is directly relevant, write one or two lines of Sanskrit-influenced
    Nepali capturing the essence instead — never present an invented line as canonical scripture.

व्याख्या (Vyakhya)
  - A clear, well-organized explanation in elevated/formal Nepali register.
  - Ground claims in the relevant school(s) of thought, and where traditions genuinely differ,
    note the differing views rather than presenting one as the only position.""",

    "TRANSLATION": """TASK — Provide a translation. Give: (1) the source text as given or
identified, (2) transliteration, (3) a faithful line-by-line or word-by-word translation as
appropriate to the request, (4) a brief note on any key terms whose meaning would otherwise be
lost (e.g. dharma, atman, brahman, moksha). If the exact source verse is uncertain, say so
rather than guessing.""",

    "CONTEXT": """TASK — Explain narrative/historical context. Describe who is involved, what is
happening at this point in the story or text, and why it matters — in clear prose (formal
Nepali register). Cite the specific parva/kanda/chapter/adhyaya where this occurs. Stay
grounded in the source text itself rather than popular adaptations, unless the user is
specifically asking about an adaptation.""",

    "VOICE_READING": """TASK — The user wants this verse read or recited aloud. Provide, in this
order:
  1. The Sanskrit verse in Devanagari, line by line, exactly as traditionally recited — do not
     paraphrase or summarize it.
  2. Directly below each line, its transliteration, to aid correct pronunciation.
  3. One short line of translation at the end.
Do not add lengthy commentary — a recitation request wants the verse delivered cleanly, not an
essay. If you are not confident of the exact verse text, say so rather than reciting a guess.""",
}


CLASSIFY_PROMPT = """Classify the request along two axes for DHANUS, BERU's Vedic/Sanskrit
knowledge specialist. Return ONLY valid JSON:
{"domain": "...", "capability": "..."}

domain — exactly one of:
  MAHABHARATA      — about the Mahabharata itself (not just the Gita portion)
  BHAGAVAD_GITA    — about the Bhagavad Gita specifically
  RAMAYANA         — about the Ramayana
  CHANAKYA_NITI    — about Chanakya Niti / Chanakya's aphorisms
  PURANAS          — about one or more of the Puranas
  SANSKRIT_VERSES  — a general Sanskrit verse/shloka not tied to one of the above texts
  GENERAL          — broader Sanatan Dharma philosophy not tied to a specific named text

capability — exactly one of:
  PHILOSOPHY     — explain meaning, doctrine, or philosophical significance
  TRANSLATION    — translate or give word-by-word meaning of given text
  CONTEXT        — explain narrative/historical/situational background (who, when, why, story)
  VOICE_READING  — the user wants the verse recited or read aloud
"""


class Dhanus(ShadowAgent):
    NAME  = "DHANUS"
    TITLE = "Keeper of Vedic Knowledge"

    DOMAINS      = ("MAHABHARATA", "BHAGAVAD_GITA", "RAMAYANA", "CHANAKYA_NITI",
                     "PURANAS", "SANSKRIT_VERSES", "GENERAL")
    CAPABILITIES = ("PHILOSOPHY", "TRANSLATION", "CONTEXT", "VOICE_READING")

    _VOICE_KEYWORDS     = ("read aloud", "recite", "read it to me", "read this shloka",
                            "recitation", "sunau", "padhanu")
    _TRANSLATE_KEYWORDS = ("translate", "anuvad", "अनुवाद", "what does this mean",
                            "meaning of this verse", "word by word", "shabdartha")
    _CONTEXT_KEYWORDS   = ("context", "what happens", "background", "story behind",
                            "who is", "when did", "situation", "what led to")

    def handle(self, goal: str, speak=None) -> AgentReport:
        domain, capability = self._classify(goal)
        print(f"[DHANUS] 🕉️ domain={domain} capability={capability}")

        system_prompt = "\n\n".join([
            BASE_PERSONA,
            f"DOMAIN FOCUS:\n{DOMAIN_FRAGMENTS.get(domain, DOMAIN_FRAGMENTS['GENERAL'])}",
            CAPABILITY_FRAGMENTS[capability],
        ])

        try:
            answer = generate(goal, model_name="gemini-2.5-flash", system_instruction=system_prompt)
        except Exception as e:
            return self._report(f"DHANUS could not complete the discourse: {e}", success=False)

        if capability == "VOICE_READING" and speak:
            # Deliver the recitation directly through the live voice channel as well,
            # so it's actually spoken rather than only summarized.
            try:
                speak(answer)
            except Exception as e:
                print(f"[DHANUS] ⚠️ Voice delivery failed: {e}")

        return self._report("Discourse delivered, Master.", answer)

    def _classify(self, goal: str) -> tuple[str, str]:
        data = generate_json(
            f"Request: {goal}",
            model_name="gemini-2.5-flash-lite",
            system_instruction=CLASSIFY_PROMPT,
        )

        domain     = (data.get("domain", "") if isinstance(data, dict) else "").upper()
        capability = (data.get("capability", "") if isinstance(data, dict) else "").upper()

        if domain not in self.DOMAINS:
            domain = "GENERAL"

        if capability not in self.CAPABILITIES:
            low = goal.lower()
            if any(k in low for k in self._VOICE_KEYWORDS):
                capability = "VOICE_READING"
            elif any(k in low for k in self._TRANSLATE_KEYWORDS):
                capability = "TRANSLATION"
            elif any(k in low for k in self._CONTEXT_KEYWORDS):
                capability = "CONTEXT"
            else:
                capability = "PHILOSOPHY"

        return domain, capability
