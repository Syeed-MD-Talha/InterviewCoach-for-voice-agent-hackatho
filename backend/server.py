#!/usr/bin/env python3
"""InterviewCoach browser server.

    python backend/server.py   (from the workspace root)

Serves the interview coach UI. The API key stays in this process;
the page only gets 60-second tokens. Interview tool calls are proxied
to interview_tools_server.py on port 8002.

Based on the AssemblyAI voice-agent-starter browser server.
"""

import copy
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent  # backend/
ROOT = HERE.parent  # workspace root
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"

# ---------------------------------------------------------------------------
# Minimal copies of lib.py helpers (no external deps)
# ---------------------------------------------------------------------------

def _load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    import re
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


def _aai(path: str, method: str = "GET", body=None) -> dict:
    api_key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    base = os.environ.get("AGENTS_API_BASE", "https://agents.assemblyai.com/v1")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as res:
            text = res.read().decode()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"{method} {path} failed ({err.code}): {err.read().decode()}") from None


def _publish_or_get_agent() -> dict:
    """Publish agents/interview-coach.jsonc or reuse if it already exists."""
    import re

    agent_file = ROOT / "agents" / "interview-coach.jsonc"
    if not agent_file.exists():
        sys.exit("agents/interview-coach.jsonc not found. Run from the hackathon workspace root.")

    # Parse JSONC (strip // comments and /* */ blocks)
    text = agent_file.read_text(encoding="utf-8")
    out = []
    in_string = escaped = in_line = in_block = False
    i = 0
    while i < len(text):
        c = text[i]
        n = text[i + 1] if i + 1 < len(text) else ""
        if in_line:
            if c == "\n":
                in_line = False
                out.append(c)
            i += 1
            continue
        if in_block:
            if c == "*" and n == "/":
                in_block = False
                i += 1
            i += 1
            continue
        if in_string:
            out.append(c)
            escaped = (c == "\\") if not escaped else False
            if c == '"' and not escaped:
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and n == "/":
            in_line = True
            i += 2
            continue
        if c == "/" and n == "*":
            in_block = True
            i += 2
            continue
        if c in "}]":
            while out and out[-1].isspace():
                out.pop()
            if out and out[-1] == ",":
                out.pop()
        out.append(c)
        i += 1
    agent = json.loads("".join(out))

    # Check for existing stored ID
    stored_id = os.environ.get("AGENT_ID") or os.environ.get("AGENT_ID_INTERVIEWCOACH", "")

    if stored_id:
        try:
            _aai(f"/agents/{stored_id}", method="PUT", body=agent)
            print(f'Updated "{agent["name"]}" (id: {stored_id})')
            return {"id": stored_id, "name": agent["name"]}
        except RuntimeError:
            print(f"Agent {stored_id} not found, creating a new one.")

    # Try to reuse by name
    try:
        existing_agents = _aai("/agents").get("agents", [])
        existing = next((a for a in existing_agents if a.get("name") == agent.get("name")), None)
        if existing:
            _aai(f"/agents/{existing['id']}", method="PUT", body=agent)
            _save_agent_id(existing["id"])
            print(f'Reused "{agent["name"]}" (id: {existing["id"]})')
            return {"id": existing["id"], "name": agent["name"]}
    except RuntimeError:
        pass

    # Create new
    result = _aai("/agents", method="POST", body=agent)
    _save_agent_id(result["id"])
    print(f'Created "{agent["name"]}" (id: {result["id"]})')
    return {"id": result["id"], "name": agent["name"]}


