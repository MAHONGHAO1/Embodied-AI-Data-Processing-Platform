import { useEffect, useRef } from 'react';
import type { CSSProperties } from 'react';
import { init, use, type ECharts, type EChartsCoreOption } from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

interface Props {
  option: object;
  style?: CSSProperties;
  notMerge?: boolean;
  lazyUpdate?: boolean;
}

export default function EChartsCore({ option, style, notMerge, lazyUpdate }: Props) {
  const elementRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ECharts | null>(null);

  useEffect(() => {
    const element = elementRef.current;
    if (!element) return;
    const chart = init(element);
    chartRef.current = chart;
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(element);
    return () => {
      observer.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option as EChartsCoreOption, { notMerge, lazyUpdate });
  }, [option, notMerge, lazyUpdate]);

  return <div ref={elementRef} role="img" aria-label="数据图表" style={style} />;
}
