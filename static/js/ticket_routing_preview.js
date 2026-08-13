(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    root.ticketRoutingPreview = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    function normalize(value) {
        return String(value || '').trim();
    }

    function applyRegionChange(state) {
        const currentManager = normalize(state.currentManager);
        const autoFilledManager = normalize(state.autoFilledManager);
        if (!state.enabled || (currentManager && currentManager !== autoFilledManager)) {
            return {
                manager: currentManager,
                autoFilledManager,
            };
        }

        const region = normalize(state.region).toLowerCase();
        const nextManager = normalize((state.routingManagers || {})[region]);
        return {
            manager: nextManager,
            autoFilledManager: nextManager,
        };
    }

    function recordManualSelection(manager, autoFilledManager) {
        const selectedManager = normalize(manager);
        const previousAutoFill = normalize(autoFilledManager);
        return {
            manager: selectedManager,
            autoFilledManager: selectedManager === previousAutoFill ? previousAutoFill : '',
        };
    }

    return {
        applyRegionChange,
        recordManualSelection,
    };
}));
