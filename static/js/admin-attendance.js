(function () {
    "use strict";

    const widgetSelector = "#attendance-widget";

    function csrfToken() {
        return document.querySelector('meta[name="csrf-token"]')?.content || "";
    }

    function showError(message) {
        const target = document.querySelector(`${widgetSelector} [data-attendance-error]`);
        if (target) target.textContent = message;
    }

    function replaceWidget(html) {
        const current = document.querySelector(widgetSelector);
        if (!current) return;
        const template = document.createElement("template");
        template.innerHTML = html.trim();
        const replacement = template.content.querySelector(widgetSelector);
        if (replacement) current.replaceWith(replacement);
    }

    async function refreshAttendance() {
        const response = await fetch("/admin/dashboard/attendance", {
            credentials: "same-origin",
            headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        if (!response.ok) throw new Error("attendance_refresh_failed");
        replaceWidget(await response.text());
    }

    function currentPosition() {
        return new Promise((resolve, reject) => {
            if (!navigator.geolocation) {
                reject(new Error("location_unavailable"));
                return;
            }
            navigator.geolocation.getCurrentPosition(resolve, reject, {
                enableHighAccuracy: true,
                timeout: 10000,
                maximumAge: 0,
            });
        });
    }

    function locationMessage(error) {
        if (error && error.code === 1) {
            return "Location access is required to record attendance.";
        }
        if (error && error.code === 3) {
            return "Location request timed out. Please try again.";
        }
        return "Your current location could not be obtained. Please try again.";
    }

    async function punch(button) {
        button.disabled = true;
        const originalText = button.textContent;
        button.textContent = "Getting location…";
        showError("");

        let position;
        try {
            position = await currentPosition();
        } catch (error) {
            showError(locationMessage(error));
            button.disabled = false;
            button.textContent = originalText;
            return;
        }

        button.textContent = "Recording…";
        const action = button.dataset.attendanceAction;
        const idempotencyKey = crypto.randomUUID();
        const payload = {
            latitude: position.coords.latitude,
            longitude: position.coords.longitude,
            accuracy_m: position.coords.accuracy,
            observed_at: new Date(position.timestamp).toISOString(),
        };

        try {
            const response = await fetch(`/admin/dashboard/attendance/${action}`, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": csrfToken(),
                    "Idempotency-Key": idempotencyKey,
                },
                body: JSON.stringify(payload),
            });
            if (!response.ok) throw new Error("attendance_punch_failed");
            replaceWidget(await response.text());
        } catch (_error) {
            // A timed-out mutation is ambiguous. Read ERP's authoritative state
            // before presenting another action; never infer local success.
            try {
                await refreshAttendance();
                showError("Attendance state was refreshed. Please verify it before retrying.");
            } catch (_refreshError) {
                showError("Attendance is temporarily unavailable. Please try again.");
                button.disabled = false;
                button.textContent = originalText;
            }
        }
    }

    document.addEventListener("click", function (event) {
        const action = event.target.closest("[data-attendance-action]");
        if (action) {
            punch(action);
            return;
        }
        if (event.target.closest("[data-attendance-refresh]")) {
            refreshAttendance().catch(function () {
                showError("Attendance is temporarily unavailable. Please try again.");
            });
        }
    });
})();
