function projectInfrastructurePicker(initial) {
    return {
        type: initial.type || '', id: initial.id || '',
        search: initial.selected ? initial.selected.label : '',
        results: [], open: false, loading: false, searched: false, error: '', active: -1, sequence: 0,
        edited() {
            this.id = ''; this.sequence++; this.results = []; this.active = -1;
            this.loading = !!this.type && this.search.trim().length >= 2;
            this.searched = false; this.error = '';
        },
        changeType() { this.edited(); this.search = ''; this.open = false; this.loading = false; },
        clear() { this.type = ''; this.changeType(); },
        choose(item) {
            if (!item) return;
            this.sequence++; this.id = item.id; this.search = item.label;
            this.open = false; this.loading = false; this.error = ''; this.active = -1;
        },
        move(delta) {
            this.open = true;
            this.active = Math.max(-1, Math.min(this.results.length - 1, this.active + delta));
        },
        async lookup() {
            if (!this.type || this.id) return;
            const seq = ++this.sequence;
            this.open = true; this.error = ''; this.results = []; this.active = -1;
            if (this.search.trim().length < 2) { this.loading = false; return; }
            this.loading = true;
            try {
                const params = new URLSearchParams({infrastructure_type: this.type, q: this.search.trim(), limit: '20'});
                const response = await fetch(`/admin/projects/infrastructure-options?${params}`, {headers: {'Accept': 'application/json'}});
                if (!response.ok) throw new Error('lookup failed');
                const data = await response.json();
                if (seq === this.sequence) { this.results = data.results; this.searched = true; }
            } catch (_) {
                if (seq === this.sequence) this.error = 'Infrastructure search is unavailable. Try again.';
            } finally {
                if (seq === this.sequence) this.loading = false;
            }
        }
    };
}
