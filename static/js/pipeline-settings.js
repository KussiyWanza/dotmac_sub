(() => {
    function initializeFilters(root) {
        const search = root.querySelector("#pipeline-search");
        const status = root.querySelector("#pipeline-status-filter");
        const cards = Array.from(root.querySelectorAll("[data-pipeline-card]"));
        const empty = root.querySelector("[data-filter-empty]");
        if (!search || !status || !cards.length) return;

        const apply = () => {
            const query = search.value.trim().toLowerCase();
            const selectedStatus = status.value;
            let visibleCount = 0;
            cards.forEach((card) => {
                const matchesName = card.dataset.pipelineName.includes(query);
                const matchesStatus =
                    selectedStatus === "all" ||
                    card.dataset.pipelineStatus === selectedStatus;
                card.hidden = !(matchesName && matchesStatus);
                if (!card.hidden) visibleCount += 1;
            });
            if (empty) empty.hidden = visibleCount !== 0;
        };

        search.addEventListener("input", apply);
        status.addEventListener("change", apply);
    }

    function updateStageOrder(table, reorderForm) {
        const rows = Array.from(table.querySelectorAll("[data-stage-row]"));
        rows.forEach((row, index) => {
            const orderInput = row.querySelector('input[name="order_index"]');
            if (orderInput) orderInput.value = String(index);
        });
        reorderForm.querySelector('input[name="stage_ids"]').value = rows
            .map((row) => row.dataset.stageId)
            .join(",");
    }

    function initializeStageOrdering(root) {
        root.querySelectorAll("[data-stage-ordering]").forEach((table) => {
            const card = table.closest("[data-pipeline-card]");
            const reorderForm = card && card.querySelector("[data-stage-reorder-form]");
            if (!reorderForm) return;

            let draggedRow = null;
            table.addEventListener("dragstart", (event) => {
                const row = event.target.closest("[data-stage-row]");
                if (!row) return;
                draggedRow = row;
                row.classList.add("opacity-50");
                event.dataTransfer.effectAllowed = "move";
            });
            table.addEventListener("dragend", () => {
                if (draggedRow) draggedRow.classList.remove("opacity-50");
                draggedRow = null;
            });
            table.addEventListener("dragover", (event) => {
                const target = event.target.closest("[data-stage-row]");
                if (!draggedRow || !target || draggedRow === target) return;
                event.preventDefault();
                const bounds = target.getBoundingClientRect();
                const insertAfter = event.clientY > bounds.top + bounds.height / 2;
                target.parentNode.insertBefore(
                    draggedRow,
                    insertAfter ? target.nextSibling : target
                );
            });
            table.addEventListener("drop", (event) => {
                if (!draggedRow) return;
                event.preventDefault();
                updateStageOrder(table, reorderForm);
                if (typeof reorderForm.requestSubmit === "function") {
                    reorderForm.requestSubmit();
                }
            });
        });
    }

    function initializeBulkConfirmations(root) {
        root.querySelectorAll("[data-bulk-assignment-form]").forEach((form) => {
            form.addEventListener("submit", (event) => {
                const scope = form.querySelector("[data-assignment-scope]");
                if (
                    scope &&
                    scope.value === "all" &&
                    !window.confirm(
                        "Overwrite existing pipeline and stage assignments for all active leads?"
                    )
                ) {
                    event.preventDefault();
                }
            });
        });
    }

    function initialize() {
        document.querySelectorAll("[data-pipeline-settings]").forEach((root) => {
            initializeFilters(root);
            initializeStageOrdering(root);
            initializeBulkConfirmations(root);
        });
    }

    document.addEventListener("DOMContentLoaded", initialize);
})();
