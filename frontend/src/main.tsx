import React, { Suspense, lazy } from 'react'
import ReactDOM from 'react-dom/client'
import { Navigate, RouterProvider, createHashRouter } from 'react-router-dom'
import App from './App'
import Market from './pages/Market'
import { TableSkeleton } from './components/TableSkeleton'
import './styles.css'

/**
 * 图表页和持仓页各自拖着一个大依赖（lightweight-charts / recharts），
 * 但落地页是行情看板，用不上它们。按路由拆包后首屏只需要下载
 * react + router + 看板本身。
 *
 * 行情看板不拆：它是默认入口，拆了反而多一次往返。
 */
const Chart = lazy(() => import('./pages/Chart'))
const Portfolio = lazy(() => import('./pages/Portfolio'))
const News = lazy(() => import('./pages/News'))
const Settings = lazy(() => import('./pages/Settings'))

const chunkFallback = (
  <div className="page col">
    <div className="panel"><TableSkeleton rows={8} cols={6} /></div>
  </div>
)
const lazyPage = (el: React.ReactNode) => <Suspense fallback={chunkFallback}>{el}</Suspense>

/**
 * 用 data router（createHashRouter）而不是 <HashRouter> + <Routes>：
 * 只有 data router 支持 viewTransition，页面切换才能走浏览器原生的
 * View Transitions API。路径与 hash 形式不变，老书签照常可用。
 */
const router = createHashRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <Navigate to="/market" replace /> },
      { path: 'market', element: <Market /> },
      { path: 'chart', element: lazyPage(<Chart />) },
      { path: 'chart/:symbol', element: lazyPage(<Chart />) },
      { path: 'portfolio', element: lazyPage(<Portfolio />) },
      { path: 'news', element: lazyPage(<News />) },
      { path: 'settings', element: lazyPage(<Settings />) },
      // 手敲错路径时回行情看板，而不是给一屏空白
      { path: '*', element: <Navigate to="/market" replace /> },
    ],
  },
])

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
)
