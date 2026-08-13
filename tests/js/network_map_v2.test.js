'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const mapV2 = require('../../static/js/admin/network_map_v2.js');

function point(id, type, latitude, longitude, name) {
    return {
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [longitude, latitude] },
        properties: { id, type, name }
    };
}

test('coordinate search accepts valid coordinates and rejects invalid coordinates', () => {
    assert.deepEqual(mapV2.parseCoordinates('9.0765, 7.3986'), {
        latitude: 9.0765,
        longitude: 7.3986
    });
    assert.equal(mapV2.parseCoordinates('91, 7'), null);
    assert.equal(mapV2.parseCoordinates('not coordinates'), null);
});

test('V2 captures the Leaflet instance without changing the map factory result', () => {
    const browser = {};
    mapV2.captureLeafletMap(browser);
    browser.L = {};
    const expected = { name: 'leaflet-map' };
    browser.L.map = () => expected;

    assert.equal(browser.L.map('network-map'), expected);
    assert.equal(browser.__networkMapV2Map, expected);
});

test('combined search fields include canonical id, code, type, and status', () => {
    const feature = point('canonical-123', 'fdh_cabinet', 9, 7, 'Central FDH');
    feature.properties.code = 'FDH-001';
    feature.properties.status = 'active';
    const text = mapV2.searchableText(feature);

    assert.match(text, /canonical-123/);
    assert.match(text, /fdh-001/);
    assert.match(text, /fdh_cabinet/);
    assert.match(text, /active/);
});

test('nearest FDH selects by distance without claiming a topology connection', () => {
    const features = [
        point('far', 'fdh_cabinet', 9.5, 7.5, 'Far FDH'),
        point('near', 'fdh_cabinet', 9.01, 7.01, 'Near FDH'),
        point('olt', 'olt_device', 9.001, 7.001, 'Ignored OLT')
    ];
    const nearest = mapV2.nearestFeature(
        { latitude: 9, longitude: 7 },
        features,
        'fdh_cabinet'
    );

    assert.equal(nearest.feature.properties.id, 'near');
    assert.ok(nearest.distanceMeters > 0);
});

test('nearby endpoints remain separate unless they share a canonical id', () => {
    const models = mapV2.topologyMarkerModels([
        {
            id: 'segment-a',
            name: 'A',
            topology_status: 'disconnected',
            geometry_status: 'stored_valid',
            from_endpoint: { id: 'endpoint-a', latitude: 9, longitude: 7 },
            to_endpoint: { id: 'endpoint-b', latitude: 9.1, longitude: 7.1 }
        },
        {
            id: 'segment-b',
            name: 'B',
            topology_status: 'disconnected',
            geometry_status: 'stored_valid',
            from_endpoint: { id: 'endpoint-near-a', latitude: 9.000001, longitude: 7.000001 },
            to_endpoint: { id: 'endpoint-c', latitude: 9.2, longitude: 7.2 }
        }
    ]);

    assert.equal(models.length, 4);
    assert.notEqual(models[0].endpoint.id, models[2].endpoint.id);
});

test('layer mapping distinguishes fibre classes and CRM parity overlays', () => {
    const feeder = {
        type: 'Feature',
        geometry: { type: 'LineString', coordinates: [[7, 9], [7.1, 9.1]] },
        properties: { id: 'feeder', type: 'fiber_segment', segment_type: 'feeder' }
    };

    assert.equal(mapV2.layerForFeature(feeder), 'feeder');
    assert.equal(mapV2.layerForFeature(point('olt', 'olt_device', 9, 7, 'OLT')), 'olt');
    assert.equal(
        mapV2.layerForFeature(point('building', 'service_building', 9, 7, 'Building')),
        'service_buildings'
    );
});
