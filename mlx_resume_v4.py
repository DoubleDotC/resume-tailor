"""
mlx_resume_v4.py — 4-Pass Local LLM Resume Tailoring
======================================================
Pipeline:
  Pass 1 · Analyzer    — Dissects the JD into ATS intelligence + company signals.
  Pass 2 · Writer      — Rewrites the resume with keyword injection and dynamic skill
                         categories. Preserves certifications.
  Pass 3 · Validator   — Scores ATS coverage and bullet quality separately.
  Pass 4 · Corrector   — Surgically patches missing keywords, weak bullets, and removes
                         fabricated skills using Pass 3 feedback. Re-validates to confirm.

Key improvements over v3:
  - Self-correction loop: validator findings feed back to fix the resume, not just report.
  - Fabrication removal: corrector actively strips invented skills flagged by validator.
  - Certifications preserved in output model and rendered between Skills and Education.
  - Dynamic skill categories: writer decides labels based on JD domain (not hardcoded).
  - Separate ATS score and quality score for precision correction targeting.
  - Score gate: warns loudly if final score is below --min-score threshold.

Usage:
    python mlx_resume_v4.py google_analyst --master master_resume.md --job jd.txt
    python mlx_resume_v4.py google_analyst --no-pdf --no-validate
    python mlx_resume_v4.py google_analyst --model mlx-community/Qwen3-14B-4bit
    python mlx_resume_v4.py google_analyst --writer-temp 0.4 --retries 3 --min-score 75
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Type, TypeVar

from jinja2 import Template
from pydantic import BaseModel, Field, ValidationError, model_validator

try:
    from mlx_lm import generate, load
    from mlx_lm.sample_utils import make_sampler
    from mlx_lm.utils import hf_repo_to_path, load_model, load_tokenizer
except ImportError:
    print("mlx-lm not installed. Run: pip install mlx-lm")
    sys.exit(1)


def _load(model_path: str):
    """
    Load a model and tokenizer. Falls back to strict=False for VLMs (e.g. Qwen3.5)
    whose weights include a vision_tower not handled by the text-only architecture.
    The text-language-model weights load cleanly; unused vision weights are skipped.
    """
    try:
        return load(model_path, tokenizer_config={"trust_remote_code": True})
    except ValueError as e:
        if "parameters not in model" in str(e):
            log.warning(
                "Standard load failed (likely a VLM with vision weights). "
                "Retrying with strict=False — vision tower will be ignored."
            )
            resolved = hf_repo_to_path(model_path)
            model, _ = load_model(resolved, strict=False)
            tokenizer = load_tokenizer(resolved, tokenizer_config_extra={"trust_remote_code": True})
            return model, tokenizer
        raise

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    model_path: str            = "mlx-community/Qwen3.5-9B-MLX-4bit"
    analyzer_model_path: str   = ""          # empty = use model_path

    # Token budgets — calibrated to actual output sizes, not theoretical maximums.
    # Tighter budgets = faster generation on memory-bandwidth-limited hardware (M-series).
    analyzer_max_tokens: int    = 1000   # structured JSON ~600 tokens typical
    inference_max_tokens: int   = 1500   # short list with rationale fields
    writer_max_tokens: int      = 4000   # full resume JSON ~2000-2500 tokens
    validator_max_tokens: int   = 1800   # keyword list + bullet ratings ~1200 tokens
    revalidator_max_tokens: int = 1200   # scores + keyword coverage only, no fabrication check
    corrector_max_tokens: int   = 2000   # sparse patch ~400 tokens typical

    # Qwen3.5 recommended sampling params (README §Quickstart).
    # Thinking mode is kept OFF for all passes on 9B: the model exhausts its entire
    # token budget on reasoning before producing any output, giving empty results.
    # Thinking mode becomes practical at 32B+. A --thinking flag is available to
    # re-enable it if running a larger model.
    #
    # Non-thinking mode: temp=0.7, top_p=0.8, top_k=20
    analyzer_temp: float  = 0.7
    writer_temp: float    = 0.7
    validator_temp: float = 0.7
    corrector_temp: float = 0.7
    top_k: int            = 20
    top_p_thinking: float = 0.95   # used when enable_thinking=True (large models)
    top_p_fast: float     = 0.8    # used when enable_thinking=False (default)
    min_p: float          = 0.0

    # Thinking mode: OFF by default (see note above). Pass --thinking to enable.
    writer_thinking: bool    = False
    corrector_thinking: bool = False
    analyzer_thinking: bool  = False
    validator_thinking: bool = False

    max_json_retries: int   = 2
    pandoc_template: str    = "resume_template.tex"
    pdf_engine: str         = "xelatex"
    min_score: int          = 70          # warn if final score is below this
    skip_inference: bool    = False       # --no-inference flag

# ---------------------------------------------------------------------------
# Data Models — Pass 1: JD Analyzer
# ---------------------------------------------------------------------------

class TerminologyBridge(BaseModel):
    candidate_term: str = Field(description="Term likely used in the resume")
    jd_term: str        = Field(default="", description="Equivalent term used in the JD")

    @model_validator(mode="before")
    @classmethod
    def _normalize_jd_term(cls, data: dict) -> dict:
        """Models often output 'synonym', 'analog', 'equivalent' instead of 'jd_term'."""
        if isinstance(data, dict) and not data.get("jd_term"):
            for alt in ("synonym", "analog", "analogue", "equivalent", "jd_equivalent", "job_term"):
                if data.get(alt):
                    data["jd_term"] = data[alt]
                    break
        return data


class JDAnalysis(BaseModel):
    """Structured intelligence extracted from the job description."""
    role_type: str = Field(description="analyst | engineer | leadership | hybrid")
    seniority: str = Field(description="junior | mid | senior | lead | manager")
    domain: str    = Field(description="Primary discipline, e.g. 'cybersecurity'")
    company_name: str = Field(default="", description="Company name if identifiable from JD")
    ats_keywords: List[str] = Field(
        description="Exact phrases from the JD that ATS systems will scan for"
    )
    required_skills: List[str] = Field(
        description="Hard requirements — 'must have', 'required', 'x+ years'"
    )
    preferred_skills: List[str] = Field(
        description="Nice-to-haves — 'preferred', 'a plus', 'ideally'"
    )
    top_responsibilities: List[str] = Field(
        description="4-5 most important day-to-day responsibilities"
    )
    terminology_map: List[TerminologyBridge] = Field(
        default_factory=list,
        description="Translation hints from resume-speak to JD-speak"
    )
    company_signals: List[str] = Field(
        default_factory=list,
        description="3-5 phrases revealing company culture, values, or priorities"
    )

# ---------------------------------------------------------------------------
# Data Models — Pass 2: Resume Writer
# ---------------------------------------------------------------------------

class SkillCategory(BaseModel):
    label: str = Field(description="Category heading, e.g. 'Cybersecurity & Governance'")
    items: str = Field(description="Comma-separated skills in this category")


class WorkExperience(BaseModel):
    company: str
    title: str
    dates: str = Field(default="Present")
    points: List[str] = Field(
        description="3-5 metric-driven bullets. FIRST bullet = single most impressive achievement."
    )


class Education(BaseModel):
    institution: str
    degree: str


class ResumeContent(BaseModel):
    name: str
    contact_info: str        = Field(default="")
    summary: str             = Field(default="", description="2-3 sentence executive summary")
    experience: List[WorkExperience]
    skills_categories: List[SkillCategory] = Field(
        default_factory=list,
        description="2-4 skill groups with dynamic labels based on the JD domain"
    )
    certifications: List[str] = Field(
        default_factory=list,
        description="Exact certification names from master resume, e.g. 'CompTIA Security+ (2025)'"
    )
    education: List[Education]

# ---------------------------------------------------------------------------
# Data Models — Pass 3: Validator
# ---------------------------------------------------------------------------

class KeywordCheck(BaseModel):
    keyword: str
    present: bool
    location: str = Field(default="")


class BulletRating(BaseModel):
    company: str
    bullet_preview: str = Field(description="First ~60 chars of the bullet")
    score: int          = Field(description="1-5 quality score")
    feedback: str       = Field(default="")


class ValidationReport(BaseModel):
    ats_score: int          = Field(default=0, description="0-100 ATS keyword coverage (achievable only)")
    quality_score: int      = Field(default=0, description="0-100 bullet quality score")
    overall_score: int      = Field(default=0, description="Composite score (recomputed in code)")
    gap_score: int          = Field(default=0, description="0-100 total JD fit including blocked keywords")
    keyword_coverage: List[KeywordCheck]
    bullet_ratings: List[BulletRating]
    fabrication_flags: List[str] = Field(default_factory=list)
    summary_feedback: str        = Field(default="")
    top_improvements: List[str]  = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_and_recover(cls, data: dict) -> dict:
        """Normalize fabrication_flags and recover missing score fields."""
        if not isinstance(data, dict):
            return data
        # Flatten fabrication_flags dicts → strings
        if isinstance(data.get("fabrication_flags"), list):
            normalized = []
            for item in data["fabrication_flags"]:
                if isinstance(item, str):
                    normalized.append(item)
                elif isinstance(item, dict):
                    text = (
                        item.get("description") or item.get("detail")
                        or item.get("issue") or item.get("reason")
                        or ", ".join(f"{k}: {v}" for k, v in item.items())
                    )
                    normalized.append(str(text))
            data["fabrication_flags"] = normalized
        # Coerce score fields to int (LLM sometimes outputs floats like 88.5)
        for field in ("ats_score", "quality_score", "overall_score"):
            if data.get(field) is not None:
                data[field] = int(round(float(data[field])))
        # Recover missing score fields
        ats = data.get("ats_score")
        qual = data.get("quality_score")
        overall = data.get("overall_score")
        if ats is not None and overall is not None and qual is None:
            data["quality_score"] = max(0, min(100, 2 * overall - ats))
        elif qual is not None and ats is not None and overall is None:
            data["overall_score"] = (ats + qual) // 2
        return data

# ---------------------------------------------------------------------------
# Data Models — Pass 4: Corrector
# ---------------------------------------------------------------------------

class BulletCorrection(BaseModel):
    company: str
    original_preview: str = Field(description="First ~60 chars of the bullet to find and replace")
    corrected_bullet: str
    reason: str = Field(default="")


class SkillAddition(BaseModel):
    category_label: str = Field(description="Must match an existing skills_categories label exactly")
    items_to_add: str   = Field(description="Comma-separated items to append to this category")


class SkillRemoval(BaseModel):
    category_label: str  = Field(description="Must match an existing skills_categories label exactly")
    items_to_remove: str = Field(description="Comma-separated fabricated items to remove from this category")

    @model_validator(mode="before")
    @classmethod
    def _coerce_list_to_string(cls, data: dict) -> dict:
        if isinstance(data, dict) and isinstance(data.get("items_to_remove"), list):
            data["items_to_remove"] = ", ".join(str(x) for x in data["items_to_remove"])
        return data


class CorrectedResume(BaseModel):
    """Sparse patch — only changed fields. Null/empty = keep original."""
    summary: Optional[str]                 = Field(default=None)
    bullet_corrections: List[BulletCorrection] = Field(default_factory=list)
    skill_additions: List[SkillAddition]       = Field(default_factory=list)
    skill_removals: List[SkillRemoval]         = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _filter_null_bullets(cls, data: dict) -> dict:
        """Drop BulletCorrection entries where corrected_bullet is null/None."""
        if isinstance(data, dict) and "bullet_corrections" in data:
            data["bullet_corrections"] = [
                bc for bc in (data["bullet_corrections"] or [])
                if isinstance(bc, dict) and bc.get("corrected_bullet") is not None
            ]
        return data

# ---------------------------------------------------------------------------
# Data Models — Pass 1b: Inference (optional)
# ---------------------------------------------------------------------------

class InferableKeyword(BaseModel):
    keyword: str = Field(description="The JD keyword being inferred")
    rationale: str = Field(
        description="Which specific experience in the master resume demonstrates this capability"
    )
    grounding: str = Field(
        description="Near-verbatim phrase from the master resume that is the evidence"
    )
    usage_guidance: str = Field(
        description="How the writer should express this keyword naturally in the resume"
    )


class InferenceResult(BaseModel):
    inferable: List[InferableKeyword] = Field(
        default_factory=list,
        description="Keywords the candidate can legitimately use, with master resume grounding"
    )
    truly_blocked: List[str] = Field(
        default_factory=list,
        description="Keywords that require fabrication — no legitimate inference possible"
    )

# ---------------------------------------------------------------------------
# Generic model parser
# ---------------------------------------------------------------------------

T = TypeVar("T", bound=BaseModel)

# Phrases that are explicitly banned from the resume summary.
# Defined at module level so the post-Pass-3 checker and tests can reference it.
_BANNED_SUMMARY_PHRASES = [
    "results-driven", "proven track record", "passionate professional",
    "dynamic professional", "highly motivated", "dedicated professional",
    "passionate about", "strong passion", "proven ability", "dynamic",
]


def _recompute_scores(report: ValidationReport) -> ValidationReport:
    """
    Override LLM-computed scores with deterministic values derived from the structured data.

    The LLM frequently miscalculates scores (applies fabrication penalties to ATS, omits
    quality_score, etc.). Computing in code ensures consistency across all runs.

    Scoring rules:
      ats_score    = achievable keywords present / achievable total × 100
                     (excludes keywords flagged "NOT IN MASTER RESUME")
      gap_score    = all keywords present / all keywords total × 100
                     (includes blocked keywords — honest JD fit %)
      quality_score = average bullet score / 5 × 100
      overall_score = (ats + quality) / 2  –  10 pts per fabrication flag
                      capped at 60 if any fabrication flag exists
    """
    # ATS: achievable keywords only (no fabrication needed)
    achievable = [k for k in report.keyword_coverage
                  if "NOT IN MASTER RESUME" not in (k.location or "")]
    present    = sum(1 for k in achievable if k.present)
    ats_score  = round(present / max(len(achievable), 1) * 100)

    # Gap: all keywords including blocked (honest "JD fit" metric)
    all_kws    = report.keyword_coverage
    all_present = sum(1 for k in all_kws if k.present)
    gap_score  = round(all_present / max(len(all_kws), 1) * 100)

    # Quality: average of bullet scores
    scores = [b.score for b in report.bullet_ratings]
    quality_score = round((sum(scores) / max(len(scores), 1)) / 5 * 100)

    # Overall: composite minus fabrication penalty (-10 per flag, capped at 60 if any)
    overall = round((ats_score + quality_score) / 2)
    n_fab = len(report.fabrication_flags)
    overall = max(0, overall - n_fab * 10)
    if n_fab > 0:
        overall = min(overall, 60)  # hard cap: any fabrication prevents "great" score

    data = report.model_dump()
    data.update(ats_score=ats_score, quality_score=quality_score,
                overall_score=overall, gap_score=gap_score)
    return ValidationReport(**data)


def parse_model(text: str, model_cls: Type[T]) -> Optional[T]:
    """Strip thinking tokens, extract JSON block, parse into Pydantic model."""
    cleaned = _strip_thinking_tokens(text)
    json_str = _extract_json_block(cleaned)
    try:
        data = json.loads(json_str)
        return model_cls(**data)
    except json.JSONDecodeError as e:
        log.warning("JSON decode error (%s): %s", model_cls.__name__, e)
    except ValidationError as e:
        log.warning("Schema validation error (%s): %s", model_cls.__name__, e)
    return None

# ---------------------------------------------------------------------------
# String utilities
# ---------------------------------------------------------------------------

# "$" excluded intentionally — dollar signs in metric bullets must not be escaped.
_LATEX_ESCAPE_MAP = {
    "&":  r"\&",
    "%":  r"\%",
    "#":  r"\#",
    "_":  r"\_",
    "{":  r"\{",
    "}":  r"\}",
    "~":  r"\textasciitilde{}",
    "^":  r"\^{}",
    "\\": r"\textbackslash{}",
}
_LATEX_ESCAPE_RE = re.compile(
    "|".join(re.escape(k) for k in _LATEX_ESCAPE_MAP)
)


def escape_latex(text: str) -> str:
    if not text:
        return ""
    return _LATEX_ESCAPE_RE.sub(lambda m: _LATEX_ESCAPE_MAP[m.group(0)], text)


def _strip_thinking_tokens(text: str) -> str:
    """
    Remove <think>...</think> CoT blocks emitted by Qwen3 and similar models.
    Fallback: if stripping leaves nothing, salvage JSON from inside the block.
    """
    think_match = re.search(r"<think>(.*?)</think>", text, flags=re.DOTALL)
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if not cleaned and think_match:
        log.debug("Output was entirely inside <think> block — extracting from within.")
        cleaned = think_match.group(1).strip()
    return cleaned


def _extract_json_block(text: str) -> str:
    """Extract the first complete JSON object from a string."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start: end + 1]
    return text


