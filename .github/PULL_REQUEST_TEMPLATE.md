## Summary

- What changed:
- Why:

## Verification

- Tests run:
- Manual checks:

## Typed Contracts

- [ ] Every changed cross-component boundary uses a typed immutable input and a
  typed outcome or domain error; transport strings and mappings are converted
  only at adapters.
- [ ] Domain identifiers, closed values, optionality, collections, and
  provenance use precise types (`UUID`, enums, value objects, `CommandContext`),
  not `Any`, bare containers, primitive bags, or magic strings.
- [ ] Tests and mocks construct the same typed contracts as production callers.
- [ ] Any touched legacy untyped boundary was migrated in this change, or an
  approved architecture decision and retirement plan is linked here.

- Typed-contract evidence or approved deviation:

## Version Impact

Apply exactly one label when this PR changes deployable behavior or shipped artifacts:
`version:patch`, `version:minor`, `version:major`, or `version:none`.

- Version impact:
- If `version:none`: Version impact: none because ...

## UI / Design Review

Complete this section for any UI-facing change. Use `N/A` where appropriate.
Follow `docs/UI_INFORMATION_AND_ACTION_STANDARD.md` for information depth,
state provenance, table scope, and action ownership.

- [ ] I reviewed `docs/DESIGN_REVIEW_CHECKLIST.md`.
- [ ] I identified the screen's audience, job, decision, read owner, and action owner.
- [ ] Status, reasons, freshness, and action eligibility come from authoritative services rather than UI inference.
- [ ] Tables show only decision-bearing columns and preserve RBAC/tenant scope for filters, pagination, totals, and exports.
- [ ] This change keeps exactly one primary header action per screen.
- [ ] Semantic state uses semantic colors, not module accent colors.
- [ ] The first viewport still supports a real task.
- [ ] Empty/loading/HTMX states preserve layout stability.
- [ ] Dark mode text, borders, and helper copy remain readable.
- [ ] If charts are present, they are limited, purposeful, and supported by totals/deltas outside the chart.
- [ ] If this changes a shared pattern, I updated `docs/FRONTEND_SPEC.md` or noted why not.

- Information/action contract or `N/A`:

## Notes

- Screens or routes affected:
- Follow-up work:
