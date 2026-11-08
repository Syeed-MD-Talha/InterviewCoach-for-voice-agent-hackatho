# 🎙️ InterviewCoach — AI Voice Interviews That Hire

> **AssemblyAI Voice Agent Hackathon submission.** Meet **Talha**, the AI interviewer: employers create a role once and share one link — candidates interview by voice, get scored live, and passers receive auto-generated next-round passes. No scheduling, no phone screens, no gut feeling.

---

## ✨ What it does

**For employers (hiring dashboard, sign-in required)**
- Create roles with JD, topics, and pass score — interview links auto-generate
- AI-drafted question banks (Vultr DeepSeek, with role-aware local fallback) + voice dictation
- Live transcripts, per-answer scores, pass rates, and auto-issued next-round passes

**For candidates (one link, no account)**
- A real voice conversation with Talha — logo at rest, video avatar that listens while you talk and speaks back
- Live transcript, progress bar, per-answer scores with STAR-based tips
- Instant verdict — beat the bar and your personal next-round pass opens on the spot

---

## 🏗️ Architecture

```
Browser mic → AssemblyAI Voice Agent API (STT + LLM + TTS, barge-in)
                     ↓ tool.call events
              backend/interview_tools_server.py (port 8002)
                     ↓
              Question bank + heuristic scorer + auto passes
                     ↓ tool.result
              AssemblyAI → spoken coaching → browser speaker
```

**Zero pip dependencies for the core** — Python stdlib + SQLite only. The full voice pipeline runs inside AssemblyAI.

---

## 🚀 Quickstart

### 1. Configure keys

```sh
cp .env.example .env
```

Edit `.env`:

```
ASSEMBLYAI_API_KEY=your_key_here   # required — https://www.assemblyai.com/dashboard/api-keys
VULTR_API_KEY=...                  # optional — AI-drafted questions (local templates otherwise)
```

### 2. Start the tools server (terminal 1, from the project root)

```sh
python backend/interview_tools_server.py
```

### 3. Start the web server (terminal 2, from the project root)

```sh
python backend/server.py
```

### 4. Use it

| Page | URL | Access |
|---|---|---|
| Landing | `http://localhost:3000/` | public |
| Interview room | `http://localhost:3000/interview/<slug>` | link only |
| Next-round pass | `http://localhost:3000/onboard/<token>` | pass holders |
| Admin sign-in | `http://localhost:3000/login` | — |
| Hiring dashboard | `http://localhost:3000/admin` | signed in |

Create your admin account at `/login`, set up a role, copy the interview link, and talk to Talha.

---

## 🧠 How scoring works

Every answer is scored out of 10 on **length fit** (50–200 spoken words), **filler-word control**, and **STAR-structure bonus**, against a per-role pass threshold. Each question carries a *"what good looks like"* rubric so every candidate is judged the same way.

---

## 📁 Project structure

```
├── backend/                  # Python servers (stdlib only)
│   ├── server.py             # pages, auth, token minting, tool proxy, media
│   ├── interview_tools_server.py  # questions, scoring, passes (:8002)
│   └── database.py           # SQLite: users, roles, sessions, passes
├── templates/                # landing, interview room, admin, auth, pass
├── static/app.js             # browser voice client (WebSocket + audio worklets)
├── media/                    # logo + listening/speaking avatar videos
├── agents/interview-coach.jsonc  # voice agent definition (prompt, tools, voice)
├── .env.example              # copy to .env (never commit real keys)
└── requirements.txt          # stdlib only
```

The SQLite database (`interviewcoach.db`) is created and seeded automatically on first run — including demo roles.

---

## 🛠️ Voice agent tools

| Tool | When called | What it does |
|---|---|---|
| `get_question` | each round | next interview question for the role |
| `evaluate_answer` | after each answer | score + coaching tip |
| `get_session_summary` | at the end | average, verdict, tips, auto-issues pass link if passed |

Change the interviewer's voice anytime with one line in `agents/interview-coach.jsonc`:

```json
"voice": { "voice_id": "george" }
```

---

## 🙏 Built with

- [AssemblyAI Voice Agent API](https://www.assemblyai.com/docs/voice-agents/voice-agent-api) — real-time STT, managed LLM, TTS, turn detection
- Python 3.9+ standard library — zero installs for the core
- [Vultr Serverless Inference](https://www.vultr.com/products/serverless-inference/) (optional) — AI-drafted question banks via `deepseek-v4.1-flash-1017`
