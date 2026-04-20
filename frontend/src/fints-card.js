function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = String(str ?? "");
  return div.innerHTML;
}

class FintsAtruviaCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
  }

  setConfig(config) {
    if (!config.entity && (!config.entities || config.entities.length === 0)) {
      throw new Error("Please define 'entity' or 'entities' in the card config");
    }
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 3;
  }

  _getEntityIds() {
    if (this._config.entities) {
      return Array.isArray(this._config.entities)
        ? this._config.entities
        : [this._config.entities];
    }
    return [this._config.entity];
  }

  _formatCurrency(amount, currency = "EUR") {
    if (amount == null) return "–";
    return new Intl.NumberFormat("de-DE", {
      style: "currency",
      currency: currency,
    }).format(amount);
  }

  _formatDate(dateStr) {
    if (!dateStr) return "";
    try {
      return new Intl.DateTimeFormat("de-DE", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
      }).format(new Date(dateStr));
    } catch {
      return dateStr;
    }
  }

  _maskIban(iban) {
    if (!iban) return "";
    const clean = iban.replace(/\s/g, "");
    const last4 = clean.slice(-4);
    const country = clean.slice(0, 2);
    const masked = "**** **** **** ****";
    return `${country}** ${masked.slice(4)} ${last4}`;
  }

  _truncate(str, maxLen) {
    if (!str) return "";
    return str.length > maxLen ? str.slice(0, maxLen) + "…" : str;
  }

  _renderEntity(entityId) {
    if (!this._hass) return "";

    const stateObj = this._hass.states[entityId];
    if (!stateObj) {
      return `
        <ha-card header="Unbekannte Entität">
          <div class="card-content">
            <p class="error">Entität nicht gefunden: ${escapeHtml(entityId)}</p>
          </div>
        </ha-card>`;
    }

    const attr = stateObj.attributes || {};
    const currency = attr.unit_of_measurement || "EUR";
    const balance = parseFloat(stateObj.state);
    const isNegative = !isNaN(balance) && balance < 0;
    const balanceClass = isNegative ? "balance negative" : "balance positive";
    const balanceFormatted = isNaN(balance) ? stateObj.state : this._formatCurrency(balance, currency);

    const iban = attr.iban || "";
    const maskedIban = iban ? this._maskIban(iban) : "";
    const last4 = iban ? iban.replace(/\s/g, "").slice(-4) : entityId.split(".").pop();

    const accountName =
      attr.friendly_name ||
      attr.account_name ||
      (iban ? `Konto …${last4}` : entityId.split(".").pop());

    const twoPending = attr["2fa_pending"] === true || attr["2fa_pending"] === "true";

    const transactions = Array.isArray(attr.transactions) ? attr.transactions : [];
    const lastFive = transactions.slice(0, 5);

    const transactionRows = lastFive
      .map((tx, i) => {
        const txAmount = parseFloat(tx.amount);
        const txClass = !isNaN(txAmount) && txAmount < 0 ? "amount negative" : "amount positive";
        const txFormatted = isNaN(txAmount) ? tx.amount : this._formatCurrency(txAmount, currency);
        const txDate = this._formatDate(tx.date || tx.booking_date || tx.booking_datetime);
        const purpose = this._truncate(tx.purpose || tx.reference || tx.creditor_name || "–", 40);
        const rowClass = i % 2 === 0 ? "transaction even" : "transaction odd";
        return `
          <div class="${rowClass}">
            <span class="date">${txDate}</span>
            <span class="${txClass}">${txFormatted}</span>
            <span class="purpose">${escapeHtml(purpose)}</span>
          </div>`;
      })
      .join("");

    const warningBanner = twoPending
      ? `<div class="warning-banner">⚠ Re-Authentifizierung erforderlich</div>`
      : "";

    const transactionsSection =
      lastFive.length > 0
        ? `<details>
            <summary>Letzte Transaktionen (${lastFive.length})</summary>
            <div class="transactions">${transactionRows}</div>
          </details>`
        : `<p class="no-transactions">Keine Transaktionen verfügbar</p>`;

    return `
      <ha-card header="Konto ${escapeHtml(last4)}">
        <div class="card-content">
          <div class="account-name">${escapeHtml(accountName)}</div>
          <div class="${balanceClass}">${balanceFormatted}</div>
          ${maskedIban ? `<div class="iban">IBAN: ${escapeHtml(maskedIban)}</div>` : ""}
          ${warningBanner}
          ${transactionsSection}
        </div>
      </ha-card>`;
  }

  _render() {
    if (!this._config || !this._hass) return;

    const entityIds = this._getEntityIds();
    const cards = entityIds.map((id) => this._renderEntity(id)).join("\n");

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
        }
        ha-card {
          display: block;
          margin-bottom: 8px;
        }
        .card-content {
          padding: 0 16px 16px;
        }
        .account-name {
          font-size: 0.9rem;
          color: var(--secondary-text-color, #727272);
          margin-bottom: 4px;
          text-transform: uppercase;
          letter-spacing: 0.05em;
        }
        .balance {
          font-size: 2rem;
          font-weight: bold;
          margin: 8px 0;
          line-height: 1.1;
        }
        .balance.positive {
          color: var(--success-color, #43a047);
        }
        .balance.negative {
          color: var(--error-color, #db4437);
        }
        .iban {
          font-size: 0.85rem;
          color: var(--secondary-text-color, #727272);
          font-family: monospace;
          letter-spacing: 0.05em;
          margin-bottom: 8px;
        }
        .warning-banner {
          background-color: var(--warning-color, #ff9800);
          color: var(--warning-text-color, #fff);
          padding: 8px 12px;
          border-radius: 4px;
          font-size: 0.9rem;
          font-weight: 500;
          margin: 8px 0;
        }
        details {
          margin-top: 12px;
        }
        summary {
          cursor: pointer;
          font-size: 0.9rem;
          font-weight: 500;
          color: var(--primary-text-color, #212121);
          padding: 4px 0;
          user-select: none;
        }
        summary:hover {
          color: var(--primary-color, #03a9f4);
        }
        .transactions {
          margin-top: 8px;
        }
        .transaction {
          display: grid;
          grid-template-columns: 90px 1fr 1fr;
          gap: 6px;
          padding: 5px 6px;
          border-radius: 3px;
          font-size: 0.8rem;
          align-items: center;
        }
        .transaction.even {
          background-color: var(--table-row-background-color, transparent);
        }
        .transaction.odd {
          background-color: var(--table-row-alternative-background-color, rgba(0,0,0,0.04));
        }
        .date {
          color: var(--secondary-text-color, #727272);
          white-space: nowrap;
        }
        .amount {
          font-weight: 500;
          text-align: right;
          white-space: nowrap;
        }
        .amount.positive {
          color: var(--success-color, #43a047);
        }
        .amount.negative {
          color: var(--error-color, #db4437);
        }
        .purpose {
          color: var(--primary-text-color, #212121);
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .no-transactions {
          font-size: 0.85rem;
          color: var(--secondary-text-color, #727272);
          margin-top: 8px;
        }
        .error {
          color: var(--error-color, #db4437);
          font-size: 0.9rem;
        }
      </style>
      ${cards}
    `;
  }
}

customElements.define("fints-atruvia-card", FintsAtruviaCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "fints-atruvia-card",
  name: "FinTS Atruvia Banking Card",
  description: "Zeigt Kontostand und Transaktionen für FinTS Atruvia Konten",
});
