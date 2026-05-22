#!/usr/bin/env python3
"""
Populate .po files for en / es / zh_Hans with curated translations for the
most common user-facing Russian strings.

Line-based parser (no regex over the whole file — gettext .po entries
follow a simple line grammar). Idempotent: fills only blank msgstr lines.

Usage:
    python scripts/translate_po.py
    python manage.py compilemessages
"""
from __future__ import annotations
from pathlib import Path

# Translations dict imported from sibling module so the giant table is
# easier to maintain. Keep it inline here for portability.
TRANSLATIONS: dict[str, dict[str, str]] = {
    # Buttons / actions
    "Сохранить":      {"en": "Save", "es": "Guardar", "zh_Hans": "保存"},
    "Отмена":         {"en": "Cancel", "es": "Cancelar", "zh_Hans": "取消"},
    "Отменить":       {"en": "Cancel", "es": "Cancelar", "zh_Hans": "取消"},
    "Подтвердить":    {"en": "Confirm", "es": "Confirmar", "zh_Hans": "确认"},
    "Отправить":      {"en": "Send", "es": "Enviar", "zh_Hans": "发送"},
    "Закрыть":        {"en": "Close", "es": "Cerrar", "zh_Hans": "关闭"},
    "Открыть":        {"en": "Open", "es": "Abrir", "zh_Hans": "打开"},
    "Удалить":        {"en": "Delete", "es": "Eliminar", "zh_Hans": "删除"},
    "Изменить":       {"en": "Edit", "es": "Editar", "zh_Hans": "编辑"},
    "Редактировать":  {"en": "Edit", "es": "Editar", "zh_Hans": "编辑"},
    "Добавить":       {"en": "Add", "es": "Añadir", "zh_Hans": "添加"},
    "Копировать":     {"en": "Copy", "es": "Copiar", "zh_Hans": "复制"},
    "Скачать":        {"en": "Download", "es": "Descargar", "zh_Hans": "下载"},
    "Загрузить":      {"en": "Upload", "es": "Subir", "zh_Hans": "上传"},
    "Найти":          {"en": "Search", "es": "Buscar", "zh_Hans": "搜索"},
    "Поиск":          {"en": "Search", "es": "Búsqueda", "zh_Hans": "搜索"},
    "Фильтр":         {"en": "Filter", "es": "Filtro", "zh_Hans": "筛选"},
    "Сравнить":       {"en": "Compare", "es": "Comparar", "zh_Hans": "比较"},
    "Подробнее":      {"en": "Details", "es": "Detalles", "zh_Hans": "详情"},
    "Далее":          {"en": "Next", "es": "Siguiente", "zh_Hans": "下一步"},
    "Назад":          {"en": "Back", "es": "Atrás", "zh_Hans": "返回"},
    "Готово":         {"en": "Done", "es": "Listo", "zh_Hans": "完成"},
    "Применить":      {"en": "Apply", "es": "Aplicar", "zh_Hans": "应用"},
    "Войти":          {"en": "Sign in", "es": "Iniciar sesión", "zh_Hans": "登录"},
    "Выйти":          {"en": "Sign out", "es": "Cerrar sesión", "zh_Hans": "退出"},
    "Регистрация":    {"en": "Sign up", "es": "Registrarse", "zh_Hans": "注册"},
    # Navigation
    "Главная":        {"en": "Home", "es": "Inicio", "zh_Hans": "首页"},
    "Меню":           {"en": "Menu", "es": "Menú", "zh_Hans": "菜单"},
    "Настройки":      {"en": "Settings", "es": "Ajustes", "zh_Hans": "设置"},
    "Профиль":        {"en": "Profile", "es": "Perfil", "zh_Hans": "个人资料"},
    "Личный кабинет": {"en": "Account", "es": "Mi cuenta", "zh_Hans": "个人中心"},
    "Уведомления":    {"en": "Notifications", "es": "Notificaciones", "zh_Hans": "通知"},
    "Сообщения":      {"en": "Messages", "es": "Mensajes", "zh_Hans": "消息"},
    "Заказы":         {"en": "Orders", "es": "Pedidos", "zh_Hans": "订单"},
    "Мои заказы":     {"en": "My orders", "es": "Mis pedidos", "zh_Hans": "我的订单"},
    "Заказ":          {"en": "Order", "es": "Pedido", "zh_Hans": "订单"},
    "Каталог":        {"en": "Catalog", "es": "Catálogo", "zh_Hans": "目录"},
    "Поставщики":     {"en": "Suppliers", "es": "Proveedores", "zh_Hans": "供应商"},
    "Поставщик":      {"en": "Supplier", "es": "Proveedor", "zh_Hans": "供应商"},
    "Покупатели":     {"en": "Buyers", "es": "Compradores", "zh_Hans": "买家"},
    "Покупатель":     {"en": "Buyer", "es": "Comprador", "zh_Hans": "买家"},
    "Команда":        {"en": "Team", "es": "Equipo", "zh_Hans": "团队"},
    "Аналитика":      {"en": "Analytics", "es": "Analítica", "zh_Hans": "分析"},
    "Финансы":        {"en": "Finance", "es": "Finanzas", "zh_Hans": "财务"},
    "Платежи":        {"en": "Payments", "es": "Pagos", "zh_Hans": "支付"},
    "Логистика":      {"en": "Logistics", "es": "Logística", "zh_Hans": "物流"},
    "Документы":      {"en": "Documents", "es": "Documentos", "zh_Hans": "文档"},
    "Таможня":        {"en": "Customs", "es": "Aduana", "zh_Hans": "海关"},
    "Рекламации":     {"en": "Claims", "es": "Reclamaciones", "zh_Hans": "理赔"},
    "Рекламация":     {"en": "Claim", "es": "Reclamación", "zh_Hans": "理赔"},
    "Спор":           {"en": "Dispute", "es": "Disputa", "zh_Hans": "争议"},
    "Дашборд":        {"en": "Dashboard", "es": "Panel", "zh_Hans": "仪表盘"},
    "Магазин":        {"en": "Shop", "es": "Tienda", "zh_Hans": "商店"},
    "Чат":            {"en": "Chat", "es": "Chat", "zh_Hans": "聊天"},
    "Новый чат":      {"en": "New chat", "es": "Nuevo chat", "zh_Hans": "新对话"},
    "Чаты":           {"en": "Chats", "es": "Chats", "zh_Hans": "聊天"},
    # Order statuses
    "КП в работе":      {"en": "Quote in progress", "es": "Cotización en curso", "zh_Hans": "报价处理中"},
    "КП готово":        {"en": "Quote ready", "es": "Cotización lista", "zh_Hans": "报价已出"},
    "Зарезервировано":  {"en": "Reserved", "es": "Reservado", "zh_Hans": "已预订"},
    "В производстве":   {"en": "In production", "es": "En producción", "zh_Hans": "生产中"},
    "Готов к отгрузке": {"en": "Ready to ship", "es": "Listo para envío", "zh_Hans": "可发货"},
    "В пути":           {"en": "In transit", "es": "En tránsito", "zh_Hans": "运输中"},
    "На таможне":       {"en": "Customs clearance", "es": "En aduana", "zh_Hans": "海关清关"},
    "Доставлен":        {"en": "Delivered", "es": "Entregado", "zh_Hans": "已送达"},
    "Принят":           {"en": "Accepted", "es": "Aceptado", "zh_Hans": "已验收"},
    "Закрыт":           {"en": "Closed", "es": "Cerrado", "zh_Hans": "已关闭"},
    "Отменён":          {"en": "Cancelled", "es": "Cancelado", "zh_Hans": "已取消"},
    "Активные":         {"en": "Active", "es": "Activos", "zh_Hans": "进行中"},
    "Архив":            {"en": "Archive", "es": "Archivo", "zh_Hans": "归档"},
    "Все":              {"en": "All", "es": "Todos", "zh_Hans": "全部"},
    "Ожидание":         {"en": "Pending", "es": "Pendiente", "zh_Hans": "待处理"},
    "В ожидании":       {"en": "Pending", "es": "Pendiente", "zh_Hans": "等待中"},
    # Supplier statuses
    "Надёжный":  {"en": "Trusted", "es": "De confianza", "zh_Hans": "可信"},
    "Песочница": {"en": "Sandbox", "es": "Sandbox", "zh_Hans": "沙盒"},
    "Рисковый":  {"en": "Risky", "es": "Riesgoso", "zh_Hans": "高风险"},
    "Исключён":  {"en": "Excluded", "es": "Excluido", "zh_Hans": "已排除"},
    # Forms / fields
    "Логин":           {"en": "Username", "es": "Usuario", "zh_Hans": "用户名"},
    "Пароль":          {"en": "Password", "es": "Contraseña", "zh_Hans": "密码"},
    "Имя":             {"en": "Name", "es": "Nombre", "zh_Hans": "姓名"},
    "Фамилия":         {"en": "Last name", "es": "Apellido", "zh_Hans": "姓氏"},
    "Email":           {"en": "Email", "es": "Correo", "zh_Hans": "邮箱"},
    "Телефон":         {"en": "Phone", "es": "Teléfono", "zh_Hans": "电话"},
    "Компания":        {"en": "Company", "es": "Empresa", "zh_Hans": "公司"},
    "Роль":            {"en": "Role", "es": "Rol", "zh_Hans": "角色"},
    "Адрес":           {"en": "Address", "es": "Dirección", "zh_Hans": "地址"},
    "Город":           {"en": "City", "es": "Ciudad", "zh_Hans": "城市"},
    "Страна":          {"en": "Country", "es": "País", "zh_Hans": "国家"},
    "Язык интерфейса": {"en": "Interface language", "es": "Idioma de la interfaz", "zh_Hans": "界面语言"},
    "Язык":            {"en": "Language", "es": "Idioma", "zh_Hans": "语言"},
    # Tables
    "Артикул":      {"en": "Part №", "es": "Pieza №", "zh_Hans": "零件号"},
    "Бренд":        {"en": "Brand", "es": "Marca", "zh_Hans": "品牌"},
    "Наименование": {"en": "Name", "es": "Nombre", "zh_Hans": "名称"},
    "Количество":   {"en": "Quantity", "es": "Cantidad", "zh_Hans": "数量"},
    "Кол-во":       {"en": "Qty", "es": "Cant.", "zh_Hans": "数量"},
    "Цена":         {"en": "Price", "es": "Precio", "zh_Hans": "价格"},
    "Сумма":        {"en": "Total", "es": "Importe", "zh_Hans": "合计"},
    "Итого":        {"en": "Total", "es": "Total", "zh_Hans": "总计"},
    "Валюта":       {"en": "Currency", "es": "Moneda", "zh_Hans": "币种"},
    "Статус":       {"en": "Status", "es": "Estado", "zh_Hans": "状态"},
    "Дата":         {"en": "Date", "es": "Fecha", "zh_Hans": "日期"},
    "Срок":         {"en": "Deadline", "es": "Plazo", "zh_Hans": "截止"},
    "Действие":     {"en": "Action", "es": "Acción", "zh_Hans": "操作"},
    "Комментарий":  {"en": "Comment", "es": "Comentario", "zh_Hans": "备注"},
    "Склад":        {"en": "Warehouse", "es": "Almacén", "zh_Hans": "仓库"},
    "Остаток":      {"en": "Stock", "es": "Stock", "zh_Hans": "库存"},
    "Состояние":    {"en": "Condition", "es": "Estado", "zh_Hans": "状况"},
    "Аналог":       {"en": "Aftermarket", "es": "Alternativo", "zh_Hans": "副厂件"},
    "Описание":     {"en": "Description", "es": "Descripción", "zh_Hans": "描述"},
    "Тип":          {"en": "Type", "es": "Tipo", "zh_Hans": "类型"},
    "Категория":    {"en": "Category", "es": "Categoría", "zh_Hans": "类别"},
    "Сегодня":      {"en": "Today", "es": "Hoy", "zh_Hans": "今天"},
    "Вчера":        {"en": "Yesterday", "es": "Ayer", "zh_Hans": "昨天"},
    "На этой неделе": {"en": "This week", "es": "Esta semana", "zh_Hans": "本周"},
    "Ранее":        {"en": "Earlier", "es": "Anteriores", "zh_Hans": "更早"},
    # Roles
    "Менеджер":      {"en": "Manager", "es": "Gerente", "zh_Hans": "经理"},
    "Логист":        {"en": "Logistician", "es": "Logístico", "zh_Hans": "物流"},
    "Финансист":     {"en": "Finance", "es": "Finanzas", "zh_Hans": "财务"},
    "Оператор":      {"en": "Operator", "es": "Operador", "zh_Hans": "运营"},
    "Администратор": {"en": "Administrator", "es": "Administrador", "zh_Hans": "管理员"},
    "Продавец":      {"en": "Seller", "es": "Vendedor", "zh_Hans": "卖家"},
    # Theme / settings
    "Светлая":         {"en": "Light", "es": "Claro", "zh_Hans": "浅色"},
    "Тёмная":          {"en": "Dark", "es": "Oscuro", "zh_Hans": "深色"},
    "Тёмная тема":     {"en": "Dark theme", "es": "Tema oscuro", "zh_Hans": "深色主题"},
    "Светлая тема":    {"en": "Light theme", "es": "Tema claro", "zh_Hans": "浅色主题"},
    "Тема":            {"en": "Theme", "es": "Tema", "zh_Hans": "主题"},
    "Переключить тему": {"en": "Toggle theme", "es": "Cambiar tema", "zh_Hans": "切换主题"},
    "Звук уведомлений": {"en": "Notification sound", "es": "Sonido de notificación", "zh_Hans": "通知声音"},
    "Классический режим": {"en": "Classic mode", "es": "Modo clásico", "zh_Hans": "经典模式"},
    "Классический":    {"en": "Classic", "es": "Clásico", "zh_Hans": "经典"},
    # Welcome
    "Какую запчасть найти?": {"en": "Which part do you need?", "es": "¿Qué pieza buscas?", "zh_Hans": "需要哪个零件？"},
    "200+ поставщиков":      {"en": "200+ suppliers", "es": "200+ proveedores", "zh_Hans": "200+ 家供应商"},
    "Открытые RFQ":          {"en": "Open RFQs", "es": "RFQ abiertas", "zh_Hans": "未结询价"},
    "перетащите":            {"en": "drop", "es": "suelta", "zh_Hans": "拖入"},
    "или фото прямо сюда":   {"en": "or a photo right here", "es": "o una foto aquí", "zh_Hans": "或照片到这里"},
    "Фото детали":           {"en": "Part photo", "es": "Foto de la pieza", "zh_Hans": "零件照片"},
    "Прикрепить файл (Excel, PDF)": {"en": "Attach file (Excel, PDF)", "es": "Adjuntar archivo (Excel, PDF)", "zh_Hans": "附加文件 (Excel, PDF)"},
    "Отправить или голос":   {"en": "Send or voice", "es": "Enviar o voz", "zh_Hans": "发送或语音"},
    # Misc
    "Да":         {"en": "Yes", "es": "Sí", "zh_Hans": "是"},
    "Нет":        {"en": "No", "es": "No", "zh_Hans": "否"},
    "ОК":         {"en": "OK", "es": "OK", "zh_Hans": "确定"},
    "Ошибка":     {"en": "Error", "es": "Error", "zh_Hans": "错误"},
    "Успешно":    {"en": "Success", "es": "Éxito", "zh_Hans": "成功"},
    "Внимание":   {"en": "Attention", "es": "Atención", "zh_Hans": "注意"},
    "Загрузка…":  {"en": "Loading…", "es": "Cargando…", "zh_Hans": "加载中…"},
    "Сохранено":  {"en": "Saved", "es": "Guardado", "zh_Hans": "已保存"},
    "Скопировано": {"en": "Copied", "es": "Copiado", "zh_Hans": "已复制"},
    "Отправлено": {"en": "Sent", "es": "Enviado", "zh_Hans": "已发送"},

    # ── Sidebar / chat ──
    "Проекты":           {"en": "Projects", "es": "Proyectos", "zh_Hans": "项目"},
    "Проект":            {"en": "Project", "es": "Proyecto", "zh_Hans": "项目"},
    "Новый проект":      {"en": "New project", "es": "Nuevo proyecto", "zh_Hans": "新项目"},
    "Недавние":          {"en": "Recent", "es": "Recientes", "zh_Hans": "最近"},
    "Очистить историю":  {"en": "Clear history", "es": "Limpiar historial", "zh_Hans": "清除历史"},
    "Очистить":          {"en": "Clear", "es": "Limpiar", "zh_Hans": "清除"},
    "Поиск чатов и проектов": {"en": "Search chats & projects", "es": "Buscar chats y proyectos", "zh_Hans": "搜索聊天和项目"},
    "Пользователь":      {"en": "User", "es": "Usuario", "zh_Hans": "用户"},
    "Загрузка...":       {"en": "Loading…", "es": "Cargando…", "zh_Hans": "加载中…"},
    "Переименовать":     {"en": "Rename", "es": "Renombrar", "zh_Hans": "重命名"},
    "Прочитать все":     {"en": "Mark all read", "es": "Marcar todo como leído", "zh_Hans": "全部标为已读"},
    "На связи":          {"en": "Online", "es": "En línea", "zh_Hans": "在线"},
    "AI Ассистент":      {"en": "AI Assistant", "es": "Asistente IA", "zh_Hans": "AI 助手"},
    "Перейти в AI-режим (chat-first)": {"en": "Switch to AI mode (chat-first)", "es": "Cambiar a modo IA (chat-first)", "zh_Hans": "切换到 AI 模式（聊天优先）"},
    "Привет! Я помогу с заказами, RFQ, каталогом и аналитикой.":
        {"en": "Hi! I’ll help with orders, RFQs, catalog and analytics.",
         "es": "¡Hola! Te ayudaré con pedidos, RFQ, catálogo y analítica.",
         "zh_Hans": "您好！我会协助处理订单、询价、目录和分析。"},
    "Задайте вопрос или выберите подсказку.": {"en": "Ask a question or pick a suggestion.", "es": "Haz una pregunta o elige una sugerencia.", "zh_Hans": "提问或选择建议。"},
    "Спросите что-нибудь...": {"en": "Ask anything…", "es": "Pregunta lo que quieras…", "zh_Hans": "提问任何问题…"},
    "Напишите сообщение...":  {"en": "Type a message…", "es": "Escribe un mensaje…", "zh_Hans": "输入消息…"},

    # ── Tools / hero ──
    "Прикрепить":     {"en": "Attach", "es": "Adjuntar", "zh_Hans": "附加"},
    "Фото":           {"en": "Photo", "es": "Foto", "zh_Hans": "照片"},
    "Голос":          {"en": "Voice", "es": "Voz", "zh_Hans": "语音"},
    "Роль":           {"en": "Role", "es": "Rol", "zh_Hans": "角色"},

    # ── KYB / company ──
    "ОГРН":           {"en": "Registration №", "es": "Reg. №", "zh_Hans": "注册号"},
    "ИНН":            {"en": "Tax ID", "es": "ID fiscal", "zh_Hans": "税号"},
    "КПП":            {"en": "Tax dept. code", "es": "Cód. dept. fiscal", "zh_Hans": "税务部门代码"},
    "Юридический адрес": {"en": "Legal address", "es": "Domicilio legal", "zh_Hans": "法定地址"},

    # ── Quotes / RFQ ──
    "Запрос котировки": {"en": "Quote request", "es": "Solicitud de cotización", "zh_Hans": "询价"},
    "Котировка":      {"en": "Quote", "es": "Cotización", "zh_Hans": "报价"},
    "Котировки":      {"en": "Quotes", "es": "Cotizaciones", "zh_Hans": "报价"},
    "Истекла":        {"en": "Expired", "es": "Expirada", "zh_Hans": "已过期"},
    "Бессрочно":      {"en": "No expiry", "es": "Sin caducidad", "zh_Hans": "无限期"},
    "Другое":         {"en": "Other", "es": "Otro", "zh_Hans": "其他"},
    "Общее":          {"en": "General", "es": "General", "zh_Hans": "通用"},
    "Отклонено (наша цена ниже)": {"en": "Rejected (our price is lower)", "es": "Rechazado (nuestro precio es menor)", "zh_Hans": "已拒绝（我们的价格更低）"},

    # ── Audit / events ──
    "История":         {"en": "History", "es": "Historial", "zh_Hans": "历史"},
    "Журнал":          {"en": "Log", "es": "Registro", "zh_Hans": "日志"},
    "События":         {"en": "Events", "es": "Eventos", "zh_Hans": "事件"},
    "Событие":         {"en": "Event", "es": "Evento", "zh_Hans": "事件"},

    # ── Payments / finance ──
    "Счёт":            {"en": "Invoice", "es": "Factura", "zh_Hans": "发票"},
    "Счета":           {"en": "Invoices", "es": "Facturas", "zh_Hans": "发票"},
    "Оплата":          {"en": "Payment", "es": "Pago", "zh_Hans": "付款"},
    "Резерв":          {"en": "Reserve", "es": "Reserva", "zh_Hans": "预留"},
    "Депозит":         {"en": "Deposit", "es": "Depósito", "zh_Hans": "押金"},
    "Возврат":         {"en": "Refund", "es": "Reembolso", "zh_Hans": "退款"},
    "Эскроу":          {"en": "Escrow", "es": "Depósito en garantía", "zh_Hans": "托管"},
    "Удержание":       {"en": "Hold", "es": "Retención", "zh_Hans": "扣留"},
    "Сверка":          {"en": "Reconciliation", "es": "Conciliación", "zh_Hans": "对账"},

    # ── Logistics ──
    "Авиа":            {"en": "Air", "es": "Aéreo", "zh_Hans": "空运"},
    "Море":            {"en": "Sea", "es": "Marítimo", "zh_Hans": "海运"},
    "ЖД":              {"en": "Rail", "es": "Ferroviario", "zh_Hans": "铁路"},
    "Авто":            {"en": "Road", "es": "Por carretera", "zh_Hans": "公路"},
    "Маршрут":         {"en": "Route", "es": "Ruta", "zh_Hans": "路线"},
    "Порт":            {"en": "Port", "es": "Puerto", "zh_Hans": "港口"},
    "Порт отправления": {"en": "Port of departure", "es": "Puerto de salida", "zh_Hans": "起运港"},
    "Порт назначения": {"en": "Port of destination", "es": "Puerto de destino", "zh_Hans": "目的港"},
    "Трекинг":         {"en": "Tracking", "es": "Seguimiento", "zh_Hans": "追踪"},
    "Вес":             {"en": "Weight", "es": "Peso", "zh_Hans": "重量"},
    "Объём":           {"en": "Volume", "es": "Volumen", "zh_Hans": "体积"},
    "Габариты":        {"en": "Dimensions", "es": "Dimensiones", "zh_Hans": "尺寸"},

    # ── Quality / rating ──
    "Рейтинг":         {"en": "Rating", "es": "Calificación", "zh_Hans": "评级"},
    "Оценка":          {"en": "Score", "es": "Puntuación", "zh_Hans": "得分"},
    "Отзыв":           {"en": "Review", "es": "Reseña", "zh_Hans": "评价"},
    "Отзывы":          {"en": "Reviews", "es": "Reseñas", "zh_Hans": "评价"},
    "Качество":        {"en": "Quality", "es": "Calidad", "zh_Hans": "质量"},
    "Сроки":           {"en": "Lead time", "es": "Plazos", "zh_Hans": "交期"},
    "Надёжность":      {"en": "Reliability", "es": "Fiabilidad", "zh_Hans": "可靠性"},

    # ── Misc helpful ──
    "День":            {"en": "Day", "es": "Día", "zh_Hans": "天"},
    "Дни":             {"en": "Days", "es": "Días", "zh_Hans": "天"},
    "Час":             {"en": "Hour", "es": "Hora", "zh_Hans": "小时"},
    "Часы":            {"en": "Hours", "es": "Horas", "zh_Hans": "小时"},
    "Минута":          {"en": "Minute", "es": "Minuto", "zh_Hans": "分钟"},
    "Минуты":          {"en": "Minutes", "es": "Minutos", "zh_Hans": "分钟"},
    "Назначить":       {"en": "Assign", "es": "Asignar", "zh_Hans": "分配"},
    "Принять":         {"en": "Accept", "es": "Aceptar", "zh_Hans": "接受"},
    "Отклонить":       {"en": "Reject", "es": "Rechazar", "zh_Hans": "拒绝"},
    "Согласовать":     {"en": "Approve", "es": "Aprobar", "zh_Hans": "批准"},
    "Завершить":       {"en": "Complete", "es": "Completar", "zh_Hans": "完成"},
    "Продолжить":      {"en": "Continue", "es": "Continuar", "zh_Hans": "继续"},
    "Создать":         {"en": "Create", "es": "Crear", "zh_Hans": "创建"},
    "Просмотр":        {"en": "View", "es": "Ver", "zh_Hans": "查看"},
    "Архивировать":    {"en": "Archive", "es": "Archivar", "zh_Hans": "存档"},
    "Восстановить":    {"en": "Restore", "es": "Restaurar", "zh_Hans": "恢复"},
    "Обновить":        {"en": "Refresh", "es": "Actualizar", "zh_Hans": "刷新"},
    "Экспорт":         {"en": "Export", "es": "Exportar", "zh_Hans": "导出"},
    "Импорт":          {"en": "Import", "es": "Importar", "zh_Hans": "导入"},

    # ── Order pipeline / cards ──
    "Сделка":             {"en": "Deal", "es": "Operación", "zh_Hans": "交易"},
    "Сделка ORD-{id}":    {"en": "Deal ORD-{id}", "es": "Operación ORD-{id}", "zh_Hans": "交易 ORD-{id}"},
    "Подтверждён продавцом": {"en": "Confirmed by seller", "es": "Confirmado por el vendedor", "zh_Hans": "卖家已确认"},
    "Транзит за рубеж":   {"en": "International transit", "es": "Tránsito internacional", "zh_Hans": "国际运输"},
    "Транзит по РФ":      {"en": "Transit in RF", "es": "Tránsito local", "zh_Hans": "国内运输"},
    "На выдаче":          {"en": "Pickup ready", "es": "Listo para recoger", "zh_Hans": "可取货"},
    "Подтверждён":        {"en": "Confirmed", "es": "Confirmado", "zh_Hans": "已确认"},
    "Завершён":           {"en": "Completed", "es": "Completado", "zh_Hans": "已完成"},
    "Транзит":            {"en": "Transit", "es": "Tránsito", "zh_Hans": "运输"},

    # ── Buttons in cards ──
    "💳 Оплатить остаток ${rem} (90%)": {
        "en": "💳 Pay balance ${rem} (90%)",
        "es": "💳 Pagar saldo ${rem} (90%)",
        "zh_Hans": "💳 支付尾款 ${rem} (90%)",
    },
    "✓ Подтвердить приёмку": {"en": "✓ Confirm acceptance", "es": "✓ Confirmar recepción", "zh_Hans": "✓ 确认验收"},
    "📦 Трекинг":             {"en": "📦 Tracking", "es": "📦 Seguimiento", "zh_Hans": "📦 追踪"},
    "Оплатить остаток":       {"en": "Pay balance", "es": "Pagar saldo", "zh_Hans": "支付尾款"},
    "Состояние депозита":     {"en": "Deposit status", "es": "Estado del depósito", "zh_Hans": "押金状态"},

    # ── Notification body texts (templated; placeholders are preserved) ──
    "Обновление по заказу ORD-{id}: {event}": {
        "en": "Order ORD-{id} update: {event}",
        "es": "Actualización del pedido ORD-{id}: {event}",
        "zh_Hans": "订单 ORD-{id} 更新：{event}",
    },
    "✅ Поставщик подтвердил заказ ORD-{id} — запускают производство.": {
        "en": "✅ Supplier confirmed order ORD-{id} — production starting.",
        "es": "✅ El proveedor confirmó el pedido ORD-{id} — inicia producción.",
        "zh_Hans": "✅ 供应商已确认订单 ORD-{id} — 即将投产。",
    },
    "🏭 ORD-{id} в производстве. Сообщим когда готов к отгрузке.": {
        "en": "🏭 ORD-{id} in production. We’ll notify when ready to ship.",
        "es": "🏭 ORD-{id} en producción. Avisaremos cuando esté listo para envío.",
        "zh_Hans": "🏭 ORD-{id} 生产中。可发货时将通知您。",
    },
    "📦 ORD-{id} готов к отгрузке. Оплатите остаток 90% — поедет.": {
        "en": "📦 ORD-{id} is ready to ship. Pay the 90% balance to dispatch.",
        "es": "📦 ORD-{id} listo para envío. Paga el 90% restante para despachar.",
        "zh_Hans": "📦 ORD-{id} 可发货。支付 90% 尾款即可发出。",
    },
    "💳 Остаток 90% оплачен по ORD-{id} — заказ отгружают.": {
        "en": "💳 90% balance paid for ORD-{id} — shipping out.",
        "es": "💳 90% pagado para ORD-{id} — saliendo del almacén.",
        "zh_Hans": "💳 ORD-{id} 90% 尾款已支付 — 准备发货。",
    },
    "🚚 ORD-{id} отгружен и в пути.": {
        "en": "🚚 ORD-{id} dispatched and in transit.",
        "es": "🚚 ORD-{id} despachado y en tránsito.",
        "zh_Hans": "🚚 ORD-{id} 已发货，运输中。",
    },
    "🛫 ORD-{id} в транзите за рубеж.": {
        "en": "🛫 ORD-{id} in international transit.",
        "es": "🛫 ORD-{id} en tránsito internacional.",
        "zh_Hans": "🛫 ORD-{id} 国际运输中。",
    },
    "🛃 ORD-{id} проходит таможню.": {
        "en": "🛃 ORD-{id} clearing customs.",
        "es": "🛃 ORD-{id} en aduana.",
        "zh_Hans": "🛃 ORD-{id} 海关清关中。",
    },
    "🚛 ORD-{id} в транзите по РФ.": {
        "en": "🚛 ORD-{id} in domestic transit.",
        "es": "🚛 ORD-{id} en tránsito local.",
        "zh_Hans": "🚛 ORD-{id} 国内运输中。",
    },
    "📬 ORD-{id} на выдаче — забирайте.": {
        "en": "📬 ORD-{id} ready for pickup.",
        "es": "📬 ORD-{id} listo para recoger.",
        "zh_Hans": "📬 ORD-{id} 可取货。",
    },
    "🏁 ORD-{id} доставлен. Подтвердите приёмку — деньги уйдут продавцу.": {
        "en": "🏁 ORD-{id} delivered. Confirm acceptance to release funds.",
        "es": "🏁 ORD-{id} entregado. Confirma la recepción para liberar el pago.",
        "zh_Hans": "🏁 ORD-{id} 已送达。确认验收后资金将释放给卖家。",
    },
    "🎉 ORD-{id} завершён. Эскроу освобождён продавцу.": {
        "en": "🎉 ORD-{id} completed. Escrow released to supplier.",
        "es": "🎉 ORD-{id} completado. Depósito liberado al proveedor.",
        "zh_Hans": "🎉 ORD-{id} 已完成。托管款项已释放给供应商。",
    },
    "💰 ORD-{id}: резерв 10% оплачен покупателем — можно подтверждать заказ.": {
        "en": "💰 ORD-{id}: buyer paid 10% reserve — please confirm the order.",
        "es": "💰 ORD-{id}: el comprador pagó la reserva del 10% — confirma el pedido.",
        "zh_Hans": "💰 ORD-{id}：买家已支付 10% 预付款 — 请确认订单。",
    },
    "✅ ORD-{id} подтверждён — запустите производство.": {
        "en": "✅ ORD-{id} confirmed — start production.",
        "es": "✅ ORD-{id} confirmado — comienza la producción.",
        "zh_Hans": "✅ ORD-{id} 已确认 — 开始生产。",
    },
    "🏭 ORD-{id} в производстве (статус обновлён).": {
        "en": "🏭 ORD-{id} in production (status updated).",
        "es": "🏭 ORD-{id} en producción (estado actualizado).",
        "zh_Hans": "🏭 ORD-{id} 生产中（状态已更新）。",
    },
    "📦 ORD-{id} помечен «готов к отгрузке». Ждём оплаты 90% от покупателя.": {
        "en": "📦 ORD-{id} marked “ready to ship”. Awaiting buyer’s 90% payment.",
        "es": "📦 ORD-{id} marcado como “listo para envío”. Esperando el 90% del comprador.",
        "zh_Hans": "📦 ORD-{id} 标记为“可发货”。等待买家支付 90%。",
    },
    "💳 Покупатель оплатил остаток 90% по ORD-{id}. Можно отгружать.": {
        "en": "💳 Buyer paid the 90% balance for ORD-{id}. Ship it out.",
        "es": "💳 El comprador pagó el 90% por ORD-{id}. Puedes enviar.",
        "zh_Hans": "💳 买家已支付 ORD-{id} 90% 尾款。可以发货。",
    },
    "🚚 ORD-{id}: вы отгрузили. Покупатель уведомлён.": {
        "en": "🚚 ORD-{id}: you dispatched. Buyer notified.",
        "es": "🚚 ORD-{id}: enviado. Comprador notificado.",
        "zh_Hans": "🚚 ORD-{id}：已发货。买家已收到通知。",
    },
    "🛫 ORD-{id}: транзит за рубеж — следите за трекингом.": {
        "en": "🛫 ORD-{id}: international transit — follow the tracking.",
        "es": "🛫 ORD-{id}: tránsito internacional — sigue el seguimiento.",
        "zh_Hans": "🛫 ORD-{id}：国际运输中 — 请关注追踪。",
    },
    "🛃 ORD-{id}: на таможне (оператор оформляет).": {
        "en": "🛃 ORD-{id}: at customs (operator handling).",
        "es": "🛃 ORD-{id}: en aduana (el operador gestiona).",
        "zh_Hans": "🛃 ORD-{id}：海关中（运营处理）。",
    },
    "🚛 ORD-{id}: транзит по РФ.": {
        "en": "🚛 ORD-{id}: domestic transit.",
        "es": "🚛 ORD-{id}: tránsito local.",
        "zh_Hans": "🚛 ORD-{id}：国内运输中。",
    },
    "📬 ORD-{id}: передан на выдачу — покупатель заберёт.": {
        "en": "📬 ORD-{id}: ready for pickup — buyer will collect.",
        "es": "📬 ORD-{id}: listo para recoger — el comprador lo retirará.",
        "zh_Hans": "📬 ORD-{id}：可取货 — 买家会自取。",
    },
    "🏁 ORD-{id} доставлен. Покупатель должен подтвердить приёмку.": {
        "en": "🏁 ORD-{id} delivered. Buyer must confirm acceptance.",
        "es": "🏁 ORD-{id} entregado. El comprador debe confirmar la recepción.",
        "zh_Hans": "🏁 ORD-{id} 已送达。买家需确认验收。",
    },
    "🎉 ORD-{id}: покупатель подтвердил приёмку — деньги переведены вам из эскроу.": {
        "en": "🎉 ORD-{id}: buyer confirmed — funds released to you from escrow.",
        "es": "🎉 ORD-{id}: el comprador confirmó — los fondos se liberaron del depósito.",
        "zh_Hans": "🎉 ORD-{id}：买家已确认 — 资金已从托管释放给您。",
    },

    # ── Landing meganav / topnav / hero ──
    "главная":               {"en": "home", "es": "inicio", "zh_Hans": "首页"},
    "Главное меню":          {"en": "Main menu", "es": "Menú principal", "zh_Hans": "主菜单"},
    "Каталог":               {"en": "Catalog", "es": "Catálogo", "zh_Hans": "目录"},
    "О нас":                 {"en": "About", "es": "Sobre nosotros", "zh_Hans": "关于我们"},
    "важная информация":     {"en": "important information", "es": "información importante", "zh_Hans": "重要信息"},
    "массовый поиск":        {"en": "bulk search", "es": "búsqueda masiva", "zh_Hans": "批量搜索"},
    "бренды":                {"en": "brands", "es": "marcas", "zh_Hans": "品牌"},
    "стать поставщиком":     {"en": "become a supplier", "es": "ser proveedor", "zh_Hans": "成为供应商"},
    "вопрос/ответ":          {"en": "FAQ", "es": "preguntas y respuestas", "zh_Hans": "问答"},
    "контакты":              {"en": "contacts", "es": "contactos", "zh_Hans": "联系方式"},
    "заказы":                {"en": "orders", "es": "pedidos", "zh_Hans": "订单"},
    "кабинет":               {"en": "account", "es": "cuenta", "zh_Hans": "账户"},
    "закрыть":               {"en": "close", "es": "cerrar", "zh_Hans": "关闭"},
    "ЗАПЧАСТИ · OEM И AFTERMARKET · ПО ВСЕМУ МИРУ": {"en": "PARTS · OEM & AFTERMARKET · WORLDWIDE", "es": "PIEZAS · OEM Y AFTERMARKET · A NIVEL MUNDIAL", "zh_Hans": "零件 · 原厂与副厂 · 全球"},
    "ПРИМЕРЫ:":              {"en": "EXAMPLES:", "es": "EJEMPLOS:", "zh_Hans": "示例："},
    "Прикрепить файл":       {"en": "Attach file", "es": "Adjuntar archivo", "zh_Hans": "附加文件"},
    "Сфотографировать деталь": {"en": "Take a photo of the part", "es": "Hacer foto de la pieza", "zh_Hans": "拍摄零件"},
    "Поиск по каталогу":     {"en": "Catalog search", "es": "Buscar en catálogo", "zh_Hans": "目录搜索"},
    "Голосовой ввод":        {"en": "Voice input", "es": "Entrada de voz", "zh_Hans": "语音输入"},
    "Загрузите спецификацию в Excel или опишите словами — соберу предложения от": {
        "en": "Upload a spec in Excel or describe in words — I’ll gather offers from",
        "es": "Sube una especificación en Excel o descríbelo — buscaré ofertas de",
        "zh_Hans": "上传 Excel 规格或文字描述 — 我会汇总来自",
    },
    "поставщиков":           {"en": "suppliers", "es": "proveedores", "zh_Hans": "供应商"},
    "ч":                     {"en": "h", "es": "h", "zh_Hans": "小时"},
    "средний ETA":           {"en": "average ETA", "es": "ETA promedio", "zh_Hans": "平均到货时间"},
    "к рынку":               {"en": "vs market", "es": "vs mercado", "zh_Hans": "相对市价"},

    # ── About / trust / how ──
    "Узнать больше →":       {"en": "Learn more →", "es": "Saber más →", "zh_Hans": "了解更多 →"},
    "B2B-платформа, которая соединяет покупателей с проверенными поставщиками, производителями и логистическими партнёрами по всему миру. Получайте расчёт за секунды, закупайте по лучшим ценам и контролируйте поставку на каждом этапе.": {
        "en": "is a B2B platform connecting buyers with verified suppliers, manufacturers and logistics partners worldwide. Get quotes in seconds, buy at the best prices and track delivery at every stage.",
        "es": "es una plataforma B2B que conecta a compradores con proveedores verificados, fabricantes y socios logísticos en todo el mundo. Cotizaciones en segundos, mejores precios y seguimiento en cada etapa.",
        "zh_Hans": "是一个 B2B 平台，连接买家与全球认证供应商、制造商和物流合作伙伴。秒级报价、最优价格、全程跟踪。",
    },
    "Загрузка спецификации": {"en": "Spec upload", "es": "Carga de especificación", "zh_Hans": "上传规格"},
    "Excel, CSV или текст письма. Система распознаёт OEM-номера и количества.": {
        "en": "Excel, CSV or email text. The system recognizes OEM numbers and quantities.",
        "es": "Excel, CSV o texto del correo. El sistema reconoce números OEM y cantidades.",
        "zh_Hans": "Excel、CSV 或邮件文本。系统自动识别 OEM 编号和数量。",
    },
    "до минуты":             {"en": "under a minute", "es": "menos de un minuto", "zh_Hans": "不到一分钟"},
    "AI-помощник":           {"en": "AI assistant", "es": "Asistente IA", "zh_Hans": "AI 助手"},
    "Чат вместо сложных форм. Помогает принять решение по логистике и поставке — объясняет Incoterms, сравнивает маршруты.": {
        "en": "Chat instead of complex forms. Helps decide on logistics and delivery — explains Incoterms, compares routes.",
        "es": "Chat en lugar de formularios complejos. Ayuda a decidir sobre logística y entrega — explica Incoterms, compara rutas.",
        "zh_Hans": "用聊天替代复杂表单。帮助决策物流与交付 — 解释 Incoterms，比较路线。",
    },
    "в чате":                {"en": "in chat", "es": "en el chat", "zh_Hans": "聊天中"},
    "Точная цена и срок":    {"en": "Accurate price and lead time", "es": "Precio y plazo exactos", "zh_Hans": "准确报价与交期"},
    "Подтверждённая цена и срок по каждой позиции. Сравнение в одной таблице, заказ через эскроу": {
        "en": "Confirmed price and lead time for every line. Compare in one table, order via escrow",
        "es": "Precio y plazo confirmados por línea. Compara en una tabla, pide vía depósito",
        "zh_Hans": "每项确认价格与交期。一张表格对比，通过托管下单",
    },
    "от 30 секунд до 48 часов": {"en": "from 30 sec to 48 hrs", "es": "de 30 s a 48 h", "zh_Hans": "30 秒到 48 小时"},
    "Контроль поставки":     {"en": "Delivery control", "es": "Control de entrega", "zh_Hans": "交付控制"},
    "Все стадии в одном окне: оплата → отгрузка → транзит → таможня → выдача. При просрочке — автоматическое подключение оператора.": {
        "en": "All stages in one screen: payment → shipping → transit → customs → pickup. SLA breach → operator auto-engaged.",
        "es": "Todas las etapas en una pantalla: pago → envío → tránsito → aduana → entrega. Incumplimiento de SLA → operador automático.",
        "zh_Hans": "所有阶段一屏可见：付款 → 发货 → 运输 → 海关 → 取货。SLA 超时自动接入运营。",
    },
    "в реальном времени":    {"en": "real-time", "es": "tiempo real", "zh_Hans": "实时"},

    # ── Infra section ──
    "Инфраструктура,":       {"en": "Infrastructure", "es": "Infraestructura", "zh_Hans": "基础设施"},
    "ускоряющая поставки":   {"en": "that speeds up deliveries", "es": "que acelera las entregas", "zh_Hans": "加速供应链"},
    "Прямые закупки, прозрачные цены и контроль поставок — в одном окне.": {
        "en": "Direct procurement, transparent prices and supply control — in one place.",
        "es": "Compras directas, precios transparentes y control de suministros — en un solo lugar.",
        "zh_Hans": "直接采购、透明价格、供应控制 — 一站式。",
    },
    "Расчёт и закупка":      {"en": "Quote & buy", "es": "Cotizar y comprar", "zh_Hans": "报价与采购"},
    "Получайте цену и наличие за секунды. OEM и aftermarket — напрямую от производителей.": {
        "en": "Get price and stock in seconds. OEM and aftermarket — direct from manufacturers.",
        "es": "Obtén precio y stock en segundos. OEM y aftermarket — directo de fábrica.",
        "zh_Hans": "秒级获取价格与库存。原厂与副厂 — 直接对接制造商。",
    },
    "Управление поставками": {"en": "Supply management", "es": "Gestión de suministro", "zh_Hans": "供应管理"},
    "Контроль логистики на всех этапах. Отгрузка CIP, DDP, EXW. Отслеживание и документы.": {
        "en": "Logistics control at every stage. CIP, DDP, EXW shipping. Tracking and documents.",
        "es": "Control logístico en cada etapa. Envíos CIP, DDP, EXW. Seguimiento y documentos.",
        "zh_Hans": "全程物流控制。CIP/DDP/EXW 发货。跟踪与文件。",
    },
    "Интеграции":            {"en": "Integrations", "es": "Integraciones", "zh_Hans": "集成"},
    "Интеграция с ERP, ТОИР, EDI и телематикой. API для производственных систем.": {
        "en": "Integration with ERP, EAM, EDI and telematics. API for production systems.",
        "es": "Integración con ERP, EAM, EDI y telemática. API para sistemas de producción.",
        "zh_Hans": "对接 ERP、EAM、EDI 和远程信息处理。生产系统 API。",
    },
    "Консолидация":          {"en": "Consolidation", "es": "Consolidación", "zh_Hans": "整合"},
    "Международные хабы: ОАЭ, Китай, ЕС. Совместная упаковка и оптимизация маршрутов.": {
        "en": "International hubs: UAE, China, EU. Joint packaging and route optimization.",
        "es": "Hubs internacionales: EAU, China, UE. Embalaje conjunto y optimización de rutas.",
        "zh_Hans": "国际枢纽：阿联酋、中国、欧盟。联合打包与路线优化。",
    },
    "Платежи":               {"en": "Payments", "es": "Pagos", "zh_Hans": "支付"},
    "Оплата в любой валюте — мгновенно. Банковские и криптоканалы. Автоматическое распределение.": {
        "en": "Pay in any currency — instantly. Bank and crypto channels. Automatic distribution.",
        "es": "Paga en cualquier moneda — al instante. Canales bancarios y cripto. Distribución automática.",
        "zh_Hans": "任何币种 — 即时付款。银行与加密通道。自动分配。",
    },
    "Сеть поставщиков":      {"en": "Supplier network", "es": "Red de proveedores", "zh_Hans": "供应商网络"},
    "Подключайтесь напрямую к экосистеме производителей, логистов и сервисных компаний.": {
        "en": "Plug directly into the manufacturer, logistics and service ecosystem.",
        "es": "Conéctate directamente al ecosistema de fabricantes, logística y servicios.",
        "zh_Hans": "直接接入制造商、物流与服务生态。",
    },
    "Партнёрская инфраструктура": {"en": "Partner infrastructure", "es": "Infraestructura para partners", "zh_Hans": "合作伙伴基础设施"},
    "Логистика, финансы, ERP, телематика, консолидация. Совместное развитие рынка без посредничества.": {
        "en": "Logistics, finance, ERP, telematics, consolidation. Joint market growth without intermediaries.",
        "es": "Logística, finanzas, ERP, telemática, consolidación. Crecimiento conjunto sin intermediarios.",
        "zh_Hans": "物流、金融、ERP、远程信息、整合。无中介的联合增长。",
    },
    "Посмотреть демо →":     {"en": "View demo →", "es": "Ver demo →", "zh_Hans": "查看演示 →"},

    # ── Industries ──
    "Кому мы":               {"en": "Who we", "es": "A quién", "zh_Hans": "我们服务"},
    "служим":                {"en": "serve", "es": "servimos", "zh_Hans": "对象"},
    "Производители и OEM":   {"en": "Manufacturers & OEM", "es": "Fabricantes y OEM", "zh_Hans": "制造商与 OEM"},
    "Прямые каналы поставок, интеграция ERP, контроль цен и данных, снижение издержек.": {
        "en": "Direct supply channels, ERP integration, price and data control, cost reduction.",
        "es": "Canales directos, integración ERP, control de precios y datos, reducción de costes.",
        "zh_Hans": "直接供应渠道、ERP 集成、价格与数据控制、降本增效。",
    },
    "Дилеры и дистрибьюторы": {"en": "Dealers & distributors", "es": "Concesionarios y distribuidores", "zh_Hans": "经销商与分销商"},
    "Оптимизируйте закупки и логистику в одном окне. Быстрый доступ к складам производителей и консолидация грузов.": {
        "en": "Optimize procurement and logistics in one place. Fast access to manufacturer warehouses and freight consolidation.",
        "es": "Optimiza compras y logística en un solo lugar. Acceso rápido a almacenes y consolidación de cargas.",
        "zh_Hans": "采购与物流一站式优化。快速对接厂库与货物整合。",
    },
    "Сервисные компании":    {"en": "Service companies", "es": "Empresas de servicio", "zh_Hans": "服务企业"},
    "Ускорьте подбор и заказ запчастей. Получайте лучшие цены напрямую от OEM и поставщиков.": {
        "en": "Speed up part selection and ordering. Get the best prices directly from OEMs and suppliers.",
        "es": "Acelera la selección y pedido de piezas. Mejores precios directos de OEM y proveedores.",
        "zh_Hans": "加速零件选型与下单。直接从 OEM 与供应商获取最佳价格。",
    },
    "Эксплуатирующие организации": {"en": "Operators / fleet owners", "es": "Operadores y propietarios de flota", "zh_Hans": "运营/车队"},
    "Контролируйте снабжение техники на объектах. Единая база позиций, статусы поставок и документация.": {
        "en": "Control equipment supply on site. Unified item database, delivery statuses and documents.",
        "es": "Controla el suministro de equipo en obra. Base unificada de piezas, estados y documentos.",
        "zh_Hans": "现场设备供应控制。统一零件库、交付状态与文档。",
    },
    "Логистические операторы": {"en": "Logistics operators", "es": "Operadores logísticos", "zh_Hans": "物流运营商"},
    "Интеграция с международными хабами и таможенными системами. Маршруты, консолидация и отслеживание.": {
        "en": "Integration with international hubs and customs systems. Routes, consolidation and tracking.",
        "es": "Integración con hubs internacionales y sistemas aduaneros. Rutas, consolidación y seguimiento.",
        "zh_Hans": "对接国际枢纽与海关系统。路线、整合与跟踪。",
    },
    "ВЭД и трейдеры":        {"en": "Foreign trade & traders", "es": "Comercio exterior y trading", "zh_Hans": "进出口与贸易"},
    "Подключайтесь к сети глобальных производителей. Создавайте собственные торговые предложения внутри системы.": {
        "en": "Plug into the global manufacturer network. Build your own trade offers inside the platform.",
        "es": "Conéctate a la red global de fabricantes. Crea tus propias ofertas dentro de la plataforma.",
        "zh_Hans": "接入全球制造商网络。在平台内创建自有贸易报价。",
    },

    # ── Final CTA / footer-ish ──
    "Запросите котировку по вашим запчастям": {
        "en": "Get a quote for your parts",
        "es": "Solicita una cotización para tus piezas",
        "zh_Hans": "为您的零件获取报价",
    },
    "Отправьте список деталей — платформа подберёт поставщиков, рассчитает стоимость и предложит оптимальные условия поставки.": {
        "en": "Send the parts list — the platform will find suppliers, calculate cost and propose optimal delivery terms.",
        "es": "Envía la lista de piezas — la plataforma encontrará proveedores, calculará costes y propondrá condiciones óptimas.",
        "zh_Hans": "发送零件清单 — 平台将匹配供应商、计算成本并提供最优交付方案。",
    },
    "Получить котировку →":  {"en": "Get a quote →", "es": "Obtener cotización →", "zh_Hans": "获取报价 →"},
    "Бренды":                {"en": "Brands", "es": "Marcas", "zh_Hans": "品牌"},
    "Смотреть все →":        {"en": "View all →", "es": "Ver todos →", "zh_Hans": "查看全部 →"},
    "Спецпредложения":       {"en": "Special offers", "es": "Ofertas especiales", "zh_Hans": "特别优惠"},
    "200+ поставщиков":      {"en": "200+ suppliers", "es": "200+ proveedores", "zh_Hans": "200+ 家供应商"},

    # ── Trust cells / 4-grid ──
    "Прямой канал цен":   {"en": "Direct price channel", "es": "Canal de precios directo", "zh_Hans": "直接价格通道"},
    "OEM и aftermarket напрямую от производителей. Мгновенный расчёт по любому базису: EXW, FOB, CIF, DDP.": {
        "en": "OEM and aftermarket direct from manufacturers. Instant quote on any basis: EXW, FOB, CIF, DDP.",
        "es": "OEM y aftermarket directo de fábrica. Cotización al instante en cualquier base: EXW, FOB, CIF, DDP.",
        "zh_Hans": "原厂与副厂直接来自制造商。任意贸易条件即时报价：EXW、FOB、CIF、DDP。",
    },
    "Контроль на всех этапах": {"en": "End-to-end control", "es": "Control en cada etapa", "zh_Hans": "全程控制"},
    "13 статусов заказа, автоматический SLA, рейтинг поставщиков по реальным событиям, полный аудит каждой сделки.": {
        "en": "13 order statuses, automatic SLA, supplier rating based on real events, full audit of every deal.",
        "es": "13 estados del pedido, SLA automático, calificación de proveedores por eventos reales, auditoría completa.",
        "zh_Hans": "13 个订单状态、自动 SLA、基于真实事件的供应商评级、每笔交易完整审计。",
    },
    "Глобальная сеть поставок": {"en": "Global supply network", "es": "Red global de suministro", "zh_Hans": "全球供应网络"},
    "Хабы в ОАЭ, Китае и ЕС. Консолидация грузов. Оплата в любой валюте.": {
        "en": "Hubs in UAE, China and EU. Cargo consolidation. Payment in any currency.",
        "es": "Hubs en EAU, China y UE. Consolidación de carga. Pago en cualquier moneda.",
        "zh_Hans": "阿联酋、中国、欧盟枢纽。货物整合。任意币种支付。",
    },
    "Интеграция в ваши процессы": {"en": "Integration with your stack", "es": "Integración con tu sistema", "zh_Hans": "嵌入您的流程"},
    "ERP, 1С, ТОИР/EAM, EDI. Данные поставок попадают прямо в вашу систему.": {
        "en": "ERP, 1C, EAM, EDI. Supply data lands straight in your system.",
        "es": "ERP, 1C, EAM, EDI. Los datos llegan directamente a tu sistema.",
        "zh_Hans": "ERP、1C、EAM、EDI。供应数据直接进入您的系统。",
    },

    # ── Alert ribbon ──
    "До 21 июня":           {"en": "Until June 21", "es": "Hasta el 21 de junio", "zh_Hans": "截至 6 月 21 日"},
    "пользователи, оформившие покупку через платформу, получают дополнительные": {
        "en": "users who purchase through the platform get extra",
        "es": "los usuarios que compren en la plataforma reciben",
        "zh_Hans": "通过平台下单的用户可获得额外",
    },
    "скидки и бонусы":      {"en": "discounts and bonuses", "es": "descuentos y bonificaciones", "zh_Hans": "折扣和奖励"},
    "Подробнее →":          {"en": "Learn more →", "es": "Más información →", "zh_Hans": "了解更多 →"},

    # ── Warehouse section ──
    "Наш склад":            {"en": "Our warehouse", "es": "Nuestro almacén", "zh_Hans": "我们的仓库"},
    "Виртуальный тур →":    {"en": "Virtual tour →", "es": "Tour virtual →", "zh_Hans": "虚拟游览 →"},
    "Транзитный склад":     {"en": "Transit warehouse", "es": "Almacén de tránsito", "zh_Hans": "中转仓库"},
    "Проверенные позиции":  {"en": "Verified items", "es": "Artículos verificados", "zh_Hans": "已验证商品"},
    "Сеть транзитных хабов в": {"en": "Transit hub network in", "es": "Red de hubs de tránsito en", "zh_Hans": "中转枢纽网络覆盖"},
    "7 странах":            {"en": "7 countries", "es": "7 países", "zh_Hans": "7 个国家"},
    "консолидация грузов · отслеживание на каждом этапе доставки.": {
        "en": "cargo consolidation · tracking at every stage.",
        "es": "consolidación de carga · seguimiento en cada etapa.",
        "zh_Hans": "货物整合 · 全程跟踪。",
    },

    # ── Footer ──
    "Покупателям":          {"en": "For buyers", "es": "Para compradores", "zh_Hans": "买家"},
    "Депозит и отсрочка":   {"en": "Deposit & deferred payment", "es": "Depósito y pago aplazado", "zh_Hans": "押金与延期付款"},
    "Компания":             {"en": "Company", "es": "Empresa", "zh_Hans": "公司"},
    "О компании":           {"en": "About company", "es": "Acerca de la empresa", "zh_Hans": "公司介绍"},
    "Реквизиты":            {"en": "Bank details", "es": "Datos bancarios", "zh_Hans": "公司资料"},
    "Сертификаты":          {"en": "Certificates", "es": "Certificados", "zh_Hans": "证书"},
    "Стать поставщиком":    {"en": "Become a supplier", "es": "Ser proveedor", "zh_Hans": "成为供应商"},
    "Контакты":             {"en": "Contacts", "es": "Contactos", "zh_Hans": "联系方式"},
    "Информация":           {"en": "Information", "es": "Información", "zh_Hans": "信息"},
    "Доставка":             {"en": "Delivery", "es": "Entrega", "zh_Hans": "配送"},
    "Возврат товара":       {"en": "Returns", "es": "Devoluciones", "zh_Hans": "退货"},
    "Гарантии":             {"en": "Warranties", "es": "Garantías", "zh_Hans": "质保"},
    "Параллельный импорт":  {"en": "Parallel import", "es": "Importación paralela", "zh_Hans": "平行进口"},
    "Вопрос/ответ":         {"en": "FAQ", "es": "Preguntas y respuestas", "zh_Hans": "问答"},
    "Подписаться на новости": {"en": "Subscribe to news", "es": "Suscribirse a las noticias", "zh_Hans": "订阅新闻"},
    "Подписаться":          {"en": "Subscribe", "es": "Suscribirse", "zh_Hans": "订阅"},
    "Раз в неделю — новые поступления, спецпредложения и обновления каталога.": {
        "en": "Weekly — new arrivals, special offers and catalog updates.",
        "es": "Semanalmente — novedades, ofertas especiales y actualizaciones del catálogo.",
        "zh_Hans": "每周一封 — 新品、特价与目录更新。",
    },
    "МО, г. Долгопрудный, Дорожный пр-д, 10": {
        "en": "Moscow Region, Dolgoprudny, Dorozhny pr-d, 10",
        "es": "Región de Moscú, Dolgoprudny, Dorozhny pr-d, 10",
        "zh_Hans": "莫斯科州，多尔戈普鲁德内市，Dorozhny pr-d 10",
    },

    # ── Deal cards ──
    "Кольцо уплотнительное гидроцилиндра": {
        "en": "Hydraulic cylinder seal ring",
        "es": "Anillo de sellado del cilindro hidráulico",
        "zh_Hans": "液压缸密封圈",
    },
    "Ремкомплект гидроцилиндра стрелы": {
        "en": "Boom cylinder repair kit",
        "es": "Kit de reparación del cilindro de la pluma",
        "zh_Hans": "动臂油缸修理包",
    },
    "в наличии":            {"en": "in stock", "es": "en stock", "zh_Hans": "有货"},
    "шт":                   {"en": "pcs", "es": "uds", "zh_Hans": "件"},
    "отгрузка сегодня":     {"en": "ships today", "es": "envío hoy", "zh_Hans": "今日发货"},
    "оригинал":             {"en": "original", "es": "original", "zh_Hans": "原厂"},
    "9 позиций":            {"en": "9 items", "es": "9 artículos", "zh_Hans": "9 项"},
}


