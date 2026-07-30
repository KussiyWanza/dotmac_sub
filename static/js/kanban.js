/* Lightweight Kanban renderer with immediate drag-drop persistence.
 * Ported from dotmac_crm static/js/kanban.js (Phase 3 admin sales, PR 11);
 * drag-over ring recolored to sub's amber accent. */
(() => {
    function parseJson(value) {
        if (!value) return null;
        try {
            return JSON.parse(value);
        } catch (error) {
            console.warn("kanban: invalid JSON config", error);
            return null;
        }
    }

    function getCsrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        if (meta && meta.content) return meta.content;
        const match = document.cookie.match(/csrf_token=([^;]+)/);
        return match ? match[1] : "";
    }

    function formatValue(value) {
        if (value === null || value === undefined) return "—";
        return String(value);
    }

    function createCard(record, config, columnId) {
        const card = document.createElement("div");
        card.className = "rounded-lg border border-slate-200 bg-white p-3 shadow-sm transition hover:shadow-md dark:border-slate-700 dark:bg-slate-900/40 cursor-grab";
        card.draggable = true;
        card.dataset.cardId = record[config.idField];
        card.dataset.columnId = columnId;
        if (record.url) {
            card.dataset.url = record.url;
        }

        const title = document.createElement("div");
        title.className = "text-sm font-semibold text-slate-900 dark:text-white";
        title.textContent = formatValue(record[config.titleField]);

        const subtitle = document.createElement("div");
        subtitle.className = "mt-1 text-xs text-slate-500 dark:text-slate-400";
        subtitle.textContent = formatValue(record[config.subtitleField]);

        card.appendChild(title);
        card.appendChild(subtitle);

        const metaFields = config.metaFields || [];
        if (metaFields.length) {
            const meta = document.createElement("div");
            meta.className = "mt-2 flex flex-wrap gap-2 text-xs text-slate-500 dark:text-slate-400";
            metaFields.forEach((field) => {
                const tag = document.createElement("span");
                tag.className = "rounded-full border border-slate-200 px-2 py-0.5 dark:border-slate-700";
                tag.textContent = `${field}: ${formatValue(record[field])}`;
                meta.appendChild(tag);
            });
            card.appendChild(meta);
        }

        // Make card clickable to navigate to detail page (but not during drag)
        card.addEventListener("click", (event) => {
            if (card.classList.contains("opacity-60")) return; // dragging
            if (card.dataset.url) {
                window.location.href = card.dataset.url;
            }
        });

        return card;
    }

    function createStageIcon(icon) {
        const symbols = {
            circle: "●",
            target: "◎",
            document: "▤",
            handshake: "◇",
            clock: "◷",
            check: "✓",
            close: "×",
        };
        if (!icon || !symbols[icon]) return null;
        const element = document.createElement("span");
        element.className = "text-sm";
        element.textContent = symbols[icon];
        element.setAttribute("aria-hidden", "true");
        return element;
    }

    function renderColumn(column, records, config) {
        const wrapper = document.createElement("div");
        wrapper.className = "flex min-w-[220px] flex-1 flex-col rounded-lg border border-t-4 border-slate-200 bg-slate-50 p-3 dark:border-slate-700 dark:bg-slate-900/20";
        if (/^#[0-9A-Fa-f]{6}$/.test(column.color || "")) {
            wrapper.style.borderTopColor = column.color;
        }

        const header = document.createElement("div");
        header.className = "mb-3 flex items-center justify-between";
        const heading = document.createElement("div");
        heading.className = "flex min-w-0 items-center gap-2";
        const icon = createStageIcon(column.icon);
        if (icon) heading.appendChild(icon);
        const title = document.createElement("h3");
        title.className = "truncate text-sm font-semibold text-slate-900 dark:text-white";
        title.textContent = formatValue(column.title);
        heading.appendChild(title);
        if (column.stage_type && column.stage_type !== "standard") {
            const stageType = document.createElement("span");
            stageType.className =
                "rounded-full bg-white px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500 dark:bg-slate-800 dark:text-slate-300";
            stageType.textContent =
                column.stage_type === "closed_won" ? "Won" : "Lost";
            heading.appendChild(stageType);
        }
        const count = document.createElement("span");
        count.className = "text-xs text-slate-500 dark:text-slate-400";
        count.textContent = String(records.length);
        header.appendChild(heading);
        header.appendChild(count);

        const body = document.createElement("div");
        body.className = "flex min-h-[80px] flex-col gap-3";
        body.dataset.columnId = column.id;

        if (!records.length) {
            const empty = document.createElement("div");
            empty.className = "rounded-md border border-dashed border-slate-200 px-3 py-4 text-center text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400";
            empty.textContent = "No items";
            body.appendChild(empty);
        }

        records.forEach((record) => {
            body.appendChild(createCard(record, config, column.id));
        });

        wrapper.appendChild(header);
        wrapper.appendChild(body);
        return wrapper;
    }

    function attachDnD(boardEl, updateEndpoint) {
        const columns = boardEl.querySelectorAll("[data-column-id]");

        boardEl.addEventListener("dragstart", (event) => {
            const card = event.target.closest("[data-card-id]");
            if (!card) return;
            event.dataTransfer.setData("text/plain", card.dataset.cardId);
            event.dataTransfer.setData("from-column", card.dataset.columnId);
            card.classList.add("opacity-60");
        });

        boardEl.addEventListener("dragend", (event) => {
            const card = event.target.closest("[data-card-id]");
            if (!card) return;
            card.classList.remove("opacity-60");
        });

        columns.forEach((column) => {
            column.addEventListener("dragover", (event) => {
                event.preventDefault();
                column.classList.add("ring-1", "ring-amber-500");
            });

            column.addEventListener("dragleave", () => {
                column.classList.remove("ring-1", "ring-amber-500");
            });

            column.addEventListener("drop", async (event) => {
                event.preventDefault();
                column.classList.remove("ring-1", "ring-amber-500");
                const cardId = event.dataTransfer.getData("text/plain");
                const fromColumn = event.dataTransfer.getData("from-column");
                const toColumn = column.dataset.columnId;
                if (!cardId || !toColumn) return;

                const card = boardEl.querySelector(`[data-card-id="${cardId}"]`);
                if (!card) return;

                const emptyState = column.querySelector(".border-dashed");
                if (emptyState) emptyState.remove();

                card.dataset.columnId = toColumn;
                column.appendChild(card);

                if (fromColumn && fromColumn !== toColumn) {
                    const fromEl = boardEl.querySelector(`[data-column-id="${fromColumn}"]`);
                    if (fromEl && fromEl.querySelectorAll("[data-card-id]").length === 0) {
                        const empty = document.createElement("div");
                        empty.className = "rounded-md border border-dashed border-slate-200 px-3 py-4 text-center text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400";
                        empty.textContent = "No items";
                        fromEl.appendChild(empty);
                    }
                }

                if (!updateEndpoint) return;

                const position = Array.from(column.querySelectorAll("[data-card-id]")).indexOf(card);
                try {
                    const response = await fetch(updateEndpoint, {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            "X-CSRF-Token": getCsrfToken(),
                        },
                        credentials: "same-origin",
                        body: JSON.stringify({
                            id: cardId,
                            from: fromColumn,
                            to: toColumn,
                            position,
                        }),
                    });
                    if (!response.ok) {
                        throw new Error(`Persist failed: ${response.status}`);
                    }
                } catch (error) {
                    console.error("kanban: persist error", error);
                    card.classList.add("border-rose-400");
                }
            });
        });
    }

    async function initBoard(boardEl) {
        const endpoint = boardEl.dataset.kanbanEndpoint;
        const updateEndpoint = boardEl.dataset.updateEndpoint;
        const config = parseJson(boardEl.dataset.config) || {};
        const resolvedConfig = {
            columnField: config.columnField || "status",
            idField: config.idField || "id",
            titleField: config.titleField || "name",
            subtitleField: config.subtitleField || "type",
            metaFields: config.metaFields || [],
        };

        if (!endpoint) {
            console.warn("kanban: missing data-kanban-endpoint");
            return;
        }

        let payload;
        try {
            const response = await fetch(endpoint, { credentials: "same-origin" });
            if (!response.ok) {
                throw new Error(`Kanban data fetch failed: ${response.status}`);
            }
            payload = await response.json();
        } catch (error) {
            console.error("kanban: fetch error", error);
            boardEl.innerHTML = "<p class=\"text-sm text-slate-500 dark:text-slate-400\">Unable to load board.</p>";
            return;
        }

        const columns = payload.columns || [];
        const records = payload.records || [];
        const board = document.createElement("div");
        board.className = "flex gap-4 overflow-x-auto pb-2";

        columns.forEach((column) => {
            const columnRecords = records.filter((record) => record[resolvedConfig.columnField] === column.id);
            board.appendChild(renderColumn(column, columnRecords, resolvedConfig));
        });

        boardEl.innerHTML = "";
        boardEl.appendChild(board);
        attachDnD(boardEl, updateEndpoint);
    }

    function initAll() {
        document.querySelectorAll("[data-kanban]").forEach((boardEl) => {
            initBoard(boardEl);
        });
    }

    document.addEventListener("DOMContentLoaded", initAll);

    window.DotmacKanban = { initAll };
})();
