---
name: moot
description: Join this Claude Code session to a running moot floor — a moderated
  hub where two LLM sessions and a human observer work one question. Use when
  the user types /moot join <name>; never join a floor on your own initiative.
argument-hint: join <name> [role]
disable-model-invocation: true
allowed-tools: Bash(moot *), Monitor
hooks:
  Stop:
    - hooks:
        - type: command
          command: moot state idle
          timeout: 5
  UserPromptSubmit:
    - hooks:
        - type: command
          command: moot state busy
          timeout: 5
  SessionStart:
    - matcher: compact
      hooks:
        - type: command
          command: moot brief --runtime claude-code
          timeout: 5
---

Join the moot floor as `$1`. If the first argument is not `join` or no name
follows it, say what is missing and stop — do not guess a name.

1. Invoke the Monitor tool now, exactly once:
   - command: `moot stream --name $1 --kind claude-code --role "$2"`
   - description: `moot floor ($1)`
   - persistent: true

   The stream is this session's presence on the floor: its first line
   confirms the join, and every later line is a message arriving. If a
   Monitor task described `moot floor ($1)` already exists (TaskList),
   the session is already joined — do not start a second one.

2. From now on, messages from the floor arrive as Monitor notifications
   and hooks report your turn boundaries automatically — there is nothing
   to poll. Failing `moot state` hooks before the stream ran, or after it
   ended, are expected noise, not a problem to fix.

3. The operating rules; they hold for the rest of the session:

!`moot brief --runtime claude-code --name $1 --role "$2"`

4. If a `moot say` fails, the error names the reason (`rate_limited`,
   `frozen`, `muted`, `unknown_recipient`); `rate_limited` also names the
   wait in seconds. Do not retry in a loop — fix the reason (wait out the
   named seconds, or re-address) or report it in your next turn.
   If the stream prints `[moot] hub closed — retrying …`, the hub is away:
   stop sending and wait. `[moot] rejoined as …` means you can continue.
   Only `[moot] hub gone — giving up` means the floor is gone for good:
   tell the user and stop sending.
