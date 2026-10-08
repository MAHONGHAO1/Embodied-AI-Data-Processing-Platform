/* Route-owned pages: load their code and styles only when Vue mounts the page. */
const QuicStudioPageComponents = (() => {
  const pages = {
    'train-console': { js: '/js/train-console.js?v=9', css: '/css/train-console.css?v=2', module: () => QuicTrainConsole },
    'collection-overview': { deps: ['/js/dashboard-charts.js?v=2', '/js/collection-dashboard-filters.js?v=1', '/js/collection-data-board.js?v=3', '/js/collection-capacity-board.js?v=4', '/js/collection-efficiency-board.js?v=2'], js: '/js/collection-overview.js?v=7', module: () => QuicStudioCollectionOverview },
    'data-overview': { js: '/js/data-overview.js?v=12', module: () => QuicStudioDataOverview },
    'intake-review-workbench': { js: '/js/intake-review-workbench.js?v=2', css: '/css/intake-review-workbench.css?v=1', module: () => QuicDataIntakeReviewWorkbench },
    'package-annotation-workbench': { js: '/js/package-annotation-workbench.js?v=5', css: '/css/package-annotation-workbench.css?v=3', module: () => QuicDataPackageAnnotationWorkbench },
    'data-catalog': { js: '/js/data-catalog.js?v=5', css: '/css/data-catalog.css?v=4', module: () => QuicDataCatalog },
  };
  const assets = new Map(), components = new Map();
  function loadAsset(url, style = false) {
    if (assets.has(url)) return assets.get(url);
    const promise = new Promise((resolve, reject) => {
      const element = document.createElement(style ? 'link' : 'script');
      if (style) { element.rel = 'stylesheet'; element.href = url; }
      else { element.src = url; element.async = true; }
      element.onload = () => { element.onload = null; element.onerror = null; resolve(); };
      element.onerror = () => {
        assets.delete(url);
        element.remove();
        reject(new Error('页面加载失败，请刷新重试。 / Page load failed. Please refresh.'));
      };
      document.head.appendChild(element);
    });
    assets.set(url, promise);
    return promise;
  }
  function load(name) {
    if (components.has(name)) return components.get(name);
    const page = pages[name];
    if (!page) return Promise.reject(new Error('Unknown page'));
    const assets = page.deps
      ? page.deps.reduce((chain, url) => chain.then(() => loadAsset(url)), Promise.resolve())
        .then(() => Promise.all([loadAsset(page.js), ...(page.css ? [loadAsset(page.css, true)] : [])]))
      : Promise.all([loadAsset(page.js), ...(page.css ? [loadAsset(page.css, true)] : [])]);
    const promise = assets
      .then(() => {
        let component;
        page.module().install({ component(registered, definition) { if (registered === name) component = definition; } });
        if (!component) throw new Error('Page component registration failed');
        return component;
      }).catch(error => { components.delete(name); throw error; });
    components.set(name, promise);
    return promise;
  }
  function install(app) {
    for (const name of Object.keys(pages)) {
      app.component(name, Vue.defineAsyncComponent({
        loader: () => load(name), delay: 150,
        loadingComponent: { template: '<div role="status" class="surface-panel">正在加载… / Loading…</div>' },
        errorComponent: { template: '<div role="alert" class="surface-panel">页面加载失败，请刷新重试。 / Page load failed. Please refresh.</div>' },
        onError(error, retry, fail, attempts) { if (attempts < 2) retry(); else fail(); },
      }));
    }
  }
  return { load, install };
})();