def _fmt_bullets(items: List[str]) -> str:
    return "\n".join(f"  • {item}" for item in items)


def _filter_achievable_keywords(keywords: List[str], master_text: str) -> tuple[List[str], List[str]]:
    """
    Split JD keywords into (achievable, blocked).

    achievable = keywords the candidate can legitimately use based on master resume.
    blocked    = keywords requiring fabrication (not in master resume at all).

    Matching strategy (in order):
      1. Exact phrase match (case-insensitive)
      2. Normalized match — strips spaces for "Power BI" ↔ "PowerBI"
      3. Full-token match — words ≥ 7 chars from the keyword must match as whole
         words (\\b boundaries) in the master text. Substring matches are rejected
         to avoid "Threat" in "ThreatLocker" matching "threat modeling".
    """
    master_lower = master_text.lower()
    master_norm  = re.sub(r"\s+", "", master_lower)  # "powerbi" from "PowerBI"

    achievable, blocked = [], []
    for kw in keywords:
        kw_lower = kw.lower().strip()
        matched = False
        # Short keywords (≤2 chars, e.g. "R") must match as a whole word only
        if len(kw_lower) <= 2:
            if re.search(r"\b" + re.escape(kw_lower) + r"\b", master_lower):
                matched = True
        elif kw_lower in master_lower:
            matched = True
        if not matched:
            kw_norm = re.sub(r"\s+", "", kw_lower)
            if kw_norm and kw_norm in master_norm:
                matched = True
        if not matched:
            # Tier-3: each long token must appear as a whole word, not a substring
            tokens = re.findall(r"\b[a-z]{7,}\b", kw_lower)
            if tokens and all(
                re.search(r"\b" + re.escape(t) + r"\b", master_lower)
                for t in tokens
            ):
                matched = True
        (achievable if matched else blocked).append(kw)
    return achievable, blocked


def _fmt_terminology(bridges: List[TerminologyBridge]) -> str:
    if not bridges:
        return "  (none identified)"
    return "\n".join(f'  "{b.candidate_term}" → "{b.jd_term}"' for b in bridges)


def _fmt_inferable_block(inferable: "List[InferableKeyword]") -> str:
    if not inferable:
        return (
            "  NONE. There are zero pre-approved inferable keywords.\n"
            "  Any keyword that does not appear verbatim in the master resume is a fabrication."
        )
    lines = []
    for kw in inferable:
        lines.append(f"  • {kw.keyword}")
        lines.append(f'    Evidence  : "{kw.grounding}"')
        lines.append(f"    Use as    : {kw.usage_guidance}")
    return "\n".join(lines)


