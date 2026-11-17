# Landing: how a session ends

A session that stops without landing leaves work nobody can find. Run this list at the end of
any session that touched a task, and before any handoff.

1. Run the acceptance check for what you are about to claim. Keep the command and its result.
2. Commit work in progress on a work branch, and push it where your rules allow. Nothing
   stays only in the working tree, and nothing stays only on this machine without a reason.
   Each commit says how it was verified: a `Verified:` line with the command or the run, or
   `Verified: none`.
3. Write back to the task:
   - what was done, with the proof;
   - the next action, as something that can be run;
   - what was ruled out, and why;
   - where the work is: branch, worktree path, pull request.
4. Set the task's state. It is one of:
   - closed, with the result as the first sentence;
   - open again, with no holder, so the queue can pick it up;
   - waiting, with its kind and one line saying what is needed ([escalation.md](escalation.md)).
   A task is never left in progress under a session that is ending.
5. File what the human has to know as inbox items. Something said only in this session has not
   been told to them.
6. If the successor needs narrative, write the handoff file ([handoff.md](handoff.md)) and
   link it from the task.
7. Close with the state in one sentence. If you filed an inbox item, the sentence starts with
   it.
