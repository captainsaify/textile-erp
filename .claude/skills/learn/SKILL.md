---
name: learn
description: Teach today's 30-45 minute codebase session, following LEARN.md and tracking progress in docs/learn/progress.md. Use when the user says "teach me today's session", "teach me", "next session", "/learn", or asks to understand any part of this codebase.
---

# Teach today's session

Teaching is a conversation, so this runs **in the current conversation**
— never as a subagent. A subagent finishes and reports; it cannot ask
"explain that back to me" and wait for the answer, which is the part
that makes the session work.

## Do this

1. Read `.claude/agents/codebase-tutor.md`. That file holds the whole
   teaching method — who he is, the shape of a session, and the rules.
   It is the single source of truth; this file only says *where* to run.
2. Read `docs/learn/progress.md` to find which session is next.
3. Read `LEARN.md` for that session's question and files.
4. Teach it here, talking to him directly, one message at a time. Stop
   and let him answer when you ask him something — do not ask a question
   and then answer it yourself in the same message.
5. Update `docs/learn/progress.md` when the session ends.

## The two rules worth repeating here

- **If he cannot explain it back, that is your failure.** Teach it
  again a different way. Never move on to stay on schedule.
- **Never explain a file you have not read in this session.** Line
  numbers drift. Read it, then cite it.
