"""InterviewCoach tools server.

Handles three tools called by the AssemblyAI voice agent:
  - get_question    → returns the next interview question for a role
  - evaluate_answer → scores an answer with local heuristics (live coaching)
  - get_session_summary → marks the whole conversation with the final LLM

The managed LLM inside AssemblyAI handles dialogue and coaching feedback.
Live per-answer scores use the local heuristic math (word count, fillers,
STAR); the official candidate mark comes from marking.py at session end.

Runs on port 8002. The browser forwards client-side tool.call events here.

Usage:
    python backend/interview_tools_server.py   (from the workspace root)
"""

import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from database import get_role, init_db, list_roles, save_session, seed_default_roles
from marking import mark_conversation

# ---------------------------------------------------------------------------
# Question bank — realistic questions per role category
# ---------------------------------------------------------------------------

QUESTION_BANK: dict[str, list[str]] = {
    "software engineer": [
        "Tell me about a time you had to debug a particularly tricky production issue. How did you approach it?",
        "Describe a situation where you disagreed with a technical decision made by your team. How did you handle it?",
        "Walk me through how you would design a URL shortening service like bit.ly.",
        "Tell me about the most complex system you have built or contributed to. What were the biggest challenges?",
        "How do you ensure the quality of your code, and can you give me an example of a time that process caught a serious bug?",
    ],
    "product manager": [
        "Tell me about a product you launched. How did you define success, and how did it go?",
        "Describe a time you had to prioritise between multiple competing features. How did you decide?",
        "How would you improve our onboarding experience if you joined tomorrow?",
        "Tell me about a time a product decision you made turned out to be wrong. What did you learn?",
        "How do you balance technical debt against new feature development when working with engineering?",
    ],
    "data scientist": [
        "Walk me through a machine learning project you built end to end, from data to deployment.",
        "How do you handle missing or imbalanced data in a dataset?",
        "Tell me about a time your model worked well in development but underperformed in production.",
        "How would you explain a complex model result to a non-technical stakeholder?",
        "Describe a situation where you had to make a recommendation under uncertainty. How did you communicate the confidence level?",
    ],
    "designer": [
        "Walk me through your design process from brief to final delivery.",
        "Tell me about a time user research changed the direction of your design significantly.",
        "How do you handle feedback from stakeholders that conflicts with what users actually need?",
        "Describe your most challenging design project. What made it hard, and how did you work through it?",
        "How do you measure whether a design is successful after it ships?",
    ],
    "default": [
        "Tell me about yourself and why you are applying for this role.",
        "Describe a time you faced a significant challenge at work. How did you handle it?",
        "Tell me about a time you worked in a cross-functional team to deliver a project.",
        "What is your greatest professional achievement and why?",
        "Where do you see yourself in the next three to five years?",
    ],
}

FILLER_WORDS = {"um", "uh", "like", "you know", "basically", "literally", "right", "actually", "kind of", "sort of"}

# ---------------------------------------------------------------------------
# In-memory session state
# ---------------------------------------------------------------------------

# Per-interview session keyed by a client-supplied session_id.
# session = { role_slug: str, role_name: str, answers: [...] }
SESSIONS: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Scoring logic — local heuristics, no external API needed
# ---------------------------------------------------------------------------

def _count_fillers(text: str) -> int:
    count = 0
    lower = text.lower()
    for word in FILLER_WORDS:
        count += len(re.findall(r"\b" + re.escape(word) + r"\b", lower))
    return count


def _word_count(text: str) -> int:
    return len(text.split())


