# Handoff file

Written when a successor session needs narrative the task record cannot hold. Six sections in
this order; leave out a section that has nothing to say. Aim for one screen: past that, move
detail into the project's files and link to them. A Chinese version of the template is in
[handoff.zh-CN.md](handoff.zh-CN.md).

```markdown
# <what is to be achieved, one sentence>

<YYYY-MM-DD> handoff. Previous session: <title>, id <session id>. Directory <absolute path>, branch <name>.

## Goal and done-condition
<What is to be achieved, one to three sentences.>
- Done when: <a condition that can be checked, one per line>

## State
- Verified: <fact> — <proof: path:line, command and result, pull request link>
- Unverified: <fact> — <why it was not checked>
- Assumed: <guess> — <how to check it>

## Decided
- <decision> — <reason, or the person's own words with the date>

## Forbidden and dead ends
- Forbidden: <action> — <reason>
- Tried, did not work: <approach> — <why it failed>

## Next
1. <the first action, runnable as written, with its command or file>
2. <what follows>
- Waiting on the human: <question> — recommend <option>

## Inputs
| What | Where |
|---|---|
| <what it is> | <absolute path or link> |
```

## Writing rules

- Two strengths only. A hard limit starts with "Forbidden", "Only" or "Must" and gives its
  reason after a dash. Everything else is a plain statement. Leave out "should", "ideally",
  "try to", "consider": write an option as a default with its exception, "X by default; Z when Y".
- Every fact carries its level: verified with the proof, unverified with why, assumed with how
  to check. A claim without proof is unverified.
- Write a trap with its trigger: "when <situation>, <action>", not "watch out for X".
- Self-contained: nothing points into the conversation ("as discussed above", "the earlier
  plan"). Absolute paths, absolute dates, one line of explanation for a codename on first use,
  one point per sentence.
- Commands, paths, identifiers, numbers and error text stay verbatim in code formatting.
- No emphasis as force: no capitals, bold or repeated warnings. The reader is a model, and a
  stated reason holds better than volume.
- Point, do not paste: state lives in the tracker, the logs and the commits, and the handoff
  links to them.

## Before sending it

Read it cold and fix what fails:

- Could a session holding only this file start work?
- Does every "Verified" line show its proof?
- Is the first item under "Next" an action that can be run as written, and not a topic?
