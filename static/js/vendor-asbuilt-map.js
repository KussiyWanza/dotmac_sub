(function (window, document) {
    "use strict";

    function mount(config) {
        var mapElement = document.getElementById(config.mapId);
        if (!mapElement || typeof window.L === "undefined") return;

        var routeGeojson = config.contextGeojson || { type: "FeatureCollection", features: [] };
        var canCapture = Boolean(config.canCapture);
        var canProposeClosure = Boolean(config.canProposeClosure);
        var map = window.L.map(mapElement).setView([9.0820, 8.6753], 6);

        window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            attribution:
                '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            maxZoom: 19,
        }).addTo(map);

        function themeColor(token, fallback) {
            return typeof window.themeColor === "function"
                ? window.themeColor(token)
                : fallback;
        }

        function setError(message) {
            var errorElement = document.getElementById(config.errorId);
            if (!errorElement) return;
            errorElement.textContent = message || "";
            errorElement.classList.toggle("hidden", !message);
        }

        if (routeGeojson.features && routeGeojson.features.length) {
            var contextLayer = window.L.geoJSON(routeGeojson, {
                style: function (feature) {
                    var kind = feature.properties && feature.properties.kind;
                    return {
                        color:
                            kind === "as_built"
                                ? themeColor("semantic-positive-600", "#10b981")
                                : themeColor("data-2", "#7c3aed"),
                        weight: 3,
                        opacity: 0.6,
                        dashArray: "6 6",
                    };
                },
                pointToLayer: function (feature, latlng) {
                    var status = feature.properties && feature.properties.status;
                    var colors = { pending: "#f59e0b", applied: "#10b981", rejected: "#ef4444" };
                    return window.L.circleMarker(latlng, {
                        radius: 7,
                        color: "#ffffff",
                        weight: 2,
                        fillColor: colors[status] || "#f59e0b",
                        fillOpacity: 0.95,
                    });
                },
                onEachFeature: function (feature, layer) {
                    var properties = feature.properties || {};
                    if (properties.kind !== "closure_proposal") return;
                    var popup = document.createElement("div");
                    var heading = document.createElement("strong");
                    heading.textContent = properties.name || "Proposed closure";
                    popup.appendChild(heading);
                    popup.appendChild(document.createElement("br"));
                    popup.appendChild(
                        document.createTextNode(
                            "Status: " + String(properties.status || "pending").replaceAll("_", " "),
                        ),
                    );
                    if (properties.review_notes) {
                        popup.appendChild(document.createElement("br"));
                        popup.appendChild(document.createTextNode("Review note: " + properties.review_notes));
                    }
                    layer.bindPopup(popup);
                },
            }).addTo(map);
            try {
                var contextBounds = contextLayer.getBounds();
                if (contextBounds.isValid()) {
                    map.fitBounds(contextBounds, { padding: [30, 30], maxZoom: 16 });
                }
            } catch (error) {
                /* Empty geometry. */
            }
        }

        if (!canCapture && !canProposeClosure) {
            setTimeout(function () {
                map.invalidateSize();
            }, 150);
            return;
        }

        var points = [];
        var line = window.L.polyline([], {
            color: themeColor("primary-600", "#0d9488"),
            weight: 4,
        }).addTo(map);
        var markers = window.L.layerGroup().addTo(map);
        var geojsonElement = document.getElementById(config.geojsonId);
        var lengthElement = document.getElementById(config.lengthId);
        var submitElement = document.getElementById(config.submitId);
        var hintElement = document.getElementById(config.hintId);
        var undoElement = document.getElementById(config.undoId);
        var clearElement = document.getElementById(config.clearId);
        var locateElement = document.getElementById(config.locateId);
        var closurePinMode = false;
        var closureMarker = null;
        var closureToggle = document.getElementById(config.closureToggleId);
        var closureForm = document.getElementById(config.closureFormId);
        var closureLatitude = document.getElementById(config.closureLatitudeId);
        var closureLongitude = document.getElementById(config.closureLongitudeId);
        var closureSubmit = document.getElementById(config.closureSubmitId);
        var closureHint = document.getElementById(config.closureHintId);

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
            if (!canCapture) return;
            line.setLatLngs(points);
            markers.clearLayers();
            points.forEach(function (point, index) {
                window.L.circleMarker(point, {
                    radius: 5,
                    color: "#ffffff",
                    weight: 2,
                    fillColor: themeColor("primary-600", "#0d9488"),
                    fillOpacity: 1,
                })
                    .bindTooltip(String(index + 1), { permanent: false })
                    .addTo(markers);
            });
            var valid = points.length >= 2;
            if (submitElement) submitElement.disabled = !valid;
            if (undoElement) undoElement.disabled = points.length === 0;
            if (clearElement) clearElement.disabled = points.length === 0;
            setError("");
            if (valid) {
                if (geojsonElement) {
                    geojsonElement.value = JSON.stringify({
                        type: "LineString",
                        coordinates: points.map(function (point) {
                            return [point[1], point[0]];
                        }),
                    });
                }
                var meters = Math.round(tracedLength() * 10) / 10;
                if (lengthElement) lengthElement.value = meters;
                if (hintElement) {
                    hintElement.textContent = points.length + " points - " + meters + " m traced";
                }
            } else {
                if (geojsonElement) geojsonElement.value = "";
                if (hintElement) hintElement.textContent = "Add at least 2 points to submit.";
            }
        }

        function addPoint(latitude, longitude) {
            points.push([latitude, longitude]);
            redraw();
        }

        if (closureToggle && closureForm) {
            closureToggle.addEventListener("click", function () {
                closurePinMode = !closurePinMode;
                closureForm.classList.toggle("hidden", !closurePinMode);
                closureToggle.textContent = closurePinMode ? "Cancel closure pin" : "Pin new closure";
            });
        }

        map.on("click", function (event) {
            if (closurePinMode && closureLatitude && closureLongitude) {
                closureLatitude.value = event.latlng.lat.toFixed(7);
                closureLongitude.value = event.latlng.lng.toFixed(7);
                if (closureMarker) map.removeLayer(closureMarker);
                closureMarker = window.L.marker(event.latlng)
                    .addTo(map)
                    .bindTooltip("Proposed closure")
                    .openTooltip();
                if (closureSubmit) closureSubmit.disabled = false;
                if (closureHint) {
                    closureHint.textContent =
                        event.latlng.lat.toFixed(6) +
                        ", " +
                        event.latlng.lng.toFixed(6) +
                        " - pending staff review";
                }
                return;
            }
            if (canCapture) addPoint(event.latlng.lat, event.latlng.lng);
        });

        if (canCapture) {
            if (undoElement) {
                undoElement.addEventListener("click", function () {
                    points.pop();
                    redraw();
                });
            }
            if (clearElement) {
                clearElement.addEventListener("click", function () {
                    points = [];
                    if (lengthElement) lengthElement.value = "";
                    redraw();
                });
            }
            if (locateElement) {
                locateElement.addEventListener("click", function () {
                    if (!navigator.geolocation) {
                        setError("Location is not available on this device.");
                        return;
                    }
                    navigator.geolocation.getCurrentPosition(
                        function (position) {
                            addPoint(position.coords.latitude, position.coords.longitude);
                            map.setView([position.coords.latitude, position.coords.longitude], 17);
                        },
                        function () {
                            setError(
                                "Could not get your location. Check this site's location permission.",
                            );
                        },
                        { enableHighAccuracy: true, timeout: 10000 },
                    );
                });
            }
            var form = document.getElementById(config.formId);
            if (form) {
                form.addEventListener("submit", function (event) {
                    if (points.length >= 2) return;
                    event.preventDefault();
                    setError("Trace at least 2 points for the as-built route.");
                });
            }
        }

        setTimeout(function () {
            map.invalidateSize();
        }, 150);
    }

    window.VendorAsBuiltMap = { mount: mount };
})(window, document);