def build_inference_prompt(
    blocked_keywords: List[str],
    master_resume: str,
    job_desc: str,
    analysis: "JDAnalysis",
) -> str:
    blocked_block = _fmt_bullets(blocked_keywords)
    return f"""\
You are a conservative career consultant performing a semantic inference audit.
Your job: determine which blocked JD keywords represent capabilities the candidate
ACTUALLY HAS — just expressed in different terminology in their resume.
Return ONLY valid JSON. No explanation, no markdown fences.

══════════════════════════════════════════════════════
 CRITICAL CONSERVATISM RULE
══════════════════════════════════════════════════════
- If you are even slightly uncertain → place in truly_blocked.
- Inference requires SPECIFIC documented experience, not general plausibility.
- You CANNOT infer domain expertise the candidate clearly does not have.
- The grounding field MUST contain a near-verbatim phrase from the master resume.
  If you cannot find a specific phrase, the keyword belongs in truly_blocked.
- Every keyword in the input list must appear in EXACTLY ONE of: inferable or truly_blocked.

══════════════════════════════════════════════════════
 INFERENCE STANDARD — a keyword is inferable ONLY IF ALL THREE:
══════════════════════════════════════════════════════
1. The master resume documents a SPECIFIC activity that requires the same underlying
   capability (different words, same skill).
2. The inference does NOT require claiming domain expertise in an area the candidate
   clearly has no background in.
3. The grounding phrase appears (or near-appears) verbatim in the master resume.

══════════════════════════════════════════════════════
 EXAMPLES
══════════════════════════════════════════════════════
VALID INFERENCE:
  "post-implementation support" — inferable if master shows a completed migration with
  user-facing follow-through (e.g. "SharePoint migration for 1,000+ users, access
  recertification automation"). Different words, same activity.

INVALID (must be truly_blocked):
  "pipeline health" from an engineering background — "pipeline" in data engineering ≠
  sales pipeline. The domain expertise is genuinely absent.
  "managed service providers" — requires specific MSP partnership experience; enterprise
  IT work does not imply MSP knowledge.
  "Small-Medium Business" — a market segment with no documented exposure in the resume.
  Any keyword where the skill MIGHT exist but is not documented in the master resume.

══════════════════════════════════════════════════════
 ROLE CONTEXT
══════════════════════════════════════════════════════
Role: {analysis.role_type} / {analysis.seniority} / {analysis.domain}
Company: {analysis.company_name or "(not identified)"}

══════════════════════════════════════════════════════
 BLOCKED KEYWORDS TO EVALUATE
══════════════════════════════════════════════════════
{blocked_block}

══════════════════════════════════════════════════════
 MASTER RESUME  (sole source of evidence)
══════════════════════════════════════════════════════
{master_resume}

══════════════════════════════════════════════════════
 TARGET JOB DESCRIPTION  (context only — not evidence)
══════════════════════════════════════════════════════
{job_desc}

══════════════════════════════════════════════════════
 OUTPUT JSON SCHEMA  (return ONLY this)
══════════════════════════════════════════════════════
{{
  "inferable": [
    {{
      "keyword": "post-implementation support",
      "rationale": "AIA role includes SharePoint migration for 1,000+ users with access recertification follow-through, which is post-implementation support work",
      "grounding": "Box to SharePoint migration for 1,000+ users via access recertification automation",
      "usage_guidance": "Use in the AIA migration bullet to describe post-go-live user support and recertification follow-through"
    }}
  ],
  "truly_blocked": [
    "pipeline health",
    "sales management",
    "Small-Medium Business"
  ]
}}
"""

# ---------------------------------------------------------------------------
# Patch application — Pass 4 Corrector
# ---------------------------------------------------------------------------

def _find_bullet_index(points: List[str], preview: str) -> int:
    """Find the index of a bullet by matching its preview text."""
    needle = preview.strip().lower()[:50]
    for i, point in enumerate(points):
        point_lower = point.strip().lower()
        if point_lower.startswith(needle):
            return i
        if needle in point_lower:
            return i
    # Looser match: first 30 chars
    needle_short = needle[:30]
    for i, point in enumerate(points):
        if needle_short in point.strip().lower():
            return i
    return -1


def apply_corrections(resume: ResumeContent, patch: CorrectedResume) -> ResumeContent:
    """
    Apply a sparse CorrectedResume patch to a ResumeContent.
    Returns a new ResumeContent — never mutates the original.
    """
    # Deep-copy via model dump → reconstruct
    data = resume.model_dump()
    new_resume = ResumeContent(**data)

    if patch.summary:
        new_resume.summary = patch.summary

    for correction in patch.bullet_corrections:
        for job in new_resume.experience:
            if job.company.strip().lower() == correction.company.strip().lower():
                idx = _find_bullet_index(job.points, correction.original_preview)
                if idx >= 0:
                    job.points[idx] = correction.corrected_bullet
                    log.info(
                        "Corrected bullet in '%s' at index %d.", job.company, idx
                    )
                else:
                    log.warning(
                        "Could not find bullet preview '%s' in '%s' — skipping.",
                        correction.original_preview[:40],
                        job.company,
                    )

    for addition in patch.skill_additions:
        for cat in new_resume.skills_categories:
            if cat.label.strip().lower() == addition.category_label.strip().lower():
                existing = cat.items.rstrip(", ")
                cat.items = f"{existing}, {addition.items_to_add}" if existing else addition.items_to_add
                log.info("Added skills to category '%s'.", cat.label)
                break
        else:
            log.warning("Skill category '%s' not found — skipping addition.", addition.category_label)

    for removal in patch.skill_removals:
        for cat in new_resume.skills_categories:
            if cat.label.strip().lower() == removal.category_label.strip().lower():
                remove_set = {s.strip().lower() for s in removal.items_to_remove.split(",")}
                kept = [
                    s.strip()
                    for s in cat.items.split(",")
                    if s.strip().lower() not in remove_set
                ]
                cat.items = ", ".join(kept)
                log.info(
                    "Removed fabricated skills from '%s': %s",
                    cat.label, removal.items_to_remove,
                )
                break
        else:
            log.warning(
                "Skill category '%s' not found — skipping removal.", removal.category_label
            )

    return new_resume

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def build_analyzer_prompt(job_desc: str) -> str:
    return f"""\
You are an expert ATS analyst and recruitment specialist.
Dissect the job description and extract structured intelligence for resume tailoring.
Return ONLY valid JSON. No explanation, no markdown fences, no preamble.

## EXTRACTION TASKS

### 1. ATS Keywords
Extract the 15–20 most critical phrases an ATS will scan for. Quality over quantity.

INCLUDE:
  - Technical tools and platforms stated as requirements (copy verbatim from the JD)
  - Domain-specific methodologies, frameworks, or processes (verbatim)
  - Key functional competencies directly tied to job responsibilities (verbatim)

EXCLUDE — do NOT add these even if they appear in the JD:
  - Personality traits and adjectives ("highly analytical", "passionate", "quantitative mind")
  - Generic soft skills ("communication skills", "problem-solving", "work independently")
  - Geographic locations and team names ("Asia Pacific", "Japan", "APJC")
  - Company brand names and products the candidate would not list on a resume
    (e.g. "Meraki", "Webex", "Cisco" — unless they are industry-standard tools)
  - Vague business phrases ("opportunities to improve", "expanding in this market")
  - Duplicate concepts — pick the single most specific phrasing, not multiple variants

Deduplicate: if two phrases mean the same thing, keep only the more specific one.
These strings will be injected verbatim into the resume — keep them precise and short.

### 2. Required Skills
Hard requirements: "required", "must have", "x+ years of", listed as mandatory.

### 3. Preferred Skills
Nice-to-haves: "preferred", "a plus", "nice to have", "ideally".

### 4. Top Responsibilities
The 4-5 most critical day-to-day activities. Use action phrases (e.g. "Manage SIEM alerts").

### 5. Role Classification
  - role_type: "analyst" | "engineer" | "leadership" | "hybrid"
  - seniority:  "junior" | "mid" | "senior" | "lead" | "manager"
  - domain: primary discipline (e.g. "cybersecurity", "data engineering")
  - company_name: company name if identifiable, else empty string

### 6. Terminology Map
Only if the JD uses meaningfully different language than a typical candidate resume.
Example: candidate writes "Chronicle log ingestion" → JD says "security data infrastructure"
Skip obvious synonyms. Max 6 entries.

### 7. Company Signals
Extract 3-5 short phrases that reveal this company's culture, values, or priorities.
Look for language like "data-driven culture", "cross-functional collaboration",
"security-first mindset", "customer obsession", "move fast". These will anchor the
cover letter to show cultural fit. Infer from context if not stated explicitly.

## JOB DESCRIPTION
{job_desc}

## OUTPUT SCHEMA
{{
  "role_type": "analyst",
  "seniority": "mid",
  "domain": "cybersecurity",
  "company_name": "Acme Corp",
  "ats_keywords": ["keyword 1", "keyword 2"],
  "required_skills": ["skill 1"],
  "preferred_skills": ["skill 1"],
  "top_responsibilities": ["responsibility 1"],
  "terminology_map": [
    {{"candidate_term": "...", "jd_term": "..."}}
  ],
  "company_signals": ["data-driven culture", "cross-functional collaboration"]
}}
"""


