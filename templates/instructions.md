# Instruction snippet

A block for an agent instruction file (`AGENTS.md`, `CLAUDE.md` or similar). Replace the
placeholders, and cut what your setup already says. Keep it short: every line here is paid for
in every session.

```markdown
## Working without being read

The person does not read session transcripts. What follows replaces that.

- Record. Work that outlives a turn is a task in <tracker>. The task holds the goal, a
  done-condition that can be checked, the holder and where the work is. State is written there
  and in git, not kept in the conversation.
- Evidence. Report "done" with its proof: the command and its result, a merged pull request, a
  file on disk. A claim without proof is reported as unverified. Run the acceptance check
  again yourself before reporting another agent's work as done. Run the change for real when
  you can; tests alone can pass while the feature is broken. Each commit carries a
  `Verified:` line: the check that ran, or `none`.
- Checking. A change that merges to the main branch, spans several files, or came from a
  cheaper model gets one check by an agent with a clean context before it is reported.
  Three rounds at most, each given the earlier findings; a check still failing after the
  third goes to the person. A verdict is for one commit. An agent's approval is never the
  person's.
- Escalate. Only three things reach the person: a decision only they can make, a review that
  needs their eye, and a notice of loss, failure or a stop. Each is a waiting task labelled
  `wait:decision`, `wait:review` or `wait:notice`, with one line after `blocker:`. Everything
  else you settle and record. Something said only in a session has not been told to them.
- Landing. A session ends with its task closed, reopened for the queue, or waiting. Never left
  in progress.
- Reconcile. A session that coordinates work starts with `<path>/bin/oversight reconcile` and
  acts on what it lists. When the person asks what needs them, answer with
  `<path>/bin/oversight digest`, not from memory.
- Learn. A problem the person found themselves becomes a check first (test, lint, hook), a
  tool second, a line in this file last.
```
