import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';

const EChartsCore = lazy(() => import('./EChartsCore'));

export default function Chart(props: { option: object; style?: CSSProperties; notMerge?: boolean; lazyUpdate?: boolean }) {
  const host = useRef<HTMLDivElement>(null);
  const [nearViewport, setNearViewport] = useState(false);

  useEffect(() => {
    const node = host.current;
    if (!node) return;
    if (!('IntersectionObserver' in window)) { setNearViewport(true); return; }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        setNearViewport(true);
        observer.disconnect();
      }
    }, { rootMargin: '180px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const style = props.style || {};
  return <div ref={host} className="chart-loading-frame" style={style} aria-label="数据图表">
    {nearViewport ? <Suspense fallback={<div className="chart-loading">正在加载图表…</div>}><EChartsCore {...props} /></Suspense>
      : <div className="chart-loading" aria-hidden="true" />}
  </div>;
}