def build_writer_prompt(
    master_resume: str,
    job_desc: str,
    analysis: JDAnalysis,
    blocked_keywords: Optional[List[str]] = None,
    inferable_keywords: Optional[List[InferableKeyword]] = None,
) -> str:
    keywords_block    = _fmt_bullets(analysis.ats_keywords)
    required_block    = _fmt_bullets(analysis.required_skills)
    resp_block        = _fmt_bullets(analysis.top_responsibilities)
    terminology_block = _fmt_terminology(analysis.terminology_map)
    inferable_block = _fmt_inferable_block(inferable_keywords or [])
    blocked_block = (
        _fmt_bullets(blocked_keywords)
        if blocked_keywords
        else "  (none — all remaining blocked keywords are achievable or inferable)"
    )

    return f"""\
You are a world-class resume writer specializing in ATS optimization and executive positioning.
Rewrite the candidate's resume for the target role. Return ONLY valid JSON — no preamble,
no explanation, no markdown fences.

══════════════════════════════════════════════════════
 JD INTELLIGENCE BRIEF
══════════════════════════════════════════════════════
Role: {analysis.role_type.upper()} | Seniority: {analysis.seniority} | Domain: {analysis.domain}

TOP RESPONSIBILITIES — prioritize bullets that directly address these:
{resp_block}

REQUIRED SKILLS:
{required_block}

TERMINOLOGY BRIDGES — translate resume language → JD language where relevant:
{terminology_block}

══════════════════════════════════════════════════════
 ⚠  MANDATORY ATS KEYWORD INJECTION
══════════════════════════════════════════════════════
Every keyword below MUST appear naturally somewhere in the resume output
(summary, bullet points, or skills section). Embedding them increases ATS match rate.
Do NOT force-fit awkwardly — find the most natural placement for each.

{keywords_block}

══════════════════════════════════════════════════════
 BULLET WRITING FORMULA  (apply to EVERY bullet)
══════════════════════════════════════════════════════
Format:  [Strong Past-Tense Verb]  +  [What you did / How]  +  [Quantified Result or Scope]

Strong verbs:
  Engineered, Architected, Automated, Reduced, Increased, Deployed, Designed,
  Implemented, Drove, Built, Delivered, Streamlined, Spearheaded, Unified, Scaled

✅ GOOD — strong verb, clear action, quantified result:
  "Automated vulnerability data aggregation across 5 security APIs, eliminating 30 hrs/month
   of manual analyst work and freeing CSIRT capacity for active threat response"

✅ GOOD — scope clear, business impact stated:
  "Architected the organization's first unified DLP dashboard consolidating 6 security tools,
   providing real-time data risk visibility and reducing mean incident response time by ~40%"

❌ BAD — no result, fails the "so what?" test:
  "Worked on Python automation projects integrating multiple APIs"

❌ BAD — weak verb, no impact, no scope:
  "Responsible for dashboard development and monitoring"

⭐ KILLER FIRST BULLET RULE:
  For the most recent or most relevant role, the FIRST bullet must be the single most
  impressive, most quantified achievement from that role. Recruiters read bullet 1 first.
  Make it undeniable.

══════════════════════════════════════════════════════
 SUMMARY FORMULA  (2-3 sentences max)
══════════════════════════════════════════════════════
Sentence 1: [Professional identity] + [core technical value proposition for THIS role]
Sentence 2: [Most impressive proof point] + [what it means for this team]
Sentence 3 (optional): [Differentiating credential or unique angle]

Mirror the JD's language for a {analysis.role_type} / {analysis.seniority} role in {analysis.domain}.
Lead with the most relevant credential or metric. NO generic filler ("results-driven",
"passionate", "dynamic professional", "proven track record").

══════════════════════════════════════════════════════
 SKILLS CATEGORIES
══════════════════════════════════════════════════════
Create 2-4 skill categories appropriate for a {analysis.domain} {analysis.role_type} role.
Use labels that match the JD domain (e.g. "Cybersecurity & Governance", "Data Engineering",
"Cloud Infrastructure", "Finance & Compliance"). Do NOT use generic labels like "Skills".
Include ALL skills from the master resume's SKILLS section, distributed across categories.
SKILLS FABRICATION RULE: Every item in skills_categories MUST be taken verbatim from the master
resume's SKILLS section. Do NOT add any tool, language, platform, or framework that is not
explicitly listed in the master resume — even if the JD requires it. Missing a JD keyword is
acceptable; adding a fabricated skill is a hard failure.

══════════════════════════════════════════════════════
 INFERABLE KEYWORDS — use naturally WITH SPECIFIC GROUNDING
══════════════════════════════════════════════════════
These JD keywords are NOT verbatim in the master resume, but the candidate's documented
experience demonstrates the underlying capability. You MAY use each one naturally in the
resume — but ONLY in the context of the specific evidence cited below. Do NOT use these
keywords in a context that is not grounded by the master resume evidence provided.
Do NOT list them bare in the skills section.

{inferable_block}

══════════════════════════════════════════════════════
 HARD RULES  (violations flagged in Pass 3)
══════════════════════════════════════════════════════
1. NEVER alter dates, job titles, or company names.
2. PRESERVE all original hard metrics exactly (%, counts, hours, dollar amounts).
3. PRESERVE contact_info exactly as it appears in the master resume header (including any URL).
4. PRESERVE all certifications from the master resume in the certifications list.
5. METRIC ATTRIBUTION (critical — most common failure mode):
   Every quantified metric belongs to the role AND context it came from in the master resume.
   NEVER move a number from one role to fill a gap in another. NEVER invent a new metric for a
   role that didn't have one. NEVER reuse a metric but change what it describes — if the master
   says "258% increase in validated data sources", you CANNOT write "reduced incident response
   time by 258%". The number AND its subject must stay identical to the master. If a role has
   no quantified metric, describe scope or impact in words.
6. BULLET CONTENT MUST MAP TO MASTER RESUME (critical — most common hallucination):
   Every bullet must describe an activity that ACTUALLY APPEARS in the master resume for
   that role. Do NOT write bullets about projects, systems, teams, or responsibilities
   that are not in the master. If the JD mentions a specific product/system (e.g. APOS,
   Salesforce, SAP) that the candidate never worked on, do NOT write bullets about it —
   write about what the candidate DID do that is closest in skill to what the JD needs.
7. ALLOWED inference: if the JD needs an operational skill (e.g. Threat Hunting) and the
   resume shows the engineering equivalent (built detection pipelines), you MAY infer the
   capability — describe it in words only, no fabricated numbers.
8. PROHIBITED fabrication: no tools, certifications, employers, or skills not in the master
   resume. If the JD needs a tool you don't have, use the generic capability instead
   (e.g. "SIEM management" not "Splunk").
9. Per role: 3-5 bullets. Cut bullets that don't map to any JD responsibility.
   Prioritize bullets addressing TOP RESPONSIBILITIES above all others.
10. TRULY BLOCKED KEYWORDS — these JD terms require fabrication of experience the
    candidate genuinely does not have. Do NOT write any bullet, skill, or summary
    sentence that references these terms in any form:
{blocked_block}

══════════════════════════════════════════════════════
 MASTER RESUME
══════════════════════════════════════════════════════
{master_resume}

══════════════════════════════════════════════════════
 TARGET JOB DESCRIPTION
══════════════════════════════════════════════════════
{job_desc}

══════════════════════════════════════════════════════
 OUTPUT JSON SCHEMA  (return ONLY this — no text before or after)
══════════════════════════════════════════════════════
{{
  "name": "string",
  "contact_info": "City | Email | Phone",
  "summary": "Two-to-three sentence executive summary.",
  "experience": [
    {{
      "company": "string",
      "title": "string",
      "dates": "string",
      "points": ["bullet 1", "bullet 2", "bullet 3"]
    }}
  ],
  "skills_categories": [
    {{"label": "Cybersecurity & Governance", "items": "Skill A, Skill B, Skill C"}},
    {{"label": "Data & Engineering", "items": "Skill D, Skill E"}}
  ],
  "certifications": ["CompTIA Security+ (2025)"],
  "education": [
    {{"institution": "string", "degree": "string"}}
  ]
}}
"""