def score_answer(answer: str, question: str, role: str = "") -> dict:
    """Score a spoken interview answer using local heuristics."""
    wc = _word_count(answer)
    fillers = _count_fillers(answer)

    # Length score: 50–200 words is ideal for a spoken answer
    if wc < 20:
        length_score = 3
        length_tip = "Your answer was very short. Aim for at least a minute of speaking."
    elif wc < 50:
        length_score = 5
        length_tip = "Try to develop your answer a bit more with a specific example."
    elif wc <= 200:
        length_score = 9
        length_tip = "Good length — that is about the right depth for a spoken answer."
    else:
        length_score = 7
        length_tip = "That was quite long. Try to be more concise and lead with the key point."

    # Filler score
    if fillers == 0:
        filler_score = 10
        filler_tip = ""
    elif fillers <= 2:
        filler_score = 8
        filler_tip = f"You used {fillers} filler word(s) — that is fine, just keep an eye on it."
    elif fillers <= 5:
        filler_score = 6
        filler_tip = f"You used {fillers} filler words. Replacing them with a brief pause sounds more confident."
    else:
        filler_score = 4
        filler_tip = f"You used {fillers} filler words — try pausing instead of filling silence."

    # STAR structure bonus
    star_keywords = ["situation", "task", "action", "result", "when i", "i decided", "the outcome", "as a result"]
    star_count = sum(1 for kw in star_keywords if kw in answer.lower())
    structure_bonus = min(star_count, 2)

    overall = round((length_score * 0.5 + filler_score * 0.5) + structure_bonus)
    overall = max(1, min(10, overall))

    tips = [t for t in [length_tip, filler_tip] if t]
    if star_count < 2:
        tips.append("Try using the STAR structure: Situation, Task, Action, Result.")

    return {
        "score": overall,
        "feedback": tips[0] if tips else "Solid answer. Keep that up.",
        "filler_count": fillers,
        "word_count": wc,
    }


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _get_session(args: dict) -> dict:
    """Return or create a per-interview session keyed by session_id."""
    session_id = args.get("session_id", "default")
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {
            "role_slug": "",
            "role_name": "",
            "answers": [],
            "candidate_name": "",
            "candidate_email": "",
        }
    session = SESSIONS[session_id]
    # Keep candidate details up to date — the browser sends them with every
    # tool call after the pre-join screen.
    if args.get("candidate_name"):
        session["candidate_name"] = args.get("candidate_name", "")
    if args.get("candidate_email"):
        session["candidate_email"] = args.get("candidate_email", "")
    return session


def handle_get_question(args: dict) -> dict:
    session = _get_session(args)
    role_slug = args.get("role", "").strip().lower()
    question_number = int(args.get("question_number", 1))

    role = get_role(role_slug)
    criteria = ""
    competency = ""
    if role:
        session["role_slug"] = role["slug"]
        session["role_name"] = role["name"]
        bank = role["questions"]
        total = len(bank)
    else:
        # Fallback to legacy keyword matching if no DB role is found.
        session["role_slug"] = role_slug
        session["role_name"] = role_slug
        bank = QUESTION_BANK.get("default")
        for key in QUESTION_BANK:
            if key != "default" and key in role_slug:
                bank = QUESTION_BANK[key]
                break
        total = 5

    idx = (question_number - 1) % len(bank)
    item = bank[idx]
    if isinstance(item, dict):
        question = item.get("question", "")
        criteria = item.get("criteria", "")
        competency = item.get("competency", "")
    else:
        question = item

    return {
        "question_number": question_number,
        "criteria": criteria,
        "competency": competency,
        "question": question,
        "total_questions": total,
    }


def handle_evaluate_answer(args: dict) -> dict:
    session = _get_session(args)
    question = args.get("question", "")
    answer = args.get("answer", "")
    role = args.get("role", session.get("role_slug", "professional"))

    if not answer.strip():
        return {"error": "No answer transcript received.", "score": 0}

    result = score_answer(answer, question, role)

    session["answers"].append({
        "question": question,
        "answer": answer,
        "score": result["score"],
        "feedback": result["feedback"],
    })

    return {
        "score": result["score"],
        "feedback": result["feedback"],
        "filler_count": result.get("filler_count", 0),
        "word_count": result.get("word_count", 0),
        "answers_completed": len(session["answers"]),
    }


