/**
 * Invoice Calculator Component
 * ============================
 * Alpine.js component for real-time invoice total calculations.
 *
 * Features:
 * - Real-time subtotal, tax, and total calculations
 * - Line item management (add/remove)
 * - Currency formatting
 * - Tax rate selection and application
 * - Sticky totals summary panel
 */

function createInvoiceCalculator(config = {}) {
    return {
        // Invoice metadata
        accountId: config.accountId || '',
        invoiceNumber: config.invoiceNumber || '',
        status: config.status || 'draft',
        currency: config.currency || 'NGN',
        issuedAt: config.issuedAt || '',
        dueAt: config.dueAt || '',
        memo: config.memo || '',
        discountType: config.discountType || '',
        discountValue: config.discountValue || '',
        discountReason: config.discountReason || '',
        discountLocked: Boolean(config.discountLocked),

        // Line items
        lineItems: [],
        nextItemId: 1,

        // Tax rates from backend
        taxRates: config.taxRates || [],

        // Form state
        submitting: false,

        // Settings
        currencySymbol: config.currencySymbol || '\u20A6',
        decimalPlaces: 2,
        minItems: 1,
        maxItems: 50,

        // Currency to locale mapping
        currencyLocales: {
            'NGN': 'en-NG',
            'USD': 'en-US',
            'EUR': 'de-DE',
            'GBP': 'en-GB',
            'ZAR': 'en-ZA',
            'KES': 'en-KE',
            'GHS': 'en-GH',
            'XAF': 'fr-CM',
            'XOF': 'fr-SN'
        },

        init() {
            // Initialize with existing line items or create one empty item
            if (config.lineItems && config.lineItems.length > 0) {
                this.lineItems = config.lineItems.map(item => ({
                    id: this.nextItemId++,
                    lineId: item.id || item.line_id || '',
                    description: item.description || '',
                    quantity: parseFloat(item.quantity) || 1,
                    unitPrice: parseFloat(item.unit_price) || 0,
                    taxRateId: item.tax_rate_id || '',
                    taxRate: this.getTaxRateValue(item.tax_rate_id)
                }));
            } else {
                this.addLineItem();
            }

            // Set default dates if creating new invoice
            if (!config.invoiceId) {
                const today = new Date().toISOString().split('T')[0];
                if (!this.issuedAt) {
                    this.issuedAt = today;
                }
                if (!this.dueAt) {
                    // Default to 30 days from issue date
                    const dueDate = new Date();
                    dueDate.setDate(dueDate.getDate() + (config.paymentTermsDays || 30));
                    this.dueAt = dueDate.toISOString().split('T')[0];
                }
            }

            // Watch issued date changes to update due date
            this.$watch('issuedAt', (value) => {
                if (value && !config.invoiceId) {
                    const issueDate = new Date(value);
                    issueDate.setDate(issueDate.getDate() + (config.paymentTermsDays || 30));
                    this.dueAt = issueDate.toISOString().split('T')[0];
                }
            });

            this.bindAccountField();
        },

        bindAccountField() {
            const sync = () => {
                const accountField = this.$el.querySelector("[name='account_id']");
                if (accountField) {
                    this.accountId = accountField.value || "";
                }
            };

            sync();
            this.$el.addEventListener("change", (event) => {
                const target = event && event.target;
                if (target && target.name === "account_id") {
                    this.accountId = target.value || "";
                }
            });

            // HTMX swaps account selector markup after customer changes.
            this.$el.addEventListener("htmx:afterSwap", sync);
        },

        // Line item management
        addLineItem() {
            if (this.lineItems.length >= this.maxItems) {
                this.showToast('Maximum number of line items reached', 'warning');
                return;
            }

            this.lineItems.push({
                id: this.nextItemId++,
                lineId: '',
                description: '',
                quantity: 1,
                unitPrice: 0,
                taxRateId: '',
                taxRate: 0
            });

            // Focus the new description field
            this.$nextTick(() => {
                const inputs = this.$el.querySelectorAll('[data-line-description]');
                if (inputs.length > 0) {
                    inputs[inputs.length - 1].focus();
                }
            });
        },

        removeLineItem(index) {
            if (this.lineItems.length <= this.minItems) {
                this.showToast('At least one line item is required', 'warning');
                return;
            }
            this.lineItems.splice(index, 1);
        },

        duplicateLineItem(index) {
            if (this.lineItems.length >= this.maxItems) {
                this.showToast('Maximum number of line items reached', 'warning');
                return;
            }

            const item = this.lineItems[index];
            const newItem = {
                id: this.nextItemId++,
                lineId: '',
                description: item.description,
                quantity: item.quantity,
                unitPrice: item.unitPrice,
                taxRateId: item.taxRateId,
                taxRate: item.taxRate
            };

            this.lineItems.splice(index + 1, 0, newItem);
        },

        // Tax rate handling
        getTaxRateValue(taxRateId) {
            if (!taxRateId) return 0;
            const rate = this.taxRates.find(r => r.id === taxRateId || r.id === parseInt(taxRateId));
            return rate ? parseFloat(rate.rate) : 0;
        },

        updateLineItemTaxRate(index) {
            const item = this.lineItems[index];
            item.taxRate = this.getTaxRateValue(item.taxRateId);
        },

        // Calculations - using fixed-point math to avoid floating point errors
        round(value, decimals = 2) {
            // Use Math.round with multiplier for precision
            const factor = Math.pow(10, decimals);
            return Math.round((parseFloat(value) || 0) * factor) / factor;
        },

        getLineItemTotal(item) {
            const quantity = this.round(item.quantity, 3);
            const unitPrice = this.round(item.unitPrice, 2);
            return this.round(quantity * unitPrice, 2);
        },

        getLineItemTax(item) {
            const subtotal = this.getLineItemTotal(item);
            const taxRate = parseFloat(item.taxRate) || 0;
            return this.round(subtotal * (taxRate / 100), 2);
        },

        getLineItemGross(item) {
            return this.round(this.getLineItemTotal(item) + this.getLineItemTax(item), 2);
        },

        get subtotal() {
            return this.round(
                this.lineItems.reduce((sum, item) => sum + this.getLineItemTotal(item), 0),
                2
            );
        },

        get taxTotal() {
            const originalTax = this.round(
                this.lineItems.reduce((sum, item) => sum + this.getLineItemTax(item), 0),
                2
            );
            if (this.subtotal <= 0 || this.discountAmount <= 0) return originalTax;
            return this.round(originalTax * (this.discountedSubtotal / this.subtotal), 2);
        },

        get discountAmount() {
            const value = this.round(this.discountValue, 2);
            if (!this.discountType || value <= 0) return 0;
            if (this.discountType === 'percentage') {
                return this.round(Math.min(this.subtotal, this.subtotal * value / 100), 2);
            }
            return this.round(Math.min(this.subtotal, value), 2);
        },

        get discountedSubtotal() {
            return this.round(Math.max(0, this.subtotal - this.discountAmount), 2);
        },

        get total() {
            return this.round(this.discountedSubtotal + this.taxTotal, 2);
        },

        // Formatting
        getLocaleForCurrency() {
            return this.currencyLocales[this.currency] || 'en-US';
        },

        formatCurrency(amount) {
            return new Intl.NumberFormat(this.getLocaleForCurrency(), {
                style: 'currency',
                currency: this.currency,
                minimumFractionDigits: this.decimalPlaces,
                maximumFractionDigits: this.decimalPlaces
            }).format(this.round(amount, 2));
        },

        formatNumber(amount) {
            return new Intl.NumberFormat(this.getLocaleForCurrency(), {
                minimumFractionDigits: this.decimalPlaces,
                maximumFractionDigits: this.decimalPlaces
            }).format(this.round(amount, 2));
        },

        // Form submission
        getFormData() {
            return {
                account_id: this.accountId,
                invoice_number: this.invoiceNumber,
                status: this.status,
                currency: this.currency,
                issued_at: this.issuedAt,
                due_at: this.dueAt,
                memo: this.memo,
                discount_type: this.discountType || null,
                discount_value: this.discountValue || null,
                discount_reason: this.discountReason || null,
                line_items: this.lineItems.map(item => ({
                    id: item.lineId || null,
                    description: item.description,
                    quantity: parseFloat(item.quantity) || 1,
                    unit_price: parseFloat(item.unitPrice) || 0,
                    tax_rate_id: item.taxRateId || null
                }))
            };
        },

        getLineItemsJSON() {
            return JSON.stringify(this.lineItems.map(item => ({
                id: item.lineId || null,
                description: item.description,
                quantity: this.round(item.quantity, 3),
                unitPrice: this.round(item.unitPrice, 2),
                taxRateId: item.taxRateId || null
            })));
        },

        async handleSubmit(event) {
            // Prevent double submission
            if (this.submitting) {
                event.preventDefault();
                return false;
            }

            // Validate required fields
            if (!this.accountId) {
                this.showToast('Please select an account', 'error');
                event.preventDefault();
                return false;
            }

            // Validate line items have descriptions
            const emptyItems = this.lineItems.filter(item => !item.description.trim());
            if (emptyItems.length > 0) {
                this.showToast('Please enter a description for all line items', 'error');
                event.preventDefault();
                return false;
            }

            if (this.discountType && (!(parseFloat(this.discountValue) > 0))) {
                this.showToast('Enter a Discount value greater than zero', 'error');
                event.preventDefault();
                return false;
            }

            if (this.discountType === 'percentage' && parseFloat(this.discountValue) > 100) {
                this.showToast('Percentage discount cannot be greater than 100', 'error');
                event.preventDefault();
                return false;
            }

            if (this.discountType === 'fixed_amount' && parseFloat(this.discountValue) > this.subtotal) {
                this.showToast('Discount cannot be greater than the subtotal', 'error');
                event.preventDefault();
                return false;
            }

            this.submitting = true;
            return true;
        },

        // Utility
        showToast(message, type = 'info') {
            window.dispatchEvent(new CustomEvent('show-toast', {
                detail: { message, type }
            }));
        },

        // Quick actions
        clearAllLineItems() {
            this.lineItems = [];
            this.addLineItem();
        },

        applyTaxToAll(taxRateId) {
            const taxRate = this.getTaxRateValue(taxRateId);
            this.lineItems.forEach(item => {
                item.taxRateId = taxRateId;
                item.taxRate = taxRate;
            });
        }
    };
}

window.invoiceCalculator = createInvoiceCalculator;

function registerInvoiceCalculator() {
    if (!window.Alpine) {
        return;
    }

    window.Alpine.data('invoiceCalculator', createInvoiceCalculator);
}

if (window.Alpine) {
    registerInvoiceCalculator();
} else {
    document.addEventListener('alpine:init', registerInvoiceCalculator);
}
