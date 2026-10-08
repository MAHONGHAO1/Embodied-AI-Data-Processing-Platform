/* Demo fixtures are an explicit local-preview dependency, never a production entry dependency. */
(() => {
  const host = String(window.location.hostname || '');
  const params = new URLSearchParams(window.location.search || '');
  const localDemo = ['localhost', '127.0.0.1'].includes(host)
    && (params.get('demo') === '1' || params.get('mock') === '1');
  function load(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = src;
      script.async = false;
      script.onload = resolve;
      script.onerror = () => reject(new Error(`Unable to load ${src}`));
      document.head.appendChild(script);
    });
  }
  async function boot() {
    if (localDemo) {
      await load('/js/demo-data.js?v=15');
      await load('/js/mining-console.js?v=2');
    }
    await load('/js/app.js?v=407');
  }
  void boot().catch(() => {
    const root = document.getElementById('app');
    if (root) root.textContent = '页面加载失败，请刷新重试。 / Page load failed. Please refresh.';
  });
})();
