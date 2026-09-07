(function () {
    if (window.OperationTracker && window.initOperationTracker) {
        return;
    }

/**
 * Operation Tracker - Real-time operation status via WebSocket
 *
 * Provides real-time feedback for long-running operations like:
 * - ONT Authorization
 * - Provisioning
 * - Firmware updates
 * - Config changes
 *
 * Usage:
 *   // Initialize once (usually in base template)
 *   const tracker = new OperationTracker({ token: '...' });
 *   tracker.connect();
 *
 *   // Track an operation (after API returns operation_id)
 *   tracker.track('operation-uuid', {
 *       onRunning: (data) => console.log('Started:', data.message),
 *       onSuccess: (data) => console.log('Done:', data.message),
 *       onError: (data) => console.log('Failed:', data.message),
 *       showToasts: true,  // Auto-show toast notifications
 *       redirectOnSuccess: '/admin/network/onts/...'  // Optional redirect
 *   });
 *
 *   // Or use the global helper after form submission
 *   window.trackOperation(operationId);
 */
class OperationTracker {
    constructor(options = {}) {
        this.ws = null;
        this.token = options.token || null;
        this.connected = false;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 10;
        this.baseReconnectDelay = 1000;
        this.maxReconnectDelay = 30000;
        this.heartbeatInterval = null;
        this.heartbeatTimeout = 25000;

        // Track operations: operationId -> { callbacks, status }
        this.operations = new Map();

        // Default options
        this.defaultShowToasts = options.showToasts !== false;
        this.onStatusChange = options.onStatusChange || (() => {});
    }

    /**
     * Connect to WebSocket server.
     * Uses session_token cookie by default (no token param needed for web sessions).
     * @param {string} token - Optional JWT access token (for API clients)
     */
    connect(token) {
        if (token) {
            this.token = token;
        }

        if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
            return;
        }

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        // Same-origin sessions use their HttpOnly cookie. API tokens travel in
        // the WebSocket subprotocol header, never in a logged request URL.
        const url = `${protocol}//${window.location.host}/ws/inbox`;
        const protocols = this.token ? ['dotmac-auth', this.token] : [];

        try {
            this.ws = protocols.length ? new WebSocket(url, protocols) : new WebSocket(url);
            this._setupEventHandlers();
        } catch (error) {
            console.error('[OperationTracker] Connection error:', error);
            this._scheduleReconnect();
        }
    }

    _setupEventHandlers() {
        this.ws.onopen = () => {
            console.log('[OperationTracker] Connected');
            this.connected = true;
            this.reconnectAttempts = 0;
            this.onStatusChange(true);
            this._startHeartbeat();
            this._resubscribeAll();
        };

        this.ws.onclose = (event) => {
            console.log('[OperationTracker] Disconnected', event.code);
            this.connected = false;
            this.onStatusChange(false);
            this._stopHeartbeat();

            // Don't reconnect on auth failure
            if (event.code === 4001) {
                console.warn('[OperationTracker] Authentication failed');
                return;
            }

            this._scheduleReconnect();
        };

        this.ws.onerror = (error) => {
            console.error('[OperationTracker] Error:', error);
        };

        this.ws.onmessage = (event) => {
            this._handleMessage(event.data);
        };
    }

    _handleMessage(data) {
        try {
            const message = JSON.parse(data);
            const eventType = message.event;
            const eventData = message.data || {};

            if (eventType === 'operation_status') {
                this._handleOperationStatus(eventData);
            } else if (eventType === 'connection_ack') {
                console.log('[OperationTracker] Connection acknowledged');
            }
            // Ignore other events (heartbeat, etc.)
        } catch (error) {
            console.error('[OperationTracker] Failed to parse message:', error);
        }
    }

    _handleOperationStatus(data) {
        const operationId = data.operation_id;
        const operation = this.operations.get(operationId);

        if (!operation) {
            // Not tracking this operation
            return;
        }

        const { callbacks, showToasts } = operation;
        const status = data.status;
        const message = data.message || '';

        console.log(`[OperationTracker] ${operationId}: ${status} - ${message}`);

        // Update operation state
        operation.lastStatus = status;
        operation.lastData = data;

        // Show toast notification if enabled
        if (showToasts) {
            this._showToast(status, message, data);
        }

        // Call appropriate callback
        switch (status) {
            case 'running':
            case 'waiting':
                if (callbacks.onRunning) {
                    callbacks.onRunning(data);
                }
                break;

            case 'succeeded':
                if (callbacks.onSuccess) {
                    callbacks.onSuccess(data);
                }
                // Auto-redirect if configured
                if (callbacks.redirectOnSuccess && data.view_url) {
                    setTimeout(() => {
                        window.location.href = data.view_url;
                    }, 1500);
                }
                // Cleanup after success
                this._cleanupOperation(operationId);
                break;

            case 'warning':
                if (callbacks.onWarning) {
                    callbacks.onWarning(data);
                } else if (callbacks.onSuccess) {
                    callbacks.onSuccess(data);
                }
                this._cleanupOperation(operationId);
                break;

            case 'failed':
            case 'error':
                if (callbacks.onError) {
                    callbacks.onError(data);
                }
                this._cleanupOperation(operationId);
                break;
        }

        // Emit DOM event for Alpine.js integration
        window.dispatchEvent(new CustomEvent('operation-status', {
            detail: { operationId, ...data }
        }));
    }

    _showToast(status, message, data) {
        const toastTypes = {
            'running': 'info',
            'waiting': 'info',
            'succeeded': 'success',
            'warning': 'warning',
            'failed': 'error',
            'error': 'error'
        };

        const titles = {
            'running': 'Processing',
            'waiting': 'Waiting',
            'succeeded': 'Success',
            'warning': 'Warning',
            'failed': 'Failed',
            'error': 'Error'
        };

        const type = toastTypes[status] || 'info';
        const title = titles[status] || 'Status Update';

        // Duration: keep success/error longer, running shorter
        const duration = (status === 'running' || status === 'waiting') ? 3000 : 5000;

        window.dispatchEvent(new CustomEvent('toast', {
            detail: {
                type,
                title,
                message: message || `Operation ${status}`,
                duration
            }
        }));
    }

    _cleanupOperation(operationId) {
        const operation = this.operations.get(operationId);
        if (operation) {
            // Unsubscribe from WebSocket channel
            this._send({ type: 'unsubscribe', topic: `operation:${operationId}` });
            this.operations.delete(operationId);
        }
    }

    _resubscribeAll() {
        for (const operationId of this.operations.keys()) {
            this._send({ type: 'subscribe', topic: `operation:${operationId}` });
        }
    }

    _send(message) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(message));
        }
    }

    _scheduleReconnect() {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            console.warn('[OperationTracker] Max reconnect attempts reached');
            return;
        }

        const delay = Math.min(
            this.baseReconnectDelay * Math.pow(2, this.reconnectAttempts),
            this.maxReconnectDelay
        );
        this.reconnectAttempts++;

        console.log(`[OperationTracker] Reconnecting in ${delay}ms`);
        setTimeout(() => this.connect(), delay);
    }

    _startHeartbeat() {
        this._stopHeartbeat();
        this.heartbeatInterval = setInterval(() => {
            if (this.connected) {
                this._send({ type: 'ping' });
            }
        }, this.heartbeatTimeout);
    }

    _stopHeartbeat() {
        if (this.heartbeatInterval) {
            clearInterval(this.heartbeatInterval);
            this.heartbeatInterval = null;
        }
    }

    /**
     * Track an operation for real-time status updates.
     * @param {string} operationId - The operation ID from the API response
     * @param {object} options - Tracking options
     * @param {function} options.onRunning - Called when operation starts/is running
     * @param {function} options.onSuccess - Called on successful completion
     * @param {function} options.onWarning - Called on warning (partial success)
     * @param {function} options.onError - Called on failure
     * @param {boolean} options.showToasts - Show toast notifications (default: true)
     * @param {boolean} options.redirectOnSuccess - Redirect to view_url on success
     */
    track(operationId, options = {}) {
        if (!operationId) {
            console.warn('[OperationTracker] No operation ID provided');
            return;
        }

        // Store operation with callbacks
        this.operations.set(operationId, {
            callbacks: {
                onRunning: options.onRunning,
                onSuccess: options.onSuccess,
                onWarning: options.onWarning,
                onError: options.onError,
                redirectOnSuccess: options.redirectOnSuccess
            },
            showToasts: options.showToasts !== false && this.defaultShowToasts,
            lastStatus: 'pending',
            lastData: null,
            startedAt: Date.now()
        });

        // Subscribe to operation channel
        this._send({ type: 'subscribe', topic: `operation:${operationId}` });

        console.log(`[OperationTracker] Tracking operation: ${operationId}`);

        // Show initial toast
        if (options.showToasts !== false && this.defaultShowToasts) {
            window.dispatchEvent(new CustomEvent('toast', {
                detail: {
                    type: 'info',
                    title: 'Operation Queued',
                    message: 'Your request is being processed...',
                    duration: 3000
                }
            }));
        }
    }

    /**
     * Stop tracking an operation.
     * @param {string} operationId - The operation ID to stop tracking
     */
    untrack(operationId) {
        this._cleanupOperation(operationId);
    }

    /**
     * Check if currently connected.
     * @returns {boolean}
     */
    isConnected() {
        return this.connected && this.ws && this.ws.readyState === WebSocket.OPEN;
    }

    /**
     * Get status of a tracked operation.
     * @param {string} operationId
     * @returns {object|null}
     */
    getOperationStatus(operationId) {
        const op = this.operations.get(operationId);
        return op ? { status: op.lastStatus, data: op.lastData } : null;
    }

    /**
     * Disconnect from WebSocket.
     */
    disconnect() {
        this._stopHeartbeat();
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
        this.connected = false;
        this.operations.clear();
    }
}

