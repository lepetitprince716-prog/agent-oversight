# Escalation: the inbox item

Three kinds of thing may reach the human. Each is one waiting task in the tracker, labelled
with its kind. Everything else the agents settle and record.

| Kind | Label | When | The line says |
|---|---|---|---|
| Decision | `wait:decision` | A choice only the human can make: a direction, a trade-off they own, an irreversible or outward step. | The question and the recommended option. |
| Review | `wait:review` | Finished work whose acceptance needs their eye: taste, or a high-risk change. | What to look at and where: a path, a link, a preview. |
| Notice | `wait:notice` | Loss or exposure; a result they rely on that failed or could not be checked; work that stopped and will not resume by itself. | What happened and what was already done about it. |

## The line

One line, under about 80 characters, that can be answered without opening the session. It
goes at the end of the task's notes after the marker `blocker:` (or `卡点：`).

- Decision: `blocker: keep the v1 schema or migrate now — recommend migrate, it is reversible until Friday`
- Review: `blocker: look at the three cover options in out/covers/sheet.png`
- Notice: `blocker: nightly export wrote 0 rows since the 12th — fixed the query, 4 days of data are missing`

When the wait is on a pull request, name it so that `oversight reconcile` can look it up:
`PR #12` for the task's own repository, `owner/name#12` or the pull request's URL for any
other.

## With beads

```bash
# an existing task
bd update <id> -s blocked --add-label wait:decision --append-notes 'blocker: <the line>'

# no task yet
bd create '<short title>' -l wait:notice,repo:<name> --notes 'blocker: <the line>'
bd update <new id> -s blocked

# answered or resolved, work continues
bd update <id> -s open -a '' --remove-label wait:decision

# answered or resolved, nothing left
bd close <id> -r '<the result, first sentence>'
```

## Rules

- File the item when it becomes known, not at the end of the session.
- One item per thing. Two decisions are two items.
- Close or reopen the item in the turn it is answered or resolved. `oversight reconcile`
  finds the ones nobody closed.
- Silence is not an answer. An item that waits on a yes stays waiting.
- When nearly every item of some kind gets the same answer, stop asking that kind and write
  the answer into the instructions.
