/* Page-only dependencies; successful loads are shared, failures can be retried. */
const QuicStudioPageAssets = (() => {
  const sources = {
    qr: {
      src: '/vendor/qrcode-generator/1.5.0/qrcode.js',
    },
    charts: {
      src: '/vendor/echarts/5.5.1/echarts.min.js',
    },
  };
  const pending = new Map();
  function load(name) {
    if (pending.has(name)) return pending.get(name);
    const source = sources[name];
    if (!source) return Promise.reject(new Error('Unknown page dependency'));
    const promise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = source.src;
      script.async = true;
      script.onload = () => { script.onload = null; script.onerror = null; resolve(); };
      script.onerror = () => {
        pending.delete(name);
        script.remove();
        reject(new Error('页面组件加载失败，请重试。 / Page component failed to load. Please retry.'));
      };
      document.head.appendChild(script);
    });
    pending.set(name, promise);
    return promise;
  }
  return { load };
})();
