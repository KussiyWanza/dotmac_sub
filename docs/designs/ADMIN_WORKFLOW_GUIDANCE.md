# Admin workflow guidance

`ui.admin_workflow_guidance` owns the read-only, deploy-versioned registry of
plain-language Admin workflow guidance.  It projects content to the protected
Help Center and supplies contextual explanations to Admin templates.

Workflow and domain owners continue to own facts, permissions, eligibility,
state transitions, and outcomes. Guidance never enables an action or changes
state. The Help Center route remains an adapter protected by the shared Admin
staff-authentication gate; it requires no additional feature permission.

The authoritative input is the typed `WORKFLOW_GUIDANCE` registry. It is fresh
at the deployed application revision; there is no cache or external fallback.
Drift is detected by `workflow_guidance_gate`, which requires guidance evidence
when an Admin workflow changes. Repair is a reviewed application change and
redeployment of the exact revision.

Each guide uses either a deliberate section prefix or a segment-aware route
template. Route templates match exact segments, accept named `{identifier}`
segments, and may end in `**` for descendants. A matching route template is
more specific than a section prefix. Broad prefixes must not attach one
workflow's instructions to unrelated child pages; explicit exclusions protect
reserved child sections when an identifier-shaped route would otherwise match.
The Help Center presents Getting started first, followed by the remaining
categories alphabetically.

Guided Admin pages use one shared circular help control immediately beside the
main page title. The shared layout attaches the control to the first main
heading, including pages with custom headers. Clicking or tapping it opens the
guide in a centered modal. The modal traps keyboard focus while open and closes
from its close control, the backdrop, or the Escape key. Hover is supplementary
and never the only way to open help.