def build_validator_prompt(
    resume: ResumeContent,
    analysis: JDAnalysis,
    master_resume: str,
    inferable_keywords: Optional[List[InferableKeyword]] = None,
) -> str:
    resume_json    = resume.model_dump_json(indent=2)
    keywords_block = _fmt_bullets(analysis.ats_keywords)
    inferable_block = _fmt_inferable_block(inferable_keywords or [])

    return f"""\
You are a strict ATS auditor and resume quality reviewer.
Audit the tailored resume against the JD analysis. Return ONLY valid JSON.

══════════════════════════════════════════════════════
 AUDIT TASKS
══════════════════════════════════════════════════════

### 1. ATS Keyword Coverage → ats_score (0–100)
For EACH keyword in the ATS KEYWORDS TO AUDIT list, check presence and location.
Keywords fall into three tiers — handle each differently:

TIER A — ACHIEVABLE (verbatim in master resume, listed in ATS KEYWORDS TO AUDIT):
  Standard scoring: mark present=true/false. If absent, note best placement.
  Absent achievable keywords ARE penalized in ats_score.

TIER B — INFERABLE (listed in INFERABLE KEYWORDS section below with grounding):
  These are pre-approved as legitimate. If the keyword or a natural paraphrase of it
  appears anywhere in the tailored resume, mark present=true.
  If absent, mark present=false and suggest placement — but do NOT mark as
  "NOT IN MASTER RESUME". Absent inferable keywords ARE penalized in ats_score.
  Do NOT flag inferable keywords as fabrications in Task 3.

TIER C — EVERYTHING ELSE (not in either list above):
  If a keyword appears in the resume but was not in the ATS list or inferable list,
  and is not in the master resume, mark present=false and note:
  "NOT IN MASTER RESUME — cannot be added without fabrication".
  Do NOT factor these into ats_score denominator.

ats_score = (keywords present / achievable + inferable total) × 100

### 2. Bullet Quality → quality_score (0–100)
Score each bullet 1–5:
  5 = strong action verb + quantified result + directly addresses a JD responsibility
  4 = strong verb + clear result, minor JD alignment gap
  3 = has a metric but weak verb or weak relevance to JD
  2 = no metric, vague language, or passive construction
  1 = "responsible for" / "assisted" / needs complete rewrite
Give brief actionable feedback for any bullet rated 3 or below.
quality_score = (average bullet score / 5) × 100

### 3. Fabrication Check
Compare tailored resume against master resume line by line. Flag ANY of the following:

EXCEPTION — INFERABLE KEYWORDS (do NOT flag these as fabrications):
The keywords listed in the INFERABLE KEYWORDS section were pre-approved based on the
candidate's documented experience. If these keywords appear in the resume in a context
that matches the grounding evidence provided, they are legitimate — do NOT flag them.
Only flag an inferable keyword if it appears in a context entirely unrelated to the
grounding evidence (e.g. used for a different role or fabricated activity).

A. INVENTED BULLET CONTENT — the most common and critical failure.
   Each bullet in the tailored resume must map to a real activity from the master resume.
   If a bullet describes a responsibility, system, team, or project that does NOT appear
   anywhere in the master resume (e.g., a bullet about "APOS data foundation" when the
   master has no APOS work), flag it as invented content.

B. METRIC FABRICATION — a number, percentage, count, or duration appears in the tailored
   resume that is not in the master resume for that same role.

C. SKILL/TOOL FABRICATION — a tool, language, framework, or certification not in the master.

D. TITLE / DATE / COMPANY changes — any alteration to employer names, job titles, or dates.

### 4. Summary Effectiveness
Does the summary:
  - Open with a relevant credential/proof point (not generic filler)?
  - Mirror JD language for a {analysis.role_type} / {analysis.seniority} role?
  - Include at least one specific metric or named technology?
Provide actionable feedback in summary_feedback. If the summary is strong, say so.

### 5. Overall Score (0–100)
overall_score = (ats_score × 0.5) + (quality_score × 0.5)
Apply a fabrication penalty: –15 points per verified fabrication.

### 6. Top Improvements
List the 3-5 highest-impact changes that would raise the overall_score the most.
Be specific — name the exact bullet, keyword, or section.

══════════════════════════════════════════════════════
 INFERABLE KEYWORDS  (Tier B — pre-approved, do NOT flag as fabrications)
══════════════════════════════════════════════════════
{inferable_block}

══════════════════════════════════════════════════════
 ATS KEYWORDS TO AUDIT  (Tier A — achievable from master resume)
══════════════════════════════════════════════════════
{keywords_block}

══════════════════════════════════════════════════════
 TAILORED RESUME
══════════════════════════════════════════════════════
{resume_json}

══════════════════════════════════════════════════════
 MASTER RESUME  (source of truth for fabrication check)
══════════════════════════════════════════════════════
{master_resume}

══════════════════════════════════════════════════════
 OUTPUT JSON SCHEMA
══════════════════════════════════════════════════════
{{
  "ats_score": 80,
  "quality_score": 75,
  "overall_score": 78,
  "keyword_coverage": [
    {{"keyword": "data loss prevention", "present": true,  "location": "skills_categories"}},
    {{"keyword": "stakeholder management","present": false, "location": "Add to AIA migration bullet"}}
  ],
  "bullet_ratings": [
    {{"company": "AIA", "bullet_preview": "Automated vulnerability data…", "score": 5, "feedback": ""}},
    {{"company": "AIA", "bullet_preview": "Responsible for reporting…",    "score": 2, "feedback": "Start with strong verb; add quantified result"}}
  ],
  "fabrication_flags": [],
  "summary_feedback": "Strong opening. Consider adding mention of [X] — a top JD requirement.",
  "top_improvements": [
    "Add 'stakeholder management' to the AIA migration bullet",
    "Rewrite Mazars bullet — no metric present, score 2"
  ]
}}
"""


def build_revalidator_prompt(
    resume: ResumeContent,
    analysis: JDAnalysis,
    inferable_keywords: Optional[List[InferableKeyword]] = None,
) -> str:
    """
    Lightweight re-validation prompt used after Pass 4 corrections.

    Drops the master resume and fabrication check entirely — the corrector already
    operates under strict master-resume constraints, so re-checking fabrications adds
    no value. Only re-scores ATS keyword coverage and bullet quality.
    Roughly half the prefill cost of the full validator prompt.
    """
    resume_json    = resume.model_dump_json(indent=2)
    keywords_block = _fmt_bullets(analysis.ats_keywords)
    inferable_block = _fmt_inferable_block(inferable_keywords or [])

    return f"""\
You are an ATS auditor re-scoring a resume after targeted corrections.
Return ONLY valid JSON. No explanation, no markdown fences.

══════════════════════════════════════════════════════
 INFERABLE KEYWORDS  (Tier B — pre-approved, count as present if used)
══════════════════════════════════════════════════════
{inferable_block}

══════════════════════════════════════════════════════
 ATS KEYWORDS TO AUDIT  (Tier A)
══════════════════════════════════════════════════════
{keywords_block}

══════════════════════════════════════════════════════
 TASK 1 — ATS Keyword Coverage → ats_score (0–100)
══════════════════════════════════════════════════════
For each keyword: is it present in the resume? If yes, where?
Tier A absent = penalized. Tier B absent = penalized (but suggest placement, not fabrication).
ats_score = (present / total Tier A + Tier B) × 100

══════════════════════════════════════════════════════
 TASK 2 — Bullet Quality → quality_score (0–100)
══════════════════════════════════════════════════════
Score each bullet 1–5:
  5 = strong verb + quantified result + directly addresses a JD responsibility
  4 = strong verb + clear result, minor gap
  3 = metric present but weak verb or weak relevance
  2 = no metric, vague, passive
  1 = "responsible for" / "assisted"
quality_score = (average / 5) × 100

══════════════════════════════════════════════════════
 CORRECTED RESUME
══════════════════════════════════════════════════════
{resume_json}

══════════════════════════════════════════════════════
 OUTPUT JSON SCHEMA
══════════════════════════════════════════════════════
{{
  "ats_score": 85,
  "quality_score": 90,
  "overall_score": 87,
  "keyword_coverage": [
    {{"keyword": "data-driven analysis", "present": true, "location": "summary"}},
    {{"keyword": "sales performance metrics", "present": false, "location": "Add to Mazars bullet"}}
  ],
  "bullet_ratings": [
    {{"company": "AIA Digital+ Malaysia", "bullet_preview": "Engineered DLP framework…", "score": 5, "feedback": ""}},
    {{"company": "Mazars Malaysia", "bullet_preview": "Performed in-depth financial…", "score": 3, "feedback": "Add a specific metric"}}
  ],
  "fabrication_flags": [],
  "summary_feedback": "",
  "top_improvements": []
}}
"""


def build_corrector_prompt(
    resume: ResumeContent,
    report: ValidationReport,
    analysis: JDAnalysis,
    master_resume: str,
) -> str:
    resume_json = resume.model_dump_json(indent=2)

    # Build a numbered bullet index so corrector can copy original_preview exactly
    bullet_index_lines = []
    for job in resume.experience:
        bullet_index_lines.append(f"  {job.company}:")
        for i, pt in enumerate(job.points):
            bullet_index_lines.append(f"    [{i}] \"{pt[:70]}\"")
    bullet_index_block = "\n".join(bullet_index_lines)

    missing_keywords = [k for k in report.keyword_coverage
                        if not k.present and "NOT IN MASTER RESUME" not in (k.location or "")]
    weak_bullets     = [b for b in report.bullet_ratings if b.score <= 3]

    missing_block = (
        _fmt_bullets([f"{k.keyword}  →  suggested location: {k.location}" for k in missing_keywords])
        if missing_keywords else "  (none — all keywords present)"
    )
    weak_block = (
        "\n".join(
            f"  Company: {b.company}\n"
            f"  Bullet:  \"{b.bullet_preview}…\"\n"
            f"  Score:   {b.score}/5\n"
            f"  Fix:     {b.feedback}\n"
            for b in weak_bullets
        ) if weak_bullets else "  (none — all bullets rated 4+. Do NOT generate any bullet_corrections.)"
    )
    summary_issue = report.summary_feedback if report.summary_feedback else "(summary is acceptable — do not change, set summary to null)"
    fab_block = (
        _fmt_bullets(report.fabrication_flags)
        if report.fabrication_flags
        else "  (none — no fabricated content detected)"
    )

    return f"""\
You are a surgical resume patcher. An ATS audit has identified specific issues.
Fix ONLY the issues listed below. Do NOT touch anything else.
Return ONLY a valid JSON patch object — not the full resume.

══════════════════════════════════════════════════════
 ISSUES TO FIX
══════════════════════════════════════════════════════

MISSING ATS KEYWORDS — weave these naturally into the resume:
{missing_block}

WEAK BULLETS (score ≤ 3) — rewrite these specific bullets only:
{weak_block}

SUMMARY FEEDBACK:
{summary_issue}

FABRICATION FLAGS — address every flag below:
{fab_block}

══════════════════════════════════════════════════════
 CONSTRAINTS — CRITICAL
══════════════════════════════════════════════════════
1. Only fix what is listed. If WEAK BULLETS says "none — all bullets rated 4+", set bullet_corrections to [].
2. If SUMMARY FEEDBACK says "acceptable — do not change", set summary to null.
3. FABRICATION FLAGS — for INVENTED BULLET CONTENT or METRIC FABRICATION: you MUST
   rewrite or remove the offending bullet regardless of its quality score. Fabrication
   is a hard failure — a bullet rated 4-5 that contains invented content must still be
   corrected. Replace the invented claim with an accurate one from the master resume, or
   remove the bullet entirely if no accurate replacement exists.
   TITLE / DATE changes: cannot be patched — skip.
   Set skill_removals to [] always — skill cleanup is handled upstream, not here.
   CRITICAL: Only change bullets listed in WEAK BULLETS or FABRICATION FLAGS. Make NO
   changes based on your own judgment beyond what is explicitly listed above.
4. Do NOT invent tools, certifications, employers, or metrics not in the master resume.
5. METRIC ATTRIBUTION: every number belongs to the role it came from. Never move a metric
   from one company's bullets to another, and never invent a new metric for a role.
6. Preserve all original numbers exactly (%, counts, hours, dollar amounts).
7. Keywords must be woven naturally into existing context — do not list them bare.
8. original_preview MUST be copied verbatim from the BULLET INDEX below — use the exact
   text shown (first ~70 chars). Do NOT paraphrase, do NOT use text from the validator report.
9. For missing keywords: ONLY add via skill_additions if the candidate genuinely has that skill
   based on the master resume. Do NOT add skills or tools not present in the master resume
   just to hit ATS coverage — that is fabrication.

══════════════════════════════════════════════════════
 BULLET INDEX  (copy original_preview from here exactly)
══════════════════════════════════════════════════════
{bullet_index_block}

══════════════════════════════════════════════════════
 MASTER RESUME  (do not invent beyond this)
══════════════════════════════════════════════════════
{master_resume}

══════════════════════════════════════════════════════
 CURRENT RESUME (JSON — patch against this)
══════════════════════════════════════════════════════
{resume_json}

══════════════════════════════════════════════════════
 OUTPUT JSON PATCH  (return ONLY this)
══════════════════════════════════════════════════════
{{
  "summary": null,
  "bullet_corrections": [
    {{
      "company": "AIA Digital+ Malaysia",
      "original_preview": "Responsible for reporting and dashboard...",
      "corrected_bullet": "Engineered automated weekly reporting pipeline...",
      "reason": "Replaced passive 'responsible for' with strong verb; added quantified scope"
    }}
  ],
  "skill_additions": [
    {{
      "category_label": "Cybersecurity & Governance",
      "items_to_add": "Threat Intelligence, SOAR"
    }}
  ],
  "skill_removals": []
}}
"""


