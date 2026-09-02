---
name: telegram-poll
description: "Send a native Telegram poll (sendPoll API) with 2-10 options. Use when: the user asks for a sondaggio, poll, survey, vote, /poll, or wants a quick group decision. NOT for: quizzes with correct answers (use a different type), message reactions, or non-Telegram contexts."
triggers: [sondaggio, poll, vota, vote, /poll, survey, "crea un sondaggio", "manda un sondaggio", "fai un sondaggio", "lanciamo un sondaggio"]
always: false
metadata:
  openvurp:
    emoji: "🗳️"
    requires:
      bins: [python3, curl]
---

# Telegram Poll (sendPoll)

Send a **native Telegram poll** in any chat the bot can write to. This is the
real Telegram UI poll (the one users see as a clickable card with vote bars
underneath), not a fake text question with reply numbers.

## When to use

✅ USE this skill when the user asks for a sondaggio/poll/vote in a Telegram
chat — even if they only say "fammi un sondaggio su X" or "chiedi al gruppo".

❌ DO NOT use this skill for: quizzes with a correct answer (Telegram supports
`type="quiz"` but the user didn't ask for that), emoji reactions, or
non-Telegram contexts. For a quick vote inside a 1-a-1 chat, ask first — polls
in DMs are usually awkward.

## What you need to gather

Before calling the API, make sure you have ALL of these. If something is
missing, ask the user — don't guess.

1. **chat_id** — where to post. Default: `config.TELEGRAM_ALLOWED_USERS[0]`
   (the owner's private chat) only if the user didn't say otherwise. If they
   said "nel gruppo", ask which one — never broadcast blindly. You can also
   read `memory/telegram_state.json` → `last_chat_id` for the most recent
   chat the bot talked to.
2. **question** — the poll text, 1–300 chars. Rephrase long user prompts.
3. **options** — list of 2–10 strings, each 1–100 chars. If the user gave
   fewer than 2, ask. If more than 10, pick the best 10 and say which you
   dropped.
4. **is_anonymous** — default `true` (standard). Switch to `false` only if the
   user explicitly wants visible voters.
5. **allows_multiple_answers** — default `false`. Set `true` only if the user
   says "multipla", "più risposte", or "checkbox".
6. **is_closed / close_date / open_period** — leave empty unless asked.

## The call

Use the Telegram Bot API method `sendPoll` directly with `curl` or `python3`.
Do **not** try to bend the `notify` tool into sending a poll — that tool only
does text and media. The token lives in `config.TELEGRAM_TOKEN`.

### With python3 (preferred — handles JSON cleanly)

```python
import json, os, requests, sys

token = os.environ.get("TELEGRAM_TOKEN") or open("config.py").read().split("TELEGRAM_TOKEN = ")[1].split("\n")[0].strip().strip('"').strip("'")
chat_id = sys.argv[1]
payload = {
    "chat_id": chat_id,
    "question": sys.argv[2],
    "options": json.loads(sys.argv[3]),   # JSON array, e.g. '["A","B","C"]'
    "is_anonymous": True,
    "allows_multiple_answers": False,
}
r = requests.post(f"https://api.telegram.org/bot{token}/sendPoll", json=payload, timeout=15)
print(r.json())
```

Run it like:

```bash
TELEGRAM_TOKEN="$TELEGRAM_TOKEN" python3 skills/telegram-poll.py "<chat_id>" "Domanda?" '["Sì","No","Forse"]'
```

### With curl (fallback, harder to read on errors)

```bash
curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendPoll" \
  -d "chat_id=${CHAT_ID}" \
  -d "question=Domanda?" \
  --data-urlencode "options=[\"Sì\",\"No\",\"Forse\"]"
```

## After the call

1. Parse the response. `ok=true` and a non-null `result.message_id` = success.
2. Report the poll URL to the user in plain prose: "🗳️ Inviato in <chat>: <url>".
3. If `ok=false`, read `description` and tell the user the real reason (often
   "chat not found", "not enough options", or "poll answers too long").

## Guardrails

- Always confirm the **target chat** before sending. The user said "qui" can
  mean both "in this 1-a-1" and "in our usual group" — pick based on context
  but say which you picked.
- Cap options at 10 (Telegram limit). If the user gave more, trim and tell
  them.
- Never store the token in the skill file. Read it from `config.py` /
  `TELEGRAM_TOKEN` env at call time.
- If the bot lacks `can_send_polls` in the target chat, Telegram returns 400.
  Tell the user to grant the permission and don't retry.
- In a 1-a-1 with the user, prefer asking if they really want a native poll
  there — usually a text question is enough.
