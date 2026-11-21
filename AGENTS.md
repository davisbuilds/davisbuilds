# AGENTS.md

GitHub profile README and generated profile images.

## Verification

Before pushing, run `python3 scripts/check_publication_hygiene.py` and
`python3 scripts/check_comments.py`. The Publication Hygiene workflow runs both
on every pull request.

## Guardrails

- **Comments.** Don't add code comments. The one exception is a decision record:
  start it with `WHY:` and state a non-obvious reason the code can't express.
  Never justify a shortcut in a comment; fix it or record it in
  `docs/project/BACKLOG.md`. `python3 scripts/check_comments.py` enforces a
  ratchet: after deleting comments run it with `--update`, and never raise a
  count in `.comment-baseline.json`.

## Planning and continuity

Reuse clear task context. Save planning docs when decisions, acceptance, or
coordination need persistence. Leave a brief handoff only when needed to resume
unfinished work. Commit shared docs and keep status current. Before archiving,
preserve lasting decisions and open work in maintained docs and repair links.
