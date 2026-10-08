/** Quic EGO on-site offline collection QR code control and generation pure function module. */
const QuicDataQrControlCodes = (() => {
  const VERSION = 1;
  const DEFAULT_SEGMENT_ID = 'manual_boundary';
  const COLLECTOR_ID_PATTERN = /^\d+$/;

  function collectorId(profile) {
    const value = String(profile?.profile_key || '').trim();
    return COLLECTOR_ID_PATTERN.test(value) ? value : '';
  }

  function forCollector(profile) {
    const id = collectorId(profile);
    if (!id) return null;
    const control = JSON.stringify({ v: VERSION, kind: 'control', collector_id: id });
    const segment = JSON.stringify({
      v: VERSION,
      kind: 'segment',
      collector_id: id,
      segment_id: DEFAULT_SEGMENT_ID,
    });
    return {
      collector_id: id,
      control: { kind: 'control', payload: control },
      segment: { kind: 'segment', segment_id: DEFAULT_SEGMENT_ID, payload: segment },
    };
  }

  function svg(payload, { alt = 'QR code', title = 'QR code' } = {}) {
    if (typeof qrcode !== 'function' || typeof payload !== 'string' || !payload) return '';
    const code = qrcode(0, 'M');
    code.addData(payload, 'Byte');
    code.make();
    return code.createSvgTag(5, 4, String(alt), String(title));
  }

  function downloadSvg(svgMarkup, fileName) {
    if (!svgMarkup || typeof document === 'undefined' || typeof URL === 'undefined' || typeof Blob === 'undefined') return false;
    const safeName = String(fileName || 'quic-ego-code.svg').replace(/[^A-Za-z0-9._-]+/g, '-').slice(0, 120) || 'quic-ego-code.svg';
    const blob = new Blob([svgMarkup], { type: 'image/svg+xml;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = safeName.endsWith('.svg') ? safeName : `${safeName}.svg`;
    anchor.style.display = 'none';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    return true;
  }

  function escapeHtml(value) {
    return String(value || '').replace(/[&<>"']/g, (character) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[character]);
  }

  function print(cards, { title = 'Quic EGO control codes', collector = '' } = {}) {
    if (typeof window === 'undefined' || !Array.isArray(cards) || !cards.length) return false;
    const popup = window.open('', '_blank', 'noopener,noreferrer,width=920,height=760');
    if (!popup) return false;
    const cardsHtml = cards.map((card) => `
      <section class="card">
        <h2>${escapeHtml(card.label)}</h2>
        <div class="code">${String(card.svg || '')}</div>
        <code>${escapeHtml(card.collector_id)}</code>
      </section>
    `).join('');
    popup.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${escapeHtml(title)}</title><style>body{font-family:Arial,sans-serif;margin:28px;color:#172033}h1{font-size:20px;margin:0 0 22px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px}.card{border:1px solid #cfd6e3;padding:20px;text-align:center}.card h2{font-size:17px;margin:0 0 14px}.code svg{width:250px;height:250px;max-width:100%}.card code{display:block;margin-top:12px;font-size:14px}@media print{body{margin:12mm}.card{break-inside:avoid}}</style></head><body><h1>${escapeHtml(title)} · ${escapeHtml(collector)}</h1><div class="grid">${cardsHtml}</div><script>window.addEventListener('load',()=>window.print())<\/script></body></html>`);
    popup.document.close();
    return true;
  }

  return Object.freeze({ VERSION, DEFAULT_SEGMENT_ID, collectorId, forCollector, svg, downloadSvg, print });
})();

if (typeof globalThis !== 'undefined') globalThis.QuicDataQrControlCodes = QuicDataQrControlCodes;
