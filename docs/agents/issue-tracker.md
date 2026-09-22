# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI. Infer
the repository from the GitHub remote.

## Conventions

- Create: `gh issue create --title "..." --body-file <file>`.
- Read: `gh issue view <number> --comments`; fetch labels when triaging.
- List: `gh issue list --state open --json number,title,body,labels,comments`
  with appropriate state and label filters.
- Comment: `gh issue comment <number> --body-file <file>`.
- Label: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`.
- Close: `gh issue close <number> --comment "..."`.

When a skill says "publish to the issue tracker", create a GitHub issue.
When it says "fetch the relevant ticket", use `gh issue view <number> --comments`.

## Pull requests as a triage surface

**PRs as a request surface: no.** Set this to `yes` if external PRs should
enter the triage queue.

When enabled, use `gh pr` equivalents. List open PRs and keep external
authors (`CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE`); read the PR
body, comments, labels, and diff. GitHub issues and PRs share a number space,
so resolve an ambiguous `#<number>` by checking the PR, then the issue.

## Wayfinding operations

A wayfinding map is one issue labelled `wayfinder:map`. Its child tickets
are GitHub sub-issues where available; otherwise link them from a task list
in the map and add `Part of #<map>` to each child. Label children
`wayfinder:<type>` (`research`, `prototype`, `grilling`, or `task`).

Use native GitHub issue dependencies to record blockers. If unavailable,
put `Blocked by: #<number>` at the top of the child issue. An unassigned,
open child with no open blocker is ready to claim. Claim it with
`gh issue edit <number> --add-assignee @me`. To resolve it, comment with
the answer, close it, and add a short decision with a link to the map.
