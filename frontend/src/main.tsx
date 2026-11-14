import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles.css';

/** 渲染层异常不能让整页白屏：业务状态在后端磁盘上，刷新即可重新读取。 */
class WorkbenchErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('工作台渲染异常', error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="fatal-error" role="alert">
          <h1>工作台界面暂时无法显示</h1>
          <p>本机后端数据未受影响，审核与产物仍保存在磁盘上。请刷新页面重试；若重复出现，请保留下方错误信息与浏览器控制台日志。</p>
          <pre>{String(this.state.error.message || this.state.error)}</pre>
          <button type="button" onClick={() => window.location.reload()}>重新加载工作台</button>
        </div>
      );
    }
    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <WorkbenchErrorBoundary><App /></WorkbenchErrorBoundary>
  </React.StrictMode>,
);
