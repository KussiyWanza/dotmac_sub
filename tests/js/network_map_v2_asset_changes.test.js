'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const mapV2 = require('../../static/js/admin/network_map_v2.js');

test('only authoritative passive point features are editable from V2', () => {
    assert.equal(mapV2.editableAssetType({ properties: { type: 'fdh_cabinet' } }), 'fdh_cabinet');
    assert.equal(mapV2.editableAssetType({ properties: { type: 'fiber_segment' } }), null);
    assert.equal(mapV2.editableAssetType({ properties: { type: 'olt_device' } }), null);
});

test('proposal details expose exact before and after values', () => {
    const rows = mapV2.proposalDiffRows(
        { name: 'Before', latitude: 9, longitude: 7, is_active: true },
        { name: 'After', latitude: 9, longitude: 7, is_active: true }
    );

    assert.deepEqual(rows, [{ field: 'name', before: 'Before', after: 'After' }]);
});

test('movement previews are explicitly non-topological and use exact proposal coordinates', () => {
    const previews = mapV2.proposalPreviewModels([
        {
            id: 'proposal-1',
            status: 'pending',
            operation: 'move',
            asset_type: 'fdh_cabinet',
            before: { latitude: 9, longitude: 7 },
            after: { latitude: 9.1, longitude: 7.1 }
        }
    ]);

    assert.deepEqual(previews[0].before, { latitude: 9, longitude: 7 });
    assert.deepEqual(previews[0].after, { latitude: 9.1, longitude: 7.1 });
    assert.equal(previews[0].topology, false);
    assert.match(previews[0].label, /not canonical topology/i);
});

test('reviewed proposals are not rendered as pending map previews', () => {
    const previews = mapV2.proposalPreviewModels([
        {
            id: 'proposal-1',
            status: 'rejected',
            operation: 'move',
            before: { latitude: 9, longitude: 7 },
            after: { latitude: 9.1, longitude: 7.1 }
        }
    ]);

    assert.deepEqual(previews, []);
});
