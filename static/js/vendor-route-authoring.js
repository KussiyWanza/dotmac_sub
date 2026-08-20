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
        var filterSummaryElement = document.getElementById(config.filterSummaryId);
        function elementsForSelector(selector) {
            return selector ? Array.from(document.querySelectorAll(selector)) : [];
        }

        var layerFilters = elementsForSelector(config.layerFilterSelector);
        var statusFilters = elementsForSelector(config.statusFilterSelector);
        var poiFilters = elementsForSelector(config.poiFilterSelector);
        var filterActionButtons = elementsForSelector(config.filterActionSelector);
        var poiNearbyElement = document.getElementById(config.poiNearbyId);
        var poiClearElement = document.getElementById(config.poiClearId);
        var poiRadiusElement = document.getElementById(config.poiRadiusId);
        var plantFilterButtons = elementsForSelector(config.plantFilterSelector);
        var plantViewElement = document.getElementById(config.plantViewId);
        var map = window.L.map(mapElement).setView([9.0820, 8.6753], 6);
        var contextLayers = {};
        var contextLayerEntries = [];
        var networkPlantLayerEntries = [];
        var plantSegmentsVisible = true;
        var visiblePlantSegmentTypes = null;
        var searchMarker = null;
        var searchController = null;
        var nearbyController = null;
        var nearbyPointCount = 0;
        var nearbyDefaultLabel = poiNearbyElement
            ? poiNearbyElement.textContent
            : "";

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

        function selectedValues(inputs) {
            return inputs
                .filter(function (input) {
                    return input.checked;
                })
                .map(function (input) {
                    return input.value;
                });
        }

        function selectedPoiTypes() {
            var values = selectedValues(poiFilters);
            return values.length ? values : ["fdh_cabinet", "splice_closure"];
        }

        function plantAssetCount(assetType) {
            return networkPlantFeatures.filter(function (feature) {
                return String((feature.properties || {}).type || "") === assetType;
            }).length;
        }

        function plantAssetSelected(assetType) {
            if (assetType === "fiber_segment") return plantSegmentsVisible;
            var input = poiFilters.find(function (candidate) {
                return candidate.value === assetType;
            });
            return Boolean(input && input.checked);
        }

        function syncPlantToolbarState() {
            plantFilterButtons.forEach(function (button) {
                var assetType = button.dataset.routePlantFilter || "";
                var active = plantAssetSelected(assetType);
                button.setAttribute("aria-pressed", String(active));
                button.classList.toggle("border-cyan-500", active);
                button.classList.toggle("bg-cyan-950/40", active);
                button.classList.toggle("text-cyan-200", active);
                button.classList.toggle("border-slate-600", !active);
                button.classList.toggle("text-slate-300", !active);
                var count = button.querySelector("[data-route-plant-count]");
                if (count) count.textContent = String(plantAssetCount(assetType));
            });
        }

        function setPlantPointSelection(selectedTypes) {
            poiFilters.forEach(function (input) {
                input.checked = selectedTypes.indexOf(input.value) !== -1;
            });
            syncFilterActionState("poi");
        }

        function applyPlantViewPreset(preset) {
            if (preset === "custom") return;
            visiblePlantSegmentTypes = null;
            if (preset === "osp") {
                setPlantPointSelection([
                    "fdh_cabinet",
                    "splice_closure",
                    "fiber_access_point",
                ]);
                plantSegmentsVisible = false;
            } else if (preset === "backbone") {
                setPlantPointSelection([]);
                plantSegmentsVisible = true;
                visiblePlantSegmentTypes = ["feeder"];
            } else if (preset === "customer_edge") {
                setPlantPointSelection(["fiber_access_point", "service_building"]);
                plantSegmentsVisible = true;
                visiblePlantSegmentTypes = ["distribution", "drop"];
            } else if (preset === "all") {
                setPlantPointSelection(
                    plantFilterButtons
                        .map(function (button) {
                            return button.dataset.routePlantFilter || "";
                        })
                        .filter(function (assetType) {
                            return assetType && assetType !== "fiber_segment";
                        }),
                );
                plantSegmentsVisible = true;
            } else if (preset === "none") {
                setPlantPointSelection([]);
                plantSegmentsVisible = false;
            }
            clearNearbyPoints();
            hideSearchResults();
            syncNetworkPlantVisibility();
            updateFilterSummary();
        }

        function labelForValue(value) {
            return String(value || "").replaceAll("_", " ");
        }

        function featureLayerKey(feature) {
            var properties = feature.properties || {};
            return properties.kind === "as_built"
                ? "as_built"
                : properties.kind === "closure_proposal"
                  ? "closure_proposal"
                  : "proposed";
        }

        function featureStatusKey(feature) {
            var properties = feature.properties || {};
            return String(properties.status || "").trim();
        }

        function contextFeatureVisible(feature) {
            var visibleLayers = selectedValues(layerFilters);
            var visibleStatuses = selectedValues(statusFilters);
            var layerKey = featureLayerKey(feature);
            var statusKey = featureStatusKey(feature);
            if (layerFilters.length && visibleLayers.indexOf(layerKey) === -1) {
                return false;
            }
            if (
                statusKey &&
                statusFilters.length &&
                visibleStatuses.indexOf(statusKey) === -1
            ) {
                return false;
            }
            return (
                (!layerFilters.length || visibleLayers.length > 0) &&
                (!statusKey || !statusFilters.length || visibleStatuses.length > 0)
            );
        }

        function networkPlantFeatureVisible(feature) {
            var properties = feature.properties || {};
            var assetType = String(properties.type || "");
            if (assetType === "fiber_segment") {
                return (
                    plantSegmentsVisible &&
                    (!visiblePlantSegmentTypes ||
                        visiblePlantSegmentTypes.indexOf(
                            String(properties.segment_type || ""),
                        ) !== -1)
                );
            }
            var selectedTypes = selectedValues(poiFilters);
            return !poiFilters.length || selectedTypes.indexOf(assetType) !== -1;
        }

        function syncNetworkPlantVisibility() {
            networkPlantLayerEntries.forEach(function (entry) {
                var shouldShow = networkPlantFeatureVisible(entry.feature);
                var isShown = map.hasLayer(entry.layer);
                if (shouldShow && !isShown) {
                    entry.layer.addTo(map);
                } else if (!shouldShow && isShown) {
                    map.removeLayer(entry.layer);
                }
            });
            syncPlantToolbarState();
        }

        function syncContextLayerVisibility() {
            contextLayerEntries.forEach(function (entry) {
                var shouldShow = contextFeatureVisible(entry.feature);
                var isShown = map.hasLayer(entry.layer);
                if (shouldShow && !isShown) {
                    entry.layer.addTo(map);
                } else if (!shouldShow && isShown) {
                    map.removeLayer(entry.layer);
                }
            });
            updateFilterSummary();
        }

        function updateFilterSummary() {
            if (!filterSummaryElement) return;
            var visibleContextCount = contextLayerEntries.filter(function (entry) {
                return contextFeatureVisible(entry.feature);
            }).length;
            var visiblePlantCount = networkPlantLayerEntries.filter(
                function (entry) {
                    return networkPlantFeatureVisible(entry.feature);
                },
            ).length;
            var layerLabels = selectedValues(layerFilters).map(labelForValue);
            var statusLabels = selectedValues(statusFilters).map(labelForValue);
            var poiLabels = selectedPoiTypes().map(labelForValue);
            var parts = [
                visibleContextCount + " route/context feature" + (visibleContextCount === 1 ? "" : "s"),
                visiblePlantCount +
                    " canonical plant feature" +
                    (visiblePlantCount === 1 ? "" : "s"),
                nearbyPointCount + " reference plant point" + (nearbyPointCount === 1 ? "" : "s"),
            ];
            if (layerFilters.length) {
                parts.push("layers: " + (layerLabels.join(", ") || "none"));
            }
            if (statusFilters.length) {
                parts.push("statuses: " + (statusLabels.join(", ") || "none"));
            }
            if (poiFilters.length) {
                parts.push("points: " + (poiLabels.join(", ") || "default"));
            }
            filterSummaryElement.textContent = parts.join(" | ");
        }

        function networkPlantIcon(assetType) {
            var paths = {
                fdh_cabinet:
                    '<path d="M4 4h16v16H4zM8 8h3v3H8zm5 0h3v3h-3zM8 13h3v3H8zm5 0h3v3h-3z"/>',
                splice_closure:
                    '<circle cx="12" cy="12" r="4"/><path d="M12 2v6m0 8v6M2 12h6m8 0h6"/>',
                fiber_access_point:
                    '<path d="M12 3a6 6 0 0 0-6 6c0 4.5 6 12 6 12s6-7.5 6-12a6 6 0 0 0-6-6zm0 8.5A2.5 2.5 0 1 1 12 6a2.5 2.5 0 0 1 0 5.5z"/>',
                service_building:
                    '<path d="M4 21V5h10v4h6v12h-6v-4h-4v4zm4-12h2V7H8zm0 4h2v-2H8zm6 0h2v-2h-2zm0 4h2v-2h-2z"/>',
            };
            return window.L.divIcon({
                className: "",
                html:
                    '<span class="vendor-plant-marker" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor">' +
                    (paths[assetType] || paths.fdh_cabinet) +
                    "</svg></span>",
                iconSize: [28, 28],
                iconAnchor: [14, 14],
                popupAnchor: [0, -16],
            });
        }

        var networkPlantFeatures = (
            (config.networkPlantGeojson && config.networkPlantGeojson.features) || []
        );
        if (networkPlantFeatures.length) {
            var networkPlantLayer = window.L.geoJSON(
                { type: "FeatureCollection", features: networkPlantFeatures },
                {
                    style: function (feature) {
                        var properties = feature.properties || {};
                        var colors = {
                            feeder: "#2563eb",
                            distribution: "#0ea5e9",
                            drop: "#22d3ee",
                        };
                        return {
                            color: colors[properties.segment_type] || "#38bdf8",
                            weight: 3,
                            opacity: 0.8,
                        };
                    },
                    pointToLayer: function (feature, latlng) {
                        var properties = feature.properties || {};
                        return window.L.marker(latlng, {
                            icon: networkPlantIcon(properties.type),
                            keyboard: true,
                            title: properties.name || "Network plant",
                        });
                    },
                    onEachFeature: function (feature, layer) {
                        var properties = feature.properties || {};
                        networkPlantLayerEntries.push({
                            feature: feature,
                            layer: layer,
                        });
                        var popup = document.createElement("div");
                        var heading = document.createElement("strong");
                        heading.textContent = properties.name || "Network plant";
                        popup.appendChild(heading);
                        popup.appendChild(document.createElement("br"));
                        popup.appendChild(
                            document.createTextNode(
                                String(
                                    properties.type || "network asset",
                                ).replaceAll("_", " "),
                            ),
                        );
                        layer.bindPopup(popup);
                    },
                },
            ).addTo(map);
            syncNetworkPlantVisibility();
            if (!contextFeatures.length) {
                var networkPlantBounds = networkPlantLayer.getBounds();
                if (networkPlantBounds.isValid()) {
                    map.fitBounds(networkPlantBounds, {
                        padding: [30, 30],
                        maxZoom: 16,
                    });
                }
            }
        }

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
                        contextLayerEntries.push({ feature: feature, layer: layer });
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
            syncContextLayerVisibility();
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
        var poiLayer = window.L.layerGroup().addTo(map);

        function setError(message) {
            errorElement.textContent = message || "";
            errorElement.classList.toggle("hidden", !message);
        }

        function setDisabled(element, disabled) {
            if (!element) return;
            element.disabled = disabled;
        }

        function setNearbyLoading(isLoading) {
            if (!poiNearbyElement) return;
            poiNearbyElement.disabled = isLoading;
            poiNearbyElement.setAttribute("aria-busy", isLoading ? "true" : "false");
            poiNearbyElement.textContent = isLoading
                ? "Loading points"
                : nearbyDefaultLabel || "Show near me";
        }

        function abortNearbyRequest() {
            if (!nearbyController) return;
            nearbyController.abort();
            nearbyController = null;
            setNearbyLoading(false);
        }

        function abortSearchRequest() {
            if (!searchController) return;
            searchController.abort();
            searchController = null;
        }

        function clearNearbyPoints() {
            abortNearbyRequest();
            poiLayer.clearLayers();
            nearbyPointCount = 0;
            updateFilterSummary();
        }

        function filtersForTarget(target) {
            if (target === "layer") return layerFilters;
            if (target === "status") return statusFilters;
            if (target === "poi") return poiFilters;
            return [];
        }

        function syncFilterActionState(target) {
            var inputs = filtersForTarget(target);
            if (!inputs.length) return;
            var checkedCount = inputs.filter(function (input) {
                return input.checked;
            }).length;
            filterActionButtons.forEach(function (button) {
                if (button.dataset.routeFilterTarget !== target) return;
                var action = button.dataset.routeFilterAction || "";
                var active =
                    (action === "all" && checkedCount === inputs.length) ||
                    (action === "none" && checkedCount === 0);
                button.setAttribute("aria-pressed", String(active));
                button.classList.toggle("text-emerald-400", active);
                button.classList.toggle("text-slate-400", !active);
            });
        }

        function applyFilterAction(target, action) {
            var inputs = filtersForTarget(target);
            if (!inputs.length) return;
            var checked = action === "all";
            inputs.forEach(function (input) {
                input.checked = checked;
            });
            syncFilterActionState(target);
            if (target === "poi") {
                if (plantViewElement) plantViewElement.value = "custom";
                visiblePlantSegmentTypes = null;
                syncNetworkPlantVisibility();
                abortSearchRequest();
                hideSearchResults();
                clearNearbyPoints();
                if (searchElement && searchElement.value.trim().length >= 2) {
                    runSearch();
                }
                return;
            }
            syncContextLayerVisibility();
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
            setDisabled(submitElement, !valid);
            setDisabled(undoElement, points.length === 0);
            setDisabled(clearElement, points.length === 0);
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

        function assetTypeLabel(value) {
            return labelForValue(value);
        }

        function bindPoiPopup(marker, item) {
            var popup = document.createElement("div");
            var heading = document.createElement("strong");
            heading.textContent = item.title || item.subtitle || "Reference plant";
            popup.appendChild(heading);
            popup.appendChild(document.createElement("br"));
            popup.appendChild(document.createTextNode(assetTypeLabel(item.type)));
            popup.appendChild(document.createElement("br"));
            popup.appendChild(
                document.createTextNode("Reference only, not project-assigned."),
            );
            if (item.subtitle) {
                popup.appendChild(document.createElement("br"));
                popup.appendChild(document.createTextNode(item.subtitle));
            }
            if (item.distance_m !== null && item.distance_m !== undefined) {
                popup.appendChild(document.createElement("br"));
                popup.appendChild(
                    document.createTextNode(
                        Math.round(Number(item.distance_m)) + " m away",
                    ),
                );
            }
            marker.bindPopup(popup);
        }

        function renderNearbyPoints(items) {
            poiLayer.clearLayers();
            nearbyPointCount = 0;
            items.forEach(function (item) {
                var latitude = Number(item.latitude);
                var longitude = Number(item.longitude);
                if (
                    !Number.isFinite(latitude) ||
                    !Number.isFinite(longitude)
                ) {
                    return;
                }
                var marker = window.L.circleMarker([latitude, longitude], {
                    radius: 6,
                    color: "#ffffff",
                    weight: 2,
                    fillColor: window.themeColor("semantic-info-600"),
                    fillOpacity: 0.9,
                }).addTo(poiLayer);
                bindPoiPopup(marker, item);
                nearbyPointCount += 1;
            });
            updateFilterSummary();
            if (searchHintElement) {
                searchHintElement.textContent = nearbyPointCount
                    ? nearbyPointCount + " reference plant points shown for selected types."
                    : "No selected reference plant types found nearby.";
            }
            try {
                var bounds = poiLayer.getBounds();
                if (bounds.isValid()) {
                    map.fitBounds(bounds, { padding: [30, 30], maxZoom: 17 });
                }
            } catch (error) {
                /* No nearby points to fit. */
            }
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
                showSearchMessage("No selected reference plant types match.");
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
                        "Use decimal coordinates like 6.5244, 3.3792 or search selected reference plant types.";
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
            abortSearchRequest();
            searchController = new AbortController();
            showSearchMessage("Searching selected reference plant types...");
            fetch(
                "/api/v1/field/vendor/map-assets/search?types=" +
                    encodeURIComponent(selectedPoiTypes().join(",")) +
                    "&limit=20&q=" +
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

        function loadNearbyPoints(latitude, longitude) {
            abortNearbyRequest();
            nearbyController = new AbortController();
            setNearbyLoading(true);
            var radius = Number(poiRadiusElement ? poiRadiusElement.value : 1000);
            if (!Number.isFinite(radius) || radius <= 0) radius = 1000;
            fetch(
                "/api/v1/field/vendor/map-assets/nearby?lat=" +
                    encodeURIComponent(latitude) +
                    "&lng=" +
                    encodeURIComponent(longitude) +
                    "&radius_m=" +
                    encodeURIComponent(radius) +
                    "&types=" +
                    encodeURIComponent(selectedPoiTypes().join(",")) +
                    "&limit=100",
                { signal: nearbyController.signal },
            )
                .then(function (response) {
                    if (!response.ok) throw new Error("nearby_failed");
                    return response.json();
                })
                .then(function (payload) {
                    nearbyController = null;
                    setNearbyLoading(false);
                    renderNearbyPoints(
                        Array.isArray(payload.items) ? payload.items : [],
                    );
                })
                .catch(function (error) {
                    if (error.name === "AbortError") return;
                    nearbyController = null;
                    setNearbyLoading(false);
                    setError("Nearby map points are unavailable right now.");
                });
        }

        map.on("click", function (event) {
            addPoint(event.latlng.lat, event.latlng.lng);
        });
        if (undoElement) {
            undoElement.addEventListener("click", function () {
                points.pop();
                redraw();
            });
        }
        if (clearElement) {
            clearElement.addEventListener("click", function () {
                points = [];
                lengthElement.value = "";
                redraw();
            });
        }
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
                abortSearchRequest();
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
        layerFilters.concat(statusFilters).forEach(function (input) {
            input.addEventListener("change", function () {
                syncFilterActionState(
                    input.hasAttribute("data-route-layer-filter") ? "layer" : "status",
                );
                syncContextLayerVisibility();
            });
        });
        filterActionButtons.forEach(function (button) {
            button.addEventListener("click", function () {
                applyFilterAction(
                    button.dataset.routeFilterTarget || "",
                    button.dataset.routeFilterAction || "",
                );
            });
        });
        plantFilterButtons.forEach(function (button) {
            button.addEventListener("click", function () {
                var assetType = button.dataset.routePlantFilter || "";
                if (plantViewElement) plantViewElement.value = "custom";
                visiblePlantSegmentTypes = null;
                if (assetType === "fiber_segment") {
                    plantSegmentsVisible = !plantSegmentsVisible;
                    syncNetworkPlantVisibility();
                    updateFilterSummary();
                    return;
                }
                var input = poiFilters.find(function (candidate) {
                    return candidate.value === assetType;
                });
                if (!input) return;
                input.checked = !input.checked;
                input.dispatchEvent(new Event("change", { bubbles: true }));
            });
        });
        if (plantViewElement) {
            plantViewElement.addEventListener("change", function () {
                applyPlantViewPreset(plantViewElement.value);
            });
        }
        poiFilters.forEach(function (input) {
            input.addEventListener("change", function () {
                if (plantViewElement) plantViewElement.value = "custom";
                visiblePlantSegmentTypes = null;
                syncFilterActionState("poi");
                syncNetworkPlantVisibility();
                abortSearchRequest();
                hideSearchResults();
                clearNearbyPoints();
                if (searchElement && searchElement.value.trim().length >= 2) {
                    runSearch();
                }
            });
        });
        if (poiRadiusElement) {
            poiRadiusElement.addEventListener("change", function () {
                clearNearbyPoints();
                if (searchHintElement) {
                    searchHintElement.textContent =
                        "Nearby points cleared. Use Show near me to load the new radius.";
                }
            });
        }
        if (poiNearbyElement) {
            poiNearbyElement.addEventListener("click", function () {
                setError("");
                mapElement.scrollIntoView({ behavior: "smooth", block: "center" });
                if (!navigator.geolocation) {
                    setError("Location is not available on this device.");
                    return;
                }
                navigator.geolocation.getCurrentPosition(
                    function (position) {
                        loadNearbyPoints(
                            position.coords.latitude,
                            position.coords.longitude,
                        );
                        map.setView(
                            [position.coords.latitude, position.coords.longitude],
                            16,
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
        }
        if (poiClearElement) {
            poiClearElement.addEventListener("click", function () {
                clearNearbyPoints();
                if (searchHintElement) {
                    searchHintElement.textContent =
                        "Use decimal coordinates like 6.5244, 3.3792 or search selected reference plant types.";
                }
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
        }
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

        ["layer", "status", "poi"].forEach(syncFilterActionState);
        updateFilterSummary();

        setTimeout(function () {
            map.invalidateSize();
        }, 150);
    }

    window.VendorRouteAuthoring = { mount: mount };
})(window, document);
