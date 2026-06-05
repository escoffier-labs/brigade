# Frontend

Use this cross-harness tool source when implementing, reviewing, or polishing a user-facing frontend.

## Intent

Build interfaces that are usable, coherent, responsive, and visually appropriate for the product context instead of generic demo screens.

## Use When

- A task changes a web, mobile, dashboard, game, or interactive UI.
- A feature needs layout, components, interaction states, or responsive behavior.
- A frontend should be checked for visual quality before handoff.

## Procedure

1. Read the existing app structure, design system, routes, and component patterns before inventing new UI.
2. Identify the primary user workflow and make the first screen useful for that workflow.
3. Use established local components, icons, spacing, colors, and state patterns where they exist.
4. Build complete controls and states: loading, empty, error, active, disabled, hover, focus, and mobile layouts when relevant.
5. Keep text fitted to its containers and check for overlap at common desktop and mobile sizes.
6. Verify with the smallest meaningful browser or screenshot check when the app can run locally.

## Boundaries

- Do not create a landing page when the user asked for an app, tool, game, or workflow screen.
- Do not add decorative gradients, blobs, cards, or large hero sections unless they serve the product.
- Do not introduce new UI dependencies unless the task explicitly requires them or the repo already uses them.
- Do not leave placeholder controls, fake actions, or unreachable states when the user expects a working experience.

## Output Shape

Prefer:

- the implemented UI change
- the verification command or browser check
- any remaining responsive or asset caveat