def _save_agent_id(agent_id: str) -> None:
    env_file = ROOT / ".env"
    os.environ["AGENT_ID_INTERVIEWCOACH"] = agent_id
    try:
        text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        import re
        key = "AGENT_ID_INTERVIEWCOACH"
        line = f"{key}={agent_id}"
        pattern = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(line, text, count=1)
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += line + "\n"
        env_file.write_text(text, encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Interview tools proxy — forwards /tool requests to port 8002
# ---------------------------------------------------------------------------

INTERVIEW_TOOLS_URL = "http://localhost:8002/tool"


def _local_tool(body: bytes):
    """Run a tool call in-process.

    The tools server is a separate process, but its handlers are plain
    functions over stdlib + SQLite. Calling them directly means the interview
    still works when only this server is running — no second terminal, no
    connection-refused dead end mid-interview.
    """
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return 400, json.dumps({"error": "invalid JSON"}).encode()
    tool = data.get("tool", "")
    args = data.get("arguments", {}) or {}
    try:
        import interview_tools_server as tools
        tools.init_db()
        tools.seed_default_roles()
        handlers = {
            "get_question": tools.handle_get_question,
            "evaluate_answer": tools.handle_evaluate_answer,
            "get_session_summary": tools.handle_get_session_summary,
        }
        handler = handlers.get(tool)
        if not handler:
            return 404, json.dumps({"error": f"unknown tool: {tool}"}).encode()
        return 200, json.dumps(handler(args)).encode()
    except Exception as err:  # noqa: BLE001 - surface any failure to the agent
        print(f"In-process tool {tool} failed: {err}")
        return 500, json.dumps({"error": f"tool failed: {err}"}).encode()


def _proxy_tool(body: bytes):
    """Forward a tool call to interview_tools_server.py, falling back in-process."""
    req = urllib.request.Request(
        INTERVIEW_TOOLS_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        # Final marking calls an LLM and can take a while; allow enough time
        # so the fallback path never re-runs (and double-charges) the mark.
        with urllib.request.urlopen(req, timeout=60) as res:
            return 200, res.read()
    except urllib.error.HTTPError as err:
        # 502/503/504 mean the tools server is down (or a proxy can't reach it),
        # not that the tool itself failed — serve it here instead.
        if err.code in (502, 503, 504):
            return _local_tool(body)
        return err.code, err.read()
    except OSError:
        # Tools server not running (or not reachable): serve it here instead.
        return _local_tool(body)


# ---------------------------------------------------------------------------
# JD/topics -> question bank generator (local heuristic + optional LLM)
# ---------------------------------------------------------------------------

def _generate_questions_local(role_name: str, jd_text: str, topics: list, count: int = 5) -> list[dict]:
    """Role-aware fallback used when no LLM API key is set.

    Picks a role family (engineer, qa, designer, marketing, data, pm, general),
    then builds a behavioral/situational/technical mix: one tailored question per
    topic plus family-specific fillers. Variant selection is hashed on
    (role, topic) so output is stable per role but differs across roles.
    """
    import hashlib
    import re

    role = (role_name or "this role").strip()
    topics = [t.strip() for t in (topics or []) if str(t).strip()][:8]
    blob = f"{role}\n{jd_text or ''}\n{', '.join(topics)}".lower()

    def family() -> str:
        checks = [
            ("qa", ["qa", "quality assurance", "tester", "test "]),
            ("designer", ["design", "ux", "ui ", "product design"]),
            ("marketing", ["market", "seo", "content", "brand", "growth", "social media"]),
            ("data", ["data", "ml ", "machine learning", "analytics", "scientist"]),
            ("pm", ["product manager", "product owner", "program manager", "project manager"]),
            ("engineer", ["engineer", "developer", "software", "devops", "backend", "frontend",
                          "full-stack", "full stack", "mobile", "sde"]),
        ]
        for name, keys in checks:
            if any(k in blob for k in keys):
                return name
        return "general"

    fam = family()

    def pick(variants: list, salt: str) -> int:
        h = hashlib.md5(f"{role}|{salt}".encode()).hexdigest()
        return int(h, 16) % len(variants)

    # Distinctive JD keywords (skip generic hiring filler).
    jd_keywords: list[str] = []
    if jd_text:
        words = re.findall(r"[A-Za-z][A-Za-z0-9+#./\-]{2,}", jd_text)
        stop = {"and", "the", "for", "with", "you", "your", "our", "will", "have", "has",
                "from", "that", "this", "role", "team", "teams", "work", "working",
                "experience", "experienced", "ability", "strong", "help", "including",
                "ensure", "across", "about", "como", "candidate", "looking", "join",
                "joining", "year", "years", "plus", "etc", "who", "are", "job",
                "responsibilities", "requirements", "qualifications", "skills",
                "knowledge", "tools", "using", "use", "used", "day", "new", "key"}
        seen: set[str] = set()
        freq: dict[str, int] = {}
        for w in words:
            lw = w.lower().rstrip("s") if len(w) > 4 else w.lower()
            if lw not in stop:
                freq[lw] = freq.get(lw, 0) + 1
        for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0])):
            if w not in seen:
                seen.add(w)
                jd_keywords.append(w)
            if len(jd_keywords) >= 8:
                break

    # Per-topic templates — several phrasings so roles/topics don't repeat.
    topic_openers = [
        "Tell me about a time you dealt with {topic} as a {role}. What happened?",
        "Walk me through a real situation where {topic} mattered in your work as a {role}.",
        "Describe a challenging {topic} problem you've faced. How did you approach it?",
        "Give me an example of {topic} work you're proud of. What was your contribution?",
    ]
    topic_criteria = [
        "Specific {topic} example, personal actions, measurable outcome (STAR).",
        "Depth on {topic}: trade-offs considered, result achieved, lesson learned.",
    ]

    # Family-specific pools: (question, criteria, competency).
    pools: dict[str, list[tuple[str, str, str]]] = {
        "engineer": [
            ("How do you debug a production issue you can't reproduce locally?",
             "Structured approach: logs, metrics, bisecting, hypothesis testing.", "debugging"),
            ("Walk me through how you'd design a high-traffic service or feature for scale.",
             "Requirements, components, trade-offs, bottlenecks named.", "system design"),
            ("How do you keep code quality high on a fast-moving team?",
             "Concrete practices: reviews, tests, tooling — plus an example.", "code quality"),
            ("Tell me about a technical disagreement with a teammate and how it resolved.",
             "Listening, evidence used, outcome without blame.", "collaboration"),
            ("Describe the most complex system you built or contributed to.",
             "Architecture clarity, personal scope, hardest challenge.", "experience"),
        ],
        "qa": [
            ("How do you decide what to test when time is short before a release?",
             "Risk-based prioritization, coverage reasoning, what gets cut.", "test strategy"),
            ("Walk me through how you'd test a new feature end to end before release.",
             "Test plan: happy path, edge cases, negative cases, automation split.", "test planning"),
            ("Tell me about the trickiest bug you ever found. How did you track it down?",
             "Investigation steps, reproduction, root cause, reporting quality.", "debugging"),
            ("How do you write a bug report developers actually thank you for?",
             "Repro steps, expected vs actual, environment, severity reasoning.", "communication"),
            ("How do you balance manual testing with automation on your team?",
             "Criteria for automating, ROI thinking, real example.", "automation"),
        ],
        "designer": [
            ("Walk me through your design process from brief to delivery.",
             "Structured process, research input, iteration, handoff.", "design process"),
            ("Tell me about a time user research changed your design direction.",
             "What you learned, what changed, measured impact.", "user research"),
            ("How do you handle stakeholder feedback that conflicts with user needs?",
             "Balancing pushback with evidence, resolution story.", "stakeholders"),
            ("Describe your hardest design project and how you worked through it.",
             "Constraints named, decisions justified, outcome.", "craft"),
            ("How do you know a design succeeded after it shipped?",
             "Metrics defined, before/after, follow-up actions.", "measurement"),
        ],
        "marketing": [
            ("Tell me about a campaign you ran end to end. What was the outcome?",
             "Goal, channels, budget thinking, measured result.", "campaigns"),
            ("How do you market a product with almost no budget?",
             "Creativity, prioritization, organic/earned tactics.", "resourcefulness"),
            ("How do you measure whether a campaign worked?",
             "Metrics, attribution approach, decisions driven by data.", "analytics"),
            ("Tell me about a time data forced you to pivot a campaign mid-flight.",
             "Signal spotted, change made, result.", "experimentation"),
            ("How do you keep up with how fast marketing channels change?",
             "Learning habits plus a recent example applied.", "growth"),
        ],
        "data": [
            ("Walk me through an ML project you took from data to deployment.",
             "Pipeline completeness: data, modeling, validation, serving, monitoring.", "end-to-end ML"),
            ("How do you handle missing or imbalanced data?",
             "Concrete techniques and why each was chosen.", "data quality"),
            ("Tell me about a model that worked offline but failed in production.",
             "Diagnosis, drift/leakage thinking, fix.", "deployment"),
            ("How do you explain a complex model result to non-technical stakeholders?",
             "Clarity, analogy or visual, decision enabled.", "communication"),
            ("Describe a recommendation you made under uncertainty.",
             "Confidence communicated honestly, outcome tracked.", "judgment"),
        ],
        "pm": [
            ("Tell me about a product you launched and how you defined success.",
             "Goal, metrics, outcome, honest retrospective.", "ownership"),
            ("How do you prioritize between competing features with limited capacity?",
             "Framework used, trade-offs, stakeholder alignment.", "prioritization"),
            ("How would you improve our onboarding if you joined tomorrow?",
             "User empathy, quick wins vs bets, success measures.", "product sense"),
            ("Tell me about a product call you got wrong and what you learned.",
             "Ownership, lesson, behavior change since.", "judgment"),
            ("How do you balance tech debt against new features with engineering?",
             "Shared language with eng, sequencing logic, example.", "execution"),
        ],
        "general": [
            ("Tell me about yourself and why this {role} role fits you.",
             "Relevant background tied to the role, motivation.", "background"),
            ("Describe a significant work challenge and how you handled it.",
             "Ownership, actions, result — STAR structure.", "problem solving"),
            ("Tell me about delivering a project with a cross-functional team.",
             "Role clarity, friction handled, outcome.", "teamwork"),
            ("What is your proudest professional achievement and why?",
             "Specific impact, personal contribution.", "achievement"),
            ("Where do you see yourself growing in the next few years?",
             "Direction plus how this role fits it.", "motivation"),
        ],
    }
    pool = pools[fam]

    out: list[dict] = []
    seen_q: set[str] = set()

    def add(question: str, criteria: str, competency: str) -> None:
        key = question.strip().lower()
        if key and key not in seen_q:
            seen_q.add(key)
            out.append({"question": question.strip(), "criteria": criteria,
                        "competency": competency})

    # 1) One tailored question per topic (varied phrasing per role+topic).
    for topic in topics:
        if len(out) >= count:
            break
        opener = topic_openers[pick(topic_openers, "opener:" + topic)]
        crit = topic_criteria[pick(topic_criteria, "crit:" + topic)].format(topic=topic)
        add(opener.format(topic=topic, role=role), crit, topic)

    # 2) Family-specific fillers, rotated by role so banks differ.
    start = pick(pool, "pool-start")
    for offset in range(len(pool)):
        if len(out) >= count:
            break
        q, c, comp = pool[(start + offset) % len(pool)]
        add(q.format(role=role), c, comp)

    # JD keywords enrich the first technical criteria instead of being
    # spliced into question text (which read awkwardly).
    if jd_keywords and out:
        out[0]["criteria"] += f" Role context: {', '.join(jd_keywords[:4])}."

    # 3) Rare fallback: keyword-driven question if still short.
    if len(out) < count and jd_keywords:
        add(f"How much hands-on experience do you have with {jd_keywords[0]}? Give a concrete example.",
            f"Depth on {jd_keywords[0]} beyond buzzwords; specific story.", jd_keywords[0])

    return out[:count]


