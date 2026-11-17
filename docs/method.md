# The method

One person runs many coding agents: interactive sessions, background workers, scheduled runs,
across several repositories. That person no longer reads transcripts or the agents' reasoning.
Reading along used to be the last check. Without it, four things go wrong:

- an agent reports work as done that is not done;
- bad news is said once, in the closing message of a session nobody opens;
- the record of what is waiting and who holds what stops matching reality;
- work is dropped when a session ends, and nothing notices.

The method replaces reading along with six parts. Each one is small. Where a part rests on
someone else's published practice, [sources.md](sources.md) names it.

## Record: the tracker is the record, sessions are disposable

Every piece of work that outlives a turn is a task in one tracker. A task holds four things:
the goal in a sentence, a done-condition that can be checked, who holds it, and where the work
is (branch, worktree, pull request). Sessions are compacted, archived and killed; the tracker
and git are what survive, so state is written there and nowhere else.

A session ends by landing ([templates/landing.md](../templates/landing.md)): it writes back
what was done with its proof, what comes next and what was ruled out, commits work in progress
on a work branch, and releases the task or hands it over. A task left "in progress" by a
session that has ended is the most common way work gets lost.

A handoff file is written only when the successor needs narrative the task cannot hold. It has
six sections in a fixed order and points at files instead of repeating them
([templates/handoff.md](../templates/handoff.md)). The writing rules matter more than the
layout, because a handoff has one reader who acts on it at once:

- Two strengths only. A hard limit says so and gives its reason. Everything else is a plain
  statement. Soft words (should, ideally, try to) are left out: an option is written as a
  default with its exception.
- Every fact carries its level: verified, with the proof; unverified, with why; assumed, with
  how to check it.
- A trap is written with its trigger: "when X happens, do Y", not "watch out for X".
- Nothing points into a conversation the reader cannot see. Paths and dates are absolute.
- Emphasis is not force. No capitals, no repeated warnings. A stated reason holds better.

## Evidence: done is a claim with proof

"Done" is reported together with what proves it: the command and its output, a merged pull
request, a file on disk, a green run. A claim without proof is unverified, and is reported as
unverified. A worker's own report is a lead; the session that owns the task runs the
acceptance check again before it says done.

The strongest proof is the change running for real: the app driven end to end, the command
run against real data, the old system and the new one fed the same input. Tests the maker
wrote share the maker's assumptions and can pass while the feature is broken. Keep the way to
run the real thing in the repository, as a script or a skill, and keep it fast and reliable: a
slow or flaky check teaches agents to rerun it instead of fixing the code.

Who checks depends on the risk of the change, not on who made it:

- Low: reversible, local, covered by a test that ran. The maker's own check is enough.
- Medium: behaviour changes across several files, anything merged to the main branch, and
  anything produced by a cheaper model or a batch. A checker with a clean context looks at
  the result and the done-condition. It does not see the maker's reasoning, so it cannot
  inherit the maker's blind spots.
- High: irreversible or outward (publishing, deleting, spending, credentials, a security
  policy), or a matter of taste. The human decides.

An agent's approval never counts as the human's. Review has to converge: three rounds at
most, later rounds are given the earlier findings and look only at what changed, the checker
reports defects against the done-condition and not preferences, and the maker may disagree and
say why. A check that still fails after the third round goes to the human as a notice. A
checker asked to find problems always finds some; without these limits two agents can trade
comments all night. One checker that is told what matters in this code does better than a
stack of checkers, so write that down per repository and improve it when the checker misses.

A verdict belongs to one commit: when the change moves, the verdict is void. The check has to
live where the change cannot edit it, so a reviewer's rules and a guard's rule file are read
from the base branch, not from the change they judge. Where the platform runs the change's
own copy of the pipeline, as GitHub does with a pull request's workflow file, that part of
the change is read by someone other than its maker. Each commit says how it was verified,
`none` included, so unverified work can be found later instead of assumed.

A checker costs tokens and time. It pays where the task is beyond what the maker does reliably
alone, and is overhead elsewhere. That is why the tiers exist.

## Reconcile: compare the record with the world, by machine

The record drifts. A task keeps waiting on a pull request that merged weeks ago. A task stays
in progress under a session that ended. A scheduled CI run goes red and stays red. No session
notices, because each session trusts its own notes, and the human is not reading.

`oversight reconcile` compares what the tracker says with what is true and lists every
disagreement, with the evidence and a suggested action. It is read-only and deterministic.
Agents act on the list: close, reopen, requeue, or file a notice. The split is deliberate:
machines watch, agents act, and nothing is killed or reclaimed by a timer.

