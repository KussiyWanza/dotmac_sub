const test = require('node:test');
const assert = require('node:assert/strict');

const preview = require('../../static/js/ticket_routing_preview.js');

const routingManagers = {
    'test-region': '00000000-0000-0000-0000-000000000001',
    'other-region': '00000000-0000-0000-0000-000000000002',
};

test('region selection visibly fills an empty manager', () => {
    assert.deepEqual(preview.applyRegionChange({
        enabled: true,
        region: 'test-region',
        currentManager: '',
        autoFilledManager: '',
        routingManagers,
    }), {
        manager: routingManagers['test-region'],
        autoFilledManager: routingManagers['test-region'],
    });
});

test('a manually selected manager is not overwritten', () => {
    const manualManager = '00000000-0000-0000-0000-000000000099';
    assert.deepEqual(preview.applyRegionChange({
        enabled: true,
        region: 'other-region',
        currentManager: manualManager,
        autoFilledManager: '',
        routingManagers,
    }), {
        manager: manualManager,
        autoFilledManager: '',
    });
});

test('an automatic manager updates and clears with the region', () => {
    const first = routingManagers['test-region'];
    assert.deepEqual(preview.applyRegionChange({
        enabled: true,
        region: 'other-region',
        currentManager: first,
        autoFilledManager: first,
        routingManagers,
    }), {
        manager: routingManagers['other-region'],
        autoFilledManager: routingManagers['other-region'],
    });

    assert.deepEqual(preview.applyRegionChange({
        enabled: true,
        region: 'no-rule-region',
        currentManager: first,
        autoFilledManager: first,
        routingManagers,
    }), {
        manager: '',
        autoFilledManager: '',
    });
});

test('edit mode preserves the existing manager', () => {
    const existingManager = '00000000-0000-0000-0000-000000000099';
    assert.deepEqual(preview.applyRegionChange({
        enabled: false,
        region: 'test-region',
        currentManager: existingManager,
        autoFilledManager: '',
        routingManagers,
    }), {
        manager: existingManager,
        autoFilledManager: '',
    });
});