// ============================================================================
// Alpine.js Component Helper
// ============================================================================

/**
 * Alpine.js data component for operation tracking.
 *
 * Usage in templates:
 *   <div x-data="operationTrackerComponent()" x-init="init()">
 *       <button @click="authorize(oltId, serial)" :disabled="isProcessing">
 *           <span x-show="!isProcessing">Authorize</span>
 *           <span x-show="isProcessing" x-text="statusMessage">Processing...</span>
 *       </button>
 *   </div>
 */
function operationTrackerComponent() {
    return {
        isProcessing: false,
        statusMessage: '',
        currentOperationId: null,
        lastError: null,

        init() {
            // Listen for operation status events
            window.addEventListener('operation-status', (e) => {
                if (e.detail.operationId === this.currentOperationId) {
                    this.handleStatus(e.detail);
                }
            });
        },

        handleStatus(data) {
            this.statusMessage = data.message || data.status;

            if (data.status === 'succeeded' || data.status === 'warning') {
                this.isProcessing = false;
                this.currentOperationId = null;
                // Optionally refresh the page section
                if (data.view_url) {
                    this.$dispatch('operation-complete', data);
                }
            } else if (data.status === 'failed' || data.status === 'error') {
                this.isProcessing = false;
                this.lastError = data.message || 'Operation failed';
                this.currentOperationId = null;
            }
        },

        /**
         * Start tracking an operation.
         * @param {string} operationId
         */
        trackOperation(operationId) {
            this.isProcessing = true;
            this.statusMessage = 'Processing...';
            this.currentOperationId = operationId;
            this.lastError = null;

            if (window.operationTracker) {
                window.operationTracker.track(operationId, {
                    showToasts: true
                });
            }
        },

        /**
         * Submit a form and track the resulting operation.
         * Expects the API to return { operation_id: '...' }
         */
        async submitAndTrack(url, data, method = 'POST') {
            this.isProcessing = true;
            this.statusMessage = 'Submitting...';
            this.lastError = null;

            try {
                const response = await fetch(url, {
                    method,
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json'
                    },
                    body: JSON.stringify(data)
                });

                const result = await response.json();

                if (result.operation_id) {
                    this.trackOperation(result.operation_id);
                } else if (result.error) {
                    this.isProcessing = false;
                    this.lastError = result.error;
                } else {
                    // Immediate success (no async operation)
                    this.isProcessing = false;
                    this.$dispatch('operation-complete', result);
                }
            } catch (error) {
                this.isProcessing = false;
                this.lastError = error.message || 'Request failed';
            }
        }
    };
}

// ============================================================================
// Global Instance & Helpers
// ============================================================================

// Create global instance (will be initialized with token from page)
window.OperationTracker = OperationTracker;
window.operationTracker = null;

/**
 * Initialize the global operation tracker.
 * Call this once from the base template. Uses session cookie for auth.
 *
 * @param {string} token - Optional JWT access token (uses cookie if not provided)
 */
window.initOperationTracker = function(token) {
    if (window.operationTracker && window.operationTracker instanceof OperationTracker) {
        // Already initialized, just reconnect if needed
        if (!window.operationTracker.isConnected()) {
            window.operationTracker.connect(token);
        }
        return window.operationTracker;
    }

    window.operationTracker = new OperationTracker({ token });
    window.operationTracker.connect();
    return window.operationTracker;
};

/**
 * Quick helper to track an operation.
 * @param {string} operationId
 * @param {object} options
 */
window.trackOperation = function(operationId, options = {}) {
    if (!window.operationTracker) {
        console.warn('[OperationTracker] Not initialized. Call initOperationTracker(token) first.');
        return;
    }
    window.operationTracker.track(operationId, options);
};

window.operationTrackerComponent = operationTrackerComponent;

// For module systems
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { OperationTracker, operationTrackerComponent };
}
})();
