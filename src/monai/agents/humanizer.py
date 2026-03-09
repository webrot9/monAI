"""Humanizer agent — makes all content indistinguishable from expert human work.

Not about "tricking" detectors. It's about producing genuinely better content:
- Match client/platform voice and tone
- Vary sentence structure (break AI-typical patterns)
- Inject specificity, opinions, natural imperfections
- Self-critique loop: draft → analyze → rewrite
- Maintain style profiles per client/platform
- Track quality scores over time
"""

from __future__ import annotations

import json
import logging
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

HUMANIZER_SCHEMA = """
CREATE TABLE IF NOT EXISTS style_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,              -- client name, platform, or "default"
    voice_description TEXT NOT NULL,        -- natural language description of the voice
    sample_phrases TEXT,                    -- JSON list of example phrases/patterns
    avoid_patterns TEXT,                    -- JSON list of patterns to avoid
    formality_level TEXT DEFAULT 'neutral', -- casual, neutral, formal, academic
    personality_traits TEXT,               -- JSON list of personality traits
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS content_quality (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL,
    original_score REAL,                   -- pre-humanization quality score (0-1)
    final_score REAL,                      -- post-humanization quality score (0-1)
    rewrites INTEGER DEFAULT 0,            -- number of rewrite passes
    style_profile TEXT,                    -- which profile was used
    issues_found TEXT,                     -- JSON list of AI-tell issues found
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class Humanizer(BaseAgent):
    name = "humanizer"
    description = (
        "Content quality agent. Post-processes all outbound content to ensure "
        "it's indistinguishable from expert human work. Matches client voice, "
        "varies style, adds specificity, and eliminates AI-typical patterns."
    )

    # Common AI writing patterns to detect and fix
    AI_TELLS = [
        "certainly", "moreover", "furthermore", "it's important to note",
        "in conclusion", "in today's world", "in the realm of",
        "it's worth noting", "delve into", "let's explore",
        "crucial", "comprehensive", "robust", "leverage",
        "streamline", "utilize", "facilitate", "endeavor",
        "harness the power", "game-changer", "deep dive",
        "cutting-edge", "innovative solution", "seamlessly",
    ]

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        with db.connect() as conn:
            conn.executescript(HUMANIZER_SCHEMA)

    def plan(self) -> list[str]:
        return ["review_quality"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "ready"}

    def humanize(self, content: str, style_profile: str = "default",
                 context: str = "") -> str:
        """Main entry point: take content and make it human-quality.

        Args:
            content: The raw AI-generated content
            style_profile: Name of the style profile to match
            context: Additional context (audience, purpose, etc.)

        Returns:
            Humanized content
        """
        # Step 1: Analyze the content for AI tells
        analysis = self._analyze_ai_tells(content)

        # Step 2: Get the style profile
        profile = self._get_or_create_profile(style_profile)

        # Step 3: Rewrite with style matching
        rewritten = self._rewrite(content, profile, analysis, context)

        # Step 4: Self-critique and refine
        final = self._self_critique(rewritten, profile, context)

        # Step 5: Track quality
        self._record_quality(content, final, style_profile, analysis)

        return final

    def _analyze_ai_tells(self, content: str) -> dict[str, Any]:
        """Detect AI-typical patterns in the content."""
        content_lower = content.lower()
        found_tells = [tell for tell in self.AI_TELLS if tell in content_lower]

        # Check structural patterns
        sentences = content.split(".")
        lengths = [len(s.split()) for s in sentences if s.strip()]
        avg_len = sum(lengths) / max(len(lengths), 1)

        # AI tends toward uniform sentence length
        if lengths:
            variance = sum((l - avg_len) ** 2 for l in lengths) / len(lengths)
        else:
            variance = 0

        return {
            "ai_tells_found": found_tells,
            "ai_tell_count": len(found_tells),
            "avg_sentence_length": avg_len,
            "sentence_length_variance": variance,
            "low_variance": variance < 15,  # AI typical: low variance
            "sentence_count": len(lengths),
        }

    def _get_or_create_profile(self, name: str) -> dict[str, Any]:
        """Get a style profile, creating a default if none exists."""
        rows = self.db.execute(
            "SELECT * FROM style_profiles WHERE name = ?", (name,)
        )
        if rows:
            return dict(rows[0])

        # Create default profile
        default_profile = {
            "name": name,
            "voice_description": (
                "Professional but approachable. Uses concrete examples. "
                "Varies sentence length naturally. Occasionally uses contractions. "
                "Has opinions and isn't afraid to state them. Writes like a human "
                "expert who cares about their craft."
            ),
            "sample_phrases": json.dumps([]),
            "avoid_patterns": json.dumps(self.AI_TELLS),
            "formality_level": "neutral",
            "personality_traits": json.dumps([
                "direct", "knowledgeable", "slightly opinionated", "practical"
            ]),
        }
        self.db.execute_insert(
            "INSERT INTO style_profiles (name, voice_description, sample_phrases, "
            "avoid_patterns, formality_level, personality_traits) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, default_profile["voice_description"],
             default_profile["sample_phrases"], default_profile["avoid_patterns"],
             default_profile["formality_level"], default_profile["personality_traits"]),
        )
        return default_profile

    def create_profile(self, name: str, voice_description: str,
                       sample_text: str = "", formality: str = "neutral",
                       traits: list[str] | None = None) -> dict[str, Any]:
        """Create a new style profile from a description and sample text."""
        # Extract patterns from sample text if provided
        sample_phrases: list[str] = []
        if sample_text:
            extracted = self.think_json(
                f"Analyze this writing sample and extract the key style elements.\n\n"
                f"Sample:\n{sample_text[:2000]}\n\n"
                "Return: {\"characteristic_phrases\": [str], \"tone\": str, "
                "\"sentence_patterns\": [str], \"vocabulary_preferences\": [str]}"
            )
            sample_phrases = extracted.get("characteristic_phrases", [])

        profile = {
            "name": name,
            "voice_description": voice_description,
            "sample_phrases": json.dumps(sample_phrases),
            "avoid_patterns": json.dumps(self.AI_TELLS),
            "formality_level": formality,
            "personality_traits": json.dumps(traits or []),
        }

        self.db.execute_insert(
            "INSERT OR REPLACE INTO style_profiles "
            "(name, voice_description, sample_phrases, avoid_patterns, "
            "formality_level, personality_traits) VALUES (?, ?, ?, ?, ?, ?)",
            (name, voice_description, profile["sample_phrases"],
             profile["avoid_patterns"], formality, json.dumps(traits or [])),
        )
        self.log_action("profile_created", name, voice_description[:200])
        return profile

    def _rewrite(self, content: str, profile: dict[str, Any],
                 analysis: dict[str, Any], context: str) -> str:
        """Rewrite content to match the style profile and fix AI tells."""
        tells_to_fix = analysis.get("ai_tells_found", [])
        low_variance = analysis.get("low_variance", False)

        rewrite_instructions = (
            f"Rewrite this content to sound like a real human expert wrote it.\n\n"
            f"VOICE: {profile.get('voice_description', '')}\n"
            f"FORMALITY: {profile.get('formality_level', 'neutral')}\n"
            f"PERSONALITY: {profile.get('personality_traits', '[]')}\n"
        )

        if context:
            rewrite_instructions += f"CONTEXT: {context}\n"

        if tells_to_fix:
            rewrite_instructions += (
                f"\nREMOVE these AI-typical words/phrases (replace with natural alternatives):\n"
                f"{', '.join(tells_to_fix)}\n"
            )

        if low_variance:
            rewrite_instructions += (
                "\nVARY SENTENCE LENGTH: Mix short punchy sentences with longer ones. "
                "Current writing has too-uniform sentence lengths (a dead giveaway).\n"
            )

        rewrite_instructions += (
            "\nRULES:\n"
            "- Keep the same information and meaning\n"
            "- Add specific details where vague\n"
            "- Use natural transitions, not formulaic ones\n"
            "- Include occasional contractions\n"
            "- Don't start every paragraph the same way\n"
            "- Have a point of view — don't hedge everything\n"
            "- Write like you're explaining to a smart colleague\n"
            "- NO filler phrases, NO empty superlatives\n"
            "\nReturn ONLY the rewritten content, nothing else."
        )

        rewritten = self.llm.chat(
            [
                {"role": "system", "content": (
                    "You are a professional editor who makes AI-generated text "
                    "sound natural and human. You preserve meaning while improving "
                    "voice, style, and readability. You never add fluff or filler."
                )},
                {"role": "user", "content": f"{rewrite_instructions}\n\nCONTENT:\n{content}"},
            ],
            model=self.config.llm.model,
            temperature=0.8,  # Higher temp for more creative variation
        )

        return rewritten

    def _self_critique(self, content: str, profile: dict[str, Any],
                       context: str) -> str:
        """Self-critique the rewritten content and refine if needed."""
        critique = self.think_json(
            "Critique this content for quality and naturalness.\n\n"
            f"Content:\n{content[:3000]}\n\n"
            "Check:\n"
            "1. Does it sound like a real human wrote it? (score 1-10)\n"
            "2. Are there any remaining AI-typical patterns?\n"
            "3. Is the information specific (not vague)?\n"
            "4. Does sentence length vary naturally?\n"
            "5. Is there a clear voice/personality?\n\n"
            "Return: {\"human_score\": int, \"remaining_issues\": [str], "
            "\"needs_rewrite\": bool, \"specific_fixes\": [str]}"
        )

        if critique.get("needs_rewrite") and critique.get("human_score", 10) < 7:
            # One more pass to fix remaining issues
            fixes = critique.get("specific_fixes", [])
            refined = self.llm.chat(
                [
                    {"role": "system", "content": (
                        "Apply these specific fixes to the content. "
                        "Change only what's listed. Keep everything else."
                    )},
                    {"role": "user", "content": (
                        f"Fixes needed:\n" + "\n".join(f"- {f}" for f in fixes) +
                        f"\n\nContent:\n{content}"
                    )},
                ],
                model=self.config.llm.model_mini,  # Mini is enough for targeted fixes
                temperature=0.5,
            )
            return refined

        return content

    def _record_quality(self, original: str, final: str,
                        style_profile: str, analysis: dict[str, Any]):
        """Record quality metrics for tracking improvement over time."""
        # Simple hash for dedup
        content_hash = str(hash(original[:500]))

        self.db.execute_insert(
            "INSERT INTO content_quality (content_hash, original_score, final_score, "
            "rewrites, style_profile, issues_found) VALUES (?, ?, ?, ?, ?, ?)",
            (content_hash,
             max(0, 1 - analysis.get("ai_tell_count", 0) * 0.1),
             0.9,  # Will be updated by external quality checks
             1,
             style_profile,
             json.dumps(analysis.get("ai_tells_found", []))),
        )

    def get_quality_stats(self) -> dict[str, Any]:
        """Get aggregate quality statistics."""
        rows = self.db.execute(
            "SELECT COUNT(*) as total, AVG(original_score) as avg_original, "
            "AVG(final_score) as avg_final, AVG(rewrites) as avg_rewrites "
            "FROM content_quality"
        )
        if rows:
            return dict(rows[0])
        return {"total": 0}
