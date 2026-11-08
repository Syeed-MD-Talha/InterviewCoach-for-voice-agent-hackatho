"""Final-LLM marking for a completed interview.

The per-answer scores produced during the call are only a running guide. When
the interview ends, the whole conversation is sent to one final marking model
(Vultr first, then OpenAI, both OpenAI-compatible). That model returns a single
structured mark that the hiring admin sees on the dashboard.

If no LLM key is configured (or the call fails), a deterministic local mark is
built from the per-answer scores so an interview always has a result.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

MAX_TRANSCRIPT_CHARS = 14000
MAX_JD_CHARS = 2500

RECOMMENDATIONS = {"strong_hire", "hire", "borderline", "no_hire"}


# ---------------------------------------------------------------------------
# Env loading — the tools server can run as its own process, so it must not
# rely on server.py having populated os.environ.
# ---------------------------------------------------------------------------

def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$", line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2)
        if key in os.environ:
            continue
        os.environ[key] = re.sub(r"^(['\"])(.*)\1$", r"\2", raw)


# ---------------------------------------------------------------------------
# Conversation normalisation
# ---------------------------------------------------------------------------

def _as_conversation(value: Any) -> list[dict[str, str]]:
    """Accept [{role, content}], [{who, text}] or a plain string."""
    if isinstance(value, str):
        return [{"role": "user", "content": value}] if value.strip() else []
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            role = "user"
        elif isinstance(item, dict):
            text = str(item.get("content") or item.get("text") or item.get("said") or "").strip()
            raw_role = str(item.get("role") or item.get("who") or "user").lower()
            role = "assistant" if raw_role in ("assistant", "agent", "interviewer", "talha") else "user"
        else:
            continue
        if text:
            out.append({"role": role, "content": text})
    return out


def _conversation_from_answers(answers: list[dict[str, Any]]) -> list[dict[str, str]]:
    convo: list[dict[str, str]] = []
    for a in answers or []:
        question = str(a.get("question") or "").strip()
        answer = str(a.get("answer") or "").strip()
        if question:
            convo.append({"role": "assistant", "content": question})
        if answer:
            convo.append({"role": "user", "content": answer})
    return convo


def _merge_answers(conversation: list[dict[str, str]],
                   answers: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Guarantee every recorded answer reaches the marker.

    The browser transcript is normally complete, but a missed final transcript
    event must not hide an answer that evaluate_answer already recorded.
    """
    merged = list(conversation)
    seen = "\n".join(turn["content"] for turn in merged).lower()
    for a in answers or []:
        if not isinstance(a, dict):
            continue
        question = str(a.get("question") or "").strip()
        answer = str(a.get("answer") or "").strip()
        if not answer or answer.lower() in seen:
            continue
        if question:
            merged.append({"role": "assistant", "content": question})
        merged.append({"role": "user", "content": answer})
        seen += "\n" + answer.lower()
    return merged


def _transcript_text(conversation: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for turn in conversation:
        speaker = "Interviewer" if turn["role"] == "assistant" else "Candidate"
        lines.append(f"{speaker}: {turn['content']}")
    text = "\n\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[:MAX_TRANSCRIPT_CHARS] + "\n\n[... transcript truncated ...]"
    return text


def _question_rubric(role: dict[str, Any] | None) -> str:
    questions = (role or {}).get("questions") or []
    rows: list[str] = []
    for i, q in enumerate(questions, 1):
        if isinstance(q, str):
            rows.append(f"{i}. {q}")
            continue
        text = str(q.get("question") or "").strip()
        criteria = str(q.get("criteria") or "").strip()
        competency = str(q.get("competency") or "").strip()
        extra = []
        if competency:
            extra.append(f"competency: {competency}")
        if criteria:
            extra.append(f"what good looks like: {criteria}")
        rows.append(f"{i}. {text}" + (f" ({'; '.join(extra)})" if extra else ""))
    return "\n".join(rows) or "(no explicit question bank)"


# ---------------------------------------------------------------------------
# LLM call — Vultr then OpenAI (OpenAI-compatible chat completions)
# ---------------------------------------------------------------------------

def _chat_json(prompt: str, max_tokens: int = 1400) -> tuple[dict | None, str]:
    load_env()
    providers: list[tuple[str, str, str, list[str], dict]] = []

    vultr_key = os.environ.get("VULTR_API_KEY", "").strip()
    if vultr_key:
        models = [m.strip() for m in os.environ.get(
            "VULTR_MODEL", "deepseek-v4.1-flash-1017").split(",") if m.strip()]
        base = os.environ.get("VULTR_API_BASE", "https://api.vultrinference.com/v1").rstrip("/")
        extra = {}
        effort = os.environ.get("VULTR_REASONING_EFFORT", "").strip()
        if effort:
            extra["reasoning_effort"] = effort
        providers.append(("vultr", base + "/chat/completions", vultr_key, models, extra))

    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key:
        providers.append(("openai", "https://api.openai.com/v1/chat/completions",
                          openai_key, ["gpt-4o-mini"], {}))

    # Keep the whole marking attempt inside the agent's tool timeout so a
    # slow provider falls back to the local mark instead of stalling the call.
    deadline = time.monotonic() + 45
    for source, url, api_key, models, extra in providers:
        for model in models:
            remaining = deadline - time.monotonic()
            if remaining <= 2:
                return None, "local"
            try:
                payload: dict[str, Any] = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                }
                payload.update(extra)
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode(),
                    method="POST",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=min(25, remaining)) as res:
                    data = json.loads(res.read().decode())
                text = data["choices"][0]["message"]["content"]
                parsed = _extract_json(text)
                if parsed:
                    return parsed, source
            except Exception as err:  # noqa: BLE001 - try the next provider/model
                print(f"Final marking failed ({source}/{model}): {err}")
    return None, "local"


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    raw = text[start:end + 1] if start >= 0 and end > start else text
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# Normalisation of the model's mark
# ---------------------------------------------------------------------------

