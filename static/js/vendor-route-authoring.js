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
        var searchElement = document.getElementById(config.searchId);
        var searchResultsElement = document.getElementById(config.searchResultsId);
        var searchClearElement = document.getElementById(config.searchClearId);
        var searchHintElement = document.getElementById(config.searchHintId);
        var map = window.L.map(mapElement).setView([9.0820, 8.6753], 6);
        var contextLayers = {};
        var searchMarker = null;
        var searchController = null;

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
                    properties.kind === "closure_proposal" ||
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
                    pointToLayer: function (feature, latlng) {
                        var properties = feature.properties || {};
                        var colors = {
                            pending: "#f59e0b",
                            applied: "#10b981",
                            rejected: "#ef4444",
                        };
                        return window.L.circleMarker(latlng, {
                            radius: 7,
                            color: "#ffffff",
                            weight: 2,
                            fillColor: colors[properties.status] || "#f59e0b",
                            fillOpacity: 0.95,
                        });
                    },
                    onEachFeature: function (feature, layer) {
                        var properties = feature.properties || {};
                        if (properties.id) contextLayers[String(properties.id)] = layer;
                        if (properties.kind === "closure_proposal") {
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
                        }
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
            lengthElement.value = meters;
            hintElement.textContent =
                points.length + " points \u00b7 " + meters + " m traced";
        }

        function addPoint(latitude, longitude) {
            points.push([latitude, longitude]);
            redraw();
        }

        function escapeHtml(value) {
            return String(value || "").replace(/[&<>"']/g, function (character) {
                return {
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#39;",
                }[character];
            });
        }

        function parseCoordinates(value) {
            var match = String(value || "")
                .trim()
                .match(/^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$/);
            if (!match) return null;
            var latitude = Number(match[1]);
            var longitude = Number(match[2]);
            if (
                !Number.isFinite(latitude) ||
                !Number.isFinite(longitude) ||
                Math.abs(latitude) > 90 ||
                Math.abs(longitude) > 180
            ) {
                return null;
            }
            return { latitude: latitude, longitude: longitude };
        }

        function focusSearchTarget(latitude, longitude, label) {
            var latLng = [latitude, longitude];
            if (searchMarker) map.removeLayer(searchMarker);
            searchMarker = window.L.marker(latLng).addTo(map);
            if (label) searchMarker.bindPopup(label).openPopup();
            map.setView(latLng, 17);
        }

        function hideSearchResults() {
            if (!searchResultsElement) return;
            searchResultsElement.classList.add("hidden");
            searchResultsElement.innerHTML = "";
        }

        function showSearchMessage(message) {
            if (!searchResultsElement) return;
            searchResultsElement.innerHTML =
                '<div class="px-3 py-2 text-xs text-slate-500 dark:text-slate-400">' +
                escapeHtml(message) +
                "</div>";
            searchResultsElement.classList.remove("hidden");
        }

        function renderSearchResults(items) {
            if (!searchResultsElement) return;
            if (!items.length) {
                showSearchMessage("No cabinet or closure matches.");
                return;
            }
            searchResultsElement.innerHTML = items
                .map(function (item, index) {
                    var typeLabel = String(item.type || "").replaceAll("_", " ");
                    return (
                        '<button type="button" class="block w-full border-b border-slate-100 px-3 py-2 text-left last:border-b-0 hover:bg-primary-50 dark:border-slate-800 dark:hover:bg-slate-800" data-search-index="' +
                        index +
                        '">' +
                        '<span class="block font-semibold text-slate-800 dark:text-slate-100">' +
                        escapeHtml(item.title || item.subtitle || "Unnamed asset") +
                        "</span>" +
                        '<span class="block text-xs text-slate-500 dark:text-slate-400">' +
                        escapeHtml(typeLabel) +
                        (item.subtitle ? " · " + escapeHtml(item.subtitle) : "") +
                        "</span>" +
                        "</button>"
                    );
                })
                .join("");
            searchResultsElement.classList.remove("hidden");
            searchResultsElement._searchItems = items;
        }

        function runSearch() {
            if (!searchElement) return;
            var query = searchElement.value.trim();
            if (!query) {
                hideSearchResults();
                if (searchHintElement) {
                    searchHintElement.textContent =
                        "Use decimal coordinates like 6.5244, 3.3792 or search cabinet and closure names.";
                }
                return;
            }
            var coordinates = parseCoordinates(query);
            if (coordinates) {
                focusSearchTarget(
                    coordinates.latitude,
                    coordinates.longitude,
                    "Search location",
                );
                hideSearchResults();
                if (searchHintElement) {
                    searchHintElement.textContent =
                        coordinates.latitude.toFixed(6) +
                        ", " +
                        coordinates.longitude.toFixed(6);
                }
                return;
            }
            if (query.length < 2) {
                showSearchMessage("Type at least 2 characters.");
                return;
            }
            if (searchController) searchController.abort();
            searchController = new AbortController();
            fetch(
                "/api/v1/field/vendor/map-assets/search?types=fdh_cabinet,splice_closure&limit=20&q=" +
                    encodeURIComponent(query),
                { signal: searchController.signal },
            )
                .then(function (response) {
                    if (!response.ok) throw new Error("search_failed");
                    return response.json();
                })
                .then(function (items) {
                    renderSearchResults(Array.isArray(items) ? items : []);
                })
                .catch(function (error) {
                    if (error.name === "AbortError") return;
                    showSearchMessage("Search is unavailable right now.");
                });
        }

        map.on("click", function (event) {
            addPoint(event.latlng.lat, event.latlng.lng);
        });
        undoElement.addEventListener("click", function () {
            points.pop();
            redraw();
        });
        clearElement.addEventListener("click", function () {
            points = [];
            lengthElement.value = "";
            redraw();
        });
        if (searchElement) {
            var searchTimer = null;
            searchElement.addEventListener("input", function () {
                clearTimeout(searchTimer);
                searchTimer = setTimeout(runSearch, 250);
            });
            searchElement.addEventListener("keydown", function (event) {
                if (event.key !== "Enter") return;
                event.preventDefault();
                runSearch();
            });
        }
        if (searchClearElement) {
            searchClearElement.addEventListener("click", function () {
                if (searchElement) searchElement.value = "";
                if (searchMarker) {
                    map.removeLayer(searchMarker);
                    searchMarker = null;
                }
                hideSearchResults();
            });
        }
        if (searchResultsElement) {
            searchResultsElement.addEventListener("click", function (event) {
                var button = event.target.closest("[data-search-index]");
                if (!button) return;
                var items = searchResultsElement._searchItems || [];
                var item = items[Number(button.dataset.searchIndex)];
                if (!item) return;
                var title = item.title || item.subtitle || "Map asset";
                focusSearchTarget(item.latitude, item.longitude, title);
                if (searchHintElement) {
                    searchHintElement.textContent =
                        title +
                        " · " +
                        Number(item.latitude).toFixed(6) +
                        ", " +
                        Number(item.longitude).toFixed(6);
                }
                hideSearchResults();
            });
        }
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
