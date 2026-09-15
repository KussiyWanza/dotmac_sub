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
heading, including pages with custom headers. Clicking or tapping it opens a
centered **Page Overview** modal. The modal lists only the registered actions
permitted by the current request's cached role permissions and explains in a
small badge that other actions may be hidden and that record state can still
affect availability. This filtering performs no additional database read and
never replaces route or command authorization.

Each page action has a stable identifier, plain-language title, permission key,
and ordered instructions. The modal loads only action titles. Its single
**Open full help** link selects the exact guide by stable identifier. The Help
Center organizes every clickable Admin-sidebar destination as an expandable
section, with its meaningful list, creation, detail, or workflow pages beneath
it. Selecting a page renders its action instructions in the middle column and
the same action titles as in-page links in the right column. Help-only guides
do not make the shared layout add a contextual help control to new pages.

The modal traps keyboard focus while open and closes from its close control,
the backdrop, or the Escape key. Hover is supplementary and never the only way
to open help.
