/**
 * i18n.js — клиентский переводчик интерфейса.
 *
 * Принцип:
 *  1. Сервер кладёт активный язык в <html lang="..."> (через шаблон).
 *  2. window.t(key, vars?) возвращает строку для текущего языка.
 *  3. На DOMContentLoaded мы пробегаем все [data-i18n], [data-i18n-placeholder],
 *     [data-i18n-title], [data-i18n-aria-label] и подставляем перевод.
 *  4. window.applyI18n(root?) можно дёрнуть после динамической вставки HTML.
 *  5. window.setLanguage(lang) меняет активный язык, шлёт POST /api/set-language/
 *     для сохранения в профиле и перерисовывает DOM.
 *
 * Поддерживаемые языки: ru, en, zh-hans, es.
 * Ключи переводов — короткие латиницей snake.case, либо точечные пути
 * (например 'status.ready_to_ship').
 */
(function () {
  'use strict';

  // ── Словари ──────────────────────────────────────────────────────────
  const DICT = {
    ru: {
      // Topbar / brand
      'brand.name': 'Consolidator',
      'topbar.menu': 'Меню',
      'topbar.notifications': 'Уведомления',
      'topbar.theme_light': 'Светлая тема',
      'topbar.theme_dark': 'Тёмная тема',
      'role.buyer': 'Покупатель',
      'role.seller': 'Поставщик',
      'role.operator': 'Оператор',
      'role.admin': 'Администратор',

      // Sidebar
      'side.search': 'Поиск чатов…',
      'side.new_chat': 'Новый чат',
      'side.pinned': 'Закреплённые',
      'side.recent': 'Недавние',
      'side.today': 'Сегодня',
      'side.yesterday': 'Вчера',
      'side.this_week': 'На этой неделе',
      'side.earlier': 'Ранее',
      'side.clear': 'Очистить',
      'side.settings': 'Настройки',
      'side.logout': 'Выйти',
      'side.profile': 'Профиль',

      // Settings panel
      'settings.title': 'Настройки',
      'settings.close': 'Закрыть',
      'settings.theme': 'Тема',
      'settings.theme.light': 'Светлая',
      'settings.theme.dark': 'Тёмная',
      'settings.language': 'Язык интерфейса',
      'settings.notifications': 'Уведомления',
      'settings.density': 'Плотность',
      'settings.account': 'Аккаунт',
      'settings.logout': 'Выйти',
      'settings.team': 'Команда компании',
      'settings.landing': 'Перейти на сайт',
      'settings.export': 'Скачать мои данные',
      'settings.password': 'Сменить пароль',
      'settings.privacy': 'Конфиденциальность и аккаунт',

      // Welcome
      'welcome.h1': 'Чем могу помочь?',
      'welcome.p': 'Запросите КП, отгрузку, статус заказа или загрузите файл — я подберу варианты и проведу до сделки.',
      'welcome.drop_hint': 'Перетащите файл или нажмите',
      'welcome.drop_paste': 'для вставки из буфера',

      // Common buttons
      'btn.open': 'Открыть',
      'btn.cancel': 'Отмена',
      'btn.confirm': 'Подтвердить',
      'btn.save': 'Сохранить',
      'btn.send': 'Отправить',
      'btn.next': 'Далее',
      'btn.back': 'Назад',
      'btn.close': 'Закрыть',
      'btn.delete': 'Удалить',
      'btn.edit': 'Изменить',
      'btn.add': 'Добавить',
      'btn.copy': 'Копировать',
      'btn.download': 'Скачать',
      'btn.upload': 'Загрузить',
      'btn.search': 'Найти',
      'btn.filter': 'Фильтр',
      'btn.compare': 'Сравнить',
      'btn.details': 'Подробнее',
      'btn.pay': 'Оплатить',
      'btn.pay_balance': 'Оплатить остаток',
      'btn.pay_reserve': 'Внести резерв 10%',

      // Order statuses
      'status.kp_pending': 'КП в работе',
      'status.kp_ready': 'КП готово',
      'status.reserved': 'Зарезервировано',
      'status.in_production': 'В производстве',
      'status.ready_to_ship': 'Готов к отгрузке',
      'status.transit_local': 'В пути (локально)',
      'status.transit_abroad': 'В пути (за рубеж)',
      'status.customs': 'На таможне',
      'status.delivered': 'Доставлен',
      'status.accepted': 'Принят',
      'status.closed': 'Закрыт',
      'status.disputed': 'Спор',
      'status.cancelled': 'Отменён',

      // Incoterms
      'incoterm.fob': 'FOB',
      'incoterm.cip': 'CIP',
      'incoterm.ddp': 'DDP',

      // Tables (generic headers)
      'tbl.order': 'Заказ',
      'tbl.buyer': 'Покупатель',
      'tbl.seller': 'Поставщик',
      'tbl.part': 'Артикул',
      'tbl.brand': 'Бренд',
      'tbl.qty': 'Кол-во',
      'tbl.price': 'Цена',
      'tbl.total': 'Сумма',
      'tbl.currency': 'Валюта',
      'tbl.status': 'Статус',
      'tbl.date': 'Дата',
      'tbl.deadline': 'Срок',
      'tbl.eta': 'ETA',
      'tbl.actor': 'Ответственный',
      'tbl.action': 'Действие',
      'tbl.note': 'Комментарий',
      'tbl.warehouse': 'Склад',
      'tbl.stock': 'Остаток',
      'tbl.condition': 'Состояние',
      'tbl.cond.oem': 'OEM',
      'tbl.cond.aftermarket': 'А/н',

      // Toasts
      'toast.saved': 'Сохранено',
      'toast.error': 'Ошибка',
      'toast.copied': 'Скопировано',
      'toast.sent': 'Отправлено',
      'toast.language_changed': 'Язык обновлён',

      // Welcome screen by role
      'guest.mode':              'Публичный просмотр',
      'welcome.guest.title':     'Найдите запчасть или сравните предложения',
      'welcome.guest.subtitle':  'Введите артикул, название или загрузите спецификацию. Поиск и сравнение доступны без регистрации; аккаунт понадобится для сохранения заявки и оформления заказа.',
      'welcome.buyer.title':    'Какую запчасть найти?',
      'welcome.buyer.subtitle': 'Загрузите спецификацию в Excel, перетащите фото детали или опишите словами — соберу и сравню предложения поставщиков.',
      'welcome.seller.title':   'Что в работе сегодня?',
      'welcome.seller.subtitle':'Срочные задачи, входящие заявки и отгрузки. Каталог, финансы и команда — по запросу.',
      'welcome.operator.title': 'Что в работе на платформе?',
      'welcome.operator.subtitle':'Вы управляете всей сделкой: ведёте заказ от оплаты до доставки, координируете логистов, таможенных брокеров и контролируете платежи.',
      'welcome.operator_logist.title':'Логистика',
      'welcome.operator_logist.subtitle':'Отгрузки, контейнеры и сроки — управляйте через чат.',
      'welcome.operator_customs.title':'Таможня',
      'welcome.operator_customs.subtitle':'Грузы под растаможкой, ТН ВЭД, документы, санкционный скрининг.',
      'welcome.operator_payment.title':'Платежи',
      'welcome.operator_payment.subtitle':'Инвойсы, эскроу, возвраты — управляйте через чат.',
      'welcome.operator_manager.title':'Ключевые клиенты',
      'welcome.operator_manager.subtitle':'Ключевые клиенты: заказчики, проекты, отгрузки, начисления.',
      'welcome.admin.title':    'Платформа',
      'welcome.admin.subtitle': 'Оборот, пользователи и модерация — управление всей площадкой.',

      // Quick-action pills (без эмодзи — эмодзи добавляется в JS)
      'pill.find_part':       'Найти запчасть',
      'pill.compare_suppliers':'Сравнить поставщиков',
      'pill.knowledge':       'База знаний',
      'pill.my_orders':       'Мои сделки',
      'pill.open_rfq':        'Открытые заявки',
      'pill.deposit':         'Депозит',
      'pill.auto_discount':   'Уровни скидки',
      'pill.upload_price':    'Загрузить прайс',
      'pill.my_products':     'Мои товары',
      'pill.drawings':        'Чертежи',
      'pill.drawings_by_part': 'Чертежи по артикулу',
      'pill.customers':        'Заказчики',
      'pill.accruals':         'Начисления',
      'pill.my_deals':         'Мои сделки',
      'pill.my_kam':           'Мой менеджер',
      'pill.market_twin':      'Слепок рынка',
      'pill.customs_data':     'Таможня',
      'pill.invite':           'Пригласить',
      'pill.verification':    'Верификация',
      'pill.urgent':          'Срочное',
      'pill.to_ship':         'К отгрузке',
      'pill.new_rfq':         'Новые заявки',
      'pill.demand':          'Спрос',
      'pill.overview':        'Сводка',
      'pill.queue':           'Список заказов',
      'pill.sla_breach':      'Нарушения сроков',
      'pill.payments_escrow': 'Платежи / Эскроу',
      'pill.customs':         'Таможня',
      'pill.logistics':       'Логистика',
      'pill.kyb_suppliers':   'Проверка поставщиков',
      'pill.my_suppliers':    'Мои поставщики',
      'pill.my_user_chats':    'Мои диалоги',
      'pill.claims':          'Рекламации',
      'pill.analytics':       'Аналитика',
      'pill.rfq':             'Заявки',
      'pill.rfq_semi':        'Нужно подтвердить · 15 мин',
      'pill.rfq_manual':      'Ручной подбор · 48 ч',
      'pill.rfq_auto':        'Автоподбор',
      'rfq.mode.auto':        'Автоподбор',
      'rfq.mode.semi':        'Проверка оператором',
      'rfq.mode.manual':      'Индивидуальный подбор',
      'pill.history':         'История',
      'pill.support':         'Поддержка',
      'pill.customs_summary': 'Сводка таможни',
      'pill.hs_code':         'ТН ВЭД',
      'pill.sanctions':       'Санкции',
      'pill.at_customs':      'На таможне',
      'pill.escrow':          'Эскроу',
      'pill.payments_stats':  'Аналитика',
      'pill.awaiting_reserve':'Ожидают резерва',
      'pill.refunds':         'Возвраты',
      'pill.gmv':             'Оборот',
      'pill.users':           'Пользователи',
      'pill.moderation':      'Модерация',
      'pill.catalog':         'Каталог',
      'pill.settings_admin':  'Настройки платформы',

      'card.notification':    'Уведомление',
      'card.price_calc':      'Расчёт цены',
      'card.confirm_action':  'Подтвердите действие',
      'card.today':           'Сегодня',
      'card.catalog':         'Каталог',
      'card.warehouses':      'Склады',
      'card.best_offers':     'Лучшие предложения',
      'card.comparison':      'Сравнение',
      'card.history':         'История',
      'card.list':            'Список',
      'card.enter_data':      'Введите данные',
      'card.integration':     'Способы интеграции',
      'card.preview':         'Превью',
      'card.seller_queue':    'Очередь поставщика',
      'card.shipping':        'Выберите доставку',
      'card.match_results':   'Результаты подбора',
      'card.file':            'Файл',
      'card.untitled':        'Без названия',
      'stock.in_stock':       'В наличии',
      'stock.not_available':  'Нет в наличии',
      'stock.on_order':       'Под заказ',
      'stock.pcs':            'шт',
      'tag.coming_soon':      'Скоро',
      'tag.recommended':      'Рекомендуется',
      'tag.active':           'Активно',
      'common.confirm':       'Подтвердить',
      'common.cancel':        'Отмена',
      'common.send':          'Отправить',
      'common.error':         'Ошибка',
      'common.you':           'Вы',
      'common.action_done':   'Действие',
      'common.carrier':       'Перевозчик',
      'common.next':          'Дальше',
      'common.cancelled_note':'↩︎ Действие отменено',
      'common.do_not_cancel': 'Не отменять',
      'working.search.0':     'Ищу в каталоге…',
      'working.search.1':     'Подбираю варианты…',
      'working.search.2':     'Проверяю наличие…',
      'working.search.3':     'Сравниваю цены…',
      'working.rfq.0':        'Готовлю запрос…',
      'working.rfq.1':        'Уведомляю поставщиков…',
      'working.rfq.2':        'Создаю карточку заявки…',
      'working.orders.0':     'Загружаю заказы…',
      'working.orders.1':     'Сортирую по дате…',
      'working.orders.2':     'Проверяю статусы…',
      'working.shipment.0':   'Запрашиваю трекинг…',
      'working.shipment.1':   'Уточняю местоположение…',
      'working.shipment.2':   'Считаю ETA…',
      'working.budget.0':     'Считаю расходы…',
      'working.budget.1':     'Группирую по статусам…',
      'working.budget.2':     'Готовлю отчёт…',
      'working.analytics.0':  'Собираю метрики…',
      'working.analytics.1':  'Анализирую данные…',
      'working.analytics.2':  'Формирую сводку…',
      'working.claim.0':      'Оформляю рекламацию…',
      'working.claim.1':      'Уведомляю поддержку…',
      'working.sla.0':        'Проверяю SLA…',
      'working.sla.1':        'Считаю нарушения…',
      'working.suppliers.0':  'Загружаю поставщиков…',
      'working.suppliers.1':  'Считаю рейтинги…',
      'working.default.0':    'Думаю…',
      'working.default.1':    'Анализирую запрос…',
      'working.default.2':    'Готовлю ответ…',
      'working.default.3':    'Подбираю информацию…',
      'bucket.today':         'Сегодня',
      'bucket.yesterday':     'Вчера',
      'bucket.week':          'На этой неделе',
      'bucket.month':         'В этом месяце',
      'bucket.older':         'Старше',
      'prompt.rename_warehouse': 'Новое название склада:',
      'prompt.rename_chat':      'Новое название чата:',
      'prompt.copy_link':        'Скопируйте ссылку и откройте в обычном браузере:',
    },

    en: {
      'brand.name': 'Consolidator',
      'topbar.menu': 'Menu',
      'topbar.notifications': 'Notifications',
      'topbar.theme_light': 'Light theme',
      'topbar.theme_dark': 'Dark theme',
      'role.buyer': 'Buyer',
      'role.seller': 'Seller',
      'role.operator': 'Operator',
      'role.admin': 'Administrator',

      'side.search': 'Search chats…',
      'side.new_chat': 'New chat',
      'side.pinned': 'Pinned',
      'side.recent': 'Recent',
      'side.today': 'Today',
      'side.yesterday': 'Yesterday',
      'side.this_week': 'This week',
      'side.earlier': 'Earlier',
      'side.clear': 'Clear',
      'side.settings': 'Settings',
      'side.logout': 'Log out',
      'side.profile': 'Profile',

      'settings.title': 'Settings',
      'settings.close': 'Close',
      'settings.theme': 'Theme',
      'settings.theme.light': 'Light',
      'settings.theme.dark': 'Dark',
      'settings.language': 'Interface language',
      'settings.notifications': 'Notifications',
      'settings.density': 'Density',
      'settings.account': 'Account',
      'settings.logout': 'Log out',
      'settings.team': 'Company team',
      'settings.landing': 'Go to website',
      'settings.export': 'Export my data',
      'settings.password': 'Change password',
      'settings.privacy': 'Privacy & account',

      'welcome.h1': 'How can I help?',
      'welcome.p': 'Request a quote, shipment, order status or upload a file — I’ll match suppliers and walk you through the deal.',
      'welcome.drop_hint': 'Drop a file or press',
      'welcome.drop_paste': 'to paste from clipboard',

      'btn.open': 'Open',
      'btn.cancel': 'Cancel',
      'btn.confirm': 'Confirm',
      'btn.save': 'Save',
      'btn.send': 'Send',
      'btn.next': 'Next',
      'btn.back': 'Back',
      'btn.close': 'Close',
      'btn.delete': 'Delete',
      'btn.edit': 'Edit',
      'btn.add': 'Add',
      'btn.copy': 'Copy',
      'btn.download': 'Download',
      'btn.upload': 'Upload',
      'btn.search': 'Search',
      'btn.filter': 'Filter',
      'btn.compare': 'Compare',
      'btn.details': 'Details',
      'btn.pay': 'Pay',
      'btn.pay_balance': 'Pay balance',
      'btn.pay_reserve': 'Reserve 10%',

      'status.kp_pending': 'Quote in progress',
      'status.kp_ready': 'Quote ready',
      'status.reserved': 'Reserved',
      'status.in_production': 'In production',
      'status.ready_to_ship': 'Ready to ship',
      'status.transit_local': 'In transit (local)',
      'status.transit_abroad': 'In transit (intl.)',
      'status.customs': 'Customs clearance',
      'status.delivered': 'Delivered',
      'status.accepted': 'Accepted',
      'status.closed': 'Closed',
      'status.disputed': 'Disputed',
      'status.cancelled': 'Cancelled',

      'incoterm.fob': 'FOB',
      'incoterm.cip': 'CIP',
      'incoterm.ddp': 'DDP',

      'tbl.order': 'Order',
      'tbl.buyer': 'Buyer',
      'tbl.seller': 'Supplier',
      'tbl.part': 'Part №',
      'tbl.brand': 'Brand',
      'tbl.qty': 'Qty',
      'tbl.price': 'Price',
      'tbl.total': 'Total',
      'tbl.currency': 'Currency',
      'tbl.status': 'Status',
      'tbl.date': 'Date',
      'tbl.deadline': 'Deadline',
      'tbl.eta': 'ETA',
      'tbl.actor': 'Responsible',
      'tbl.action': 'Action',
      'tbl.note': 'Note',
      'tbl.warehouse': 'Warehouse',
      'tbl.stock': 'Stock',
      'tbl.condition': 'Condition',
      'tbl.cond.oem': 'OEM',
      'tbl.cond.aftermarket': 'Aftermkt',

      'toast.saved': 'Saved',
      'toast.error': 'Error',
      'toast.copied': 'Copied',
      'toast.sent': 'Sent',
      'toast.language_changed': 'Language updated',

      'guest.mode':              'Public preview',
      'welcome.guest.title':     'Find a part or compare offers',
      'welcome.guest.subtitle':  'Enter a part number or name, or upload a specification. Search and comparison are available without registration; an account is required to save a request and place an order.',
      'welcome.buyer.title':    'Which part do you need?',
      'welcome.buyer.subtitle': 'Upload an Excel specification, drag in a photo, or describe the part — I’ll collect and compare supplier offers.',
      'welcome.seller.title':   'What’s on the agenda today?',
      'welcome.seller.subtitle':'Urgent tasks, incoming requests and shipments. Catalog, finance and team — on demand.',
      'welcome.operator.title': 'What’s active on the platform?',
      'welcome.operator.subtitle':'You manage every deal from payment to delivery, coordinating logistics, customs brokers and payments.',
      'welcome.operator_logist.title':'Logistics',
      'welcome.operator_logist.subtitle':'Shipments, containers, SLA — manage from chat.',
      'welcome.operator_customs.title':'Customs',
      'welcome.operator_customs.subtitle':'Cargo clearance, HS codes, documents, sanctions screening.',
      'welcome.operator_payment.title':'Payments',
      'welcome.operator_payment.subtitle':'Invoices, escrow, refunds — manage from chat.',
      'welcome.operator_manager.title':'KAM',
      'welcome.operator_manager.subtitle':'Key accounts: customers, projects, shipments, commissions.',
      'welcome.admin.title':    'Platform',
      'welcome.admin.subtitle': 'GMV, users, moderation — full marketplace control.',

      'pill.find_part':       'Find a part',
      'pill.compare_suppliers':'Compare suppliers',
      'pill.knowledge':       'Knowledge base',
      'pill.my_orders':       'My deals',
      'pill.open_rfq':        'Open requests',
      'pill.deposit':         'Deposit',
      'pill.auto_discount':   'Auto-discount',
      'pill.upload_price':    'Upload price list',
      'pill.my_products':     'My products',
      'pill.drawings':        'Drawings',
      'pill.drawings_by_part': 'Drawings by part #',
      'pill.customers':        'Customers',
      'pill.accruals':         'Accruals',
      'pill.my_deals':         'My deals',
      'pill.my_kam':           'My manager',
      'pill.market_twin':      'Market twin',
      'pill.customs_data':     'Customs',
      'pill.invite':           'Invite',
      'pill.verification':    'Verification',
      'pill.urgent':          'Urgent',
      'pill.to_ship':         'To ship',
      'pill.new_rfq':         'New requests',
      'pill.demand':          'Demand',
      'pill.overview':        'Overview',
      'pill.queue':           'Orders list',
      'pill.sla_breach':      'SLA breaches',
      'pill.payments_escrow': 'Payments / Escrow',
      'pill.customs':         'Customs',
      'pill.logistics':       'Logistics',
      'pill.kyb_suppliers':   'KYB suppliers',
      'pill.my_user_chats':    'My user chats',
      'pill.my_suppliers':    'My suppliers',
      'pill.claims':          'Claims',
      'pill.analytics':       'Analytics',
      'pill.rfq':             'Requests',
      'pill.rfq_semi':        'Needs approval · 15 min',
      'pill.rfq_manual':      'Manual review · 48 h',
      'pill.rfq_auto':        'Automatic matching',
      'rfq.mode.auto':        'Automatic matching',
      'rfq.mode.semi':        'Operator review',
      'rfq.mode.manual':      'Individual sourcing',
      'pill.history':         'History',
      'pill.support':         'Support',
      'pill.customs_summary': 'Customs summary',
      'pill.hs_code':         'HS code',
      'pill.sanctions':       'Sanctions',
      'pill.at_customs':      'At customs',
      'pill.escrow':          'Escrow',
      'pill.payments_stats':  'Analytics',
      'pill.awaiting_reserve':'Awaiting reserve',
      'pill.refunds':         'Refunds',
      'pill.gmv':             'GMV',
      'pill.users':           'Users',
      'pill.moderation':      'Moderation',
      'pill.catalog':         'Catalog',
      'pill.settings_admin':  'Settings',

      // Card title fallbacks
      'card.notification':    'Notification',
      'card.price_calc':      'Price quote',
      'card.confirm_action':  'Confirm action',
      'card.today':           'Today',
      'card.catalog':         'Catalog',
      'card.warehouses':      'Warehouses',
      'card.best_offers':     'Best offers',
      'card.comparison':      'Comparison',
      'card.history':         'History',
      'card.list':            'List',
      'card.enter_data':      'Enter data',
      'card.integration':     'Integration methods',
      'card.preview':         'Preview',
      'card.seller_queue':    'Seller queue',
      'card.shipping':        'Choose shipping',
      'card.match_results':   'Match results',
      'card.file':            'File',
      'card.untitled':        'Untitled',
      // Stock chips
      'stock.in_stock':       'In stock',
      'stock.not_available':  'Out of stock',
      'stock.on_order':       'On order',
      'stock.pcs':            'pcs',
      // Status mini-tags
      'tag.coming_soon':      'Soon',
      'tag.recommended':      'Recommended',
      'tag.active':           'Active',
      // Common buttons
      'common.confirm':       'Confirm',
      'common.cancel':        'Cancel',
      'common.send':          'Send',
      'common.error':         'Error',
      'common.you':           'You',
      'common.action_done':   'Action',
      'common.carrier':       'Carrier',
      'common.next':          'Next',
      'common.cancelled_note':'↩︎ Action cancelled',
      'common.do_not_cancel': 'Don’t cancel',
      // Working / loading messages (по категориям)
      'working.search.0':     'Searching catalog…',
      'working.search.1':     'Matching options…',
      'working.search.2':     'Checking stock…',
      'working.search.3':     'Comparing prices…',
      'working.rfq.0':        'Preparing request…',
      'working.rfq.1':        'Notifying suppliers…',
      'working.rfq.2':        'Creating request card…',
      'working.orders.0':     'Loading orders…',
      'working.orders.1':     'Sorting by date…',
      'working.orders.2':     'Checking statuses…',
      'working.shipment.0':   'Requesting tracking…',
      'working.shipment.1':   'Locating shipment…',
      'working.shipment.2':   'Computing ETA…',
      'working.budget.0':     'Calculating expenses…',
      'working.budget.1':     'Grouping by status…',
      'working.budget.2':     'Building report…',
      'working.analytics.0':  'Gathering metrics…',
      'working.analytics.1':  'Analyzing data…',
      'working.analytics.2':  'Forming summary…',
      'working.claim.0':      'Filing claim…',
      'working.claim.1':      'Notifying support…',
      'working.sla.0':        'Checking SLA…',
      'working.sla.1':        'Counting breaches…',
      'working.suppliers.0':  'Loading suppliers…',
      'working.suppliers.1':  'Computing ratings…',
      'working.default.0':    'Thinking…',
      'working.default.1':    'Analyzing request…',
      'working.default.2':    'Preparing response…',
      'working.default.3':    'Gathering info…',
      // Sidebar buckets
      'bucket.today':         'Today',
      'bucket.yesterday':     'Yesterday',
      'bucket.week':          'This week',
      'bucket.month':         'This month',
      'bucket.older':         'Older',
      // Prompts
      'prompt.rename_warehouse': 'New warehouse name:',
      'prompt.rename_chat':      'New chat name:',
      'prompt.copy_link':        'Copy the link and open it in a regular browser:',
    },

    'zh-hans': {
      'brand.name': 'Consolidator',
      'topbar.menu': '菜单',
      'topbar.notifications': '通知',
      'topbar.theme_light': '浅色主题',
      'topbar.theme_dark': '深色主题',
      'role.buyer': '采购方',
      'role.seller': '供应商',
      'role.operator': '运营',
      'role.admin': '管理员',

      'side.search': '搜索会话…',
      'side.new_chat': '新对话',
      'side.pinned': '置顶',
      'side.recent': '最近',
      'side.today': '今天',
      'side.yesterday': '昨天',
      'side.this_week': '本周',
      'side.earlier': '更早',
      'side.clear': '清除',
      'side.settings': '设置',
      'side.logout': '退出',
      'side.profile': '个人资料',

      'settings.title': '设置',
      'settings.close': '关闭',
      'settings.theme': '主题',
      'settings.theme.light': '浅色',
      'settings.theme.dark': '深色',
      'settings.language': '界面语言',
      'settings.notifications': '通知',
      'settings.density': '密度',
      'settings.account': '账户',
      'settings.logout': '退出',
      'settings.team': '公司团队',
      'settings.landing': '前往网站',
      'settings.export': '下载我的数据',
      'settings.password': '更改密码',
      'settings.privacy': '隐私与账户',

      'welcome.h1': '有什么可以帮您？',
      'welcome.p': '请求报价、发货、订单状态或上传文件 — 我会匹配供应商并完成交易。',
      'welcome.drop_hint': '拖入文件或按',
      'welcome.drop_paste': '从剪贴板粘贴',

      'btn.open': '打开',
      'btn.cancel': '取消',
      'btn.confirm': '确认',
      'btn.save': '保存',
      'btn.send': '发送',
      'btn.next': '下一步',
      'btn.back': '返回',
      'btn.close': '关闭',
      'btn.delete': '删除',
      'btn.edit': '编辑',
      'btn.add': '添加',
      'btn.copy': '复制',
      'btn.download': '下载',
      'btn.upload': '上传',
      'btn.search': '搜索',
      'btn.filter': '筛选',
      'btn.compare': '比较',
      'btn.details': '详情',
      'btn.pay': '支付',
      'btn.pay_balance': '支付尾款',
      'btn.pay_reserve': '预付 10%',

      'status.kp_pending': '报价处理中',
      'status.kp_ready': '报价已出',
      'status.reserved': '已预订',
      'status.in_production': '生产中',
      'status.ready_to_ship': '可发货',
      'status.transit_local': '本地运输中',
      'status.transit_abroad': '国际运输中',
      'status.customs': '海关清关',
      'status.delivered': '已送达',
      'status.accepted': '已验收',
      'status.closed': '已关闭',
      'status.disputed': '争议中',
      'status.cancelled': '已取消',

      'incoterm.fob': 'FOB',
      'incoterm.cip': 'CIP',
      'incoterm.ddp': 'DDP',

      'tbl.order': '订单',
      'tbl.buyer': '采购方',
      'tbl.seller': '供应商',
      'tbl.part': '零件号',
      'tbl.brand': '品牌',
      'tbl.qty': '数量',
      'tbl.price': '单价',
      'tbl.total': '合计',
      'tbl.currency': '币种',
      'tbl.status': '状态',
      'tbl.date': '日期',
      'tbl.deadline': '截止',
      'tbl.eta': 'ETA',
      'tbl.actor': '负责人',
      'tbl.action': '操作',
      'tbl.note': '备注',
      'tbl.warehouse': '仓库',
      'tbl.stock': '库存',
      'tbl.condition': '状况',
      'tbl.cond.oem': '原厂',
      'tbl.cond.aftermarket': '副厂',

      'toast.saved': '已保存',
      'toast.error': '错误',
      'toast.copied': '已复制',
      'toast.sent': '已发送',
      'toast.language_changed': '语言已更新',

      'guest.mode':              '公开预览',
      'welcome.guest.title':     '查找零件或比较报价',
      'welcome.guest.subtitle':  '输入零件号或名称，或上传规格文件。无需注册即可搜索和比较；保存询价和下单时需要账户。',
      'welcome.buyer.title':    '需要哪个零件？',
      'welcome.buyer.subtitle': '上传 Excel 规格、拖入照片或描述零件 — 我会汇总并比较供应商报价。',
      'welcome.seller.title':   '今天有什么任务？',
      'welcome.seller.subtitle':'紧急任务、新询价与发货。目录、财务和团队 — 按需调用。',
      'welcome.operator.title': '平台上正在处理什么？',
      'welcome.operator.subtitle':'您负责从付款到交付的整笔交易，协调物流、海关代理并控制付款。',
      'welcome.operator_logist.title':'物流',
      'welcome.operator_logist.subtitle':'发货、集装箱、SLA — 通过聊天管理。',
      'welcome.operator_customs.title':'海关',
      'welcome.operator_customs.subtitle':'清关货物、海关编码、文件、制裁筛查。',
      'welcome.operator_payment.title':'支付',
      'welcome.operator_payment.subtitle':'发票、托管、退款 — 通过聊天管理。',
      'welcome.operator_manager.title':'KAM',
      'welcome.operator_manager.subtitle':'关键客户：客户、项目、发货、提成。',
      'welcome.admin.title':    '平台',
      'welcome.admin.subtitle': 'GMV、用户、审核 — 整体市场管理。',

      'pill.find_part':       '查找零件',
      'pill.compare_suppliers':'比较供应商',
      'pill.knowledge':       '知识库',
      'pill.my_orders':       '我的交易',
      'pill.open_rfq':        '未结询价',
      'pill.deposit':         '押金',
      'pill.auto_discount':   '自动折扣',
      'pill.upload_price':    '上传价格表',
      'pill.my_products':     '我的商品',
      'pill.drawings':        '图纸',
      'pill.drawings_by_part': '按零件号查图纸',
      'pill.customers':        '客户',
      'pill.accruals':         '提成',
      'pill.my_deals':         '我的交易',
      'pill.my_kam':           '我的经理',
      'pill.market_twin':      '市场快照',
      'pill.customs_data':     '海关',
      'pill.invite':           '邀请',
      'pill.verification':    '验证',
      'pill.urgent':          '紧急',
      'pill.to_ship':         '待发货',
      'pill.new_rfq':         '新询价',
      'pill.demand':          '需求',
      'pill.overview':        '概览',
      'pill.queue':           '订单队列',
      'pill.sla_breach':      'SLA 违规',
      'pill.payments_escrow': '支付 / 托管',
      'pill.customs':         '海关',
      'pill.logistics':       '物流',
      'pill.my_user_chats':    '我的对话',
      'pill.kyb_suppliers':   '供应商 KYB',
      'pill.my_suppliers':    '我的供应商',
      'pill.claims':          '理赔',
      'pill.analytics':       '分析',
      'pill.rfq':             '询价',
      'pill.rfq_semi':        '待确认 · 15分钟',
      'pill.rfq_manual':      '人工匹配 · 48小时',
      'pill.rfq_auto':        '自动匹配',
      'rfq.mode.auto':        '自动匹配',
      'rfq.mode.semi':        '运营人员审核',
      'rfq.mode.manual':      '专属选品',
      'pill.history':         '历史',
      'pill.support':         '支持',
      'pill.customs_summary': '海关汇总',
      'pill.hs_code':         '海关编码',
      'pill.sanctions':       '制裁',
      'pill.at_customs':      '在海关',
      'pill.escrow':          '托管',
      'pill.payments_stats':  '分析',
      'pill.awaiting_reserve':'待预付',
      'pill.refunds':         '退款',
      'pill.gmv':             'GMV',
      'pill.users':           '用户',
      'pill.moderation':      '审核',
      'pill.catalog':         '目录',
      'pill.settings_admin':  '设置',

      'card.notification':    '通知',
      'card.price_calc':      '价格计算',
      'card.confirm_action':  '确认操作',
      'card.today':           '今天',
      'card.catalog':         '目录',
      'card.warehouses':      '仓库',
      'card.best_offers':     '最佳报价',
      'card.comparison':      '对比',
      'card.history':         '历史',
      'card.list':            '列表',
      'card.enter_data':      '输入数据',
      'card.integration':     '集成方式',
      'card.preview':         '预览',
      'card.seller_queue':    '卖家队列',
      'card.shipping':        '选择运输方式',
      'card.match_results':   '匹配结果',
      'card.file':            '文件',
      'card.untitled':        '未命名',
      'stock.in_stock':       '有货',
      'stock.not_available':  '无货',
      'stock.on_order':       '可订购',
      'stock.pcs':            '件',
      'tag.coming_soon':      '即将',
      'tag.recommended':      '推荐',
      'tag.active':           '激活',
      'common.confirm':       '确认',
      'common.cancel':        '取消',
      'common.send':          '发送',
      'common.error':         '错误',
      'common.you':           '您',
      'common.action_done':   '操作',
      'common.carrier':       '承运商',
      'common.next':          '下一步',
      'common.cancelled_note':'↩︎ 操作已取消',
      'common.do_not_cancel': '不取消',
      'working.search.0':     '正在搜索目录…',
      'working.search.1':     '匹配选项…',
      'working.search.2':     '检查库存…',
      'working.search.3':     '比较价格…',
      'working.rfq.0':        '准备询价…',
      'working.rfq.1':        '通知供应商…',
      'working.rfq.2':        '创建询价卡…',
      'working.orders.0':     '加载订单…',
      'working.orders.1':     '按日期排序…',
      'working.orders.2':     '检查状态…',
      'working.shipment.0':   '请求追踪…',
      'working.shipment.1':   '确认位置…',
      'working.shipment.2':   '计算到货时间…',
      'working.budget.0':     '计算支出…',
      'working.budget.1':     '按状态分组…',
      'working.budget.2':     '准备报告…',
      'working.analytics.0':  '收集指标…',
      'working.analytics.1':  '分析数据…',
      'working.analytics.2':  '生成摘要…',
      'working.claim.0':      '处理理赔…',
      'working.claim.1':      '通知支持…',
      'working.sla.0':        '检查 SLA…',
      'working.sla.1':        '统计违规…',
      'working.suppliers.0':  '加载供应商…',
      'working.suppliers.1':  '计算评级…',
      'working.default.0':    '思考中…',
      'working.default.1':    '分析请求…',
      'working.default.2':    '准备回复…',
      'working.default.3':    '收集信息…',
      'bucket.today':         '今天',
      'bucket.yesterday':     '昨天',
      'bucket.week':          '本周',
      'bucket.month':         '本月',
      'bucket.older':         '更早',
      'prompt.rename_warehouse': '新仓库名称：',
      'prompt.rename_chat':      '新聊天名称：',
      'prompt.copy_link':        '复制链接并在普通浏览器中打开：',
    },

    ar: {
      'brand.name': 'Consolidator',
      'topbar.menu': 'القائمة',
      'topbar.notifications': 'الإشعارات',
      'topbar.theme_light': 'السمة الفاتحة',
      'topbar.theme_dark': 'السمة الداكنة',
      'role.buyer': 'مشتري',
      'role.seller': 'مورّد',
      'role.operator': 'مشغّل',
      'role.admin': 'مسؤول',

      'side.search': 'بحث في المحادثات…',
      'side.new_chat': 'محادثة جديدة',
      'side.pinned': 'مثبّت',
      'side.recent': 'الأحدث',
      'side.today': 'اليوم',
      'side.yesterday': 'أمس',
      'side.this_week': 'هذا الأسبوع',
      'side.earlier': 'سابقاً',
      'side.clear': 'مسح',
      'side.settings': 'الإعدادات',
      'side.logout': 'تسجيل الخروج',
      'side.profile': 'الملف الشخصي',

      'settings.title': 'الإعدادات',
      'settings.close': 'إغلاق',
      'settings.theme': 'السمة',
      'settings.theme.light': 'فاتحة',
      'settings.theme.dark': 'داكنة',
      'settings.language': 'لغة الواجهة',
      'settings.notifications': 'الإشعارات',
      'settings.density': 'الكثافة',
      'settings.account': 'الحساب',
      'settings.logout': 'تسجيل الخروج',
      'settings.team': 'فريق الشركة',
      'settings.landing': 'الذهاب إلى الموقع',
      'settings.export': 'تنزيل بياناتي',
      'settings.password': 'تغيير كلمة المرور',
      'settings.privacy': 'الخصوصية والحساب',

      'welcome.h1': 'كيف يمكنني المساعدة؟',
      'welcome.p': 'اطلب عرض سعر، أو شحنة، أو حالة طلب، أو ارفع ملفاً — سأبحث عن موردين وأتممنا الصفقة.',
      'welcome.drop_hint': 'أفلت ملفاً أو اضغط',
      'welcome.drop_paste': 'للصق من الحافظة',

      'btn.open': 'فتح',
      'btn.cancel': 'إلغاء',
      'btn.confirm': 'تأكيد',
      'btn.save': 'حفظ',
      'btn.send': 'إرسال',
      'btn.next': 'التالي',
      'btn.back': 'رجوع',
      'btn.close': 'إغلاق',
      'btn.delete': 'حذف',
      'btn.edit': 'تحرير',
      'btn.add': 'إضافة',
      'btn.copy': 'نسخ',
      'btn.download': 'تنزيل',
      'btn.upload': 'رفع',
      'btn.search': 'بحث',
      'btn.filter': 'تصفية',
      'btn.compare': 'مقارنة',
      'btn.details': 'التفاصيل',
      'btn.pay': 'الدفع',
      'btn.pay_balance': 'دفع المتبقي',
      'btn.pay_reserve': 'حجز 10%',

      'status.kp_pending': 'عرض السعر قيد الإعداد',
      'status.kp_ready': 'عرض السعر جاهز',
      'status.reserved': 'محجوز',
      'status.in_production': 'قيد الإنتاج',
      'status.ready_to_ship': 'جاهز للشحن',
      'status.transit_local': 'في الطريق (محلي)',
      'status.transit_abroad': 'في الطريق (دولي)',
      'status.customs': 'في الجمارك',
      'status.delivered': 'تم التسليم',
      'status.accepted': 'تم القبول',
      'status.closed': 'مغلق',
      'status.disputed': 'نزاع',
      'status.cancelled': 'ملغى',

      'incoterm.fob': 'FOB',
      'incoterm.cip': 'CIP',
      'incoterm.ddp': 'DDP',

      'tbl.order': 'الطلب',
      'tbl.buyer': 'المشتري',
      'tbl.seller': 'المورّد',
      'tbl.part': 'رقم القطعة',
      'tbl.brand': 'الماركة',
      'tbl.qty': 'الكمية',
      'tbl.price': 'السعر',
      'tbl.total': 'الإجمالي',
      'tbl.currency': 'العملة',
      'tbl.status': 'الحالة',
      'tbl.date': 'التاريخ',
      'tbl.deadline': 'الموعد النهائي',
      'tbl.eta': 'الوصول المتوقع',
      'tbl.actor': 'المسؤول',
      'tbl.action': 'الإجراء',
      'tbl.note': 'ملاحظة',
      'tbl.warehouse': 'المستودع',
      'tbl.stock': 'المخزون',
      'tbl.condition': 'الحالة',
      'tbl.cond.oem': 'أصلي',
      'tbl.cond.aftermarket': 'بديل',

      'toast.saved': 'تم الحفظ',
      'toast.error': 'خطأ',
      'toast.copied': 'تم النسخ',
      'toast.sent': 'تم الإرسال',
      'toast.language_changed': 'تم تحديث اللغة',

      'guest.mode':              'معاينة عامة',
      'welcome.guest.title':     'ابحث عن قطعة أو قارن العروض',
      'welcome.guest.subtitle':  'أدخل رقم القطعة أو اسمها، أو ارفع ملف المواصفات. البحث والمقارنة متاحان دون تسجيل؛ يلزم حساب لحفظ الطلب وإتمام الشراء.',
      'welcome.buyer.title':    'ما القطعة التي تبحث عنها؟',
      'welcome.buyer.subtitle': 'ارفع مواصفات Excel أو اسحب صورة أو صف القطعة — سأجمع عروض الموردين وأقارنها.',
      'welcome.seller.title':   'ما المهام لليوم؟',
      'welcome.seller.subtitle':'مهام عاجلة، طلبات أسعار واردة، وشحنات. الفهرس والمالية والفريق — عند الطلب.',
      'welcome.operator.title': 'ما الذي يجري على المنصة؟',
      'welcome.operator.subtitle':'أنت تدير الصفقة بأكملها من الدفع إلى التسليم، وتنسق اللوجستيات والوسطاء الجمركيين وتراقب المدفوعات.',
      'welcome.operator_logist.title':'اللوجستيات',
      'welcome.operator_logist.subtitle':'الشحنات والحاويات و SLA — أدرها من المحادثة.',
      'welcome.operator_customs.title':'الجمارك',
      'welcome.operator_customs.subtitle':'البضائع قيد التخليص، رموز HS، الوثائق، فحص العقوبات.',
      'welcome.operator_payment.title':'المدفوعات',
      'welcome.operator_payment.subtitle':'الفواتير والضمان والاستردادات — أدرها من المحادثة.',
      'welcome.operator_manager.title':'KAM',
      'welcome.operator_manager.subtitle':'العملاء الرئيسيون: العملاء، المشاريع، الشحنات، العمولات.',
      'welcome.admin.title':    'المنصّة',
      'welcome.admin.subtitle': 'GMV، المستخدمون، الإشراف — التحكم الكامل بالمنصّة.',

      'pill.find_part':       'البحث عن قطعة',
      'pill.compare_suppliers':'مقارنة الموردين',
      'pill.knowledge':       'قاعدة المعرفة',
      'pill.my_orders':       'صفقاتي',
      'pill.open_rfq':        'استفسارات مفتوحة',
      'pill.deposit':         'وديعة',
      'pill.auto_discount':   'خصم تلقائي',
      'pill.upload_price':    'رفع قائمة الأسعار',
      'pill.my_products':     'منتجاتي',
      'pill.drawings':        'الرسومات',
      'pill.drawings_by_part': 'الرسومات حسب رقم القطعة',
      'pill.customers':        'العملاء',
      'pill.accruals':         'المستحقات',
      'pill.my_deals':         'صفقاتي',
      'pill.my_kam':           'مديري',
      'pill.market_twin':      'صورة السوق',
      'pill.customs_data':     'الجمارك',
      'pill.invite':           'دعوة',
      'pill.verification':    'التحقق',
      'pill.urgent':          'عاجل',
      'pill.to_ship':         'جاهز للشحن',
      'pill.new_rfq':         'استفسارات جديدة',
      'pill.demand':          'الطلب',
      'pill.overview':        'نظرة عامة',
      'pill.queue':           'قائمة الطلبات',
      'pill.sla_breach':      'انتهاكات SLA',
      'pill.payments_escrow': 'مدفوعات / ضمان',
      'pill.customs':         'الجمارك',
      'pill.my_user_chats':    'محادثاتي',
      'pill.logistics':       'اللوجستيات',
      'pill.kyb_suppliers':   'KYB الموردين',
      'pill.my_suppliers':    'الموردون',
      'pill.claims':          'الشكاوى',
      'pill.analytics':       'التحليلات',
      'pill.rfq':             'الاستفسارات',
      'pill.rfq_semi':        'بحاجة إلى موافقة · 15 دقيقة',
      'pill.rfq_manual':      'اختيار يدوي · 48 ساعة',
      'pill.rfq_auto':        'اختيار تلقائي',
      'rfq.mode.auto':        'اختيار تلقائي',
      'rfq.mode.semi':        'مراجعة المشغل',
      'rfq.mode.manual':      'اختيار مخصص',
      'pill.history':         'السجل',
      'pill.support':         'الدعم',
      'pill.customs_summary': 'ملخص الجمارك',
      'pill.hs_code':         'رمز HS',
      'pill.sanctions':       'العقوبات',
      'pill.at_customs':      'في الجمارك',
      'pill.escrow':          'الضمان',
      'pill.payments_stats':  'التحليلات',
      'pill.awaiting_reserve':'بانتظار الحجز',
      'pill.refunds':         'الاستردادات',
      'pill.gmv':             'GMV',
      'pill.users':           'المستخدمون',
      'pill.moderation':      'الإشراف',
      'pill.catalog':         'الفهرس',
      'pill.settings_admin':  'الإعدادات',

      'card.notification':    'إشعار',
      'card.price_calc':      'حساب السعر',
      'card.confirm_action':  'تأكيد الإجراء',
      'card.today':           'اليوم',
      'card.catalog':         'الفهرس',
      'card.warehouses':      'المستودعات',
      'card.best_offers':     'أفضل العروض',
      'card.comparison':      'مقارنة',
      'card.history':         'السجل',
      'card.list':            'قائمة',
      'card.enter_data':      'أدخل البيانات',
      'card.integration':     'طرق التكامل',
      'card.preview':         'معاينة',
      'card.seller_queue':    'قائمة المورّد',
      'card.shipping':        'اختر الشحن',
      'card.match_results':   'نتائج المطابقة',
      'card.file':            'ملف',
      'card.untitled':        'بدون عنوان',
      'stock.in_stock':       'متوفر',
      'stock.not_available':  'غير متوفر',
      'stock.on_order':       'بطلب',
      'stock.pcs':            'قطعة',
      'tag.coming_soon':      'قريباً',
      'tag.recommended':      'موصى به',
      'tag.active':           'نشط',
      'common.confirm':       'تأكيد',
      'common.cancel':        'إلغاء',
      'common.send':          'إرسال',
      'common.error':         'خطأ',
      'common.you':           'أنت',
      'common.action_done':   'إجراء',
      'common.carrier':       'الناقل',
      'common.next':          'التالي',
      'common.cancelled_note':'↩︎ تم إلغاء الإجراء',
      'common.do_not_cancel': 'لا تلغ',
      'working.search.0':     'جارٍ البحث في الفهرس…',
      'working.search.1':     'مطابقة الخيارات…',
      'working.search.2':     'فحص التوفّر…',
      'working.search.3':     'مقارنة الأسعار…',
      'working.rfq.0':        'إعداد الطلب…',
      'working.rfq.1':        'إعلام الموردين…',
      'working.rfq.2':        'إنشاء بطاقة الاستفسار…',
      'working.orders.0':     'تحميل الطلبات…',
      'working.orders.1':     'الفرز حسب التاريخ…',
      'working.orders.2':     'فحص الحالات…',
      'working.shipment.0':   'طلب التتبّع…',
      'working.shipment.1':   'تحديد الموقع…',
      'working.shipment.2':   'حساب وقت الوصول…',
      'working.budget.0':     'حساب المصاريف…',
      'working.budget.1':     'تجميع حسب الحالة…',
      'working.budget.2':     'إعداد التقرير…',
      'working.analytics.0':  'جمع المقاييس…',
      'working.analytics.1':  'تحليل البيانات…',
      'working.analytics.2':  'صياغة الملخّص…',
      'working.claim.0':      'تسجيل الشكوى…',
      'working.claim.1':      'إعلام الدعم…',
      'working.sla.0':        'فحص SLA…',
      'working.sla.1':        'حساب الانتهاكات…',
      'working.suppliers.0':  'تحميل الموردين…',
      'working.suppliers.1':  'حساب التقييمات…',
      'working.default.0':    'يفكّر…',
      'working.default.1':    'تحليل الطلب…',
      'working.default.2':    'إعداد الجواب…',
      'working.default.3':    'جمع المعلومات…',
      'bucket.today':         'اليوم',
      'bucket.yesterday':     'أمس',
      'bucket.week':          'هذا الأسبوع',
      'bucket.month':         'هذا الشهر',
      'bucket.older':         'أقدم',
      'prompt.rename_warehouse': 'اسم المستودع الجديد:',
      'prompt.rename_chat':      'اسم المحادثة الجديد:',
      'prompt.copy_link':        'انسخ الرابط وافتحه في متصفح عادي:',
    },

    es: {
      'brand.name': 'Consolidator',
      'topbar.menu': 'Menú',
      'topbar.notifications': 'Notificaciones',
      'topbar.theme_light': 'Tema claro',
      'topbar.theme_dark': 'Tema oscuro',
      'role.buyer': 'Comprador',
      'role.seller': 'Proveedor',
      'role.operator': 'Operador',
      'role.admin': 'Administrador',

      'side.search': 'Buscar chats…',
      'side.new_chat': 'Nuevo chat',
      'side.pinned': 'Fijados',
      'side.recent': 'Recientes',
      'side.today': 'Hoy',
      'side.yesterday': 'Ayer',
      'side.this_week': 'Esta semana',
      'side.earlier': 'Anteriores',
      'side.clear': 'Limpiar',
      'side.settings': 'Ajustes',
      'side.logout': 'Cerrar sesión',
      'side.profile': 'Perfil',

      'settings.title': 'Ajustes',
      'settings.close': 'Cerrar',
      'settings.theme': 'Tema',
      'settings.theme.light': 'Claro',
      'settings.theme.dark': 'Oscuro',
      'settings.language': 'Idioma de la interfaz',
      'settings.notifications': 'Notificaciones',
      'settings.density': 'Densidad',
      'settings.account': 'Cuenta',
      'settings.logout': 'Cerrar sesión',
      'settings.team': 'Equipo de empresa',
      'settings.landing': 'Ir al sitio web',
      'settings.export': 'Descargar mis datos',
      'settings.password': 'Cambiar contraseña',
      'settings.privacy': 'Privacidad y cuenta',

      'welcome.h1': '¿En qué puedo ayudar?',
      'welcome.p': 'Solicita una cotización, envío, estado del pedido o sube un archivo — buscaré proveedores y cerraremos el trato.',
      'welcome.drop_hint': 'Suelta un archivo o pulsa',
      'welcome.drop_paste': 'para pegar desde el portapapeles',

      'btn.open': 'Abrir',
      'btn.cancel': 'Cancelar',
      'btn.confirm': 'Confirmar',
      'btn.save': 'Guardar',
      'btn.send': 'Enviar',
      'btn.next': 'Siguiente',
      'btn.back': 'Atrás',
      'btn.close': 'Cerrar',
      'btn.delete': 'Eliminar',
      'btn.edit': 'Editar',
      'btn.add': 'Añadir',
      'btn.copy': 'Copiar',
      'btn.download': 'Descargar',
      'btn.upload': 'Subir',
      'btn.search': 'Buscar',
      'btn.filter': 'Filtrar',
      'btn.compare': 'Comparar',
      'btn.details': 'Detalles',
      'btn.pay': 'Pagar',
      'btn.pay_balance': 'Pagar saldo',
      'btn.pay_reserve': 'Reservar 10%',

      'status.kp_pending': 'Cotización en curso',
      'status.kp_ready': 'Cotización lista',
      'status.reserved': 'Reservado',
      'status.in_production': 'En producción',
      'status.ready_to_ship': 'Listo para envío',
      'status.transit_local': 'En tránsito (local)',
      'status.transit_abroad': 'En tránsito (intl.)',
      'status.customs': 'En aduana',
      'status.delivered': 'Entregado',
      'status.accepted': 'Aceptado',
      'status.closed': 'Cerrado',
      'status.disputed': 'En disputa',
      'status.cancelled': 'Cancelado',

      'incoterm.fob': 'FOB',
      'incoterm.cip': 'CIP',
      'incoterm.ddp': 'DDP',

      'tbl.order': 'Pedido',
      'tbl.buyer': 'Comprador',
      'tbl.seller': 'Proveedor',
      'tbl.part': 'Pieza №',
      'tbl.brand': 'Marca',
      'tbl.qty': 'Cant.',
      'tbl.price': 'Precio',
      'tbl.total': 'Total',
      'tbl.currency': 'Moneda',
      'tbl.status': 'Estado',
      'tbl.date': 'Fecha',
      'tbl.deadline': 'Plazo',
      'tbl.eta': 'ETA',
      'tbl.actor': 'Responsable',
      'tbl.action': 'Acción',
      'tbl.note': 'Nota',
      'tbl.warehouse': 'Almacén',
      'tbl.stock': 'Stock',
      'tbl.condition': 'Estado',
      'tbl.cond.oem': 'OEM',
      'tbl.cond.aftermarket': 'Alt.',

      'toast.saved': 'Guardado',
      'toast.error': 'Error',
      'toast.copied': 'Copiado',
      'toast.sent': 'Enviado',
      'toast.language_changed': 'Idioma actualizado',

      'guest.mode':              'Vista pública',
      'welcome.guest.title':     'Busca una pieza o compara ofertas',
      'welcome.guest.subtitle':  'Introduce el número o nombre de la pieza, o sube una especificación. La búsqueda y comparación están disponibles sin registro; necesitarás una cuenta para guardar la solicitud y realizar el pedido.',
      'welcome.buyer.title':    '¿Qué pieza buscas?',
      'welcome.buyer.subtitle': 'Sube una especificación en Excel, arrastra una foto o describe la pieza — reuniré y compararé ofertas de proveedores.',
      'welcome.seller.title':   '¿Qué hay para hoy?',
      'welcome.seller.subtitle':'Tareas urgentes, solicitudes entrantes y envíos. Catálogo, finanzas y equipo — bajo demanda.',
      'welcome.operator.title': '¿Qué está activo en la plataforma?',
      'welcome.operator.subtitle':'Gestionas cada operación desde el pago hasta la entrega, coordinando logística, agentes aduaneros y pagos.',
      'welcome.operator_logist.title':'Logística',
      'welcome.operator_logist.subtitle':'Envíos, contenedores, SLA — gestiona desde el chat.',
      'welcome.operator_customs.title':'Aduana',
      'welcome.operator_customs.subtitle':'Despacho aduanero, códigos HS, documentos, control de sanciones.',
      'welcome.operator_payment.title':'Pagos',
      'welcome.operator_payment.subtitle':'Facturas, depósito en garantía, reembolsos — gestiona desde el chat.',
      'welcome.operator_manager.title':'KAM',
      'welcome.operator_manager.subtitle':'Clientes clave: clientes, proyectos, envíos, comisiones.',
      'welcome.admin.title':    'Plataforma',
      'welcome.admin.subtitle': 'GMV, usuarios, moderación — control completo del marketplace.',

      'pill.find_part':       'Buscar una pieza',
      'pill.compare_suppliers':'Comparar proveedores',
      'pill.knowledge':       'Base de conocimiento',
      'pill.my_orders':       'Mis operaciones',
      'pill.open_rfq':        'Solicitudes abiertas',
      'pill.deposit':         'Depósito',
      'pill.auto_discount':   'Descuento automático',
      'pill.upload_price':    'Subir lista de precios',
      'pill.my_products':     'Mis productos',
      'pill.drawings':        'Planos',
      'pill.drawings_by_part': 'Planos por artículo',
      'pill.customers':        'Clientes',
      'pill.accruals':         'Comisiones',
      'pill.my_deals':         'Mis tratos',
      'pill.my_kam':           'Mi gerente',
      'pill.market_twin':      'Mapa del mercado',
      'pill.customs_data':     'Aduanas',
      'pill.invite':           'Invitar',
      'pill.verification':    'Verificación',
      'pill.urgent':          'Urgente',
      'pill.to_ship':         'Por enviar',
      'pill.new_rfq':         'Solicitudes nuevas',
      'pill.demand':          'Demanda',
      'pill.overview':        'Resumen',
      'pill.queue':           'Cola de pedidos',
      'pill.sla_breach':      'Incumplimientos SLA',
      'pill.payments_escrow': 'Pagos / Depósito',
      'pill.my_user_chats':    'Mis diálogos',
      'pill.customs':         'Aduana',
      'pill.logistics':       'Logística',
      'pill.kyb_suppliers':   'KYB proveedores',
      'pill.my_suppliers':    'Mis proveedores',
      'pill.claims':          'Reclamos',
      'pill.analytics':       'Analítica',
      'pill.rfq':             'Solicitudes',
      'pill.rfq_semi':        'Requiere aprobación · 15 min',
      'pill.rfq_manual':      'Selección manual · 48 h',
      'pill.rfq_auto':        'Selección automática',
      'rfq.mode.auto':        'Selección automática',
      'rfq.mode.semi':        'Revisión del operador',
      'rfq.mode.manual':      'Selección personalizada',
      'pill.history':         'Historial',
      'pill.support':         'Soporte',
      'pill.customs_summary': 'Resumen aduana',
      'pill.hs_code':         'Código HS',
      'pill.sanctions':       'Sanciones',
      'pill.at_customs':      'En aduana',
      'pill.escrow':          'Depósito',
      'pill.payments_stats':  'Analítica',
      'pill.awaiting_reserve':'Pendiente de reserva',
      'pill.refunds':         'Reembolsos',
      'pill.gmv':             'GMV',
      'pill.users':           'Usuarios',
      'pill.moderation':      'Moderación',
      'pill.catalog':         'Catálogo',
      'pill.settings_admin':  'Configuración',

      'card.notification':    'Notificación',
      'card.price_calc':      'Cálculo de precio',
      'card.confirm_action':  'Confirmar acción',
      'card.today':           'Hoy',
      'card.catalog':         'Catálogo',
      'card.warehouses':      'Almacenes',
      'card.best_offers':     'Mejores ofertas',
      'card.comparison':      'Comparación',
      'card.history':         'Historial',
      'card.list':            'Lista',
      'card.enter_data':      'Introduce los datos',
      'card.integration':     'Métodos de integración',
      'card.preview':         'Vista previa',
      'card.seller_queue':    'Cola del vendedor',
      'card.shipping':        'Elige el envío',
      'card.match_results':   'Resultados',
      'card.file':            'Archivo',
      'card.untitled':        'Sin título',
      'stock.in_stock':       'En stock',
      'stock.not_available':  'Sin stock',
      'stock.on_order':       'Bajo pedido',
      'stock.pcs':            'uds',
      'tag.coming_soon':      'Próximamente',
      'tag.recommended':      'Recomendado',
      'tag.active':           'Activo',
      'common.confirm':       'Confirmar',
      'common.cancel':        'Cancelar',
      'common.send':          'Enviar',
      'common.error':         'Error',
      'common.you':           'Tú',
      'common.action_done':   'Acción',
      'common.carrier':       'Transportista',
      'common.next':          'Siguiente',
      'common.cancelled_note':'↩︎ Acción cancelada',
      'common.do_not_cancel': 'No cancelar',
      'working.search.0':     'Buscando en el catálogo…',
      'working.search.1':     'Buscando opciones…',
      'working.search.2':     'Verificando stock…',
      'working.search.3':     'Comparando precios…',
      'working.rfq.0':        'Preparando solicitud…',
      'working.rfq.1':        'Notificando proveedores…',
      'working.rfq.2':        'Creando tarjeta de solicitud…',
      'working.orders.0':     'Cargando pedidos…',
      'working.orders.1':     'Ordenando por fecha…',
      'working.orders.2':     'Verificando estados…',
      'working.shipment.0':   'Solicitando seguimiento…',
      'working.shipment.1':   'Localizando envío…',
      'working.shipment.2':   'Calculando ETA…',
      'working.budget.0':     'Calculando gastos…',
      'working.budget.1':     'Agrupando por estado…',
      'working.budget.2':     'Preparando informe…',
      'working.analytics.0':  'Recopilando métricas…',
      'working.analytics.1':  'Analizando datos…',
      'working.analytics.2':  'Formando resumen…',
      'working.claim.0':      'Procesando reclamación…',
      'working.claim.1':      'Notificando soporte…',
      'working.sla.0':        'Verificando SLA…',
      'working.sla.1':        'Contando incumplimientos…',
      'working.suppliers.0':  'Cargando proveedores…',
      'working.suppliers.1':  'Calculando calificaciones…',
      'working.default.0':    'Pensando…',
      'working.default.1':    'Analizando solicitud…',
      'working.default.2':    'Preparando respuesta…',
      'working.default.3':    'Recopilando información…',
      'bucket.today':         'Hoy',
      'bucket.yesterday':     'Ayer',
      'bucket.week':          'Esta semana',
      'bucket.month':         'Este mes',
      'bucket.older':         'Más antiguo',
      'prompt.rename_warehouse': 'Nuevo nombre del almacén:',
      'prompt.rename_chat':      'Nuevo nombre del chat:',
      'prompt.copy_link':        'Copia el enlace y ábrelo en un navegador normal:',
    },
  };

  // ── Аддендум: ключи = РУССКИЕ строки. Используется esc()/tr() в chat-first.js
  // для прямого перевода клиентских UI-литералов (без выдумывания семантических
  // ключей). ru-перевод не нужен — t() возвращает ключ как есть. Пополняется по мере
  // покрытия фронта; неизвестные строки (данные/бэкенд) проходят без изменений.
  const _ADDENDUM = {
    en: {
      'купил, едет': 'bought, in transit', 'жду котировок': 'awaiting quotes',
      'Нет чатов': 'No chats', 'Записей нет': 'No records',
      'Поступлений пока не было.': 'No income yet.', 'проект': 'project',
      'Убрать': 'Remove', 'Добавить пилюлю': 'Add a pill',
      'Меню пилюль: добавить, закрепить, переставить': 'Pill menu: add, pin, reorder',
      'недостаточно': 'insufficient', 'всё подключено': 'all connected',
      'нет': 'none', 'Загрузка...': 'Loading...', 'Поиск...': 'Searching...',
    },
    'zh-hans': {
      'купил, едет': '已购买，运输中', 'жду котировок': '等待报价',
      'Нет чатов': '暂无对话', 'Записей нет': '暂无记录',
      'Поступлений пока не было.': '暂无入账。', 'проект': '项目',
      'Убрать': '移除', 'Добавить пилюлю': '添加快捷按钮',
      'Меню пилюль: добавить, закрепить, переставить': '按钮菜单：添加、固定、排序',
      'недостаточно': '不足', 'всё подключено': '全部已连接',
      'нет': '无', 'Загрузка...': '加载中…', 'Поиск...': '搜索中…',
    },
    es: {
      'купил, едет': 'comprado, en tránsito', 'жду котировок': 'esperando cotizaciones',
      'Нет чатов': 'Sin chats', 'Записей нет': 'Sin registros',
      'Поступлений пока не было.': 'Aún no hay ingresos.', 'проект': 'proyecto',
      'Убрать': 'Quitar', 'Добавить пилюлю': 'Añadir botón',
      'Меню пилюль: добавить, закрепить, переставить': 'Menú de botones: añadir, fijar, reordenar',
      'недостаточно': 'insuficiente', 'всё подключено': 'todo conectado',
      'нет': 'ninguno', 'Загрузка...': 'Cargando...', 'Поиск...': 'Buscando...',
    },
    ar: {
      'купил, едет': 'تم الشراء، قيد النقل', 'жду котировок': 'بانتظار العروض',
      'Нет чатов': 'لا محادثات', 'Записей нет': 'لا سجلات',
      'Поступлений пока не было.': 'لا إيداعات بعد.', 'проект': 'مشروع',
      'Убрать': 'إزالة', 'Добавить пилюлю': 'إضافة زر',
      'Меню пилюль: добавить, закрепить, переставить': 'قائمة الأزرار: إضافة، تثبيت، إعادة ترتيب',
      'недостаточно': 'غير كافٍ', 'всё подключено': 'كل شيء متصل',
      'нет': 'لا', 'Загрузка...': 'جارٍ التحميل...', 'Поиск...': 'جارٍ البحث...',
    },
  };
  Object.keys(_ADDENDUM).forEach(function (l) {
    DICT[l] = DICT[l] || {};
    Object.assign(DICT[l], _ADDENDUM[l]);
  });
  // ru: ключ=русский (идентичность) — иначе под ru сработает en-фолбэк и русский
  // интерфейс покажет английский для этих ключей.
  DICT.ru = DICT.ru || {};
  Object.keys(_ADDENDUM.en).forEach(function (k) {
    if (DICT.ru[k] == null) DICT.ru[k] = k;
  });

  // Аддендум 2: строки клиентских карточек/форм (форма доставки, навигация, метки).
  const _ADDENDUM2 = {
    en: {
      '📍 Куда доставить?': '📍 Where to deliver?', 'Страна': 'Country',
      '(для CIP/DDP)': '(for CIP/DDP)', 'Город / место прибытия': 'City / arrival point',
      'Адрес доставки': 'Delivery address', '(улица, дом · для DDP)': '(street, building · for DDP)',
      'Начните вводить страну…': 'Start typing a country…', 'Напр.: Москва': 'e.g.: Moscow',
      'Напр.: ул. Профсоюзная 84, корп. 5, офис 12': 'e.g.: 84 Profsoyuznaya St., bldg 5, office 12',
      '🧮 Рассчитать цены CIP / DDP →': '🧮 Calculate CIP / DDP prices →',
      '🚚 ДОСТАВКА': '🚚 DELIVERY', 'Назад': 'Back', 'Главная': 'Home',
      '← Назад': '← Back', '🏠 Главная': '🏠 Home', '💡 Также можете:': '💡 You can also:',
      'Морской порт': 'Sea port', 'Город прибытия': 'Arrival city', 'Страна отправления': 'Country of departure',
      'до вашего порта': 'to your port', 'самовывоз из порта': 'self-pickup from the port',
      'Связаться с поставщиком': 'Contact the supplier', 'Список поставщиков': 'Supplier list',
      'Стать поставщиком': 'Become a supplier', 'По поставщикам': 'By suppliers',
      '🚚 Выберите базис (FOB — самовывоз без доплат)': '🚚 Choose terms (FOB — self-pickup, no surcharge)',
      'Откуда': 'From', 'до двери, all-in': 'to the door, all-in', 'недоступно': 'unavailable',
      'укажите место прибытия': 'specify the arrival point', 'нет тарифа на фрахт': 'no freight rate',
    },
    'zh-hans': {
      '📍 Куда доставить?': '📍 送货至何处？', 'Страна': '国家',
      '(для CIP/DDP)': '（用于 CIP/DDP）', 'Город / место прибытия': '城市 / 到货地点',
      'Адрес доставки': '送货地址', '(улица, дом · для DDP)': '（街道、门牌 · 用于 DDP）',
      'Начните вводить страну…': '开始输入国家…', 'Напр.: Москва': '例如：莫斯科',
      'Напр.: ул. Профсоюзная 84, корп. 5, офис 12': '例如：Profsoyuznaya 街 84 号 5 栋 12 室',
      '🧮 Рассчитать цены CIP / DDP →': '🧮 计算 CIP / DDP 价格 →',
      '🚚 ДОСТАВКА': '🚚 配送', 'Назад': '返回', 'Главная': '首页',
      '← Назад': '← 返回', '🏠 Главная': '🏠 首页', '💡 Также можете:': '💡 您还可以：',
      'Морской порт': '海港', 'Город прибытия': '到货城市', 'Страна отправления': '发货国',
      'до вашего порта': '至您的港口', 'самовывоз из порта': '从港口自提',
      'Связаться с поставщиком': '联系供应商', 'Список поставщиков': '供应商列表',
      'Стать поставщиком': '成为供应商', 'По поставщикам': '按供应商',
      '🚚 Выберите базис (FOB — самовывоз без доплат)': '🚚 选择贸易术语（FOB — 自提，无附加费）',
      'Откуда': '出发地', 'до двери, all-in': '门到门，全包', 'недоступно': '不可用',
      'укажите место прибытия': '请填写到货地点', 'нет тарифа на фрахт': '无运费报价',
    },
    es: {
      '📍 Куда доставить?': '📍 ¿A dónde entregar?', 'Страна': 'País',
      '(для CIP/DDP)': '(para CIP/DDP)', 'Город / место прибытия': 'Ciudad / lugar de llegada',
      'Адрес доставки': 'Dirección de entrega', '(улица, дом · для DDP)': '(calle, número · para DDP)',
      'Начните вводить страну…': 'Empiece a escribir un país…', 'Напр.: Москва': 'P. ej.: Moscú',
      'Напр.: ул. Профсоюзная 84, корп. 5, офис 12': 'P. ej.: calle Profsoyuznaya 84, bloque 5, oficina 12',
      '🧮 Рассчитать цены CIP / DDP →': '🧮 Calcular precios CIP / DDP →',
      '🚚 ДОСТАВКА': '🚚 ENTREGA', 'Назад': 'Atrás', 'Главная': 'Inicio',
      '← Назад': '← Atrás', '🏠 Главная': '🏠 Inicio', '💡 Также можете:': '💡 También puede:',
      'Морской порт': 'Puerto marítimo', 'Город прибытия': 'Ciudad de llegada', 'Страна отправления': 'País de origen',
      'до вашего порта': 'hasta su puerto', 'самовывоз из порта': 'recogida en el puerto',
      'Связаться с поставщиком': 'Contactar con el proveedor', 'Список поставщиков': 'Lista de proveedores',
      'Стать поставщиком': 'Hacerse proveedor', 'По поставщикам': 'Por proveedores',
      '🚚 Выберите базис (FOB — самовывоз без доплат)': '🚚 Elija el incoterm (FOB — recogida, sin recargo)',
      'Откуда': 'Origen', 'до двери, all-in': 'puerta a puerta, todo incluido', 'недоступно': 'no disponible',
      'укажите место прибытия': 'indique el lugar de llegada', 'нет тарифа на фрахт': 'sin tarifa de flete',
    },
    ar: {
      '📍 Куда доставить?': '📍 إلى أين التسليم؟', 'Страна': 'الدولة',
      '(для CIP/DDP)': '(لـ CIP/DDP)', 'Город / место прибытия': 'المدينة / مكان الوصول',
      'Адрес доставки': 'عنوان التسليم', '(улица, дом · для DDP)': '(الشارع، المبنى · لـ DDP)',
      'Начните вводить страну…': 'ابدأ كتابة الدولة…', 'Напр.: Москва': 'مثال: موسكو',
      'Напр.: ул. Профсоюзная 84, корп. 5, офис 12': 'مثال: شارع Profsoyuznaya 84، مبنى 5، مكتب 12',
      '🧮 Рассчитать цены CIP / DDP →': '🧮 احسب أسعار CIP / DDP ←',
      '🚚 ДОСТАВКА': '🚚 التسليم', 'Назад': 'رجوع', 'Главная': 'الرئيسية',
      '← Назад': '← رجوع', '🏠 Главная': '🏠 الرئيسية', '💡 Также можете:': '💡 يمكنك أيضاً:',
      'Морской порт': 'ميناء بحري', 'Город прибытия': 'مدينة الوصول', 'Страна отправления': 'دولة المغادرة',
      'до вашего порта': 'حتى مينائك', 'самовывоз из порта': 'استلام ذاتي من الميناء',
      'Связаться с поставщиком': 'التواصل مع المورّد', 'Список поставщиков': 'قائمة الموردين',
      'Стать поставщиком': 'كن مورّداً', 'По поставщикам': 'حسب الموردين',
      '🚚 Выберите базис (FOB — самовывоз без доплат)': '🚚 اختر الأساس (FOB — استلام ذاتي بدون رسوم)',
      'Откуда': 'من', 'до двери, all-in': 'حتى الباب، شامل الكل', 'недоступно': 'غير متاح',
      'укажите место прибытия': 'حدّد مكان الوصول', 'нет тарифа на фрахт': 'لا توجد تعرفة شحن',
    },
  };
  Object.keys(_ADDENDUM2).forEach(function (l) {
    DICT[l] = DICT[l] || {};
    Object.assign(DICT[l], _ADDENDUM2[l]);
  });
  Object.keys(_ADDENDUM2.en).forEach(function (k) {
    if (DICT.ru[k] == null) DICT.ru[k] = k;
  });

  // Аддендум 3: подсказки-чипы (suggestions) — фронт-дефолты + частые бэкенд-строки.
  const _ADDENDUM3 = {
    en: {
      'Также можете:': 'You can also:', 'Покажи мои заказы': 'Show my orders',
      'Создать RFQ': 'Create RFQ', 'Создать RFQ на все': 'Create an RFQ for all',
      'Аналитика за месяц': 'Monthly analytics', 'Найти запчасть': 'Find a part',
      'Все мои заказы': 'All my orders', 'Все мои сделки': 'All my deals',
      'Все мои заявки': 'All my requests', 'Связаться с оператором': 'Contact the operator',
      'Связаться с менеджером': 'Contact the manager', 'Сравнить всех поставщиков': 'Compare all suppliers',
      'Сравнить котировки': 'Compare quotes', 'Все RFQ': 'All RFQs', 'Открытые RFQ': 'Open RFQs',
      'Пополнить депозит': 'Top up deposit', 'Мой баланс': 'My balance', 'Найти аналоги': 'Find analogs',
    },
    'zh-hans': {
      'Также можете:': '您还可以：', 'Покажи мои заказы': '显示我的订单',
      'Создать RFQ': '创建询价', 'Создать RFQ на все': '为全部创建询价',
      'Аналитика за месяц': '月度分析', 'Найти запчасть': '查找零件',
      'Все мои заказы': '我的所有订单', 'Все мои сделки': '我的所有交易',
      'Все мои заявки': '我的所有申请', 'Связаться с оператором': '联系操作员',
      'Связаться с менеджером': '联系经理', 'Сравнить всех поставщиков': '比较所有供应商',
      'Сравнить котировки': '比较报价', 'Все RFQ': '所有询价', 'Открытые RFQ': '开启的询价',
      'Пополнить депозит': '充值押金', 'Мой баланс': '我的余额', 'Найти аналоги': '查找替代件',
    },
    es: {
      'Также можете:': 'También puede:', 'Покажи мои заказы': 'Mostrar mis pedidos',
      'Создать RFQ': 'Crear RFQ', 'Создать RFQ на все': 'Crear un RFQ para todos',
      'Аналитика за месяц': 'Analítica mensual', 'Найти запчасть': 'Buscar una pieza',
      'Все мои заказы': 'Todos mis pedidos', 'Все мои сделки': 'Todas mis operaciones',
      'Все мои заявки': 'Todas mis solicitudes', 'Связаться с оператором': 'Contactar con el operador',
      'Связаться с менеджером': 'Contactar con el gerente', 'Сравнить всех поставщиков': 'Comparar todos los proveedores',
      'Сравнить котировки': 'Comparar cotizaciones', 'Все RFQ': 'Todos los RFQ', 'Открытые RFQ': 'RFQ abiertos',
      'Пополнить депозит': 'Recargar depósito', 'Мой баланс': 'Mi saldo', 'Найти аналоги': 'Buscar análogos',
    },
    ar: {
      'Также можете:': 'يمكنك أيضاً:', 'Покажи мои заказы': 'أظهر طلباتي',
      'Создать RFQ': 'إنشاء طلب عرض', 'Создать RFQ на все': 'إنشاء طلب عرض للجميع',
      'Аналитика за месяц': 'تحليلات شهرية', 'Найти запчасть': 'البحث عن قطعة',
      'Все мои заказы': 'كل طلباتي', 'Все мои сделки': 'كل صفقاتي',
      'Все мои заявки': 'كل طلباتي', 'Связаться с оператором': 'التواصل مع المشغّل',
      'Связаться с менеджером': 'التواصل مع المدير', 'Сравнить всех поставщиков': 'قارن كل الموردين',
      'Сравнить котировки': 'قارن العروض', 'Все RFQ': 'كل طلبات العروض', 'Открытые RFQ': 'طلبات العروض المفتوحة',
      'Пополнить депозит': 'إيداع الوديعة', 'Мой баланс': 'رصيدي', 'Найти аналоги': 'البحث عن بدائل',
    },
  };
  Object.keys(_ADDENDUM3).forEach(function (l) {
    DICT[l] = DICT[l] || {};
    Object.assign(DICT[l], _ADDENDUM3[l]);
  });
  Object.keys(_ADDENDUM3.en).forEach(function (k) {
    if (DICT.ru[k] == null) DICT.ru[k] = k;
  });

  // ── Состояние ────────────────────────────────────────────────────────
  function detectLang() {
    const htmlLang = (document.documentElement.getAttribute('lang') || '').toLowerCase();
    if (DICT[htmlLang]) return htmlLang;
    // Нормализация: 'zh-CN' → 'zh-hans', 'ru-RU' → 'ru'
    if (htmlLang.startsWith('zh')) return 'zh-hans';
    const base = htmlLang.split('-')[0];
    if (DICT[base]) return base;
    return 'ru';
  }

  let currentLang = detectLang();

  // ── Публичный API ────────────────────────────────────────────────────
  function t(key, vars) {
    const dict = DICT[currentLang] || DICT.ru;
    let s = dict[key];
    if (s == null) {
      // Фолбэк: попробовать en, потом ru, потом ключ as-is.
      s = (DICT.en && DICT.en[key]) || (DICT.ru && DICT.ru[key]) || key;
    }
    if (vars && typeof vars === 'object') {
      Object.keys(vars).forEach(function (k) {
        s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), String(vars[k]));
      });
    }
    return s;
  }

  function applyI18n(root) {
    const scope = root || document;
    scope.querySelectorAll('[data-i18n]').forEach(function (el) {
      const key = el.getAttribute('data-i18n');
      if (key) el.textContent = t(key);
    });
    scope.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
      el.setAttribute('placeholder', t(el.getAttribute('data-i18n-placeholder')));
    });
    scope.querySelectorAll('[data-i18n-title]').forEach(function (el) {
      el.setAttribute('title', t(el.getAttribute('data-i18n-title')));
    });
    scope.querySelectorAll('[data-i18n-aria-label]').forEach(function (el) {
      el.setAttribute('aria-label', t(el.getAttribute('data-i18n-aria-label')));
    });
  }

  function getCookie(name) {
    const m = document.cookie.match('(^|;)\\s*' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[2]) : '';
  }

  function setLanguage(lang) {
    if (!DICT[lang]) {
      console.warn('[i18n] Unsupported language:', lang);
      return Promise.reject(new Error('unsupported'));
    }
    currentLang = lang;
    document.documentElement.setAttribute('lang', lang);
    document.documentElement.setAttribute('dir', lang === 'ar' ? 'rtl' : 'ltr');
    applyI18n();
    // Сохраняем выбор пользователя на сервере (если залогинен)
    return fetch('/api/set-language/', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCookie('csrftoken'),
      },
      body: JSON.stringify({ language: lang }),
      credentials: 'same-origin',
    }).catch(function (e) {
      console.warn('[i18n] set-language failed (ignored):', e);
    }).finally(function () {
      // Полная перерисовка: chat-first.js рендерит динамику русскими литералами,
      // которые переводятся через esc()/tr() только при новом рендере. Reload —
      // самый надёжный способ применить язык ко всему SPA.
      try { location.reload(); } catch (e) {}
    });
  }

  // ── Экспорт ─────────────────────────────────────────────────────────
  window.t = t;
  window.applyI18n = applyI18n;
  window.setLanguage = setLanguage;
  window.getCurrentLanguage = function () { return currentLang; };
  window.getAvailableLanguages = function () {
    return [
      { code: 'ru',      label: 'Русский',  rtl: false },
      { code: 'en',      label: 'English',  rtl: false },
      { code: 'zh-hans', label: '中文',     rtl: false },
      { code: 'ar',      label: 'العربية', rtl: true  },
    ];
  };
  window.isRTL = function (lang) {
    return (lang || currentLang) === 'ar';
  };

  // ── Регистрация внешнего словаря ─────────────────────────────────────
  // i18n_auto.js (сгенерированные переводы фронт-литералов chat-first.js)
  // вызывает это с объектом { "русская строка": {en, "zh-hans", es, ar}, … }.
  // Не перезатираем уже существующие (курируемые) записи. Добавляем ru-идентичность,
  // чтобы под 'ru' не срабатывал en-фолбэк.
  window.registerI18n = function (addendum) {
    if (!addendum || typeof addendum !== 'object') return 0;
    let n = 0;
    Object.keys(addendum).forEach(function (ru) {
      const tr = addendum[ru];
      if (!tr || typeof tr !== 'object') return;
      Object.keys(tr).forEach(function (lang) {
        if (!DICT[lang]) DICT[lang] = {};
        if (DICT[lang][ru] == null && tr[lang] != null) DICT[lang][ru] = tr[lang];
      });
      if (DICT.ru[ru] == null) DICT.ru[ru] = ru;
      n++;
    });
    return n;
  };

  // ── DOM-перевод динамики ─────────────────────────────────────────────
  // chat-first.js рендерит часть UI русскими литералами прямо в HTML (мимо esc()).
  // localizeNode переводит ТЕКСТОВЫЕ УЗЛЫ и placeholder/title по ТОЧНОМУ совпадению
  // с DICT[currentLang] (ключ = русская строка). Данные, числа и составные строки
  // без точного ключа не трогаются. Под 'ru' — no-op.
  const _SKIP_TAGS = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, INPUT: 1, CODE: 1, PRE: 1, OPTION: 0 };
  function _trVal(s) {
    const k = (s == null ? '' : String(s)).trim();
    if (!k) return null;
    const d = DICT[currentLang];
    if (!d) return null;
    const v = d[k];
    return (v != null && v !== k) ? v : null;
  }
  function _applyTextTr(node) {
    const tr = _trVal(node.nodeValue);
    if (tr == null) { _applyPatternTr(node); return; }
    const m = node.nodeValue.match(/^(\s*)([\s\S]*?)(\s*)$/);
    node.nodeValue = (m ? m[1] : '') + tr + (m ? m[3] : '');
  }
  var _PATTERNS = [];
  window.registerPatterns = function(patterns) { _PATTERNS = _PATTERNS.concat(patterns || []); };
  function _applyPatternTr(node) {
    if (!_PATTERNS.length || currentLang === 'ru') return;
    const text = (node.nodeValue || '').trim();
    if (!text) return;
    for (let i = 0; i < _PATTERNS.length; i++) {
      const p = _PATTERNS[i];
      const m = text.match(p.ru);
      if (m && p[currentLang]) {
        let out = p[currentLang];
        for (let g = 1; g < m.length; g++) out = out.replace(new RegExp('\\$' + g, 'g'), m[g]);
        const ws = node.nodeValue.match(/^(\s*)([\s\S]*?)(\s*)$/);
        node.nodeValue = (ws ? ws[1] : '') + out + (ws ? ws[3] : '');
        return;
      }
    }
  }
  function localizeNode(root) {
    if (currentLang === 'ru' || !root) return;
    try {
      if (root.nodeType === 3) { _applyTextTr(root); return; }
      if (root.nodeType !== 1) return;
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode: function (n) {
          const p = n.parentNode;
          if (!p || _SKIP_TAGS[p.nodeName]) return NodeFilter.FILTER_REJECT;
          if (p.closest && p.closest('[data-no-i18n],[contenteditable="true"]')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      const nodes = [];
      let n;
      while ((n = walker.nextNode())) nodes.push(n);
      nodes.forEach(_applyTextTr);
      root.querySelectorAll && root.querySelectorAll('[placeholder]').forEach(function (el) {
        const tr = _trVal(el.getAttribute('placeholder'));
        if (tr != null) el.setAttribute('placeholder', tr);
      });
      root.querySelectorAll && root.querySelectorAll('[title]').forEach(function (el) {
        const tr = _trVal(el.getAttribute('title'));
        if (tr != null) el.setAttribute('title', tr);
      });
    } catch (e) { /* no-op */ }
  }
  window.localizeNode = localizeNode;

  function _startObserver() {
    if (currentLang === 'ru' || typeof MutationObserver === 'undefined') return;
    try {
      const obs = new MutationObserver(function (muts) {
        for (let i = 0; i < muts.length; i++) {
          const added = muts[i].addedNodes;
          for (let j = 0; added && j < added.length; j++) localizeNode(added[j]);
        }
      });
      obs.observe(document.body, { childList: true, subtree: true });
    } catch (e) { /* no-op */ }
  }

  // ── Авто-применение при загрузке ─────────────────────────────────────
  function _initI18n() {
    applyI18n();
    localizeNode(document.body);
    _startObserver();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _initI18n);
  } else {
    _initI18n();
  }
})();
