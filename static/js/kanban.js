/* Accessible sales Kanban with filtering, reliable moves, and drag auto-scroll. */
(() => {
    const moneyFormatter = new Intl.NumberFormat("en-NG", {
        maximumFractionDigits: 2,
    });

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
        const cookie = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
        if (cookie && cookie[1]) return decodeURIComponent(cookie[1]);
        return document.querySelector('meta[name="csrf-token"]')?.content || "";
    }

    function text(value, fallback = "Not set") {
        return value === null || value === undefined || value === ""
            ? fallback
            : String(value);
    }

    function fieldLabel(field) {
        return field.replaceAll("_", " ");
    }

    function formatMeta(field, value, record, config) {
        if (value === null || value === undefined || value === "") return "Not set";
        if (field === "estimated_value") {
            const currency = text(record[config.currencyField], "NGN");
            return `${currency} ${moneyFormatter.format(Number(value))}`;
        }
        if (field === "probability") return `${value}%`;
        return String(value);
    }

    function announce(boardEl, message, error = false) {
        let notice = boardEl.querySelector("[data-kanban-notice]");
        if (!notice) {
            notice = document.createElement("div");
            notice.dataset.kanbanNotice = "";
            notice.className = "mb-3 rounded-lg border px-3 py-2 text-sm";
            notice.setAttribute("role", "status");
            notice.setAttribute("aria-live", "polite");
            boardEl.prepend(notice);
        }
        notice.classList.toggle("border-rose-500/50", error);
        notice.classList.toggle("bg-rose-950/40", error);
        notice.classList.toggle("text-rose-200", error);
        notice.classList.toggle("border-emerald-500/40", !error);
        notice.classList.toggle("bg-emerald-950/30", !error);
        notice.classList.toggle("text-emerald-200", !error);
        notice.textContent = message;
        window.clearTimeout(notice.dismissTimer);
        notice.dismissTimer = window.setTimeout(() => notice.remove(), 3500);
    }

    async function persistMove(updateEndpoint, cardId, fromColumn, toColumn, position) {
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
        if (!response.ok) throw new Error(`Persist failed: ${response.status}`);
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
        if (!symbols[icon]) return null;
        const element = document.createElement("span");
        element.className = "text-sm";
        element.textContent = symbols[icon];
        element.setAttribute("aria-hidden", "true");
        return element;
    }

    function createCard(record, config, column, columns, canMove) {
        const card = document.createElement("article");
        card.className = "rounded-lg border border-slate-700 bg-slate-950/45 p-3 shadow-sm transition hover:border-slate-500 hover:shadow-md";
        card.draggable = canMove;
        card.tabIndex = 0;
        card.dataset.cardId = record[config.idField];
        card.dataset.columnId = column.id;
        card.dataset.search = `${text(record[config.titleField], "")} ${text(record[config.subtitleField], "")}`.toLocaleLowerCase();

        const header = document.createElement("div");
        header.className = "flex items-start gap-2";
        const title = document.createElement("h4");
        title.className = "min-w-0 flex-1 text-sm font-semibold text-white";
        title.textContent = text(record[config.titleField], "Untitled lead");
        header.appendChild(title);

        if (record.url) {
            const detail = document.createElement("a");
            detail.href = record.url;
            detail.className = "flex-shrink-0 text-cyan-400 hover:text-cyan-300 focus:outline-none focus:ring-2 focus:ring-cyan-400";
            detail.textContent = "↗";
            detail.setAttribute("aria-label", `Open ${title.textContent}`);
            header.appendChild(detail);
        }
        card.appendChild(header);

        const subtitleValue = text(record[config.subtitleField], "");
        if (subtitleValue) {
            const subtitle = document.createElement("p");
            subtitle.className = "mt-1 text-xs text-slate-400";
            subtitle.textContent = subtitleValue;
            card.appendChild(subtitle);
        }

        const meta = document.createElement("dl");
        meta.className = "mt-3 grid grid-cols-2 gap-2";
        (config.metaFields || []).forEach((field) => {
            const item = document.createElement("div");
            item.className = "min-w-0 rounded-md bg-slate-900 px-2 py-1.5";
            const label = document.createElement("dt");
            label.className = "truncate text-[9px] font-semibold uppercase tracking-wide text-slate-500";
            label.textContent = fieldLabel(field);
            const value = document.createElement("dd");
            value.className = "mt-0.5 truncate text-xs font-semibold text-slate-200";
            value.textContent = formatMeta(field, record[field], record, config);
            item.append(label, value);
            meta.appendChild(item);
        });
        card.appendChild(meta);

        if (canMove) {
            const moveRow = document.createElement("label");
            moveRow.className = "mt-3 flex items-center gap-2 border-t border-slate-700 pt-2 text-[10px] text-slate-400";
            moveRow.append("Move to");
            const select = document.createElement("select");
            select.dataset.moveSelect = "";
            select.className = "min-w-0 flex-1 rounded-md border border-slate-600 bg-slate-950 px-2 py-1 text-[10px] text-slate-200";
            select.setAttribute("aria-label", `Move ${title.textContent} to stage`);
            columns.forEach((candidate) => {
                const option = document.createElement("option");
                option.value = candidate.id;
                option.textContent = candidate.title;
                option.selected = candidate.id === column.id;
                select.appendChild(option);
            });
            moveRow.appendChild(select);
            card.appendChild(moveRow);
        }
        return card;
    }

    function renderColumn(column, records, config, columns, canMove) {
        const wrapper = document.createElement("section");
        wrapper.className = "flex h-[min(650px,calc(100vh-22rem))] min-h-[360px] w-[264px] min-w-[264px] flex-col rounded-lg border border-t-4 border-slate-700 bg-slate-950/35 p-3";
        wrapper.dataset.kanbanColumn = column.id;
        if (/^#[0-9A-Fa-f]{6}$/.test(column.color || "")) wrapper.style.borderTopColor = column.color;

        const header = document.createElement("div");
        header.className = "mb-3 flex flex-shrink-0 items-start justify-between gap-2 border-b border-slate-700 pb-3";
        const heading = document.createElement("div");
        heading.className = "flex min-w-0 items-center gap-2";
        const icon = createStageIcon(column.icon);
        if (icon) heading.appendChild(icon);
        const title = document.createElement("h3");
        title.className = "truncate text-sm font-semibold text-white";
        title.textContent = text(column.title);
        heading.appendChild(title);
        const count = document.createElement("span");
        count.dataset.kanbanCount = "";
        count.className = "rounded-full bg-slate-800 px-2 py-0.5 text-xs font-semibold tabular-nums text-slate-300";
        count.textContent = String(records.length);
        header.append(heading, count);

        const body = document.createElement("div");
        body.className = "flex min-h-[80px] flex-1 flex-col gap-3 overflow-y-auto pr-1";
        body.dataset.columnId = column.id;
        body.dataset.kanbanColumnBody = "";
        records.forEach((record) => body.appendChild(createCard(record, config, column, columns, canMove)));
        wrapper.append(header, body);
        return wrapper;
    }

    function updateCounts(boardEl) {
        boardEl.querySelectorAll("[data-kanban-column]").forEach((column) => {
            column.querySelector("[data-kanban-count]").textContent = String(column.querySelectorAll("[data-card-id]").length);
        });
    }

    function applyFilters(boardEl) {
        const root = boardEl.parentElement;
        const search = root.querySelector("[data-kanban-search]")?.value.trim().toLocaleLowerCase() || "";
        const stage = root.querySelector("[data-kanban-stage-filter]")?.value || "";
        let visible = 0;
        boardEl.querySelectorAll("[data-kanban-column]").forEach((column) => {
            const stageMatches = !stage || stage === column.dataset.kanbanColumn;
            column.classList.toggle("hidden", !stageMatches);
            column.querySelectorAll("[data-card-id]").forEach((card) => {
                const matches = !search || card.dataset.search.includes(search);
                card.classList.toggle("hidden", !matches);
                if (stageMatches && matches) visible += 1;
            });
        });
        const result = root.querySelector("[data-kanban-result-count]");
        if (result) result.textContent = `${visible} loaded lead${visible === 1 ? "" : "s"} shown`;
    }

    function attachInteractions(boardEl, updateEndpoint) {
        let dragged = null;
        let scrollFrame = null;
        const pointer = { x: 0, y: 0 };
        const viewport = boardEl.querySelector("[data-kanban-scroll]");
        const edge = 72;

        const stopAutoScroll = () => {
            if (scrollFrame) cancelAnimationFrame(scrollFrame);
            scrollFrame = null;
        };
        const autoScroll = () => {
            if (!dragged) return stopAutoScroll();
            const rect = viewport.getBoundingClientRect();
            if (pointer.x < rect.left + edge) viewport.scrollLeft -= Math.max(5, (rect.left + edge - pointer.x) / 4);
            else if (pointer.x > rect.right - edge) viewport.scrollLeft += Math.max(5, (pointer.x - rect.right + edge) / 4);
            const body = document.elementFromPoint(pointer.x, pointer.y)?.closest("[data-kanban-column-body]");
            if (body) {
                const bodyRect = body.getBoundingClientRect();
                if (pointer.y < bodyRect.top + edge) body.scrollTop -= Math.max(4, (bodyRect.top + edge - pointer.y) / 5);
                else if (pointer.y > bodyRect.bottom - edge) body.scrollTop += Math.max(4, (pointer.y - bodyRect.bottom + edge) / 5);
            }
            scrollFrame = requestAnimationFrame(autoScroll);
        };

        async function move(card, target) {
            const source = card.parentElement;
            const from = card.dataset.columnId;
            const to = target.dataset.columnId;
            if (!from || !to || from === to) return;
            const originalNext = card.nextSibling;
            target.appendChild(card);
            card.dataset.columnId = to;
            card.classList.add("opacity-60", "pointer-events-none");
            updateCounts(boardEl);
            try {
                const position = Array.from(target.querySelectorAll("[data-card-id]")).indexOf(card);
                await persistMove(updateEndpoint, card.dataset.cardId, from, to, position);
                card.querySelector("[data-move-select]").value = to;
                announce(boardEl, "Lead stage updated.");
            } catch (error) {
                console.error("kanban: persist error", error);
                source.insertBefore(card, originalNext);
                card.dataset.columnId = from;
                card.querySelector("[data-move-select]").value = from;
                updateCounts(boardEl);
                announce(boardEl, "The lead could not be moved. Its original stage was restored.", true);
            } finally {
                card.classList.remove("opacity-60", "pointer-events-none");
                applyFilters(boardEl);
            }
        }

        boardEl.addEventListener("dragstart", (event) => {
            const card = event.target.closest("[data-card-id]");
            if (!card) return;
            dragged = card;
            pointer.x = event.clientX;
            pointer.y = event.clientY;
            card.classList.add("opacity-60");
            scrollFrame = requestAnimationFrame(autoScroll);
        });
        boardEl.addEventListener("dragover", (event) => {
            if (!dragged) return;
            event.preventDefault();
            pointer.x = event.clientX;
            pointer.y = event.clientY;
            boardEl.querySelectorAll(".ring-1").forEach((item) => item.classList.remove("ring-1", "ring-amber-500"));
            event.target.closest("[data-column-id]")?.classList.add("ring-1", "ring-amber-500");
        });
        boardEl.addEventListener("drop", (event) => {
            event.preventDefault();
            const target = event.target.closest("[data-column-id]");
            if (dragged && target) move(dragged, target);
        });
        boardEl.addEventListener("dragend", () => {
            dragged?.classList.remove("opacity-60");
            dragged = null;
            stopAutoScroll();
            boardEl.querySelectorAll(".ring-1").forEach((item) => item.classList.remove("ring-1", "ring-amber-500"));
        });
        boardEl.addEventListener("change", (event) => {
            const select = event.target.closest("[data-move-select]");
            if (!select) return;
            const card = select.closest("[data-card-id]");
            const target = boardEl.querySelector(`[data-column-id="${CSS.escape(select.value)}"]`);
            if (card && target) move(card, target);
        });
    }

    async function initBoard(boardEl) {
        const config = parseJson(boardEl.dataset.config) || {};
        const resolvedConfig = {
            columnField: config.columnField || "status",
            idField: config.idField || "id",
            titleField: config.titleField || "name",
            subtitleField: config.subtitleField || "type",
            metaFields: config.metaFields || [],
            currencyField: config.currencyField || "currency",
        };
        try {
            const response = await fetch(boardEl.dataset.kanbanEndpoint, { credentials: "same-origin" });
            if (!response.ok) throw new Error(`Kanban data fetch failed: ${response.status}`);
            const payload = await response.json();
            const columns = payload.columns || [];
            const records = payload.records || [];
            const canMove = boardEl.dataset.canMove === "true";
            const board = document.createElement("div");
            board.dataset.kanbanScroll = "";
            board.className = "flex gap-4 overflow-x-auto pb-3";
            columns.forEach((column) => {
                const matches = records.filter((record) => record[resolvedConfig.columnField] === column.id);
                board.appendChild(renderColumn(column, matches, resolvedConfig, columns, canMove));
            });
            boardEl.replaceChildren(board);

            const root = boardEl.parentElement;
            const stageFilter = root.querySelector("[data-kanban-stage-filter]");
            columns.forEach((column) => {
                const option = document.createElement("option");
                option.value = column.id;
                option.textContent = column.title;
                stageFilter?.appendChild(option);
            });
            root.querySelector("[data-kanban-search]")?.addEventListener("input", () => applyFilters(boardEl));
            stageFilter?.addEventListener("change", () => applyFilters(boardEl));
            applyFilters(boardEl);
            if (canMove) attachInteractions(boardEl, boardEl.dataset.updateEndpoint);
        } catch (error) {
            console.error("kanban: fetch error", error);
            boardEl.innerHTML = '<div class="rounded-xl border border-rose-500/40 bg-rose-950/30 p-6 text-sm text-rose-200">Unable to load the pipeline board. Refresh the page to try again.</div>';
        }
    }

    function initAll() {
        document.querySelectorAll("[data-kanban]").forEach(initBoard);
    }

    document.addEventListener("DOMContentLoaded", initAll);
    window.DotmacKanban = { initAll };
})();
