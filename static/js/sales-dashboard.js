(function () {
    "use strict";

    function initializeForecast(root) {
        const scope = root && root.querySelectorAll ? root : document;
        scope.querySelectorAll("[data-sales-dashboard-data]").forEach((dashboard) => {
            const canvas = dashboard.querySelector("[data-sales-forecast]");
            const payloadNode = dashboard.querySelector(
                "[data-sales-forecast-payload]"
            );
            if (!canvas || !payloadNode || canvas.dataset.initialized === "true") {
                return;
            }
            if (!window.DotmacCharts || !window.Chart) {
                return;
            }

            let payload;
            try {
                payload = JSON.parse(payloadNode.textContent || "{}");
            } catch (_error) {
                return;
            }
            if (!Array.isArray(payload.labels) || !Array.isArray(payload.datasets)) {
                return;
            }

            const existing = window.Chart.getChart(canvas);
            if (existing) {
                existing.destroy();
            }
            window.DotmacCharts.createAreaChart(
                canvas,
                {
                    labels: payload.labels,
                    datasets: payload.datasets,
                },
                {
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            display: true,
                            position: "bottom",
                        },
                    },
                }
            );
            canvas.dataset.initialized = "true";
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        initializeForecast(document);
    });
    document.addEventListener("htmx:afterSwap", function (event) {
        initializeForecast(event.detail.target);
    });
})();