# ---------------------------------------------------------------------------
# Generation engine
# ---------------------------------------------------------------------------

def _generate(
    model,
    tokenizer,
    prompt: str,
    max_tokens: int,
    temp: float,
    top_p: float = 0.95,
    top_k: int = 20,
    min_p: float = 0.0,
    enable_thinking: bool = False,
) -> str:
    """
    Apply chat template, run inference, strip CoT tokens, return clean text.

    enable_thinking=True  → Qwen3.5 emits a <think>...</think> block before JSON.
                            Use for Writer and Corrector passes where deep reasoning
                            measurably improves output quality.
    enable_thinking=False → Direct response, faster. Use for structured extraction
                            passes (Analyzer, Validator) where speed matters more.

    Sampling params follow Qwen3.5 README recommendations:
      thinking  : temp=1.0, top_p=0.95, top_k=20, min_p=0.0
      no-think  : temp=0.7, top_p=0.8,  top_k=20, min_p=0.0
    """
    messages = [{"role": "user", "content": prompt}]
    try:
        token_ids = tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            enable_thinking=enable_thinking,
        )
    except TypeError:
        token_ids = tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True
        )
    prompt_str = tokenizer.decode(token_ids)
    raw = generate(
        model, tokenizer,
        prompt=prompt_str,
        max_tokens=max_tokens,
        sampler=make_sampler(temp=temp, top_p=top_p, top_k=top_k, min_p=min_p),
        verbose=False,
    )
    return _strip_thinking_tokens(raw)


def _run_pass(
    label: str,
    model,
    tokenizer,
    prompt: str,
    max_tokens: int,
    temp: float,
    model_cls: Type[T],
    max_retries: int,
    top_p: float = 0.95,
    top_k: int = 20,
    min_p: float = 0.0,
    enable_thinking: bool = False,
    debug_dir: Optional[Path] = None,
    debug_stem: Optional[str] = None,
) -> Optional[T]:
    """
    Run a single generation pass with retry logic.
    Saves raw LLM output to debug_dir on each attempt for post-mortem inspection.
    """
    mode = "thinking" if enable_thinking else "fast"
    for attempt in range(1, max_retries + 2):
        log.info("%s [%s] (attempt %d/%d) …", label, mode, attempt, max_retries + 1)
        raw = _generate(
            model, tokenizer, prompt, max_tokens, temp,
            top_p=top_p, top_k=top_k, min_p=min_p,
            enable_thinking=enable_thinking,
        )

        if debug_dir and debug_stem:
            raw_path = debug_dir / f"{debug_stem}_attempt{attempt}.txt"
            raw_path.write_text(raw, encoding="utf-8")

        result = parse_model(raw, model_cls)
        if result:
            log.info("%s succeeded on attempt %d.", label, attempt)
            return result
        log.warning("%s — parse failed on attempt %d.", label, attempt)

    log.error("%s — all attempts exhausted.", label)
    return None

# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------

def _section(title: str) -> None:
    print("\n" + "═" * 64)
    print(f"  {title}")
    print("═" * 64)


