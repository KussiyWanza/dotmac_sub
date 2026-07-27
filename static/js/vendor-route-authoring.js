(function (window, document) {
    "use strict";

    function mount(config) {
        var mapElement = document.getElementById(config.mapId);
        var form = document.getElementById(config.formId);
        if (!mapElement || !form || typeof window.L === "undefined") return;

        var geojsonElement = document.getElementById(config.geojsonId);
        var lengthElement = document.getElementById(config.lengthId);
        var submitElement = document.getElementById(config.submitId);
        var hintElement = document.getElementById(config.hintId);
        var errorElement = document.getElementById(config.errorId);
        var locateElement = document.getElementById(config.locateId);
        var undoElement = document.getElementById(config.undoId);
        var clearElement = document.getElementById(config.clearId);
        var map = window.L.map(mapElement).setView([9.0820, 8.6753], 6);
        var contextLayers = {};

        window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            attribution:
                '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            maxZoom: 19,
        }).addTo(map);

        var contextFeatures = (config.contextGeojson.features || []).filter(
            function (feature) {
                var properties = feature.properties || {};
                return (
                    properties.kind === "as_built" ||
                    String(properties.quote_id || "") === String(config.quoteId)
                );
            },
        );
        if (contextFeatures.length) {
            var contextLayer = window.L.geoJSON(
                { type: "FeatureCollection", features: contextFeatures },
                {
                    style: function (feature) {
                        var kind = feature.properties && feature.properties.kind;
                        return {
                            color:
                                kind === "as_built"
                                    ? window.themeColor("semantic-positive-600")
                                    : window.themeColor("data-2"),
                            weight: 3,
                            opacity: 0.65,
                            dashArray: "6 6",
                        };
                    },
                    onEachFeature: function (feature, layer) {
                        var properties = feature.properties || {};
                        if (properties.id) contextLayers[String(properties.id)] = layer;
                    },
                },
            ).addTo(map);
            var contextBounds = contextLayer.getBounds();
            if (contextBounds.isValid()) {
                map.fitBounds(contextBounds, { padding: [30, 30], maxZoom: 16 });
            }
        }

        var points = [];
        var drawnLine = window.L.polyline([], {
            color: window.themeColor("primary-600"),
            weight: 4,
        }).addTo(map);
        var markers = window.L.layerGroup().addTo(map);
        var lengthEdited = false;

        function setError(message) {
            errorElement.textContent = message || "";
            errorElement.classList.toggle("hidden", !message);
        }

        function haversine(first, second) {
            var radius = 6371000;
            var radians = function (degrees) {
                return (degrees * Math.PI) / 180;
            };
            var latitudeDelta = radians(second[0] - first[0]);
            var longitudeDelta = radians(second[1] - first[1]);
            var value =
                Math.sin(latitudeDelta / 2) * Math.sin(latitudeDelta / 2) +
                Math.cos(radians(first[0])) *
                    Math.cos(radians(second[0])) *
                    Math.sin(longitudeDelta / 2) *
                    Math.sin(longitudeDelta / 2);
            return 2 * radius * Math.asin(Math.min(1, Math.sqrt(value)));
        }

        function tracedLength() {
            var total = 0;
            for (var index = 1; index < points.length; index += 1) {
                total += haversine(points[index - 1], points[index]);
            }
            return total;
        }

        function redraw() {
            drawnLine.setLatLngs(points);
            markers.clearLayers();
            points.forEach(function (point, index) {
                window.L.circleMarker(point, {
                    radius: 5,
                    color: window.themeColor("slate-50"),
                    weight: 2,
                    fillColor: window.themeColor("primary-600"),
                    fillOpacity: 1,
                })
                    .bindTooltip(String(index + 1))
                    .addTo(markers);
            });

            var valid = points.length >= 2;
            submitElement.disabled = !valid;
            undoElement.disabled = points.length === 0;
            clearElement.disabled = points.length === 0;
            setError("");

            if (!valid) {
                geojsonElement.value = "";
                hintElement.textContent = "Add at least 2 points to save a route draft.";
                return;
            }

            geojsonElement.value = JSON.stringify({
                type: "LineString",
                coordinates: points.map(function (point) {
                    return [point[1], point[0]];
                }),
            });
            var meters = Math.round(tracedLength() * 10) / 10;
            if (!lengthEdited) lengthElement.value = meters;
            hintElement.textContent =
                points.length + " points \u00b7 " + meters + " m traced";
        }

        function addPoint(latitude, longitude) {
            points.push([latitude, longitude]);
            redraw();
        }

        map.on("click", function (event) {
            addPoint(event.latlng.lat, event.latlng.lng);
        });
        lengthElement.addEventListener("input", function () {
            lengthEdited = true;
        });
        undoElement.addEventListener("click", function () {
            points.pop();
            redraw();
        });
        clearElement.addEventListener("click", function () {
            points = [];
            lengthEdited = false;
            lengthElement.value = "";
            redraw();
        });
        locateElement.addEventListener("click", function () {
            if (!navigator.geolocation) {
                setError("Location is not available on this device.");
                return;
            }
            navigator.geolocation.getCurrentPosition(
                function (position) {
                    addPoint(position.coords.latitude, position.coords.longitude);
                    map.setView(
                        [position.coords.latitude, position.coords.longitude],
                        17,
                    );
                },
                function () {
                    setError(
                        "Could not get your location. Check this site's location permission.",
                    );
                },
                { enableHighAccuracy: true, timeout: 10000 },
            );
        });
        form.addEventListener("submit", function (event) {
            if (points.length >= 2) return;
            event.preventDefault();
            setError("Trace at least 2 points before saving this route draft.");
        });

        document.querySelectorAll("[data-route-focus]").forEach(function (button) {
            button.addEventListener("click", function () {
                var layer = contextLayers[String(button.dataset.routeFocus || "")];
                if (!layer || typeof layer.getBounds !== "function") return;
                var bounds = layer.getBounds();
                if (bounds.isValid()) {
                    map.fitBounds(bounds, { padding: [30, 30], maxZoom: 17 });
                }
            });
        });

        setTimeout(function () {
            map.invalidateSize();
        }, 150);
    }

    window.VendorRouteAuthoring = { mount: mount };
})(window, document);
