(function (root, factory) {
    const api = factory(root);
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : null, function (root) {
    'use strict';

    const FEATURE_LAYER = Object.freeze({
        pop_site: 'pop',
        fdh_cabinet: 'fdh',
        splice_closure: 'closures',
        access_point: 'access_points',
        support_structure: 'support_structures',
        network_device: 'network_devices',
        ont: 'onts',
        olt_device: 'olt',
        service_building: 'service_buildings'
    });

    function layerForFeature(feature) {
        const properties = feature && feature.properties ? feature.properties : {};
        if (properties.type === 'customer') {
            return properties.connectivity && properties.connectivity.layer === 'connected'
                ? 'customers_connected'
                : 'customers_not_connected';
        }
        if (properties.type === 'fiber_segment') return properties.segment_type || 'distribution';
        return FEATURE_LAYER[properties.type] || null;
    }

    function uniqueFeatures(features) {
        const seen = new Set();
        return (features || []).filter((feature) => {
            const properties = feature && feature.properties ? feature.properties : {};
            const key = `${properties.type || ''}:${properties.id || ''}`;
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
    }

    function searchableText(feature) {
        const properties = feature && feature.properties ? feature.properties : {};
        return [
            properties.name,
            properties.id,
            properties.code,
            properties.address,
            properties.street,
            properties.city,
            properties.type,
            properties.status,
            properties.segment_type,
            properties.pop_site_name
        ].filter(Boolean).join(' ').toLowerCase();
    }

    function parseCoordinates(value) {
        const match = String(value || '').trim().match(/^(-?\d+(?:\.\d+)?)\s*(?:,|;|\s+)\s*(-?\d+(?:\.\d+)?)$/);
        if (!match) return null;
        const latitude = Number(match[1]);
        const longitude = Number(match[2]);
        if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
        if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) return null;
        return { latitude, longitude };
    }

    function haversineMeters(a, b) {
        const radians = (degrees) => degrees * Math.PI / 180;
        const dLat = radians(b.latitude - a.latitude);
        const dLon = radians(b.longitude - a.longitude);
        const lat1 = radians(a.latitude);
        const lat2 = radians(b.latitude);
        const value = Math.sin(dLat / 2) ** 2
            + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
        return 6371000 * 2 * Math.atan2(Math.sqrt(value), Math.sqrt(1 - value));
    }

    function nearestFeature(origin, features, featureType) {
        let nearest = null;
        (features || []).forEach((feature) => {
            const properties = feature && feature.properties ? feature.properties : {};
            const geometry = feature && feature.geometry ? feature.geometry : {};
            if (properties.type !== featureType || geometry.type !== 'Point') return;
            const coordinates = geometry.coordinates || [];
            const target = { longitude: Number(coordinates[0]), latitude: Number(coordinates[1]) };
            if (!Number.isFinite(target.latitude) || !Number.isFinite(target.longitude)) return;
            const distance = haversineMeters(origin, target);
            if (!nearest || distance < nearest.distanceMeters) {
                nearest = { feature, distanceMeters: distance };
            }
        });
        return nearest;
    }

    function topologyMarkerModels(segmentTopology) {
        const byId = new Map();
        (segmentTopology || []).forEach((segment) => {
            ['from_endpoint', 'to_endpoint'].forEach((key) => {
                const endpoint = segment[key];
                if (!endpoint || !endpoint.id) return;
                if (!Number.isFinite(endpoint.latitude) || !Number.isFinite(endpoint.longitude)) return;
                const existing = byId.get(endpoint.id) || { endpoint, segments: [] };
                existing.segments.push({
                    id: segment.id,
                    name: segment.name,
                    topology_status: segment.topology_status,
                    geometry_status: segment.geometry_status
                });
                byId.set(endpoint.id, existing);
            });
        });
        return Array.from(byId.values());
    }

    const GOVERNED_ASSET_TYPES = Object.freeze([
        'fdh_cabinet', 'splice_closure', 'access_point', 'support_structure'
    ]);

    function editableAssetType(feature) {
        const properties = feature && feature.properties ? feature.properties : {};
        return GOVERNED_ASSET_TYPES.includes(properties.type) ? properties.type : null;
    }

    function proposalDiffRows(before, after) {
        const keys = new Set(Object.keys(before || {}).concat(Object.keys(after || {})));
        return Array.from(keys).sort().filter((key) => {
            return JSON.stringify(before ? before[key] : null) !== JSON.stringify(after ? after[key] : null);
        }).map((key) => ({
            field: key,
            before: before && before[key] != null ? before[key] : null,
            after: after && after[key] != null ? after[key] : null
        }));
    }

    function proposalPreviewModels(proposals) {
        return (proposals || []).filter((proposal) => proposal.status === 'pending').flatMap((proposal) => {
            const before = proposal.before || null;
            const after = proposal.after || null;
            if (!after || !Number.isFinite(Number(after.latitude)) || !Number.isFinite(Number(after.longitude))) return [];
            const model = {
                proposalId: proposal.id,
                operation: proposal.operation,
                assetType: proposal.asset_type,
                after: { latitude: Number(after.latitude), longitude: Number(after.longitude) },
                before: null,
                topology: false,
                label: 'Pending proposal preview — not canonical topology'
            };
            if (before && Number.isFinite(Number(before.latitude)) && Number.isFinite(Number(before.longitude))) {
                model.before = { latitude: Number(before.latitude), longitude: Number(before.longitude) };
            }
            return [model];
        });
    }

    function captureLeafletMap(globalObject) {
        if (!globalObject) return;
        let leaflet = globalObject.L;
        const wrap = (value) => {
            if (!value) return;
            let mapFactory = value.map;
            const install = (factory) => {
                if (typeof factory !== 'function' || factory.__networkMapV2Wrapped) return factory;
                const wrappedMap = function () {
                    const map = factory.apply(this, arguments);
                    globalObject.__networkMapV2Map = map;
                    return map;
                };
                wrappedMap.__networkMapV2Wrapped = true;
                return wrappedMap;
            };
            if (typeof mapFactory === 'function') {
                value.map = install(mapFactory);
                return;
            }
            const mapDescriptor = Object.getOwnPropertyDescriptor(value, 'map');
            if (mapDescriptor && !mapDescriptor.configurable) return;
            Object.defineProperty(value, 'map', {
                configurable: true,
                enumerable: true,
                get: function () { return mapFactory; },
                set: function (factory) { mapFactory = install(factory); }
            });
        };
        if (leaflet) {
            wrap(leaflet);
            return;
        }
        const descriptor = Object.getOwnPropertyDescriptor(globalObject, 'L');
        if (descriptor && !descriptor.configurable) return;
        Object.defineProperty(globalObject, 'L', {
            configurable: true,
            enumerable: true,
            get: function () { return leaflet; },
            set: function (value) {
                leaflet = value;
                wrap(value);
            }
        });
    }

    const api = {
        layerForFeature,
        uniqueFeatures,
        searchableText,
        parseCoordinates,
        haversineMeters,
        nearestFeature,
        topologyMarkerModels,
        editableAssetType,
        proposalDiffRows,
        proposalPreviewModels,
        captureLeafletMap
    };

    if (!root || !root.document) return api;
    captureLeafletMap(root);

    root.document.addEventListener('DOMContentLoaded', function () {
        try {
            initialise(root);
        } catch (error) {
            const host = root.document.querySelector('.network-map-layout');
            if (host) {
                const alert = root.document.createElement('div');
                alert.className = 'rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700';
                alert.textContent = 'Network Map V2 overlays are unavailable. The original map remains usable.';
                host.before(alert);
            }
            root.console.error('Network Map V2 initialization failed', error);
        }
    });

    function initialise(globalObject) {
        const document = globalObject.document;
        const L = globalObject.L;
        const map = globalObject.__networkMapV2Map;
        const dataElement = document.getElementById('network-map-v2-data');
        if (!L || !map || !dataElement) throw new Error('V2 map prerequisites are missing');

        const payload = JSON.parse(dataElement.textContent || '{}');
        const overlay = payload.overlay || {};
        const baseFeatures = Array.isArray(payload.base_features) ? payload.base_features : [];
        const additionalFeatures = Array.isArray(overlay.additional_features) ? overlay.additional_features : [];
        const features = uniqueFeatures(baseFeatures.concat(additionalFeatures));
        const topology = Array.isArray(overlay.segment_topology) ? overlay.segment_topology : [];
        const counts = overlay.layer_counts || {};
        const unavailable = Array.isArray(overlay.unavailable_layers) ? overlay.unavailable_layers : [];
        const governance = payload.governance || {};

        const layers = {
            olt: L.layerGroup().addTo(map),
            service_buildings: L.layerGroup().addTo(map),
            topology_endpoints: L.layerGroup().addTo(map),
            proposal_previews: L.layerGroup().addTo(map),
            tools: L.layerGroup().addTo(map),
            selection: L.layerGroup().addTo(map)
        };
        const renderedFeatureLayers = new Map();
        renderAdditionalFeatures({ L, map, layers, features: additionalFeatures, renderedFeatureLayers, topology, document });
        renderTopologyEndpoints({ L, layers, topology, document });
        renderProposalPreviews({ L, layers, proposals: governance.items || [] });
        installV2Panel({ document, L, map, layers, features, topology, counts, unavailable, overlay });
        installCounts({ document, counts });
        installLayerControls({ document, map, layers, counts, unavailable });
        installSearch({ document, L, map, layers, features, topology, renderedFeatureLayers });
        installDeepLinks({ globalObject, document, L, map, layers, features, topology, renderedFeatureLayers });
        installGovernanceWorkbench({ globalObject, document, L, map, layers, features, governance });
    }

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function humanize(value) {
        return String(value || 'unknown').replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
    }

    function formatDistance(meters) {
        return meters >= 1000 ? `${(meters / 1000).toFixed(2)} km` : `${meters.toFixed(0)} m`;
    }

    function endpointSummary(endpoint) {
        if (!endpoint || !endpoint.id) return 'Missing canonical endpoint';
        const label = endpoint.name || endpoint.id;
        const connection = endpoint.has_explicit_connection ? 'explicitly attached' : 'disconnected';
        return `${escapeHtml(label)} (${escapeHtml(endpoint.endpoint_type || 'other')}; ${connection})`;
    }

    function featurePopup(feature, topology) {
        const properties = feature.properties || {};
        const segment = topology.find((item) => item.id === properties.id);
        let html = `<div class="text-sm"><strong>${escapeHtml(properties.name || 'Unnamed')}</strong>`;
        html += `<div class="mt-1 text-xs text-slate-500">${escapeHtml(humanize(properties.type))}</div>`;
        if (properties.code) html += `<div>Code: ${escapeHtml(properties.code)}</div>`;
        if (properties.status) html += `<div>Status: ${escapeHtml(humanize(properties.status))}</div>`;
        if (properties.pop_site_name) html += `<div>POP: ${escapeHtml(properties.pop_site_name)}</div>`;
        if (properties.street || properties.city) html += `<div>Location: ${escapeHtml([properties.street, properties.city].filter(Boolean).join(', '))}</div>`;
        if (properties.segment_type) html += `<div>Class: ${escapeHtml(humanize(properties.segment_type))}</div>`;
        if (properties.fiber_count) html += `<div>Fibres: ${escapeHtml(properties.fiber_count)}</div>`;
        if (properties.length_m) html += `<div>Length: ${escapeHtml(formatDistance(Number(properties.length_m)))}</div>`;
        if (segment) {
            html += `<hr class="my-2"><div>Geometry: ${escapeHtml(humanize(segment.geometry_status))}</div>`;
            html += `<div>Topology: ${escapeHtml(humanize(segment.topology_status))}</div>`;
            html += `<div>From: ${endpointSummary(segment.from_endpoint)}</div>`;
            html += `<div>To: ${endpointSummary(segment.to_endpoint)}</div>`;
        }
        html += `<div class="mt-2 font-mono text-xs">${escapeHtml(properties.id || '')}</div></div>`;
        return html;
    }

    function showDetailPanel(document, feature, topology) {
        const panel = document.getElementById('asset-detail-panel');
        const content = document.getElementById('asset-detail-content');
        if (!panel || !content) return;
        content.innerHTML = featurePopup(feature, topology);
        panel.classList.remove('hidden');
        document.dispatchEvent(new CustomEvent('network-map-v2:asset-selected', { detail: { feature } }));
    }

    function pointIcon(L, color, label) {
        return L.divIcon({
            className: '',
            html: `<span aria-label="${escapeHtml(label)}" style="display:flex;width:24px;height:24px;align-items:center;justify-content:center;border-radius:9999px;border:2px solid white;background:${color};color:white;font-size:10px;font-weight:700;box-shadow:0 2px 8px rgba(15,23,42,.35)">${escapeHtml(label)}</span>`,
            iconSize: [24, 24],
            iconAnchor: [12, 12],
            popupAnchor: [0, -12]
        });
    }

    function renderAdditionalFeatures(context) {
        const { L, layers, features, renderedFeatureLayers, topology, document } = context;
        features.forEach((feature) => {
            const geometry = feature.geometry || {};
            const properties = feature.properties || {};
            if (geometry.type !== 'Point' || !Array.isArray(geometry.coordinates)) return;
            const latitude = Number(geometry.coordinates[1]);
            const longitude = Number(geometry.coordinates[0]);
            if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return;
            const config = properties.type === 'olt_device'
                ? { layer: 'olt', color: '#e11d48', label: 'OLT' }
                : { layer: 'service_buildings', color: '#0284c7', label: 'B' };
            const marker = L.marker([latitude, longitude], { icon: pointIcon(L, config.color, config.label), zIndexOffset: 450 });
            marker.bindPopup(featurePopup(feature, topology));
            marker.on('click', () => showDetailPanel(document, feature, topology));
            marker.addTo(layers[config.layer]);
            renderedFeatureLayers.set(`${properties.type}:${properties.id}`, marker);
        });
    }

    function renderTopologyEndpoints(context) {
        const { L, layers, topology } = context;
        topologyMarkerModels(topology).forEach((model) => {
            const endpoint = model.endpoint;
            const hasProblem = model.segments.some((segment) => segment.topology_status !== 'connected');
            const marker = L.circleMarker([endpoint.latitude, endpoint.longitude], {
                radius: 6,
                color: hasProblem ? '#dc2626' : '#0f766e',
                fillColor: hasProblem ? '#fecaca' : '#99f6e4',
                fillOpacity: 0.95,
                weight: 2
            });
            const segmentRows = model.segments.map((segment) =>
                `<li>${escapeHtml(segment.name)} — ${escapeHtml(humanize(segment.topology_status))}</li>`
            ).join('');
            marker.bindPopup(`<div class="text-sm"><strong>${escapeHtml(endpoint.name || endpoint.id)}</strong><div>${escapeHtml(humanize(endpoint.endpoint_type))}</div><div>${endpoint.has_explicit_connection ? 'Explicit connection' : 'Disconnected endpoint'}</div><ul class="mt-1 list-disc pl-4">${segmentRows}</ul><div class="mt-2 text-xs">Endpoint markers never create connectivity by proximity.</div></div>`);
            marker.addTo(layers.topology_endpoints);
        });
    }

    function renderProposalPreviews(context) {
        const { L, layers, proposals } = context;
        proposalPreviewModels(proposals).forEach((preview) => {
            const after = [preview.after.latitude, preview.after.longitude];
            const marker = L.circleMarker(after, {
                radius: 9,
                color: '#7c3aed',
                fillColor: '#ddd6fe',
                fillOpacity: 0.75,
                weight: 3,
                dashArray: '4 3'
            }).bindPopup(`<strong>${escapeHtml(humanize(preview.operation))} proposal</strong><div>${escapeHtml(preview.label)}</div><div class="mt-1 font-mono text-xs">${escapeHtml(preview.proposalId)}</div>`);
            marker.addTo(layers.proposal_previews);
            if (preview.operation === 'move' && preview.before) {
                L.polyline(
                    [[preview.before.latitude, preview.before.longitude], after],
                    { color: '#7c3aed', weight: 2, opacity: 0.8, dashArray: '5 7' }
                ).bindPopup('Proposed movement only — this dashed guide is not fibre geometry or topology.')
                    .addTo(layers.proposal_previews);
            }
        });
    }

    function proposalValue(value) {
        if (value == null || value === '') return '—';
        return typeof value === 'object' ? JSON.stringify(value) : String(value);
    }

    function proposalCard(proposal, governance) {
        const rows = proposalDiffRows(proposal.before, proposal.after);
        const history = (proposal.audit_history || []).map((entry) =>
            `<li>${escapeHtml(humanize(entry.action))} · ${escapeHtml(entry.actor_label || entry.actor_id || 'unknown actor')} · ${escapeHtml(entry.occurred_at || '')}</li>`
        ).join('');
        const canReview = governance.can_review && proposal.status === 'pending'
            && String(proposal.requested_by_actor_id || '') !== String(governance.actor_id || '');
        return `<article class="rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-900" data-proposal-id="${escapeHtml(proposal.id)}">
            <div class="flex flex-wrap items-center justify-between gap-2"><strong>${escapeHtml(humanize(proposal.operation))} ${escapeHtml(humanize(proposal.asset_type))}</strong><span class="rounded bg-slate-100 px-2 py-1 text-xs dark:bg-slate-700">${escapeHtml(humanize(proposal.status))}</span></div>
            <p class="mt-1 text-xs text-slate-600 dark:text-slate-300">${escapeHtml(proposal.request_reason || '')}</p>
            <div class="mt-2 overflow-x-auto"><table class="w-full text-xs"><thead><tr><th class="text-left">Field</th><th class="text-left">Before</th><th class="text-left">After</th></tr></thead><tbody>${rows.map((row) => `<tr><td class="py-1 pr-2 font-medium">${escapeHtml(humanize(row.field))}</td><td class="py-1 pr-2">${escapeHtml(proposalValue(row.before))}</td><td class="py-1">${escapeHtml(proposalValue(row.after))}</td></tr>`).join('') || '<tr><td colspan="3" class="py-2 text-slate-500">No changed values available.</td></tr>'}</tbody></table></div>
            ${proposal.review_notes ? `<p class="mt-2 text-xs"><strong>Reviewer comments:</strong> ${escapeHtml(proposal.review_notes)}</p>` : ''}
            <details class="mt-2 text-xs"><summary class="cursor-pointer font-medium">Audit history (${(proposal.audit_history || []).length})</summary><ol class="mt-1 space-y-1">${history || '<li>No audit events are available.</li>'}</ol></details>
            ${canReview ? `<div class="mt-3"><label class="block text-xs font-medium">Reviewer comments<textarea data-review-notes rows="2" required class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"></textarea></label><div class="mt-2 flex gap-2"><button type="button" data-review-action="approve" class="rounded bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white">Approve</button><button type="button" data-review-action="reject" class="rounded bg-red-600 px-3 py-1.5 text-xs font-semibold text-white">Reject</button></div></div>` : ''}
        </article>`;
    }

    function installGovernanceWorkbench(context) {
        const { globalObject, document, L, map, layers, features, governance } = context;
        const controls = document.getElementById('network-map-v2-controls');
        if (!controls) return;
        const editable = features.filter((feature) => editableAssetType(feature));
        const panel = document.createElement('section');
        panel.id = 'network-map-v2-governance';
        panel.className = 'mt-4 rounded-xl border border-violet-200 bg-violet-50 p-4 dark:border-violet-800 dark:bg-violet-950/30';
        const permissionMessage = governance.can_propose
            ? 'Changes remain pending until an independent authorized reviewer approves them.'
            : 'You can inspect proposal evidence but cannot submit asset changes.';
        panel.innerHTML = `<div class="flex flex-wrap items-start justify-between gap-3"><div><h2 class="font-semibold text-slate-900 dark:text-white">Governed asset changes</h2><p class="text-xs text-slate-600 dark:text-slate-300">${escapeHtml(permissionMessage)}</p></div><label class="flex items-center gap-2 text-xs"><input id="v2-proposal-preview-toggle" type="checkbox" checked> Proposal previews (${Number(governance.total || 0)})</label></div>
            <p id="v2-governance-message" class="mt-3 rounded bg-white/70 p-2 text-xs text-slate-600 dark:bg-slate-900/50 dark:text-slate-300">${escapeHtml(governance.unavailable_message || '')}</p>
            ${governance.can_propose ? `<form id="v2-proposal-form" class="mt-3 grid gap-3 rounded-lg border border-violet-200 bg-white p-3 dark:border-violet-800 dark:bg-slate-900 sm:grid-cols-2">
                <label class="text-xs font-medium">Operation<select name="operation" class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"><option value="create">Create</option><option value="edit">Edit</option><option value="move">Move</option></select></label>
                <label class="text-xs font-medium">Asset type<select name="asset_type" class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800">${GOVERNED_ASSET_TYPES.map((type) => `<option value="${type}">${escapeHtml(humanize(type))}</option>`).join('')}</select></label>
                <label class="text-xs font-medium sm:col-span-2">Existing asset<select name="asset_id" class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"><option value="">Select a mapped asset</option>${editable.map((feature) => `<option value="${escapeHtml(feature.properties.id)}" data-type="${escapeHtml(feature.properties.type)}">${escapeHtml(feature.properties.name || feature.properties.code || feature.properties.id)} · ${escapeHtml(humanize(feature.properties.type))}</option>`).join('')}</select></label>
                <label class="text-xs font-medium">Name<input name="name" maxlength="160" class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"></label>
                <label class="text-xs font-medium">Code<input name="code" maxlength="80" class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"></label>
                <label class="text-xs font-medium">Latitude<input name="latitude" type="number" min="-90" max="90" step="any" class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"></label>
                <label class="text-xs font-medium">Longitude<input name="longitude" type="number" min="-180" max="180" step="any" class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"></label>
                <label class="text-xs font-medium">Access-point type<input name="access_point_type" maxlength="60" class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"></label>
                <label class="text-xs font-medium">Placement<input name="placement" maxlength="60" class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"></label>
                <label class="text-xs font-medium">Street<input name="street" maxlength="200" class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"></label>
                <label class="text-xs font-medium">City<input name="city" maxlength="100" class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"></label>
                <label class="text-xs font-medium sm:col-span-2">Support type<select name="support_type" class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"><option value="">Default pole</option><option value="pole">Pole</option><option value="tower">Tower</option><option value="building_attachment">Building attachment</option><option value="other">Other</option></select></label>
                <label class="text-xs font-medium sm:col-span-2">Notes<textarea name="notes" maxlength="4000" rows="2" class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"></textarea></label>
                <label class="text-xs font-medium sm:col-span-2">Reason<textarea name="reason" minlength="3" maxlength="1000" rows="2" required class="mt-1 w-full rounded border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800"></textarea></label>
                <div class="flex flex-wrap gap-2 sm:col-span-2"><button type="button" id="v2-place-proposal" class="rounded border border-violet-400 px-3 py-2 text-xs font-semibold text-violet-700 dark:text-violet-200">Place preview on map</button><button type="submit" class="rounded bg-violet-700 px-3 py-2 text-xs font-semibold text-white">Submit pending proposal</button></div>
                <p class="text-xs text-slate-500 sm:col-span-2">A draggable marker changes only the proposal coordinates. It never moves the canonical marker or any fibre route.</p>
            </form>` : ''}
            <div class="mt-4"><h3 class="text-sm font-semibold">Proposal history</h3><div id="v2-proposal-list" class="mt-2 grid gap-3">${(governance.items || []).map((proposal) => proposalCard(proposal, governance)).join('') || '<p class="rounded-lg border border-dashed border-slate-300 p-4 text-sm text-slate-500">No asset proposals exist yet. This is a valid empty state.</p>'}</div>${governance.truncated ? `<p class="mt-2 text-xs text-amber-700">Showing ${Number(governance.limit || 0)} of ${Number(governance.total || 0)} proposals.</p>` : ''}</div>`;
        controls.after(panel);

        const previewToggle = document.getElementById('v2-proposal-preview-toggle');
        previewToggle.addEventListener('change', () => {
            if (previewToggle.checked) map.addLayer(layers.proposal_previews);
            else map.removeLayer(layers.proposal_previews);
        });
        if (!governance.can_propose) {
            installReviewActions({ globalObject, document, governance });
            return;
        }
        const form = document.getElementById('v2-proposal-form');
        const operation = form.elements.operation;
        const assetType = form.elements.asset_type;
        const assetId = form.elements.asset_id;
        const latitude = form.elements.latitude;
        const longitude = form.elements.longitude;
        let draftMarker = null;

        const selectedFeature = () => editable.find((feature) => String(feature.properties.id) === String(assetId.value));
        const syncForm = () => {
            const feature = selectedFeature();
            const requiresAsset = operation.value !== 'create';
            assetId.disabled = !requiresAsset;
            assetId.required = requiresAsset;
            assetType.disabled = requiresAsset;
            const requiresCoordinates = operation.value !== 'edit';
            latitude.disabled = !requiresCoordinates;
            longitude.disabled = !requiresCoordinates;
            latitude.required = requiresCoordinates;
            longitude.required = requiresCoordinates;
            if (!feature || !requiresAsset) return;
            const properties = feature.properties || {};
            const coordinates = feature.geometry && feature.geometry.coordinates ? feature.geometry.coordinates : [];
            assetType.value = properties.type;
            form.elements.name.value = properties.name || '';
            form.elements.code.value = properties.code || '';
            form.elements.notes.value = properties.notes || '';
            form.elements.access_point_type.value = properties.access_point_type || '';
            form.elements.placement.value = properties.placement || '';
            form.elements.street.value = properties.street || '';
            form.elements.city.value = properties.city || '';
            form.elements.support_type.value = properties.support_type || '';
            latitude.value = coordinates[1] == null ? '' : coordinates[1];
            longitude.value = coordinates[0] == null ? '' : coordinates[0];
            const moving = operation.value === 'move';
            form.elements.name.disabled = moving;
            form.elements.code.disabled = moving;
            form.elements.notes.disabled = moving;
            form.elements.access_point_type.disabled = moving;
            form.elements.placement.disabled = moving;
            form.elements.street.disabled = moving;
            form.elements.city.disabled = moving;
            form.elements.support_type.disabled = moving;
        };
        operation.addEventListener('change', () => {
            form.elements.name.disabled = false;
            form.elements.code.disabled = false;
            form.elements.notes.disabled = false;
            form.elements.access_point_type.disabled = false;
            form.elements.placement.disabled = false;
            form.elements.street.disabled = false;
            form.elements.city.disabled = false;
            form.elements.support_type.disabled = false;
            syncForm();
        });
        assetId.addEventListener('change', syncForm);
        document.addEventListener('network-map-v2:asset-selected', (event) => {
            const feature = event.detail && event.detail.feature;
            if (!editableAssetType(feature)) return;
            operation.value = 'edit';
            assetId.value = feature.properties.id;
            syncForm();
        });
        document.getElementById('v2-place-proposal').addEventListener('click', () => {
            if (draftMarker) layers.tools.removeLayer(draftMarker);
            const initial = Number.isFinite(Number(latitude.value)) && Number.isFinite(Number(longitude.value))
                ? [Number(latitude.value), Number(longitude.value)]
                : [map.getCenter().lat, map.getCenter().lng];
            draftMarker = L.marker(initial, { draggable: true, icon: pointIcon(L, '#7c3aed', 'P') })
                .bindPopup('Pending proposal position only. Dragging does not mutate the asset or fibre geometry.')
                .addTo(layers.tools);
            const syncCoordinates = () => {
                const point = draftMarker.getLatLng();
                latitude.value = point.lat.toFixed(7);
                longitude.value = point.lng.toFixed(7);
            };
            syncCoordinates();
            draftMarker.on('dragend', syncCoordinates);
            draftMarker.openPopup();
        });
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            const data = new FormData(form);
            const body = {
                asset_type: assetType.value,
                operation: operation.value,
                asset_id: operation.value === 'create' ? null : assetId.value,
                reason: String(data.get('reason') || ''),
                idempotency_key: globalObject.crypto.randomUUID()
            };
            if (operation.value !== 'move') {
                body.name = String(data.get('name') || '');
                body.code = String(data.get('code') || '') || null;
                body.notes = String(data.get('notes') || '') || null;
                if (assetType.value === 'access_point') {
                    body.access_point_type = String(data.get('access_point_type') || '') || null;
                    body.placement = String(data.get('placement') || '') || null;
                    body.street = String(data.get('street') || '') || null;
                    body.city = String(data.get('city') || '') || null;
                }
                if (assetType.value === 'support_structure') {
                    body.support_type = String(data.get('support_type') || '') || null;
                }
            }
            if (operation.value !== 'edit') {
                body.latitude = Number(data.get('latitude'));
                body.longitude = Number(data.get('longitude'));
            }
            await postProposal(globalObject, '/admin/network/map-v2/proposals', body);
        });
        installReviewActions({ globalObject, document, governance });
        syncForm();
    }

    function installReviewActions(context) {
        const { globalObject, document } = context;
        document.querySelectorAll('[data-review-action]').forEach((button) => {
            button.addEventListener('click', async () => {
                const card = button.closest('[data-proposal-id]');
                const notes = card.querySelector('[data-review-notes]').value.trim();
                if (notes.length < 3) {
                    showGovernanceMessage(document, 'Reviewer comments must contain at least three characters.', true);
                    return;
                }
                const proposal = (context.governance.items || []).find((item) => item.id === card.dataset.proposalId);
                await postProposal(
                    globalObject,
                    `/admin/network/map-v2/proposals/${encodeURIComponent(proposal.id)}/${button.dataset.reviewAction}`,
                    { expected_proposal_sha256: proposal.proposal_sha256, review_notes: notes, idempotency_key: globalObject.crypto.randomUUID() }
                );
            });
        });
    }

    function showGovernanceMessage(document, message, error) {
        const element = document.getElementById('v2-governance-message');
        if (!element) return;
        element.textContent = message;
        element.classList.toggle('text-red-700', Boolean(error));
    }

    async function postProposal(globalObject, url, body) {
        const document = globalObject.document;
        showGovernanceMessage(document, 'Saving governed proposal evidence…', false);
        try {
            const response = await globalObject.fetch(url, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || ''
                },
                body: JSON.stringify(body)
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.message || 'The proposal command was refused.');
            globalObject.location.reload();
        } catch (error) {
            showGovernanceMessage(document, error.message || 'The proposal command failed.', true);
        }
    }

    function installV2Panel(context) {
        const { document, L, map, layers, features, topology, overlay } = context;
        const layout = document.querySelector('.network-map-layout');
        if (!layout) return;
        const connected = topology.filter((segment) => segment.topology_status === 'connected').length;
        const disconnected = topology.filter((segment) => segment.topology_status === 'disconnected').length;
        const incomplete = topology.filter((segment) => segment.topology_status === 'incomplete').length;
        const panel = document.createElement('section');
        panel.id = 'network-map-v2-controls';
        panel.className = 'rounded-xl border border-cyan-200 bg-cyan-50 p-4 dark:border-cyan-800 dark:bg-cyan-950/30';
        panel.innerHTML = `
            <div class="flex flex-wrap items-center justify-between gap-3">
                <div><h2 class="font-semibold text-slate-900 dark:text-white">Network Map V2</h2><p class="text-xs text-slate-600 dark:text-slate-300">Read-only parity preview. Lines are rendered only from stored route geometry.</p></div>
                <div class="flex flex-wrap gap-2">
                    <select id="v2-layer-preset" class="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-800 dark:text-white"><option value="all">All layers</option><option value="osp">OSP</option><option value="backbone">Backbone</option><option value="edge">Customer edge</option><option value="sites">Sites</option><option value="clear">Clear</option></select>
                    <button id="v2-measure" type="button" class="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium dark:border-slate-600 dark:bg-slate-800 dark:text-white">Measure distance</button>
                    <button id="v2-nearest-fdh" type="button" class="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium dark:border-slate-600 dark:bg-slate-800 dark:text-white">Nearest FDH</button>
                    <button id="v2-clear-tools" type="button" class="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium dark:border-slate-600 dark:bg-slate-800 dark:text-white">Clear tools</button>
                </div>
            </div>
            <div class="mt-3 flex flex-wrap gap-2 text-xs"><span class="rounded bg-emerald-100 px-2 py-1 text-emerald-800">Connected ${connected}</span><span class="rounded bg-red-100 px-2 py-1 text-red-800">Disconnected ${disconnected}</span><span class="rounded bg-amber-100 px-2 py-1 text-amber-800">Incomplete ${incomplete}</span><span class="rounded bg-slate-100 px-2 py-1 text-slate-700">Unmatched OLT ${Number(overlay.unmatched_olt_count || 0)}</span><span id="v2-visible-count" class="rounded bg-blue-100 px-2 py-1 text-blue-800">Visible 0</span></div>`;
        layout.before(panel);

        let mode = null;
        let measurePoints = [];
        const measureButton = document.getElementById('v2-measure');
        const nearestButton = document.getElementById('v2-nearest-fdh');
        const resetModes = () => {
            mode = null;
            measurePoints = [];
            measureButton.textContent = 'Measure distance';
            nearestButton.textContent = 'Nearest FDH';
        };
        measureButton.addEventListener('click', () => {
            layers.tools.clearLayers();
            resetModes();
            mode = 'measure';
            measureButton.textContent = 'Click map points';
        });
        nearestButton.addEventListener('click', () => {
            layers.tools.clearLayers();
            resetModes();
            mode = 'nearest';
            nearestButton.textContent = 'Click a location';
        });
        document.getElementById('v2-clear-tools').addEventListener('click', () => {
            layers.tools.clearLayers();
            layers.selection.clearLayers();
            resetModes();
        });
        map.on('click', (event) => {
            if (mode === 'measure') {
                measurePoints.push(event.latlng);
                L.circleMarker(event.latlng, { radius: 4, color: '#475569' }).addTo(layers.tools);
                if (measurePoints.length > 1) {
                    let total = 0;
                    for (let index = 1; index < measurePoints.length; index += 1) {
                        total += haversineMeters(
                            { latitude: measurePoints[index - 1].lat, longitude: measurePoints[index - 1].lng },
                            { latitude: measurePoints[index].lat, longitude: measurePoints[index].lng }
                        );
                    }
                    L.polyline(measurePoints, { color: '#475569', dashArray: '6 6', weight: 3 })
                        .bindPopup(`Measurement only — not network topology: ${escapeHtml(formatDistance(total))}`)
                        .addTo(layers.tools);
                }
            } else if (mode === 'nearest') {
                const nearest = nearestFeature(
                    { latitude: event.latlng.lat, longitude: event.latlng.lng },
                    features,
                    'fdh_cabinet'
                );
                if (!nearest) {
                    L.popup().setLatLng(event.latlng).setContent('No mapped FDH is available.').openOn(map);
                    return;
                }
                const coordinates = nearest.feature.geometry.coordinates;
                const marker = L.circleMarker([coordinates[1], coordinates[0]], { radius: 10, color: '#f97316', fillOpacity: 0.2 })
                    .bindPopup(`Nearest mapped FDH: ${escapeHtml(nearest.feature.properties.name)} (${escapeHtml(formatDistance(nearest.distanceMeters))}). No route or connectivity is implied.`)
                    .addTo(layers.tools);
                map.setView(marker.getLatLng(), Math.max(map.getZoom(), 15));
                marker.openPopup();
                resetModes();
            }
        });
        installPresets(document);
    }

    function installCounts(context) {
        const { document, counts } = context;
        const checkboxLayer = {
            'layer-pop': 'pop', 'layer-fdh': 'fdh', 'layer-closures': 'closures',
            'layer-access-points': 'access_points', 'layer-support-structures': 'support_structures',
            'layer-network-devices': 'network_devices', 'layer-onts': 'onts',
            'layer-customers-connected': 'customers_connected',
            'layer-customers-not-connected': 'customers_not_connected',
            'layer-feeder': 'feeder', 'layer-distribution': 'distribution', 'layer-drop': 'drop'
        };
        Object.entries(checkboxLayer).forEach(([checkboxId, layer]) => {
            const checkbox = document.getElementById(checkboxId);
            if (!checkbox || checkbox.dataset.v2CountInstalled) return;
            const badge = document.createElement('span');
            badge.className = 'v2-layer-count ml-auto rounded bg-slate-100 px-1.5 py-0.5 text-xs font-semibold text-slate-600 dark:bg-slate-700 dark:text-slate-300';
            badge.dataset.layer = layer;
            checkbox.closest('label').appendChild(badge);
            checkbox.dataset.v2CountInstalled = 'true';
            checkbox.addEventListener('change', () => updateVisibleCount(document, counts));
        });
        updateVisibleCount(document, counts);
    }

    function updateVisibleCount(document, counts) {
        let visible = 0;
        document.querySelectorAll('input[data-v2-layer]').forEach((checkbox) => {
            const layer = checkbox.dataset.v2Layer;
            const count = Number(counts[layer] || 0);
            if (checkbox.checked && !checkbox.disabled) visible += count;
            const badge = checkbox.closest('label') && checkbox.closest('label').querySelector('.v2-layer-count');
            if (badge) badge.textContent = String(count);
        });
        document.querySelectorAll('.v2-layer-count[data-layer]').forEach((badge) => {
            const layer = badge.dataset.layer;
            badge.textContent = String(Number(counts[layer] || 0));
            const checkbox = badge.closest('label').querySelector('input[type="checkbox"]');
            if (checkbox && checkbox.checked) visible += Number(counts[layer] || 0);
        });
        const visibleElement = document.getElementById('v2-visible-count');
        if (visibleElement) visibleElement.textContent = `Visible ${visible}`;
    }

    function installLayerControls(context) {
        const { document, map, layers, counts, unavailable } = context;
        const grid = document.querySelector('.network-map-layers .grid');
        if (!grid) return;
        const definitions = [
            { id: 'v2-layer-olt', layer: 'olt', label: 'OLT devices', color: '#e11d48' },
            { id: 'v2-layer-service-buildings', layer: 'service_buildings', label: 'Service buildings', color: '#0284c7' },
            { id: 'v2-layer-topology-endpoints', layer: 'topology_endpoints', label: 'Topology endpoints', color: '#0f766e' }
        ];
        definitions.forEach((definition) => {
            const label = document.createElement('label');
            label.className = 'flex items-center gap-2 cursor-pointer';
            label.innerHTML = `<input type="checkbox" id="${definition.id}" data-v2-layer="${definition.layer}" checked class="h-4 w-4 rounded border-slate-300"><span class="text-sm text-slate-700 dark:text-slate-300">${escapeHtml(definition.label)}</span><span class="v2-layer-count ml-auto rounded bg-slate-100 px-1.5 py-0.5 text-xs font-semibold text-slate-600 dark:bg-slate-700 dark:text-slate-300">${Number(counts[definition.layer] || 0)}</span>`;
            const checkbox = label.querySelector('input');
            checkbox.addEventListener('change', () => {
                if (checkbox.checked) map.addLayer(layers[definition.layer]);
                else map.removeLayer(layers[definition.layer]);
                updateVisibleCount(document, counts);
            });
            grid.appendChild(label);
        });
        unavailable.forEach((item) => {
            const label = document.createElement('label');
            label.className = 'col-span-2 flex items-start gap-2 opacity-70';
            label.title = item.reason;
            label.innerHTML = `<input type="checkbox" disabled class="mt-0.5 h-4 w-4 rounded border-slate-300"><span class="text-sm text-slate-600 dark:text-slate-400">${escapeHtml(humanize(item.layer))} — unavailable</span>`;
            grid.appendChild(label);
        });
        updateVisibleCount(document, counts);
    }

    function installPresets(document) {
        const presets = {
            all: null,
            osp: new Set(['layer-fdh', 'layer-closures', 'layer-access-points', 'layer-distribution', 'v2-layer-olt', 'v2-layer-topology-endpoints']),
            backbone: new Set(['layer-pop', 'layer-network-devices', 'layer-feeder', 'v2-layer-olt', 'v2-layer-topology-endpoints']),
            edge: new Set(['layer-access-points', 'layer-drop', 'layer-onts', 'v2-layer-service-buildings', 'v2-layer-topology-endpoints']),
            sites: new Set(['layer-pop', 'layer-network-devices', 'v2-layer-olt']),
            clear: new Set()
        };
        const select = document.getElementById('v2-layer-preset');
        if (!select) return;
        select.addEventListener('change', () => {
            const enabled = presets[select.value];
            document.querySelectorAll('.network-map-layers input[type="checkbox"]:not(:disabled)').forEach((checkbox) => {
                const shouldEnable = enabled === null || enabled.has(checkbox.id);
                if (checkbox.checked !== shouldEnable) {
                    checkbox.checked = shouldEnable;
                    checkbox.dispatchEvent(new Event('change', { bubbles: true }));
                }
            });
        });
    }

    function installSearch(context) {
        const { document, L, map, layers, features, topology, renderedFeatureLayers } = context;
        const input = document.getElementById('map-search-input');
        const results = document.getElementById('search-results');
        const clear = document.getElementById('map-search-clear');
        if (!input || !results || !clear) return;
        let timer = null;
        let matches = [];
        let selected = -1;

        const choose = (index) => {
            const match = matches[index];
            if (!match) return;
            if (match.coordinates) {
                layers.selection.clearLayers();
                const marker = L.marker([match.coordinates.latitude, match.coordinates.longitude]).addTo(layers.selection);
                marker.bindPopup(`Coordinates: ${match.coordinates.latitude.toFixed(6)}, ${match.coordinates.longitude.toFixed(6)}`).openPopup();
                map.setView(marker.getLatLng(), 17);
            } else {
                selectFeature({ document, L, map, layers, feature: match.feature, topology, renderedFeatureLayers });
            }
            input.value = match.label;
            results.classList.remove('visible');
        };
        const render = (query) => {
            const coordinates = parseCoordinates(query);
            matches = coordinates
                ? [{ coordinates, label: `${coordinates.latitude.toFixed(6)}, ${coordinates.longitude.toFixed(6)}` }]
                : features.filter((feature) => searchableText(feature).includes(query.toLowerCase())).slice(0, 20).map((feature) => ({ feature, label: feature.properties.name || 'Unnamed' }));
            selected = -1;
            if (!matches.length) {
                results.innerHTML = `<div class="search-no-results">No V2 results found for "${escapeHtml(query)}"</div>`;
            } else {
                results.innerHTML = `<div class="search-result-count">${matches.length} V2 result${matches.length === 1 ? '' : 's'}</div>` + matches.map((match, index) => {
                    const properties = match.feature ? match.feature.properties : { type: 'coordinate' };
                    return `<button type="button" class="search-result-item w-full text-left" data-v2-index="${index}"><div class="search-result-content"><div class="search-result-name">${escapeHtml(match.label)}</div><div class="search-result-meta">${escapeHtml(properties.code || properties.id || '')}</div></div><span class="search-result-type">${escapeHtml(humanize(properties.type))}</span></button>`;
                }).join('');
                results.querySelectorAll('[data-v2-index]').forEach((element) => element.addEventListener('click', () => choose(Number(element.dataset.v2Index))));
            }
            results.classList.add('visible');
        };
        input.addEventListener('input', () => {
            clearTimeout(timer);
            const query = input.value.trim();
            if (query.length < 2 && !parseCoordinates(query)) return;
            timer = setTimeout(() => render(query), 220);
        });
        input.addEventListener('keydown', (event) => {
            if (!results.classList.contains('visible')) return;
            if (event.key === 'ArrowDown') selected = Math.min(selected + 1, matches.length - 1);
            else if (event.key === 'ArrowUp') selected = Math.max(selected - 1, 0);
            else if (event.key === 'Enter') choose(selected >= 0 ? selected : 0);
            else if (event.key === 'Escape') results.classList.remove('visible');
            else return;
            event.preventDefault();
            event.stopImmediatePropagation();
            results.querySelectorAll('[data-v2-index]').forEach((element, index) => element.classList.toggle('selected', index === selected));
        }, true);
        clear.addEventListener('click', () => layers.selection.clearLayers());
    }

    function selectFeature(context) {
        const { document, L, map, layers, feature, topology, renderedFeatureLayers } = context;
        const properties = feature.properties || {};
        const existing = renderedFeatureLayers.get(`${properties.type}:${properties.id}`);
        if (existing) {
            map.setView(existing.getLatLng(), Math.max(map.getZoom(), 16));
            existing.openPopup();
            showDetailPanel(document, feature, topology);
            return;
        }
        const geometry = feature.geometry || {};
        layers.selection.clearLayers();
        if (geometry.type === 'Point' && Array.isArray(geometry.coordinates)) {
            const marker = L.circleMarker([geometry.coordinates[1], geometry.coordinates[0]], { radius: 10, color: '#0ea5e9', fillOpacity: 0.2 }).addTo(layers.selection);
            marker.bindPopup(featurePopup(feature, topology)).openPopup();
            map.setView(marker.getLatLng(), Math.max(map.getZoom(), 16));
        } else if (geometry.type === 'LineString' && Array.isArray(geometry.coordinates)) {
            const line = L.polyline(geometry.coordinates.map((coordinate) => [coordinate[1], coordinate[0]]), { color: '#f59e0b', weight: 7, opacity: 0.65 }).addTo(layers.selection);
            line.bindPopup(featurePopup(feature, topology)).openPopup();
            map.fitBounds(line.getBounds(), { padding: [40, 40] });
        }
        showDetailPanel(document, feature, topology);
    }

    function installDeepLinks(context) {
        const { globalObject, document, L, map, layers, features, topology, renderedFeatureLayers } = context;
        const segmentId = new URLSearchParams(globalObject.location.search || '').get('segment_id');
        if (!segmentId) return;
        const feature = features.find((item) => item.properties && item.properties.type === 'fiber_segment' && item.properties.id === segmentId);
        if (feature) {
            selectFeature({ document, L, map, layers, feature, topology, renderedFeatureLayers });
            return;
        }
        const segment = topology.find((item) => item.id === segmentId);
        if (!segment) return;
        const endpoints = [segment.from_endpoint, segment.to_endpoint].filter((endpoint) => endpoint && Number.isFinite(endpoint.latitude) && Number.isFinite(endpoint.longitude));
        if (endpoints.length) {
            const bounds = L.latLngBounds(endpoints.map((endpoint) => [endpoint.latitude, endpoint.longitude]));
            map.fitBounds(bounds, { padding: [60, 60], maxZoom: 17 });
            L.popup().setLatLng(bounds.getCenter()).setContent(`Segment ${escapeHtml(segment.name)} has ${escapeHtml(humanize(segment.geometry_status))} geometry. No fallback line was drawn.`).openOn(map);
        }
    }

    return api;
});
