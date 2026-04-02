(function () {
  const endpoint = "/api/v1/supplier/dashboard";
  const refreshIntervalMs = 60000;
  const initialNode = document.getElementById("supplier-dashboard-initial");
  let isRefreshing = false;
  let lastPayload = null;
  const demoRevenue = [
    { m: "Окт", val: 12.4 },
    { m: "Ноя", val: 18.1 },
    { m: "Дек", val: 15.6 },
    { m: "Янв", val: 22.3 },
    { m: "Фев", val: 28.7 },
    { m: "Мар", val: 24.1 },
  ];
  const demoSla = [
    { m: "Окт", compliance: 94 },
    { m: "Ноя", compliance: 91 },
    { m: "Дек", compliance: 96 },
    { m: "Янв", compliance: 93 },
    { m: "Фев", compliance: 97 },
    { m: "Мар", compliance: 95 },
  ];
  const demoMetrics = [
    { label: "Активные заказы", value: "45", trend: "+12%", trend_tone: "up" },
    { label: "SLA compliance", value: "95.2%", trend: "+2.1%", trend_tone: "up" },
    { label: "Конверсия", value: "68%", trend: "-3%", trend_tone: "down" },
    { label: "Рейтинг", value: "4.26", trend: "+0.02", trend_tone: "up" },
  ];
  const demoOrderStatuses = [
    { label: "Производство", count: 12, status_key: "production" },
    { label: "Готов к отгрузке", count: 8, status_key: "ready_to_ship" },
    { label: "Отгружено", count: 15, status_key: "shipped" },
    { label: "Доставлено", count: 6, status_key: "delivered" },
    { label: "Ожидает резерв", count: 4, status_key: "awaiting_reserve" },
  ];
  const demoRequests = [
    { id: "RQ-4821", part: "Гидроцилиндр 707-01-0K930", client: "Полюс Золото", type: "Срочный", time: "12 мин назад", brand: "Komatsu", request_type: "urgent", url: "/seller/requests/" },
    { id: "RQ-4819", part: "Турбокомпрессор 6505-67-5030", client: "СУЭК", type: "Стандартный", time: "48 мин назад", brand: "Komatsu", request_type: "standard", url: "/seller/requests/" },
    { id: "RQ-4817", part: "Насос гидравлический по чертежу", client: "Норникель", type: "По чертежу", time: "1.5 ч назад", brand: "Hitachi", request_type: "drawing", url: "/seller/requests/" },
    { id: "RQ-4815", part: "Фильтр 600-185-4100", client: "Евраз", type: "Стандартный", time: "2 ч назад", brand: "Komatsu", request_type: "standard", url: "/seller/requests/" },
  ];
  const demoEvents = [
    { text: "Заказ #ORD-3847 отгружен", detail: "Komatsu PC800 → Полюс Золото", time: "09:14", status: "success" },
    { text: "SLA: дедлайн по #ORD-3832", detail: "Проверка качества — 4ч", time: "08:51", status: "warning" },
    { text: "Резерв ¥2.34M получен", detail: "#ORD-3851 подтверждён", time: "08:30", status: "info" },
    { text: "Рейтинг пересчитан: 4.72 → 4.74", detail: "Закрытие #ORD-3829 + отзыв клиента", time: "08:12", status: "success" },
    { text: "Новый запрос от Норникель", detail: "Насос по чертежу", time: "07:45", status: "info" },
    { text: "SLA нарушение зафиксировано", detail: "#ORD-3801 — просрочка +2д", time: "Вчера", status: "danger" },
  ];

  const hasPositiveValues = function (arr, key) {
    if (!Array.isArray(arr) || !arr.length) return false;
    for (let i = 0; i < arr.length; i += 1) {
      const n = Number(arr[i] && arr[i][key]);
      if (Number.isFinite(n) && n > 0) return true;
    }
    return false;
  };

  const normalizeRevenueSeries = function (series) {
    const src = Array.isArray(series) ? series : [];
    const mapped = src.map(function (r) {
      const val = Number(
        r && Object.prototype.hasOwnProperty.call(r, "val")
          ? r.val
          : (r && Object.prototype.hasOwnProperty.call(r, "value") ? r.value : 0)
      );
      return {
        m: (r && (r.m || r.month || r.label)) || "",
        val: Number.isFinite(val) ? val : 0,
      };
    }).filter(function (r) { return r.m; });
    if (!mapped.length || !hasPositiveValues(mapped, "val")) return demoRevenue;
    return mapped;
  };

  const normalizeSlaSeries = function (series) {
    const src = Array.isArray(series) ? series : [];
    const mapped = src.map(function (r) {
      const raw = r && Object.prototype.hasOwnProperty.call(r, "compliance")
        ? r.compliance
        : (r && Object.prototype.hasOwnProperty.call(r, "v") ? r.v : 0);
      const v = Number(raw);
      return {
        m: (r && (r.m || r.month || r.label)) || "",
        compliance: Number.isFinite(v) ? v : 0,
      };
    }).filter(function (r) { return r.m; });
    if (!mapped.length || !hasPositiveValues(mapped, "compliance")) return demoSla;
    return mapped;
  };

  const normalizeMetricsCards = function (cards) {
    const src = Array.isArray(cards) ? cards : [];
    if (!src.length) return demoMetrics;
    const mapped = src.map(function (c) {
      return {
        label: c && c.label ? c.label : "",
        value: c && c.value != null ? String(c.value) : "—",
        trend: c && c.trend ? c.trend : "",
        trend_tone: c && c.trend_tone ? c.trend_tone : ((c && c.trendUp) ? "up" : "down"),
      };
    }).filter(function (c) { return c.label; });
    return mapped.length ? mapped : demoMetrics;
  };

  const escapeHtml = function (value) {
    const s = String(value == null ? "" : value);
    return s
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  };

  const renderHeader = function (payload, opts) {
    const header = payload && payload.header ? payload.header : {};
    const heroValueNode = document.getElementById("dashboard-hero-value");
    const heroTrendNode = document.getElementById("dashboard-hero-trend");
    const heroTrendSubNode = document.getElementById("dashboard-hero-trend-sub");

    const updatedAtRaw = payload && payload.updated_at ? String(payload.updated_at) : "";
    if (heroValueNode) {
      const revenueSeries = normalizeRevenueSeries((payload && payload.revenue_series) || []);
      const current = revenueSeries.length ? Number(revenueSeries[revenueSeries.length - 1].val || 0) : 0;
      const prev = revenueSeries.length > 1 ? Number(revenueSeries[revenueSeries.length - 2].val || 0) : 0;
      const delta = prev > 0 ? ((current - prev) / prev) * 100 : 0;
      heroValueNode.textContent = "¥" + current.toFixed(1) + "M";
      if (heroTrendNode) heroTrendNode.textContent = (delta >= 0 ? "+" : "") + delta.toFixed(1) + "%";
      if (heroTrendSubNode) heroTrendSubNode.textContent = "vs ¥" + prev.toFixed(1) + "M";
    }

    const subtitleNode = document.getElementById("dashboard-subtitle");
    if (subtitleNode) {
      const statusText = (opts && opts.statusText) ? String(opts.statusText) : "";
      let updatedAtText = "—";
      if (updatedAtRaw) {
        const d = new Date(updatedAtRaw);
        updatedAtText = Number.isNaN(d.getTime()) ? updatedAtRaw : d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
      }
      const company = header.company_name || payload.company || "Компания";
      subtitleNode.innerHTML = "Компания: " + escapeHtml(company) + " · Обновлено: " + escapeHtml(updatedAtText) + (statusText ? (' <span class="muted">(' + escapeHtml(statusText) + ")</span>") : "");
    }
  };

  const severityTitle = function (severity) {
    if (severity === "critical") return "Критично";
    if (severity === "warning") return "Внимание";
    return "Норма";
  };

  const renderActionCards = function (payload) {
    const root = document.getElementById("dashboard-action-cards");
    if (!root) return;
    const cards = payload.action_cards || payload.widgets || [];
    if (!cards.length) {
      root.innerHTML =
        '<article class="card card-body dashboard-action-card">' +
        '<p class="muted">Нет активных тревожных карточек</p>' +
        '<p class="price">0</p>' +
        '<p class="muted">Система под контролем</p>' +
        "</article>";
      return;
    }
    const accentByKey = {
      orders: "#7F77DD",
      catalog_updates: "#1D9E75",
      new_rfqs: "#378ADD",
      import_errors: "#BA7517",
    };

    root.innerHTML = cards
      .map(function (card) {
        const key = card.key || "";
        const accent = accentByKey[key] || "#7eaef0";
        const value = card.value == null ? 0 : card.value;
        const subParts = [];
        if (key === "orders" && card.risk != null) subParts.push("SLA риск: " + String(card.risk));
        if (key === "catalog_updates") subParts.push("за 24 часа");
        if (key === "new_rfqs") subParts.push("новые");
        if (key === "import_errors") subParts.push("за 30 дней");

        const sub = subParts.length ? '<span class="dashboard-metric-sub">' + escapeHtml(subParts.join(" • ")) + "</span>" : "";
        const cta = card.cta && card.cta.url
          ? '<a class="dashboard-metric-cta" href="' + escapeHtml(card.cta.url) + '">' + escapeHtml(card.cta.label || "Открыть") + "</a>"
          : "";

        return (
          '<article class="dashboard-metric-card">' +
          '<div class="dashboard-metric-accent" style="background:' + escapeHtml(accent) + '"></div>' +
          '<div class="dashboard-metric-label">' + escapeHtml(card.label || card.key || "Metric") + "</div>" +
          '<div class="dashboard-metric-value">' + escapeHtml(String(value)) + "</div>" +
          '<div class="dashboard-metric-foot">' + sub + cta + "</div>" +
          "</article>"
        );
      })
      .join("");
  };

  const renderMetricCards = function (payload) {
    const root = document.getElementById("dashboard-metrics-cards");
    if (!root) return;
    const cards = normalizeMetricsCards(payload.metrics_cards || []);
    if (!cards.length) return;
    root.innerHTML = cards.map(function (c, idx) {
      const tone = c.trend_tone || (c.trendUp ? "up" : "down");
      const trend = c.trend
        ? '<span class="dashboard-trend-badge ' + (tone === "up" ? "is-up" : (tone === "down" ? "is-down" : "is-neutral")) + '">' + escapeHtml(c.trend) + "</span>"
        : "";
      return (
        '<article class="dashboard-hero-stat">' +
        '<div class="dashboard-metric-label">' + escapeHtml(c.label || "") + "</div>" +
        '<div class="dashboard-hero-stat-row"><div class="dashboard-hero-stat-value">' + escapeHtml(c.value || "—") + "</div>" + trend + "</div>" +
        (idx < cards.length - 1 ? '<span class="dashboard-hero-stat-sep"></span>' : "") +
        "</article>"
      );
    }).join("");
  };

  const renderHeroLineChart = function (payload) {
    const root = document.getElementById("dashboard-hero-line-chart");
    if (!root) return;
    const series = normalizeRevenueSeries((payload && payload.revenue_series) || []);
    if (!series.length) {
      root.innerHTML = "";
      return;
    }
    const points = [];
    for (let i = 0; i < series.length; i += 1) {
      const base = Number(series[i].val || 0);
      points.push(base * 0.92, base, base * 1.04, base * 0.98);
    }
    const min = Math.min.apply(null, points);
    const max = Math.max.apply(null, points);
    const width = Math.max(680, root.clientWidth || 900);
    const height = 132;
    const padX = 8;
    const padY = 10;
    const chartW = width - padX * 2;
    const chartH = height - padY * 2;
    const step = chartW / (points.length - 1 || 1);
    const path = points.map(function (v, i) {
      const x = padX + i * step;
      const y = padY + (1 - ((v - min) / ((max - min) || 1))) * chartH;
      return (i ? "L" : "M") + x + " " + y;
    }).join(" ");
    root.innerHTML =
      '<svg width="100%" height="' + height + '" viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="none">' +
      '<defs><linearGradient id="heroFade" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="#64B5F6" stop-opacity="0.18"/>' +
      '<stop offset="100%" stop-color="#64B5F6" stop-opacity="0"/>' +
      '</linearGradient></defs>' +
      '<path d="' + path + ' L ' + (width - padX) + ' ' + (height - padY) + ' L ' + padX + ' ' + (height - padY) + ' Z" fill="url(#heroFade)"/>' +
      '<path d="' + path + '" fill="none" stroke="#64B5F6" stroke-width="2"/>' +
      "</svg>";
  };

  const formatNumber = function (value) {
    const n = Number(value || 0);
    if (!Number.isFinite(n)) return String(value || "");
    return n.toLocaleString("ru-RU");
  };

  const renderRating = function (payload) {
    const root = document.getElementById("dashboard-rating");
    if (!root) return;
    const r = payload.rating || {};
    const overall = Number(r.overall || 4.26);
    const maxScore = Number(r.max_score || 5);
    const pct = maxScore > 0 ? Math.max(0, Math.min(100, (overall / maxScore) * 100)) : 0;
    const circumference = 2 * Math.PI * 42;
    const offset = circumference - (pct / 100) * circumference;

    const color = "#64B5F6";
    const ext = r.external || { value: 4.3 };
    const beh = r.behavior || { value: 4.2 };

    root.innerHTML =
      '<div class="dashboard-rating">' +
      '<div class="dashboard-rating-ring">' +
      '<svg width="96" height="96" viewBox="0 0 96 96" style="transform: rotate(-90deg)">' +
      '<circle cx="48" cy="48" r="42" fill="none" stroke="#2C2C2C" stroke-width="6"></circle>' +
      '<circle cx="48" cy="48" r="42" fill="none" stroke="' + color + '" stroke-width="6" stroke-linecap="round" ' +
      'stroke-dasharray="' + String(circumference) + '" stroke-dashoffset="' + String(offset) + '"></circle>' +
      "</svg>" +
      '<div class="dashboard-rating-center"><span class="dashboard-rating-score">' + escapeHtml(overall.toFixed(2)) + '</span>' +
      '<span class="dashboard-rating-max">/ ' + escapeHtml(String(maxScore)) + "</span></div>" +
      "</div>" +
      '<div class="dashboard-rating-copy">' +
      '<p class="muted">Формула: внешняя 60% + поведение 40%</p>' +
      '<div class="dashboard-rating-bars">' +
      '<div class="dashboard-rating-bar"><span class="muted">Внешняя (60%)</span><strong>' + escapeHtml(String(ext.value || "—")) + '</strong></div>' +
      '<div class="dashboard-rating-bar"><span class="muted">Поведение (40%)</span><strong>' + escapeHtml(String(beh.value || "—")) + '</strong></div>' +
      "</div>" +
      "</div>" +
      "</div>";
  };

  const renderOrdersByStatus = function (payload) {
    const root = document.getElementById("dashboard-orders-by-status");
    if (!root) return;
    const rows = (payload.orders_by_status || []).length ? payload.orders_by_status : demoOrderStatuses;
    if (!rows.length) {
      root.innerHTML = '<p class="muted">Нет данных по заказам.</p>';
      return;
    }
    const palette = {
      production: "#7F77DD",
      ready_to_ship: "#1D9E75",
      shipped: "#378ADD",
      delivered: "#639922",
      awaiting_reserve: "#BA7517",
    };
    const total = rows.reduce(function (acc, r) { return acc + Number(r.count || 0); }, 0);
    const safeTotal = total > 0 ? total : 1;

    const stack = rows
      .filter(function (r) { return Number(r.count || 0) > 0; })
      .map(function (row) {
        const count = Number(row.count || 0);
        const color = palette[row.status_key] || row.color || "#7eaef0";
        const width = Math.max(4, Math.round((count / safeTotal) * 100));
        return '<span class="dashboard-status-segment" style="width:' + String(width) + '%;background:' + escapeHtml(color) + '"></span>';
      })
      .join("");

    const legend = rows.map(function (row, i) {
      const label = row.label || row.name || "—";
      const count = Number(row.count || 0);
      const color = palette[row.status_key] || row.color || "#7eaef0";
      return (
        '<div class="dashboard-status-item">' +
        '<span class="dashboard-status-dot" style="background:' + escapeHtml(color) + '"></span>' +
        '<span class="dashboard-status-label">' + escapeHtml(label) + "</span>" +
        '<strong class="dashboard-status-count">' + escapeHtml(String(count)) + "</strong>" +
        "</div>"
      );
    }).join("");

    root.innerHTML =
      '<div class="dashboard-status-wrap">' +
      '<div class="dashboard-status-top"><span></span><strong class="dashboard-status-total">' + escapeHtml(String(total)) + "</strong></div>" +
      '<div class="dashboard-status-stack">' + stack + "</div>" +
      '<div class="dashboard-status-legend">' + legend + "</div>" +
      "</div>";
  };

  const renderSvgBarChart = function (rootId, series, barColor) {
    const root = document.getElementById(rootId);
    if (!root) return;
    const data = series || [];
    if (!data.length) {
      root.innerHTML = '<p class="muted">Нет данных.</p>';
      return;
    }
    const width = Math.min(560, Math.max(320, root.clientWidth || 520));
    const height = 198;
    const padL = 34;
    const padR = 14;
    const padT = 10;
    const padB = 22;

    const max = data.reduce(function (acc, r) { return Math.max(acc, Number(r.val || 0)); }, 0) || 1;
    const ticks = 4;
    const tickValues = [];
    for (let i = 0; i <= ticks; i += 1) {
      tickValues.push((max / ticks) * i);
    }

    const baseY = height - padB;
    const chartH = baseY - padT;
    const chartW = width - padL - padR;

    // Recharts-like density: barCategoryGap ~= 30%
    // Use ~70% bar width and ~30% gap within each category slot.
    const slot = chartW / data.length;
    const barW = slot * 0.7;
    const gap = slot * 0.3;

    const grid = tickValues.map(function (_v, i) {
      const y = baseY - (chartH / ticks) * i;
      return '<line x1="' + padL + '" y1="' + y + '" x2="' + (width - padR) + '" y2="' + y + '" stroke="#0c1f3f" stroke-dasharray="3 3" stroke-width="1" />';
    }).join("");

    const yAxisLabels = tickValues.map(function (v, i) {
      const y = baseY - (chartH / ticks) * i;
      return '<text x="' + (padL - 6) + '" y="' + (y + 4) + '" text-anchor="end" font-size="11" fill="#93a7cb">' + escapeHtml(String(Math.round(v))) + "</text>";
    }).join("");

    const bars = data.map(function (r, i) {
      const v = Number(r.val || 0);
      const h = Math.max(2, (v / max) * chartH);
      const x = padL + i * slot + gap / 2;
      const y = baseY - h;
      const labelX = x + barW / 2;
      return (
        '<rect data-tip-label="Выручка" data-tip-value="¥' + escapeHtml(String(v)) + 'M" x="' + x + '" y="' + y + '" width="' + barW + '" height="' + h + '" rx="4" ry="4" fill="' + escapeHtml(barColor) + '" opacity="0.95"></rect>' +
        '<text x="' + labelX + '" y="' + (height - 6) + '" text-anchor="middle" font-size="11" fill="#93a7cb">' + escapeHtml(r.m || "") + "</text>"
      );
    }).join("");

    root.innerHTML =
      '<div class="dashboard-chart-wrap">' +
      '<div class="dashboard-tooltip" hidden></div>' +
      '<svg class="dashboard-chart-svg" width="100%" height="' + height + '" viewBox="0 0 ' + width + ' ' + height + '">' +
      grid +
      yAxisLabels +
      '<line x1="' + padL + '" y1="' + baseY + '" x2="' + (width - padR) + '" y2="' + baseY + '" stroke="#0c1f3f" stroke-width="1" />' +
      bars +
      "</svg>" +
      "</div>";

    const tooltip = root.querySelector(".dashboard-tooltip");
    const svg = root.querySelector("svg");
    if (!tooltip || !svg) return;
    svg.addEventListener("mousemove", function (e) {
      const target = e.target;
      if (!target || !target.getAttribute) return;
      const label = target.getAttribute("data-tip-label");
      const value = target.getAttribute("data-tip-value");
      if (!value) {
        tooltip.hidden = true;
        return;
      }
      const rect = root.getBoundingClientRect();
      tooltip.innerHTML =
        '<div class="dashboard-tooltip-label">' + escapeHtml(label || "") + "</div>" +
        '<div class="dashboard-tooltip-value">' + escapeHtml(value) + "</div>";
      tooltip.hidden = false;
      tooltip.style.left = (e.clientX - rect.left + 12) + "px";
      tooltip.style.top = (e.clientY - rect.top - 28) + "px";
    });
    svg.addEventListener("mouseleave", function () {
      tooltip.hidden = true;
    });
  };

  const renderSvgAreaChart = function (rootId, series, strokeColor) {
    const root = document.getElementById(rootId);
    if (!root) return;
    const data = series || [];
    if (!data.length) {
      root.innerHTML = '<p class="muted">Нет данных.</p>';
      return;
    }
    const width = Math.min(560, Math.max(320, root.clientWidth || 520));
    const height = 198;
    const padL = 34;
    const padR = 14;
    const padT = 10;
    const padB = 22;
    const minY = 85;
    const maxY = 100;
    const ticks = 3;

    const baseY = height - padB;
    const chartH = baseY - padT;
    const chartW = width - padL - padR;
    const step = chartW / (data.length - 1 || 1);

    const grid = (function () {
      const lines = [];
      for (let i = 0; i <= ticks; i += 1) {
        const y = baseY - (chartH / ticks) * i;
        lines.push('<line x1="' + padL + '" y1="' + y + '" x2="' + (width - padR) + '" y2="' + y + '" stroke="#0c1f3f" stroke-dasharray="3 3" stroke-width="1" />');
      }
      return lines.join("");
    })();

    const yAxisLabels = (function () {
      const labels = [];
      for (let i = 0; i <= ticks; i += 1) {
        const v = minY + ((maxY - minY) / ticks) * i;
        const y = baseY - (chartH / ticks) * i;
        labels.push('<text x="' + (padL - 6) + '" y="' + (y + 4) + '" text-anchor="end" font-size="11" fill="#93a7cb">' + escapeHtml(String(Math.round(v))) + "</text>");
      }
      return labels.join("");
    })();

    const pts = data.map(function (r, i) {
      const raw = r && Object.prototype.hasOwnProperty.call(r, "compliance") ? r.compliance : 0;
      const v = Number(raw);
      const safe = Number.isFinite(v) ? v : 0;
      const clamped = Math.max(minY, Math.min(maxY, safe));
      const x = padL + i * step;
      const y = padT + (1 - (clamped - minY) / (maxY - minY)) * chartH;
      return { x: x, y: y, m: (r && r.m) || "", v: safe };
    });
    const path = pts.map(function (p, i) { return (i ? "L" : "M") + p.x + " " + p.y; }).join(" ");
    const area = path + " L " + (padL + (data.length - 1) * step) + " " + baseY + " L " + padL + " " + baseY + " Z";
    const dots = pts.map(function (p) {
      return '<circle data-tip-label="SLA" data-tip-value="' + escapeHtml(String(p.v)) + '%" cx="' + p.x + '" cy="' + p.y + '" r="3" fill="' + escapeHtml(strokeColor) + '"></circle>';
    }).join("");
    const labels = pts.map(function (p) {
      return '<text x="' + p.x + '" y="' + (height - 6) + '" text-anchor="middle" font-size="11" fill="#93a7cb">' + escapeHtml(p.m) + "</text>";
    }).join("");

    root.innerHTML =
      '<div class="dashboard-chart-wrap">' +
      '<div class="dashboard-tooltip" hidden></div>' +
      '<svg class="dashboard-chart-svg" width="100%" height="' + height + '" viewBox="0 0 ' + width + ' ' + height + '">' +
      '<defs><linearGradient id="slaGradDark" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="' + escapeHtml(strokeColor) + '" stop-opacity="0.22" />' +
      '<stop offset="100%" stop-color="' + escapeHtml(strokeColor) + '" stop-opacity="0" />' +
      "</linearGradient></defs>" +
      grid +
      yAxisLabels +
      '<line x1="' + padL + '" y1="' + baseY + '" x2="' + (width - padR) + '" y2="' + baseY + '" stroke="#0c1f3f" stroke-width="1" />' +
      '<path d="' + area + '" fill="url(#slaGradDark)" />' +
      '<path d="' + path + '" fill="none" stroke="' + escapeHtml(strokeColor) + '" stroke-width="2" />' +
      dots +
      labels +
      "</svg>" +
      "</div>";

    const tooltip = root.querySelector(".dashboard-tooltip");
    const svg = root.querySelector("svg");
    if (!tooltip || !svg) return;
    svg.addEventListener("mousemove", function (e) {
      const target = e.target;
      if (!target || !target.getAttribute) return;
      const label = target.getAttribute("data-tip-label");
      const value = target.getAttribute("data-tip-value");
      if (!value) {
        tooltip.hidden = true;
        return;
      }
      const rect = root.getBoundingClientRect();
      tooltip.innerHTML =
        '<div class="dashboard-tooltip-label">' + escapeHtml(label || "") + "</div>" +
        '<div class="dashboard-tooltip-value">' + escapeHtml(value) + "</div>";
      tooltip.hidden = false;
      tooltip.style.left = (e.clientX - rect.left + 12) + "px";
      tooltip.style.top = (e.clientY - rect.top - 28) + "px";
    });
    svg.addEventListener("mouseleave", function () {
      tooltip.hidden = true;
    });
  };

  const renderMiniChart = function (rootId, series, valueKey, suffix) {
    const root = document.getElementById(rootId);
    if (!root) return;
    const data = series || [];
    if (!data.length) {
      root.innerHTML = '<p class="muted">Нет данных.</p>';
      return;
    }
    const max = data.reduce(function (acc, r) { return Math.max(acc, Number(r[valueKey] || 0)); }, 0) || 1;
    root.innerHTML =
      '<div class="dashboard-bars">' +
      data.map(function (row) {
        const label = row.m || "";
        const value = Number(row[valueKey] || 0);
        const width = Math.round((value / max) * 100);
        return (
          '<div class="dashboard-bars-row" style="grid-template-columns: 56px minmax(0,1fr) 64px">' +
          '<span class="muted" style="text-align:right">' + escapeHtml(label) + "</span>" +
          '<div class="dashboard-bar-track"><div class="dashboard-bar-fill" style="width:' + String(width) + '%;background:#1D9E75"></div></div>' +
          '<span class="muted" style="text-align:right">' + escapeHtml(formatNumber(value.toFixed(1))) + escapeHtml(suffix || "") + "</span>" +
          "</div>"
        );
      }).join("") +
      "</div>";
  };

  const renderIncomingRequests = function (payload) {
    const root = document.getElementById("dashboard-incoming-requests");
    if (!root) return;
    const items = (payload.incoming_requests || []).length ? payload.incoming_requests : demoRequests;
    const badge = document.getElementById("dashboard-incoming-badge");
    if (badge) {
      const n = items.length;
      badge.textContent = String(n) + " новых";
      badge.hidden = n === 0;
    }
    if (!items.length) {
      root.innerHTML = '<p class="muted">Нет новых запросов.</p>';
      return;
    }
    const header =
      '<div class="dashboard-request-head">' +
      '<span>ID</span><span>Деталь</span><span>Клиент</span><span>Сумма</span><span>Время</span>' +
      "</div>";

    const rowsHtml = items.map(function (item) {
      const dot = item.dot || "#378ADD";
      const type = item.type || "";
      const requestType = item.request_type || "";
      const brand = item.brand || "";
      const id = item.id || "";
      const part = item.part || "";
      const client = item.client || "";
      const amount = item.amount || "—";
      const time = item.time || "";
      const url = item.url || "/seller/requests/";
      const typeStyle = (function () {
        if (requestType === "urgent") return { bg: "#23151d", color: "#ff9db2", dot: "#E24B4A" };
        if (requestType === "drawing") return { bg: "#121332", color: "#bcb7ff", dot: "#7F77DD" };
        if (requestType === "standard") return { bg: "#0a172a", color: "#a9c7f5", dot: "#378ADD" };
        if (type === "Срочный") return { bg: "#23151d", color: "#ff9db2" };
        if (type === "По чертежу") return { bg: "#121332", color: "#bcb7ff" };
        return { bg: "#0a172a", color: "#a9c7f5" };
      })();
      return (
        '<a class="dashboard-request-row" href="' + escapeHtml(url) + '">' +
        '<span class="dashboard-dot" style="background:' + escapeHtml(typeStyle.dot || dot) + '"></span>' +
        '<div class="dashboard-request-idcol">' + (id ? '<span class="dashboard-request-id">' + escapeHtml(id) + "</span>" : "") + "</div>" +
        '<div class="dashboard-request-main">' +
        '<div class="dashboard-request-meta">' +
        '<span class="dashboard-badge" style="background:' + escapeHtml(typeStyle.bg) + ';color:' + escapeHtml(typeStyle.color) + ';border-color:transparent">' + escapeHtml(type) + "</span>" +
        (brand ? '<span class="dashboard-badge">' + escapeHtml(brand) + "</span>" : "") +
        "</div>" +
        '<div class="dashboard-request-part">' + escapeHtml(part) + "</div>" +
        "</div>" +
        '<span class="dashboard-request-client muted">' + escapeHtml(client) + "</span>" +
        '<span class="dashboard-request-amount">' + escapeHtml(amount) + "</span>" +
        '<span class="dashboard-request-time muted">' + escapeHtml(time) + "</span>" +
        "</a>"
      );
    }).join("");
    root.innerHTML = header + rowsHtml;
  };

  const renderEventsFeed = function (payload) {
    const root = document.getElementById("dashboard-events-feed");
    if (!root) return;
    const items = (payload.events_feed || []).length ? payload.events_feed : demoEvents;
    if (!items.length) {
      root.innerHTML = '<p class="muted">Нет событий.</p>';
      return;
    }
    root.innerHTML = items.map(function (ev) {
      const status = ev.status || "info";
      const text = ev.text || "";
      const detail = ev.detail || "";
      const time = ev.time || "";
      const dot = (function () {
        if (status === "success") return "#28C840";
        if (status === "warning") return "#FEBC2E";
        if (status === "danger") return "#FF5F57";
        return "#64B5F6";
      })();
      return (
        '<div class="dashboard-event-row">' +
        '<span class="dashboard-event-dot" style="background:' + escapeHtml(dot) + '"></span>' +
        '<div><div class="dashboard-event-title">' + escapeHtml(text) + '</div>' +
        (detail ? '<div class="muted">' + escapeHtml(detail) + "</div>" : "") +
        "</div>" +
        '<span class="dashboard-event-time">' + escapeHtml(time) + "</span>" +
        "</div>"
      );
    }).join("");
  };

  const renderAttentionPanels = function (payload) {
    // Back-compat: if older templates still exist, keep rendering.
    const attentionNode = document.getElementById("dashboard-attention-list");
    const eventsNode = document.getElementById("dashboard-events-list");
    if (!attentionNode || !eventsNode) return;
    attentionNode.innerHTML = '<p class="muted">Секция перемещена в новый layout.</p>';
    eventsNode.innerHTML = '<p class="muted">Секция перемещена в новый layout.</p>';
  };

  const renderProfileAccess = function (payload) {
    const lineNode = document.getElementById("dashboard-profile-line");
    const tagsNode = document.getElementById("dashboard-profile-tags");
    if (!lineNode || !tagsNode) return;
    const p = payload.profile_access || {};
    lineNode.innerHTML =
      "Отдел: <strong>" + escapeHtml(p.department || "—") + "</strong>" +
      " • Роль: <strong>" + escapeHtml(p.role || "—") + "</strong>" +
      " • Компания: <strong>" + escapeHtml(p.company || payload.company || "—") + "</strong>";
    const tags = p.permissions_tags || payload.profile_access_tags || [];
    if (!tags.length) {
      tagsNode.innerHTML = '<span class="chip chip-warn">Теги доступов не настроены</span>';
      return;
    }
    tagsNode.innerHTML = tags.map(function (tag) {
      return '<span class="chip chip-ok">' + escapeHtml(tag) + "</span>";
    }).join("");
  };

  const renderAccountHealth = function (payload) {
    const grid = document.getElementById("dashboard-health-grid");
    const updatedNode = document.getElementById("dashboard-health-updated");
    if (!grid || !updatedNode) return;
    const h = payload.account_health || {};
    grid.innerHTML =
      '<article class="card card-body"><p class="muted">Импортов всего</p><p class="price">' + escapeHtml(h.imports_total || 0) + "</p></article>" +
      '<article class="card card-body"><p class="muted">Сбойных импортов (30д)</p><p class="price">' + escapeHtml(h.failed_imports_30d || 0) + "</p></article>" +
      '<article class="card card-body"><p class="muted">Протухшие товары</p><p class="price">' + escapeHtml(h.stale_products_count || 0) + "</p></article>" +
      '<article class="card card-body"><p class="muted">Низкая полнота</p><p class="price">' + escapeHtml(h.low_completeness_count || 0) + "</p></article>";
    updatedNode.textContent = "Последнее обновление каталога: " + (h.last_catalog_update_at || "—");
  };

  const renderQuickActions = function (payload) {
    const root = document.getElementById("dashboard-quick-actions-grid");
    if (!root) return;
    const actions = payload.quick_actions || [];
    if (!actions.length) {
      root.innerHTML = '<span class="chip">Действия пока не настроены</span>';
      return;
    }
    root.innerHTML = actions
      .map(function (action) {
        if (action.enabled) {
          return '<a class="btn" href="' + escapeHtml(action.url || "#") + '">' + escapeHtml(action.label || action.key) + "</a>";
        }
        const reason = action.reason ? " • " + escapeHtml(action.reason) : "";
        return '<span class="chip">' + escapeHtml(action.label || action.key) + " (locked)" + reason + "</span>";
      })
      .join("");
  };

  const renderDashboard = function (payload, opts) {
    lastPayload = payload;
    try { renderHeader(payload, opts); } catch (_e) {}
    try { renderHeroLineChart(payload); } catch (_e) {}
    try { renderMetricCards(payload); } catch (_e) {}
    try { renderRating(payload); } catch (_e) {}
    try { renderOrdersByStatus(payload); } catch (_e) {}
    try {
      const rev = normalizeRevenueSeries((payload && payload.revenue_series) || []);
      renderSvgBarChart("dashboard-revenue-chart", rev, "#64B5F6");
    } catch (_e) {}
    try {
      const slaSeries = normalizeSlaSeries((payload && payload.sla_series) || []);
      renderSvgAreaChart("dashboard-sla-chart", slaSeries, "#64B5F6");
    } catch (_e) {}
    try { renderIncomingRequests(payload); } catch (_e) {}
    try { renderEventsFeed(payload); } catch (_e) {}
    try { renderProfileAccess(payload); } catch (_e) {}
    try { renderAccountHealth(payload); } catch (_e) {}
    try { renderQuickActions(payload); } catch (_e) {}
  };

  const parseInitialPayload = function () {
    if (!initialNode) return null;
    try {
      return JSON.parse(initialNode.textContent || "{}");
    } catch (e) {
      return null;
    }
  };

  const initialPayload = parseInitialPayload();
  if (initialPayload) {
    renderDashboard(initialPayload, { statusText: "initial" });
  }

  const refreshDashboard = function () {
    if (isRefreshing) return;
    isRefreshing = true;
    fetch(endpoint, { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("dashboard_fetch_failed_" + String(response.status));
        return response.json();
      })
      .then(function (payload) {
        renderDashboard(payload, { statusText: "live" });
      })
      .catch(function () {
        // Make failure visible instead of silently swallowing it.
        if (lastPayload) {
          renderHeader(lastPayload, { statusText: "api error" });
        } else {
          const titleNode = document.getElementById("dashboard-title");
          const subtitleNode = document.getElementById("dashboard-subtitle");
          if (titleNode) titleNode.textContent = "Дашборд";
          if (subtitleNode) subtitleNode.textContent = "Ошибка загрузки данных дашборда (API недоступен / нет доступа).";
        }
      })
      .finally(function () {
        isRefreshing = false;
      });
  };

  refreshDashboard();
  window.setInterval(refreshDashboard, refreshIntervalMs);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refreshDashboard();
  });
})();
