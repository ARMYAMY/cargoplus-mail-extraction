// CargoPlus Web Console JavaScript
document.addEventListener('DOMContentLoaded', () => {
  let throughputChart = null;
  let revenueChart = null;
  let historyVolumeChart = null;
  let historyRevenueChart = null;
  let historyLoadedDays = null;
  let historyRequestSequence = 0;
  let currentTenants = [];
  let selectedFiles = [];
  const adminToken = localStorage.getItem('cargo_admin_token') || '';
  const MIN_UNIT_PRICE = 0.01;
  const MAX_UNIT_PRICE = 100;
  const MIN_RECHARGE_AMOUNT = 0.01;
  const MAX_RECHARGE_AMOUNT = 1000000;
  const MIN_TENANT_CONCURRENCY = 1;
  const MAX_TENANT_CONCURRENCY = 30;
  const MIN_BENCHMARK_TASKS = 1;
  const MAX_BENCHMARK_TASKS = 100;

  let isRedirectingToLogin = false;

  async function adminFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    const currentToken = localStorage.getItem('cargo_admin_token') || '';
    if (!headers.has('Authorization') && currentToken) {
      headers.set('Authorization', `Bearer ${currentToken}`);
    }

    try {
      const response = await fetch(url, { ...options, headers, credentials: 'same-origin' });

      // 1. Sliding Session: Update token if backend returned refreshed token
      const refreshedToken = response.headers.get('X-Refreshed-Token');
      if (refreshedToken) {
        localStorage.setItem('cargo_admin_token', refreshedToken);
      }

      // 2. Intercept 401/403 session expiration cleanly
      if ((response.status === 401 || response.status === 403) && !isRedirectingToLogin) {
        isRedirectingToLogin = true;
        localStorage.removeItem('cargo_admin_token');
        showToast('error', '登录会话已过期，请重新登录，正在为您跳转...', '会话已过期');
        setTimeout(() => {
          window.location.href = '/login?expired=1';
        }, 1200);
      }

      return response;
    } catch (err) {
      throw err;
    }
  }

  async function downloadAdminAttachment(feedbackId, filename) {
    const url = `/admin/feedbacks/${encodeURIComponent(feedbackId)}/attachments/${encodeURIComponent(filename)}`;
    const response = await adminFetch(url, { method: 'GET' });
    if (!response.ok) {
      let detail = '';
      try {
        const body = await response.json();
        detail = typeof body?.detail === 'string' ? body.detail : '';
      } catch (_) {
        // The proxy may return a plain-text or HTML error page.
      }
      throw new Error(detail || `服务返回 HTTP ${response.status}`);
    }

    const blob = await response.blob();
    if (blob.size === 0) {
      throw new Error('服务器返回了空附件');
    }
    const blobUrl = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = blobUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    // WebView/Safari may not start reading the Blob until after this handler
    // returns, so revoking synchronously can cancel an otherwise valid download.
    window.setTimeout(() => window.URL.revokeObjectURL(blobUrl), 60_000);
  }

  // Tab Switching
  const navItems = document.querySelectorAll('.nav-item');
  const tabContents = document.querySelectorAll('.tab-content');
  const pageTitle = document.getElementById('page-title');
  const pageSubtitle = document.getElementById('page-subtitle');

  const tabMeta = {
    dashboard: {
      title: '系统大盘总览',
      subtitle: '监控日均 5,000~10,000 封海运邮件抽取吞吐量、SLA 及多租户扣费状态',
    },
    tenants: {
      title: '租户与密钥管理',
      subtitle: '管理货代企业租户账户、API Key 授权、并发上限与自定义单价',
    },
    tasks_billing: {
      title: '任务全流程追踪与资金对账',
      subtitle: '支持在单证任务解析详情（57字段与耗时）与财务收支流水（扣费与充值）之间无缝切换',
    },
    workbench: {
      title: '在线调试工作台',
      subtitle: '支持直接粘贴邮件正文、上传订舱单/提单单证文件，进行实时 V3 抽取验证',
    },
    llm_config: {
      title: '大模型服务配置',
      subtitle: '配置上游大模型 API 接口地址 (Base URL)、认证密钥 (API Key) 与模型参数',
    },
    feedback_optimization: {
      title: '反馈审核与模型自进化优化',
      subtitle: '审核租户纠错反馈并自动退款冲正，维护动态 Few-Shot 样本库，执行全量金标回归评测与版本发布',
    },
  };

  navItems.forEach((btn) => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      navItems.forEach((b) => b.classList.remove('active'));
      tabContents.forEach((c) => c.classList.remove('active'));
      btn.classList.add('active');
      const targetContent = document.getElementById(`tab-${tab}`);
      if (targetContent) targetContent.classList.add('active');

      if (tabMeta[tab]) {
        pageTitle.textContent = tabMeta[tab].title;
        pageSubtitle.textContent = tabMeta[tab].subtitle;
      }

      // Load corresponding tab data
      if (tab === 'dashboard') refreshActiveDashboardView();
      if (tab === 'tenants') loadTenantsTable();
      if (tab === 'tasks_billing') {
        loadTasksTable();
        loadBillingTable();
      }
      if (tab === 'workbench') populateWorkbenchKeySelect();
      if (tab === 'llm_config') loadLLMConfig();
      if (tab === 'feedback_optimization') {
        loadAdminFeedbacks();
        loadAdminFewShots();
        loadAdminVersions();
      }
    });
  });

  // Global Refresh Button
  document.getElementById('btn-refresh').addEventListener('click', () => {
    const activeTab = document.querySelector('.nav-item.active').dataset.tab;
    if (activeTab === 'dashboard') refreshActiveDashboardView(true);
    if (activeTab === 'tenants') loadTenantsTable();
    if (activeTab === 'tasks_billing') {
      loadTasksTable();
      loadBillingTable();
    }
    if (activeTab === 'llm_config') loadLLMConfig();
    if (activeTab === 'feedback_optimization') {
      loadAdminFeedbacks();
      loadAdminFewShots();
      loadAdminVersions();
    }
  });


  // Modal Management Helpers
  function openModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add('active');
  }

  function closeModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
  }

  document.querySelectorAll('[data-close]').forEach((btn) => {
    btn.addEventListener('click', () => {
      closeModal(btn.dataset.close);
    });
  });

  // -------------------------------------------------------------
  // 1. Dashboard Logic
  // -------------------------------------------------------------
  async function loadDashboardStats() {
    try {
      const res = await adminFetch('/admin/stats');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      document.getElementById('stat-today-total').textContent = data.today_total.toLocaleString();
      document.getElementById('stat-success-count').textContent = `${data.today_success} 成功`;
      document.getElementById('stat-failed-count').textContent = `${data.today_failed} 失败`;
      document.getElementById('stat-success-rate').textContent = `${data.today_success_rate}%`;
      document.getElementById('stat-avg-duration').textContent = `${data.avg_duration_ms} ms`;
      document.getElementById('stat-today-revenue').textContent = `¥${data.today_revenue.toFixed(2)}`;
      document.getElementById('stat-total-balance').textContent = `¥${data.total_tenant_balance.toFixed(2)}`;
      document.getElementById('stat-queue-backlog').textContent = data.queue_backlog.toLocaleString();

      const activeCount = Object.keys(data.active_tenants_running || {}).length;
      document.getElementById('stat-active-tenants').textContent = `${activeCount} 租户活跃处理中`;

      renderCharts(data.history_14_days || []);
    } catch (err) {
      console.error('Failed to load dashboard stats:', err);
    }
  }

  function renderCharts(historyData) {
    const labels = historyData.map((d) => d.date);
    const totals = historyData.map((d) => d.total);
    const successes = historyData.map((d) => d.success);
    const revenues = historyData.map((d) => d.revenue);

    // Throughput Chart
    const ctxThroughput = document.getElementById('chart-throughput').getContext('2d');
    if (throughputChart) throughputChart.destroy();
    throughputChart = new Chart(ctxThroughput, {
      type: 'line',
      data: {
        labels: labels,
        datasets: [
          {
            label: '邮件总量',
            data: totals,
            borderColor: '#38bdf8',
            backgroundColor: 'rgba(56, 189, 248, 0.1)',
            fill: true,
            tension: 0.3,
          },
          {
            label: '成功抽取',
            data: successes,
            borderColor: '#10b981',
            backgroundColor: 'transparent',
            borderDash: [4, 4],
            tension: 0.3,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: '#94a3b8' } },
        },
        scales: {
          x: { ticks: { color: '#64748b' }, grid: { color: 'rgba(255,255,255,0.05)' } },
          y: { ticks: { color: '#64748b' }, grid: { color: 'rgba(255,255,255,0.05)' } },
        },
      },
    });

    // Revenue Chart
    const ctxRev = document.getElementById('chart-revenue').getContext('2d');
    if (revenueChart) revenueChart.destroy();
    revenueChart = new Chart(ctxRev, {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [
          {
            label: '扣费营收 (元)',
            data: revenues,
            backgroundColor: 'rgba(37, 99, 235, 0.75)',
            borderRadius: 4,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: '#94a3b8' } },
        },
        scales: {
          x: { ticks: { color: '#64748b' }, grid: { color: 'rgba(255,255,255,0.05)' } },
          y: { ticks: { color: '#64748b' }, grid: { color: 'rgba(255,255,255,0.05)' } },
        },
      },
    });
  }

  const dashboardViewTabs = Array.from(document.querySelectorAll('[data-dashboard-view]'));
  const dashboardPanels = Array.from(document.querySelectorAll('.dashboard-panel'));

  function getActiveDashboardView() {
    return document.querySelector('[data-dashboard-view].active')?.dataset.dashboardView || 'realtime';
  }

  function refreshActiveDashboardView(force = false) {
    if (getActiveDashboardView() === 'history') {
      loadHistoricalDashboardStats(force);
    } else {
      loadDashboardStats();
    }
  }

  function activateDashboardView(view) {
    dashboardViewTabs.forEach((tab) => {
      const isActive = tab.dataset.dashboardView === view;
      tab.classList.toggle('active', isActive);
      tab.setAttribute('aria-selected', String(isActive));
      tab.tabIndex = isActive ? 0 : -1;
    });

    dashboardPanels.forEach((panel) => {
      const isActive = panel.id === `dashboard-panel-${view}`;
      panel.classList.toggle('active', isActive);
      panel.hidden = !isActive;
    });

    if (view === 'history') loadHistoricalDashboardStats();
  }

  dashboardViewTabs.forEach((tab, index) => {
    tab.addEventListener('click', () => activateDashboardView(tab.dataset.dashboardView));
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      let nextIndex = index;
      if (event.key === 'ArrowRight') nextIndex = (index + 1) % dashboardViewTabs.length;
      if (event.key === 'ArrowLeft') nextIndex = (index - 1 + dashboardViewTabs.length) % dashboardViewTabs.length;
      if (event.key === 'Home') nextIndex = 0;
      if (event.key === 'End') nextIndex = dashboardViewTabs.length - 1;
      const nextTab = dashboardViewTabs[nextIndex];
      nextTab.focus();
      activateDashboardView(nextTab.dataset.dashboardView);
    });
  });

  document.getElementById('history-period-days')?.addEventListener('change', () => {
    historyLoadedDays = null;
    loadHistoricalDashboardStats(true);
  });

  async function loadHistoricalDashboardStats(force = false) {
    const periodSelect = document.getElementById('history-period-days');
    const days = parseInt(periodSelect?.value || '90', 10);
    if (!force && historyLoadedDays === days) return;

    const requestSequence = ++historyRequestSequence;
    setHistoricalDashboardLoading(true);
    try {
      const res = await adminFetch(`/admin/stats/history?days=${days}`);
      let data;
      if (res.status === 404) {
        data = await buildHistoricalStatsFromLegacyEndpoints(days);
      } else {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        data = await res.json();
      }
      if (requestSequence !== historyRequestSequence) return;

      historyLoadedDays = days;
      renderHistoricalSummary(data);
      renderHistoricalCharts(data.history || []);
      renderHistoricalTenantRankings(data.tenant_rankings || []);
    } catch (err) {
      if (requestSequence !== historyRequestSequence) return;
      historyLoadedDays = null;
      console.error('Failed to load historical dashboard stats:', err);
      const tbody = document.querySelector('#table-history-tenants tbody');
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="6" class="text-center text-danger py-8">历史统计加载失败: ${escapeHtml(err.message)}</td></tr>`;
      }
      showToast('error', `历史经营分析加载失败: ${err.message}`);
    } finally {
      if (requestSequence === historyRequestSequence) setHistoricalDashboardLoading(false);
    }
  }

  async function fetchAllAdminPages(baseUrl) {
    const separator = baseUrl.includes('?') ? '&' : '?';
    const firstResponse = await adminFetch(`${baseUrl}${separator}page=1&page_size=100`);
    if (!firstResponse.ok) throw new Error(`${baseUrl} HTTP ${firstResponse.status}`);
    const firstPage = await firstResponse.json();
    const items = [...(firstPage.items || [])];
    const totalPages = Math.max(1, Math.ceil(Number(firstPage.total || items.length) / 100));

    for (let page = 2; page <= totalPages; page += 1) {
      const response = await adminFetch(`${baseUrl}${separator}page=${page}&page_size=100`);
      if (!response.ok) throw new Error(`${baseUrl} 第 ${page} 页 HTTP ${response.status}`);
      const pageData = await response.json();
      items.push(...(pageData.items || []));
    }
    return items;
  }

  async function buildHistoricalStatsFromLegacyEndpoints(days) {
    const [tasks, billingTransactions] = await Promise.all([
      fetchAllAdminPages('/admin/tasks'),
      fetchAllAdminPages('/admin/billing/transactions?type=DEDUCTION'),
    ]);
    const now = new Date();
    const utcToday = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
    const periodStart = new Date(utcToday);
    periodStart.setUTCDate(periodStart.getUTCDate() - (days - 1));
    const periodStartMs = periodStart.getTime();
    const validRevenueTransactions = billingTransactions.filter((tx) => {
      const amount = Number(tx.amount);
      return Number.isFinite(amount) && amount >= 0 && amount <= MAX_UNIT_PRICE;
    });
    const revenueAnomalyCount = billingTransactions.length - validRevenueTransactions.length;
    const completedTasks = tasks.filter((task) => ['SUCCESS', 'FAILED'].includes(task.status));
    const successfulTasks = tasks.filter((task) => task.status === 'SUCCESS');
    const failedTasks = tasks.filter((task) => task.status === 'FAILED');
    const periodTasks = tasks.filter((task) => new Date(task.created_at).getTime() >= periodStartMs);
    const periodSuccessful = periodTasks.filter((task) => task.status === 'SUCCESS');
    const periodFailed = periodTasks.filter((task) => task.status === 'FAILED');
    const totalRevenue = validRevenueTransactions.reduce((sum, tx) => sum + Number(tx.amount || 0), 0);
    const periodRevenueTransactions = validRevenueTransactions.filter(
      (tx) => new Date(tx.created_at).getTime() >= periodStartMs,
    );
    const periodRevenue = periodRevenueTransactions.reduce((sum, tx) => sum + Number(tx.amount || 0), 0);
    const durations = successfulTasks
      .map((task) => Number(task.duration_ms))
      .filter((duration) => Number.isFinite(duration) && duration >= 0);
    const sortedTaskDates = tasks
      .map((task) => new Date(task.created_at))
      .filter((date) => !Number.isNaN(date.getTime()))
      .sort((left, right) => left - right);

    const historyMap = {};
    for (let index = 0; index < days; index += 1) {
      const date = new Date(periodStart);
      date.setUTCDate(periodStart.getUTCDate() + index);
      const key = date.toISOString().slice(0, 10);
      historyMap[key] = { date: key, total: 0, success: 0, failed: 0, success_rate: 0, revenue: 0 };
    }
    periodTasks.forEach((task) => {
      const key = new Date(task.created_at).toISOString().slice(0, 10);
      const day = historyMap[key];
      if (!day) return;
      day.total += 1;
      if (task.status === 'SUCCESS') day.success += 1;
      if (task.status === 'FAILED') day.failed += 1;
    });
    periodRevenueTransactions.forEach((tx) => {
      const key = new Date(tx.created_at).toISOString().slice(0, 10);
      if (historyMap[key]) historyMap[key].revenue += Number(tx.amount || 0);
    });
    Object.values(historyMap).forEach((day) => {
      const completed = day.success + day.failed;
      day.success_rate = completed ? Math.round((day.success / completed * 100) * 10) / 10 : 0;
      day.revenue = Math.round(day.revenue * 10000) / 10000;
    });

    const tenantMap = new Map();
    tasks.forEach((task) => {
      const tenantId = task.tenant_id || 'unknown';
      if (!tenantMap.has(tenantId)) {
        const knownTenant = currentTenants.find((tenant) => tenant.id === tenantId);
        tenantMap.set(tenantId, {
          tenant_id: tenantId,
          tenant_name: knownTenant?.name || tenantId,
          total: 0,
          success: 0,
          failed: 0,
          success_rate: 0,
          revenue: 0,
        });
      }
      const tenant = tenantMap.get(tenantId);
      tenant.total += 1;
      if (task.status === 'SUCCESS') tenant.success += 1;
      if (task.status === 'FAILED') tenant.failed += 1;
    });
    validRevenueTransactions.forEach((tx) => {
      const tenant = tenantMap.get(tx.tenant_id);
      if (tenant) tenant.revenue += Number(tx.amount || 0);
    });
    const tenantRankings = Array.from(tenantMap.values())
      .map((tenant) => {
        const completed = tenant.success + tenant.failed;
        return {
          ...tenant,
          success_rate: completed ? Math.round((tenant.success / completed * 100) * 10) / 10 : 0,
          revenue: Math.round(tenant.revenue * 10000) / 10000,
        };
      })
      .sort((left, right) => right.total - left.total)
      .slice(0, 10);

    return {
      lifetime: {
        total: tasks.length,
        success: successfulTasks.length,
        failed: failedTasks.length,
        in_progress: Math.max(0, tasks.length - completedTasks.length),
        success_rate: completedTasks.length ? Math.round((successfulTasks.length / completedTasks.length * 100) * 10) / 10 : 0,
        revenue: Math.round(totalRevenue * 10000) / 10000,
        avg_duration_ms: durations.length ? Math.round(durations.reduce((sum, value) => sum + value, 0) / durations.length) : 0,
        avg_revenue_per_success: successfulTasks.length ? Math.round((totalRevenue / successfulTasks.length) * 10000) / 10000 : 0,
        revenue_anomaly_count: revenueAnomalyCount,
        first_task_at: sortedTaskDates[0]?.toISOString() || null,
        last_task_at: sortedTaskDates.at(-1)?.toISOString() || null,
      },
      period: {
        days,
        start_date: periodStart.toISOString().slice(0, 10),
        end_date: utcToday.toISOString().slice(0, 10),
        total: periodTasks.length,
        success: periodSuccessful.length,
        failed: periodFailed.length,
        in_progress: Math.max(0, periodTasks.length - periodSuccessful.length - periodFailed.length),
        success_rate: periodSuccessful.length + periodFailed.length
          ? Math.round((periodSuccessful.length / (periodSuccessful.length + periodFailed.length) * 100) * 10) / 10
          : 0,
        revenue: Math.round(periodRevenue * 10000) / 10000,
      },
      history: Object.values(historyMap),
      tenant_rankings: tenantRankings,
    };
  }

  function setHistoricalDashboardLoading(isLoading) {
    const periodSelect = document.getElementById('history-period-days');
    if (periodSelect) periodSelect.disabled = isLoading;
    const panel = document.getElementById('dashboard-panel-history');
    if (panel) panel.classList.toggle('is-loading', isLoading);
  }

  function renderHistoricalSummary(data) {
    const lifetime = data.lifetime || {};
    const period = data.period || {};
    const periodSelect = document.getElementById('history-period-days');
    const periodLabel = periodSelect?.selectedOptions?.[0]?.textContent || `最近 ${period.days || 90} 天`;

    document.getElementById('history-total').textContent = Number(lifetime.total || 0).toLocaleString('zh-CN');
    document.getElementById('history-success-rate').textContent = `${Number(lifetime.success_rate || 0).toFixed(1)}%`;
    document.getElementById('history-success-count').textContent = `${Number(lifetime.success || 0).toLocaleString('zh-CN')} 成功`;
    document.getElementById('history-failed-count').textContent = `${Number(lifetime.failed || 0).toLocaleString('zh-CN')} 失败`;
    document.getElementById('history-revenue').textContent = formatDashboardCurrency(lifetime.revenue);
    document.getElementById('history-avg-revenue').textContent = formatDashboardCurrency(lifetime.avg_revenue_per_success);
    const revenueQualityBadge = document.getElementById('history-revenue-quality');
    const revenueAnomalyCount = Number(lifetime.revenue_anomaly_count || 0);
    revenueQualityBadge.textContent = revenueAnomalyCount > 0 ? `${revenueAnomalyCount} 条异常已排除` : '成功扣费';
    revenueQualityBadge.classList.toggle('warning', revenueAnomalyCount > 0);
    document.getElementById('history-avg-duration').textContent = formatDuration(lifetime.avg_duration_ms);
    document.getElementById('history-in-progress').textContent = `${Number(lifetime.in_progress || 0).toLocaleString('zh-CN')} 封`;
    document.getElementById('history-date-range').textContent = formatHistoricalDateRange(lifetime.first_task_at, lifetime.last_task_at);

    document.getElementById('history-period-label').textContent = periodLabel;
    document.getElementById('history-period-total').textContent = Number(period.total || 0).toLocaleString('zh-CN');
    document.getElementById('history-period-success-rate').textContent = `${Number(period.success_rate || 0).toFixed(1)}%`;
    document.getElementById('history-period-revenue').textContent = formatDashboardCurrency(period.revenue);
  }

  function renderHistoricalCharts(history) {
    const labels = history.map((item) => item.date.slice(5));
    const totals = history.map((item) => Number(item.total || 0));
    const successes = history.map((item) => Number(item.success || 0));
    const successRates = history.map((item) => Number(item.success_rate || 0));
    const revenues = history.map((item) => Number(item.revenue || 0));
    const styles = getComputedStyle(document.documentElement);
    const primary = styles.getPropertyValue('--accent-primary').trim();
    const success = styles.getPropertyValue('--success').trim();
    const warning = styles.getPropertyValue('--warning').trim();
    const textSecondary = styles.getPropertyValue('--text-secondary').trim();
    const textMuted = styles.getPropertyValue('--text-muted').trim();
    const gridColor = styles.getPropertyValue('--border-subtle').trim();

    const volumeCanvas = document.getElementById('chart-history-volume');
    if (historyVolumeChart) historyVolumeChart.destroy();
    historyVolumeChart = new Chart(volumeCanvas.getContext('2d'), {
      data: {
        labels,
        datasets: [
          {
            type: 'bar',
            label: '邮件总量',
            data: totals,
            backgroundColor: primary,
            borderRadius: 4,
            order: 3,
          },
          {
            type: 'line',
            label: '成功抽取',
            data: successes,
            borderColor: success,
            backgroundColor: success,
            pointRadius: 1.5,
            borderWidth: 2,
            tension: 0.3,
            order: 2,
          },
          {
            type: 'line',
            label: '成功率 (%)',
            data: successRates,
            borderColor: warning,
            backgroundColor: warning,
            borderDash: [5, 4],
            pointRadius: 0,
            borderWidth: 2,
            tension: 0.3,
            yAxisID: 'yRate',
            order: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { labels: { color: textSecondary, usePointStyle: true } } },
        scales: {
          x: { ticks: { color: textMuted, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, grid: { color: gridColor } },
          y: { beginAtZero: true, ticks: { color: textMuted, precision: 0 }, grid: { color: gridColor } },
          yRate: { beginAtZero: true, max: 100, position: 'right', ticks: { color: warning, callback: (value) => `${value}%` }, grid: { drawOnChartArea: false } },
        },
      },
    });

    const revenueCanvas = document.getElementById('chart-history-revenue');
    if (historyRevenueChart) historyRevenueChart.destroy();
    historyRevenueChart = new Chart(revenueCanvas.getContext('2d'), {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: '扣费营收 (元)',
          data: revenues,
          borderColor: warning,
          backgroundColor: warning,
          fill: false,
          pointRadius: 2,
          pointHoverRadius: 5,
          borderWidth: 2,
          tension: 0.3,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { labels: { color: textSecondary, usePointStyle: true } },
          tooltip: { callbacks: { label: (context) => `营收 ${formatDashboardCurrency(context.parsed.y)}` } },
        },
        scales: {
          x: { ticks: { color: textMuted, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, grid: { color: gridColor } },
          y: { beginAtZero: true, ticks: { color: textMuted, callback: (value) => `¥${value}` }, grid: { color: gridColor } },
        },
      },
    });
  }

  function renderHistoricalTenantRankings(rankings) {
    const tbody = document.querySelector('#table-history-tenants tbody');
    if (!tbody) return;
    if (!rankings.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-8">暂无历史租户数据</td></tr>';
      return;
    }

    tbody.innerHTML = rankings.map((tenant, index) => {
      const rate = Number(tenant.success_rate || 0);
      const rateClass = rate >= 99 ? 'badge-success' : rate >= 95 ? 'badge-warning' : 'badge-danger';
      return `
        <tr>
          <td><span class="history-rank ${index < 3 ? 'top-three' : ''}">${index + 1}</span></td>
          <td><strong>${escapeHtml(tenant.tenant_name)}</strong><div class="text-muted font-mono" style="font-size:0.72rem; margin-top:3px;">${escapeHtml(tenant.tenant_id)}</div></td>
          <td class="font-mono"><strong>${Number(tenant.total || 0).toLocaleString('zh-CN')}</strong></td>
          <td><span class="text-success">${Number(tenant.success || 0).toLocaleString('zh-CN')}</span> / <span class="text-danger">${Number(tenant.failed || 0).toLocaleString('zh-CN')}</span></td>
          <td><span class="badge ${rateClass}">${rate.toFixed(1)}%</span></td>
          <td class="font-mono"><strong>${formatDashboardCurrency(tenant.revenue)}</strong></td>
        </tr>`;
    }).join('');
  }

  function formatDashboardCurrency(value) {
    const amount = Number(value || 0);
    if (!Number.isFinite(amount)) return '金额异常';
    return `¥${amount.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }

  function formatDuration(value) {
    const milliseconds = Number(value || 0);
    if (!Number.isFinite(milliseconds) || milliseconds < 0) return '--';
    if (milliseconds >= 1000) return `${(milliseconds / 1000).toFixed(2)} s`;
    return `${Math.round(milliseconds)} ms`;
  }

  function formatHistoricalDateRange(firstTaskAt, lastTaskAt) {
    if (!firstTaskAt || !lastTaskAt) return '尚无历史任务';
    const formatDate = (value) => new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(new Date(value));
    return `${formatDate(firstTaskAt)} 至 ${formatDate(lastTaskAt)}`;
  }

  // -------------------------------------------------------------
  // 2. Tenants & Keys Logic
  // -------------------------------------------------------------
  // 2. Tenants & Keys Management Logic
  // -------------------------------------------------------------
  async function loadTenantsTable() {
    const tbody = document.querySelector('#table-tenants tbody');
    try {
      const res = await adminFetch('/admin/tenants');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      currentTenants = await res.json();

      if (currentTenants.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-8">暂无租户，点击右上角开通新租户</td></tr>';
        return;
      }

      tbody.innerHTML = currentTenants
        .map((t) => {
          const keysSummary = (t.api_keys || []).length > 0
            ? (t.api_keys || [])
                .map((k) => `<button type="button" class="badge badge-info mr-1 btn-view-keys" data-id="${t.id}" data-name="${escapeHtml(t.name)}" style="border:none; cursor:pointer;" title="点击查看/管理此 Key">${escapeHtml(k.key_prefix)}...</button>`)
                .join(' ')
            : `<button type="button" class="btn btn-xs btn-subtle btn-view-keys" data-id="${t.id}" data-name="${escapeHtml(t.name)}">+ 生成 Key</button>`;

          const statusBadge = t.is_active
            ? '<span class="badge badge-success">正常</span>'
            : '<span class="badge badge-warning">待审核</span>';

          const statusBtn = t.is_active
            ? ''
            : `<button class="btn btn-xs btn-success btn-toggle-status" data-id="${t.id}" data-active="true" title="审核通过并开通租户">审核开通</button>`;

          return `
          <tr>
            <td>
              <strong>${escapeHtml(t.name)}</strong>
              <div class="text-muted font-mono" style="font-size:0.75rem;">${t.id}</div>
            </td>
            <td><strong class="text-success">¥${(parseFloat(t.balance) - parseFloat(t.reserved_balance || 0)).toFixed(2)}</strong></td>
            <td>¥${parseFloat(t.unit_price).toFixed(2)} / 次</td>
            <td>${t.max_concurrency} 并发</td>
            <td>${keysSummary}</td>
            <td>${statusBadge}</td>
            <td class="text-muted">${formatDate(t.created_at)}</td>
            <td>
              <div style="display:flex; gap:6px; flex-wrap:wrap;">
                <button class="btn btn-xs btn-primary btn-recharge-tenant" data-id="${t.id}" data-name="${escapeHtml(t.name)}">充值</button>
                <button class="btn btn-xs btn-secondary btn-view-keys" data-id="${t.id}" data-name="${escapeHtml(t.name)}">🔑 密钥</button>
                <button class="btn btn-xs btn-secondary btn-edit-tenant" data-id="${t.id}" data-name="${escapeHtml(t.name)}" data-phone="${escapeHtml(t.contact_phone || '')}" data-price="${t.unit_price}" data-concurrency="${t.max_concurrency}" data-active="${t.is_active}">修改配置</button>
                ${statusBtn}
              </div>
            </td>
          </tr>
        `;
        })
        .join('');

      // Wire View / Manage Keys Buttons
      document.querySelectorAll('.btn-view-keys').forEach((btn) => {
        btn.addEventListener('click', () => {
          openManageKeysModal(btn.dataset.id, btn.dataset.name);
        });
      });

      // Wire Recharge Buttons
      document.querySelectorAll('.btn-recharge-tenant').forEach((btn) => {
        btn.addEventListener('click', () => {
          document.getElementById('rc-tenant-id').value = btn.dataset.id;
          document.getElementById('rc-tenant-name').value = `${btn.dataset.name} (${btn.dataset.id})`;
          document.getElementById('rc-amount').value = '100.00';
          openModal('modal-recharge');
        });
      });


      // Wire Edit Config Buttons
      document.querySelectorAll('.btn-edit-tenant').forEach((btn) => {
        btn.addEventListener('click', () => {
          document.getElementById('edit-tenant-id').value = btn.dataset.id;
          document.getElementById('edit-tenant-name').value = btn.dataset.name;
          document.getElementById('edit-tenant-phone').value = btn.dataset.phone;
          document.getElementById('edit-tenant-unit-price').value = parseFloat(btn.dataset.price).toFixed(2);
          document.getElementById('edit-tenant-concurrency').value = btn.dataset.concurrency;
          document.getElementById('edit-tenant-status').value = btn.dataset.active;
          openModal('modal-edit-tenant');
        });
      });

      // Wire Toggle Status (Audit Approve) Buttons
      document.querySelectorAll('.btn-toggle-status').forEach((btn) => {
        btn.addEventListener('click', async () => {
          const tenantId = btn.dataset.id;
          const targetActive = btn.dataset.active === 'true';
          const actionWord = targetActive ? '审核通过并开通' : '设为待审核';

          const confirmed = await showConfirmModal({
            title: `${actionWord}确认`,
            message: `确定要将该租户【${actionWord}】吗？\n审核开通后租户即可正常登录并调用 API 抽取服务。`,
            iconType: 'success',
            confirmText: '确认开通',
          });
          if (!confirmed) return;

          try {
            const res = await adminFetch(`/admin/tenants/${tenantId}/status?is_active=${targetActive}`, {
              method: 'PUT',
            });
            if (!res.ok) throw new Error('修改状态失败');
            const data = await res.json();
            showToast('success', data.message || '租户审核已通过并开通！');
            loadTenantsTable();
          } catch (err) {
            showToast('error', `操作失败: ${err.message}`);
          }
        });
      });

    } catch (err) {
      console.error('Failed to load tenants:', err);
      tbody.innerHTML = `<tr><td colspan="8" class="text-center text-danger py-8">加载失败: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  // Submit Edit Tenant Config
  document.getElementById('btn-submit-edit-tenant')?.addEventListener('click', async () => {
    const tenantId = document.getElementById('edit-tenant-id').value;
    const name = document.getElementById('edit-tenant-name').value.trim();
    const phone = document.getElementById('edit-tenant-phone').value.trim();
    const price = parseFloat(document.getElementById('edit-tenant-unit-price').value);
    const concurrency = Number(document.getElementById('edit-tenant-concurrency').value);
    const isActive = document.getElementById('edit-tenant-status').value === 'true';

    if (!name) return showToast('warning', '请输入企业名称');
    if (!Number.isFinite(price) || price < MIN_UNIT_PRICE || price > MAX_UNIT_PRICE) {
      return showToast('warning', '单次调用单价须在 ¥0.01 至 ¥100.00 之间');
    }
    if (
      !Number.isInteger(concurrency)
      || concurrency < MIN_TENANT_CONCURRENCY
      || concurrency > MAX_TENANT_CONCURRENCY
    ) {
      return showToast('warning', '租户最大并发须为 1 至 30 之间的整数');
    }

    try {
      const res = await adminFetch(`/admin/tenants/${tenantId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name,
          contact_phone: phone,
          unit_price: price,
          max_concurrency: concurrency,
          is_active: isActive,
        }),
      });
      if (!res.ok) throw new Error('保存租户配置失败');
      closeModal('modal-edit-tenant');
      showToast('success', '租户配置与并发单价已成功更新！');
      loadTenantsTable();
    } catch (err) {
      showToast('error', `保存失败: ${err.message}`);
    }
  });

  // Open Create Tenant Modal
  document.getElementById('btn-open-create-tenant').addEventListener('click', () => {
    openModal('modal-create-tenant');
  });

  // Submit Create Tenant
  document.getElementById('btn-submit-create-tenant').addEventListener('click', async () => {
    const name = document.getElementById('ct-name').value.trim();
    if (!name) return showToast('warning', '请输入企业/租户名称');


    const email = document.getElementById('ct-email').value.trim();
    const phone = document.getElementById('ct-phone').value.trim();
    const priceInput = document.getElementById('ct-price').value.trim();
    const balanceInput = document.getElementById('ct-balance').value.trim();
    const price = priceInput === '' ? 0.5 : Number(priceInput);
    const balance = balanceInput === '' ? 100.0 : Number(balanceInput);
    const concurrencyInput = document.getElementById('ct-concurrency').value.trim();
    const concurrency = concurrencyInput === '' ? 20 : Number(concurrencyInput);

    if (!Number.isFinite(price) || price < MIN_UNIT_PRICE || price > MAX_UNIT_PRICE) {
      return showToast('warning', '单次调用单价须在 ¥0.01 至 ¥100.00 之间');
    }
    if (!Number.isFinite(balance) || balance < 0 || balance > MAX_RECHARGE_AMOUNT) {
      return showToast('warning', '初始充值额度须在 ¥0.00 至 ¥1,000,000.00 之间');
    }
    if (
      !Number.isInteger(concurrency)
      || concurrency < MIN_TENANT_CONCURRENCY
      || concurrency > MAX_TENANT_CONCURRENCY
    ) {
      return showToast('warning', '租户最大并发须为 1 至 30 之间的整数');
    }

    try {
      const res = await adminFetch('/admin/tenants', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          contact_email: email,
          contact_phone: phone,
          unit_price: price,
          initial_balance: balance,
          max_concurrency: concurrency,
        }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail?.message || '创建失败');
      }
      const tenantData = await res.json();
      closeModal('modal-create-tenant');

      // Show generated Key
      const keyObj = tenantData.api_keys?.[0];
      if (keyObj && keyObj.raw_api_key) {
        document.getElementById('show-raw-key').value = keyObj.raw_api_key;
        document.getElementById('show-api-secret').value = keyObj.api_secret || '';
        openModal('modal-show-key');
      }

      loadTenantsTable();
    } catch (err) {
      showToast('error', `创建失败: ${err.message}`);
    }
  });

  // Submit Recharge
  document.getElementById('btn-submit-recharge')?.addEventListener('click', async () => {
    const submitButton = document.getElementById('btn-submit-recharge');
    const tenantId = document.getElementById('rc-tenant-id').value;
    const amount = parseFloat(document.getElementById('rc-amount').value);
    const desc = document.getElementById('rc-desc').value.trim();

    if (!Number.isFinite(amount) || amount < MIN_RECHARGE_AMOUNT || amount > MAX_RECHARGE_AMOUNT) {
      return showToast('warning', '单笔充值额度须在 ¥0.01 至 ¥1,000,000.00 之间');
    }

    try {
      submitButton.disabled = true;
      const res = await adminFetch(`/admin/recharge/${tenantId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          amount: amount,
          description: desc,
          operator: 'ADMIN',
        }),
      });
      if (!res.ok) {
        const errorBody = await res.json().catch(() => ({}));
        throw new Error(errorBody.detail?.message || '充值请求失败');
      }
      const transaction = await res.json();
      const balanceBefore = Number(transaction.balance_before);
      const balanceAfter = Number(transaction.balance_after);
      const creditedAmount = balanceAfter - balanceBefore;
      if (
        !Number.isFinite(balanceBefore)
        || !Number.isFinite(balanceAfter)
        || Math.abs(creditedAmount - amount) > 0.0001
      ) {
        throw new Error('账务核验失败：充值流水已返回，但账户余额未按充值金额增加');
      }
      closeModal('modal-recharge');
      showToast('success', `充值成功，当前余额 ¥${balanceAfter.toFixed(2)}`);
      await loadTenantsTable();
    } catch (err) {
      showToast('error', `充值失败: ${err.message}`);
    } finally {
      submitButton.disabled = false;
    }
  });


  // Copy helpers
  document.getElementById('btn-copy-raw-key')?.addEventListener('click', () => {
    copyToClipboard(document.getElementById('show-raw-key').value, 'API Key 密文已复制！');
  });
  document.getElementById('btn-copy-secret')?.addEventListener('click', () => {
    copyToClipboard(document.getElementById('show-api-secret').value, 'Webhook Secret 已复制！');
  });

  // -------------------------------------------------------------
  // API Key Management Modal Logic
  // -------------------------------------------------------------
  let activeKeyTenantId = null;

  async function openManageKeysModal(tenantId, tenantName) {
    activeKeyTenantId = tenantId;
    document.getElementById('keys-tenant-name').textContent = tenantName || tenantId;
    document.getElementById('keys-tenant-id').textContent = `(${tenantId})`;
    document.getElementById('new-key-alert').style.display = 'none';

    // cURL Snippet
    const curlSnippet = `curl -X POST "http://localhost:8000/api/v1/extract/sync" \\
  -H "Authorization: Bearer <YOUR_API_KEY>" \\
  -H "X-Tenant-ID: ${tenantId}" \\
  -H "Content-Type: application/json" \\
  -d '{"mail_subject": "Booking Ref...", "mail_body": "Freight Prepaid", "attachments": []}'`;
    document.getElementById('keys-curl-snippet').textContent = curlSnippet;

    openModal('modal-manage-keys');
    await loadTenantKeysList(tenantId);
  }

  async function loadTenantKeysList(tenantId) {
    const tbody = document.querySelector('#table-tenant-keys tbody');
    tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">正在查询密钥列表...</td></tr>';

    try {
      const res = await adminFetch(`/admin/tenants/${tenantId}/keys`);
      if (!res.ok) throw new Error('查询密钥失败');
      const keys = await res.json();

      if (keys.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">该租户暂无 API Key，请点击右上角「生成新 API Key」</td></tr>';
        return;
      }

      tbody.innerHTML = keys.map(k => {
        const fullKey = [k.raw_key, k.raw_api_key]
          .find(value => typeof value === 'string' && value.length > 20) || '';
        const hasFullKey = Boolean(fullKey);
        const displayKey = k.key_prefix ? `${k.key_prefix}...` : (fullKey ? `${fullKey.substring(0, 11)}...` : '-');

        let keyCopyBtn = '';
        if (hasFullKey) {
          keyCopyBtn = `<button type="button" class="btn btn-xs btn-primary btn-copy-key-val" data-key="${escapeHtml(fullKey)}" title="复制完整 55 位 API Key">复制 Key</button>`;
        } else {
          keyCopyBtn = `<button type="button" class="btn btn-xs btn-outline-warning btn-legacy-key-hint" title="历史旧密钥仅单向哈希加密存储，无法还原明文。建议生成新 Key 使用">旧版单向加密</button>`;
        }

        return `
        <tr>
          <td style="white-space:nowrap;"><strong>${escapeHtml(k.name || '默认密钥')}</strong></td>
          <td style="white-space:nowrap;">
            <div style="display:inline-flex; align-items:center; gap:8px;">
              <code style="color:#38bdf8; font-size:0.82rem; font-family:var(--font-mono);">${escapeHtml(displayKey)}</code>
              ${keyCopyBtn}
            </div>
          </td>
          <td style="white-space:nowrap;">
            <div style="display:inline-flex; align-items:center; gap:8px;">
              <span style="font-family:var(--font-mono); font-size:0.8rem; color:#fbbf24;">${escapeHtml(k.api_secret ? k.api_secret.substring(0, 10) + '...' : '-')}</span>
              ${k.api_secret ? `<button type="button" class="btn btn-xs btn-secondary btn-copy-secret-val" data-secret="${escapeHtml(k.api_secret)}" title="复制完整 Secret">复制 Secret</button>` : ''}
            </div>
          </td>
          <td style="white-space:nowrap; text-align:center;">${k.is_active ? '<span class="badge badge-success">正常</span>' : '<span class="badge badge-danger">已吊销</span>'}</td>
          <td class="text-muted" style="font-size:0.75rem; white-space:nowrap;">${formatDate(k.created_at)}</td>
          <td style="white-space:nowrap; text-align:center;">
            <button type="button" class="btn btn-xs btn-danger btn-revoke-key" data-id="${k.id}" title="吊销此密钥">吊销</button>
          </td>
        </tr>
      `;
      }).join('');

      // Wire copy buttons
      document.querySelectorAll('.btn-copy-key-val').forEach(btn => {
        btn.addEventListener('click', () => {
          const keyVal = btn.dataset.key || '';
          if (keyVal.length > 20) {
            copyToClipboard(keyVal, '完整 API Key 已成功复制到剪贴板！');
          } else {
            showToast('warning', '该 Key 为历史加密版本，未留存明文，请点击右上角生成新 Key 使用！');
          }
        });
      });

      document.querySelectorAll('.btn-legacy-key-hint').forEach(btn => {
        btn.addEventListener('click', () => {
          showToast('warning', '该 Key 为历史版本创建（仅哈希存储），无法反解明文。请点击右上角「生成新 API Key」即可获得完整明文密钥并随时复制！');
        });
      });

      document.querySelectorAll('.btn-copy-secret-val').forEach(btn => {
        btn.addEventListener('click', () => {
          const secret = btn.dataset.secret || '';
          copyToClipboard(secret, 'Webhook Secret 已成功复制！');
        });
      });

      document.querySelectorAll('.btn-revoke-key').forEach(btn => {
        btn.addEventListener('click', async () => {
          const confirmed = await showConfirmModal({
            title: '吊销 API 密钥确认',
            message: '确定要吊销该 API Key 吗？\n吊销后使用此 Key 的外部集成系统将立即无法继续调用！',
            iconType: 'danger',
            confirmText: '确认吊销',
            isDanger: true,
          });
          if (!confirmed) return;

          try {
            const delRes = await adminFetch(`/admin/tenants/keys/${btn.dataset.id}`, { method: 'DELETE' });
            if (!delRes.ok) throw new Error('吊销失败');
            showToast('success', 'API Key 已成功吊销！');
            loadTenantKeysList(tenantId);
            loadTenantsTable();
          } catch (delErr) {
            showToast('error', `吊销失败: ${delErr.message}`);
          }
        });
      });
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="6" class="text-center text-danger py-4">加载失败: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  // Create new key inside modal
  document.getElementById('btn-create-new-key')?.addEventListener('click', async () => {
    if (!activeKeyTenantId) return;
    const keyName = await showPromptModal({
      title: '🔑 生成新 API Key',
      label: '请输入新 API Key 的名称 / 用途说明:',
      placeholder: '例如: ERP 自动化对接',
      defaultValue: '生产 API Key',
      confirmText: '立即生成',
    });
    if (keyName === null || !keyName.trim()) return;

    try {
      const res = await adminFetch(`/admin/tenants/${activeKeyTenantId}/keys?key_name=${encodeURIComponent(keyName.trim())}`, {
        method: 'POST',
      });
      if (!res.ok) throw new Error('生成新密钥失败');
      const data = await res.json();

      document.getElementById('new-key-alert').style.display = 'block';
      document.getElementById('new-key-full-val').value = data.raw_api_key || '';
      document.getElementById('new-key-secret-val').textContent = data.api_secret || '';

      showToast('success', '新 API Key 生成成功，请及时复制保存！');
      await loadTenantKeysList(activeKeyTenantId);
      loadTenantsTable();
    } catch (err) {
      showToast('error', `生成失败: ${err.message}`);
    }
  });



  // -------------------------------------------------------------
  // 3. Tasks Monitor Logic (with Complete Pagination)
  // -------------------------------------------------------------
  let tasksCurrentPage = 1;
  let tasksPageSize = 20;
  let tasksTotalPages = 1;

  async function loadTasksTable() {
    const tbody = document.querySelector('#table-tasks tbody');
    const statusFilter = document.getElementById('filter-task-status')?.value;
    const search = document.getElementById('search-task')?.value.trim();

    let url = `/admin/tasks?page=${tasksCurrentPage}&page_size=${tasksPageSize}`;
    if (statusFilter) url += `&status=${statusFilter}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;

    try {
      const res = await adminFetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      tasksTotalPages = Math.max(1, Math.ceil((data.total || 0) / tasksPageSize));
      if (document.getElementById('task-pagination-info')) document.getElementById('task-pagination-info').textContent = `共 ${data.total} 条记录`;
      if (document.getElementById('tasks-total-count')) document.getElementById('tasks-total-count').textContent = data.total;
      if (document.getElementById('tasks-curr-page')) document.getElementById('tasks-curr-page').textContent = tasksCurrentPage;
      if (document.getElementById('tasks-total-pages')) document.getElementById('tasks-total-pages').textContent = tasksTotalPages;

      if (document.getElementById('tasks-btn-prev')) document.getElementById('tasks-btn-prev').disabled = tasksCurrentPage <= 1;
      if (document.getElementById('tasks-btn-next')) document.getElementById('tasks-btn-next').disabled = tasksCurrentPage >= tasksTotalPages;

      if (!data.items || data.items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted py-8">暂无匹配任务</td></tr>';
        return;
      }

      tbody.innerHTML = data.items
        .map((task) => {
          let statusBadge = '';
          if (task.status === 'SUCCESS') statusBadge = '<span class="badge badge-success">成功</span>';
          else if (task.status === 'FAILED') statusBadge = '<span class="badge badge-danger">失败</span>';
          else if (task.status === 'PROCESSING') statusBadge = '<span class="badge badge-info">处理中</span>';
          else statusBadge = '<span class="badge badge-warning">排队中</span>';

          const chargeText = task.is_charged ? `<strong class="text-danger">-¥${parseFloat(task.charged_amount).toFixed(2)}</strong>` : '<span class="text-muted">¥0.00</span>';
          const durationText = task.duration_ms ? `${task.duration_ms} ms` : '-';

          return `
          <tr>
            <td class="font-mono" style="font-size:0.78rem;"><strong>${task.id}</strong></td>
            <td class="font-mono" style="font-size:0.78rem;">${task.tenant_id}</td>
            <td>
              <div style="max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(task.mail_subject || '')}">
                ${escapeHtml(task.mail_subject || '无主题')}
              </div>
            </td>
            <td>${statusBadge}</td>
            <td>${durationText}</td>
            <td>${chargeText}</td>
            <td><span class="badge badge-muted">${task.callback_status}</span></td>
            <td class="text-muted">${formatDate(task.created_at)}</td>
            <td>
              <button class="btn btn-sm btn-secondary btn-view-task" data-id="${task.id}">查看详情</button>
            </td>
          </tr>
        `;
        })
        .join('');

      document.querySelectorAll('.btn-view-task').forEach((btn) => {
        btn.addEventListener('click', () => {
          showTaskDetailModal(btn.dataset.id);
        });
      });
    } catch (err) {
      console.error('Failed to load tasks:', err);
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-danger py-8">加载失败: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  document.getElementById('tasks-btn-prev')?.addEventListener('click', () => {
    if (tasksCurrentPage > 1) {
      tasksCurrentPage--;
      loadTasksTable();
    }
  });

  document.getElementById('tasks-btn-next')?.addEventListener('click', () => {
    if (tasksCurrentPage < tasksTotalPages) {
      tasksCurrentPage++;
      loadTasksTable();
    }
  });

  document.getElementById('tasks-page-size')?.addEventListener('change', (e) => {
    tasksPageSize = parseInt(e.target.value) || 20;
    tasksCurrentPage = 1;
    loadTasksTable();
  });

  document.getElementById('filter-task-status')?.addEventListener('change', () => {
    tasksCurrentPage = 1;
    loadTasksTable();
  });

  document.getElementById('search-task')?.addEventListener('input', debounce(() => {
    tasksCurrentPage = 1;
    loadTasksTable();
  }, 400));

  function taskStatusBadge(status) {
    const labels = {
      PENDING: ['待处理', 'badge-warning'],
      PROCESSING: ['处理中', 'badge-info'],
      SUCCESS: ['成功', 'badge-success'],
      FAILED: ['失败', 'badge-danger'],
    };
    const [label, className] = labels[status] || [status || '未知', 'badge-muted'];
    return `<span class="badge ${className}">${escapeHtml(label)}</span>`;
  }

  function taskMoney(value) {
    const amount = Number(value || 0);
    return `¥${Number.isFinite(amount) ? amount.toFixed(2) : '0.00'}`;
  }

  function formatTaskTime(value) {
    return value ? formatDate(value) : '-';
  }

  async function showTaskDetailModal(taskId, feedbackId = null) {
    try {
      const res = await adminFetch(`/admin/tasks/${encodeURIComponent(taskId)}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const task = await res.json();

      document.getElementById('td-title').textContent = task.mail_subject || '任务详情';
      document.getElementById('td-subtitle').textContent = `关联任务 · ${task.id}`;
      document.getElementById('td-id').textContent = task.id;
      document.getElementById('td-tenant').textContent = task.tenant_id;
      document.getElementById('td-status-badge').innerHTML = taskStatusBadge(task.status);
      document.getElementById('td-input-type').textContent = task.input_type || '未知输入类型';
      document.getElementById('td-created').textContent = formatTaskTime(task.created_at);
      document.getElementById('td-started').textContent = formatTaskTime(task.started_at);
      document.getElementById('td-completed').textContent = formatTaskTime(task.completed_at);
      document.getElementById('td-duration').textContent = task.duration_ms ? `${task.duration_ms} ms` : '-';
      document.getElementById('td-billing').textContent = task.is_charged
        ? `已扣 ${taskMoney(task.charged_amount)}`
        : '尚未扣费';
      document.getElementById('td-reservation').textContent = task.is_reserved
        ? `已预留 ${taskMoney(task.reserved_amount)}`
        : '无预留资金';
      document.getElementById('td-webhook-status').innerHTML = taskStatusBadge(task.callback_status || 'NONE');
      document.getElementById('td-webhook-url').textContent = task.callback_url || '未配置';
      document.getElementById('td-subject').textContent = task.mail_subject || '无主题';
      document.getElementById('td-input-summary').textContent = task.input_summary || '暂无输入摘要';

      const attachmentList = document.getElementById('td-attachments');
      attachmentList.replaceChildren();
      const attachmentNames = Array.isArray(task.attachment_names) ? task.attachment_names : [];
      if (attachmentNames.length === 0) {
        attachmentList.textContent = '无附件';
      } else {
        attachmentNames.forEach((name) => {
          const chip = document.createElement('span');
          chip.textContent = name;
          chip.title = name;
          attachmentList.appendChild(chip);
        });
      }

      const taskFeedback = task.feedback;
      const feedbackSummary = document.getElementById('td-feedback-summary');
      const feedbackMeta = document.getElementById('td-feedback-meta');
      const feedbackButton = document.getElementById('td-btn-open-feedback');
      if (taskFeedback) {
        const feedbackLabel = {
          PENDING: '待审核',
          ACCEPTED: taskFeedback.is_refunded ? '已采纳 / 已退款' : '已采纳',
          RESOLVED: '已修复发布',
          REJECTED: '已驳回',
        }[taskFeedback.status] || taskFeedback.status;
        feedbackSummary.textContent = feedbackLabel;
        const refundText = taskFeedback.is_refunded
          ? `，已退款 ${taskMoney(taskFeedback.refund_amount)}`
          : '';
        feedbackMeta.textContent = `${taskFeedback.diff_fields_count || 0} 处字段差异${refundText}`;
        feedbackButton.style.display = '';
        feedbackButton.onclick = () => {
          closeModal('modal-task-detail');
          window.openFeedbackDiffModal(feedbackId || taskFeedback.id);
        };
      } else {
        feedbackSummary.textContent = '暂无反馈';
        feedbackMeta.textContent = '该任务尚未关联纠错工单';
        feedbackButton.style.display = 'none';
        feedbackButton.onclick = null;
      }

      const codeElem = document.getElementById('td-json-code');
      const outputLabel = document.getElementById('td-output-label');
      if (task.result_json) {
        codeElem.textContent = JSON.stringify(task.result_json, null, 2);
        outputLabel.textContent = 'Cargo V3 结构化结果';
      } else if (task.error_message) {
        codeElem.textContent = `// 任务失败错误日志:\n${task.error_message}`;
        outputLabel.textContent = '失败原因';
      } else {
        codeElem.textContent = '// 任务处理中或无输出结果...';
        outputLabel.textContent = '暂无结果';
      }

      openModal('modal-task-detail');
    } catch (err) {
      showToast('error', `获取详情失败: ${err.message}`);
    }
  }

  const taskDetailCopyButton = document.getElementById('td-btn-copy-json');
  if (taskDetailCopyButton) {
    taskDetailCopyButton.addEventListener('click', () => {
      copyToClipboard(document.getElementById('td-json-code')?.textContent || '');
    });
  }

  window.viewTaskDetailAdmin = function(taskId, feedbackId) {
    return showTaskDetailModal(taskId, feedbackId);
  };

  // -------------------------------------------------------------
  // 4. Billing Logs Logic (with Real Admin API & Pagination)
  // -------------------------------------------------------------
  let billingCurrentPage = 1;
  let billingPageSize = 20;
  let billingTotalPages = 1;

  async function loadBillingTable() {
    const tbody = document.querySelector('#table-billing tbody');
    const typeFilter = document.getElementById('filter-billing-type')?.value;
    const search = document.getElementById('search-billing')?.value.trim();

    let url = `/admin/billing/transactions?page=${billingCurrentPage}&page_size=${billingPageSize}`;
    if (typeFilter) url += `&type=${typeFilter}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;

    try {
      const res = await adminFetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      billingTotalPages = data.total_pages || Math.max(1, Math.ceil((data.total || 0) / billingPageSize));
      if (document.getElementById('billing-count-info')) document.getElementById('billing-count-info').textContent = `共 ${data.total} 条明细`;
      if (document.getElementById('billing-total-count')) document.getElementById('billing-total-count').textContent = data.total;
      if (document.getElementById('billing-curr-page')) document.getElementById('billing-curr-page').textContent = billingCurrentPage;
      if (document.getElementById('billing-total-pages')) document.getElementById('billing-total-pages').textContent = billingTotalPages;

      if (document.getElementById('billing-btn-prev')) document.getElementById('billing-btn-prev').disabled = billingCurrentPage <= 1;
      if (document.getElementById('billing-btn-next')) document.getElementById('billing-btn-next').disabled = billingCurrentPage >= billingTotalPages;

      if (!data.items || data.items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted py-8">暂无流水明细记录</td></tr>';
        return;
      }

      tbody.innerHTML = data.items
        .map((tx) => {
          const isDeduction = tx.type === 'DEDUCTION';
          const typeBadge = isDeduction
            ? '<span class="badge badge-danger">API 扣费</span>'
            : '<span class="badge badge-success">账户充值</span>';
          const amountText = isDeduction
            ? `<strong class="text-danger">-¥${parseFloat(tx.amount).toFixed(2)}</strong>`
            : `<strong class="text-success">+¥${parseFloat(tx.amount).toFixed(2)}</strong>`;

          return `
          <tr>
            <td class="font-mono" style="font-size:0.78rem;">${escapeHtml(tx.id)}</td>
            <td class="font-mono" style="font-size:0.78rem;">${escapeHtml(tx.tenant_id)}</td>
            <td>${typeBadge}</td>
            <td>${amountText}</td>
            <td>¥${parseFloat(tx.balance_before || 0).toFixed(2)}</td>
            <td><strong>¥${parseFloat(tx.balance_after || 0).toFixed(2)}</strong></td>
            <td>
              <div style="max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(tx.description || tx.task_id || '')}">
                ${escapeHtml(tx.description || tx.task_id || '-')}
              </div>
            </td>
            <td>${escapeHtml(tx.operator || 'SYSTEM')}</td>
            <td class="text-muted">${formatDate(tx.created_at)}</td>
          </tr>
        `;
        })
        .join('');
    } catch (err) {
      console.error('Failed to load billing table:', err);
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-danger py-8">加载失败: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  document.getElementById('billing-btn-prev')?.addEventListener('click', () => {
    if (billingCurrentPage > 1) {
      billingCurrentPage--;
      loadBillingTable();
    }
  });

  document.getElementById('billing-btn-next')?.addEventListener('click', () => {
    if (billingCurrentPage < billingTotalPages) {
      billingCurrentPage++;
      loadBillingTable();
    }
  });

  document.getElementById('billing-page-size')?.addEventListener('change', (e) => {
    billingPageSize = parseInt(e.target.value) || 20;
    billingCurrentPage = 1;
    loadBillingTable();
  });

  document.getElementById('filter-billing-type')?.addEventListener('change', () => {
    billingCurrentPage = 1;
    loadBillingTable();
  });

  document.getElementById('search-billing')?.addEventListener('input', debounce(() => {
    billingCurrentPage = 1;
    loadBillingTable();
  }, 400));


  // -------------------------------------------------------------
  // 5. Workbench Online Testing Logic
  // -------------------------------------------------------------
  function getAvailableTenantBalance(tenant) {
    return parseFloat(tenant.balance || 0) - parseFloat(tenant.reserved_balance || 0);
  }

  function updateBenchmarkTenantSummary() {
    const select = document.getElementById('bench-tenant-select');
    const summary = document.getElementById('bench-tenant-summary');
    const concurrencySelect = document.getElementById('bench-concurrency-limit');
    if (!select || !summary) return;

    const tenant = currentTenants.find((item) => item.id === select.value);
    if (!tenant) {
      summary.textContent = '当前没有可用于压测的租户';
      return;
    }

    const availableBalance = getAvailableTenantBalance(tenant);
    const tenantConcurrency = Math.max(
      MIN_TENANT_CONCURRENCY,
      Math.min(Number(tenant.max_concurrency) || MIN_TENANT_CONCURRENCY, MAX_TENANT_CONCURRENCY),
    );
    if (concurrencySelect) {
      Array.from(concurrencySelect.options).forEach((option) => {
        option.disabled = Number(option.value) > tenantConcurrency;
      });
      const selectedOption = concurrencySelect.options[concurrencySelect.selectedIndex];
      if (!selectedOption || selectedOption.disabled) {
        const allowedOptions = Array.from(concurrencySelect.options).filter((option) => !option.disabled);
        concurrencySelect.value = allowedOptions.at(-1)?.value || String(MIN_TENANT_CONCURRENCY);
      }
    }
    summary.textContent = `任务及成功扣费将归属 ${tenant.name}（${tenant.id}）；当前可用余额 ¥${availableBalance.toFixed(2)}，单价 ¥${parseFloat(tenant.unit_price).toFixed(2)} / 次，租户并发上限 ${tenantConcurrency}。`;
  }

  async function populateWorkbenchKeySelect() {
    try {
      const res = await adminFetch('/admin/tenants');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const tenants = await res.json();
      currentTenants = tenants;

      const selText = document.getElementById('wb-select-key');
      const selFile = document.getElementById('wb-select-key-file');
      const selBench = document.getElementById('bench-tenant-select');
      const selects = [selText, selFile, selBench].filter(Boolean);
      const previousValues = new Map(selects.map((select) => [select, select.value]));

      selects.forEach((select) => select.replaceChildren());

      tenants.forEach((tenant) => {
        const availableBalance = getAvailableTenantBalance(tenant);
        const statusNote = tenant.is_active === false ? ' [待审核]' : '';
        const optionLabel = `${tenant.name}（可用 ¥${availableBalance.toFixed(2)}，¥${parseFloat(tenant.unit_price).toFixed(2)} / 次）${statusNote}`;
        selects.forEach((select) => {
          const option = document.createElement('option');
          option.value = tenant.id;
          option.textContent = optionLabel;
          option.disabled = tenant.is_active === false;
          select.appendChild(option);
        });
      });

      selects.forEach((select) => {
        const previousValue = previousValues.get(select);
        const previousOption = Array.from(select.options).find((option) => option.value === previousValue && !option.disabled);
        if (previousOption) {
          select.value = previousValue;
        } else {
          const firstValid = Array.from(select.options).find((option) => !option.disabled);
          if (firstValid) {
            select.value = firstValid.value;
          }
        }
      });
      updateBenchmarkTenantSummary();
    } catch (err) {
      console.error('Failed to populate keys:', err);
    }
  }

  function getSelectedModel() {
    const modelInput = document.getElementById('llm-model');
    if (modelInput && modelInput.value && modelInput.value.trim()) {
      return modelInput.value.trim();
    }
    const badge = document.querySelector('.engine-badge');
    if (badge && badge.textContent) {
      return badge.textContent.trim();
    }
    return 'SenseTime · deepseek-v4-flash';
  }

  // Mode Switch
  const modeTextBtn = document.getElementById('wb-mode-text');
  const modeFileBtn = document.getElementById('wb-mode-file');
  const formText = document.getElementById('wb-form-text');
  const formFile = document.getElementById('wb-form-file');

  modeTextBtn.addEventListener('click', () => {
    modeTextBtn.classList.add('active');
    modeFileBtn.classList.remove('active');
    formText.style.display = 'block';
    formFile.style.display = 'none';
  });

  modeFileBtn.addEventListener('click', () => {
    modeFileBtn.classList.add('active');
    modeTextBtn.classList.remove('active');
    formText.style.display = 'none';
    formFile.style.display = 'block';
  });

  // Fill sample
  document.getElementById('wb-btn-fill-sample').addEventListener('click', () => {
    document.getElementById('wb-mail-subject').value = 'Booking BK123456 - Yantian to Melbourne';
    document.getElementById('wb-mail-body').value = 'Please arrange booking. Freight prepaid.';
    document.getElementById('wb-attachment-text').value = `SHIPPER: ABC TRADING CO., LTD.
ADD: NO.1 ROAD, SHENZHEN, CHINA
TEL: +86 755 12345678
FAX: 0755-88889999
EMAIL: ops@example.com
CONSIGNEE: XYZ IMPORT PTY LTD
100 TEST STREET, MELBOURNE
EMAIL: import@example.com
NOTIFY: SAME AS CONSIGNEE
TEL: +61 3 9000 0000
POL: YANTIAN
POD: MELBOURNE
VESSEL/VOYAGE: KOTA TEST / 001S
ETD: 2023/8/11
BOOKING NO: BK123456
CONTAINER: ABCU1234567 / SEAL123 / 40HQ
GOODS: DAILY NECESSITIES 日用品
HS CODE: 3924900000
PACKAGES: 501 PACKAGES
G.W.: 9,170.000 KGS
MEAS: 68.000 CBM`;
  });

  // Execute Text Run
  const runTextButton = document.getElementById('wb-btn-run-text');
  runTextButton.addEventListener('click', async () => {
    const subject = document.getElementById('wb-mail-subject').value.trim();
    const body = document.getElementById('wb-mail-body').value.trim();
    const attText = document.getElementById('wb-attachment-text').value.trim();
    let selectedTenantId = document.getElementById('wb-select-key')?.value;
    const buttonLabel = runTextButton.querySelector('span') || runTextButton;
    const statusElem = document.getElementById('wb-result-status-text');
    const jsonElem = document.getElementById('wb-json-output');

    if (!body && !attText) return showToast('warning', '请输入邮件正文或附件文本');

    if (!selectedTenantId && currentTenants && currentTenants.length > 0) {
      const activeTenant = currentTenants.find((t) => t.is_active !== false) || currentTenants[0];
      if (activeTenant) {
        selectedTenantId = activeTenant.id;
        const selectElem = document.getElementById('wb-select-key');
        if (selectElem) selectElem.value = activeTenant.id;
      }
    }

    if (!selectedTenantId) {
      return showToast('warning', '请选择承担本次抽取与扣费的租户（若无可用租户请先在租户管理中启用）');
    }

    const activeModel = getSelectedModel();
    runTextButton.disabled = true;
    if (buttonLabel) buttonLabel.textContent = '正在抽取...';
    if (statusElem) statusElem.textContent = '⏳ 大模型抽取与 V3 规则清洗中...';
    if (jsonElem) jsonElem.textContent = `// 正在调用大模型 (${activeModel}) 进行结构化抽取中，请稍候...`;

    const headers = {
      'Content-Type': 'application/json',
      'X-Tenant-ID': selectedTenantId,
    };

    try {
      const res = await adminFetch('/api/v1/extract/sync', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify({
          mail_subject: subject,
          mail_body: body,
          attachments: attText ? [{ filename: 'booking.txt', content_type: 'text/plain', text: attText, tables: [], ocr_text: '' }] : [],
        }),
      });

      if (!res.ok) {
        const errorBody = await res.json().catch(() => ({}));
        throw new Error(errorBody.detail?.message || errorBody.detail?.error || errorBody.message || `抽取失败 (HTTP ${res.status})`);
      }

      const data = await res.json();
      const chargedAmount = parseFloat(data.charged_amount || 0);
      const usedModel = data.model_used || activeModel;
      if (statusElem) {
        statusElem.innerHTML = `✅ 抽取成功 · 模型: <strong>${escapeHtml(usedModel)}</strong> · 耗时: <strong>${data.duration_ms} ms</strong> · 扣费: <strong class="text-danger">¥${chargedAmount.toFixed(2)}</strong>`;
      }
      if (jsonElem) {
        jsonElem.textContent = JSON.stringify(data.data, null, 2);
      }
      showToast('success', `抽取成功 (耗时 ${data.duration_ms} ms)`);
    } catch (err) {
      if (statusElem) {
        statusElem.innerHTML = `<span class="text-danger">❌ 抽取失败: ${escapeHtml(err.message)}</span>`;
      }
      if (jsonElem) {
        jsonElem.textContent = `// 错误详情:\n${err.message}`;
      }
      showToast('error', `抽取失败: ${err.message}`);
    } finally {
      runTextButton.disabled = false;
      if (buttonLabel) buttonLabel.textContent = '执行 V3 结构化抽取';
    }
  });

  // Dropzone file handling
  const dropzone = document.getElementById('wb-dropzone');
  const fileInput = document.getElementById('wb-file-input');
  const selectedFilesContainer = document.getElementById('wb-selected-files');

  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropzone.classList.add('dragover');
  });
  dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
  dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
    handleFiles(e.dataTransfer.files);
  });
  fileInput.addEventListener('change', () => handleFiles(fileInput.files));

  function handleFiles(files) {
    for (let f of files) {
      selectedFiles.push(f);
    }
    renderSelectedFiles();
  }

  function renderSelectedFiles() {
    selectedFilesContainer.innerHTML = selectedFiles
      .map(
        (f, idx) => `
      <div class="selected-file-chip">
        <span>📄 ${escapeHtml(f.name)} (${(f.size / 1024).toFixed(1)} KB)</span>
        <button type="button" class="btn btn-sm text-danger" onclick="removeFile(${idx})">&times;</button>
      </div>
    `
      )
      .join('');
  }

  window.removeFile = function (index) {
    selectedFiles.splice(index, 1);
    renderSelectedFiles();
  };

  // Run File Upload
  document.getElementById('wb-btn-run-file')?.addEventListener('click', async () => {
    if (selectedFiles.length === 0) return showToast('warning', '请先选择或拖拽单证文件上传');


    const statusElem = document.getElementById('wb-result-status-text');
    const jsonElem = document.getElementById('wb-json-output');
    statusElem.textContent = '⏳ 上传单证文件并解析中...';
    jsonElem.textContent = '// 文件上传成功，后台 Worker 正在异步解析并抽取 V3 字段，正在轮询任务结果...';

    const formData = new FormData();
    selectedFiles.forEach((f) => formData.append('files', f));

    const selectedTenantId = document.getElementById('wb-select-key-file').value;
    const headers = {};
    if (selectedTenantId) {
      headers['X-Tenant-ID'] = selectedTenantId;
    }

    try {
      const res = await adminFetch('/api/v1/extract/async/upload', {
        method: 'POST',
        headers: headers,
        body: formData,
      });



      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail?.message || '上传任务提交失败');
      }

      const submitData = await res.json();
      const taskId = submitData.task_id;
      statusElem.textContent = `⏳ 任务 ${taskId} 排队处理中，正在轮询状态...`;

      // Poll task result
      let attempts = 0;
      const pollInterval = setInterval(async () => {
        attempts++;
        try {
          const pollRes = await adminFetch(`/admin/tasks?search=${encodeURIComponent(taskId)}`);
          const pollData = await pollRes.json();
          const task = pollData.items.find((t) => t.id === taskId);

          if (task && (task.status === 'SUCCESS' || task.status === 'FAILED')) {
            clearInterval(pollInterval);
            if (task.status === 'SUCCESS') {
              statusElem.innerHTML = `✅ 抽取成功 · 耗时: <strong>${task.duration_ms} ms</strong> · 扣费: <strong class="text-danger">¥${parseFloat(task.charged_amount).toFixed(2)}</strong>`;
              jsonElem.textContent = JSON.stringify(task.result_json, null, 2);
            } else {
              statusElem.innerHTML = `<span class="text-danger">❌ 抽取失败: ${escapeHtml(task.error_message || '未知错误')}</span>`;
              jsonElem.textContent = `// 错误日志:\n${task.error_message}`;
            }
          } else if (attempts > 60) {
            clearInterval(pollInterval);
            statusElem.textContent = '⚠️ 轮询超时，请前往任务全流程监控页查看';
          }
        } catch (pollErr) {
          console.error(pollErr);
        }
      }, 1500);
    } catch (err) {
      statusElem.innerHTML = `<span class="text-danger">❌ 提交失败: ${escapeHtml(err.message)}</span>`;
      jsonElem.textContent = `// 提交异常:\n${err.message}`;
    }
  });

  document.getElementById('wb-btn-copy-json').addEventListener('click', () => {
    copyToClipboard(document.getElementById('wb-json-output').textContent);
  });

  // -------------------------------------------------------------
  // Concurrency Stress Test Logic
  // -------------------------------------------------------------
  const btnOpenBench = document.getElementById('btn-open-concurrency-modal');
  const benchTenantSelect = document.getElementById('bench-tenant-select');
  if (benchTenantSelect) {
    benchTenantSelect.addEventListener('change', updateBenchmarkTenantSummary);
  }

  if (btnOpenBench) {
    btnOpenBench.addEventListener('click', () => {
      const workbenchTenantId = document.getElementById('wb-select-key')?.value || '';
      const matchingOption = Array.from(benchTenantSelect?.options || []).find(
        (option) => option.value === workbenchTenantId && !option.disabled
      );
      if (matchingOption) benchTenantSelect.value = workbenchTenantId;
      updateBenchmarkTenantSummary();
      openModal('modal-concurrency-test');
    });
  }

  const btnStartBench = document.getElementById('btn-start-bench');
  if (btnStartBench) {
    btnStartBench.addEventListener('click', async () => {
      const taskCount = Number(document.getElementById('bench-task-count').value);
      const concurrency = Number(document.getElementById('bench-concurrency-limit').value);
      const selectedTenantId = benchTenantSelect?.value || '';
      const selectedTenant = currentTenants.find((tenant) => tenant.id === selectedTenantId && tenant.is_active !== false);
      const progressSection = document.getElementById('bench-progress-section');
      const progressBar = document.getElementById('bench-progress-bar');
      const progressText = document.getElementById('bench-progress-text');
      const statusBadge = document.getElementById('bench-status-badge');
      const liveLog = document.getElementById('bench-live-log');

      if (!selectedTenant) {
        showToast('warning', '请选择一个已启用的压测目标租户');
        benchTenantSelect?.focus();
        return;
      }

      const tenantConcurrency = Math.min(
        Number(selectedTenant.max_concurrency) || MIN_TENANT_CONCURRENCY,
        MAX_TENANT_CONCURRENCY,
      );
      if (
        !Number.isInteger(taskCount)
        || taskCount < MIN_BENCHMARK_TASKS
        || taskCount > MAX_BENCHMARK_TASKS
      ) {
        return showToast('warning', '压测任务总数须为 1 至 100 之间的整数');
      }
      if (
        !Number.isInteger(concurrency)
        || concurrency < MIN_TENANT_CONCURRENCY
        || concurrency > tenantConcurrency
        || concurrency > taskCount
      ) {
        return showToast(
          'warning',
          `客户端并发须为 1 至 ${Math.min(tenantConcurrency, taskCount)} 之间的整数`,
        );
      }

      const estimatedCost = taskCount * parseFloat(selectedTenant.unit_price || 0);
      const confirmed = await showConfirmModal({
        title: '⚡ 并发压力测试确认',
        message: `确认在「${selectedTenant.name}」下以 ${concurrency} 并发提交 ${taskCount} 个真实任务吗？\n成功任务预计最高扣费 ¥${estimatedCost.toFixed(2)}。`,
        iconType: 'warning',
        confirmText: '启动压测',
      });
      if (!confirmed) return;


      progressSection.style.display = 'block';
      progressBar.style.width = '5%';
      progressText.textContent = `正在并发提交 ${taskCount} 个任务...`;
      statusBadge.textContent = '提交入队中...';
      statusBadge.className = 'text-warning';
      btnStartBench.disabled = true;

      const sampleDocs = [
        { subject: "Booking - MSC - Yantian to Hamburg", body: "Please arrange booking. Freight prepaid.", text: "SHIPPER: GLORY SHIPPING CO.\nCONSIGNEE: EURO LOGISTICS\nPOL: YANTIAN\nPOD: HAMBURG\nCONTAINER: MSCU9988776 / 40HQ\nGOODS: AUTOMOTIVE PARTS 汽车零部件\nPACKAGES: 420 CARTONS\nG.W.: 8,500.00 KGS\nMEAS: 58.5 CBM" },
        { subject: "Bkg Ref: COSCO - Ningbo to Long Beach", body: "FCL shipment booking. CY-CY.", text: "SHIPPER: NINGBO TEXTILE CORP.\nCONSIGNEE: PACIFIC APPAREL\nPOL: NINGBO\nPOD: LONG BEACH\nCONTAINER: CCLU1234567 / 20GP\nGOODS: COTTON SHIRTS 纯棉衬衫\nPACKAGES: 800 CARTONS\nG.W.: 6,200.00 KGS\nMEAS: 28.0 CBM" },
        { subject: "Booking Memo - CMA CGM - Qingdao to Rotterdam", body: "Reefer container booking.", text: "SHIPPER: QINGDAO SEAFOOD\nCONSIGNEE: ROTTERDAM COLD STORE\nPOL: QINGDAO\nPOD: ROTTERDAM\nCONTAINER: CMAU5566778 / 40RF\nGOODS: FROZEN FISH 冷冻鱼\nPACKAGES: 1200 BOXES\nG.W.: 22,000.00 KGS\nMEAS: 60.0 CBM" },
      ];

      const submittedTaskIds = [];
      let failedSubmissionCount = 0;
      let logText = `[${new Date().toLocaleTimeString()}] 🚀 启动并发压测: 总任务 ${taskCount}, 并发 ${concurrency}\n`;
      logText += `目标租户: ${selectedTenant.name} (${selectedTenant.id})，单价 ¥${parseFloat(selectedTenant.unit_price).toFixed(2)} / 次\n`;
      liveLog.textContent = logText;

      const startTime = Date.now();

      // Submit with a bounded client-side worker pool. The selected concurrency
      // now controls real in-flight requests instead of only changing the label.
      let nextTaskIndex = 0;
      let processedSubmissionCount = 0;
      const workerCount = Math.min(concurrency, taskCount);

      async function submitWorker() {
        while (true) {
          const i = nextTaskIndex;
          nextTaskIndex += 1;
          if (i >= taskCount) return;

          const sample = sampleDocs[i % sampleDocs.length];
          const headers = { 'Content-Type': 'application/json' };
          if (selectedTenantId) headers['X-Tenant-ID'] = selectedTenantId;

          try {
            const res = await adminFetch('/api/v1/extract/async', {
              method: 'POST',
              headers,
              body: JSON.stringify({
                mail_subject: `[压测 #${i + 1}] ${sample.subject}`,
                mail_body: sample.body,
                attachments: [{ filename: `doc_${i + 1}.txt`, content_type: 'text/plain', text: sample.text, tables: [], ocr_text: '' }],
              }),
            });
            if (!res.ok) {
              const errorBody = await res.json().catch(() => ({}));
              throw new Error(errorBody.detail?.message || errorBody.message || `HTTP ${res.status}`);
            }

            const data = await res.json();
            submittedTaskIds.push(data.task_id);
            logText += `[${new Date().toLocaleTimeString()}] 任务 #${i + 1} 已入队: ${data.task_id}\n`;
          } catch (err) {
            failedSubmissionCount += 1;
            logText += `[${new Date().toLocaleTimeString()}] 任务 #${i + 1} 提交失败: ${err.message}\n`;
          } finally {
            processedSubmissionCount += 1;
            const submitPercent = 5 + Math.round((processedSubmissionCount / taskCount) * 25);
            progressBar.style.width = `${submitPercent}%`;
            progressText.textContent = `正在并发提交: ${processedSubmissionCount} / ${taskCount}`;
            liveLog.textContent = logText;
            liveLog.scrollTop = liveLog.scrollHeight;
          }
        }
      }

      await Promise.all(Array.from({ length: workerCount }, () => submitWorker()));

      if (submittedTaskIds.length === 0) {
        progressBar.style.width = '100%';
        progressText.textContent = '没有任务成功入队，请查看错误日志';
        statusBadge.textContent = '提交失败';
        statusBadge.className = 'text-danger';
        btnStartBench.disabled = false;
        return;
      }

      progressBar.style.width = '30%';
      progressText.textContent = `${submittedTaskIds.length} 个任务已入队，Worker 正在并发消费...`;
      statusBadge.textContent = '后台异步消费中...';

      // Poll tasks until completed
      const finishedTasksMap = new Map(); // taskId -> status
      const totalSubmitted = submittedTaskIds.length;
      const dynamicTimeoutMs = Math.max(300000, totalSubmitted * 6000); // at least 5 mins, 6s per task
      let pollTimer = null;
      let pollInFlight = false;
      let pollingFinished = false;

      const finishBenchmark = (timedOut, successCount, completedCount, timeoutReason = '') => {
        if (pollingFinished) return;
        pollingFinished = true;
        if (pollTimer) clearInterval(pollTimer);

        const totalElapsedSec = Math.max((Date.now() - startTime) / 1000, 0.001);
        const failedCount = completedCount - successCount;
        const unfinishedCount = totalSubmitted - completedCount;
        const throughput = completedCount / totalElapsedSec;
        progressBar.style.width = '100%';
        progressText.textContent = timedOut
          ? `压测超时: ${completedCount} / ${totalSubmitted} 完成`
          : `压测完成: ${completedCount} / ${totalSubmitted} 完成`;
        statusBadge.textContent = timedOut ? '压测超时' : '压测完成';
        statusBadge.className = timedOut ? 'text-warning' : 'text-success';
        btnStartBench.disabled = false;

        logText += `\n========================================\n`;
        logText += `📊 并发压测完成总结报告:\n`;
        logText += `总提交任务: ${totalSubmitted}\n`;
        logText += `提交失败数: ${failedSubmissionCount}\n`;
        logText += `成功完成数: ${successCount} (${((successCount / totalSubmitted) * 100).toFixed(1)}%)\n`;
        logText += `执行失败数: ${failedCount}\n`;
        if (unfinishedCount > 0) logText += `超时未完成数: ${unfinishedCount}\n`;
        if (timeoutReason) logText += `超时原因: ${timeoutReason}\n`;
        logText += `总耗时: ${totalElapsedSec.toFixed(2)} 秒\n`;
        logText += `已完成吞吐率 (TPS): ${throughput.toFixed(2)} 封/秒\n`;
        logText += `折合日处理量: ${(throughput * 86400).toFixed(0)} 封/天\n`;
        const configuredUnitPrice = Number(selectedTenant.unit_price);
        const unitPrice = Number.isFinite(configuredUnitPrice) ? configuredUnitPrice : 0.5;
        logText += `扣费对账: ¥${(successCount * unitPrice).toFixed(2)} (成功 ${successCount} 次 × ${unitPrice.toFixed(2)} 元)\n`;
        logText += `========================================\n`;
        liveLog.textContent = logText;
        liveLog.scrollTop = liveLog.scrollHeight;

        loadDashboardStats();
        loadTasksTable();
        loadTenantsTable();
      };

      const pollTaskStatuses = async () => {
        if (pollInFlight || pollingFinished) return;
        pollInFlight = true;
        try {
          const checkRes = await adminFetch('/admin/tasks/statuses', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task_ids: submittedTaskIds }),
          });
          if (!checkRes.ok) throw new Error(`任务状态查询失败 (HTTP ${checkRes.status})`);
          const items = await checkRes.json();

          for (const it of items) {
            if (it.status === 'SUCCESS' || it.status === 'FAILED') {
              finishedTasksMap.set(it.id, it.status);
            }
          }

          const completedCount = finishedTasksMap.size;
          let successCount = 0;
          for (const s of finishedTasksMap.values()) {
            if (s === 'SUCCESS') successCount++;
          }

          const pct = Math.min(95, Math.round(30 + (completedCount / totalSubmitted) * 65));
          progressBar.style.width = `${pct}%`;
          progressText.textContent = `正在消费中: ${completedCount} / ${totalSubmitted} 完成 (成功: ${successCount})`;

          const elapsedMs = Date.now() - startTime;
          const timedOut = elapsedMs > dynamicTimeoutMs;
          if (completedCount >= totalSubmitted || timedOut) {
            finishBenchmark(timedOut, successCount, completedCount);
          }
        } catch (pollErr) {
          console.error(pollErr);
          if (Date.now() - startTime > dynamicTimeoutMs) {
            const completedCount = finishedTasksMap.size;
            const successCount = Array.from(finishedTasksMap.values())
              .filter(status => status === 'SUCCESS').length;
            finishBenchmark(true, successCount, completedCount, pollErr.message);
          } else {
            progressText.textContent = `任务状态查询暂时失败，正在重试: ${pollErr.message}`;
          }
        } finally {
          pollInFlight = false;
        }
      };

      pollTimer = setInterval(pollTaskStatuses, 1500);
      pollTaskStatuses();
    });
  }

  // -------------------------------------------------------------
  // Custom UI Dialog & Toast Helpers
  // -------------------------------------------------------------
  window.showToast = function (type, message, title = '') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast-item toast-${type}`;

    let iconSvg = '';
    let defaultTitle = '';
    if (type === 'success') {
      defaultTitle = title || '操作成功';
      iconSvg = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
    } else if (type === 'error') {
      defaultTitle = title || '操作失败';
      iconSvg = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>`;
    } else if (type === 'warning') {
      defaultTitle = title || '温馨提示';
      iconSvg = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>`;
    } else {
      defaultTitle = title || '系统通知';
      iconSvg = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>`;
    }

    toast.innerHTML = `
      <div class="toast-icon-wrap">${iconSvg}</div>
      <div class="toast-content">
        <div class="toast-title">${escapeHtml(defaultTitle)}</div>
        <div class="toast-message">${escapeHtml(message)}</div>
      </div>
      <button class="toast-close" type="button">&times;</button>
    `;

    container.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add('show'));

    const dismiss = () => {
      toast.classList.remove('show');
      toast.classList.add('hide');
      setTimeout(() => toast.remove(), 300);
    };

    toast.querySelector('.toast-close').addEventListener('click', dismiss);
    const timer = setTimeout(dismiss, 3400);
    toast.addEventListener('mouseenter', () => clearTimeout(timer));
  };

  window.showConfirmModal = function ({ title = '请确认操作', message, iconType = 'warning', confirmText = '确认', cancelText = '取消', isDanger = false }) {
    return new Promise((resolve) => {
      const modal = document.getElementById('modal-global-confirm');
      const titleEl = document.getElementById('confirm-modal-title');
      const msgEl = document.getElementById('confirm-modal-message');
      const iconBox = document.getElementById('confirm-icon-box');
      const okBtn = document.getElementById('confirm-modal-ok');
      const cancelBtn = document.getElementById('confirm-modal-cancel');

      titleEl.textContent = title;
      msgEl.innerHTML = escapeHtml(message).replace(/\n/g, '<br>');
      okBtn.textContent = confirmText;
      cancelBtn.textContent = cancelText;

      if (isDanger) {
        okBtn.className = 'btn btn-danger';
        iconBox.className = 'icon-danger';
        iconBox.innerHTML = `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>`;
      } else {
        okBtn.className = 'btn btn-primary';
        iconBox.className = iconType === 'success' ? 'icon-success' : 'icon-warning';
        iconBox.innerHTML = iconType === 'success'
          ? `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"></polyline></svg>`
          : `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>`;
      }

      const cleanup = () => {
        modal.classList.remove('active');
        okBtn.onclick = null;
        cancelBtn.onclick = null;
      };

      okBtn.onclick = () => {
        cleanup();
        resolve(true);
      };
      cancelBtn.onclick = () => {
        cleanup();
        resolve(false);
      };

      modal.classList.add('active');
    });
  };

  window.showPromptModal = function ({ title = '请输入信息', label = '输入内容:', placeholder = '', defaultValue = '', confirmText = '确定', cancelText = '取消' }) {
    return new Promise((resolve) => {
      const modal = document.getElementById('modal-global-prompt');
      const titleEl = document.getElementById('prompt-modal-title');
      const labelEl = document.getElementById('prompt-modal-label');
      const inputEl = document.getElementById('prompt-modal-input');
      const okBtn = document.getElementById('prompt-modal-ok');
      const cancelBtn = document.getElementById('prompt-modal-cancel');
      const closeBtn = document.getElementById('prompt-modal-close');

      titleEl.textContent = title;
      labelEl.textContent = label;
      inputEl.placeholder = placeholder;
      inputEl.value = defaultValue;
      okBtn.textContent = confirmText;
      cancelBtn.textContent = cancelText;

      const cleanup = () => {
        modal.classList.remove('active');
        okBtn.onclick = null;
        cancelBtn.onclick = null;
        closeBtn.onclick = null;
        inputEl.onkeydown = null;
      };

      const handleOk = () => {
        const val = inputEl.value;
        cleanup();
        resolve(val);
      };

      const handleCancel = () => {
        cleanup();
        resolve(null);
      };

      okBtn.onclick = handleOk;
      cancelBtn.onclick = handleCancel;
      closeBtn.onclick = handleCancel;
      inputEl.onkeydown = (e) => {
        if (e.key === 'Enter') handleOk();
        if (e.key === 'Escape') handleCancel();
      };

      modal.classList.add('active');
      setTimeout(() => inputEl.focus(), 50);
    });
  };

  // -------------------------------------------------------------
  // Utilities
  // -------------------------------------------------------------
  function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function formatDate(dateStr) {
    if (!dateStr) return '-';
    const d = new Date(dateStr);
    return `${d.getMonth() + 1}-${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
  }

  window.copyToClipboard = function (text, customMsg = '已成功复制到剪贴板！') {
    if (!text) {
      showToast('warning', '复制内容为空');
      return;
    }

    const fallbackCopy = () => {
      try {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.top = '-9999px';
        textarea.style.left = '-9999px';
        textarea.setAttribute('readonly', '');
        document.body.appendChild(textarea);
        textarea.select();
        textarea.setSelectionRange(0, textarea.value.length);
        const successful = document.execCommand('copy');
        document.body.removeChild(textarea);
        if (successful) {
          showToast('success', customMsg);
        } else {
          showToast('error', '复制失败，请手动选择复制');
        }
      } catch (err) {
        showToast('error', '复制失败: ' + err.message);
      }
    };

    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text)
        .then(() => showToast('success', customMsg))
        .catch(() => fallbackCopy());
    } else {
      fallbackCopy();
    }
  };

  const copyToClipboard = window.copyToClipboard;


  function debounce(func, wait) {
    let timeout;
    return function (...args) {
      clearTimeout(timeout);
      timeout = setTimeout(() => func.apply(this, args), wait);
    };
  }

  // -------------------------------------------------------------
  // LLM Configuration Management & Model / Key Switching
  // -------------------------------------------------------------
  const LLM_PROVIDER_PRESETS = {
    senseaudio: {
      name: '商汤 SenseAudio',
      baseUrl: 'https://api.senseaudio.cn/v1',
      model: 'deepseek-v4-flash-0731',
    },
    deepseek: {
      name: 'DeepSeek 官方',
      baseUrl: 'https://api.deepseek.com/v1',
      model: 'deepseek-chat',
    },
    siliconflow: {
      name: '硅基流动 SiliconFlow',
      baseUrl: 'https://api.siliconflow.cn/v1',
      model: 'deepseek-ai/DeepSeek-V3',
    },
    dashscope: {
      name: '阿里云百炼 DashScope',
      baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      model: 'qwen-plus',
    },
    zhipu: {
      name: '智谱 AI (GLM)',
      baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
      model: 'glm-4-flash',
    },
    openai: {
      name: 'OpenAI 官方',
      baseUrl: 'https://api.openai.com/v1',
      model: 'gpt-4o',
    },
    ollama: {
      name: '本地 Ollama / vLLM',
      baseUrl: 'http://localhost:11434/v1',
      model: 'qwen2.5:14b',
    },
  };

  function getSelectedModel() {
    const modelSelect = document.getElementById('llm-cfg-model');
    if (!modelSelect) return 'deepseek-v4-flash-0731';
    if (modelSelect.value === '__custom__') {
      const customInput = document.getElementById('llm-cfg-custom-model');
      return (customInput?.value.trim()) || 'deepseek-v4-flash-0731';
    }
    return modelSelect.value.trim() || 'deepseek-v4-flash-0731';
  }

  function setSelectedModel(modelName) {
    const modelSelect = document.getElementById('llm-cfg-model');
    const customWrapper = document.getElementById('llm-custom-model-wrapper');
    const customInput = document.getElementById('llm-cfg-custom-model');
    if (!modelSelect || !modelName) return;

    let found = false;
    for (let i = 0; i < modelSelect.options.length; i++) {
      if (modelSelect.options[i].value === modelName) {
        modelSelect.selectedIndex = i;
        found = true;
        break;
      }
    }

    if (!found && modelName !== '__custom__') {
      const newOpt = document.createElement('option');
      newOpt.value = modelName;
      newOpt.textContent = modelName;
      const customOpt = modelSelect.querySelector('option[value="__custom__"]');
      if (customOpt) {
        modelSelect.insertBefore(newOpt, customOpt);
      } else {
        modelSelect.appendChild(newOpt);
      }
      newOpt.selected = true;
      found = true;
    }

    if (customWrapper) {
      if (modelSelect.value === '__custom__') {
        customWrapper.style.display = 'block';
        if (customInput && modelName !== '__custom__') {
          customInput.value = modelName;
        }
      } else {
        customWrapper.style.display = 'none';
      }
    }
  }

  // Model select change listener for custom option
  document.getElementById('llm-cfg-model')?.addEventListener('change', function () {
    const customWrapper = document.getElementById('llm-custom-model-wrapper');
    const customInput = document.getElementById('llm-cfg-custom-model');
    if (this.value === '__custom__') {
      if (customWrapper) customWrapper.style.display = 'block';
      if (customInput) customInput.focus();
    } else {
      if (customWrapper) customWrapper.style.display = 'none';
    }
  });

  window.applyLLMPreset = function (presetKey) {
    const preset = LLM_PROVIDER_PRESETS[presetKey];
    if (!preset) return;

    const baseUrlInput = document.getElementById('llm-cfg-base-url');
    const apiKeyInput = document.getElementById('llm-cfg-api-key');

    if (baseUrlInput) baseUrlInput.value = preset.baseUrl;
    setSelectedModel(preset.model);

    showToast('info', `已载入 ${preset.name} 预设！如切换服务商请记得填写对应的 API Key 并点击保存。`);
    if (apiKeyInput && !apiKeyInput.value) {
      apiKeyInput.focus();
    }
  };

  function getSelectedVisionModel() {
    const select = document.getElementById('vision-cfg-model-select');
    if (!select) return 'qwen3.8-27b';
    if (select.value === '__custom_vision__') {
      const custom = document.getElementById('vision-cfg-custom-model')?.value.trim();
      return custom || 'qwen3.8-27b';
    }
    return select.value;
  }

  function setSelectedVisionModel(modelName) {
    const select = document.getElementById('vision-cfg-model-select');
    const customWrapper = document.getElementById('vision-custom-model-wrapper');
    const customInput = document.getElementById('vision-cfg-custom-model');
    if (!select) return;
    let found = false;
    for (let opt of select.options) {
      if (opt.value === modelName) {
        select.value = modelName;
        found = true;
        break;
      }
    }
    if (!found && modelName) {
      select.value = '__custom_vision__';
      if (customWrapper) customWrapper.style.display = 'block';
      if (customInput) customInput.value = modelName;
    } else {
      if (customWrapper) customWrapper.style.display = 'none';
    }
  }

  document.getElementById('vision-cfg-model-select')?.addEventListener('change', function () {
    const customWrapper = document.getElementById('vision-custom-model-wrapper');
    const customInput = document.getElementById('vision-cfg-custom-model');
    if (this.value === '__custom_vision__') {
      if (customWrapper) customWrapper.style.display = 'block';
      if (customInput) customInput.focus();
    } else {
      if (customWrapper) customWrapper.style.display = 'none';
    }
  });

  // Toggle vision settings section visibility
  document.getElementById('vision-cfg-enabled')?.addEventListener('change', function () {
    const body = document.getElementById('vision-settings-body');
    const badge = document.getElementById('vision-status-badge');
    if (body) {
      body.style.display = this.checked ? 'block' : 'none';
    }
    if (badge) {
      badge.textContent = this.checked ? '已启用' : '已禁用';
      badge.className = this.checked ? 'badge badge-success' : 'badge badge-secondary';
    }
  });

  let llmRuntimeEditable = true;

  async function loadLLMConfig() {
    try {
      const res = await adminFetch('/admin/llm-config');
      if (!res.ok) {
        showToast('error', '获取大模型配置失败: HTTP ' + res.status);
        return;
      }
      const data = await res.json();

      const baseUrlInput = document.getElementById('llm-cfg-base-url');
      const apiKeyInput = document.getElementById('llm-cfg-api-key');
      const maskedKeyEl = document.getElementById('llm-cfg-masked-key');
      const timeoutInput = document.getElementById('llm-cfg-timeout');
      const statusBadge = document.getElementById('llm-status-badge');
      const saveButton = document.getElementById('btn-save-llm-config');
      const testButton = document.getElementById('btn-test-llm-connection');
      const modelSelect = document.getElementById('llm-cfg-model');
      const fetchModelsButton = document.getElementById('btn-fetch-remote-models');

      if (baseUrlInput) baseUrlInput.value = data.base_url || '';
      if (apiKeyInput) {
        apiKeyInput.value = '';
        apiKeyInput.placeholder = data.is_configured ? '若不修改密钥请留空（将保留当前生效密钥）' : '请输入 Bearer API Key';
      }
      if (maskedKeyEl) maskedKeyEl.textContent = data.api_key_masked || '未配置';
      if (data.model) {
        setSelectedModel(data.model);
      }
      if (timeoutInput) timeoutInput.value = data.timeout_seconds || 60;

      // Vision model fields
      const visionEnabledCb = document.getElementById('vision-cfg-enabled');
      const visionSettingsBody = document.getElementById('vision-settings-body');
      const visionStatusBadge = document.getElementById('vision-status-badge');
      const visionMaxImagesInput = document.getElementById('vision-cfg-max-images');
      const visionModelSelect = document.getElementById('vision-cfg-model-select');
      const visionCustomModelInput = document.getElementById('vision-cfg-custom-model');

      if (visionEnabledCb) {
        visionEnabledCb.checked = !!data.vision_enabled;
        if (visionSettingsBody) {
          visionSettingsBody.style.display = data.vision_enabled ? 'block' : 'none';
        }
        if (visionStatusBadge) {
          visionStatusBadge.textContent = data.vision_enabled ? '已启用' : '已禁用';
          visionStatusBadge.className = data.vision_enabled ? 'badge badge-success' : 'badge badge-secondary';
        }
      }
      setSelectedVisionModel(data.vision_model || 'qwen3.8-27b');
      if (visionMaxImagesInput) visionMaxImagesInput.value = data.vision_max_images_per_task || 5;

      llmRuntimeEditable = data.runtime_editable !== false;
      [
        baseUrlInput,
        apiKeyInput,
        modelSelect,
        timeoutInput,
        visionEnabledCb,
        visionMaxImagesInput,
        visionModelSelect,
        visionCustomModelInput,
      ].forEach((input) => {
        if (input) input.disabled = !llmRuntimeEditable;
      });
      if (saveButton) {
        saveButton.disabled = !llmRuntimeEditable;
        saveButton.title = llmRuntimeEditable
          ? '保存并应用配置'
          : '生产环境配置由部署变量与 Docker secrets 管理';
      }
      if (fetchModelsButton) fetchModelsButton.disabled = !llmRuntimeEditable;
      document.querySelectorAll('#llm-preset-buttons button').forEach((button) => {
        button.disabled = !llmRuntimeEditable;
      });
      if (testButton) testButton.disabled = false;

      if (statusBadge) {
        if (data.is_configured) {
          statusBadge.textContent = llmRuntimeEditable ? '已就绪 (已配置)' : '已就绪 (部署配置)';
          statusBadge.className = 'badge badge-success';
          if (llmRuntimeEditable) {
            // Dynamically populate remote models only when selections can be saved.
            fetchRemoteModels(true);
          }
        } else {
          statusBadge.textContent = '未配置 API Key';
          statusBadge.className = 'badge badge-warning';
        }
      }
    } catch (err) {
      console.error(err);
      showToast('error', '加载大模型配置出错: ' + err.message);
    }
  }

  async function saveLLMConfig() {
    if (!llmRuntimeEditable) {
      showToast('warning', '生产环境配置由部署变量与 Docker secrets 管理，请修改部署配置后重启服务');
      return;
    }
    const baseUrl = document.getElementById('llm-cfg-base-url')?.value.trim();
    const apiKey = document.getElementById('llm-cfg-api-key')?.value.trim();
    const model = getSelectedModel();
    const timeout = parseInt(document.getElementById('llm-cfg-timeout')?.value) || 60;

    if (!baseUrl) {
      showToast('warning', '请填写大模型 API 基础地址 (Base URL)');
      return;
    }
    if (!baseUrl.startsWith('http://') && !baseUrl.startsWith('https://')) {
      showToast('warning', 'Base URL 必须以 http:// 或 https:// 开头');
      return;
    }

    const visionEnabled = !!document.getElementById('vision-cfg-enabled')?.checked;
    const visionModel = getSelectedVisionModel();
    const visionMaxImages = parseInt(document.getElementById('vision-cfg-max-images')?.value) || 5;

    const btnSave = document.getElementById('btn-save-llm-config');
    if (btnSave) {
      btnSave.disabled = true;
      btnSave.innerHTML = '<span class="loading-spinner"></span> 正在保存...';
    }

    try {
      const payload = {
        base_url: baseUrl,
        model: model,
        timeout_seconds: timeout,
        vision_enabled: visionEnabled,
        vision_model: visionModel,
        vision_max_images_per_task: visionMaxImages,
      };
      if (apiKey) {
        payload.api_key = apiKey;
      }

      const res = await adminFetch('/admin/llm-config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        const updated = await res.json();
        showToast('success', `🎉 大模型与多模态配置已成功保存并生效！主模型: ${updated.model}, 视觉模型: ${updated.vision_model}`);
        const maskedKeyEl = document.getElementById('llm-cfg-masked-key');
        if (maskedKeyEl) maskedKeyEl.textContent = updated.api_key_masked || '未配置';
        const apiKeyInput = document.getElementById('llm-cfg-api-key');
        if (apiKeyInput) {
          apiKeyInput.value = '';
          apiKeyInput.placeholder = updated.is_configured ? '若不修改密钥请留空（将保留当前生效密钥）' : '请输入 Bearer API Key';
        }
        const statusBadge = document.getElementById('llm-status-badge');
        if (statusBadge) {
          statusBadge.textContent = updated.is_configured ? '已就绪 (已配置)' : '未配置 API Key';
          statusBadge.className = updated.is_configured ? 'badge badge-success' : 'badge badge-warning';
        }
        const visionStatusBadge = document.getElementById('vision-status-badge');
        if (visionStatusBadge) {
          visionStatusBadge.textContent = updated.vision_enabled ? '已启用' : '已禁用';
          visionStatusBadge.className = updated.vision_enabled ? 'badge badge-success' : 'badge badge-secondary';
        }
      } else {
        const err = await res.json().catch(() => ({}));
        showToast('error', '保存配置失败: ' + (err.detail?.message || err.detail || '未知错误'));
      }
    } catch (err) {
      showToast('error', '保存配置网络异常: ' + err.message);
    } finally {
      if (btnSave) {
        btnSave.disabled = !llmRuntimeEditable;
        btnSave.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg> <span>保存并应用配置</span>';
      }
    }
  }

  async function testLLMConnection() {
    const baseUrl = document.getElementById('llm-cfg-base-url')?.value.trim();
    const apiKey = document.getElementById('llm-cfg-api-key')?.value.trim();
    const model = getSelectedModel();
    const visionEnabled = !!document.getElementById('vision-cfg-enabled')?.checked;
    const visionModel = getSelectedVisionModel();

    const btnTest = document.getElementById('btn-test-llm-connection');
    const labelSpan = document.getElementById('btn-test-llm-label');
    const resultBox = document.getElementById('llm-test-result-box');
    const titleEl = document.getElementById('llm-test-result-title');
    const detailEl = document.getElementById('llm-test-result-detail');

    if (btnTest) btnTest.disabled = true;
    if (labelSpan) labelSpan.textContent = '正在探测连通性...';
    if (resultBox) resultBox.style.display = 'none';

    try {
      const res = await adminFetch('/admin/llm-config/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          base_url: baseUrl || undefined,
          api_key: apiKey || undefined,
          model: model || undefined,
          vision_enabled: visionEnabled,
          vision_model: visionModel || undefined,
        }),
      });

      const data = await res.json();
      if (resultBox) resultBox.style.display = 'block';

      if (res.ok && data.code === 0) {
        showToast('success', '模型连通性测试通过');
        if (resultBox) {
          resultBox.style.background = 'rgba(16, 185, 129, 0.12)';
          resultBox.style.borderColor = 'rgba(16, 185, 129, 0.4)';
        }
        if (titleEl) {
          titleEl.style.color = '#34d399';
          titleEl.textContent = '连通性测试全部通过';
        }
        if (detailEl) {
          const m = data.data?.main_model || {};
          const v = data.data?.vision_model || {};
          const safe = (value) => escapeHtml(String(value ?? ''));
          let html = `<div style="display:flex; flex-direction:column; gap:6px; font-size:0.84rem;">`;
          html += `<div><strong>主抽取模型 [${safe(m.model || model)}]</strong>: <span style="color:#34d399;">正常</span> (耗时: ${safe(m.latency_ms || 0)}ms) - 响应预览: "${safe(m.preview || 'OK')}"</div>`;
          if (visionEnabled && v.model) {
            html += `<div><strong>视觉识别模型 [${safe(v.model || visionModel)}]</strong>: <span style="color:#34d399;">正常</span> (耗时: ${safe(v.latency_ms || 0)}ms) - 响应预览: "${safe(v.preview || 'OK')}"</div>`;
          } else {
            html += `<div><strong>视觉识别模型</strong>: <span style="color:var(--text-muted);">未启用 (已跳过)</span></div>`;
          }
          html += `</div>`;
          detailEl.innerHTML = html;
        }
      } else {
        const errMsg = data.message || (data.detail?.message || data.detail || '探测失败');
        showToast('warning', errMsg);
        if (resultBox) {
          resultBox.style.background = 'rgba(239, 68, 68, 0.12)';
          resultBox.style.borderColor = 'rgba(239, 68, 68, 0.4)';
        }
        if (titleEl) {
          titleEl.style.color = '#f87171';
          titleEl.textContent = '测试结果: ' + errMsg;
        }
        if (detailEl) {
          const m = data.data?.main_model || {};
          const v = data.data?.vision_model || {};
          const safe = (value) => escapeHtml(String(value ?? ''));
          let html = `<div style="display:flex; flex-direction:column; gap:6px; font-size:0.84rem;">`;
          if (m.model) {
            const mOk = m.status === 'success';
            html += `<div><strong>主抽取模型 [${safe(m.model)}]</strong>: ${mOk ? '<span style="color:#34d399;">正常 (' + safe(m.latency_ms) + 'ms)</span>' : '<span style="color:#f87171;">失败: ' + safe(m.error || '错误') + '</span>'}</div>`;
          }
          if (v && v.model) {
            const vOk = v.status === 'success';
            html += `<div><strong>视觉识别模型 [${safe(v.model)}]</strong>: ${vOk ? '<span style="color:#34d399;">正常 (' + safe(v.latency_ms) + 'ms)</span>' : '<span style="color:#f87171;">失败: ' + safe(v.error || '错误') + '</span>'}</div>`;
          }
          html += `</div>`;
          detailEl.innerHTML = html;
        }
      }
    } catch (err) {
      showToast('error', '探测请求网络异常: ' + err.message);
      if (resultBox) {
        resultBox.style.display = 'block';
        resultBox.style.background = 'rgba(239, 68, 68, 0.12)';
        resultBox.style.borderColor = 'rgba(239, 68, 68, 0.4)';
      }
      if (titleEl) {
        titleEl.style.color = '#f87171';
        titleEl.textContent = '请求异常: ' + err.message;
      }
    } finally {
      if (btnTest) btnTest.disabled = false;
      if (labelSpan) labelSpan.textContent = '测试连通性';
    }
  }

  async function fetchRemoteModels(isAuto = false) {
    const baseUrl = document.getElementById('llm-cfg-base-url')?.value.trim();
    const apiKey = document.getElementById('llm-cfg-api-key')?.value.trim();
    const btnFetch = document.getElementById('btn-fetch-remote-models');
    const labelSpan = document.getElementById('btn-fetch-models-label');
    const modelSelect = document.getElementById('llm-cfg-model');
    const visionSelect = document.getElementById('vision-cfg-model-select');

    if (btnFetch) btnFetch.disabled = true;
    if (labelSpan) labelSpan.textContent = '拉取中...';

    try {
      const res = await adminFetch('/admin/llm-config/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          base_url: baseUrl || undefined,
          api_key: apiKey || undefined,
        }),
      });

      const data = await res.json();
      if (res.ok && data.code === 0 && data.data && Array.isArray(data.data.models) && data.data.models.length > 0) {
        const models = data.data.models;
        const currentMainVal = getSelectedModel();
        const currentVisionVal = getSelectedVisionModel();

        // 1. Populate Main Model Dropdown
        if (modelSelect) {
          modelSelect.innerHTML = '';
          models.forEach((m) => {
            const opt = document.createElement('option');
            opt.value = m;
            opt.textContent = m;
            modelSelect.appendChild(opt);
          });
          const customOpt = document.createElement('option');
          customOpt.value = '__custom__';
          customOpt.textContent = '手动输入其他自定义模型...';
          modelSelect.appendChild(customOpt);

          setSelectedModel(currentMainVal);
        }

        // 2. Populate Vision Model Dropdown (Shares models from API)
        if (visionSelect) {
          visionSelect.innerHTML = '';
          models.forEach((m) => {
            const opt = document.createElement('option');
            opt.value = m;
            opt.textContent = (m === 'qwen3.8-27b') ? `${m} (推荐)` : m;
            visionSelect.appendChild(opt);
          });
          const customOpt = document.createElement('option');
          customOpt.value = '__custom_vision__';
          customOpt.textContent = '手动输入其他视觉模型...';
          visionSelect.appendChild(customOpt);

          // If no previous vision selection or default, prefer qwen3.8-27b if present
          const targetVision = currentVisionVal || (models.includes('qwen3.8-27b') ? 'qwen3.8-27b' : models[0]);
          setSelectedVisionModel(targetVision);
        }

        if (!isAuto) {
          showToast('success', `成功从 API 获取到 ${models.length} 个可用模型，已同步更新选项`);
        }
      } else {
        if (!isAuto) {
          const msg = data.message || (data.detail?.message || data.detail || '获取模型列表失败');
          showToast('warning', msg);
        }
      }
    } catch (err) {
      if (!isAuto) {
        showToast('error', '拉取模型列表网络异常: ' + err.message);
      }
    } finally {
      if (btnFetch) btnFetch.disabled = false;
      if (labelSpan) labelSpan.textContent = '从 API 获取模型';
    }
  }

  function toggleLLMKeyVisibility() {
    const input = document.getElementById('llm-cfg-api-key');
    const btn = document.getElementById('btn-toggle-llm-key-visibility');
    if (!input || !btn) return;

    if (input.type === 'password') {
      input.type = 'text';
      btn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;
      btn.title = '隐藏密钥';
    } else {
      input.type = 'password';
      btn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
      btn.title = '显示密钥';
    }
  }

  // Bind LLM configuration button events
  document.getElementById('btn-save-llm-config')?.addEventListener('click', saveLLMConfig);
  document.getElementById('btn-test-llm-connection')?.addEventListener('click', testLLMConnection);
  document.getElementById('btn-fetch-remote-models')?.addEventListener('click', () => fetchRemoteModels(false));
  document.getElementById('btn-toggle-llm-key-visibility')?.addEventListener('click', toggleLLMKeyVisibility);

  // =========================================================================
  // 6. Feedback Audit, Few-Shot Management & Version Release Logic
  // =========================================================================
  let currentAuditFeedbackId = null;
  let currentAuditFeedbackData = null;

  window.switchFeedbackSubTab = function(subName) {
    const btnAudit = document.getElementById('btn-subtab-fb-audit');
    const btnFewShot = document.getElementById('btn-subtab-fb-fewshot');
    const btnRelease = document.getElementById('btn-subtab-fb-release');

    const viewAudit = document.getElementById('subview-fb-audit');
    const viewFewShot = document.getElementById('subview-fb-fewshot');
    const viewRelease = document.getElementById('subview-fb-release');

    [btnAudit, btnFewShot, btnRelease].forEach(b => b && b.classList.remove('active'));
    [viewAudit, viewFewShot, viewRelease].forEach(v => v && (v.style.display = 'none'));

    if (subName === 'fewshot') {
      if (btnFewShot) btnFewShot.classList.add('active');
      if (viewFewShot) viewFewShot.style.display = 'block';
      loadAdminFewShots();
    } else if (subName === 'release') {
      if (btnRelease) btnRelease.classList.add('active');
      if (viewRelease) viewRelease.style.display = 'block';
      loadAdminVersions();
    } else {
      if (btnAudit) btnAudit.classList.add('active');
      if (viewAudit) viewAudit.style.display = 'block';
      loadAdminFeedbacks();
    }
  };

  window.loadAdminFeedbacks = async function() {
    const tbody = document.querySelector('#table-admin-feedbacks tbody');
    if (!tbody) return;

    const statusFilter = document.getElementById('filter-fb-status')?.value || '';
    let url = '/admin/feedbacks?page=1&page_size=50';
    if (statusFilter) url += `&status=${statusFilter}`;

    try {
      const res = await adminFetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const payload = data.data || {};
      const items = payload.items || [];

      // Update counters
      if (document.getElementById('stat-fb-pending')) document.getElementById('stat-fb-pending').textContent = payload.pending_count || 0;
      if (document.getElementById('stat-fb-accepted')) document.getElementById('stat-fb-accepted').textContent = payload.accepted_count || 0;
      if (document.getElementById('stat-fb-resolved')) document.getElementById('stat-fb-resolved').textContent = payload.resolved_count || 0;

      if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted py-8">暂无符合条件的纠错反馈工单</td></tr>';
        return;
      }

      tbody.innerHTML = items.map(it => {
        let statusBadge = '<span class="badge badge-warning">待审核</span>';
        if (it.status === 'ACCEPTED') statusBadge = '<span class="badge badge-success">已采纳/退款</span>';
        else if (it.status === 'RESOLVED') statusBadge = `<span class="badge" style="background:#0284c7; color:#fff;">已解决 (${escapeHtml(it.resolved_version || '最新')})</span>`;
        else if (it.status === 'REJECTED') statusBadge = '<span class="badge badge-danger">已驳回</span>';

        const refundText = it.is_refunded ? `<strong class="text-danger">已退 ¥${parseFloat(it.refund_amount).toFixed(2)}</strong>` : '<span class="text-muted">未退费</span>';

        return `
          <tr>
            <td class="font-mono" style="font-size:0.8rem;"><strong>${it.id}</strong></td>
            <td><strong>${escapeHtml(it.tenant_name || it.tenant_id)}</strong></td>
            <td class="font-mono" style="font-size:0.8rem;"><button type="button" class="task-context-link btn-feedback-task-context" data-task-id="${escapeHtml(it.task_id)}" data-feedback-id="${escapeHtml(it.id)}" title="查看任务执行、输入、计费和反馈上下文">${escapeHtml(it.task_id)}</button></td>
            <td><span class="badge badge-info">${it.diff_fields_count} 处变更</span></td>
            <td>${statusBadge}</td>
            <td>${refundText}</td>
            <td style="max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(it.notes || '-')}">${escapeHtml(it.notes || '-')}</td>
            <td class="text-muted" style="font-size:0.75rem;">${formatDate(it.created_at)}</td>
            <td>
              <button class="btn btn-sm btn-primary" onclick="openFeedbackDiffModal('${it.id}')">
                <span>Diff 审核</span>
              </button>
            </td>
          </tr>
        `;
      }).join('');

      tbody.querySelectorAll('.btn-feedback-task-context').forEach((button) => {
        button.addEventListener('click', () => {
          showTaskDetailModal(button.dataset.taskId, button.dataset.feedbackId);
        });
      });
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-danger py-8">加载工单失败: ${escapeHtml(err.message)}</td></tr>`;
    }
  };

  document.getElementById('filter-fb-status')?.addEventListener('change', loadAdminFeedbacks);

  window.openFeedbackDiffModal = async function(fbId) {
    currentAuditFeedbackId = fbId;
    try {
      const res = await adminFetch(`/admin/feedbacks/${fbId}`);
      if (!res.ok) throw new Error('查询工单详情失败');
      const data = await res.json();
      const fb = data.data;
      currentAuditFeedbackData = fb;

      document.getElementById('fd-id').textContent = fb.id;
      document.getElementById('fd-tenant').textContent = `${fb.tenant_name} (${fb.tenant_id})`;
      document.getElementById('fd-notes').textContent = fb.notes || '客户未填写备注';

      let statusBadge = '<span class="badge badge-warning">待审核 (PENDING)</span>';
      if (fb.status === 'ACCEPTED') {
        statusBadge = fb.is_refunded
          ? '<span class="badge badge-success">已采纳/已退费</span>'
          : '<span class="badge badge-success">已采纳/未退费</span>';
      }
      else if (fb.status === 'RESOLVED') statusBadge = `<span class="badge" style="background:#0284c7; color:#fff;">已发布解决 (${escapeHtml(fb.resolved_version || '最新')})</span>`;
      else if (fb.status === 'REJECTED') statusBadge = '<span class="badge badge-danger">已驳回 (REJECTED)</span>';
      document.getElementById('fd-status-badge').innerHTML = statusBadge;

      // Populate Original Input Source panel
      const inputTypeBadge = document.getElementById('fd-input-type-badge');
      if (inputTypeBadge) inputTypeBadge.textContent = fb.input_type || '-';
      const taskSubjectEl = document.getElementById('fd-task-subject');
      if (taskSubjectEl) taskSubjectEl.textContent = fb.task_subject || '-';
      const taskTimeEl = document.getElementById('fd-task-time');
      if (taskTimeEl) taskTimeEl.textContent = fb.task_time || '-';

      const filePathsRow = document.getElementById('fd-file-paths-row');
      const filePathsEl = document.getElementById('fd-file-paths');
      if (fb.file_paths && fb.file_paths.length > 0) {
        if (filePathsEl) {
          const fileNames = fb.file_paths
            .filter(p => typeof p === 'string')
            .map(p => String(p).split(/[\\/]/).pop())
            .filter(Boolean);
          filePathsEl.replaceChildren();
          fileNames.forEach((name) => {
            const downloadLink = document.createElement('a');
            downloadLink.href = '#';
            downloadLink.dataset.feedbackId = String(fb.id);
            downloadLink.dataset.filename = name;
            downloadLink.className = 'fd-download-link';
            downloadLink.textContent = name;
            downloadLink.style.color = '#38bdf8';
            downloadLink.style.textDecoration = 'underline';
            downloadLink.style.marginRight = '10px';
            filePathsEl.appendChild(downloadLink);
          });
          if (filePathsRow) filePathsRow.style.display = fileNames.length > 0 ? '' : 'none';
        }
      } else {
        if (filePathsRow) filePathsRow.style.display = 'none';
        if (filePathsEl) filePathsEl.replaceChildren();
      }

      const inputSummaryEl = document.getElementById('fd-input-summary');
      if (inputSummaryEl) {
        let displayText = '';
        if (fb.input_summary) {
          displayText = fb.input_summary;
        } else if (fb.raw_input_json) {
          displayText = typeof fb.raw_input_json === 'string' ? fb.raw_input_json : JSON.stringify(fb.raw_input_json, null, 2);
        } else {
          displayText = '（无原始输入文本记录）';
        }
        inputSummaryEl.textContent = displayText;
      }

      const refundCheckbox = document.getElementById('fd-auto-refund');
      const refundLabel = document.getElementById('fd-refund-label');
      const chargedAmount = Number(fb.charged_amount || 0);
      const canRefund = Boolean(fb.is_charged) && Number.isFinite(chargedAmount) && chargedAmount > 0;
      if (refundCheckbox) {
        refundCheckbox.disabled = !canRefund;
        refundCheckbox.checked = canRefund && !fb.is_refunded;
      }
      if (refundLabel) {
        refundLabel.textContent = canRefund
          ? `自动执行本次调用扣费退款冲正 (¥${chargedAmount.toFixed(2)})`
          : '该任务无有效原始扣款，不执行退款';
      }

      const diffSet = new Set(fb.diff_fields || []);

      // Populate Original Table
      const tbodyOrig = document.querySelector('#table-fd-original tbody');
      const origObj = fb.original_result || {};
      tbodyOrig.innerHTML = Object.entries(origObj).map(([k, v]) => {
        const isDiff = diffSet.has(k);
        const valStr = typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v || '');
        return `
          <tr style="${isDiff ? 'background: rgba(239, 68, 68, 0.18); font-weight:600;' : ''}">
            <td style="color:${isDiff ? '#f87171' : 'var(--text-secondary)'}; font-family:var(--font-mono); width:35%;">${escapeHtml(k)}</td>
            <td style="color:${isDiff ? '#fff' : 'var(--text-muted)'}; word-break:break-all;">${escapeHtml(valStr)}</td>
          </tr>
        `;
      }).join('');

      // Populate Corrected Table
      const tbodyCorr = document.querySelector('#table-fd-corrected tbody');
      const corrObj = fb.corrected_result || {};
      tbodyCorr.innerHTML = Object.entries(corrObj).map(([k, v]) => {
        const isDiff = diffSet.has(k);
        const valStr = typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v || '');
        return `
          <tr style="${isDiff ? 'background: rgba(16, 185, 129, 0.18); font-weight:600;' : ''}">
            <td style="color:${isDiff ? '#34d399' : 'var(--text-secondary)'}; font-family:var(--font-mono); width:35%;">${escapeHtml(k)}</td>
            <td style="color:${isDiff ? '#fff' : 'var(--text-muted)'}; word-break:break-all;">${escapeHtml(valStr)}</td>
          </tr>
        `;
      }).join('');

      // Set audit action fields
      document.getElementById('fd-error-category').value = fb.error_category || 'PROMPT_LLM';
      document.getElementById('fd-review-comment').value = fb.review_comment || '';

      const actionPanel = document.getElementById('fd-audit-action-panel');
      const footerBtns = document.getElementById('fd-footer-audit-btns');
      const btnReject = document.getElementById('btn-fd-reject');
      const btnAccept = document.getElementById('btn-fd-accept');

      if (fb.status !== 'PENDING') {
        if (actionPanel) actionPanel.style.display = 'none';
        if (footerBtns) footerBtns.style.display = 'none';
      } else {
        if (actionPanel) { actionPanel.style.display = ''; actionPanel.style.opacity = '1'; }
        if (footerBtns) footerBtns.style.display = 'flex';
        if (btnAccept) btnAccept.disabled = false;
        if (btnReject) btnReject.disabled = false;
      }

      openModal('modal-feedback-diff');
    } catch (err) {
      showToast('error', `加载 Diff 详情失败: ${err.message}`);
    }
  };

  document.addEventListener('click', async (e) => {
    const link = e.target.closest?.('.fd-download-link');
    if (!link) return;
    e.preventDefault();
    const feedbackId = link.getAttribute('data-feedback-id');
    const filename = link.getAttribute('data-filename');
    if (!feedbackId || !filename || link.dataset.downloading === 'true') return;
    link.dataset.downloading = 'true';
    link.setAttribute('aria-disabled', 'true');
    try {
      await downloadAdminAttachment(feedbackId, filename);
    } catch (err) {
      console.error('附件下载失败', err);
      showToast('error', `附件下载失败：${err.message || '请稍后重试'}`, '下载失败');
    } finally {
      delete link.dataset.downloading;
      link.removeAttribute('aria-disabled');
    }
  });

  window.doAuditFeedback = async function(actionStatus) {
    if (!currentAuditFeedbackId) return;

    const errorCat = document.getElementById('fd-error-category')?.value || 'UNSPECIFIED';
    const comment = document.getElementById('fd-review-comment')?.value.trim() || '';
    const autoRefund = document.getElementById('fd-auto-refund')?.checked ?? true;
    const createFewShot = document.getElementById('fd-create-fewshot')?.checked ?? true;

    const actionUrl = actionStatus === 'ACCEPTED'
      ? `/admin/feedbacks/${currentAuditFeedbackId}/accept`
      : `/admin/feedbacks/${currentAuditFeedbackId}/reject`;

    try {
      const res = await adminFetch(actionUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          status: actionStatus,
          error_category: errorCat,
          review_comment: comment,
          auto_refund: autoRefund,
          create_few_shot: createFewShot,
          create_benchmark: true,
        }),
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.detail || `HTTP ${res.status}`);
      }

      const resData = await res.json();
      showToast('success', resData.message || '审核处理成功！');
      closeModal('modal-feedback-diff');
      loadAdminFeedbacks();
    } catch (err) {
      showToast('error', `审核操作失败: ${err.message}`);
    }
  };

  // -------------------------------------------------------------
  // Few-Shot CRUD
  // -------------------------------------------------------------
  window.loadAdminFewShots = async function() {
    const tbody = document.querySelector('#table-admin-fewshots tbody');
    if (!tbody) return;

    try {
      const res = await adminFetch('/admin/few-shots');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const items = data.data || [];

      if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-8">暂无动态 Few-Shot 样本，点击右上角新增或通过审核采纳自动生成</td></tr>';
        return;
      }

      tbody.innerHTML = items.map(it => `
        <tr>
          <td><strong>${escapeHtml(it.title)}</strong></td>
          <td><span class="badge badge-info font-mono">${escapeHtml(it.doc_type)}</span></td>
          <td><strong class="font-mono">${it.priority}</strong></td>
          <td>
            <button type="button" class="btn btn-xs ${it.is_active ? 'btn-success' : 'btn-secondary'}" onclick="toggleFewShotActive('${it.id}', ${it.is_active})">
              ${it.is_active ? '已启用 (注入Prompt)' : '已禁用'}
            </button>
          </td>
          <td style="max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-family:var(--font-mono); font-size:0.75rem;">${escapeHtml(it.input_excerpt)}</td>
          <td class="text-muted" style="font-size:0.75rem;">${formatDate(it.updated_at || it.created_at)}</td>
          <td>
            <button class="btn btn-xs btn-danger" onclick="deleteFewShot('${it.id}')">删除</button>
          </td>
        </tr>
      `).join('');
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="7" class="text-center text-danger py-8">加载 Few-Shot 失败: ${escapeHtml(err.message)}</td></tr>`;
    }
  };

  window.openFewShotEditModal = function() {
    document.getElementById('fse-id').value = '';
    document.getElementById('fse-name').value = '';
    document.getElementById('fse-doctype').value = 'GENERAL';
    document.getElementById('fse-priority').value = '20';
    document.getElementById('fse-input').value = '';
    document.getElementById('fse-output').value = '{\n  "PortOfDischarge": "HAMBURG",\n  "BookingNo": "BK123456"\n}';
    document.getElementById('fse-active').checked = true;
    openModal('modal-few-shot-edit');
  };

  window.saveFewShotExample = async function() {
    const title = document.getElementById('fse-name').value.trim();
    const docType = document.getElementById('fse-doctype').value.trim() || 'GENERAL';
    const priority = parseInt(document.getElementById('fse-priority').value) || 20;
    const inputExcerpt = document.getElementById('fse-input').value.trim();
    const outputRaw = document.getElementById('fse-output').value.trim();
    const isActive = document.getElementById('fse-active').checked;

    if (!title || !inputExcerpt || !outputRaw) {
      showToast('warning', '请完整填写标题、输入单证片段和期望 JSON');
      return;
    }

    let parsedOutput = {};
    try {
      parsedOutput = JSON.parse(outputRaw);
    } catch (e) {
      showToast('error', '期望标准输出必须为合法的 JSON 格式: ' + e.message);
      return;
    }

    try {
      const res = await adminFetch('/admin/few-shots', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title,
          doc_type: docType,
          priority,
          input_excerpt: inputExcerpt,
          expected_output: parsedOutput,
          is_active: isActive,
        }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      showToast('success', 'Few-Shot 样本保存成功，已热加载注入 Prompt！');
      closeModal('modal-few-shot-edit');
      loadAdminFewShots();
    } catch (err) {
      showToast('error', '保存失败: ' + err.message);
    }
  };

  window.toggleFewShotActive = async function(fsId, currentActive) {
    try {
      const res = await adminFetch(`/admin/few-shots/${fsId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: !currentActive }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      showToast('success', `示例状态已更新为: ${!currentActive ? '启用' : '禁用'}`);
      loadAdminFewShots();
    } catch (err) {
      showToast('error', '状态切换失败: ' + err.message);
    }
  };

  window.deleteFewShot = async function(fsId) {
    const confirmed = await showConfirmModal({
      title: '删除 Few-Shot 样本',
      message: '确定要删除该少样本示例吗？删除后将不再注入大模型上下文。',
      confirmText: '确认删除',
      type: 'danger',
    });
    if (!confirmed) return;

    try {
      const res = await adminFetch(`/admin/few-shots/${fsId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      showToast('success', '示例已删除');
      loadAdminFewShots();
    } catch (err) {
      showToast('error', '删除失败: ' + err.message);
    }
  };

  // -------------------------------------------------------------
  // Regression Evaluation & Version Release
  // -------------------------------------------------------------
  window.triggerRegressionEvaluation = async function() {
    const btn = document.getElementById('btn-run-eval');
    const label = document.getElementById('btn-run-eval-label');
    const card = document.getElementById('eval-result-card');
    const btnRelease = document.getElementById('btn-open-release-modal');

    if (btn) btn.disabled = true;
    if (btnRelease) btnRelease.disabled = true;
    if (label) label.textContent = '评测执行中 (正在并发重跑金标用例)...';

    try {
      const res = await adminFetch('/admin/evaluation/run', { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const evalData = data.data || {};

      if (card) card.style.display = 'block';
      document.getElementById('eval-total-cases').textContent = evalData.total_cases || 0;

      const accEl = document.getElementById('eval-overall-acc');
      const accVal = evalData.overall_accuracy_percent ?? 0.0;
      accEl.textContent = `${accVal}%`;
      accEl.style.color = accVal >= 90 ? '#34d399' : (accVal >= 80 ? '#fbbf24' : '#f87171');

      const regEl = document.getElementById('eval-regressions');
      const regVal = evalData.critical_regressions_count || 0;
      regEl.textContent = `${regVal} 个`;
      regEl.style.color = regVal === 0 ? '#34d399' : '#f87171';

      document.getElementById('eval-duration').textContent = `${evalData.duration_seconds || 0}s`;

      const gateMsg = document.getElementById('eval-release-gate-msg');
      if (evalData.can_release) {
        gateMsg.style.background = 'rgba(16, 185, 129, 0.15)';
        gateMsg.style.border = '1px solid rgba(16, 185, 129, 0.4)';
        gateMsg.style.color = '#34d399';
        gateMsg.innerHTML = '✓ 全量金标回归评测通过！未检测到核心关键字段退化，准确率达标，准予发布新版本。';
        if (btnRelease) btnRelease.disabled = false;
      } else {
        gateMsg.style.background = 'rgba(239, 68, 68, 0.15)';
        gateMsg.style.border = '1px solid rgba(239, 68, 68, 0.4)';
        gateMsg.style.color = '#f87171';
        gateMsg.innerHTML = `⚠️ 评测门禁拦截: 检测到 ${regVal} 个核心关键字段回退或综合准确率不足 80%，请先调整少样本/规则后再发布。`;
        if (btnRelease) btnRelease.disabled = true;
      }

      showToast('success', '金标回归评测执行完毕！');
    } catch (err) {
      if (btnRelease) btnRelease.disabled = true;
      showToast('error', '回归评测失败: ' + err.message);
    } finally {
      if (btn) btn.disabled = false;
      if (label) label.textContent = '▶ 运行全量金标回归评测';
    }
  };

  window.openVersionReleaseModal = function() {
    const dateStr = new Date().toISOString().slice(0, 10).replace(/-/g, '');
    document.getElementById('vr-tag').value = `v3.2.${dateStr.slice(4)}`;
    document.getElementById('vr-changelog').value = '优化大模型抽取规则，注入最新采纳的 Few-Shot 纠错样本，全量金标回归评测通过。';
    document.getElementById('vr-mark-resolved').checked = true;
    openModal('modal-version-release');
  };

  window.doReleaseVersion = async function() {
    const versionTag = document.getElementById('vr-tag').value.trim();
    const changelog = document.getElementById('vr-changelog').value.trim();
    const markResolved = document.getElementById('vr-mark-resolved').checked;

    if (!versionTag) {
      showToast('warning', '请输入版本号');
      return;
    }

    try {
      const res = await adminFetch('/admin/version/release', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          version_tag: versionTag,
          changelog,
          mark_accepted_as_resolved: markResolved,
        }),
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.detail || `HTTP ${res.status}`);
      }

      const data = await res.json();
      showToast('success', data.message || '新版本发布成功！');
      closeModal('modal-version-release');
      loadAdminVersions();
      loadAdminFeedbacks();
    } catch (err) {
      showToast('error', '版本发布失败: ' + err.message);
    }
  };

  window.loadAdminVersions = async function() {
    const tbody = document.querySelector('#table-admin-versions tbody');
    if (!tbody) return;

    try {
      const res = await adminFetch('/admin/versions');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const items = data.data || [];

      if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-6">暂无版本发布记录</td></tr>';
        return;
      }

      tbody.innerHTML = items.map(v => `
        <tr>
          <td><strong class="font-mono" style="color:#38bdf8;">${escapeHtml(v.version_tag)}</strong></td>
          <td><span class="badge badge-success font-mono">${escapeHtml(v.benchmark_score)}</span></td>
          <td><strong class="font-mono">${v.passed_test_cases}/${v.total_test_cases}</strong></td>
          <td><span class="badge badge-info">${v.resolved_feedbacks_count} 条工单</span></td>
          <td style="max-width:260px; font-size:0.8rem; color:var(--text-secondary); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(v.changelog || '-')}">${escapeHtml(v.changelog || '-')}</td>
          <td class="text-muted" style="font-size:0.75rem;">${formatDate(v.released_at)}</td>
        </tr>
      `).join('');
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="6" class="text-center text-danger py-6">加载版本历史失败: ${escapeHtml(err.message)}</td></tr>`;
    }
  };

  // Logout handler
  const btnAdminLogout = document.getElementById('btn-admin-logout');
  if (btnAdminLogout) {
    btnAdminLogout.addEventListener('click', () => {
      localStorage.removeItem('cargo_admin_token');
      window.location.href = '/login';
    });
  }

  // Initial load
  loadDashboardStats();
  populateWorkbenchKeySelect();
});