def _clamp_score(value: Any, default: float = 0.0) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = default
    return round(max(0.0, min(10.0, score)), 1)


def _string_list(value: Any, limit: int = 5) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _recommendation_for(score: float, threshold: float) -> str:
    if score >= min(10.0, threshold + 1.5):
        return "strong_hire"
    if score >= threshold:
        return "hire"
    if score >= max(0.0, threshold - 1.0):
        return "borderline"
    return "no_hire"


def _normalise_mark(raw: dict, threshold: float, source: str,
                    fallback_score: float) -> dict[str, Any]:
    score = _clamp_score(raw.get("overall_score", raw.get("score")), fallback_score)
    recommendation = str(raw.get("recommendation") or "").strip().lower()
    if recommendation not in RECOMMENDATIONS:
        recommendation = _recommendation_for(score, threshold)

    question_marks: list[dict[str, Any]] = []
    for item in raw.get("question_marks") or raw.get("answers") or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        comment = str(item.get("comment") or item.get("feedback") or "").strip()
        if question or comment:
            question_marks.append({
                "question": question,
                "score": _clamp_score(item.get("score"), score),
                "comment": comment,
            })

    return {
        "overall_score": score,
        "passed": score >= threshold,
        "pass_threshold": threshold,
        "recommendation": recommendation,
        "summary": str(raw.get("summary") or "").strip(),
        "strengths": _string_list(raw.get("strengths")),
        "improvements": _string_list(raw.get("improvements") or raw.get("top_tips")),
        "question_marks": question_marks,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Local fallback mark — deterministic, based on the per-answer scores
# ---------------------------------------------------------------------------

def _local_mark(answers: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    scored = [a for a in answers or [] if isinstance(a, dict)]
    scores = [float(a.get("score") or 0) for a in scored]
    avg = round(sum(scores) / len(scores), 1) if scores else 0.0

    question_marks = [{
        "question": str(a.get("question") or "").strip(),
        "score": _clamp_score(a.get("score")),
        "comment": str(a.get("feedback") or "").strip(),
    } for a in scored]

    improvements = _string_list([a.get("feedback") for a in scored if a.get("feedback")], 3)
    strengths = []
    for a in sorted(scored, key=lambda x: float(x.get("score") or 0), reverse=True):
        if float(a.get("score") or 0) >= 7:
            q = str(a.get("question") or "").strip()
            strengths.append(f"Strong answer on: {q}" if q else "A strong answer under pressure.")
        if len(strengths) >= 3:
            break

    recommendation = _recommendation_for(avg, threshold)
    return {
        "overall_score": avg,
        "passed": avg >= threshold,
        "pass_threshold": threshold,
        "recommendation": recommendation,
        "summary": (
            f"Local marking: the candidate answered {len(scored)} question(s) with an "
            f"average of {avg}/10 against a pass mark of {threshold}. "
            "Add VULTR_API_KEY or OPENAI_API_KEY for full conversational marking."
        ),
        "strengths": strengths,
        "improvements": improvements,
        "question_marks": question_marks,
        "source": "local",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def mark_conversation(role: dict[str, Any] | None,
                      conversation: Any,
                      answers: list[dict[str, Any]] | None = None,
                      threshold: float = 6.0,
                      role_name: str = "") -> dict[str, Any]:
    """Mark a whole interview conversation and return a structured result.

    The returned dict always has: overall_score, passed, recommendation,
    summary, strengths, improvements, question_marks and source.
    """
    answers = answers or []
    convo = _as_conversation(conversation)
    convo = _merge_answers(convo, answers) if convo else _conversation_from_answers(answers)
    fallback = _local_mark(answers, threshold)

    if not convo:
        return fallback

    name = role_name or (role or {}).get("name") or (role or {}).get("slug") or "this role"
    description = str((role or {}).get("description") or "").strip()
    jd_text = str((role or {}).get("jd_text") or "").strip()[:MAX_JD_CHARS]
    topics = ", ".join((role or {}).get("topics") or [])

    prompt = (
        "You are the final hiring assessor. A voice interview has finished. "
        "Read the full conversation and mark the candidate for the role.\n\n"
        "Return ONLY a valid JSON object with exactly these keys:\n"
        "{\n"
        '  "overall_score": number from 0 to 10 (one decimal allowed),\n'
        '  "recommendation": "strong_hire" | "hire" | "borderline" | "no_hire",\n'
        '  "summary": string (2-4 sentences, evidence-based, written for the hiring manager),\n'
        '  "strengths": [string, string, string],\n'
        '  "improvements": [string, string, string],\n'
        '  "question_marks": [{"question": string, "score": number, "comment": string}]\n'
        "}\n\n"
        "Marking rubric: relevance to the role and question, depth and specificity of "
        "examples, evidence and measurable results, STAR structure, clarity and "
        "communication. Be fair, consistent and honest. Do not invent anything the "
        "candidate did not say. The overall_score is the final mark the employer will "
        "see; it does not have to equal the average of the individual question marks.\n\n"
        f"Role: {name}\n"
        f"Pass mark: {threshold}/10\n"
        f"Role description: {description or '(none)'}\n"
        f"Topics: {topics or '(none)'}\n"
        f"Job description:\n{jd_text or '(none)'}\n\n"
        f"Question bank and rubric:\n{_question_rubric(role)}\n\n"
        f"Full interview conversation:\n{_transcript_text(convo)}"
    )

    raw, source = _chat_json(prompt)
    if not raw:
        return fallback
    return _normalise_mark(raw, threshold, source, fallback["overall_score"])
