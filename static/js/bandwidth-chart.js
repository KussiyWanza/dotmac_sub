/**
 * Bandwidth Chart Component
 *
 * Reusable Alpine.js component for displaying real-time bandwidth charts
 * with SSE updates and time range selection.
 */

// Format bytes per second to human-readable format
function formatBps(bps) {
    if (bps === 0) return '0 bps';
    const units = ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps'];
    const i = Math.floor(Math.log(bps) / Math.log(1000));
    const value = bps / Math.pow(1000, i);
    return value.toFixed(value < 10 ? 2 : 1) + ' ' + units[i];
}

// Format bytes to human-readable format
function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    const value = bytes / Math.pow(1024, i);
    return value.toFixed(value < 10 ? 2 : 1) + ' ' + units[i];
}

function parseSsePayload(raw) {
    if (raw == null) return null;
    if (typeof raw === 'object') return raw;
    try {
        return JSON.parse(raw);
    } catch (_e) {
        // Fallback for legacy payloads serialized with single quotes
        try {
            return JSON.parse(String(raw).replace(/'/g, '"'));
        } catch (_e2) {
            return null;
        }
    }
}

// Bandwidth chart Alpine.js component
function bandwidthChart(config = {}) {
    return {
        // Configuration
        subscriptionId: config.subscriptionId || null,
        apiBasePath: config.apiBasePath || '/api/v1/bandwidth',
        useMyEndpoints: config.useMyEndpoints || false, // Use /my/ endpoints for customer portal
        enableLive: config.enableLive !== false,
        directLiveEndpoint: config.directLiveEndpoint || null,
        directLivePollMs: config.directLivePollMs || 5000,
        // Normal live views use the application-owned SSE projection. The
        // direct endpoint remains an explicit diagnostic-only fallback.
        liveStream: config.liveStream || false,

        // Whether live updates are currently running. Off by default — live is
        // on-demand (the user starts it via toggleLive), not on page load.
        liveActive: false,

        // State
        chart: null,
        eventSource: null,
        reconnectTimer: null,
        statsPollTimer: null,
        directLivePollTimer: null,
        directLiveRequestInFlight: false,
        directLiveAbortController: null,
        directLiveConsecutiveErrors: 0,
        isDestroyed: false,
        loading: true,
        error: null,

        // Data
        seriesData: [],
        currentDownload: 0,
        currentUpload: 0,
        peakDownload: 0,
        peakUpload: 0,
        totalDownload: 0,
        totalUpload: 0,
        liveStatus: (config.directLiveEndpoint || config.liveStream) ? 'waiting' : 'history',
        liveSource: '',
        liveInterface: '',
        liveUpdatedAt: null,

        // Time range. Live stream only flows on the 1h view, so default there
        // when live so customers see real-time speed without first switching.
        timeRange: config.liveStream ? '1h' : '24h',
        timeRanges: [
            { value: '1h', label: '1h' },
            { value: '24h', label: '24h' },
            { value: '7d', label: '7d' },
            { value: '30d', label: '30d' },
        ],

        // Computed
        get currentDownloadFormatted() { return formatBps(this.currentDownload); },
        get currentUploadFormatted() { return formatBps(this.currentUpload); },
        get peakDownloadFormatted() { return formatBps(this.peakDownload); },
        get peakUploadFormatted() { return formatBps(this.peakUpload); },
        get totalDownloadFormatted() { return formatBytes(this.totalDownload); },
        get totalUploadFormatted() { return formatBytes(this.totalUpload); },
        get liveStatusLabel() {
            if (this.directLiveEndpoint) {
                if (this.liveStatus === 'live') return 'Live from MikroTik';
                if (this.liveStatus === 'offline') return 'PPP session offline';
                if (this.liveStatus === 'error') return 'Live read unavailable';
                return 'Waiting for MikroTik';
            }
            if (this.liveStream) {
                if (this.timeRange !== '1h') return 'Live paused (history view)';
                if (this.liveStatus === 'live') return 'Live';
                // No fresh sample (no queue mapping) or mid-reconnect: don't
                // claim "Live" or flash an error on routine SSE reconnects.
                return 'Waiting for data';
            }
            return 'Speed history';
        },
        get liveUpdatedAtFormatted() {
            if (!this.liveUpdatedAt) return '';
            const dt = new Date(this.liveUpdatedAt);
            if (Number.isNaN(dt.getTime())) return '';
            return `Updated ${dt.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
        },

        formatTimeLabel(timestamp) {
            const dt = new Date(timestamp);
            if (this.timeRange === '7d' || this.timeRange === '30d') {
                return dt.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
            }
            return dt.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
        },

        // Initialize
        async init() {
            this.isDestroyed = false;
            await this.loadData();
            this.initChart();
            // Live updates are on-demand: the user starts them with the Live
            // button (toggleLive). Nothing streams or polls on page load.
        },

        // Start/stop live updates on demand (SSE for the customer stream, or the
        // direct poll for the admin endpoint). Off by default so neither runs
        // until the user asks.
        toggleLive() {
            if (this.liveActive) {
                this.liveActive = false;
                this.stopDirectLivePolling();
                if (this.eventSource) {
                    this.eventSource.close();
                    this.eventSource = null;
                }
                if (this.reconnectTimer) {
                    clearTimeout(this.reconnectTimer);
                    this.reconnectTimer = null;
                }
                this.liveStatus = 'paused';
            } else {
                this.liveActive = true;
                this.liveStatus = 'waiting';
                this.connectSSE();
                this.startDirectLivePolling();
            }
        },

        // Cleanup
        destroy() {
            this.isDestroyed = true;
            if (this.reconnectTimer) {
                clearTimeout(this.reconnectTimer);
                this.reconnectTimer = null;
            }
            this.stopCurrentStatsPolling();
            this.stopDirectLivePolling();
            if (this.eventSource) {
                this.eventSource.close();
                this.eventSource = null;
            }
            if (this.chart) {
                DotmacCharts.unregisterChart(this.getChartId());
                this.chart = null;
            }
        },

        getChartId() {
            return `bandwidth-chart-${this.subscriptionId || 'my'}`;
        },

        getSeriesEndpoint() {
            if (this.useMyEndpoints) {
                return `${this.apiBasePath}/my/series`;
            }
            return `${this.apiBasePath}/series/${this.subscriptionId}`;
        },

        getStatsEndpoint() {
            if (this.useMyEndpoints) {
                return `${this.apiBasePath}/my/stats`;
            }
            return `${this.apiBasePath}/stats/${this.subscriptionId}`;
        },

        getLiveEndpoint() {
            if (this.useMyEndpoints) {
                return `${this.apiBasePath}/my/live`;
            }
            return `${this.apiBasePath}/live/${this.subscriptionId}`;
        },

        async loadCurrentStats() {
            try {
                const statsUrl = new URL(this.getStatsEndpoint(), window.location.origin);
                statsUrl.searchParams.set('period', this.timeRange);

                const statsResponse = await fetch(statsUrl);
                if (!statsResponse.ok) {
                    return;
                }
                const stats = await statsResponse.json();
                if (!this.directLiveEndpoint) {
                    this.currentDownload = stats.download_bps || 0;
                    this.currentUpload = stats.upload_bps || 0;
                }
                this.peakDownload = stats.peak_download_bps || 0;
                this.peakUpload = stats.peak_upload_bps || 0;
                this.totalDownload = stats.total_download_bytes || 0;
                this.totalUpload = stats.total_upload_bytes || 0;
            } catch (e) {
                console.warn('Bandwidth current stats refresh skipped:', e);
            }
        },

        startCurrentStatsPolling() {
            this.stopCurrentStatsPolling();
            if (this.isDestroyed || this.timeRange !== '1h') {
                return;
            }
            this.statsPollTimer = setInterval(() => {
                if (this.isDestroyed || this.timeRange !== '1h') {
                    this.stopCurrentStatsPolling();
                    return;
                }
                this.loadCurrentStats();
            }, 30000);
        },

        stopCurrentStatsPolling() {
            if (this.statsPollTimer) {
                clearInterval(this.statsPollTimer);
                this.statsPollTimer = null;
            }
        },

        async loadDirectLive() {
            if (!this.directLiveEndpoint || this.isDestroyed || document.hidden) return true;
            if (this.directLiveRequestInFlight) return false;

            this.directLiveRequestInFlight = true;
            const controller = new AbortController();
            this.directLiveAbortController = controller;

            try {
                const response = await fetch(
                    new URL(this.directLiveEndpoint, window.location.origin),
                    { signal: controller.signal },
                );
                if (!response.ok) {
                    throw new Error('Failed to load live MikroTik bandwidth');
                }
                const data = await response.json();
                const downloadBps = Number(data.download_bps || 0);
                const uploadBps = Number(data.upload_bps || 0);

                this.currentDownload = Number.isFinite(downloadBps) ? downloadBps : 0;
                this.currentUpload = Number.isFinite(uploadBps) ? uploadBps : 0;
                this.liveStatus = data.online === false ? 'offline' : 'live';
                this.liveSource = data.source || '';
                this.liveInterface = data.interface || '';
                this.liveUpdatedAt = data.timestamp || new Date().toISOString();
                this.directLiveConsecutiveErrors = 0;

                if (this.currentDownload > this.peakDownload) this.peakDownload = this.currentDownload;
                if (this.currentUpload > this.peakUpload) this.peakUpload = this.currentUpload;
                return true;
            } catch (e) {
                if (e && e.name === 'AbortError') return false;
                console.warn('MikroTik live bandwidth refresh skipped:', e);
                this.liveStatus = 'error';
                this.directLiveConsecutiveErrors += 1;
                return false;
            } finally {
                if (this.directLiveAbortController === controller) {
                    this.directLiveAbortController = null;
                }
                this.directLiveRequestInFlight = false;
            }
        },

        startDirectLivePolling() {
            this.stopDirectLivePolling();
            if (this.isDestroyed || !this.directLiveEndpoint || !this.liveActive) {
                return;
            }
            const pollAfterCompletion = async () => {
                if (this.isDestroyed || !this.directLiveEndpoint || !this.liveActive) return;
                const succeeded = await this.loadDirectLive();
                if (this.isDestroyed || !this.directLiveEndpoint || !this.liveActive) return;
                const backoffMultiplier = succeeded
                    ? 1
                    : Math.min(12, Math.pow(2, this.directLiveConsecutiveErrors));
                this.directLivePollTimer = setTimeout(
                    pollAfterCompletion,
                    Math.min(60000, this.directLivePollMs * backoffMultiplier),
                );
            };
            this.directLivePollTimer = setTimeout(pollAfterCompletion, 0);
        },

        stopDirectLivePolling() {
            if (this.directLivePollTimer) {
                clearTimeout(this.directLivePollTimer);
                this.directLivePollTimer = null;
            }
            if (this.directLiveAbortController) {
                this.directLiveAbortController.abort();
                this.directLiveAbortController = null;
            }
            this.directLiveRequestInFlight = false;
        },

        // Load historical data
        async loadData() {
            this.loading = true;
            this.error = null;

            try {
                // Calculate time range
                const end = new Date();
                let start;
                switch (this.timeRange) {
                    case '1h': start = new Date(end - 60 * 60 * 1000); break;
                    case '24h': start = new Date(end - 24 * 60 * 60 * 1000); break;
                    case '7d': start = new Date(end - 7 * 24 * 60 * 60 * 1000); break;
                    case '30d': start = new Date(end - 30 * 24 * 60 * 60 * 1000); break;
                    default: start = new Date(end - 24 * 60 * 60 * 1000);
                }

                // Fetch series data
                const seriesUrl = new URL(this.getSeriesEndpoint(), window.location.origin);
                seriesUrl.searchParams.set('start_at', start.toISOString());
                seriesUrl.searchParams.set('end_at', end.toISOString());

                const seriesResponse = await fetch(seriesUrl);
                if (!seriesResponse.ok) throw new Error('Failed to load bandwidth data');
                const seriesResult = await seriesResponse.json();
                this.seriesData = seriesResult.data || [];

                // Fetch stats
                const statsUrl = new URL(this.getStatsEndpoint(), window.location.origin);
                statsUrl.searchParams.set('period', this.timeRange);

                const statsResponse = await fetch(statsUrl);
                if (statsResponse.ok) {
                    const stats = await statsResponse.json();
                    if (!this.directLiveEndpoint) {
                        this.currentDownload = stats.download_bps || 0;
                        this.currentUpload = stats.upload_bps || 0;
                    }
                    this.peakDownload = stats.peak_download_bps || 0;
                    this.peakUpload = stats.peak_upload_bps || 0;
                    this.totalDownload = stats.total_download_bytes || 0;
                    this.totalUpload = stats.total_upload_bytes || 0;
                }

                this.loading = false;
            } catch (e) {
                console.error('Error loading bandwidth data:', e);
                this.error = e.message;
                this.loading = false;
            }
        },

        // Initialize Chart.js chart
        initChart() {
            const canvas = this.$refs.canvas;
            if (!canvas) return;

            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const existing = window.Chart && window.Chart.getChart ? window.Chart.getChart(canvas) : null;
            if (existing && !existing._destroyed) {
                try {
                    existing.destroy();
                } catch (_e) {
                    // Ignore stale instance errors
                }
            }

            // Prepare data
            const labels = this.seriesData.map(d => this.formatTimeLabel(d.timestamp));
            // API provides explicit subscriber-perspective download/upload.
            const downloadData = this.seriesData.map(d => (d.download_bps || 0) / 1000000); // Mbps
            const uploadData = this.seriesData.map(d => (d.upload_bps || 0) / 1000000);
            const sparseSeries = labels.length <= 1;

            const data = {
                labels: labels,
                datasets: [
                    {
                        label: 'Download',
                        data: downloadData,
                        color: DotmacCharts.colors.accent[500],
                        fillColor: DotmacCharts.colors.accent[500] + '40',
                        fill: true,
                        pointRadius: sparseSeries ? 3 : 0,
                    },
                    {
                        label: 'Upload',
                        data: uploadData,
                        color: DotmacCharts.colors.primary[500],
                        fillColor: DotmacCharts.colors.primary[500] + '40',
                        fill: true,
                        pointRadius: sparseSeries ? 3 : 0,
                    },
                ],
            };

            const options = {
                scales: {
                    x: {
                        grid: {
                            display: false,
                        },
                    },
                    y: {
                        beginAtZero: true,
                        ticks: {
                            callback: (value) => value + ' Mbps',
                        },
                    },
                },
                plugins: {
                    legend: {
                        display: true,
                        position: 'top',
                    },
                    tooltip: {
                        callbacks: {
                            label: (context) => {
                                return context.dataset.label + ': ' + formatBps(context.raw * 1000000);
                            },
                        },
                    },
                },
                interaction: {
                    intersect: false,
                    mode: 'index',
                },
            };

            // Destroy existing chart if any
            if (this.chart) {
                this.chart.destroy();
                this.chart = null;
            }

            this.chart = DotmacCharts.createAreaChart(ctx, data, options);
            DotmacCharts.registerChart(this.getChartId(), this.chart);
        },

        // Connect to SSE for real-time updates
        connectSSE() {
            if (this.isDestroyed) return;
            if (!this.enableLive) return;
            // On-demand: never (re)connect while live is paused.
            if (!this.liveActive) return;
            if (this.eventSource) {
                this.eventSource.close();
                this.eventSource = null;
            }
            this.stopCurrentStatsPolling();

            // Keep live streaming only for 1h to avoid excessive chart updates.
            if (this.timeRange !== '1h') {
                return;
            }
            this.startCurrentStatsPolling();

            try {
                const source = new EventSource(this.getLiveEndpoint());
                this.eventSource = source;

                source.addEventListener('bandwidth', (event) => {
                    if (this.isDestroyed || this.eventSource !== source) return;
                    const data = parseSsePayload(event.data);
                    if (!data) return;
                    if (!this.directLiveEndpoint) {
                        this.currentDownload = data.download_bps || 0;
                        this.currentUpload = data.upload_bps || 0;
                    }
                    if (this.liveStream) {
                        // Only claim "Live" when the backend actually has a
                        // sample for this sub (has_sample). Subscribers with no
                        // queue mapping get a steady stream of default-zero
                        // events — show "Waiting for data", not a fake live 0.
                        // Absent field (other producers) defaults to fresh.
                        const fresh = data.has_sample !== false;
                        this.liveStatus = fresh ? 'live' : 'waiting';
                        if (fresh) this.liveUpdatedAt = data.timestamp || new Date().toISOString();
                    }

                    // Update peak if necessary
                    if (this.currentDownload > this.peakDownload) this.peakDownload = this.currentDownload;
                    if (this.currentUpload > this.peakUpload) this.peakUpload = this.currentUpload;

                    // Add new point to chart
                    if (this.chart && this.chart.data.labels) {
                        if (!this.chart.canvas || !this.chart.ctx) {
                            return;
                        }
                        if (!this.chart.data.datasets || this.chart.data.datasets.length < 2) {
                            return;
                        }
                        const now = new Date();
                        this.chart.data.labels.push(this.formatTimeLabel(now));
                        this.chart.data.datasets[0].data.push(this.currentDownload / 1000000);
                        this.chart.data.datasets[1].data.push(this.currentUpload / 1000000);

                        // Keep chart lightweight to avoid client-side rendering loops.
                        const maxPoints = 900;
                        while (this.chart.data.labels.length > maxPoints) {
                            this.chart.data.labels.shift();
                            this.chart.data.datasets[0].data.shift();
                            this.chart.data.datasets[1].data.shift();
                        }

                        try {
                            this.chart.update('none'); // Update without animation
                        } catch (e) {
                            console.warn('Bandwidth chart update skipped:', e);
                        }
                    }
                });

                source.addEventListener('error', (event) => {
                    console.error('SSE error:', event);
                    if (this.liveStream) this.liveStatus = 'error';
                    if (this.eventSource === source) {
                        source.close();
                        this.eventSource = null;
                    }
                    if (this.reconnectTimer) {
                        clearTimeout(this.reconnectTimer);
                    }
                    this.reconnectTimer = setTimeout(() => {
                        this.connectSSE();
                    }, 5000);
                });

            } catch (e) {
                console.error('Failed to connect SSE:', e);
            }
        },

        // Handle time range change
        async setTimeRange(range) {
            this.timeRange = range;
            await this.loadData();
            this.initChart();

            // Reconnect SSE if needed
            this.connectSSE();
        },
    };
}

// Mini bandwidth widget for dashboard
function bandwidthWidget(config = {}) {
    return {
        subscriptionId: config.subscriptionId || null,
        apiBasePath: config.apiBasePath || '/api/v1/bandwidth',
        useMyEndpoints: config.useMyEndpoints || false,

        currentDownload: 0,
        currentUpload: 0,
        loading: true,
        eventSource: null,
        reconnectTimer: null,
        isDestroyed: false,
        consecutiveErrors: 0,
        maxConsecutiveErrors: 3,

        get currentDownloadFormatted() { return formatBps(this.currentDownload); },
        get currentUploadFormatted() { return formatBps(this.currentUpload); },

        async init() {
            this.isDestroyed = false;
            // If the initial stats fetch 404s the customer has no active
            // subscription; don't open SSE in that case.
            const hasData = await this.loadCurrent();
            if (hasData) {
                this.connectSSE();
            }
        },

        destroy() {
            this.isDestroyed = true;
            if (this.reconnectTimer) {
                clearTimeout(this.reconnectTimer);
                this.reconnectTimer = null;
            }
            if (this.eventSource) {
                this.eventSource.close();
                this.eventSource = null;
            }
        },

        getLiveEndpoint() {
            if (this.useMyEndpoints) {
                return `${this.apiBasePath}/my/live`;
            }
            return `${this.apiBasePath}/live/${this.subscriptionId}`;
        },

        async loadCurrent() {
            try {
                const endpoint = this.useMyEndpoints
                    ? `${this.apiBasePath}/my/stats?period=1h`
                    : `${this.apiBasePath}/stats/${this.subscriptionId}?period=1h`;

                const response = await fetch(endpoint);
                if (response.ok) {
                    const stats = await response.json();
                    this.currentDownload = stats.download_bps || 0;
                    this.currentUpload = stats.upload_bps || 0;
                    this.loading = false;
                    return true;
                }
                // 404 / 401 / etc. — likely no active subscription.
                this.loading = false;
                return false;
            } catch (e) {
                console.error('Error loading bandwidth:', e);
                this.loading = false;
                return false;
            }
        },

        connectSSE() {
            if (this.isDestroyed) return;
            try {
                if (this.eventSource) {
                    this.eventSource.close();
                    this.eventSource = null;
                }
                const source = new EventSource(this.getLiveEndpoint());
                this.eventSource = source;

                source.addEventListener('bandwidth', (event) => {
                    if (this.isDestroyed || this.eventSource !== source) return;
                    const data = parseSsePayload(event.data);
                    if (!data) return;
                    this.currentDownload = data.download_bps || 0;
                    this.currentUpload = data.upload_bps || 0;
                    this.consecutiveErrors = 0;
                });

                source.addEventListener('error', () => {
                    if (this.eventSource === source) {
                        source.close();
                        this.eventSource = null;
                    }
                    this.consecutiveErrors++;
                    if (this.consecutiveErrors >= this.maxConsecutiveErrors) {
                        // Server has been rejecting the stream consistently —
                        // most likely there is no active subscription. Stop
                        // hammering the endpoint and clean up timers.
                        if (this.reconnectTimer) {
                            clearTimeout(this.reconnectTimer);
                            this.reconnectTimer = null;
                        }
                        return;
                    }
                    if (this.reconnectTimer) {
                        clearTimeout(this.reconnectTimer);
                    }
                    // Exponential backoff: 5s, 10s, 20s, then stop.
                    const delay = 5000 * Math.pow(2, this.consecutiveErrors - 1);
                    this.reconnectTimer = setTimeout(() => this.connectSSE(), delay);
                });
            } catch (e) {
                console.error('SSE connection failed:', e);
            }
        },
    };
}

// Export
window.BandwidthChart = {
    formatBps,
    formatBytes,
    bandwidthChart,
    bandwidthWidget,
};
