# Worker packet and hand-back

A worker sees only its packet, so the packet is complete. A worker is used once: one packet,
one hand-back. New work goes to a fresh worker with the previous hand-back attached, not to a
worker that has drifted through a long history.

## Packet

```markdown
## Objective
<What to produce, in one or two sentences, and what it is for.>

## Context
<Only what the worker cannot find by itself: decisions already made, dead ends already tried.>

## Owned paths
<The files and folders this worker may write. Nobody else writes them meanwhile.>

## Off limits
<What it must not touch or do, each with the reason.>

## Acceptance checks
<Commands the sender will run again on the result, with the expected outcome.>

## Output
<Where the result is written.>

## Hand-back
<The format below, and its length limit.>
```

Size a packet to finish in one sitting. Larger work runs in stages: each stage has its own
acceptance check, the sender runs that check on what comes back, and the next stage goes to a
fresh worker.

## Hand-back

```markdown
Status: done | partial | blocked
Done: <what was produced, with paths>
Proof: <each acceptance check: the command and its result>
Concerns: <what looks wrong or fragile, also outside the task>
Deviations: <where the result differs from the packet, and why>
Not done: <what is left, and what blocked it>
```

## Rules for both sides

- The worker stops and reports when the packet is contradictory or a check cannot pass. It
  does not guess and does not work around a refusal.
- The worker does not start other workers.
- The sender treats the hand-back as a lead. It runs the acceptance checks itself before it
  builds on the result or reports it as done.