def run_pipeline(
    master_text: str,
    job_text: str,
    cfg: Config,
    debug_dir: Optional[Path] = None,
    skip_validation: bool = False,
) -> tuple[
    Optional[ResumeContent],
    Optional[JDAnalysis],
    Optional[ValidationReport],
]:
    """
    Execute the 4-pass pipeline.
    Returns (resume, jd_analysis, final_validation) — any member can be None.
    """
    analyzer_model_path = cfg.analyzer_model_path or cfg.model_path
    writer_model_path   = cfg.model_path

    # Load writer model (primary)
    log.info("Loading writer model: %s", writer_model_path)
    model, tokenizer = _load(writer_model_path)

    # Load analyzer model (may be the same)
    if analyzer_model_path != writer_model_path:
        log.info("Loading analyzer model: %s", analyzer_model_path)
        ana_model, ana_tokenizer = _load(analyzer_model_path)
    else:
        ana_model, ana_tokenizer = model, tokenizer

    # Sampler helpers — select top_p based on whether thinking is on or off
    def _top_p(thinking: bool) -> float:
        return cfg.top_p_thinking if thinking else cfg.top_p_fast

    # ── Pass 1: JD Analyzer ─────────────────────────────────────────────────
    _section("PASS 1 / 4 — Analyzing job description")
    jd_analysis: Optional[JDAnalysis] = _run_pass(
        label="Pass 1 · Analyzer",
        model=ana_model, tokenizer=ana_tokenizer,
        prompt=build_analyzer_prompt(job_text),
        max_tokens=cfg.analyzer_max_tokens,
        temp=cfg.analyzer_temp,
        top_p=_top_p(cfg.analyzer_thinking),
        top_k=cfg.top_k,
        min_p=cfg.min_p,
        enable_thinking=cfg.analyzer_thinking,
        model_cls=JDAnalysis,
        max_retries=cfg.max_json_retries,
        debug_dir=debug_dir,
        debug_stem="01_jd_analysis",
    )

    if not jd_analysis:
        log.error("JD analysis failed — cannot continue.")
        return None, None, None

    print(f"\n  Role       : {jd_analysis.role_type.upper()} / {jd_analysis.seniority} / {jd_analysis.domain}")
    if jd_analysis.company_name:
        print(f"  Company    : {jd_analysis.company_name}")
    # Deduplicate keywords (case-insensitive) before filtering
    seen: set = set()
    deduped: List[str] = []
    for kw in jd_analysis.ats_keywords:
        key = kw.strip().lower()
        if key not in seen:
            seen.add(key)
            deduped.append(kw)

    # Subset dedup: if keyword A is fully contained in keyword B, drop B (keep the shorter form).
    # E.g. "Python" subsumes "Python programming" — inserting both produces awkward repetition.
    subset_deduped: List[str] = []
    deduped_lower = [k.strip().lower() for k in deduped]
    for i, kw in enumerate(deduped):
        kl = deduped_lower[i]
        # Keep this keyword only if no shorter keyword is a substring of it
        subsumed = any(
            deduped_lower[j] != kl and deduped_lower[j] in kl
            for j in range(len(deduped_lower))
        )
        if not subsumed:
            subset_deduped.append(kw)

    if len(subset_deduped) < len(jd_analysis.ats_keywords):
        log.info("Deduplicated ATS keywords: %d → %d (case+subset)",
                 len(jd_analysis.ats_keywords), len(subset_deduped))
    jd_analysis.ats_keywords = subset_deduped

    # Filter JD keywords to only those achievable from the master resume
    achievable_kws, blocked_kws = _filter_achievable_keywords(jd_analysis.ats_keywords, master_text)
    jd_analysis.ats_keywords = achievable_kws

    print(f"  ATS keywords     : {len(achievable_kws)} achievable / {len(blocked_kws)} blocked")
    print(f"  Company signals  : {len(jd_analysis.company_signals)}")
    print(f"  Terminology maps : {len(jd_analysis.terminology_map)}")
    if blocked_kws:
        log.info("Blocked keywords (not in master resume): %s", ", ".join(blocked_kws))

    if debug_dir:
        p = debug_dir / "01_jd_analysis.json"
        p.write_text(jd_analysis.model_dump_json(indent=2), encoding="utf-8")
        log.info("Saved: %s", p)

    # ── Pass 1b: Semantic Inference ──────────────────────────────────────────
    inferable_kws: List[InferableKeyword] = []
    truly_blocked_kws: List[str] = blocked_kws

    if blocked_kws and not cfg.skip_inference:
        _section("PASS 1b — Semantic inference on blocked keywords")
        inference_result: Optional[InferenceResult] = _run_pass(
            label="Pass 1b · Inference",
            model=ana_model, tokenizer=ana_tokenizer,
            prompt=build_inference_prompt(blocked_kws, master_text, job_text, jd_analysis),
            max_tokens=cfg.inference_max_tokens,
            temp=0.1,   # determinism critical: same JD must always produce same inference
            top_p=_top_p(cfg.analyzer_thinking),
            top_k=cfg.top_k,
            min_p=cfg.min_p,
            enable_thinking=cfg.analyzer_thinking,
            model_cls=InferenceResult,
            max_retries=cfg.max_json_retries,
            debug_dir=debug_dir,
            debug_stem="01b_inference",
        )
        if inference_result:
            inferable_kws = inference_result.inferable
            truly_blocked_kws = inference_result.truly_blocked
            # Safety: any keyword the LLM dropped goes to truly_blocked
            accounted = {kw.keyword for kw in inferable_kws} | set(truly_blocked_kws)
            dropped = [kw for kw in blocked_kws if kw not in accounted]
            if dropped:
                log.warning("Inference dropped keywords — treating as truly_blocked: %s", dropped)
                truly_blocked_kws = list(truly_blocked_kws) + dropped
            print(f"\n  Inferable        : {len(inferable_kws)} promoted")
            print(f"  Truly blocked    : {len(truly_blocked_kws)}")
            for kw in inferable_kws:
                log.info("  Inferable: '%s' ← %s", kw.keyword, kw.grounding[:60])
            if debug_dir:
                p = debug_dir / "01b_inference.json"
                p.write_text(inference_result.model_dump_json(indent=2), encoding="utf-8")
                log.info("Saved: %s", p)
        else:
            log.warning("Inference pass failed — all blocked keywords treated as truly_blocked.")

    # ── Pass 2: Resume Writer ───────────────────────────────────────────────
    _section("PASS 2 / 4 — Writing tailored resume")
    resume_content: Optional[ResumeContent] = _run_pass(
        label="Pass 2 · Writer",
        model=model, tokenizer=tokenizer,
        prompt=build_writer_prompt(
            master_text, job_text, jd_analysis,
            blocked_keywords=truly_blocked_kws,
            inferable_keywords=inferable_kws,
        ),
        max_tokens=cfg.writer_max_tokens,
        temp=cfg.writer_temp,
        top_p=_top_p(cfg.writer_thinking),
        top_k=cfg.top_k,
        min_p=cfg.min_p,
        enable_thinking=cfg.writer_thinking,
        model_cls=ResumeContent,
        max_retries=cfg.max_json_retries,
        debug_dir=debug_dir,
        debug_stem="02_resume",
    )

    if not resume_content:
        log.error("Resume writing failed.")
        return None, jd_analysis, None

    print(f"\n  Bullets written  : {sum(len(j.points) for j in resume_content.experience)}")
    print(f"  Skill categories : {len(resume_content.skills_categories)}")
    print(f"  Certifications   : {len(resume_content.certifications)}")

    if debug_dir:
        p = debug_dir / "02_resume_content.json"
        p.write_text(resume_content.model_dump_json(indent=2), encoding="utf-8")
        log.info("Saved: %s", p)

    # ── Pass 3 & 4: Validate → Correct → Re-validate ───────────────────────
    final_validation: Optional[ValidationReport] = None

    if not skip_validation:
        _section("PASS 3 / 4 — Validating ATS coverage and quality")
        initial_validation: Optional[ValidationReport] = _run_pass(
            label="Pass 3 · Validator",
            model=model, tokenizer=tokenizer,
            prompt=build_validator_prompt(resume_content, jd_analysis, master_text, inferable_keywords=inferable_kws),
            max_tokens=cfg.validator_max_tokens,
            temp=cfg.validator_temp,
            top_p=_top_p(cfg.validator_thinking),
            top_k=cfg.top_k,
            min_p=cfg.min_p,
            enable_thinking=cfg.validator_thinking,
            model_cls=ValidationReport,
            max_retries=cfg.max_json_retries,
            debug_dir=debug_dir,
            debug_stem="03_validation_initial",
        )

        if initial_validation:
            initial_validation = _recompute_scores(initial_validation)

            # Post-Pass-3 code check: catch banned summary phrases the LLM validator may miss
            summary_lower = (resume_content.summary or "").lower()
            caught_phrases = [p for p in _BANNED_SUMMARY_PHRASES if p in summary_lower]
            if caught_phrases and not initial_validation.summary_feedback:
                initial_validation.summary_feedback = (
                    f"Summary contains banned generic phrase(s): {', '.join(caught_phrases)}. "
                    f"Rewrite to lead with specific value proposition."
                )
                log.info("Banned summary phrase(s) detected: %s", caught_phrases)

            missing_count = sum(1 for k in initial_validation.keyword_coverage
                                if not k.present and "NOT IN MASTER RESUME" not in (k.location or ""))
            weak_count    = sum(1 for b in initial_validation.bullet_ratings if b.score <= 3)
            print(f"\n  Initial score    : {initial_validation.overall_score}/100  "
                  f"(ATS: {initial_validation.ats_score}  Quality: {initial_validation.quality_score}  "
                  f"JD fit: {initial_validation.gap_score})")
            print(f"  Missing keywords : {missing_count}")
            print(f"  Weak bullets (≤3): {weak_count}")

            if debug_dir:
                p = debug_dir / "03_validation_initial.json"
                p.write_text(initial_validation.model_dump_json(indent=2), encoding="utf-8")

            # ── Pass 4: Corrector ────────────────────────────────────────────
            # Always correct if there are fixable issues (missing keywords, weak
            # bullets, fabrication flags, or summary feedback). The score threshold
            # only gates correction when there are literally no issues to fix.
            # Rationale: a score of 72 with 4 missing keywords should still be corrected.
            has_issues = (
                missing_count > 0
                or weak_count > 0
                or bool(initial_validation.summary_feedback)
                or bool(initial_validation.fabrication_flags)
            )
            needs_correction = has_issues

            if needs_correction:
                _section("PASS 4 / 4 — Correcting issues found in Pass 3")
                patch: Optional[CorrectedResume] = _run_pass(
                    label="Pass 4 · Corrector",
                    model=model, tokenizer=tokenizer,
                    prompt=build_corrector_prompt(
                        resume_content, initial_validation, jd_analysis, master_text
                    ),
                    max_tokens=cfg.corrector_max_tokens,
                    temp=cfg.corrector_temp,
                    top_p=_top_p(cfg.corrector_thinking),
                    top_k=cfg.top_k,
                    min_p=cfg.min_p,
                    enable_thinking=cfg.corrector_thinking,
                    model_cls=CorrectedResume,
                    max_retries=cfg.max_json_retries,
                    debug_dir=debug_dir,
                    debug_stem="04_correction",
                )

                if patch:
                    has_bullet_changes = bool(patch.summary) or len(patch.bullet_corrections) > 0
                    corrections_made = (
                        bool(patch.summary)
                        + len(patch.bullet_corrections)
                        + len(patch.skill_additions)
                        + len(patch.skill_removals)
                    )
                    print(f"\n  Corrections applied: {corrections_made}")
                    corrected_resume = apply_corrections(resume_content, patch)

                    if debug_dir:
                        p = debug_dir / "04_resume_corrected.json"
                        p.write_text(corrected_resume.model_dump_json(indent=2), encoding="utf-8")

                    # Re-validate to confirm improvement.
                    # Skip if corrector only touched skills (additions/removals can't regress
                    # bullet quality or ATS keyword coverage for existing achievable keywords).
                    # Use lightweight re-validator prompt — no master resume, no fabrication check.
                    if not has_bullet_changes:
                        log.info("Corrector made skill-only changes — skipping re-validation.")
                        resume_content = corrected_resume
                        final_validation = initial_validation
                    else:
                        _section("PASS 4b — Re-validating after corrections")
                        revalidation = _run_pass(
                            label="Pass 4b · Re-validator",
                            model=model, tokenizer=tokenizer,
                            prompt=build_revalidator_prompt(corrected_resume, jd_analysis, inferable_keywords=inferable_kws),
                            max_tokens=cfg.revalidator_max_tokens,
                            temp=cfg.validator_temp,
                            top_p=_top_p(cfg.validator_thinking),
                            top_k=cfg.top_k,
                            min_p=cfg.min_p,
                            enable_thinking=cfg.validator_thinking,
                            model_cls=ValidationReport,
                            max_retries=cfg.max_json_retries,
                            debug_dir=debug_dir,
                            debug_stem="04b_validation_final",
                        )

                        if revalidation:
                            revalidation = _recompute_scores(revalidation)
                            delta = revalidation.overall_score - initial_validation.overall_score
                            arrow = f"+{delta}" if delta >= 0 else str(delta)
                            print(f"\n  Final score      : {revalidation.overall_score}/100  ({arrow} from initial)")

                            # Revert if corrections made things worse (LLM scoring is noisy;
                            # use a 5-point buffer to absorb variance before reverting).
                            if revalidation.overall_score < initial_validation.overall_score - 5:
                                log.warning(
                                    "Corrections degraded score (%d → %d) — reverting to Pass 2 resume.",
                                    initial_validation.overall_score,
                                    revalidation.overall_score,
                                )
                                print(f"\n  ⚠  Score regressed — reverted to pre-correction resume.")
                                final_validation = initial_validation
                            else:
                                resume_content = corrected_resume
                                final_validation = revalidation
                        else:
                            # Re-validator failed — keep corrections anyway, use initial score
                            resume_content = corrected_resume
                            final_validation = initial_validation
                else:
                    log.warning("Corrector pass failed — keeping Pass 2 resume.")
                    final_validation = initial_validation
            else:
                _section(f"PASS 4 / 4 — Score {initial_validation.overall_score}/100 meets threshold — skipping corrector")
                final_validation = initial_validation

            if final_validation and debug_dir:
                p = debug_dir / "05_validation_final.json"
                p.write_text(final_validation.model_dump_json(indent=2), encoding="utf-8")

        else:
            log.warning("Initial validation failed — skipping correction pass.")

    return resume_content, jd_analysis, final_validation

# ---------------------------------------------------------------------------
# Validation report renderer
# ---------------------------------------------------------------------------

