(function () {
    "use strict";

    if (window.__dotmacAttendanceReminderStarted) return;
    window.__dotmacAttendanceReminderStarted = true;

    const attendanceUrl = "/admin/dashboard/attendance";
    const dashboardUrl = "/admin/dashboard";
    const reminderSelector = "[data-attendance-reminder-panel]";
    const script = document.currentScript;
    const attendanceSubject = script?.dataset.attendanceSubject || "anonymous";
    const storagePrefix = `dotmac_attendance_reminder:${attendanceSubject}:`;
    const cacheKey = `${storagePrefix}cache`;
    const snoozeKey = `${storagePrefix}snoozeUntil`;
    const dismissedDateKey = `${storagePrefix}dismissedDate`;
    const checkIntervalMs = 10 * 60 * 1000;
    const unavailableRetryMs = 5 * 60 * 1000;
    const eligibilityRetryMs = 12 * 60 * 60 * 1000;
    const snoozeMs = 10 * 60 * 1000;

    function now() {
        return Date.now();
    }

    function localDate() {
        return new Date().toISOString().slice(0, 10);
    }

    function storageGet(key) {
        try {
            return window.localStorage.getItem(key);
        } catch (_error) {
            return null;
        }
    }

    function storageSet(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch (_error) {}
    }

    function readNumber(key) {
        const value = Number(storageGet(key) || 0);
        return Number.isFinite(value) ? value : 0;
    }

    function readCache() {
        try {
            return JSON.parse(storageGet(cacheKey) || "null");
        } catch (_error) {
            return null;
        }
    }

    function writeCache(payload, ttlMs) {
        storageSet(
            cacheKey,
            JSON.stringify({
                ...payload,
                expiresAt: now() + ttlMs,
            })
        );
    }

    function isDismissed(attendanceDate) {
        return (
            attendanceDate &&
            storageGet(dismissedDateKey) === attendanceDate
        );
    }

    function isDashboardPage() {
        return window.location.pathname.replace(/\/+$/, "") === dashboardUrl;
    }

    function removeReminder() {
        document.querySelector(reminderSelector)?.remove();
    }

    function showReminder(attendanceDate) {
        if (isDashboardPage() || isDismissed(attendanceDate)) return;
        if (document.querySelector(reminderSelector)) return;

        const panel = document.createElement("section");
        panel.dataset.attendanceReminderPanel = "true";
        panel.setAttribute("role", "dialog");
        panel.setAttribute("aria-live", "polite");
        panel.setAttribute("aria-label", "Attendance check-in reminder");
        panel.className = "attendance-reminder-panel";
        panel.innerHTML = `
            <p class="attendance-reminder-eyebrow">Attendance</p>
            <h2 class="attendance-reminder-title">Check in reminder</h2>
            <p class="attendance-reminder-body">You are on shift and have not checked in.</p>
            <div class="attendance-reminder-actions">
                <button type="button" data-attendance-reminder-dashboard class="attendance-reminder-button attendance-reminder-button-primary">Go to Dashboard</button>
                <button type="button" data-attendance-reminder-snooze class="attendance-reminder-button attendance-reminder-button-secondary">Snooze 10 min</button>
                <button type="button" data-attendance-reminder-dismiss class="attendance-reminder-button attendance-reminder-button-ghost">Dismiss today</button>
            </div>
        `;

        panel
            .querySelector("[data-attendance-reminder-dashboard]")
            ?.addEventListener("click", function () {
                window.location.href = dashboardUrl;
            });
        panel
            .querySelector("[data-attendance-reminder-snooze]")
            ?.addEventListener("click", function () {
                storageSet(snoozeKey, String(now() + snoozeMs));
                removeReminder();
            });
        panel
            .querySelector("[data-attendance-reminder-dismiss]")
            ?.addEventListener("click", function () {
                storageSet(dismissedDateKey, attendanceDate || localDate());
                removeReminder();
            });

        document.body.appendChild(panel);
    }

    function parseAttendance(html) {
        const template = document.createElement("template");
        template.innerHTML = html.trim();
        const widget = template.content.querySelector("#attendance-widget");
        if (!widget) return null;
        const attendanceDate = widget.dataset.attendanceDate || localDate();
        const needsReminder =
            widget.dataset.attendanceCanCheckIn === "true" ||
            Boolean(widget.querySelector('[data-attendance-action="check-in"]'));
        return {
            attendanceDate,
            needsReminder,
            state: widget.dataset.attendanceState || "",
            errorCode: widget.dataset.attendanceErrorCode || "",
            retryable: widget.dataset.attendanceRetryable !== "false",
        };
    }

    async function readAttendance() {
        const response = await fetch(attendanceUrl, {
            credentials: "same-origin",
            headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        if (!response.ok) throw new Error("attendance_reminder_read_failed");
        return parseAttendance(await response.text());
    }

    async function checkAttendanceReminder() {
        if (readNumber(snoozeKey) > now()) return;

        const cache = readCache();
        if (cache && cache.expiresAt > now()) {
            if (cache.needsReminder) {
                showReminder(cache.attendanceDate || localDate());
            }
            return;
        }

        try {
            const attendance = await readAttendance();
            if (!attendance) {
                writeCache({ needsReminder: false }, unavailableRetryMs);
                removeReminder();
                return;
            }
            const ttlMs = attendance.state
                ? checkIntervalMs
                : attendance.retryable
                  ? unavailableRetryMs
                  : eligibilityRetryMs;
            writeCache(attendance, ttlMs);
            if (attendance.needsReminder) {
                showReminder(attendance.attendanceDate);
            } else {
                removeReminder();
            }
        } catch (_error) {
            writeCache({ needsReminder: false }, unavailableRetryMs);
            removeReminder();
        }
    }

    function start() {
        window.setTimeout(checkAttendanceReminder, 1500);
        window.setInterval(checkAttendanceReminder, checkIntervalMs);
        document.addEventListener("visibilitychange", function () {
            if (document.visibilityState === "visible") {
                checkAttendanceReminder();
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();