def _generate_questions_openai(role_name: str, jd_text: str, topics: list, count: int) -> list[dict] | None:
    """Use GPT-4o-mini when OPENAI_API_KEY is set. Returns None on any failure."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None
    return _generate_questions_llm(role_name, jd_text, topics, count,
                                    "https://api.openai.com/v1/chat/completions",
                                    api_key, ["gpt-4o-mini"])


def _generate_questions_llm(role_name: str, jd_text: str, topics: list, count: int,
                            base_url: str, api_key: str, models: list[str],
                            extra: dict | None = None) -> list[dict] | None:
    """Shared OpenAI-compatible chat caller (used for Vultr and OpenAI).

    Tries each model in order; returns None if all fail.
    """
    prompt = (
        "You design structured job interviews. Return ONLY valid JSON: a list of objects with "
        "keys question, criteria, competency. Rules: one skill per question, open-ended, "
        "behavioral/situational/technical mix, no multi-part questions, max "
        f"{count} items.\nRole: {role_name}\nTopics: {', '.join(topics)}\n"
        f"Job description:\n{(jd_text or '')[:3000]}"
    )
    for model in models:
        try:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.6,
                "max_tokens": 1200,
            }
            if extra:
                payload.update(extra)
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                base_url, data=body, method="POST",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as res:
                data = json.loads(res.read().decode())
            text = data["choices"][0]["message"]["content"]
            # Extract JSON array even if wrapped in fences
            start, end = text.find("["), text.rfind("]")
            items = json.loads(text[start:end + 1] if start >= 0 and end > start else text)
            out = []
            for it in items[:count]:
                q = str(it.get("question", "")).strip()
                if q:
                    out.append({"question": q,
                                "criteria": str(it.get("criteria", "")),
                                "competency": str(it.get("competency", ""))})
            if out:
                return out
        except Exception as err:
            print(f"LLM generation failed ({model}): {err}")
    return None


def _generate_questions_vultr(role_name: str, jd_text: str, topics: list, count: int) -> list[dict] | None:
    """Draft via Vultr Serverless Inference (OpenAI-compatible API)."""
    api_key = os.environ.get("VULTR_API_KEY", "")
    if not api_key:
        return None
    models = [m.strip() for m in os.environ.get(
        "VULTR_MODEL", "deepseek-v4.1-flash-1017").split(",") if m.strip()]
    base_url = os.environ.get("VULTR_API_BASE", "https://api.vultrinference.com/v1").rstrip("/")
    extra = {}
    effort = os.environ.get("VULTR_REASONING_EFFORT", "low").strip()
    if effort:
        extra["reasoning_effort"] = effort
    return _generate_questions_llm(role_name, jd_text, topics, count,
                                    base_url + "/chat/completions",
                                    api_key, models, extra)


def _generate_questions(role_name: str, jd_text: str, topics: list, count: int) -> tuple[list[dict], str]:
    count = max(1, min(8, int(count or 5)))
    vultr = _generate_questions_vultr(role_name, jd_text, topics, count)
    if vultr:
        return vultr, "vultr"
    ai = _generate_questions_openai(role_name, jd_text, topics, count)
    if ai:
        return ai, "openai"
    return _generate_questions_local(role_name, jd_text, topics, count), "local"


def _slugify(name: str) -> str:
    """Turn a role name into a URL-safe slug: 'Junior QA Engineer' -> 'junior-qa-engineer'."""
    import re
    import unicodedata
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "role"


def _unique_slug(name: str) -> str:
    """Slugify + numeric suffix until free: 'designer', 'designer-2', …"""
    from database import get_role
    base = _slugify(name)
    slug, n = base, 2
    while get_role(slug):
        slug = f"{base}-{n}"
        n += 1
    return slug


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

AGENT = None
PAGE = ""


AUTH_COOKIE = "ic_admin"


def _get_cookie(headers, name: str) -> str:
    raw = headers.get("Cookie", "") or ""
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k.strip() == name:
                return v.strip()
    return ""


def _current_admin(handler) -> dict | None:
    try:
        from database import get_user_by_token
        token = _get_cookie(handler.headers, AUTH_COOKIE)
        if not token:
            return None
        return get_user_by_token(token)
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: bytes, content_type: str, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path == "/token":
            try:
                token = _aai("/token?product=voice_agent&expires_in_seconds=60")
                self._send(200, json.dumps(token).encode(), "application/json")
            except RuntimeError as err:
                print(err)
                self._send(502, b'{"error":"token request failed"}', "application/json")
            return

        if path == "/agent":
            try:
                agent = _aai(f"/agents/{AGENT['id']}")
                # Scrub write-only fields
                agent_copy = copy.deepcopy(agent)
                for tool in agent_copy.get("tools", []):
                    for header in tool.get("http", {}).get("headers", []):
                        header["value"] = "<hidden>"
                self._send(200, json.dumps(agent_copy).encode(), "application/json")
            except RuntimeError as err:
                print(err)
                self._send(502, b'{"error":"could not load agent"}', "application/json")
            return

        if path == "/app.js":
            # Serve the adapted app.js that routes tools to port 8002
            self._send(200, (STATIC / "app.js").read_bytes(), "text/javascript")
            return

        if path.startswith("/media/"):
            # Whitelisted brand/interviewer media only — no path traversal.
            name = path[len("/media/"):].strip().strip("/")
            allowed = {"logo.png": "image/png",
                       "listening.mp4": "video/mp4",
                       "speaking.mp4": "video/mp4"}
            if name in allowed:
                media_file = ROOT / "media" / name
                if media_file.exists():
                    self._send(200, media_file.read_bytes(), allowed[name])
                    return
            self._send(404, b"media not found", "text/plain")
            return

        if path == "/login":
            login_file = TEMPLATES / "auth.html"
            if login_file.exists():
                self._send(200, login_file.read_bytes(), "text/html")
            else:
                self._send(404, b"auth.html not found", "text/plain")
            return

        if path == "/api/auth/me":
            user = _current_admin(self)
            if user:
                self._send(200, json.dumps({"user": user}).encode(), "application/json")
            else:
                self._send(401, b'{"error":"not signed in"}', "application/json")
            return

        if path == "/admin":
            user = _current_admin(self)
            if not user:
                self._redirect("/login")
                return
            self._send(200, (TEMPLATES / "admin.html").read_bytes(), "text/html")
            return

        if path.startswith("/admin/"):
            self._handle_admin(path)
            return

        if path.startswith("/onboard/"):
            token = path[len("/onboard/"):].strip().strip("/")
            page = _build_onboard_page(token)
            if page is None:
                self._send(404, b"Next-round pass not found. Check the link and try again.",
                            "text/plain")
            else:
                self._send(200, page.encode(), "text/html")
            return

        if path.startswith("/interview/"):
            role_slug = path[len("/interview/"):].strip().strip("/")
            if not role_slug:
                page = _build_landing_page()
                self._send(200, page.encode(), "text/html")
                return
            page = _build_interview_page(role_slug)
            self._send(200, page.encode(), "text/html")
            return

        # Default: public landing page with open roles
        try:
            landing = _build_landing_page()
        except Exception:
            landing = PAGE
        self._send(200, landing.encode(), "text/html")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError:
            return {}

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path == "/tool":
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length) if length else b"{}"
            status, data = _proxy_tool(body)
            self._send(status, data, "application/json")
            return

        if path == "/api/auth/signup":
            from database import create_auth_token, create_user, init_db
            init_db()
            data = self._read_json_body()
            try:
                # Open registration: anyone with the link can create an admin
                # account and manage their own hiring dashboard.
                user = create_user(data.get("email", ""), data.get("password", ""),
                                   data.get("name", ""))
                token = create_auth_token(user["id"])
                cookie = f"{AUTH_COOKIE}={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={14*24*3600}"
                self._send(201, json.dumps({"user": user}).encode(), "application/json",
                            {"Set-Cookie": cookie})
            except ValueError as err:
                self._send(400, json.dumps({"error": str(err)}).encode(), "application/json")
            return

        if path == "/api/auth/login":
            from database import create_auth_token, init_db, verify_user
            init_db()
            data = self._read_json_body()
            user = verify_user(data.get("email", ""), data.get("password", ""))
            if not user:
                self._send(401, b'{"error":"Invalid email or password."}', "application/json")
                return
            token = create_auth_token(user["id"])
            cookie = f"{AUTH_COOKIE}={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={14*24*3600}"
            self._send(200, json.dumps({"user": user}).encode(), "application/json",
                        {"Set-Cookie": cookie})
            return

        if path == "/api/auth/logout":
            from database import delete_auth_token
            delete_auth_token(_get_cookie(self.headers, AUTH_COOKIE))
            self._send(200, b'{"ok":true}', "application/json",
                        {"Set-Cookie": f"{AUTH_COOKIE}=; HttpOnly; Path=/; Max-Age=0"})
            return

        if path.startswith("/admin/"):
            self._handle_admin(path)
            return

        self._send(404, b'{"error":"not found"}', "application/json")

    def do_PUT(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/admin/"):
            self._handle_admin(path)
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/admin/"):
            self._handle_admin(path)
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def _handle_admin(self, path: str) -> None:
        from database import create_role, delete_role, get_role, list_roles, list_sessions, update_role

        user = _current_admin(self)
        if not user:
            self._send(401, b'{"error":"Please sign in at /login"}', "application/json")
            return

        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send(400, b'{"error":"invalid JSON"}', "application/json")
            return

        if path == "/admin/generate-questions" and self.command == "POST":
            topics = data.get("topics", [])
            if isinstance(topics, str):
                topics = [t.strip() for t in topics.replace(";", ",").split(",") if t.strip()]
            questions, source = _generate_questions(
                str(data.get("role_name", "")),
                str(data.get("jd_text", "")),
                topics,
                int(data.get("count", 5) or 5),
            )
            self._send(200, json.dumps({"questions": questions, "source": source}).encode(),
                        "application/json")
            return

        if path == "/admin/roles" and self.command == "GET":
            roles = list_roles(owner_id=user["id"], mine_only=True)
            self._send(200, json.dumps({"roles": roles}).encode(), "application/json")
            return

        if path == "/admin/roles" and self.command == "POST":
            try:
                # Interview link slug auto-generates from the role name so the
                # admin never types a URL. Collisions get a numeric suffix.
                if not str(data.get("slug", "")).strip():
                    data["slug"] = _unique_slug(str(data.get("name", "")))
                data["owner_id"] = user["id"]
                role = create_role(data)
                self._send(201, json.dumps(role).encode(), "application/json")
            except Exception as err:
                self._send(400, json.dumps({"error": str(err)}).encode(), "application/json")
            return

        if path.startswith("/admin/roles/"):
            slug = path[len("/admin/roles/"):]
            if self.command == "GET":
                role = get_role(slug)
                if role and (role.get("owner_id") is None or role.get("owner_id") == user["id"]):
                    self._send(200, json.dumps(role).encode(), "application/json")
                else:
                    self._send(404, b'{"error":"role not found"}', "application/json")
                return
            if self.command == "PUT":
                role = update_role(slug, data, owner_id=user["id"])
                if role:
                    self._send(200, json.dumps(role).encode(), "application/json")
                else:
                    self._send(404, b'{"error":"role not found"}', "application/json")
                return
            if self.command == "DELETE":
                if delete_role(slug, owner_id=user["id"]):
                    self._send(204, b"", "application/json")
                else:
                    self._send(404, b'{"error":"role not found"}', "application/json")
                return

        if path.startswith("/admin/sessions/") and self.command == "GET":
            from database import get_session
            raw_id = path[len("/admin/sessions/"):].strip().strip("/")
            try:
                session_id = int(raw_id)
            except ValueError:
                self._send(400, b'{"error":"invalid session id"}', "application/json")
                return
            session = get_session(session_id, owner_id=user["id"])
            if not session:
                self._send(404, b'{"error":"session not found"}', "application/json")
                return
            self._send(200, json.dumps({"session": session}).encode(), "application/json")
            return

        if path == "/admin/sessions" and self.command == "GET":
            sessions = list_sessions(owner_id=user["id"], mine_only=True)
            self._send(200, json.dumps({"sessions": sessions}).encode(), "application/json")
            return

        if path == "/admin/passes" and self.command == "GET":
            from database import list_onboarding_passes
            role_slug = data.get("role_slug") if isinstance(data, dict) else None
            passes = list_onboarding_passes(role_slug, owner_id=user["id"], mine_only=True)
            self._send(200, json.dumps({"passes": passes}).encode(),
                        "application/json")
            return

        self._send(404, b'{"error":"not found"}', "application/json")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:
        pass

    def handle_error(self, request, client_address) -> None:
        # Harmless noise: browsers and Render's probes routinely drop idle
        # keep-alive connections mid-read. Don't dump tracebacks for those.
        import socket
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, socket.timeout)):
            return
        super().handle_error(request, client_address)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _read_utf8(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _build_interview_page(role_slug: str) -> str:
    """Render the interview UI for a specific role slug."""
    from database import get_role

    role = get_role(role_slug)
    role_name = role["name"] if role else role_slug.replace("-", " ").title()
    role_desc = (role["description"] if role else "") or ""
    questions_total = len(role["questions"]) if role else 5
    index_html = TEMPLATES / "index.html"
    page = (
        _read_utf8(index_html)
        .replace("{{AGENT_NAME}}", AGENT["name"])
        .replace("{{AGENT_JSON}}", json.dumps(AGENT).replace("<", "\\u003c"))
        .replace("{{ROLE_SLUG}}", role_slug)
        .replace("{{ROLE_NAME}}", role_name)
        .replace("{{ROLE_DESC}}", role_desc)
        .replace("{{TOTAL_QUESTIONS}}", str(questions_total))
    )
    return page


def _build_landing_page() -> str:
    """Render the public careers landing page."""
    landing_html = TEMPLATES / "landing.html"
    if landing_html.exists():
        return _read_utf8(landing_html).replace("{{AGENT_NAME}}", AGENT["name"])
    return PAGE


def _build_onboard_page(token: str) -> str | None:
    """Render the auto-generated next-round pass. None if token unknown."""
    from database import get_onboarding_pass, get_role
    import html
    p = get_onboarding_pass(token)
    if not p:
        return None
    tpl = TEMPLATES / "onboard.html"
    if not tpl.exists():
        return None
    page = _read_utf8(tpl)
    role = get_role(p["role_slug"])
    custom_url = (role["onboarding_url"] if role else "") or ""
    name = p["candidate_name"] or "Candidate"
    if custom_url:
        next_step = "Tap the button below to book your next step."
        button = (f'<a class="btn" href="{html.escape(custom_url)}" target="_blank" rel="noopener">'
                  f'Book next step →</a>')
    else:
        next_step = "We'll reach out shortly at your interview email."
        button = ""
    return (page
            .replace("{{ROLE_NAME}}", html.escape(p["role_name"] or p["role_slug"]))
            .replace("{{CANDIDATE_NAME}}", html.escape(name))
            .replace("{{SCORE}}", html.escape(str(p["average_score"])))
            .replace("{{TOKEN}}", html.escape(p["token"]))
            .replace("{{DATE}}", html.escape(str(p["created_at"] or "")))
            .replace("{{NEXT_STEP}}", html.escape(next_step))
            .replace("{{CUSTOM_BUTTON}}", button))


def main() -> None:
    global AGENT, PAGE
    _load_env()
    try:
        from database import init_db, seed_default_roles
        init_db()
        seed_default_roles()
    except Exception as err:
        print(f"DB init warning: {err}")

    api_key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    if not api_key:
        sys.exit("Missing ASSEMBLYAI_API_KEY. Add it to .env")

    AGENT = _publish_or_get_agent()
    # Belt-and-suspenders: a stale stored ID (deleted dashboard agent or a
    # key swap) must never be served to browsers — verify, else publish fresh.
    try:
        _aai(f"/agents/{AGENT['id']}")
    except RuntimeError:
        print(f"Agent {AGENT['id']} missing at startup, publishing a fresh one.")
        os.environ.pop("AGENT_ID", None)
        os.environ.pop("AGENT_ID_INTERVIEWCOACH", None)
        AGENT = _publish_or_get_agent()
    print(f"Agent ID: {AGENT['id']}")

    # Build the landing page (public careers site). Interview rooms are
    # rendered per-role from index.html via _build_interview_page().
    landing_html = TEMPLATES / "landing.html"
    index_html = TEMPLATES / "index.html"
    if not index_html.exists():
        sys.exit("templates/index.html not found. Run from the hackathon workspace root, e.g. python backend/server.py")
    if landing_html.exists():
        PAGE = _read_utf8(landing_html).replace("{{AGENT_NAME}}", AGENT["name"])
    else:
        PAGE = (
            _read_utf8(index_html)
            .replace("{{AGENT_NAME}}", AGENT["name"])
            .replace("{{AGENT_JSON}}", json.dumps(AGENT).replace("<", "\\u003c"))
            .replace("{{ROLE_SLUG}}", "")
            .replace("{{ROLE_NAME}}", AGENT["name"])
            .replace("{{ROLE_DESC}}", "")
            .replace("{{TOTAL_QUESTIONS}}", "5")
        )

    fixed = os.environ.get("PORT")
    port = int(fixed) if fixed else 3000
    while True:
        try:
            server = ThreadingHTTPServer(("", port), Handler)
            break
        except OSError:
            if fixed or port >= 3010:
                raise
            port += 1

    print(f"\nTalk to your InterviewCoach: http://localhost:{port}")
    print(f"Admin panel: http://localhost:{port}/admin")
    print("Make sure the tools server is also running (python backend/interview_tools_server.py)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