def handle_get_session_summary(args: dict) -> dict:
    session = _get_session(args)
    role_slug = session.get("role_slug") or args.get("role", "professional")
    role_name = session.get("role_name") or role_slug
    answers = session["answers"]

    if not answers:
        return {"error": "No answers recorded in this session."}

    scores = [a["score"] for a in answers]
    heuristic_avg = round(sum(scores) / len(scores), 1)

    role = get_role(role_slug)
    threshold = role["pass_threshold"] if role else 6.0
    # Legacy custom link (optional now). The auto-generated pass is primary.
    custom_url = (role["onboarding_url"] if role else "") or ""

    candidate_name = session.get("candidate_name") or args.get("candidate_name")
    candidate_email = session.get("candidate_email") or args.get("candidate_email")

    # The browser sends the complete transcript. That whole conversation is
    # handed to the final marking model, whose score becomes the official mark.
    conversation = args.get("conversation") or session.get("conversation") or []
    mark = mark_conversation(
        role, conversation, answers, threshold, role_name=role_name)
    avg_score = mark.get("overall_score", heuristic_avg)
    passed = bool(mark.get("passed", avg_score >= threshold))

    # Collect improvement tips from the final mark, falling back to per-answer
    # coaching feedback when the marking model returned none.
    all_tips = [a["feedback"] for a in answers if a.get("feedback")]
    unique_tips = (mark.get("improvements") or list(dict.fromkeys(all_tips)))[:3]

    # Persist the result (with candidate details, transcript and final mark).
    saved_session_id = save_session(
        role_slug, answers, avg_score, passed,
        candidate_name=candidate_name, candidate_email=candidate_email,
        conversation=conversation, mark=mark)

    # Auto-generate the next-round pass link for passed candidates.
    # The browser turns onboarding_path into an absolute URL, so this works
    # on localhost and any deployed host without configuration.
    onboarding_token = ""
    onboarding_path = ""
    if passed:
        try:
            from database import create_onboarding_pass
            onboarding_pass = create_onboarding_pass(
                role_slug, role_name, avg_score,
                candidate_name=candidate_name, candidate_email=candidate_email)
            onboarding_token = onboarding_pass["token"]
            onboarding_path = onboarding_pass["path"]
        except Exception as err:
            print(f"onboarding pass creation failed: {err}")

    # Reset in-memory session for next run
    session_id = args.get("session_id", "default")
    SESSIONS[session_id] = {"role_slug": "", "role_name": "", "answers": [],
                            "candidate_name": "", "candidate_email": ""}

    return {
        "session_id": saved_session_id,
        "role": role_name,
        "role_slug": role_slug,
        "questions_answered": len(answers),
        "average_score": avg_score,
        "pass_threshold": threshold,
        "passed": passed,
        "onboarding_token": onboarding_token,
        "onboarding_path": onboarding_path,
        "onboarding_url": onboarding_path or custom_url,
        "custom_onboarding_url": custom_url,
        "top_tips": unique_tips,
        "strengths": mark.get("strengths", []),
        "recommendation": mark.get("recommendation", ""),
        "mark_source": mark.get("source", ""),
        "mark": mark,
        "summary": mark.get("summary") or (
            f"You answered {len(answers)} questions for the {role_name} role. "
            f"Your final mark was {avg_score} out of 10."
        ),
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._send_json(200, {"ok": True, "sessions": SESSIONS})
        elif path == "/roles":
            self._send_json(200, {"roles": list_roles()})
        elif path.startswith("/roles/"):
            slug = path[len("/roles/"):]
            role = get_role(slug)
            if role:
                self._send_json(200, role)
            else:
                self._send_json(404, {"error": "role not found"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode() if length else "{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
            return

        path = self.path.split("?")[0]

        # Direct endpoint calls (e.g. /get_question)
        if path == "/get_question":
            self._send_json(200, handle_get_question(data))
        elif path == "/evaluate_answer":
            self._send_json(200, handle_evaluate_answer(data))
        elif path == "/get_session_summary":
            self._send_json(200, handle_get_session_summary(data))

        # Client-side tool dispatch — browser sends { tool, arguments }
        elif path == "/tool":
            tool = data.get("tool", "")
            args = data.get("arguments", {})
            if tool == "get_question":
                self._send_json(200, handle_get_question(args))
            elif tool == "evaluate_answer":
                self._send_json(200, handle_evaluate_answer(args))
            elif tool == "get_session_summary":
                self._send_json(200, handle_get_session_summary(args))
            else:
                self._send_json(404, {"error": f"unknown tool: {tool}"})
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, *args) -> None:
        pass  # quiet by default


def main() -> None:
    init_db()
    seed_default_roles()
    port = 8002
    server = ThreadingHTTPServer(("", port), Handler)
    print(f"InterviewCoach tools server running on http://localhost:{port}")
    print("  Scoring: local heuristics (word count, filler words, STAR structure)")
    print("  LLM & voice: AssemblyAI Voice Agent API — no OpenAI key needed")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