def render_validation_report(
    report: ValidationReport,
    analysis: JDAnalysis,
) -> str:
    present  = [k for k in report.keyword_coverage if k.present]
    missing  = [k for k in report.keyword_coverage if not k.present]
    coverage = len(present) / max(len(report.keyword_coverage), 1) * 100

    lines = [
        "# Resume Validation Report",
        "",
        f"**Overall Score   : {report.overall_score}/100**",
        f"**ATS Score       : {report.ats_score}/100**  "
        f"({len(present)}/{len(report.keyword_coverage)} keywords — {coverage:.0f}% coverage)",
        f"**Quality Score   : {report.quality_score}/100**",
        "",
        "---",
        "",
        "## ATS Keywords Present",
    ]
    if present:
        for k in present:
            lines.append(f"- `{k.keyword}` — *{k.location}*")
    else:
        lines.append("_None found._")

    if missing:
        lines += ["", "## Missing ATS Keywords"]
        for k in missing:
            lines.append(f"- `{k.keyword}` — Suggested: {k.location}")

    avg = sum(b.score for b in report.bullet_ratings) / max(len(report.bullet_ratings), 1)
    stars = {5: "⭐⭐⭐⭐⭐", 4: "⭐⭐⭐⭐", 3: "⭐⭐⭐", 2: "⭐⭐", 1: "⭐"}
    lines += [
        "",
        f"## Bullet Quality  (avg {avg:.1f}/5)",
        "",
    ]
    for b in sorted(report.bullet_ratings, key=lambda x: x.score):
        lines.append(f"- {stars.get(b.score, str(b.score))}  `{b.bullet_preview}…`")
        if b.feedback:
            lines.append(f"  > *{b.feedback}*")

    if report.fabrication_flags:
        lines += ["", "## Fabrication Flags"]
        for f in report.fabrication_flags:
            lines.append(f"- {f}")
    else:
        lines += ["", "## Fabrication Check: Clean"]

    lines += [
        "",
        "## Summary Feedback",
        report.summary_feedback or "_No issues identified._",
        "",
        "## Top Improvements",
    ]
    for i, tip in enumerate(report.top_improvements, 1):
        lines.append(f"{i}. {tip}")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# LaTeX / Pandoc rendering
# ---------------------------------------------------------------------------

_PANDOC_MD_TEMPLATE = r"""---
author: "{{ data.name }}"
address: "{{ data.contact_info }}"
geometry: "margin=0.6in"
params:
  fontsize: 11pt
---

{{ data.summary }}

# EXPERIENCE
{% for job in data.experience %}
\vspace{6pt}
\noindent \textbf{\large {{ job.company }}}
\newline
\noindent \textit{\small {{ job.title }}} \hfill {{ job.dates }}
\vspace{3pt}

{% for point in job.points %}
- {{ point }}
{% endfor %}

{% endfor %}

# SKILLS
{% for cat in data.skills_categories %}
\noindent \textbf{ {{- cat.label -}}: } {{ cat.items }}

{% endfor %}

{% if data.certifications %}
# CERTIFICATIONS
{% for cert in data.certifications %}
- {{ cert }}
{% endfor %}

{% endif %}
# EDUCATION
{% for edu in data.education %}
\vspace{6pt}
\noindent \textbf{ {{ edu.institution }} }
\newline
\noindent \textit{ {{ edu.degree }} }

{% endfor %}
"""


def _escape_resume_data(data: ResumeContent) -> ResumeContent:
    """Return a LaTeX-escaped copy of ResumeContent (never mutates the original)."""
    return ResumeContent(
        name=escape_latex(data.name),
        contact_info=escape_latex(data.contact_info),
        summary=escape_latex(data.summary),
        experience=[
            WorkExperience(
                company=escape_latex(job.company),
                title=escape_latex(job.title),
                dates=escape_latex(job.dates),
                points=[escape_latex(p) for p in job.points],
            )
            for job in data.experience
        ],
        skills_categories=[
            SkillCategory(
                label=escape_latex(cat.label),
                items=escape_latex(cat.items),
            )
            for cat in data.skills_categories
        ],
        certifications=[escape_latex(c) for c in data.certifications],
        education=[
            Education(
                institution=escape_latex(edu.institution),
                degree=escape_latex(edu.degree),
            )
            for edu in data.education
        ],
    )


def render_pandoc_markdown(data: ResumeContent) -> str:
    return Template(_PANDOC_MD_TEMPLATE).render(data=_escape_resume_data(data))


def convert_to_pdf(md_path: Path, pdf_path: Path, cfg: Config) -> bool:
    log.info("Converting to PDF via pandoc → %s", pdf_path.name)
    cmd = [
        "pandoc", str(md_path),
        "-o", str(pdf_path),
        f"--pdf-engine={cfg.pdf_engine}",
        "--from", "markdown+yaml_metadata_block+raw_tex",
    ]
    if Path(cfg.pandoc_template).exists():
        cmd.extend(["--template", cfg.pandoc_template])
    else:
        log.warning("Pandoc template '%s' not found — using defaults.", cfg.pandoc_template)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("pandoc failed:\n%s", result.stderr)
        return False
    log.info("PDF created: %s", pdf_path)
    return True

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="4-pass local LLM resume tailoring: Analyze → Write → Validate → Correct",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("output_name",      help="Base name for output files (no extension)")
    parser.add_argument("--master",         default="master_resume.md",    help="Master resume path")
    parser.add_argument("--job",            default="job_description.txt", help="Job description path")
    parser.add_argument("--model",          default=Config.model_path,     help="MLX model for writer/validator/corrector")
    parser.add_argument("--analyzer-model", default="",                    help="Separate model for analyzer pass (optional)")
    parser.add_argument("--no-pdf",         action="store_true",           help="Skip PDF conversion")
    parser.add_argument("--no-validate",    action="store_true",           help="Skip Pass 3-4 (faster)")
    parser.add_argument("--no-debug",       action="store_true",           help="Don't save intermediate files")
    parser.add_argument("--retries",        type=int, default=Config.max_json_retries,
                        help="Extra JSON parse retries per pass on failure")
    parser.add_argument("--writer-temp",    type=float, default=Config.writer_temp,
                        help="Generation temperature for the writer pass (0.0–1.0)")
    parser.add_argument("--min-score",      type=int, default=Config.min_score,
                        help="Warn if final ATS score is below this threshold (0-100)")
    parser.add_argument("--no-inference",   action="store_true",
                        help="Skip semantic inference pass — all blocked keywords remain blocked")
    parser.add_argument("--thinking",       action="store_true",
                        help="Enable Qwen3.5 thinking mode for the corrector pass. "
                             "Requires large token budgets — recommended for 32B+ models only. "
                             "On 9B, the think block exhausts the token budget before any output.")
    args = parser.parse_args()

    master_path, job_path = Path(args.master), Path(args.job)
    missing = [p for p in (master_path, job_path) if not p.exists()]
    if missing:
        for p in missing:
            log.error("File not found: %s", p)
        sys.exit(1)

    debug_dir: Optional[Path] = None
    if not args.no_debug:
        debug_dir = Path(f"{args.output_name}_debug")
        debug_dir.mkdir(exist_ok=True)
        log.info("Debug output directory: %s/", debug_dir)

    cfg = Config(
        model_path=args.model,
        analyzer_model_path=args.analyzer_model,
        max_json_retries=args.retries,
        writer_temp=args.writer_temp,
        min_score=args.min_score,
        skip_inference=args.no_inference,
        corrector_thinking=args.thinking,
        # Bump token budgets when thinking is enabled
        corrector_max_tokens=10000 if args.thinking else Config.corrector_max_tokens,
        corrector_temp=1.0 if args.thinking else Config.corrector_temp,
    )

    master_text = master_path.read_text(encoding="utf-8")
    job_text    = job_path.read_text(encoding="utf-8")

    resume_data, jd_analysis, validation = run_pipeline(
        master_text, job_text, cfg,
        debug_dir=debug_dir,
        skip_validation=args.no_validate,
    )

    if not resume_data:
        log.error("Pipeline failed — no resume generated.")
        sys.exit(1)

    # Write resume outputs
    out_md  = Path(f"{args.output_name}.md")
    out_pdf = Path(f"{args.output_name}.pdf")

    out_md.write_text(render_pandoc_markdown(resume_data), encoding="utf-8")
    log.info("Markdown saved: %s", out_md)

    if not args.no_pdf:
        if not convert_to_pdf(out_md, out_pdf, cfg):
            log.warning("PDF conversion failed. Markdown available at: %s", out_md)

    # Print and save validation report
    if validation and jd_analysis:
        report_md = render_validation_report(validation, jd_analysis)
        print("\n" + "═" * 64)
        print(report_md)
        print("═" * 64)
        if debug_dir:
            rp = debug_dir / "validation_report.md"
            rp.write_text(report_md, encoding="utf-8")
            log.info("Validation report saved: %s", rp)

        # Score gate
        if validation.overall_score < cfg.min_score:
            missing_kw = [k.keyword for k in validation.keyword_coverage if not k.present]
            print(f"\n{'!' * 64}")
            print(f"  WARNING: Final score {validation.overall_score}/100 is below threshold ({cfg.min_score}).")
            if missing_kw:
                print(f"  Still missing keywords: {', '.join(missing_kw)}")
            print(f"  Consider running again with --retries 3 or a larger model.")
            print(f"{'!' * 64}")
    elif not args.no_validate:
        log.warning("Validation pass failed — check debug output for raw LLM responses.")

    # Final summary
    print(f"\n  Resume      → {out_md}")
    if not args.no_pdf and out_pdf.exists():
        print(f"  PDF         → {out_pdf}")
    if debug_dir:
        print(f"  Debug       → {debug_dir}/")


if __name__ == "__main__":
    main()
