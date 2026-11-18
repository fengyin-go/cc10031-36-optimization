import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ReportDashboardView from '@/views/ReportDashboardView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 运行概览使用增量指标专属看板，固定路径必须排在动态模块路由之前
    {
      path: '/report_dashboard',
      name: 'report-dashboard',
      component: ReportDashboardView,
    },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