# ── Arabic add-on: merge ar translations into TRANSLATIONS at import time.
# Living separately so the main RU/EN/ES/ZH table stays scannable.
_AR_TRANSLATIONS: dict[str, str] = {
    # Buttons / actions
    "Сохранить": "حفظ", "Отмена": "إلغاء", "Отменить": "إلغاء", "Подтвердить": "تأكيد",
    "Отправить": "إرسال", "Закрыть": "إغلاق", "Открыть": "فتح", "Удалить": "حذف",
    "Изменить": "تحرير", "Редактировать": "تحرير", "Добавить": "إضافة",
    "Копировать": "نسخ", "Скачать": "تنزيل", "Загрузить": "رفع", "Найти": "بحث",
    "Поиск": "بحث", "Фильтр": "تصفية", "Сравнить": "مقارنة", "Подробнее": "التفاصيل",
    "Далее": "التالي", "Назад": "رجوع", "Готово": "تم", "Применить": "تطبيق",
    "Войти": "تسجيل الدخول", "Выйти": "تسجيل الخروج", "Регистрация": "إنشاء حساب",
    # Navigation
    "Главная": "الرئيسية", "Меню": "القائمة", "Настройки": "الإعدادات",
    "Профиль": "الملف الشخصي", "Личный кабинет": "حسابي", "Уведомления": "الإشعارات",
    "Сообщения": "الرسائل", "Заказы": "الطلبات", "Мои заказы": "طلباتي",
    "Заказ": "الطلب", "Каталог": "الفهرس", "Поставщики": "الموردون",
    "Поставщик": "المورّد", "Покупатели": "المشترون", "Покупатель": "المشتري",
    "Команда": "الفريق", "Аналитика": "التحليلات", "Финансы": "المالية",
    "Платежи": "المدفوعات", "Логистика": "اللوجستيات", "Документы": "المستندات",
    "Таможня": "الجمارك", "Рекламации": "الشكاوى", "Рекламация": "شكوى",
    "Спор": "نزاع", "Дашборд": "لوحة التحكم", "Магазин": "المتجر",
    "Чат": "محادثة", "Новый чат": "محادثة جديدة", "Чаты": "المحادثات",
    # Order statuses
    "КП в работе": "عرض السعر قيد الإعداد", "КП готово": "عرض السعر جاهز",
    "Зарезервировано": "محجوز", "В производстве": "قيد الإنتاج",
    "Готов к отгрузке": "جاهز للشحن", "В пути": "في الطريق",
    "На таможне": "في الجمارك", "Доставлен": "تم التسليم",
    "Принят": "تم القبول", "Закрыт": "مغلق", "Отменён": "ملغى",
    "Активные": "نشطة", "Архив": "الأرشيف", "Все": "الكل",
    "Ожидание": "قيد الانتظار", "В ожидании": "قيد الانتظار",
    # Supplier statuses
    "Надёжный": "موثوق", "Песочница": "تجريبي",
    "Рисковый": "عالي المخاطر", "Исключён": "مستبعد",
    # Forms
    "Логин": "اسم المستخدم", "Пароль": "كلمة المرور", "Имя": "الاسم",
    "Фамилия": "اللقب", "Email": "البريد الإلكتروني", "Телефон": "الهاتف",
    "Компания": "الشركة", "Роль": "الدور", "Адрес": "العنوان", "Город": "المدينة",
    "Страна": "البلد", "Язык интерфейса": "لغة الواجهة", "Язык": "اللغة",
    # Tables
    "Артикул": "رقم القطعة", "Бренд": "الماركة", "Наименование": "الاسم",
    "Количество": "الكمية", "Кол-во": "الكمية", "Цена": "السعر",
    "Сумма": "الإجمالي", "Итого": "الإجمالي", "Валюта": "العملة",
    "Статус": "الحالة", "Дата": "التاريخ", "Срок": "الموعد النهائي",
    "Действие": "الإجراء", "Комментарий": "تعليق", "Склад": "المستودع",
    "Остаток": "المخزون", "Состояние": "الحالة", "Аналог": "بديل",
    "Описание": "الوصف", "Тип": "النوع", "Категория": "التصنيف",
    "Сегодня": "اليوم", "Вчера": "أمس",
    "На этой неделе": "هذا الأسبوع", "Ранее": "سابقاً",
    # Roles
    "Менеджер": "مدير", "Логист": "لوجستي", "Финансист": "محاسب",
    "Оператор": "مشغّل", "Администратор": "مسؤول", "Продавец": "بائع",
    # Theme / settings
    "Светлая": "فاتحة", "Тёмная": "داكنة", "Тёмная тема": "السمة الداكنة",
    "Светлая тема": "السمة الفاتحة", "Тема": "السمة",
    "Переключить тему": "تبديل السمة", "Звук уведомлений": "صوت الإشعارات",
    "Классический режим": "الوضع الكلاسيكي", "Классический": "كلاسيكي",
    # Welcome / chat
    "Какую запчасть найти?": "ما القطعة التي تبحث عنها؟",
    "200+ поставщиков": "200+ مورّد", "Открытые RFQ": "طلبات أسعار مفتوحة",
    "перетащите": "أفلت", "или фото прямо сюда": "أو صورة هنا",
    "Фото детали": "صورة القطعة",
    "Прикрепить файл (Excel, PDF)": "إرفاق ملف (Excel, PDF)",
    "Отправить или голос": "إرسال أو صوت",
    "Проекты": "المشاريع", "Проект": "مشروع", "Новый проект": "مشروع جديد",
    "Недавние": "الأحدث", "Очистить историю": "مسح السجل", "Очистить": "مسح",
    "Поиск чатов и проектов": "بحث في المحادثات والمشاريع",
    "Пользователь": "المستخدم", "Загрузка...": "جارٍ التحميل…",
    "Переименовать": "إعادة تسمية", "Прочитать все": "وضع علامة كمقروء",
    "На связи": "متصل", "AI Ассистент": "مساعد AI",
    "Перейти в AI-режим (chat-first)": "التبديل إلى وضع AI",
    "Привет! Я помогу с заказами, RFQ, каталогом и аналитикой.":
        "مرحباً! سأساعدك في الطلبات والاستفسارات والفهرس والتحليلات.",
    "Задайте вопрос или выберите подсказку.": "اطرح سؤالاً أو اختر اقتراحاً.",
    "Спросите что-нибудь...": "اسأل أي شيء…",
    "Напишите сообщение...": "اكتب رسالة…",
    # Tools
    "Прикрепить": "إرفاق", "Фото": "صورة", "Голос": "صوت",
    # Order pipeline / cards
    "Сделка": "صفقة", "Сделка ORD-{id}": "صفقة ORD-{id}",
    "Подтверждён продавцом": "تم التأكيد من المورّد",
    "Транзит за рубеж": "نقل دولي", "Транзит по РФ": "نقل محلي",
    "На выдаче": "جاهز للاستلام", "Подтверждён": "مؤكّد",
    "Завершён": "مكتمل", "Транзит": "نقل",
    "💳 Оплатить остаток ${rem} (90%)": "💳 دفع المتبقي ${rem} (90%)",
    "✓ Подтвердить приёмку": "✓ تأكيد الاستلام",
    "📦 Трекинг": "📦 تتبّع",
    "Оплатить остаток": "دفع المتبقي",
    "Состояние депозита": "حالة الإيداع",
    # Order-event templates
    "Обновление по заказу ORD-{id}: {event}": "تحديث الطلب ORD-{id}: {event}",
    "✅ Поставщик подтвердил заказ ORD-{id} — запускают производство.":
        "✅ أكّد المورّد الطلب ORD-{id} — بدء الإنتاج.",
    "🏭 ORD-{id} в производстве. Сообщим когда готов к отгрузке.":
        "🏭 ORD-{id} قيد الإنتاج. سنُعلمك عند الجاهزية للشحن.",
    "📦 ORD-{id} готов к отгрузке. Оплатите остаток 90% — поедет.":
        "📦 ORD-{id} جاهز للشحن. ادفع 90% المتبقية ليتم الإرسال.",
    "💳 Остаток 90% оплачен по ORD-{id} — заказ отгружают.":
        "💳 تم دفع 90% المتبقية لـ ORD-{id} — جارٍ الشحن.",
    "🚚 ORD-{id} отгружен и в пути.": "🚚 ORD-{id} تم شحنه وفي الطريق.",
    "🛫 ORD-{id} в транзите за рубеж.": "🛫 ORD-{id} في نقل دولي.",
    "🛃 ORD-{id} проходит таможню.": "🛃 ORD-{id} في الجمارك.",
    "🚛 ORD-{id} в транзите по РФ.": "🚛 ORD-{id} في نقل محلي.",
    "📬 ORD-{id} на выдаче — забирайте.": "📬 ORD-{id} جاهز للاستلام.",
    "🏁 ORD-{id} доставлен. Подтвердите приёмку — деньги уйдут продавцу.":
        "🏁 ORD-{id} تم التسليم. أكّد الاستلام لتحويل المبلغ للمورّد.",
    "🎉 ORD-{id} завершён. Эскроу освобождён продавцу.":
        "🎉 ORD-{id} مكتمل. تم تحرير الأموال للمورّد.",
    "💰 ORD-{id}: резерв 10% оплачен покупателем — можно подтверждать заказ.":
        "💰 ORD-{id}: دفع المشتري 10% — يمكنك تأكيد الطلب.",
    "✅ ORD-{id} подтверждён — запустите производство.":
        "✅ ORD-{id} مؤكّد — ابدأ الإنتاج.",
    "🏭 ORD-{id} в производстве (статус обновлён).":
        "🏭 ORD-{id} قيد الإنتاج (تم تحديث الحالة).",
    "📦 ORD-{id} помечен «готов к отгрузке». Ждём оплаты 90% от покупателя.":
        "📦 ORD-{id} تم تحديده كـ«جاهز للشحن». بانتظار دفع 90% من المشتري.",
    "💳 Покупатель оплатил остаток 90% по ORD-{id}. Можно отгружать.":
        "💳 دفع المشتري 90% لـ ORD-{id}. يمكن الشحن.",
    "🚚 ORD-{id}: вы отгрузили. Покупатель уведомлён.":
        "🚚 ORD-{id}: تم الشحن. أُعلم المشتري.",
    "🛫 ORD-{id}: транзит за рубеж — следите за трекингом.":
        "🛫 ORD-{id}: نقل دولي — تابع التتبّع.",
    "🛃 ORD-{id}: на таможне (оператор оформляет).":
        "🛃 ORD-{id}: في الجمارك (المشغّل يعالج).",
    "🚛 ORD-{id}: транзит по РФ.": "🚛 ORD-{id}: نقل محلي.",
    "📬 ORD-{id}: передан на выдачу — покупатель заберёт.":
        "📬 ORD-{id}: جاهز للاستلام — سيستلمه المشتري.",
    "🏁 ORD-{id} доставлен. Покупатель должен подтвердить приёмку.":
        "🏁 ORD-{id} تم التسليم. على المشتري تأكيد الاستلام.",
    "🎉 ORD-{id}: покупатель подтвердил приёмку — деньги переведены вам из эскроу.":
        "🎉 ORD-{id}: أكّد المشتري الاستلام — تم تحويل الأموال إليك.",
    # Landing
    "главная": "الرئيسية", "Главное меню": "القائمة الرئيسية",
    "О нас": "من نحن", "важная информация": "معلومات مهمة",
    "массовый поиск": "بحث جماعي", "бренды": "الماركات",
    "стать поставщиком": "كن مورّداً", "вопрос/ответ": "أسئلة وأجوبة",
    "контакты": "اتصل بنا", "заказы": "الطلبات", "кабинет": "الحساب",
    "закрыть": "إغلاق",
    "ЗАПЧАСТИ · OEM И AFTERMARKET · ПО ВСЕМУ МИРУ":
        "قطع الغيار · أصلية وبديلة · حول العالم",
    "ПРИМЕРЫ:": "أمثلة:",
    "Прикрепить файл": "إرفاق ملف",
    "Сфотографировать деталь": "صور القطعة",
    "Поиск по каталогу": "البحث في الفهرس",
    "Голосовой ввод": "إدخال صوتي",
    "Загрузите спецификацию в Excel или опишите словами — соберу предложения от":
        "ارفع مواصفات Excel أو صف بالكلمات — سأجمع عروضاً من",
    "поставщиков": "موردون",
    "ч": "ساعة", "средний ETA": "متوسط ETA", "к рынку": "مقارنة بالسوق",
    "Узнать больше →": "اعرف المزيد ←",
    "Загрузка спецификации": "رفع المواصفات",
    "до минуты": "أقل من دقيقة", "AI-помощник": "مساعد AI",
    "в чате": "في المحادثة",
    "Точная цена и срок": "سعر وموعد دقيقان",
    "от 30 секунд до 48 часов": "من 30 ثانية إلى 48 ساعة",
    "Контроль поставки": "التحكم بالتوريد",
    "в реальном времени": "في الوقت الحقيقي",
    "Инфраструктура,": "بنية تحتية",
    "ускоряющая поставки": "تُسرّع التوريد",
    "Расчёт и закупка": "التسعير والشراء",
    "Управление поставками": "إدارة التوريد",
    "Интеграции": "تكاملات",
    "Консолидация": "التجميع",
    "Сеть поставщиков": "شبكة الموردين",
    "Партнёрская инфраструктура": "بنية الشركاء",
    "Посмотреть демо →": "عرض الديمو ←",
    "Кому мы": "لمن", "служим": "نقدّم خدماتنا",
    "Производители и OEM": "المصنّعون و OEM",
    "Дилеры и дистрибьюторы": "الوكلاء والموزّعون",
    "Сервисные компании": "شركات الخدمة",
    "Эксплуатирующие организации": "منظمات التشغيل",
    "Логистические операторы": "مشغّلو اللوجستيات",
    "ВЭД и трейдеры": "التجار والمستوردون",
    "Запросите котировку по вашим запчастям": "اطلب عرض سعر لقطعك",
    "Получить котировку →": "احصل على عرض السعر ←",
    "Бренды": "الماركات", "Смотреть все →": "عرض الكل ←",
    "Спецпредложения": "عروض خاصة",
    # Misc
    "Да": "نعم", "Нет": "لا", "ОК": "موافق", "Ошибка": "خطأ",
    "Успешно": "بنجاح", "Внимание": "تنبيه", "Загрузка…": "جارٍ التحميل…",
    "Сохранено": "تم الحفظ", "Скопировано": "تم النسخ", "Отправлено": "تم الإرسال",
    "Подтверждённые": "مؤكّدة",
    # Logistics / payments
    "Авиа": "جوّي", "Море": "بحري", "ЖД": "سكك حديدية", "Авто": "بري",
    "Маршрут": "مسار", "Порт": "ميناء",
    "Порт отправления": "ميناء المغادرة",
    "Порт назначения": "ميناء الوصول",
    "Трекинг": "تتبّع", "Вес": "الوزن", "Объём": "الحجم", "Габариты": "الأبعاد",
    "Счёт": "فاتورة", "Счета": "فواتير", "Оплата": "الدفع",
    "Резерв": "احتياطي", "Депозит": "وديعة", "Возврат": "إرجاع",
    "Эскроу": "ضمان", "Удержание": "احتجاز", "Сверка": "تسوية",
    "Рейтинг": "تقييم", "Оценка": "نقاط", "Отзыв": "مراجعة", "Отзывы": "المراجعات",
    "Качество": "الجودة", "Сроки": "المواعيد", "Надёжность": "الموثوقية",
    "День": "يوم", "Дни": "أيام", "Час": "ساعة", "Часы": "ساعات",
    "Минута": "دقيقة", "Минуты": "دقائق",
    "Назначить": "تعيين", "Принять": "قبول", "Отклонить": "رفض",
    "Согласовать": "موافقة", "Завершить": "إنهاء", "Продолжить": "متابعة",
    "Создать": "إنشاء", "Просмотр": "عرض", "Архивировать": "أرشفة",
    "Восстановить": "استرجاع", "Обновить": "تحديث",
    "Экспорт": "تصدير", "Импорт": "استيراد",
    "OEM": "أصلي", "А/н": "بديل",
    "ОГРН": "رقم التسجيل", "ИНН": "الرقم الضريبي", "КПП": "رمز الإدارة الضريبية",
    "Юридический адрес": "العنوان القانوني",
    "Запрос котировки": "طلب عرض سعر",
    "Котировка": "عرض سعر", "Котировки": "عروض الأسعار",
    "Истекла": "منتهية", "Бессрочно": "بلا انتهاء",
    "Другое": "أخرى", "Общее": "عام",
    "Отклонено (наша цена ниже)": "مرفوض (سعرنا أقل)",
    "История": "السجل", "Журнал": "السجل",
    "События": "الأحداث", "Событие": "حدث",

    # Trust cells
    "Прямой канал цен": "قناة السعر المباشرة",
    "OEM и aftermarket напрямую от производителей. Мгновенный расчёт по любому базису: EXW, FOB, CIF, DDP.":
        "قطع أصلية وبديلة مباشرة من المصنّعين. تسعير فوري على أي شرط تسليم: EXW, FOB, CIF, DDP.",
    "Контроль на всех этапах": "تحكم في كل مرحلة",
    "13 статусов заказа, автоматический SLA, рейтинг поставщиков по реальным событиям, полный аудит каждой сделки.":
        "13 حالة للطلب، SLA تلقائي، تصنيف الموردين حسب الأحداث الفعلية، تدقيق كامل لكل صفقة.",
    "Глобальная сеть поставок": "شبكة توريد عالمية",
    "Хабы в ОАЭ, Китае и ЕС. Консолидация грузов. Оплата в любой валюте.":
        "مراكز في الإمارات والصين والاتحاد الأوروبي. تجميع البضائع. الدفع بأي عملة.",
    "Интеграция в ваши процессы": "تكامل مع أنظمتكم",
    "ERP, 1С, ТОИР/EAM, EDI. Данные поставок попадают прямо в вашу систему.":
        "ERP و 1C و EAM و EDI. بيانات التوريد تصل مباشرة إلى نظامكم.",

    # Alert ribbon
    "До 21 июня": "حتى 21 يونيو",
    "пользователи, оформившие покупку через платформу, получают дополнительные":
        "المستخدمون الذين يطلبون عبر المنصة يحصلون على",
    "скидки и бонусы": "خصومات ومكافآت",
    "Подробнее →": "اعرف المزيد ←",

    # Warehouse
    "Наш склад": "مستودعنا",
    "Виртуальный тур →": "جولة افتراضية ←",
    "Транзитный склад": "مستودع عبور",
    "Проверенные позиции": "قطع موثّقة",
    "Сеть транзитных хабов в": "شبكة مراكز عبور في",
    "7 странах": "7 دول",
    "консолидация грузов · отслеживание на каждом этапе доставки.":
        "تجميع البضائع · تتبّع في كل مرحلة من مراحل التسليم.",

    # Footer
    "Покупателям": "للمشترين",
    "Депозит и отсрочка": "وديعة ودفع آجل",
    "Компания": "الشركة",
    "О компании": "عن الشركة",
    "Реквизиты": "البيانات البنكية",
    "Сертификаты": "الشهادات",
    "Стать поставщиком": "كن مورّداً",
    "Контакты": "اتصل بنا",
    "Информация": "معلومات",
    "Доставка": "التوصيل",
    "Возврат товара": "إرجاع البضائع",
    "Гарантии": "الضمانات",
    "Параллельный импорт": "استيراد موازٍ",
    "Вопрос/ответ": "أسئلة وأجوبة",
    "Подписаться на новости": "اشتراك في النشرة",
    "Подписаться": "اشتراك",
    "Раз в неделю — новые поступления, спецпредложения и обновления каталога.":
        "أسبوعياً — منتجات جديدة وعروض خاصة وتحديثات الفهرس.",
    "МО, г. Долгопрудный, Дорожный пр-д, 10":
        "إقليم موسكو، دولغوبرودني، Dorozhny pr-d 10",

    # Deal cards
    "Кольцо уплотнительное гидроцилиндра": "حلقة إحكام للأسطوانة الهيدروليكية",
    "Ремкомплект гидроцилиндра стрелы": "طقم إصلاح أسطوانة الذراع",
    "в наличии": "متوفر",
    "шт": "قطعة",
    "отгрузка сегодня": "شحن اليوم",
    "оригинал": "أصلي",
    "9 позиций": "9 قطع",

    # ── Длинные landing-абзацы (industries / how-it-works / final CTA / about) ──
    # Эти были пропущены — добавляем сейчас, чтобы AR-страница не оставляла
    # русских блоков под арабскими заголовками.
    "B2B-платформа, которая соединяет покупателей с проверенными поставщиками, производителями и логистическими партнёрами по всему миру. Получайте расчёт за секунды, закупайте по лучшим ценам и контролируйте поставку на каждом этапе.":
        "منصة B2B تربط المشترين بموردين موثوقين ومصنّعين وشركاء لوجستيين حول العالم. "
        "احصل على عرض السعر في ثوانٍ، اشترِ بأفضل الأسعار وتحكم بالتوريد في كل مرحلة.",
    # — Industries cards
    "Прямые каналы поставок, интеграция ERP, контроль цен и данных, снижение издержек.":
        "قنوات توريد مباشرة، تكامل ERP، تحكم بالأسعار والبيانات، تقليل التكاليف.",
    "Оптимизируйте закупки и логистику в одном окне. Быстрый доступ к складам производителей и консолидация грузов.":
        "حسّن الشراء واللوجستيات في نافذة واحدة. وصول سريع إلى مستودعات المصنّعين وتجميع البضائع.",
    "Ускорьте подбор и заказ запчастей. Получайте лучшие цены напрямую от OEM и поставщиков.":
        "سرّع اختيار وطلب القطع. احصل على أفضل الأسعار مباشرة من المصنّعين الأصليين والموردين.",
    "Контролируйте снабжение техники на объектах. Единая база позиций, статусы поставок и документация.":
        "تحكّم بتزويد المعدات في المواقع. قاعدة موحّدة للأصناف، حالات التوريد والوثائق.",
    "Интеграция с международными хабами и таможенными системами. Маршруты, консолидация и отслеживание.":
        "تكامل مع المراكز الدولية وأنظمة الجمارك. مسارات، تجميع وتتبّع.",
    "Подключайтесь к сети глобальных производителей. Создавайте собственные торговые предложения внутри системы.":
        "اتصل بشبكة المصنّعين العالميين. أنشئ عروضك التجارية داخل النظام.",
    # — How it works
    "Excel, CSV или текст письма. Система распознаёт OEM-номера и количества.":
        "Excel أو CSV أو نص رسالة. يتعرّف النظام على أرقام OEM والكميات.",
    "Чат вместо сложных форм. Помогает принять решение по логистике и поставке — объясняет Incoterms, сравнивает маршруты.":
        "محادثة بدلاً من النماذج المعقّدة. يساعد على اتخاذ قرارات اللوجستيات والتوريد — يشرح Incoterms ويقارن المسارات.",
    "Подтверждённая цена и срок по каждой позиции. Сравнение в одной таблице, заказ через эскроу":
        "سعر ومدة مؤكّدان لكل قطعة. مقارنة في جدول واحد، طلب عبر الضمان",
    "Все стадии в одном окне: оплата → отгрузка → транзит → таможня → выдача. При просрочке — автоматическое подключение оператора.":
        "كل المراحل في نافذة واحدة: الدفع ← الشحن ← النقل ← الجمارك ← الاستلام. عند التأخير — يتدخّل المشغّل تلقائياً.",
    # — Final CTA
    "Отправьте список деталей — платформа подберёт поставщиков, рассчитает стоимость и предложит оптимальные условия поставки.":
        "أرسل قائمة القطع — ستجد المنصّة الموردين، تحسب التكلفة وتقترح أفضل شروط التوريد.",
}

