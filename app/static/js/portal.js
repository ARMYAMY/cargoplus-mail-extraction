// =========================================================================
// CargoPlus Tenant Reconciliation Portal JavaScript (Strict Isolation)
// =========================================================================

document.addEventListener('DOMContentLoaded', () => {
  let currentTenantId = '';
  let currentApiKey = localStorage.getItem('cargo_portal_api_key') || '';
  let adminToken = localStorage.getItem('cargo_admin_token') || '';
  let allTenants = [];

  const isAdmin = !!adminToken;

  // Tab switching
  const tabs = document.querySelectorAll('.tab-btn');
  const tabPanels = document.querySelectorAll('.tab-content');

  function activateTab(tab) {
    tabs.forEach((item) => {
      const isActive = item === tab;
      item.classList.toggle('active', isActive);
      item.setAttribute('aria-selected', String(isActive));
      item.tabIndex = isActive ? 0 : -1;
    });

    const target = tab.getAttribute('data-tab');
    tabPanels.forEach((panel) => {
      const isActive = panel.id === target;
      panel.classList.toggle('active', isActive);
      panel.hidden = !isActive;
    });

    if (target === 'tab-daily-statement') loadDailyStatements();
    if (target === 'tab-transactions') loadTransactions();
    if (target === 'tab-task-history') loadTenantTasks();
  }

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      activateTab(tab);
    });

    tab.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const currentIndex = Array.from(tabs).indexOf(tab);
      let nextIndex = currentIndex;
      if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % tabs.length;
      if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') nextIndex = 0;
      if (event.key === 'End') nextIndex = tabs.length - 1;
      tabs[nextIndex].focus();
      activateTab(tabs[nextIndex]);
    });
  });

  // Modal helpers
  function openModal(id) {
    const m = document.getElementById(id);
    if (m) m.classList.add('active');
  }

  function closeModal(id) {
    const m = document.getElementById(id);
    if (m) m.classList.remove('active');
  }

  document.querySelectorAll('[data-close]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const targetId = btn.getAttribute('data-close');
      closeModal(targetId);
    });
  });

  let isRedirectingPortal = false;

  function getAuthHeaders() {
    const headers = {
      'Content-Type': 'application/json',
    };
    const activeAdminToken = localStorage.getItem('cargo_admin_token') || '';
    const activeApiKey = localStorage.getItem('cargo_portal_api_key') || '';

    if (activeAdminToken) {
      headers['Authorization'] = `Bearer ${activeAdminToken}`;
      if (currentTenantId) {
        headers['X-Tenant-ID'] = currentTenantId;
      }
    } else if (activeApiKey) {
      headers['Authorization'] = `Bearer ${activeApiKey}`;
    }
    return headers;
  }

  async function portalFetch(url, options = {}) {
    const headers = new Headers(getAuthHeaders());
    new Headers(options.headers || {}).forEach((value, key) => headers.set(key, value));
    try {
      const response = await fetch(url, { ...options, headers });
      const refreshedToken = response.headers.get('X-Refreshed-Token');
      if (refreshedToken) {
        if (localStorage.getItem('cargo_admin_token')) {
          localStorage.setItem('cargo_admin_token', refreshedToken);
        } else {
          localStorage.setItem('cargo_portal_api_key', refreshedToken);
        }
      }
      if (response.status === 401 && !isRedirectingPortal) {
        isRedirectingPortal = true;
        localStorage.removeItem('cargo_admin_token');
        localStorage.removeItem('cargo_portal_api_key');
        showToast('error', '登录会话已过期，请重新登录，正在跳转...', '会话已过期');
        setTimeout(() => {
          window.location.href = '/login?expired=1';
        }, 1200);
      }
      return response;
    } catch (err) {
      throw err;
    }
  }

  // Initialize UI by Role
  async function initPortal() {
    if (isAdmin) {
      // Admin Mode: can switch any tenant
      const selectorGroup = document.getElementById('portal-tenant-selector-group');
      const adminBtn = document.getElementById('btn-portal-to-admin');
      if (selectorGroup) selectorGroup.hidden = false;
      if (adminBtn) adminBtn.hidden = false;

      try {
        const res = await portalFetch('/admin/tenants');
        if (res.ok) {
          allTenants = await res.json();
          const sel = document.getElementById('portal-tenant-select');
          sel.innerHTML = '';
          allTenants.forEach((t) => {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = `${t.name} (可用: ${formatCurrency(parseFloat(t.balance) - parseFloat(t.reserved_balance || 0))})`;
            sel.appendChild(opt);
          });

          if (allTenants.length > 0) {
            currentTenantId = allTenants[0].id;
            sel.value = currentTenantId;
          }
        }
      } catch (err) {
        console.error('Failed to load tenants list:', err);
      }
    } else {
      // Tenant Mode: locked strictly to current tenant
      const selectorGroup = document.getElementById('portal-tenant-selector-group');
      const adminBtn = document.getElementById('btn-portal-to-admin');
      if (selectorGroup) selectorGroup.hidden = true;
      if (adminBtn) adminBtn.hidden = true;
    }

    refreshAllData();
  }

  const tenantSelect = document.getElementById('portal-tenant-select');
  if (tenantSelect) {
    tenantSelect.addEventListener('change', (e) => {
      currentTenantId = e.target.value;
      refreshAllData();
    });
  }

  // Refresh all sections
  function refreshAllData() {
    loadTenantHeaderAndSummary();
    loadDailyStatements();
    loadTransactions();
    loadTenantTasks();
  }

  // 1. Header & Summary Metrics
  async function loadTenantHeaderAndSummary() {
    try {
      const res = await portalFetch('/api/v1/billing/summary');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const summary = await res.json();

      document.getElementById('tenant-display-id').textContent = summary.tenant_id;
      document.getElementById('tenant-display-balance').textContent = formatCurrency(summary.available_balance);
      document.getElementById('tenant-display-price').textContent = formatCurrency(summary.unit_price);
      document.getElementById('p-metric-recharge').textContent = formatCurrency(summary.total_recharged);
      document.getElementById('p-metric-deducted').textContent = formatCurrency(summary.total_deducted);
      document.getElementById('p-metric-success-tasks').textContent = `${summary.total_tasks_charged} 封`;

      // Update tenant name if known
      if (isAdmin && allTenants.length > 0) {
        const t = allTenants.find((item) => item.id === summary.tenant_id);
        if (t) {
          document.getElementById('tenant-display-name').textContent = t.name;
          document.getElementById('tenant-display-concurrency').textContent = t.max_concurrency;
          document.getElementById('tenant-display-email').textContent = t.contact_email || '未设置';
        }
      } else {
        document.getElementById('tenant-display-name').textContent = summary.tenant_id;
      }

      // Fetch failed tasks count
      const tasksRes = await portalFetch(`/api/v1/tasks?page=1&page_size=100`);
      if (tasksRes.ok) {
        const tasksData = await tasksRes.json();
        const failedCount = (tasksData.items || []).filter((t) => t.status === 'FAILED').length;
        document.getElementById('p-metric-failed-tasks').textContent = `${failedCount} 封`;
      }
    } catch (err) {
      console.error('Failed to load summary:', err);
    }
  }

  // 2. Daily Statements Table (with Pagination)
  let portalDailyPage = 1;
  let portalDailyPageSize = 10;
  let portalDailyTotalPages = 1;

  async function loadDailyStatements() {
    const tbody = document.querySelector('#table-daily-statement tbody');
    try {
      const res = await portalFetch(`/api/v1/billing/statements/daily?days=90&page=${portalDailyPage}&page_size=${portalDailyPageSize}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      if (data.tenant_name) {
        document.getElementById('tenant-display-name').textContent = data.tenant_name;
      }

      portalDailyTotalPages = data.total_pages || Math.max(1, Math.ceil((data.total || 0) / portalDailyPageSize));
      if (document.getElementById('portal-daily-total-count')) document.getElementById('portal-daily-total-count').textContent = data.total || (data.items || []).length;
      if (document.getElementById('portal-daily-curr-page')) document.getElementById('portal-daily-curr-page').textContent = portalDailyPage;
      if (document.getElementById('portal-daily-total-pages')) document.getElementById('portal-daily-total-pages').textContent = portalDailyTotalPages;

      if (document.getElementById('portal-daily-btn-prev')) document.getElementById('portal-daily-btn-prev').disabled = portalDailyPage <= 1;
      if (document.getElementById('portal-daily-btn-next')) document.getElementById('portal-daily-btn-next').disabled = portalDailyPage >= portalDailyTotalPages;

      if (!data.items || data.items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-8">暂无对账周期汇总数据</td></tr>';
        return;
      }

      tbody.innerHTML = data.items
        .map((item) => {
          const deductAmt = parseFloat(item.deduction_amount);
          const rechargeAmt = parseFloat(item.recharge_amount);
          const closingBal = parseFloat(item.closing_balance);
          const avgDeduct = item.deduction_count > 0 ? deductAmt / item.deduction_count : 0.5;
          const hasInvalidMoney = ![deductAmt, rechargeAmt, closingBal, avgDeduct].every(isValidMoney);

          return `
          <tr>
            <td><strong>${item.date}</strong></td>
            <td><span class="badge badge-success">${item.deduction_count} 次</span></td>
            <td><strong class="text-danger">${formatCurrency(deductAmt, '-')}</strong></td>
            <td>${item.recharge_count > 0 ? `<span class="badge badge-info">${item.recharge_count} 笔</span>` : '<span class="text-muted">0</span>'}</td>
            <td>${rechargeAmt > 0 ? `<strong class="text-success">${formatCurrency(rechargeAmt, '+')}</strong>` : '<span class="text-muted">¥0.00</span>'}</td>
            <td><strong class="font-mono">${formatCurrency(closingBal)}</strong></td>
            <td>${formatCurrency(avgDeduct)} / 次</td>
            <td>${hasInvalidMoney ? '<span class="badge badge-warning">! 金额异常</span>' : '<span class="badge badge-success">✓ 账目一致</span>'}</td>
          </tr>
        `;
        })
        .join('');
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center text-danger py-8">加载对账单失败: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  document.getElementById('portal-daily-btn-prev')?.addEventListener('click', () => {
    if (portalDailyPage > 1) {
      portalDailyPage--;
      loadDailyStatements();
    }
  });

  document.getElementById('portal-daily-btn-next')?.addEventListener('click', () => {
    if (portalDailyPage < portalDailyTotalPages) {
      portalDailyPage++;
      loadDailyStatements();
    }
  });

  document.getElementById('portal-daily-page-size')?.addEventListener('change', (e) => {
    portalDailyPageSize = parseInt(e.target.value) || 10;
    portalDailyPage = 1;
    loadDailyStatements();
  });

  // 3. Transactions Ledger Table (with Pagination)
  let portalTxPage = 1;
  let portalTxPageSize = 10;
  let portalTxTotalPages = 1;

  async function loadTransactions() {
    const tbody = document.querySelector('#table-tx-details tbody');
    const typeFilter = document.getElementById('filter-tx-type')?.value;

    let url = `/api/v1/billing/transactions?page=${portalTxPage}&page_size=${portalTxPageSize}`;
    if (typeFilter) url += `&type=${typeFilter}`;

    try {
      const res = await portalFetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const txs = data.items || data;

      portalTxTotalPages = data.total_pages || Math.max(1, Math.ceil((data.total || txs.length) / portalTxPageSize));
      if (document.getElementById('portal-tx-total-count')) document.getElementById('portal-tx-total-count').textContent = data.total !== undefined ? data.total : txs.length;
      if (document.getElementById('portal-tx-curr-page')) document.getElementById('portal-tx-curr-page').textContent = portalTxPage;
      if (document.getElementById('portal-tx-total-pages')) document.getElementById('portal-tx-total-pages').textContent = portalTxTotalPages;

      if (document.getElementById('portal-tx-btn-prev')) document.getElementById('portal-tx-btn-prev').disabled = portalTxPage <= 1;
      if (document.getElementById('portal-tx-btn-next')) document.getElementById('portal-tx-btn-next').disabled = portalTxPage >= portalTxTotalPages;

      if (!txs || txs.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-8">暂无交易流水记录</td></tr>';
        return;
      }

      tbody.innerHTML = txs
        .map((tx) => {
          const isDeduct = tx.type === 'DEDUCTION';
          const typeBadge = isDeduct ? '<span class="badge badge-danger">API 扣费</span>' : '<span class="badge badge-success">账户充值</span>';
          const amountText = isDeduct
            ? `<strong class="text-danger">${formatCurrency(tx.amount, '-')}</strong>`
            : `<strong class="text-success">${formatCurrency(tx.amount, '+')}</strong>`;

          return `
          <tr>
            <td class="font-mono" style="font-size:0.8rem;">${tx.id}</td>
            <td>${typeBadge}</td>
            <td>${amountText}</td>
            <td class="font-mono">${formatCurrency(tx.balance_before)}</td>
            <td class="font-mono"><strong>${formatCurrency(tx.balance_after)}</strong></td>
            <td class="font-mono" style="font-size:0.8rem;">${tx.task_id ? `<a href="javascript:void(0)" onclick="viewTaskDetail('${tx.task_id}')">${tx.task_id}</a>` : '-'}</td>
            <td>${escapeHtml(tx.description || '-')}</td>
            <td class="text-muted">${formatDate(tx.created_at)}</td>
          </tr>
        `;
        })
        .join('');
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center text-danger py-8">加载流水失败: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  document.getElementById('portal-tx-btn-prev')?.addEventListener('click', () => {
    if (portalTxPage > 1) {
      portalTxPage--;
      loadTransactions();
    }
  });

  document.getElementById('portal-tx-btn-next')?.addEventListener('click', () => {
    if (portalTxPage < portalTxTotalPages) {
      portalTxPage++;
      loadTransactions();
    }
  });

  document.getElementById('portal-tx-page-size')?.addEventListener('change', (e) => {
    portalTxPageSize = parseInt(e.target.value) || 10;
    portalTxPage = 1;
    loadTransactions();
  });

  document.getElementById('filter-tx-type')?.addEventListener('change', () => {
    portalTxPage = 1;
    loadTransactions();
  });

  // 4. Task History Table (with Pagination)
  let portalTaskPage = 1;
  let portalTaskPageSize = 10;
  let portalTaskTotalPages = 1;

  async function loadTenantTasks() {
    const tbody = document.querySelector('#table-portal-tasks tbody');
    const statusFilter = document.getElementById('filter-portal-task-status')?.value;

    let url = `/api/v1/tasks?page=${portalTaskPage}&page_size=${portalTaskPageSize}`;
    if (statusFilter) url += `&status=${statusFilter}`;

    try {
      const res = await portalFetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const items = data.items || [];

      portalTaskTotalPages = Math.max(1, Math.ceil((data.total || items.length) / portalTaskPageSize));
      if (document.getElementById('portal-task-count-info')) document.getElementById('portal-task-count-info').textContent = `共 ${data.total || items.length} 条记录`;
      if (document.getElementById('portal-task-total-count')) document.getElementById('portal-task-total-count').textContent = data.total || items.length;
      if (document.getElementById('portal-task-curr-page')) document.getElementById('portal-task-curr-page').textContent = portalTaskPage;
      if (document.getElementById('portal-task-total-pages')) document.getElementById('portal-task-total-pages').textContent = portalTaskTotalPages;

      if (document.getElementById('portal-task-btn-prev')) document.getElementById('portal-task-btn-prev').disabled = portalTaskPage <= 1;
      if (document.getElementById('portal-task-btn-next')) document.getElementById('portal-task-btn-next').disabled = portalTaskPage >= portalTaskTotalPages;

      if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-8">暂无邮件处理记录</td></tr>';
        return;
      }

      tbody.innerHTML = items
        .map((task) => {
          let statusBadge = '';
          if (task.status === 'SUCCESS') statusBadge = '<span class="badge badge-success">成功</span>';
          else if (task.status === 'FAILED') statusBadge = '<span class="badge badge-danger">失败</span>';
          else statusBadge = '<span class="badge badge-warning">处理中</span>';

          const chargeText = task.is_charged ? `<strong class="text-danger">-¥${parseFloat(task.charged_amount).toFixed(2)}</strong>` : '<span class="text-muted">¥0.00 (免扣费)</span>';
          const durationText = task.duration_ms ? `${task.duration_ms} ms` : '-';

          return `
          <tr>
            <td class="font-mono" style="font-size:0.8rem;"><strong>${task.id}</strong></td>
            <td>${escapeHtml(task.mail_subject || task.input_summary || '-')}</td>
            <td>${statusBadge}</td>
            <td>${durationText}</td>
            <td>${chargeText}</td>
            <td class="text-muted">${formatDate(task.created_at)}</td>
            <td>
              <button class="btn btn-sm btn-secondary" onclick="viewTaskDetail('${task.id}')">查看抽取结果</button>
            </td>
          </tr>
        `;
        })
        .join('');
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="7" class="text-center text-danger py-8">加载任务失败: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  document.getElementById('portal-task-btn-prev')?.addEventListener('click', () => {
    if (portalTaskPage > 1) {
      portalTaskPage--;
      loadTenantTasks();
    }
  });

  document.getElementById('portal-task-btn-next')?.addEventListener('click', () => {
    if (portalTaskPage < portalTaskTotalPages) {
      portalTaskPage++;
      loadTenantTasks();
    }
  });

  document.getElementById('portal-task-page-size')?.addEventListener('change', (e) => {
    portalTaskPageSize = parseInt(e.target.value) || 10;
    portalTaskPage = 1;
    loadTenantTasks();
  });

  document.getElementById('filter-portal-task-status')?.addEventListener('change', () => {
    portalTaskPage = 1;
    loadTenantTasks();
  });


  // 5. Task Detail Inspector & Inline Feedback
  let currentDetailTaskId = null;
  let currentTaskOriginalJson = {};
  let currentTaskEditedJson = {};
  const invalidTaskFieldKeys = new Set();

  window.switchTaskDetailView = function(viewName) {
    const viewFields = document.getElementById('ptd-view-fields');
    const viewJson = document.getElementById('ptd-view-json');
    const tabFields = document.getElementById('ptd-tab-fields');
    const tabJson = document.getElementById('ptd-tab-json');

    if (viewName === 'fields') {
      if (viewFields) viewFields.style.display = 'block';
      if (viewJson) viewJson.style.display = 'none';
      if (tabFields) { tabFields.className = 'btn btn-sm btn-primary'; }
      if (tabJson) { tabJson.className = 'btn btn-sm btn-secondary'; }
    } else {
      if (viewFields) viewFields.style.display = 'none';
      if (viewJson) viewJson.style.display = 'block';
      if (tabFields) { tabFields.className = 'btn btn-sm btn-secondary'; }
      if (tabJson) { tabJson.className = 'btn btn-sm btn-primary'; }
    }
  };

  function updateDiffCounter() {
    let diffCount = 0;
    const allKeys = new Set([...Object.keys(currentTaskOriginalJson), ...Object.keys(currentTaskEditedJson)]);
    allKeys.forEach(k => {
      const origVal = currentTaskOriginalJson[k];
      const editVal = currentTaskEditedJson[k];
      if (JSON.stringify(origVal) !== JSON.stringify(editVal)) {
        diffCount++;
      }
    });
    const counterEl = document.getElementById('ptd-diff-counter');
    if (counterEl) {
      if (diffCount > 0) {
        counterEl.textContent = `已修改 ${diffCount} 个字段 (有变更)`;
        counterEl.style.color = '#fbbf24';
      } else {
        counterEl.textContent = '未作修改 (与原始一致)';
        counterEl.style.color = '#38bdf8';
      }
    }
  }

  function renderTaskFieldsTable(jsonObj) {
    const tbody = document.getElementById('tbody-ptd-fields');
    if (!tbody) return;

    if (!jsonObj || Object.keys(jsonObj).length === 0) {
      tbody.innerHTML = '<tr><td colspan="2" class="text-center text-muted py-6">暂无结构化字段数据</td></tr>';
      return;
    }

    invalidTaskFieldKeys.clear();
    const rowsHtml = Object.entries(jsonObj).map(([key, val]) => {
      const isComplex = typeof val === 'object' && val !== null;
      const displayVal = isComplex ? JSON.stringify(val, null, 2) : (val !== null && val !== undefined ? String(val) : '');
      const isOriginalSame = JSON.stringify(currentTaskOriginalJson[key]) === JSON.stringify(currentTaskEditedJson[key]);

      return `
        <tr data-key="${escapeHtml(key)}" style="${!isOriginalSame ? 'background: rgba(245, 158, 11, 0.08);' : ''}">
          <td style="font-weight:600; color:#38bdf8; font-family:var(--font-mono); vertical-align:middle;">
            ${escapeHtml(key)}
            ${!isOriginalSame ? '<span class="badge badge-warning" style="margin-left:4px; font-size:0.7rem;">已改</span>' : ''}
          </td>
          <td>
            ${isComplex
              ? `<textarea class="form-control font-mono ptd-field-input" data-key="${escapeHtml(key)}" rows="3" style="width:100%; font-size:0.8rem; background:rgba(0,0,0,0.3); border:1px solid ${!isOriginalSame ? '#fbbf24' : 'rgba(255,255,255,0.1)'}; color:#fff; border-radius:6px; padding:6px 10px;">${escapeHtml(displayVal)}</textarea>`
              : `<input type="text" class="form-control ptd-field-input" data-key="${escapeHtml(key)}" value="${escapeHtml(displayVal)}" style="width:100%; font-size:0.84rem; background:rgba(0,0,0,0.3); border:1px solid ${!isOriginalSame ? '#fbbf24' : 'rgba(255,255,255,0.1)'}; color:#fff; border-radius:6px; padding:6px 10px;">`
            }
          </td>
        </tr>
      `;
    }).join('');

    tbody.innerHTML = rowsHtml;

    // Attach change listeners
    tbody.querySelectorAll('.ptd-field-input').forEach(input => {
      input.addEventListener('input', (e) => {
        const key = e.target.getAttribute('data-key');
        let newVal = e.target.value;
        if (typeof currentTaskOriginalJson[key] === 'object' && currentTaskOriginalJson[key] !== null) {
          try {
            newVal = JSON.parse(newVal);
            invalidTaskFieldKeys.delete(key);
            e.target.setCustomValidity('');
            e.target.style.borderColor = '';
          } catch (_) {
            invalidTaskFieldKeys.add(key);
            e.target.setCustomValidity('请输入合法 JSON');
            e.target.style.borderColor = '#ef4444';
            return;
          }
        }
        currentTaskEditedJson[key] = newVal;
        updateDiffCounter();

        // Update row highlight
        const row = e.target.closest('tr');
        if (row) {
          const isSame = JSON.stringify(currentTaskOriginalJson[key]) === JSON.stringify(currentTaskEditedJson[key]);
          row.style.background = isSame ? '' : 'rgba(245, 158, 11, 0.08)';
          e.target.style.borderColor = isSame ? 'rgba(255,255,255,0.1)' : '#fbbf24';
        }
      });
    });

    updateDiffCounter();
  }

  window.resetTaskFieldModifications = function() {
    currentTaskEditedJson = JSON.parse(JSON.stringify(currentTaskOriginalJson));
    renderTaskFieldsTable(currentTaskEditedJson);
    showToast('info', '已重置所有字段为系统提取原值');
  };

  window.viewTaskDetail = async function (taskId) {
    currentDetailTaskId = taskId;
    try {
      const res = await portalFetch(`/api/v1/tasks/${taskId}`);
      if (!res.ok) throw new Error('任务查询失败');
      const task = await res.json();

      document.getElementById('ptd-id').textContent = task.id;
      document.getElementById('ptd-status-badge').innerHTML = task.status === 'SUCCESS' ? '<span class="badge badge-success">成功</span>' : '<span class="badge badge-danger">失败</span>';
      document.getElementById('ptd-duration').textContent = task.duration_ms ? `${task.duration_ms} ms` : '-';
      document.getElementById('ptd-charge').innerHTML = task.is_charged ? `<strong class="text-danger">¥${parseFloat(task.charged_amount).toFixed(2)}</strong>` : '<span class="text-muted">¥0.00 (免扣费)</span>';

      const jsonCode = document.getElementById('ptd-json-code');
      const resJson = task.result_json || {};
      currentTaskOriginalJson = JSON.parse(JSON.stringify(resJson));
      currentTaskEditedJson = JSON.parse(JSON.stringify(resJson));

      if (task.result_json) {
        jsonCode.textContent = JSON.stringify(task.result_json, null, 2);
      } else if (task.error_message) {
        jsonCode.textContent = `// 错误日志:\n${task.error_message}`;
      } else {
        jsonCode.textContent = '// 暂无提取结果';
      }

      // Render editable fields table
      renderTaskFieldsTable(currentTaskEditedJson);
      switchTaskDetailView('fields');

      // Check existing feedback status for this task
      const feedbackBanner = document.getElementById('ptd-feedback-banner');
      if (feedbackBanner) {
        try {
          const fbRes = await portalFetch(`/api/v1/tasks/${taskId}/feedback`);
          if (fbRes.ok) {
            const fbJson = await fbRes.json();
            const fb = fbJson.data;
            if (fb) {
              feedbackBanner.style.display = 'block';
              if (fb.status === 'ACCEPTED') {
                feedbackBanner.style.background = 'rgba(16, 185, 129, 0.15)';
                feedbackBanner.style.border = '1px solid rgba(16, 185, 129, 0.4)';
                feedbackBanner.innerHTML = `<strong class="text-success">✓ 纠错已采纳</strong>: 管理员已确认提取错误并采纳${fb.is_refunded ? `，<strong class="text-danger">已退款冲正 ¥${fb.refund_amount}</strong>` : ''}。审核批注: ${escapeHtml(fb.review_comment || '无')}`;
              } else if (fb.status === 'RESOLVED') {
                feedbackBanner.style.background = 'rgba(56, 189, 248, 0.15)';
                feedbackBanner.style.border = '1px solid rgba(56, 189, 248, 0.4)';
                feedbackBanner.innerHTML = `<strong style="color:#38bdf8;">✓ 已优化发布 (${escapeHtml(fb.resolved_version || '最新版本')})</strong>: 该单证问题已通过动态知识库/规则发布修复！`;
              } else if (fb.status === 'REJECTED') {
                feedbackBanner.style.background = 'rgba(239, 68, 68, 0.15)';
                feedbackBanner.style.border = '1px solid rgba(239, 68, 68, 0.4)';
                feedbackBanner.innerHTML = `<strong class="text-danger">✗ 反馈已驳回</strong>: 批注原因: ${escapeHtml(fb.review_comment || '系统提取符合原件')}`;
              } else {
                feedbackBanner.style.background = 'rgba(245, 158, 11, 0.15)';
                feedbackBanner.style.border = '1px solid rgba(245, 158, 11, 0.4)';
                feedbackBanner.innerHTML = `<strong class="text-warning">⏳ 纠错反馈待审核</strong>: 您已提交纠错反馈 (包含 ${fb.diff_fields ? fb.diff_fields.length : 0} 处修改)，管理员核实后，符合原始扣款条件的任务将退款。`;
              }
            } else {
              feedbackBanner.style.display = 'none';
            }
          }
        } catch (_) {
          feedbackBanner.style.display = 'none';
        }
      }

      openModal('modal-portal-task-detail');
    } catch (err) {
      showToast('error', `查看任务详情失败: ${err.message}`);
    }
  };

  window.submitTaskFeedback = async function() {
    if (!currentDetailTaskId) return;

    if (invalidTaskFieldKeys.size > 0) {
      showToast('warning', `以下复合字段不是合法 JSON：${Array.from(invalidTaskFieldKeys).join(', ')}`);
      return;
    }

    let diffCount = 0;
    const allKeys = new Set([...Object.keys(currentTaskOriginalJson), ...Object.keys(currentTaskEditedJson)]);
    allKeys.forEach(k => {
      if (JSON.stringify(currentTaskOriginalJson[k]) !== JSON.stringify(currentTaskEditedJson[k])) {
        diffCount++;
      }
    });

    const notesInput = document.getElementById('ptd-feedback-notes');
    const notes = notesInput ? notesInput.value.trim() : '';

    if (diffCount === 0 && !notes) {
      showToast('warning', '您尚未修改任何字段，且未填写问题说明');
      return;
    }

    const confirmed = await showConfirmModal({
      title: '提交纠错反馈确认',
      message: `您修改了 ${diffCount} 个字段。提交后管理员将在后台审核；确认为系统错误且存在原始扣款流水时，将原路退还本次调用费用并进入优化流程。是否确认提交？`,
      confirmText: '立即提交反馈',
      cancelText: '再看看',
      type: 'warning',
    });
    if (!confirmed) return;

    try {
      const btn = document.getElementById('btn-submit-task-feedback');
      if (btn) { btn.disabled = true; btn.textContent = '提交中...'; }

      const res = await portalFetch(`/api/v1/tasks/${currentDetailTaskId}/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          corrected_result: currentTaskEditedJson,
          notes: notes,
        }),
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.detail || `HTTP ${res.status}`);
      }

      const resData = await res.json();
      showToast('success', resData.message || '纠错反馈提交成功！');

      // Refresh task detail view
      await window.viewTaskDetail(currentDetailTaskId);
    } catch (err) {
      showToast('error', `提交反馈失败: ${err.message}`);
    } finally {
      const btn = document.getElementById('btn-submit-task-feedback');
      if (btn) { btn.disabled = false; btn.textContent = '提交纠错反馈'; }
    }
  };

  document.getElementById('ptd-btn-copy-json')?.addEventListener('click', () => {
    const text = document.getElementById('ptd-json-code').textContent;
    copyToClipboard(text, '任务 JSON 结果已复制！');
  });

  // 6. CSV Export triggers
  async function triggerCsvDownload(url) {
    try {
      const res = await portalFetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blobUrl = URL.createObjectURL(await res.blob());
      const link = document.createElement('a');
      link.href = blobUrl;
      link.download = `cargo_billing_statement_${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(blobUrl);
      showToast('success', '财务对账单 CSV 文件已开始下载！');
    } catch (err) {
      showToast('error', `导出失败: ${err.message}`);
    }
  }

  document.getElementById('btn-export-csv')?.addEventListener('click', () => {
    triggerCsvDownload('/api/v1/billing/export-csv');
  });

  document.getElementById('btn-export-tx-csv')?.addEventListener('click', () => {
    const typeFilter = document.getElementById('filter-tx-type')?.value;
    let url = '/api/v1/billing/export-csv';
    if (typeFilter) url += `?type=${typeFilter}`;
    triggerCsvDownload(url);
  });

  // 7. Tenant API Key Management Logic
  window.openPortalKeysModal = async function() {
    const modal = document.getElementById('modal-portal-keys');
    if (modal) modal.classList.add('active');
    document.getElementById('portal-new-key-alert').style.display = 'none';

    const curlSnippet = `curl -X POST "http://localhost:8000/api/v1/extract/sync" \\
  -H "Authorization: Bearer <YOUR_API_KEY>" \\
  -H "Content-Type: application/json" \\
  -d '{"mail_subject": "Booking...", "mail_body": "Please book shipment", "attachments": []}'`;
    document.getElementById('portal-keys-curl-code').textContent = curlSnippet;

    await loadPortalKeysList();
  };

  window.closePortalModal = function(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.remove('active');
  };

  window.copyPortalText = function(text) {
    if (!text) return;
    copyToClipboard(text, 'API 凭证已成功复制！');
  };

  async function loadPortalKeysList() {
    const tbody = document.querySelector('#table-portal-keys-list tbody');
    tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted py-4">正在查询密钥...</td></tr>';

    try {
      const res = await portalFetch('/api/v1/tenants/me/keys');
      if (!res.ok) throw new Error('查询密钥失败');
      const keys = await res.json();

      if (keys.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted py-4">暂未查询到 API Key，请点击上方按钮生成</td></tr>';
        return;
      }

      tbody.innerHTML = keys.map(k => {
        const fullKey = k.raw_api_key || k.raw_key || k.key_prefix;
        const displayKey = k.key_prefix ? `${k.key_prefix}...` : (fullKey ? `${fullKey.substring(0, 11)}...` : '-');
        return `
        <tr>
          <td style="white-space:nowrap;"><strong>${escapeHtml(k.name || '默认密钥')}</strong></td>
          <td style="white-space:nowrap;">
            <div style="display:inline-flex; align-items:center; gap:8px;">
              <code style="color:#38bdf8; font-size:0.82rem; font-family:var(--font-mono);">${escapeHtml(displayKey)}</code>
              ${fullKey ? `<button type="button" class="btn btn-xs btn-secondary" onclick="copyPortalText('${escapeHtml(fullKey)}')" title="复制完整 API Key">复制</button>` : ''}
            </div>
          </td>
          <td style="white-space:nowrap;">
            <div style="display:inline-flex; align-items:center; gap:8px;">
              <span style="font-family:var(--font-mono); font-size:0.8rem; color:#fbbf24;">${escapeHtml(k.api_secret ? k.api_secret.substring(0, 10) + '...' : '-')}</span>
              ${k.api_secret ? `<button type="button" class="btn btn-xs btn-secondary" onclick="copyPortalText('${escapeHtml(k.api_secret)}')" title="复制完整 Webhook Secret">复制</button>` : ''}
            </div>
          </td>
          <td style="white-space:nowrap; text-align:center;">${k.is_active ? '<span class="badge badge-success">正常</span>' : '<span class="badge badge-danger">已禁用</span>'}</td>
          <td class="text-muted" style="font-size:0.75rem; white-space:nowrap;">${formatDate(k.created_at)}</td>
        </tr>
      `;
      }).join('');
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="5" class="text-center text-danger py-4">加载失败: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  // Tenant Generate New Key
  document.getElementById('btn-portal-gen-key')?.addEventListener('click', async () => {
    const keyName = await showPromptModal({
      title: '🔑 生成企业 API Key',
      label: '请输入新密钥用途说明 (例如: ERP 系统对接):',
      placeholder: '例如: 货代 ERP 系统对接',
      defaultValue: '自动化集成 Key',
      confirmText: '立即生成',
    });
    if (keyName === null || !keyName.trim()) return;

    try {
      const res = await portalFetch(`/api/v1/tenants/me/keys?key_name=${encodeURIComponent(keyName.trim())}`, {
        method: 'POST',
      });
      if (!res.ok) throw new Error('生成新密钥失败');
      const data = await res.json();

      document.getElementById('portal-new-key-alert').style.display = 'block';
      document.getElementById('portal-new-key-val').value = data.raw_api_key || '';
      document.getElementById('portal-new-key-secret').textContent = data.api_secret || '';

      showToast('success', '新 API Key 凭证生成成功，请及时复制！');
      await loadPortalKeysList();
    } catch (err) {
      showToast('error', `生成失败: ${err.message}`);
    }
  });

  // -------------------------------------------------------------
  // Custom UI Dialog & Toast Helpers (Portal)
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
  const MAX_DISPLAYABLE_MONEY = 99999999.9999;

  function isValidMoney(value) {
    const amount = Number(value);
    return Number.isFinite(amount) && Math.abs(amount) <= MAX_DISPLAYABLE_MONEY;
  }

  function formatCurrency(value, sign = '') {
    if (!isValidMoney(value)) return '金额异常';
    const amount = Math.abs(Number(value));
    return `${sign}¥${amount.toLocaleString('zh-CN', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }

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

  // Start initialization
  initPortal();
});
