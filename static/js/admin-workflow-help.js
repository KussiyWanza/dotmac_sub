(function () {
    "use strict";

    function attachWorkflowHelp() {
        const control = document.querySelector("[data-admin-workflow-help-control]");
        const title = Array.from(document.querySelectorAll("main h1")).find(
            (candidate) => !candidate.closest('[role="dialog"], [hidden], [x-cloak]')
        );

        if (!control || !title) {
            return;
        }

        let titleGroup = title.closest("[data-admin-workflow-title-group]");
        if (!titleGroup) {
            titleGroup = document.createElement("div");
            titleGroup.className = "flex min-w-0 items-center gap-2";
            titleGroup.dataset.adminWorkflowTitleGroup = "";
            title.before(titleGroup);
            titleGroup.appendChild(title);
        }

        titleGroup.appendChild(control);
    }

    function returnWorkflowHelpToStaging(event) {
        const control = document.querySelector("[data-admin-workflow-help-control]");
        const staging = document.querySelector("[data-admin-workflow-help-staging]");
        const swapTarget = event.detail && event.detail.target;

        if (control && staging && swapTarget && swapTarget.contains(control)) {
            staging.appendChild(control);
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", attachWorkflowHelp, { once: true });
    } else {
        attachWorkflowHelp();
    }

    document.body.addEventListener("htmx:beforeSwap", returnWorkflowHelpToStaging);
    document.body.addEventListener("htmx:afterSwap", attachWorkflowHelp);
})();