# Merge: add "ar" to existing TRANSLATIONS entries.
for _k, _v in _AR_TRANSLATIONS.items():
    if _k in TRANSLATIONS:
        TRANSLATIONS[_k]["ar"] = _v
    else:
        TRANSLATIONS[_k] = {"ar": _v}


def po_unescape(s: str) -> str:
    # Decode common .po escapes: \\n, \\t, \\\\, \\"
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == "n":
                out.append("\n"); i += 2; continue
            if nxt == "t":
                out.append("\t"); i += 2; continue
            if nxt == "r":
                out.append("\r"); i += 2; continue
            if nxt == "\\":
                out.append("\\"); i += 2; continue
            if nxt == '"':
                out.append('"'); i += 2; continue
        out.append(c); i += 1
    return "".join(out)


def po_escape(s: str) -> str:
    return (s.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", "\\n")
             .replace("\t", "\\t"))


def fill_po(path: Path, lang: str, overwrite_fuzzy: bool = True) -> tuple[int, int]:
    """
    Fill empty msgstr (and optionally overwrite fuzzy translations) from TRANSLATIONS.

    A "fuzzy" entry is detected by a `#, fuzzy` or `#~` flag line in the comment block
    immediately preceding the msgid, or by a `#|` previous-msgid line. xgettext marks
    new strings as fuzzy when it merges them with similar old ones — these often have
    wrong msgstr values and should be overwritten.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=False)
    n = len(lines)
    out: list[str] = []
    i = 0
    filled = 0
    seen = 0

    def parse_quoted(line: str) -> str | None:
        s = line.strip()
        if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
            return s[1:-1]
        return None

    def is_fuzzy_comment(comment_lines: list[str]) -> bool:
        for cl in comment_lines:
            s = cl.strip()
            if s.startswith("#,") and "fuzzy" in s:
                return True
            if s.startswith("#|"):
                return True
        return False

    while i < n:
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("msgid "):
            # Collect preceding comment lines (already emitted, but inspect from `out` tail)
            cmt_lines = []
            j = len(out) - 1
            while j >= 0 and (out[j].startswith("#") or out[j].strip() == ""):
                cmt_lines.insert(0, out[j])
                j -= 1
            fuzzy = is_fuzzy_comment(cmt_lines)

            # Collect msgid (possibly multi-line)
            seen += 1
            # initial quoted on the same line
            after = stripped[len("msgid "):].strip()
            parts = []
            q = parse_quoted(after) if after else None
            if q is not None:
                parts.append(q)
            msgid_start = i
            i += 1
            while i < n:
                q2 = parse_quoted(lines[i])
                if q2 is None:
                    break
                parts.append(q2)
                i += 1
            msgid_raw = "".join(parts)
            msgid = po_unescape(msgid_raw)

            # Now expect msgstr lines
            if i >= n or not lines[i].lstrip().startswith("msgstr"):
                # Could be msgid_plural — skip without modifying.
                # Copy original block as-is and continue.
                out.extend(lines[msgid_start:i])
                continue

            msgstr_start = i
            stripped2 = lines[i].lstrip()
            after2 = stripped2[len("msgstr"):].lstrip()
            # 'msgstr "..."' or 'msgstr[0] "..."'
            # If after2 starts with '[' — plural form, leave alone.
            if after2.startswith("["):
                # plural: copy and skip until non-quoted line
                out.extend(lines[msgid_start:i])
                out.append(lines[i])
                i += 1
                while i < n and parse_quoted(lines[i]) is not None:
                    out.append(lines[i]); i += 1
                # Continue with possible msgstr[1] etc.
                while i < n and lines[i].lstrip().startswith("msgstr["):
                    out.append(lines[i]); i += 1
                    while i < n and parse_quoted(lines[i]) is not None:
                        out.append(lines[i]); i += 1
                continue

            mparts = []
            q3 = parse_quoted(after2) if after2 else None
            if q3 is not None:
                mparts.append(q3)
            i += 1
            while i < n:
                q4 = parse_quoted(lines[i])
                if q4 is None:
                    break
                mparts.append(q4)
                i += 1
            msgstr_raw = "".join(mparts)

            # Decide if we should fill.
            has_translation = lang in (TRANSLATIONS.get(msgid) or {})
            need_fill = msgid != "" and has_translation and (
                msgstr_raw == "" or (overwrite_fuzzy and fuzzy)
            )
            # If overwriting a fuzzy entry, drop fuzzy flag from comments (was already emitted to out)
            if need_fill and fuzzy:
                # Rewrite tail of out: strip `#, fuzzy` and `#|` lines
                kept_tail = []
                for cl in cmt_lines:
                    s = cl.strip()
                    if s.startswith("#|"):
                        continue
                    if s.startswith("#,") and "fuzzy" in s:
                        # remove only "fuzzy" flag; if other flags remain, keep line
                        flags = [f.strip() for f in s[2:].split(",") if f.strip() and f.strip() != "fuzzy"]
                        if flags:
                            kept_tail.append("#, " + ", ".join(flags))
                        continue
                    kept_tail.append(cl)
                # Replace the last len(cmt_lines) lines of out with kept_tail
                if cmt_lines:
                    out = out[: len(out) - len(cmt_lines)] + kept_tail
            # Emit original msgid block unchanged.
            out.extend(lines[msgid_start:msgstr_start])
            if need_fill:
                new_val = po_escape(TRANSLATIONS[msgid][lang])
                out.append(f'msgstr "{new_val}"')
                filled += 1
            else:
                # Re-emit original msgstr block
                out.extend(lines[msgstr_start:i])
            continue
        else:
            out.append(line)
            i += 1

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return filled, seen


def main():
    base = Path(__file__).resolve().parent.parent / "locale"
    targets = {
        "en":      base / "en/LC_MESSAGES/django.po",
        "es":      base / "es/LC_MESSAGES/django.po",
        "zh_Hans": base / "zh_Hans/LC_MESSAGES/django.po",
        "ar":      base / "ar/LC_MESSAGES/django.po",
    }
    for lang, path in targets.items():
        if not path.exists():
            print(f"[skip] {path} not found"); continue
        filled, seen = fill_po(path, lang)
        print(f"[{lang}] filled {filled} / {seen} entries")


if __name__ == "__main__":
    main()
