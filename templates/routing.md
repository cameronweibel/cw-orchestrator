# cw routing rules

Claude reads this whole file every time it routes a task. Write plain
language under each workspace: common requests, keywords, client and
school names, whatever helps. Update it whenever a task routes wrong
and the change applies on the next launch. No syntax to learn.

Replace the example workspaces below with your own. The headings must
match your profile names in ~/.claude/profiles exactly, since cw also
appends corrections under the matching "## <workspace>" heading.

## work
Your company or client work. Projects, clients, proposals, invoices to
clients, the CRM, outreach. Add client names here.

## personal
Private life. Personal finances, bills, family, home admin, travel,
health, appointments.

## Tie breakers
- If a client is named, prefer work even for admin tasks.
- When unsure, return low confidence instead of guessing.
