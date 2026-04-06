/**
 * Cabinet i18n — full RU↔EN translation via localStorage('cp_lang').
 * Include in all base templates. Translates text nodes on DOMContentLoaded.
 */
(function(){
  'use strict';

  var T = {
    // ── Navigation & Layout ──
    'Дашборд':'Dashboard','Товары и прайсы':'Products & Pricing','Чертежи':'Drawings',
    'Запросы клиентов':'Customer Requests','Заказы':'Orders','Контроль SLA':'SLA Control',
    'Согласование':'Negotiations','Переторжка':'Price Negotiation','QR-контроль':'QR Control',
    'Финансы':'Finance','Рейтинг':'Rating','Аналитика':'Analytics','Команда':'Team',
    'Интеграции':'Integrations','Логистика':'Logistics','Отчёты':'Reports','Каталог':'Catalog',
    'скоро':'soon','Назад':'Back',

    // ── Seller sidebar ──
    'Рейтинг поставщика':'Supplier Rating','Надёжный':'Reliable','Проверенный':'Verified',
    'Новый':'New','Премиум':'Premium','Конверсия':'Conversion','Оценка':'Score',
    'Сегодня':'Today','Запроса':'Requests','Заказов':'Orders','Выручка':'Revenue',
    'ДИРЕКТОР':'DIRECTOR','Кабинет поставщика':'Supplier Cabinet',
    'ПАНЕЛЬ':'PANEL','Панель':'Panel','+ Виджеты':'+ Widgets','Виджеты':'Widgets',

    // ── Buyer sidebar ──
    'Кабинет покупателя':'Buyer Cabinet','Избранное':'Favorites','Запросы RFQ':'RFQ Requests',
    'Отгрузки':'Shipments','Рекламации':'Claims','Поставщики':'Suppliers','Закупки':'Purchases',
    'ПОКУПАТЕЛЬ':'BUYER','Надёжный покупатель':'Reliable Buyer','Кредитный рейтинг':'Credit Rating',
    'Экономия':'Savings','Меню':'Menu','В пути':'In Transit',

    // ── Operator roles & nav ──
    'Логист':'Logistician','Таможенный брокер':'Customs Broker','Платёжный агент':'Payment Agent',
    'Менеджер по продажам':'Sales Manager','Сменить роль':'Switch Role',
    'Ставки на заказы':'Order Quotes','Маршруты':'Routes','Порты и терминалы':'Ports & Terminals',
    'Документы':'Documents','Таможня':'Customs','Декларации':'Declarations','Тарифы':'Tariffs',
    'Запросы':'Requests','Платежи':'Payments','Счета':'Invoices','Сверка':'Reconciliation',
    'Эскроу':'Escrow','Менеджер':'Manager','Клиенты':'Clients','Воронка сделок':'Sales Funnel',

    // ── Admin nav ──
    'Панель администратора':'Admin Panel','Управление':'Management','Пользователи':'Users',
    'Модерация':'Moderation','Настройки':'Settings','Импорты':'Imports','Логи':'Logs',
    'Поддержка':'Support',

    // ── Dashboard Stats ──
    'Активные заказы':'Active Orders','Исполнение SLA':'SLA Compliance',
    'Конверсия запросов':'Request Conversion','Рейтинг поставщика':'Supplier Rating',
    'Общая выручка':'Total Revenue','Выручка по месяцам':'Revenue by Month',
    'Заказы по статусам':'Orders by Status','Входящие запросы':'Incoming Requests',
    'Лента событий':'Event Feed','Профиль и доступы':'Profile & Access',
    'Состояние аккаунта':'Account Health','Быстрые действия':'Quick Actions',
    'за 30 дней':'30 days','норматив: 90%':'target: 90%',
    'запрос \u2192 заказ':'request \u2192 order','из 5.0':'of 5.0','новых':'new',
    'Все запросы \u2192':'All Requests \u2192','Все события \u2192':'All Events \u2192',
    'Загружаем профиль...':'Loading profile...','Загрузка...':'Loading...',
    'на контроле':'under control','в процессе':'in progress',

    // ── Months ──
    'Окт':'Oct','Ноя':'Nov','Дек':'Dec','Янв':'Jan','Фев':'Feb','Мар':'Mar',
    'Апр':'Apr','Май':'May','Июн':'Jun','Июл':'Jul','Авг':'Aug','Сен':'Sep',

    // ── Right Panel Widgets ──
    'Требует действия':'Action Required','Открепить':'Unpin',
    'Ответить на запрос':'Reply to Request','Подтвердить отгрузку':'Confirm Shipment',
    'Ожидает':'Pending','Загрузить сертификат':'Upload Certificate','Требуется':'Required',
    'Обновить 14 цен':'Update 14 Prices','Рекоменд.':'Recommended',
    'Устаревшие цены (>30 дней)':'Outdated Prices (>30 days)',
    'SLA дедлайны':'SLA Deadlines','Проверка качества':'Quality Check',
    'Подтверждение заказа':'Order Confirmation','Подготовка к отгрузке':'Shipment Preparation',
    'Ожидают оплаты':'Awaiting Payment','Итого к получению':'Total Receivable',
    'В обработке':'Processing','Подтверждено':'Confirmed','К выплате':'To Be Paid',
    'Основная':'Main','Резерв 10%':'Reserve 10%','Возврат удержания':'Escrow Return',

    // ── Orders ──
    'Всего заказов':'Total Orders','Фильтры':'Filters','Статус':'Status','Все':'All',
    'Применить':'Apply','Сбросить':'Reset','Заказ #':'Order #','поз.':'items','шт.':'pcs',
    'Заказы не найдены':'No Orders Found','Страница':'Page','из':'of',
    'Мои заказы':'My Orders','Активные':'Active','Выполненные':'Completed',
    'Отменённые':'Cancelled',

    // ── Order Detail ──
    '\u2190 Ко всем заказам':'\u2190 All Orders','Позиции поставщика':'Supplier Items',
    'Открытые claims':'Open Claims','Действие поставщика':'Supplier Action',
    'Сменить статус заказа':'Change Order Status','Количество':'Quantity',
    'Цена за единицу':'Unit Price','Тип':'Type','События заказа':'Order Events',
    'Источник':'Source','Редактировать':'Edit','Документы':'Documents',

    // ── Requests / RFQ ──
    'Статус RFQ':'RFQ Status','Применить фильтры':'Apply Filters',
    'Ваших позиций':'Your Items','Общее количество':'Total Quantity',
    'запрос скидки':'discount request','Открыть в кабинете':'Open in Cabinet',
    '\u2190 Ко всем запросам':'\u2190 All Requests','Открыть общий RFQ':'Open Full RFQ',
    'Режим':'Mode','Срочность':'Urgency','Комментарий клиента':'Customer Comment',
    'Комментарий':'Comment','Причина':'Reason','Цена':'Price',
    'Создать запрос':'Create Request','Запросов RFQ':'RFQ Requests',

    // ── Products / Catalog ──
    'Всего позиций':'Total Items','в каталоге':'in catalog','Активных':'Active',
    'Требуют доработки':'Need Improvement','Заполненность':'Completeness',
    'Новые RFQ':'New RFQs','входящие запросы':'incoming requests',
    'Заказы по позициям':'Orders by Items','активных':'active',
    'Обновлено за 24ч':'Updated in 24h','позиций':'items',
    'Конверсия RFQ':'RFQ Conversion','Загрузка прайса':'Price Upload',
    'Файл':'File','Проверка':'Verification','Импорт':'Import',
    'Всего товаров':'Total Products',

    // ── Product Detail ──
    '\u2190 Ко всем товарам':'\u2190 All Products','Остаток':'Stock',
    'Полнота карточки':'Card Completeness','Свежесть данных':'Data Freshness',
    'Основная информация':'Basic Information','Описание':'Description',
    'Кросс-номера':'Cross Numbers','Логистика и производство':'Logistics & Manufacturing',
    'Вес':'Weight','кг':'kg','Габариты':'Dimensions','см':'cm','дн.':'days',
    'Качество карточки':'Card Quality','Открыть':'Open',

    // ── SLA ──
    'Таймлайн доставки':'Delivery Timeline','Отгрузка':'Shipment',
    'Под угрозой':'At Risk','требуют внимания':'require attention',
    'Нарушения SLA':'SLA Violations','просрочены':'overdue',
    'Распределение по этапам':'Distribution by Stage',
    'Нормативы SLA по этапам':'SLA Standards by Stage',
    'Время на каждом этапе':'Time at Each Stage',
    'Триггер':'Trigger','Действие':'Action','Исполнитель':'Responsible',
    'Отмена':'Cancel','Подтвердить':'Confirm','Нет данных':'No Data',

    // ── Finance ──
    'Финансовый контроль':'Financial Control','Общий оборот':'Total Turnover',
    'все заказы':'all orders','Получено':'Received','полная оплата':'full payment',
    'Ожидает оплаты':'Awaiting Payment','Резервов внесено':'Reserves Collected',
    'предоплата':'prepayment','Заказ':'Order','Клиент':'Customer','Сумма':'Amount',
    'Резерв':'Reserve','Оплата':'Payment','Схема':'Scheme','Этап':'Stage',
    'Таймлайн оплаты':'Payment Timeline','Счёт выставлен':'Invoice Issued',
    'Резерв оплачен':'Reserve Paid','Финальная оплата 90%':'Final Payment 90%',
    'Полностью оплачен':'Fully Paid','Статус заказа и SLA':'Order Status & SLA',
    'Скачать PDF':'Download PDF','Детали':'Details','Оплачено':'Paid',
    'К оплате':'To Pay','Задолженность':'Debt',

    // ── Negotiations ──
    'Чертежи и тех. параметры':'Drawings & Technical Specs',
    'Скидки и лояльность':'Discounts & Loyalty','Скидка':'Discount',
    'Всем товарам':'All Products','Активен':'Active','Не активен':'Inactive',
    'Сохранить настройки':'Save Settings','Программа лояльности':'Loyalty Program',
    'Постоянный покупатель':'Regular Buyer','Базовый':'Basic',
    'VIP покупатель':'VIP Buyer','Сохранить все':'Save All',
    'Активные позиции':'Active Items','Стартовая цена':'Starting Price',
    'Торги открыты':'Bidding Open','Согласовано':'Agreed',
    'Покупатель':'Buyer','Поставщик':'Supplier','Запрос скидки':'Discount Request',
    'Кол-во':'Qty','Дата':'Date','Ожидает ответа':'Awaiting Response',
    'Принять':'Accept','Встречная':'Counter','Принято':'Accepted',
    'Отправить':'Send','Загрузить файл':'Upload File','Передан':'Delivered',

    // ── Drawings ──
    'Чертежи и CAD-файлы':'Drawings & CAD Files','Загрузить чертёж':'Upload Drawing',
    'Всего':'Total','чертежей':'drawings','Черновики':'Drafts',
    'На проверке':'Under Review','Утверждены':'Approved','Отклонены':'Rejected',
    'Черновик':'Draft','Утверждён':'Approved','Отклонён':'Rejected','Архив':'Archive',
    'Все форматы':'All Formats','Все статусы':'All Statuses','Название':'Name',
    'Формат':'Format','Ревизия':'Revision','Размер':'Size','Обновлён':'Updated',
    'Скачать':'Download','Нет чертежей':'No Drawings','Файлы':'Files',

    // ── QR ──
    'QR-контроль поставок':'QR Supply Control','Всего сканирований':'Total Scans',
    'за всё время':'all time','сканирований':'scans','Заказов с QR':'Orders with QR',
    'зарегистрировано':'registered','Сгенерировать QR-код':'Generate QR Code',
    'Печать':'Print','Скачать PNG':'Download PNG','Сканировать QR-код':'Scan QR Code',
    'Камера':'Camera','QR-код':'QR Code','История сканирований':'Scan History',

    // ── Rating ──
    'из 100':'out of 100','Внешний рейтинг':'External Rating','вес 60%':'weight 60%',
    'Поведенческий рейтинг':'Behavioral Rating','вес 40%':'weight 40%',
    'SLA соблюдение':'SLA Compliance','Конверсия RFQ\u2192Заказ':'RFQ\u2192Order Conversion',
    'Открытые рекламации':'Open Claims','всего':'total',
    'Отмены за 30 дней':'Cancellations in 30 Days','нарушений SLA':'SLA violations',
    'Предупреждения и рекомендации':'Warnings & Recommendations',
    'Как улучшить рейтинг':'How to Improve Rating','Событие':'Event','Влияние':'Impact',
    'Внешний':'External','Поведенческий':'Behavioral','за 30д':'in 30d',

    // ── Team ──
    'Права доступа':'Access Rights','Чат':'Chat','Задачи':'Tasks',
    'Активность':'Activity','Рейтинги':'Ratings','Директор':'Director',
    'Руководитель продаж':'Sales Director','Инженер':'Engineer','Склад':'Warehouse',
    'Документооборот':'Document Management','Новая задача':'New Task',

    // ── Reports ──
    'Новый отчёт':'New Report','Отчётов за месяц':'Reports This Month',
    'Запланировано':'Scheduled','Экспортировано':'Exported','Последний отчёт':'Last Report',
    'Сводные':'Summary','Продажи':'Sales','Финансовые':'Financial',
    'Операционные':'Operational','Расписание':'Schedule','История':'History',
    'Избранные':'Favorites','Недавние':'Recent','Ключевые отчёты':'Key Reports',
    'Готов':'Ready','Предпросмотр':'Preview','Создать':'Create',

    // ── Integrations ──
    'Доступна':'Available','Подключить':'Connect','Индивидуально':'Custom',
    'Оставить запрос':'Submit Request','Связаться':'Contact',

    // ── Logistics ──
    'Карта и терминалы':'Map & Terminals','Отслеживание':'Tracking',
    'Способы доставки':'Shipping Methods','Калькулятор':'Calculator',
    'Страна':'Country','Все страны':'All Countries','Россия':'Russia',
    'Китай':'China','Тип доставки':'Delivery Type','Все типы':'All Types',
    'Морские порты':'Sea Ports','Ручной расчёт менеджером':'Manual Calculation by Manager',
    'Запрос отправлен':'Request Sent',

    // ── Statuses ──
    'Ожидание оплаты':'Awaiting Payment','Формирование заказа':'Order Formation',
    'В производстве':'In Production','Готов к отгрузке':'Ready to Ship',
    'Транзит (Зарубеж)':'Transit (Abroad)','Транзит (РФ)':'Transit (RF)',
    'Выдача':'Issuing','Отгружен':'Shipped','Доставлен':'Delivered',
    'Завершён':'Completed','Отменён':'Cancelled','Транзит Зарубеж':'Transit Abroad',
    'Транзит РФ':'Transit RF',

    // ── Payment statuses ──
    'Ожидает резерва':'Awaiting Reserve','Резерв оплачен':'Reserve Paid',
    'Подтверждение оплачено':'Confirmation Paid','Таможня оплачена':'Customs Paid',
    'Оплачен':'Paid','Возврат в обработке':'Refund Processing','Возвращён':'Refunded',

    // ── SLA statuses ──
    'В норме':'On Track','Нарушен':'Breached',

    // ── Operator role selection ──
    'Выберите рабочую роль':'Select Working Role','Отгрузок':'Shipments',
    'На таможне':'At Customs','В работе':'In Progress','Оформлено':'Processed',
    'За месяц':'This Month','Просрочен':'Overdue','Внимание':'Attention',

    // ── Operator pages ──
    'Очередь платежей':'Payment Queue','Подтверждённых':'Confirmed',
    'Ожидают подтверждения':'Awaiting Confirmation','Отклонённых':'Declined',
    'Удержано':'Held','Дата создания':'Date Created','Действия':'Actions',
    'Удержание':'Hold','Спор':'Dispute','Выпустить':'Release',
    'Расхождения':'Discrepancies','Совпадения':'Matches','Обработано':'Processed',
    'К проверке':'To Review','Банковский перевод':'Bank Transfer',
    'Аккредитив':'Letter of Credit','Онлайн-оплата':'Online Payment',
    'Взаимозачёт':'Offset','Типы платежей':'Payment Types',
    'Активных деклараций':'Active Declarations','На оформлении':'Being Processed',
    'Ожидают документов':'Awaiting Documents','Просроченных':'Overdue',
    'Код ТН ВЭД':'HS Code','Пошлина':'Duty','НДС':'VAT','Акциз':'Excise',
    'Страна происхождения':'Country of Origin','Таможенная стоимость':'Customs Value',
    'Инвойс':'Invoice','Упаковочный лист':'Packing List',
    'Сертификат происхождения':'Certificate of Origin',
    'Активные отгрузки':'Active Shipments','В транзите':'In Transit',
    'Доставлено сегодня':'Delivered Today','Задержки':'Delays',
    'Морская':'Sea Freight','Авиа':'Air Freight','Авто':'Ground',
    'Ж/Д':'Railway','Мультимодальная':'Multimodal',
    'Точка отправления':'Point of Origin','Точка назначения':'Destination',
    'Дата отгрузки':'Shipment Date','Ожидаемая дата':'Expected Date',
    'Контейнер':'Container','Вес брутто':'Gross Weight','Объём':'Volume',
    'Активных заказов':'Active Orders','Новых клиентов':'New Clients',
    'Выручка за месяц':'Monthly Revenue','Средний чек':'Average Order',
    'Предложение отправлено':'Proposal Sent','Переговоры':'Negotiations',
    'Заказ оформлен':'Order Placed','Оплата получена':'Payment Received',
    'Горячие клиенты':'Hot Clients','Риск оттока':'Churn Risk',
    'Свернуть':'Collapse',

    // ── Admin ──
    'Сегодня':'Today','Активных пользователей':'Active Users','Новых заказов':'New Orders',
    'Оборот за сутки':'Daily Turnover','Открытых тикетов':'Open Tickets',
    'Новых RFQ':'New RFQs','На модерации':'Under Moderation',
    'Последние действия':'Recent Actions','Роль':'Role',
    'Дата регистрации':'Registration Date','Последний вход':'Last Login',
    'Заблокирован':'Blocked','Разблокировать':'Unblock','Заблокировать':'Block',
    'Настройки сохранены':'Settings Saved','Профессиональный':'Professional',
    'Корпоративный':'Corporate','Тарифные планы':'Tariff Plans',
    'Комиссии по категориям':'Commissions by Category','Скидки':'Discounts',
    'Популярный':'Popular','Модерация контента':'Content Moderation',
    'Ожидают проверки':'Pending Review','Одобрено сегодня':'Approved Today',
    'Отклонено сегодня':'Declined Today','Товары':'Products','Отзывы':'Reviews',
    'Компании':'Companies','Одобрить':'Approve','Отклонить':'Decline',
    'Заметка администратора':'Admin Note','Оборот за период':'Turnover for Period',
    'Комиссия платформы':'Platform Commission','Ожидает вывода':'Pending Withdrawal',
    'Транзакции':'Transactions','На модерации':'Under Moderation',
    'Категории':'Categories','Бренды':'Brands','Все заказы':'All Orders',
    'Настройки платформы':'Platform Settings','Общие':'General',
    'Безопасность':'Security','Уведомления':'Notifications',
    'Сохранить изменения':'Save Changes','Тикеты поддержки':'Support Tickets',
    'Открытые':'Open','В работе':'In Progress','Закрытые':'Closed',
    'Приоритет':'Priority','Высокий':'High','Средний':'Medium','Низкий':'Low',
    'Тема':'Subject','Отправитель':'Sender','Ответить':'Reply',
    'Закрыть тикет':'Close Ticket','Пользователь':'User','Время':'Time',
    'Системные логи':'System Logs','Всего импортов':'Total Imports',
    'С ошибками':'With Errors','Строк':'Rows','Создано':'Created',
    'Обновлено':'Updated','Экспорт':'Export','Рассылка':'Distribution',

    // ── Import result ──
    'Результат импорта':'Import Result','Обработано':'Processed',
    'Всего строк':'Total Rows','Успешно':'Successful','Ошибок':'Errors',
    'Создано товаров':'Products Created','Обновлено офферов':'Offers Updated',
    'Обновлено цен':'Prices Updated','Откат импорта':'Import Rollback',
    'Откатить импорт':'Roll Back Import',

    // ── Misc ──
    'Пока ничего нет':'Nothing Here Yet','Применить фильтры':'Apply Filters',
    'Поиск':'Search','Период:':'Period:','7 дней':'7 Days','30 дней':'30 Days',
    '90 дней':'90 Days','6 месяцев':'6 Months','1 год':'1 Year','Всё время':'All Time',
    'Стиль:':'Style:','Официальный':'Official','Смотреть':'View',
    'Сравнить':'Compare','Открыть рекламацию':'Open Claim',
    'Отслеживать':'Track','Открыть отчёт':'Open Report',
    'Поставщиков':'Suppliers','Покупатели':'Buyers','Операторы':'Operators',
    'Администраторы':'Administrators','Завершённых':'Completed',
    'Ожидание':'Pending','Выгрузить прайс':'Export Price List',
    'Загрузить прайс':'Upload Price List','Активных':'Active',
    'Отправка\u2026':'Sending\u2026','Сохраняю\u2026':'Saving\u2026',
    'Ожидают поставки':'Awaiting Delivery','Общий бюджет':'Total Budget',
    'Активные переторжки':'Active Negotiations','Согласованных сделок':'Agreed Deals',
    'Средняя скидка':'Average Discount','Экономия за месяц':'Monthly Savings',
    'Базовая цена':'Base Price','Контрпредложение':'Counter Offer',
    'Ответ поставщика':'Supplier Response','Товар':'Product',
    'Мои поставщики':'My Suppliers','Отслеживание поставок':'Shipment Tracking',
    'Аналитика закупок':'Procurement Analytics',
    'Финансы покупателя':'Buyer Finance','Финансовая панель':'Finance Panel',
    'Каталог платформы':'Platform Catalog',
    'Импорты прайс-листов':'Price List Imports',
    'Запросы на закупку':'Purchase Requests','Всего RFQ':'Total RFQs',
    'Тарифы и комиссии':'Tariffs & Commissions',
    'Все пользователи':'All Users',
    'Ежедневный':'Daily','Еженедельно':'Weekly','Ежемесячно':'Monthly',

    // ── Period buttons ──
    '1М':'1M','3М':'3M','6М':'6M','1Г':'1Y','Всё':'All',

    // ── Dashboard blocks ──
    'График выручки':'Revenue Chart','Ключевые метрики':'Key Metrics',
    'Выручка и SLA':'Revenue & SLA','Рейтинг и заказы':'Rating & Orders',
    'Запросы и события':'Requests & Events','скрыто':'hidden',
    'Производство':'Production','К отгрузке':'Ready to Ship',
    'Отгружено':'Shipped','Ожидает резерв':'Awaiting Reserve',
    'Срочный':'Urgent','Стандартный':'Standard','По чертежу':'By Drawing',
    'Деталь':'Part','4 новых':'4 new',
    'Запросы клиентов на расценку':'Customer pricing requests',
    'Все \u2192':'All \u2192','из 5':'of 5',
    'Внешняя 60%':'External 60%','Поведение 40%':'Behavioral 40%',

    // ── Widget titles ──
    'Очередь задач':'Task Queue','Обратный отсчёт':'Countdown',
    'Входящие платежи':'Incoming Payments','Согласование и запросы':'Approvals & Requests',
    'Открытые претензии':'Open Claims','Последние сканирования':'Recent Scans',
    'Устаревшие цены':'Outdated Prices','Требуют обновления':'Need Updating',
    'Закрепить виджеты':'Pin Widgets','Ждёт согласования':'Awaiting Approval',
    'Требует корректировки':'Needs Correction','Используется':'In Use',
    'Просрочка +2 дня':'Overdue +2 days','Несоответствие чертежу':'Drawing Discrepancy',
    'На рассмотрении':'Under Review','Ждёт решения':'Awaiting Resolution',
    'Нет открытых рекламаций':'No Open Claims',
    'Приёмка завершена':'Acceptance Completed','Старше 30 дней':'Older than 30 days',
    'Отчёт проверки каче...':'Quality inspection re...',
    'Отчёт проверки качества':'Quality Inspection Report',

    // ── Page subtitles (seller) ──
    'Главная рабочая панель: что требует внимания сейчас и куда перейти дальше.':'Main dashboard: what needs attention now and where to go next.',
    'Загрузка прайсов, preview, история импортов, каталог и массовые действия в одном модуле.':'Price uploads, preview, import history, catalog and bulk actions in one module.',
    'Список заказов по вашим товарам, фильтры, статусы и переход в карточку заказа.':'Orders for your products, filters, statuses and order card navigation.',
    'Карточка товара поставщика: данные, логистика, полнота и быстрые действия.':'Supplier product card: data, logistics, completeness and quick actions.',
    'Подробная разбивка \u00b7 метрики \u00b7 рекомендации':'Detailed breakdown \u00b7 metrics \u00b7 recommendations',
    'Оплаты \u00b7 документы \u00b7 счета \u00b7 таймлайн поступлений':'Payments \u00b7 documents \u00b7 invoices \u00b7 receipt timeline',
    'Загружай чертежи \u00b7 связывай с деталями \u00b7 отслеживай ревизии':'Upload drawings \u00b7 link to parts \u00b7 track revisions',
    'Генерация кодов \u00b7 сканирование \u00b7 отслеживание этапов':'Code generation \u00b7 scanning \u00b7 stage tracking',
    'Запрос скидок и переговоры по ценам с поставщиками':'Discount requests and price negotiations with suppliers',

    // ── Misc missing ──
    'Вперёд':'Forward','позиции':'items','получен':'received','подтверждён':'confirmed',
    'Резерв оплачен':'Reserve Paid','Насос по чертежу':'Pump by drawing',
    'Заказы, SLA, платежи':'Orders, SLA, payments',
    'Общая выручка \u00b7 vs':'Total revenue \u00b7 vs',
    'фев':'Feb','мар':'Mar','апр':'Apr','май':'May','июн':'Jun',
    'июл':'Jul','авг':'Aug','сен':'Sep','окт':'Oct','ноя':'Nov','дек':'Dec','янв':'Jan',
    'Активных заказов':'Active Orders','Поставщиков':'Suppliers'
  };

  // Build reverse map EN→RU
  var R = {};
  for (var k in T) R[T[k]] = k;

  function applyLang(lang) {
    var isEn = lang === 'en';
    var map = isEn ? T : R;

    // Update html lang attribute
    document.documentElement.lang = isEn ? 'en' : 'ru';

    // Regex patterns for time units, partial strings etc.
    var rxPatterns = isEn ? [
      [/(\d+)ч\s*(\d+)м/g, '$1h $2m'],   // 3ч 47м → 3h 47m
      [/(\d+)ч/g, '$1h'],                  // 4ч → 4h
      [/(\d+)м\b/g, '$1m'],                // 12м → 12m
      [/(\d+)\s*дн\./g, '$1 days'],        // 5 дн. → 5 days
      [/(\d+)\s*позиции/g, '$1 items'],    // 4 позиции → 4 items
      [/(\d+)\s*позиций/g, '$1 items'],
    ] : [
      [/(\d+)h\s*(\d+)m/g, '$1ч $2м'],
      [/(\d+)h\b/g, '$1ч'],
      [/(\d+)m\b/g, '$1м'],
      [/(\d+)\s*days/g, '$1 дн.'],
      [/(\d+)\s*items/g, '$1 позиций'],
    ];

    // Walk all text nodes
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    var node;
    while (node = walker.nextNode()) {
      var raw = node.textContent;
      var txt = raw.trim();
      if (!txt || txt.length < 1) continue;
      // Exact match
      if (map[txt]) {
        node.textContent = raw.replace(txt, map[txt]);
        continue;
      }
      // Regex patterns
      var changed = raw;
      for (var i = 0; i < rxPatterns.length; i++) {
        changed = changed.replace(rxPatterns[i][0], rxPatterns[i][1]);
      }
      if (changed !== raw) node.textContent = changed;
    }

    // Also translate placeholders and titles
    document.querySelectorAll('[placeholder]').forEach(function(el){
      var p = el.getAttribute('placeholder').trim();
      if (map[p]) el.setAttribute('placeholder', map[p]);
    });
    document.querySelectorAll('[title]').forEach(function(el){
      var t = el.getAttribute('title').trim();
      if (map[t]) el.setAttribute('title', map[t]);
    });

    // Update lang button
    var btn = document.getElementById('cab-lang-btn');
    if (btn) btn.textContent = isEn ? 'RU' : 'EN';
  }

  window.cabinetToggleLang = function(){
    var cur = localStorage.getItem('cp_lang') || 'ru';
    var next = cur === 'ru' ? 'en' : 'ru';
    localStorage.setItem('cp_lang', next);
    applyLang(next);
  };

  window.cabinetApplyLang = applyLang;

  document.addEventListener('DOMContentLoaded', function(){
    var lang = localStorage.getItem('cp_lang') || 'ru';
    if (lang === 'en') applyLang('en');
  });
})();