The checks:

- blocker gone: a waiting task names a pull request that has merged or closed;
- blocker stuck: a waiting task names a pull request that cannot merge as it is;
- silent wait: a waiting task that does not say what it is waiting for;
- quiet holder: a task in progress with no update for days;
- schedule red: a scheduled CI workflow whose latest runs failed;
- idle pull request: an open pull request that nobody has touched for days and no task is
  waiting on, whether it cannot merge, is still a draft, or is ready and was never merged;
- local only: commits on a local branch that exist on no remote, so one disk holds the work;
- guard off: a public repository whose local clone has no pre-push hook, so nothing checks
  what leaves the machine.

A probe that fails is reported as unchecked. It is never counted as fine. Run reconcile at the
start of any session that coordinates work, before every digest, and on a schedule if you
keep one.

## Escalate: three kinds of interruption, one inbox

Only three things may reach the human:

- a decision: a choice only they can make;
- a review: finished work whose acceptance needs their eye, with where to look;
- a notice: loss or exposure, a result they rely on that failed or could not be checked, or
  work that stopped and will not resume by itself.

Everything else the agents settle and record. Approach, naming, retries, cleanup, merges on
private repositories, coordination between agents: none of it is asked about and none of it is
reported step by step.

There is one inbox: a waiting task in the tracker, labelled with its kind, with one line that
says what is needed ([templates/escalation.md](../templates/escalation.md)). A closing message
in a session nobody opens is not the inbox, and neither is a chat channel, where items pile up
and get lost. An item is written so it can be answered without opening the session: what
happened, the recommended option, where to look.

The item is closed in the turn it is answered or resolved. Reconcile catches the ones nobody
closed. The human chooses when to look: `oversight digest` prints what is waiting, by kind and
age, in ten lines.

The bar is high on purpose. An inbox that fills with things the human always approves stops
being read, and then nothing reaches them at all. When nearly every item of some kind gets the
same answer, that kind stops being asked.

## Learn: every escape becomes a check

An escape is a problem the human found themselves, or one that reconcile found late. Each
escape is turned into the cheapest mechanism that would have caught it, in this order:

1. a check: a test, a lint, a hook, a new reconcile check;
2. a tool or script the agent can run;
3. one line in an instruction file.

Prose comes last because it is the weakest: an instruction competes with every other
instruction for attention, costs tokens in every session, and cannot be tested. Instruction
files therefore have a budget. A new line carries its reason and its date, a line the agent
keeps missing is promoted to a check, and the files are pruned on a regular pass.

A lesson is kept as three things: a trigger that can be read from a change, the check, and
the incident behind it. An agent then reads only the lessons whose trigger matches what it is
changing, and nobody has to remember past failures. One sighting is an anecdote: a pattern
becomes a rule once it has been seen twice and can be recognised from a diff. A new gate
starts in observe mode, where it only logs what it would have blocked, and is switched to
enforce after its false alarms have been read.

Not reading has a measured cost: the more agent work merges without review, the more
corrective work follows. So the human, or an agent on their behalf, samples merged work now
and then, and the count of escapes, reverts and incidents decides how much may go unreviewed.
When that count rises, more changes move up a risk tier. Checkers are measured the same way:
what they missed, which of their findings a human rejected, and a sample of changes that
stayed clean, so that tuning a checker does not turn into flagging everything.

## Split work only where it pays

More agents are not more intelligence. They buy parallelism and fresh eyes, at a price in
tokens and in errors that spread between agents that do not check each other.

- One writer per set of files. Two agents editing the same files overwrite each other.
- Fan out for reading, research and checking, and for pieces that are truly independent.
- Put a check between the workers and the result: the session that hands out the work
  verifies what comes back before building on it.
- A worker gets a complete packet ([templates/worker-packet.md](../templates/worker-packet.md)):
  the objective, the paths it owns, the acceptance checks, where to write the result. It is
  used once and returns what it did, the proof, its concerns and where it deviated.
- Design and review go to the strongest model, mechanical and checkable work to a cheaper one.
- Keep few lanes in flight. Past a handful, the coordinator's own checking gets worse.

If one agent already does the task well, adding agents makes the result worse and the bill
larger.

## Limits

This method does not remove the human. It moves them from reading everything to deciding the
few things only they can decide, and it is honest about what that costs. Teams that stopped
looking at the code altogether have reported painful reversals. A checker shares many of the
maker's blind spots when both are the same model. Reconcile sees only what it has a check for.
Treat the risk tiers and the sampling as dials, and turn them when the escapes say so.
