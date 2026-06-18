/* Chat-First UI — gradient minimalist (07r/07s design).
 *
 * State machine:
 *   - WELCOME: hero + title + input + pills (sidebar collapsed by default for new users)
 *   - CONV: chat thread + sticky bottom input
 *
 * Sidebar logic:
 *   - First visit (no chat history): collapsed
 *   - Returning user (>0 chats): open by default on desktop
 *   - Mobile (<768px): always overlay (slide over content, never push)
 *   - State persisted in localStorage 'cf_sidebar_open'
 */
(function(){
  'use strict';

  const SB_KEY = 'cf_sidebar_open';
  const CONV_KEY = 'cf_active_conv';
  // Вход через поиск с главной (/chat/?q=...). Захватываем ДО очистки URL
  // (history.replaceState) — нужно для дефолта сайдбара (см. applyDefaultSidebar).
  const _ENTRY_SEARCH = (() => {
    try { return new URLSearchParams(window.location.search).has('q'); }
    catch (e) { return false; }
  })();

  // Подсказки для smart-вопросов pricelist (warehouse_address/sea_port/
  // air_port). Browser-side datalist фильтрует по введённым символам —
  // юзер вписывает "Sha" и видит Shanghai/Sharjah/Шанхай. Список плоский
  // потому что PORTS_BY_COUNTRY определён глубже внутри pricelist-handler
  // и недоступен из renderers; данные подобраны вручную как срез самых
  // частых направлений.
  const SUGGEST_SEA = [
    'Shanghai (CNSHA)', 'Ningbo (CNNGB)', 'Shenzhen Yantian (CNYTN)',
    'Shenzhen Shekou (CNSKU)', 'Qingdao (CNTAO)', 'Tianjin (CNTXG)',
    'Dalian (CNDLC)', 'Xiamen (CNXMN)', 'Hong Kong (HKHKG)',
    'Vladivostok ВМТП (RUVVO)', 'Vostochny (RUVYP)', 'Nakhodka (RUNJK)',
    'Novorossiysk (RUNVS)', 'Saint Petersburg (RULED)', 'Ust-Luga (RUULU)',
    'Hamburg (DEHAM)', 'Rotterdam (NLRTM)', 'Antwerp (BEANR)',
    'Jebel Ali (AEJEA)', 'Khalifa (AEKLF)', 'Mundra (INMUN)', 'Nhava Sheva (INNSA)',
    'Busan (KRPUS)', 'Yokohama (JPYOK)', 'Singapore (SGSIN)',
    'Long Beach (USLGB)', 'Los Angeles (USLAX)',
  ];
  const SUGGEST_AIR = [
    'Shanghai Pudong (PVG)', 'Beijing Capital (PEK)', 'Beijing Daxing (PKX)',
    'Guangzhou (CAN)', 'Shenzhen (SZX)', 'Hong Kong (HKG)', 'Chengdu (CTU)',
    'Hangzhou (HGH)', 'Sheremetyevo (SVO)', 'Domodedovo (DME)', 'Vnukovo (VKO)',
    'Vladivostok (VVO)', 'Novosibirsk (OVB)', 'Frankfurt (FRA)',
    'Amsterdam (AMS)', 'Paris CDG (CDG)', 'London Heathrow (LHR)',
    'Dubai (DXB)', 'Abu Dhabi (AUH)', 'Doha (DOH)', 'Istanbul (IST)',
    'Mumbai (BOM)', 'Delhi (DEL)', 'Seoul Incheon (ICN)', 'Tokyo Narita (NRT)',
  ];
  const SUGGEST_CITY = [
    'Shanghai, China', 'Shenzhen, China', 'Guangzhou, China', 'Ningbo, China',
    'Qingdao, China', 'Tianjin, China', 'Yiwu, China', 'Hong Kong',
    'Vladivostok, Russia', 'Saint Petersburg, Russia', 'Moscow, Russia',
    'Novorossiysk, Russia', 'Novosibirsk, Russia',
    'Dubai, UAE', 'Abu Dhabi, UAE', 'Sharjah, UAE',
    'Mumbai, India', 'Delhi, India', 'Chennai, India',
    'Busan, South Korea', 'Seoul, South Korea',
    'Tokyo, Japan', 'Yokohama, Japan',
    'Singapore', 'Bangkok, Thailand',
    'Hamburg, Germany', 'Frankfurt, Germany', 'Rotterdam, Netherlands',
    'Antwerp, Belgium', 'Istanbul, Turkey',
  ];
  const SUGGEST_BY_SOURCE = { sea: SUGGEST_SEA, air: SUGGEST_AIR, city: SUGGEST_CITY };
  // Координаты портов (по городу порта) — для сортировки по РЕАЛЬНОМУ гео-
  // расстоянию от склада. Ключ — код порта (UN/LOCODE морпорта или IATA
  // аэропорта). Юзер указал склад в Шэньяне → ближайший морпорт Далянь
  // (а не Шанхай) определяется по haversine между складом и портами.
  const PORT_COORDS = {
    AEJEA:[25.20,55.27],AEPRA:[25.20,55.27],DXB:[25.25,55.36],DWC:[24.90,55.16],
    AEKHL:[25.35,55.39],AESHJ:[25.47,55.51],SHJ:[25.33,55.52],
    AEKLF:[24.81,54.65],AEMZD:[24.52,54.38],AUH:[24.43,54.65],
    AEFJR:[25.12,56.33],AEAJM:[25.41,55.44],RKT:[25.61,55.94],
    CNSHA:[30.63,122.07],CNWAI:[31.34,121.60],PVG:[31.14,121.81],
    CNNGB:[29.87,121.55],CNYTN:[22.56,114.27],CNSKU:[22.48,113.91],
    CNCWN:[22.48,113.88],CNNSA:[22.78,113.61],CNHUA:[23.10,113.43],
    CAN:[23.39,113.30],CNTAO:[36.07,120.32],CNTXG:[38.98,117.70],
    CNDLC:[38.95,121.88],CNXMN:[24.45,118.07],HKHKG:[22.32,114.13],
    HKG:[22.31,113.91],CNLYG:[34.74,119.45],CNFUZ:[26.07,119.30],
    PEK:[40.08,116.58],PKX:[39.51,116.41],HGH:[30.23,120.43],
    CTU:[30.58,103.95],KMG:[25.10,102.93],XIY:[34.44,108.75],
    RUVVO:[43.11,131.89],RUVRP:[43.11,131.91],RUPRV:[43.10,131.93],
    RUVCT:[43.11,131.89],VVO:[43.40,132.15],RUVYP:[42.74,133.08],
    RUNJK:[42.82,132.87],RUZRB:[42.64,131.10],RUPSE:[42.65,130.80],
    RUSLA:[42.86,131.36],RUBKM:[43.11,132.35],RUVNN:[49.09,140.29],
    RUSOV:[48.97,140.29],RUNVS:[44.72,37.77],RUSHX:[44.69,37.80],
    RUNUT:[44.72,37.79],RUTMN:[45.20,36.70],RUTUA:[44.10,39.08],
    RUTMK:[45.27,37.38],RUKVK:[45.33,36.68],RUROV:[47.24,39.70],
    RULED:[59.90,30.24],RUFCT:[59.89,30.22],RUPLP:[59.87,30.21],
    RUBRO:[59.84,29.78],LED:[59.80,30.26],RUULU:[59.67,28.27],
    RUUL2:[59.67,28.27],RUKGD:[54.70,20.50],RUBLT:[54.65,19.90],
    RUMMK:[68.97,33.08],RUARH:[64.54,40.52],RUKDA:[67.15,32.41],
    SVO:[55.97,37.41],DME:[55.41,37.90],VKO:[55.60,37.27],
    KJA:[56.17,92.49],OVB:[55.01,82.65],KZN:[55.61,49.28],
    KHV:[48.53,135.19],EKB:[56.74,60.80],AER:[43.45,39.96],
    NLRTM:[51.95,4.05],NLRTA:[51.95,4.05],RTM:[51.96,4.44],
    NLAMS:[52.40,4.80],AMS:[52.31,4.76],NLVLS:[51.44,3.57],EIN:[51.45,5.37],
    TRAMB:[40.96,28.68],TRHAY:[40.99,29.02],IST:[41.26,28.74],
    TRMER:[36.80,34.63],TRIZM:[38.80,26.97],ADB:[38.29,27.16],
    TRGEB:[40.79,29.43],TRDER:[40.76,29.83],TRSAN:[36.58,36.17],
    SAW:[40.90,29.31],ESB:[40.13,32.99],AYT:[36.90,30.79],
    KZAKT:[43.65,51.16],KZKUR:[43.20,51.65],ALA:[43.35,77.04],
    NQZ:[51.02,71.47],CIT:[42.36,69.78],KGF:[49.67,73.33],AKX:[50.25,57.21],
  };
  // Гаверсинус — расстояние между точками (км). Для сортировки портов
  // по близости к складу.
  function _haversineKm(lat1, lon1, lat2, lon2) {
    var R = 6371, toR = Math.PI / 180;
    var dLat = (lat2 - lat1) * toR, dLon = (lon2 - lon1) * toR;
    var a = Math.sin(dLat/2)*Math.sin(dLat/2)
      + Math.cos(lat1*toR)*Math.cos(lat2*toR)*Math.sin(dLon/2)*Math.sin(dLon/2);
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
  }
  // Координаты выбранного склада (lat,lng) — выставляется при выборе города
  // в city-автокомплите, используется для гео-сортировки портов.
  var _warehouseCoords = null;
  // Карта название_города(lower) → [lat,lng], наполняется из geo-cities.
  var _cityCoordsMap = {};
  // По текущему значению city-инпута выставляет _warehouseCoords (если город
  // известен) — чтобы порты пересортировались по близости к складу.
  function _maybeSetWarehouseCoords(val) {
    var key = String(val || '').split(' / ')[0].trim().toLowerCase();
    if (key && _cityCoordsMap[key]) {
      _warehouseCoords = _cityCoordsMap[key];
    }
  }
  // Country-specific placeholders для smart-вопросов про порты. Когда юзер
  // выбрал страну в адресе склада — последующие port-вопросы получают
  // подсказку с примерами из этой страны (а не общемировой mix).
  const PORTS_PLACEHOLDER_BY_COUNTRY = {
    CN: { sea: 'Shanghai, Ningbo, Yantian… — начните набирать',
          air: 'PVG, PEK, CAN, HKG… — начните набирать' },
    RU: { sea: 'ВМТП, Восточный, Усть-Луга… — начните набирать',
          air: 'SVO, DME, LED, VVO… — начните набирать' },
    AE: { sea: 'Jebel Ali, Khalifa Port… — начните набирать',
          air: 'DXB, AUH, SHJ… — начните набирать' },
    TR: { sea: 'Стамбул, Мерсин, Измир… — начните набирать',
          air: 'IST, SAW, ADB… — начните набирать' },
    DE: { sea: 'Hamburg, Bremerhaven… — начните набирать',
          air: 'FRA, MUC, DUS… — начните набирать' },
    KR: { sea: 'Busan, Incheon… — начните набирать',
          air: 'ICN, GMP, PUS… — начните набирать' },
    JP: { sea: 'Yokohama, Tokyo, Kobe… — начните набирать',
          air: 'NRT, HND, KIX… — начните набирать' },
    IN: { sea: 'Mundra, Nhava Sheva, Chennai… — начните набирать',
          air: 'BOM, DEL, BLR… — начните набирать' },
    US: { sea: 'LA, Long Beach, NY/NJ… — начните набирать',
          air: 'LAX, JFK, ORD… — начните набирать' },
    IT: { sea: 'Genova, Trieste, La Spezia… — начните набирать',
          air: 'MXP, FCO, BGY… — начните набирать' },
  };
  // Подсказки по странам — после выбора в country_picker фильтруем датасеты
  // sea/air/city по стране, чтобы юзер не утопал в нерелевантных подсказках.
  // Ключ — ISO2 страны. Если страны нет в этой карте — fallback на общий
  // SUGGEST_BY_SOURCE (мировой список из ~25 крупных хабов).
  // Списки городов и портов — каждая запись содержит ОБА языка через " / ",
  // чтобы datalist combobox матчил при наборе и на латинице, и на кириллице.
  // Пример: "Shanghai / Шанхай" — браузер показывает обе строки при наборе
  // "Sha" и "Шан". Для портов формат: "Имя (КОД) / Русское_имя".
  const SUGGEST_BY_COUNTRY = {
    CN: {
      sea: ['Shanghai / Шанхай (CNSHA)', 'Shanghai Waigaoqiao / Шанхай Вайгаоцяо',
        'Ningbo / Нинбо (CNNGB)', 'Shenzhen Yantian / Шэньчжэнь Янтянь (CNYTN)',
        'Shenzhen Shekou / Шэньчжэнь Шэкоу (CNSKU)', 'Shenzhen Chiwan / Шэньчжэнь Чиван',
        'Qingdao / Циндао (CNTAO)', 'Tianjin Xingang / Тяньцзинь Синьган (CNTXG)',
        'Dalian / Далянь (CNDLC)', 'Xiamen / Сямэнь (CNXMN)',
        'Hong Kong / Гонконг (HKHKG)', 'Guangzhou Nansha / Гуанчжоу Наньша (CNNSA)'],
      air: ['Shanghai Pudong / Шанхай Пудун (PVG)', 'Beijing Capital / Пекин Шоуду (PEK)',
        'Beijing Daxing / Пекин Дасин (PKX)', 'Guangzhou / Гуанчжоу (CAN)',
        'Shenzhen / Шэньчжэнь (SZX)', 'Hong Kong / Гонконг (HKG)',
        'Hangzhou / Ханчжоу (HGH)', 'Chengdu / Чэнду (CTU)',
        'Kunming / Куньмин (KMG)', 'Xi’an / Сиань (XIY)'],
      city: ['Shanghai / Шанхай', 'Shenzhen / Шэньчжэнь', 'Guangzhou / Гуанчжоу',
        'Beijing / Пекин', 'Tianjin / Тяньцзинь', 'Qingdao / Циндао',
        'Ningbo / Нинбо', 'Xiamen / Сямэнь', 'Dalian / Далянь',
        'Hangzhou / Ханчжоу', 'Suzhou / Сучжоу', 'Nanjing / Нанкин',
        'Wuhan / Ухань', 'Chengdu / Чэнду', 'Chongqing / Чунцин',
        'Xi’an / Сиань', 'Hong Kong / Гонконг', 'Macau / Макао',
        'Foshan / Фошань', 'Dongguan / Дунгуань', 'Wenzhou / Вэньчжоу',
        'Yiwu / Иу', 'Wuxi / Уси', 'Fuzhou / Фучжоу',
        'Jinan / Цзинань', 'Changsha / Чанша', 'Zhengzhou / Чжэнчжоу',
        'Kunming / Куньмин', 'Shenyang / Шэньян', 'Harbin / Харбин',
        'Changchun / Чанчунь', 'Jilin / Гирин', 'Anshan / Аньшань',
        'Fushun / Фушунь', 'Daqing / Дацин', 'Hefei / Хэфэй',
        'Taiyuan / Тайюань', 'Shijiazhuang / Шицзячжуан', 'Tangshan / Таншань',
        'Baotou / Баотоу', 'Hohhot / Хух-Хото', 'Lanzhou / Ланьчжоу',
        'Nanchang / Наньчан', 'Guiyang / Гуйян', 'Nanning / Наньнин',
        'Haikou / Хайкоу', 'Sanya / Санья', 'Lhasa / Лхаса',
        'Urumqi / Урумчи', 'Yinchuan / Иньчуань', 'Xining / Синин',
        'Yichang / Ичан', 'Yangzhou / Янчжоу', 'Wenling / Вэньлин',
        'Putian / Путянь', 'Quanzhou / Цюаньчжоу', 'Zhongshan / Чжуншань',
        'Zhuhai / Чжухай', 'Huizhou / Хуэйчжоу', 'Jiangmen / Цзянмэнь',
        'Zhanjiang / Чжаньцзян', 'Maoming / Маомин', 'Shantou / Шаньтоу',
        'Liuzhou / Лючжоу', 'Beihai / Бэйхай', 'Mianyang / Мяньян',
        'Luzhou / Лучжоу', 'Yibin / Ибинь', 'Zigong / Цзыгун',
        'Nanchong / Наньчун', 'Deyang / Дэян', 'Lianyungang / Ляньюньган',
        'Yantai / Яньтай', 'Weifang / Вэйфан', 'Weihai / Вэйхай',
        'Linyi / Линьи', 'Zibo / Цзыбо', 'Datong / Датун',
        'Handan / Ханьдань', 'Baoding / Баодин', 'Cangzhou / Цанчжоу',
        'Qinhuangdao / Циньхуандао', 'Wuhu / Уху', 'Xuzhou / Сюйчжоу',
        'Changzhou / Чанчжоу', 'Yangzhou / Янчжоу', 'Nantong / Наньтун',
        'Lianyungang / Ляньюньган', 'Huai’an / Хуайань', 'Yancheng / Яньчэн',
        'Jiaxing / Цзясин', 'Huzhou / Хучжоу', 'Shaoxing / Шаосин',
        'Jinhua / Цзиньхуа', 'Quzhou / Цюйчжоу', 'Taizhou / Тайчжоу',
        'Lijiang / Лицзян', 'Dali / Дали'],
    },
    RU: {
      sea: ['Vladivostok ВМТП / Владивосток ВМТП', 'Vostochny / Восточный (RUVYP)',
        'Nakhodka НМТП / Находка НМТП', 'Novorossiysk НМТП / Новороссийск НМТП',
        'Saint Petersburg / Санкт-Петербург Большой порт', 'Ust-Luga / Усть-Луга',
        'Bronka / Бронка', 'Kaliningrad / Калининград', 'Murmansk / Мурманск'],
      air: ['Sheremetyevo / Шереметьево (SVO)', 'Domodedovo / Домодедово (DME)',
        'Vnukovo / Внуково (VKO)', 'Vladivostok / Владивосток (VVO)',
        'Novosibirsk / Новосибирск (OVB)', 'Yekaterinburg / Екатеринбург (SVX)',
        'Saint Petersburg Pulkovo / Санкт-Петербург Пулково (LED)'],
      city: ['Moscow / Москва', 'Saint Petersburg / Санкт-Петербург',
        'Novosibirsk / Новосибирск', 'Yekaterinburg / Екатеринбург',
        'Kazan / Казань', 'Nizhny Novgorod / Нижний Новгород',
        'Chelyabinsk / Челябинск', 'Samara / Самара',
        'Rostov-on-Don / Ростов-на-Дону', 'Ufa / Уфа',
        'Krasnoyarsk / Красноярск', 'Voronezh / Воронеж',
        'Perm / Пермь', 'Volgograd / Волгоград', 'Krasnodar / Краснодар',
        'Saratov / Саратов', 'Tyumen / Тюмень', 'Togliatti / Тольятти',
        'Barnaul / Барнаул', 'Irkutsk / Иркутск', 'Khabarovsk / Хабаровск',
        'Yaroslavl / Ярославль', 'Vladivostok / Владивосток',
        'Kaliningrad / Калининград', 'Novorossiysk / Новороссийск',
        'Sochi / Сочи', 'Belgorod / Белгород', 'Bryansk / Брянск',
        'Vladimir / Владимир', 'Vologda / Вологда', 'Ivanovo / Иваново',
        'Izhevsk / Ижевск', 'Yoshkar-Ola / Йошкар-Ола', 'Kaluga / Калуга',
        'Kemerovo / Кемерово', 'Kirov / Киров', 'Kostroma / Кострома',
        'Kursk / Курск', 'Lipetsk / Липецк', 'Magnitogorsk / Магнитогорск',
        'Makhachkala / Махачкала', 'Murmansk / Мурманск',
        'Naberezhnye Chelny / Набережные Челны', 'Nizhny Tagil / Нижний Тагил',
        'Novokuznetsk / Новокузнецк', 'Omsk / Омск', 'Orenburg / Оренбург',
        'Orel / Орёл', 'Penza / Пенза', 'Petrozavodsk / Петрозаводск',
        'Pskov / Псков', 'Ryazan / Рязань', 'Saransk / Саранск',
        'Smolensk / Смоленск', 'Stavropol / Ставрополь', 'Surgut / Сургут',
        'Syktyvkar / Сыктывкар', 'Tambov / Тамбов', 'Tver / Тверь',
        'Tomsk / Томск', 'Tula / Тула', 'Ulan-Ude / Улан-Удэ',
        'Ulyanovsk / Ульяновск', 'Cheboksary / Чебоксары',
        'Chita / Чита', 'Yakutsk / Якутск',
        'Petropavlovsk-Kamchatsky / Петропавловск-Камчатский',
        'Yuzhno-Sakhalinsk / Южно-Сахалинск', 'Blagoveshchensk / Благовещенск',
        'Vladikavkaz / Владикавказ', 'Grozny / Грозный',
        'Nalchik / Нальчик', 'Maykop / Майкоп', 'Cherkessk / Черкесск',
        'Magas / Магас', 'Elista / Элиста', 'Naryan-Mar / Нарьян-Мар',
        'Anadyr / Анадырь', 'Salekhard / Салехард', 'Khanty-Mansiysk / Ханты-Мансийск',
        'Birobidzhan / Биробиджан', 'Gorno-Altaysk / Горно-Алтайск',
        'Kyzyl / Кызыл', 'Abakan / Абакан', 'Magadan / Магадан',
        'Nakhodka / Находка', 'Ussuriysk / Уссурийск',
        'Komsomolsk-on-Amur / Комсомольск-на-Амуре',
        'Arkhangelsk / Архангельск', 'Severodvinsk / Северодвинск',
        'Cherepovets / Череповец', 'Norilsk / Норильск', 'Bratsk / Братск',
        'Angarsk / Ангарск', 'Engels / Энгельс', 'Sterlitamak / Стерлитамак',
        'Nefteyugansk / Нефтеюганск', 'Nizhnevartovsk / Нижневартовск',
        'Noyabrsk / Ноябрьск', 'Novy Urengoy / Новый Уренгой',
        'Mytishchi / Мытищи', 'Lyubertsy / Люберцы',
        'Khimki / Химки', 'Balashikha / Балашиха',
        'Korolev / Королёв', 'Podolsk / Подольск'],
    },
    AE: {
      sea: ['Jebel Ali / Джебель-Али (AEJEA)', 'Port Rashid / Порт Рашид (AEPRA)',
        'Khalifa Port / Порт Халифа (AEKLF)', 'Khor Fakkan / Хор Факкан',
        'Port of Fujairah / Порт Фуджейра', 'Hamriyah Port / Порт Хамрия'],
      air: ['Dubai / Дубай (DXB)', 'Al Maktoum / Аль-Мактум (DWC)',
        'Abu Dhabi / Абу-Даби (AUH)', 'Sharjah / Шарджа (SHJ)',
        'Ras Al Khaimah / Рас-эль-Хайма (RKT)'],
      city: ['Dubai / Дубай', 'Abu Dhabi / Абу-Даби', 'Sharjah / Шарджа',
        'Ajman / Аджман', 'Ras Al Khaimah / Рас-эль-Хайма',
        'Fujairah / Фуджейра', 'Umm Al Quwain / Умм-эль-Кайвайн',
        'Al Ain / Аль-Айн', 'Khor Fakkan / Хор-Факкан',
        'Dibba Al-Hisn / Дибба-эль-Хисн', 'Madinat Zayed / Мадинат-Зайед',
        'Ruwais / Эр-Рувайс', 'Jebel Ali / Джебель-Али',
        'Hatta / Хатта', 'Liwa / Лива', 'Ghayathi / Гаяти',
        'Al Dhaid / Эд-Даид', 'Kalba / Калба', 'Masafi / Масафи'],
    },
    TR: {
      sea: ['Istanbul Ambarli / Стамбул Амбарли', 'Mersin / Мерсин',
        'Izmit Kocaeli / Измит Коджаэли', 'Gebze / Гебзе',
        'Izmir Aliağa / Измир Алиага'],
      air: ['Istanbul / Стамбул (IST)', 'Istanbul Sabiha / Стамбул Сабиха (SAW)',
        'Ankara / Анкара (ESB)', 'Antalya / Анталья (AYT)',
        'Izmir / Измир (ADB)'],
      city: ['Istanbul / Стамбул', 'Ankara / Анкара', 'Izmir / Измир',
        'Bursa / Бурса', 'Antalya / Анталья', 'Adana / Адана',
        'Gaziantep / Газиантеп', 'Konya / Конья', 'Mersin / Мерсин',
        'Kocaeli / Коджаэли', 'Kayseri / Кайсери', 'Eskişehir / Эскишехир',
        'Diyarbakır / Диярбакыр', 'Samsun / Самсун', 'Denizli / Денизли',
        'Şanlıurfa / Шанлыурфа', 'Adapazarı / Адапазары', 'Malatya / Малатья',
        'Kahramanmaraş / Кахраманмараш', 'Erzurum / Эрзурум', 'Van / Ван',
        'Batman / Батман', 'Elazığ / Элязыг', 'Manisa / Маниса',
        'Sivas / Сивас', 'Trabzon / Трабзон', 'Çanakkale / Чанаккале',
        'Çorum / Чорум', 'Edirne / Эдирне', 'Kütahya / Кютахья',
        'Tekirdağ / Текирдаг', 'Hatay (Antakya) / Хатай (Антакья)',
        'Aydın / Айдын', 'Balıkesir / Балыкесир', 'Isparta / Ыспарта',
        'Aksaray / Аксарай', 'Sakarya / Сакарья', 'Niğde / Нигде',
        'Yozgat / Йозгат', 'Tokat / Токат', 'Bolu / Болу',
        'Düzce / Дюздже', 'Kırıkkale / Кырыккале', 'Çankırı / Чанкыры',
        'Bilecik / Биледжик', 'Karaman / Караман', 'Burdur / Бурдур'],
    },
    DE: {
      sea: ['Hamburg / Гамбург (DEHAM)', 'Bremerhaven / Бремерхафен (DEBRV)',
        'Wilhelmshaven / Вильгельмсхафен'],
      air: ['Frankfurt / Франкфурт (FRA)', 'München / Мюнхен (MUC)',
        'Düsseldorf / Дюссельдорф (DUS)', 'Köln/Bonn / Кёльн/Бонн (CGN)',
        'Leipzig / Лейпциг (LEJ)'],
      city: ['Berlin / Берлин', 'München / Мюнхен', 'Hamburg / Гамбург',
        'Köln / Кёльн', 'Frankfurt / Франкфурт', 'Stuttgart / Штутгарт',
        'Düsseldorf / Дюссельдорф', 'Dortmund / Дортмунд', 'Essen / Эссен',
        'Leipzig / Лейпциг', 'Bremen / Бремен', 'Dresden / Дрезден',
        'Hannover / Ганновер', 'Nürnberg / Нюрнберг', 'Duisburg / Дуйсбург',
        'Bochum / Бохум', 'Wuppertal / Вупперталь', 'Bielefeld / Билефельд',
        'Bonn / Бонн', 'Mannheim / Мангейм', 'Karlsruhe / Карлсруэ',
        'Augsburg / Аугсбург', 'Wiesbaden / Висбаден',
        'Gelsenkirchen / Гельзенкирхен', 'Mönchengladbach / Мёнхенгладбах',
        'Braunschweig / Брауншвейг', 'Chemnitz / Хемниц', 'Kiel / Киль',
        'Aachen / Ахен', 'Halle / Галле', 'Magdeburg / Магдебург',
        'Freiburg / Фрайбург', 'Krefeld / Крефельд', 'Lübeck / Любек',
        'Oberhausen / Оберхаузен', 'Erfurt / Эрфурт', 'Mainz / Майнц',
        'Rostock / Росток', 'Kassel / Кассель', 'Hagen / Хаген',
        'Hamm / Хамм', 'Saarbrücken / Саарбрюккен',
        'Mülheim / Мюльхайм', 'Potsdam / Потсдам', 'Ludwigshafen / Людвигсхафен',
        'Oldenburg / Ольденбург', 'Leverkusen / Леверкузен',
        'Osnabrück / Оснабрюк', 'Solingen / Золинген', 'Heidelberg / Гейдельберг',
        'Darmstadt / Дармштадт', 'Regensburg / Регенсбург', 'Würzburg / Вюрцбург',
        'Ingolstadt / Ингольштадт', 'Heilbronn / Хайльбронн',
        'Ulm / Ульм', 'Pforzheim / Пфорцхайм', 'Reutlingen / Ройтлинген'],
    },
    KR: {
      sea: ['Busan / Пусан (KRPUS)', 'Incheon / Инчхон (KRINC)',
        'Gwangyang / Кванъян (KRKAN)'],
      air: ['Seoul Incheon / Сеул Инчхон (ICN)', 'Seoul Gimpo / Сеул Кимпо (GMP)',
        'Busan / Пусан (PUS)'],
      city: ['Seoul / Сеул', 'Busan / Пусан', 'Incheon / Инчхон',
        'Daegu / Тэгу', 'Daejeon / Тэджон', 'Gwangju / Кванджу',
        'Ulsan / Ульсан', 'Suwon / Сувон', 'Goyang / Коянг',
        'Changwon / Чханвон', 'Yongin / Ёнин', 'Bucheon / Пучхон',
        'Cheongju / Чхонджу', 'Ansan / Ансан', 'Jeonju / Чонджу',
        'Cheonan / Чхонан', 'Pohang / Пхохан', 'Hwaseong / Хвасон',
        'Namyangju / Намъянджу', 'Gimhae / Кимхэ', 'Jeju / Чеджу',
        'Pyeongtaek / Пхёнтхэк', 'Uijeongbu / Ыйджонбу',
        'Siheung / Сихын', 'Paju / Паджу', 'Cheongju / Чхонджу',
        'Gunpo / Кунпхо', 'Anyang / Аньян', 'Sejong / Седжон',
        'Wonju / Вонджу', 'Asan / Асан', 'Chuncheon / Чхунчхон',
        'Gangneung / Каннын', 'Gumi / Куми', 'Gyeongju / Кёнджу',
        'Mokpo / Мокпхо', 'Yeosu / Ёсу', 'Tongyeong / Тхонъён'],
    },
    JP: {
      sea: ['Yokohama / Иокогама (JPYOK)', 'Tokyo / Токио (JPTYO)',
        'Kobe / Кобе (JPUKB)', 'Nagoya / Нагоя (JPNGO)',
        'Osaka / Осака (JPOSA)'],
      air: ['Tokyo Narita / Токио Нарита (NRT)', 'Tokyo Haneda / Токио Ханэда (HND)',
        'Osaka Kansai / Осака Кансай (KIX)', 'Nagoya / Нагоя (NGO)'],
      city: ['Tokyo / Токио', 'Yokohama / Иокогама', 'Osaka / Осака',
        'Nagoya / Нагоя', 'Sapporo / Саппоро', 'Kobe / Кобе',
        'Kyoto / Киото', 'Fukuoka / Фукуока', 'Kawasaki / Кавасаки',
        'Saitama / Сайтама', 'Hiroshima / Хиросима', 'Sendai / Сэндай',
        'Chiba / Тиба', 'Kitakyushu / Китакюсю', 'Sakai / Сакаи',
        'Niigata / Ниигата', 'Hamamatsu / Хамамацу', 'Okayama / Окаяма',
        'Sagamihara / Сагамихара', 'Shizuoka / Сидзуока',
        'Kumamoto / Кумамото', 'Kagoshima / Кагосима',
        'Funabashi / Фунабаси', 'Hachioji / Хатиодзи', 'Matsuyama / Мацуяма',
        'Higashiosaka / Хигасиосака', 'Nishinomiya / Нисиномия',
        'Kurashiki / Курасики', 'Ichikawa / Итикава', 'Fukuyama / Фукуяма',
        'Amagasaki / Амагасаки', 'Toyota / Тоёта', 'Suita / Суйта',
        'Naha / Наха', 'Kanazawa / Канадзава', 'Utsunomiya / Уцуномия',
        'Matsudo / Мацудо', 'Hirakata / Хираката', 'Kashiwa / Касива',
        'Toyonaka / Тоёнака', 'Yokosuka / Йокосука', 'Toyohashi / Тоёхаси',
        'Maebashi / Маэбаси', 'Takamatsu / Такамацу', 'Aomori / Аомори',
        'Akita / Акита', 'Iwaki / Иваки', 'Koshigaya / Косигая',
        'Wakayama / Вакаяма', 'Nara / Нара', 'Tokushima / Токусима',
        'Otsu / Оцу', 'Morioka / Мориока', 'Yamagata / Ямагата',
        'Mito / Мито', 'Fukushima / Фукусима', 'Kofu / Кофу',
        'Toyama / Тояма', 'Fukui / Фукуи', 'Gifu / Гифу',
        'Tsu / Цу', 'Tottori / Тоттори', 'Matsue / Мацуэ',
        'Yamaguchi / Ямагути', 'Saga / Сага', 'Nagasaki / Нагасаки',
        'Oita / Оита', 'Miyazaki / Миядзаки'],
    },
    IN: {
      sea: ['Mundra / Мундра (INMUN)', 'Nhava Sheva JNPT / Нхава-Шева (INNSA)',
        'Chennai / Ченнаи (INMAA)', 'Mumbai / Мумбаи (INBOM)',
        'Kolkata / Калькутта (INCCU)', 'Cochin / Кочин (INCOK)'],
      air: ['Mumbai / Мумбаи (BOM)', 'Delhi / Дели (DEL)',
        'Chennai / Ченнаи (MAA)', 'Bangalore / Бангалор (BLR)',
        'Hyderabad / Хайдарабад (HYD)', 'Kolkata / Калькутта (CCU)'],
      city: ['Mumbai / Мумбаи', 'Delhi / Дели', 'Bangalore / Бангалор',
        'Hyderabad / Хайдарабад', 'Chennai / Ченнаи', 'Kolkata / Калькутта',
        'Pune / Пуна', 'Ahmedabad / Ахмадабад', 'Surat / Сурат',
        'Jaipur / Джайпур', 'Lucknow / Лакхнау', 'Kanpur / Канпур',
        'Nagpur / Нагпур', 'Indore / Индаур', 'Visakhapatnam / Вишакхапатнам',
        'Bhopal / Бхопал', 'Patna / Патна', 'Vadodara / Вадодара',
        'Ghaziabad / Газиабад', 'Ludhiana / Лудхиана', 'Agra / Агра',
        'Nashik / Нашик', 'Faridabad / Фаридабад', 'Meerut / Мирут',
        'Rajkot / Раджкот', 'Kalyan-Dombivli / Кальян-Домбивли',
        'Vasai-Virar / Васаи-Вирар', 'Varanasi / Варанаси',
        'Srinagar / Сринагар', 'Aurangabad / Аурангабад',
        'Dhanbad / Дханбад', 'Amritsar / Амритсар', 'Navi Mumbai / Нави-Мумбаи',
        'Allahabad / Аллахабад', 'Ranchi / Ранчи', 'Howrah / Ховрах',
        'Coimbatore / Коимбатор', 'Jabalpur / Джабалпур', 'Gwalior / Гвалиор',
        'Vijayawada / Виджаявада', 'Jodhpur / Джодхпур', 'Madurai / Мадурай',
        'Raipur / Райпур', 'Kota / Кота', 'Chandigarh / Чандигарх',
        'Guwahati / Гувахати', 'Solapur / Шолапур',
        'Hubli-Dharwad / Хубли-Дхарвад', 'Bareilly / Барейли',
        'Moradabad / Морадабад', 'Mysore / Майсур', 'Tiruchirappalli / Тиручираппалли',
        'Tiruppur / Тируппур', 'Salem / Салем', 'Bhubaneswar / Бхубанешвар',
        'Aligarh / Алигарх', 'Saharanpur / Сахаранпур', 'Bikaner / Биканер',
        'Jamshedpur / Джамшедпур', 'Bhilai / Бхилаи', 'Cuttack / Каттак',
        'Firozabad / Фирозабад', 'Kochi / Кочи', 'Trivandrum / Тривандрум',
        'Pondicherry / Пондишери', 'Gurgaon / Гуруграм', 'Noida / Нойда'],
    },
    US: {
      sea: ['Los Angeles / Лос-Анджелес (USLAX)',
        'Long Beach / Лонг-Бич (USLGB)',
        'New York/New Jersey / Нью-Йорк/Нью-Джерси',
        'Savannah / Саванна', 'Seattle/Tacoma / Сиэтл/Такома',
        'Houston / Хьюстон', 'Charleston / Чарлстон'],
      air: ['Los Angeles / Лос-Анджелес (LAX)', 'New York JFK / Нью-Йорк JFK',
        'Chicago ORD / Чикаго ORD', 'Miami / Майами (MIA)',
        'Atlanta / Атланта (ATL)', 'Dallas / Даллас (DFW)',
        'Seattle / Сиэтл (SEA)'],
      city: ['New York / Нью-Йорк', 'Los Angeles / Лос-Анджелес',
        'Chicago / Чикаго', 'Houston / Хьюстон', 'Phoenix / Финикс',
        'Philadelphia / Филадельфия', 'San Antonio / Сан-Антонио',
        'San Diego / Сан-Диего', 'Dallas / Даллас', 'Austin / Остин',
        'Jacksonville / Джексонвилл', 'San Francisco / Сан-Франциско',
        'Seattle / Сиэтл', 'Miami / Майами', 'Atlanta / Атланта',
        'Boston / Бостон', 'Denver / Денвер', 'Portland / Портленд',
        'Las Vegas / Лас-Вегас', 'Washington DC / Вашингтон',
        'Nashville / Нэшвилл', 'Detroit / Детройт', 'Memphis / Мемфис',
        'Louisville / Луисвилл', 'Baltimore / Балтимор',
        'Milwaukee / Милуоки', 'Albuquerque / Альбукерке',
        'Tucson / Тусон', 'Fresno / Фресно', 'Sacramento / Сакраменто',
        'Mesa / Меса', 'Kansas City / Канзас-Сити', 'Long Beach / Лонг-Бич',
        'Colorado Springs / Колорадо-Спрингс', 'Raleigh / Роли',
        'Omaha / Омаха', 'Minneapolis / Миннеаполис', 'Cleveland / Кливленд',
        'Tulsa / Талса', 'Wichita / Уичита', 'Arlington / Арлингтон',
        'New Orleans / Новый Орлеан', 'Honolulu / Гонолулу',
        'Anaheim / Анахайм', 'Tampa / Тампа', 'Aurora / Аврора',
        'Riverside / Риверсайд', 'St. Louis / Сент-Луис',
        'Pittsburgh / Питтсбург', 'Cincinnati / Цинциннати',
        'Orlando / Орландо', 'Bakersfield / Бейкерсфилд',
        'Stockton / Стоктон', 'Buffalo / Буффало', 'Toledo / Толидо',
        'Lincoln / Линкольн', 'Plano / Плано', 'Greensboro / Гринсборо',
        'Henderson / Хендерсон', 'Madison / Мэдисон',
        'Jersey City / Джерси-Сити', 'Chandler / Чандлер',
        'Fort Wayne / Форт-Уэйн', 'Charlotte / Шарлотт',
        'Indianapolis / Индианаполис', 'Columbus / Колумбус',
        'Fort Worth / Форт-Уэрт', 'El Paso / Эль-Пасо',
        'Oklahoma City / Оклахома-Сити', 'Virginia Beach / Вирджиния-Бич',
        'Salt Lake City / Солт-Лейк-Сити', 'Anchorage / Анкоридж',
        'St. Paul / Сент-Пол'],
    },
    IT: {
      sea: ['Genova / Генуя', 'La Spezia / Ла-Специя', 'Trieste / Триест',
        'Livorno / Ливорно', 'Gioia Tauro / Джойя-Тауро'],
      air: ['Milano Malpensa / Милан Мальпенса (MXP)',
        'Roma Fiumicino / Рим Фьюмичино (FCO)',
        'Bergamo / Бергамо (BGY)'],
      city: ['Roma / Рим', 'Milano / Милан', 'Napoli / Неаполь',
        'Torino / Турин', 'Bologna / Болонья', 'Genova / Генуя',
        'Firenze / Флоренция', 'Palermo / Палермо', 'Catania / Катания',
        'Bari / Бари', 'Verona / Верона', 'Padova / Падуя',
        'Brescia / Брешиа', 'Modena / Модена', 'Parma / Парма',
        'Reggio Emilia / Реджо-Эмилия', 'Trento / Тренто',
        'Perugia / Перуджа', 'Ravenna / Равенна', 'Livorno / Ливорно',
        'Cagliari / Кальяри', 'Foggia / Фоджа', 'Rimini / Римини',
        'Salerno / Салерно', 'Ferrara / Феррара', 'Sassari / Сассари',
        'Latina / Латина', 'Giugliano in Campania / Джульяно-ин-Кампанья',
        'Monza / Монца', 'Siracusa / Сиракузы', 'Pescara / Пескара',
        'Bergamo / Бергамо', 'Vicenza / Виченца', 'Trieste / Триест',
        'Bolzano / Больцано', 'Novara / Новара', 'Piacenza / Пьяченца',
        'Ancona / Анкона', 'Lecce / Лечче', 'Udine / Удине',
        'Arezzo / Ареццо', 'Cesena / Чезена', 'La Spezia / Ла-Специя',
        'Pesaro / Пезаро', 'Alessandria / Алессандрия',
        'Pistoia / Пистоя', 'Catanzaro / Катандзаро',
        'Brindisi / Бриндизи', 'Taranto / Таранто'],
    },
    // ── Остальные страны (COUNTRIES_ALL): города в формате
    // "English / Русский", главные порты/аэропорты ключевых направлений.
    AU: { city: ['Sydney / Сидней', 'Melbourne / Мельбурн', 'Brisbane / Брисбен',
        'Perth / Перт', 'Adelaide / Аделаида', 'Canberra / Канберра',
        'Gold Coast / Голд-Кост', 'Newcastle / Ньюкасл',
        'Wollongong / Вуллонгонг', 'Hobart / Хобарт', 'Geelong / Джилонг',
        'Townsville / Таунсвилл', 'Cairns / Кэрнс', 'Darwin / Дарвин',
        'Toowoomba / Тувумба', 'Ballarat / Балларат', 'Bendigo / Бендиго',
        'Albury / Олбери', 'Mackay / Маккай', 'Rockhampton / Рокгемптон',
        'Bunbury / Банбери', 'Bundaberg / Бандаберг', 'Coffs Harbour / Коффс-Харбор',
        'Hervey Bay / Херви-Бэй', 'Wagga Wagga / Уогга-Уогга',
        'Mildura / Милдьюра', 'Shepparton / Шеппартон',
        'Port Macquarie / Порт-Маккуори', 'Tamworth / Тамворт',
        'Orange / Ориндж', 'Dubbo / Даббо', 'Geraldton / Джералдтон',
        'Kalgoorlie / Калгурли', 'Mount Isa / Маунт-Иса',
        'Broken Hill / Брокен-Хилл'],
        sea: ['Sydney / Сидней', 'Melbourne / Мельбурн', 'Brisbane / Брисбен', 'Fremantle / Фримантл'],
        air: ['Sydney / Сидней (SYD)', 'Melbourne / Мельбурн (MEL)', 'Brisbane / Брисбен (BNE)', 'Perth / Перт (PER)'] },
    AT: { city: ['Vienna / Вена', 'Graz / Грац', 'Linz / Линц',
        'Salzburg / Зальцбург', 'Innsbruck / Инсбрук', 'Klagenfurt / Клагенфурт',
        'Villach / Филлах', 'Wels / Вельс', 'St. Pölten / Санкт-Пёльтен',
        'Dornbirn / Дорнбирн', 'Steyr / Штайр', 'Feldkirch / Фельдкирх',
        'Bregenz / Брегенц', 'Leoben / Леобен', 'Krems / Кремс',
        'Wiener Neustadt / Винер-Нойштадт', 'Lustenau / Лустенау',
        'Klosterneuburg / Клостернойбург', 'Baden / Баден',
        'Wolfsberg / Вольфсберг', 'Eisenstadt / Эйзенштадт'],
        sea: [], air: ['Vienna / Вена (VIE)', 'Salzburg / Зальцбург (SZG)', 'Innsbruck / Инсбрук (INN)', 'Graz / Грац (GRZ)'] },
    AR: { city: ['Buenos Aires / Буэнос-Айрес', 'Córdoba / Кордова',
        'Rosario / Росарио', 'Mendoza / Мендоса', 'La Plata / Ла-Плата',
        'Mar del Plata / Мар-дель-Плата', 'Tucumán / Тукуман',
        'Salta / Сальта', 'Santa Fe / Санта-Фе', 'Bahía Blanca / Баия-Бланка',
        'Resistencia / Ресистенсия', 'San Juan / Сан-Хуан',
        'Posadas / Посадас', 'Neuquén / Неукен', 'Jujuy / Жужуй',
        'Corrientes / Корриентес', 'Comodoro Rivadavia / Комодоро-Ривадавия',
        'Río Cuarto / Рио-Куарто', 'Paraná / Парана', 'San Luis / Сан-Луис',
        'San Salvador de Jujuy / Сан-Сальвадор-де-Жужуй',
        'Catamarca / Катамарка', 'La Rioja / Ла-Риоха',
        'Formosa / Формоса', 'Río Gallegos / Рио-Гальегос',
        'Ushuaia / Ушуая'],
        sea: ['Buenos Aires / Буэнос-Айрес', 'Rosario / Росарио'], air: ['Buenos Aires Ezeiza / Буэнос-Айрес Эсейса (EZE)', 'Córdoba / Кордова (COR)'] },
    BE: { city: ['Brussels / Брюссель', 'Antwerp / Антверпен', 'Ghent / Гент',
        'Bruges / Брюгге', 'Liège / Льеж', 'Charleroi / Шарлеруа',
        'Namur / Намюр', 'Mons / Монс', 'Leuven / Лёвен',
        'Mechelen / Мехелен', 'Aalst / Алст', 'Hasselt / Хасселт',
        'Sint-Niklaas / Синт-Никлас', 'Ostend / Остенде',
        'Tournai / Турне', 'Verviers / Вервье',
        'Kortrijk / Кортрейк', 'Genk / Генк', 'Roeselare / Руселаре',
        'Mouscron / Мускрон', 'La Louvière / Ла-Лувьер',
        'Seraing / Серен', 'Brasschaat / Брасхат', 'Sint-Truiden / Синт-Трёйден'],
        sea: ['Antwerp / Антверпен (BEANR)', 'Zeebrugge / Зебрюгге'], air: ['Brussels / Брюссель (BRU)', 'Liège / Льеж (LGG)', 'Charleroi / Шарлеруа (CRL)'] },
    BG: { city: ['Sofia / София', 'Plovdiv / Пловдив', 'Varna / Варна',
        'Burgas / Бургас', 'Ruse / Русе', 'Stara Zagora / Стара-Загора',
        'Pleven / Плевен', 'Sliven / Сливен', 'Dobrich / Добрич',
        'Shumen / Шумен', 'Pernik / Перник', 'Yambol / Ямбол',
        'Haskovo / Хасково', 'Pazardzhik / Пазарджик',
        'Blagoevgrad / Благоевград', 'Veliko Tarnovo / Велико-Тырново',
        'Vratsa / Враца', 'Kazanlak / Казанлык', 'Asenovgrad / Асеновград',
        'Gabrovo / Габрово'],
        sea: ['Varna / Варна', 'Burgas / Бургас'], air: ['Sofia / София (SOF)', 'Varna / Варна (VAR)', 'Burgas / Бургас (BOJ)'] },
    BR: { city: ['São Paulo / Сан-Паулу', 'Rio de Janeiro / Рио-де-Жанейро',
        'Brasília / Бразилиа', 'Salvador / Салвадор',
        'Fortaleza / Форталеза', 'Belo Horizonte / Белу-Оризонти',
        'Manaus / Манаус', 'Curitiba / Куритиба', 'Recife / Ресифи',
        'Porto Alegre / Порту-Алегри', 'Belém / Белен',
        'Goiânia / Гояния', 'Campinas / Кампинас', 'Guarulhos / Гуарульюс',
        'São Luís / Сан-Луис', 'Natal / Натал', 'Maceió / Масейо',
        'Campo Grande / Кампу-Гранди', 'Teresina / Терезина',
        'João Pessoa / Жуан-Песоа', 'Florianópolis / Флорианополис',
        'Vitória / Витория', 'Cuiabá / Куяба', 'Aracaju / Аракажу',
        'Joinville / Жоинвиль', 'Caxias do Sul / Кашиас-ду-Сул',
        'Londrina / Лондрина', 'Ribeirão Preto / Рибейран-Прету',
        'Sorocaba / Сорокаба', 'Niterói / Нитерой',
        'Jaboatão dos Guararapes / Жабоатан-дус-Гуарарапис',
        'Osasco / Озаску', 'Santo André / Санту-Андре',
        'São Bernardo do Campo / Сан-Бернарду-ду-Кампу',
        'Nova Iguaçu / Нова-Игуасу', 'Duque de Caxias / Дуки-ди-Кашиас',
        'São José dos Campos / Сан-Жозе-дус-Кампус',
        'Uberlândia / Уберландия', 'Contagem / Контажен',
        'Feira de Santana / Фейра-ди-Сантана', 'Aparecida de Goiânia / Апаресида-ди-Гояния',
        'São Vicente / Сан-Висенти', 'Santos / Сантус',
        'Mauá / Мауа', 'Jundiaí / Жундиай', 'Piracicaba / Пирасикаба',
        'Macapá / Макапа', 'Boa Vista / Боа-Виста', 'Palmas / Палмас',
        'Rio Branco / Риу-Бранку', 'Porto Velho / Порту-Велью'],
        sea: ['Santos / Сантос', 'Rio de Janeiro / Рио-де-Жанейро', 'Paranaguá / Паранагуа', 'Itajaí / Итажай'],
        air: ['São Paulo Guarulhos / Сан-Паулу Гуарульюс (GRU)', 'Rio de Janeiro / Рио-де-Жанейро (GIG)', 'Brasília / Бразилиа (BSB)', 'Manaus / Манаус (MAO)'] },
    GB: { city: ['London / Лондон', 'Manchester / Манчестер',
        'Birmingham / Бирмингем', 'Glasgow / Глазго',
        'Liverpool / Ливерпуль', 'Edinburgh / Эдинбург',
        'Leeds / Лидс', 'Sheffield / Шеффилд', 'Bristol / Бристоль',
        'Newcastle upon Tyne / Ньюкасл-апон-Тайн', 'Cardiff / Кардифф',
        'Belfast / Белфаст', 'Nottingham / Ноттингем',
        'Coventry / Ковентри', 'Leicester / Лестер',
        'Stoke-on-Trent / Сток-он-Трент', 'Aberdeen / Абердин',
        'Plymouth / Плимут', 'Wolverhampton / Вулверхэмптон',
        'Derby / Дерби', 'Swansea / Суонси', 'Brighton / Брайтон',
        'Sunderland / Сандерленд', 'Reading / Рединг',
        'Oxford / Оксфорд', 'Cambridge / Кембридж', 'York / Йорк',
        'Norwich / Норвич', 'Southampton / Саутгемптон',
        'Portsmouth / Портсмут', 'Bradford / Брадфорд',
        'Hull / Халл', 'Wakefield / Уэйкфилд', 'Bath / Бат',
        'Exeter / Эксетер', 'Canterbury / Кентербери',
        'Inverness / Инвернесс', 'Bournemouth / Борнмут',
        'Milton Keynes / Милтон-Кинс', 'Northampton / Нортгемптон',
        'Luton / Лутон', 'Slough / Слау', 'Watford / Уотфорд',
        'Crawley / Кроули', 'Maidstone / Мейдстон', 'Chester / Честер',
        'Carlisle / Карлайл', 'Lancaster / Ланкастер',
        'Preston / Престон', 'Blackpool / Блэкпул',
        'Middlesbrough / Мидлсбро', 'Doncaster / Донкастер',
        'Mansfield / Мансфилд', 'Worcester / Вустер',
        'Gloucester / Глостер', 'Cheltenham / Челтнем'],
        sea: ['Felixstowe / Феликстоу', 'Southampton / Саутгемптон', 'London Gateway / Лондон Гейтвей', 'Liverpool / Ливерпуль'],
        air: ['London Heathrow / Лондон Хитроу (LHR)', 'London Gatwick / Лондон Гатвик (LGW)', 'Manchester / Манчестер (MAN)', 'Birmingham / Бирмингем (BHX)'] },
    HU: { city: ['Budapest / Будапешт', 'Debrecen / Дебрецен',
        'Szeged / Сегед', 'Miskolc / Мишкольц', 'Pécs / Печ',
        'Győr / Дёр', 'Nyíregyháza / Ньиредьхаза', 'Kecskemét / Кечкемет',
        'Székesfehérvár / Секешфехервар', 'Szombathely / Сомбатхей',
        'Szolnok / Сольнок', 'Tatabánya / Татабанья', 'Kaposvár / Капошвар',
        'Békéscsaba / Бекешчаба', 'Veszprém / Веспрем',
        'Zalaegerszeg / Залаэгерсег', 'Sopron / Шопрон',
        'Eger / Эгер', 'Nagykanizsa / Надьканижа', 'Dunaújváros / Дунауйварош'],
        sea: [], air: ['Budapest / Будапешт (BUD)', 'Debrecen / Дебрецен (DEB)'] },
    VN: { city: ['Ho Chi Minh City / Хошимин', 'Hanoi / Ханой',
        'Da Nang / Дананг', 'Hai Phong / Хайфон', 'Can Tho / Кантхо',
        'Bien Hoa / Бьенхоа', 'Hue / Хюэ', 'Nha Trang / Нячанг',
        'Buon Ma Thuot / Буонматхуот', 'Vinh / Винь',
        'Long Xuyen / Лонгсюйен', 'Quy Nhon / Куиньон',
        'Rach Gia / Рактья', 'Phan Thiet / Фантхьет',
        'Cam Ranh / Камрань', 'Vung Tau / Вунгтау',
        'Da Lat / Далат', 'Thai Nguyen / Тхайнгуен',
        'Nam Dinh / Намдинь', 'Thanh Hoa / Тханьхоа',
        'Ha Long / Халонг', 'My Tho / Митхо', 'Sa Dec / Шадек',
        'Tra Vinh / Чавинь', 'Ben Tre / Бенче'],
        sea: ['Ho Chi Minh Cai Mep / Хошимин Кай-Меп', 'Hai Phong / Хайфон', 'Da Nang / Дананг'],
        air: ['Ho Chi Minh / Хошимин (SGN)', 'Hanoi Noi Bai / Ханой Нойбай (HAN)', 'Da Nang / Дананг (DAD)'] },
    GR: { city: ['Athens / Афины', 'Thessaloniki / Салоники',
        'Patras / Патры', 'Piraeus / Пирей', 'Heraklion / Ираклион',
        'Larissa / Лариса', 'Volos / Волос', 'Rhodes / Родос',
        'Ioannina / Янина', 'Chania / Ханья', 'Chalcis / Халкида',
        'Agrinio / Агринион', 'Katerini / Катерини',
        'Trikala / Трикала', 'Serres / Серре', 'Lamia / Ламия',
        'Alexandroupoli / Александруполис', 'Kavala / Кавала',
        'Kalamata / Каламата', 'Veria / Верия',
        'Komotini / Комотини', 'Drama / Драма',
        'Kozani / Козани', 'Karditsa / Кардица', 'Corfu / Корфу'],
        sea: ['Piraeus / Пирей (GRPIR)', 'Thessaloniki / Салоники'], air: ['Athens / Афины (ATH)', 'Thessaloniki / Салоники (SKG)', 'Heraklion / Ираклион (HER)'] },
    DK: { city: ['Copenhagen / Копенгаген', 'Aarhus / Орхус',
        'Odense / Оденсе', 'Aalborg / Ольборг', 'Esbjerg / Эсбьерг',
        'Randers / Раннерс', 'Kolding / Колдинг', 'Horsens / Хорсенс',
        'Vejle / Вайле', 'Roskilde / Роскилле', 'Herning / Хернинг',
        'Helsingør / Хельсингёр', 'Silkeborg / Силькеборг',
        'Næstved / Нествед', 'Fredericia / Фредерисия',
        'Viborg / Виборг', 'Køge / Кёге', 'Holstebro / Хольстебро',
        'Taastrup / Тоструп', 'Slagelse / Слагельсе'],
        sea: ['Copenhagen / Копенгаген (DKCPH)', 'Aarhus / Орхус', 'Esbjerg / Эсбьерг'], air: ['Copenhagen / Копенгаген (CPH)', 'Billund / Биллунн (BLL)', 'Aarhus / Орхус (AAR)'] },
    EG: { city: ['Cairo / Каир', 'Alexandria / Александрия', 'Giza / Гиза',
        'Port Said / Порт-Саид', 'Suez / Суэц', 'Luxor / Луксор',
        'Mansoura / Мансура', 'Aswan / Асуан',
        'El-Mahalla El-Kubra / Эль-Махалла-эль-Кубра', 'Tanta / Танта',
        'Asyut / Асьют', 'Ismailia / Исмаилия', 'Faiyum / Эль-Файюм',
        'Zagazig / Заказик', 'Damanhour / Даманхур',
        'Damietta / Думьят', 'Beni Suef / Бени-Суэйф',
        'Minya / Эль-Минья', 'Qena / Кена', 'Sohag / Сохаг',
        'Hurghada / Хургада', 'Sharm El-Sheikh / Шарм-эш-Шейх',
        'Marsa Matruh / Марса-Матрух', 'Arish / Эль-Ариш',
        '6th of October / 6 октября', 'New Cairo / Новый Каир'],
        sea: ['Alexandria / Александрия (EGALY)', 'Port Said / Порт-Саид (EGPSD)', 'Damietta / Думьят'],
        air: ['Cairo / Каир (CAI)', 'Alexandria / Александрия (HBE)', 'Hurghada / Хургада (HRG)', 'Sharm El-Sheikh / Шарм-эш-Шейх (SSH)'] },
    IL: { city: ['Tel Aviv / Тель-Авив', 'Jerusalem / Иерусалим',
        'Haifa / Хайфа', 'Rishon LeZion / Ришон-ле-Цион',
        'Petah Tikva / Петах-Тиква', 'Ashdod / Ашдод',
        'Netanya / Нетания', 'Beer Sheva / Беэр-Шева',
        'Bnei Brak / Бней-Брак', 'Holon / Холон',
        'Ramat Gan / Рамат-Ган', 'Ashkelon / Ашкелон',
        'Rehovot / Реховот', 'Bat Yam / Бат-Ям',
        'Beit Shemesh / Бейт-Шемеш', 'Kfar Saba / Кфар-Саба',
        'Herzliya / Герцлия', 'Modi’in / Модиин',
        'Nazareth / Назарет', 'Eilat / Эйлат',
        'Tiberias / Тверия', 'Lod / Лод', 'Ramla / Рамле',
        'Hadera / Хадера', 'Acre / Акко', 'Nahariya / Нагария'],
        sea: ['Haifa / Хайфа (ILHFA)', 'Ashdod / Ашдод (ILASH)'], air: ['Tel Aviv / Тель-Авив (TLV)', 'Eilat / Эйлат (ETM)'] },
    ID: { city: ['Jakarta / Джакарта', 'Surabaya / Сурабая',
        'Bandung / Бандунг', 'Medan / Медан', 'Bekasi / Бекаси',
        'Semarang / Семаранг', 'Tangerang / Тангеранг',
        'Depok / Депок', 'Palembang / Палембанг', 'Makassar / Макасар',
        'South Tangerang / Южный Тангеранг', 'Batam / Батам',
        'Pekanbaru / Пеканбару', 'Bogor / Богор', 'Padang / Паданг',
        'Malang / Маланг', 'Denpasar / Денпасар',
        'Samarinda / Самаринда', 'Tasikmalaya / Тасикмалая',
        'Banjarmasin / Банжармасин', 'Balikpapan / Баликпапан',
        'Pontianak / Понтианак', 'Manado / Манадо',
        'Cimahi / Чимахи', 'Yogyakarta / Джокьякарта',
        'Surakarta / Суракарта', 'Mataram / Матарам',
        'Jambi / Джамби', 'Cilegon / Чилегон',
        'Kupang / Купанг', 'Ambon / Амбон',
        'Sorong / Соронг', 'Manokwari / Маноквари'],
        sea: ['Jakarta Tanjung Priok / Джакарта Танджунг-Приок', 'Surabaya Tanjung Perak / Сурабая Танджунг-Перак'],
        air: ['Jakarta Soekarno-Hatta / Джакарта (CGK)', 'Bali Denpasar / Бали Денпасар (DPS)', 'Surabaya / Сурабая (SUB)'] },
    IR: { city: ['Tehran / Тегеран', 'Mashhad / Мешхед', 'Isfahan / Исфахан',
        'Tabriz / Тебриз', 'Shiraz / Шираз', 'Karaj / Кередж',
        'Qom / Кум', 'Ahvaz / Ахваз', 'Kermanshah / Керманшах',
        'Urmia / Урмия', 'Rasht / Решт', 'Zahedan / Захедан',
        'Hamadan / Хамадан', 'Kerman / Керман', 'Yazd / Йезд',
        'Ardabil / Ардебиль', 'Bandar Abbas / Бендер-Аббас',
        'Eslamshahr / Исламшехр', 'Zanjan / Зенджан',
        'Sanandaj / Сенендедж', 'Qazvin / Казвин',
        'Khorramabad / Хорремабад', 'Gorgan / Горган',
        'Sari / Сари', 'Arak / Эрак', 'Bushehr / Бушер'],
        sea: ['Bandar Abbas / Бендер-Аббас (IRBND)', 'Bushehr / Бушер'], air: ['Tehran Imam Khomeini / Тегеран (IKA)', 'Mashhad / Мешхед (MHD)'] },
    IE: { city: ['Dublin / Дублин', 'Cork / Корк', 'Limerick / Лимерик',
        'Galway / Голуэй', 'Waterford / Уотерфорд', 'Drogheda / Дроэда',
        'Swords / Свордс', 'Dundalk / Дандолк', 'Bray / Брей',
        'Navan / Наван', 'Kilkenny / Килкенни', 'Tralee / Трали',
        'Wexford / Уэксфорд', 'Athlone / Атлон', 'Sligo / Слайго',
        'Letterkenny / Леттеркенни', 'Carlow / Карлоу',
        'Mullingar / Малингар', 'Ennis / Эннис', 'Killarney / Килларни'],
        sea: ['Dublin / Дублин', 'Cork / Корк'], air: ['Dublin / Дублин (DUB)', 'Cork / Корк (ORK)', 'Shannon / Шеннон (SNN)'] },
    ES: { city: ['Madrid / Мадрид', 'Barcelona / Барселона',
        'Valencia / Валенсия', 'Seville / Севилья',
        'Zaragoza / Сарагоса', 'Málaga / Малага', 'Bilbao / Бильбао',
        'Palma / Пальма', 'Murcia / Мурсия', 'Alicante / Аликанте',
        'Córdoba / Кордова', 'Valladolid / Вальядолид',
        'Vigo / Виго', 'Gijón / Хихон', 'L’Hospitalet / Оспиталет',
        'A Coruña / Ла-Корунья', 'Vitoria-Gasteiz / Витория-Гастейс',
        'Granada / Гранада', 'Elche / Эльче', 'Oviedo / Овьедо',
        'Badalona / Бадалона', 'Cartagena / Картахена',
        'Terrassa / Терраса', 'Jerez / Херес-де-ла-Фронтера',
        'Sabadell / Сабадель', 'Móstoles / Мостолес',
        'Santa Cruz de Tenerife / Санта-Крус-де-Тенерифе',
        'Pamplona / Памплона', 'Almería / Альмерия',
        'Alcalá / Алькала-де-Энарес', 'San Sebastián / Сан-Себастьян',
        'Burgos / Бургос', 'Salamanca / Саламанка',
        'Logroño / Логроньо', 'Huelva / Уэльва',
        'Tarragona / Таррагона', 'Lleida / Льейда',
        'Marbella / Марбелья', 'León / Леон',
        'Castellón / Кастельон-де-ла-Плана', 'Cádiz / Кадис',
        'Mataró / Матаро', 'Reus / Реус', 'Toledo / Толедо'],
        sea: ['Valencia / Валенсия (ESVLC)', 'Barcelona / Барселона (ESBCN)', 'Algeciras / Альхесирас'],
        air: ['Madrid / Мадрид (MAD)', 'Barcelona / Барселона (BCN)', 'Palma / Пальма (PMI)', 'Málaga / Малага (AGP)'] },
    KZ: { city: ['Almaty / Алматы', 'Astana / Астана', 'Shymkent / Шымкент',
        'Karaganda / Караганда', 'Aktobe / Актобе', 'Atyrau / Атырау',
        'Pavlodar / Павлодар', 'Oskemen / Усть-Каменогорск',
        'Kyzylorda / Кызылорда', 'Taraz / Тараз',
        'Kostanay / Костанай', 'Petropavl / Петропавловск',
        'Oral / Уральск', 'Aktau / Актау', 'Temirtau / Темиртау',
        'Semey / Семей', 'Kokshetau / Кокшетау',
        'Taldykorgan / Талдыкорган', 'Ekibastuz / Экибастуз',
        'Stepnogorsk / Степногорск', 'Rudny / Рудный',
        'Zhanaozen / Жанаозен', 'Balkhash / Балхаш',
        'Turkistan / Туркестан', 'Zhezkazgan / Жезказган'],
        sea: ['Aktau / Актау'], air: ['Almaty / Алматы (ALA)', 'Astana / Астана (NQZ)', 'Shymkent / Шымкент (CIT)'] },
    CA: { city: ['Toronto / Торонто', 'Montreal / Монреаль',
        'Vancouver / Ванкувер', 'Calgary / Калгари',
        'Edmonton / Эдмонтон', 'Ottawa / Оттава',
        'Winnipeg / Виннипег', 'Quebec City / Квебек',
        'Hamilton / Гамильтон', 'Kitchener / Китченер',
        'London / Лондон', 'Victoria / Виктория',
        'Halifax / Галифакс', 'Oshawa / Ошава',
        'Windsor / Виндзор', 'Saskatoon / Саскатун',
        'Regina / Реджайна', 'Sherbrooke / Шербрук',
        'St John’s / Сент-Джонс', 'Barrie / Барри',
        'Kelowna / Келоуна', 'Abbotsford / Абботсфорд',
        'Trois-Rivières / Труа-Ривьер', 'Guelph / Гуэлф',
        'Cambridge / Кембридж', 'Whitby / Уитби',
        'Greater Sudbury / Большой Садбери', 'Kingston / Кингстон',
        'Saguenay / Сагенай', 'Thunder Bay / Тандер-Бей',
        'Brantford / Брантфорд', 'Saint John / Сент-Джон',
        'Lethbridge / Летбридж', 'Yellowknife / Йеллоунайф',
        'Whitehorse / Уайтхорс', 'Iqaluit / Икалуит',
        'Charlottetown / Шарлоттаун', 'Fredericton / Фредериктон'],
        sea: ['Vancouver / Ванкувер (CAVAN)', 'Montreal / Монреаль', 'Halifax / Галифакс', 'Prince Rupert / Принс-Руперт'],
        air: ['Toronto / Торонто (YYZ)', 'Vancouver / Ванкувер (YVR)', 'Montreal / Монреаль (YUL)', 'Calgary / Калгари (YYC)'] },
    KE: { city: ['Nairobi / Найроби', 'Mombasa / Момбаса',
        'Kisumu / Кисуму', 'Nakuru / Накуру', 'Eldoret / Элдорет',
        'Ruiru / Руиру', 'Kikuyu / Кикуйю', 'Kangundo / Кангундо',
        'Malindi / Малинди', 'Naivasha / Найваша',
        'Machakos / Мачакос', 'Nyeri / Ньери', 'Kitale / Китале',
        'Thika / Тика', 'Garissa / Гарисса', 'Kakamega / Какамега',
        'Kericho / Керичо', 'Meru / Меру', 'Lamu / Ламу'],
        sea: ['Mombasa / Момбаса (KEMBA)'], air: ['Nairobi / Найроби (NBO)', 'Mombasa / Момбаса (MBA)'] },
    CO: { city: ['Bogotá / Богота', 'Medellín / Медельин', 'Cali / Кали',
        'Barranquilla / Барранкилья', 'Cartagena / Картахена',
        'Cúcuta / Кукута', 'Bucaramanga / Букараманга',
        'Ibagué / Ибаге', 'Soledad / Соледад', 'Pereira / Перейра',
        'Soacha / Соача', 'Santa Marta / Санта-Марта',
        'Villavicencio / Вильявисенсио', 'Bello / Бельо',
        'Valledupar / Вальедупар', 'Pasto / Пасто',
        'Manizales / Манисалес', 'Montería / Монтерия',
        'Buenaventura / Буэнавентура', 'Neiva / Нейва',
        'Armenia / Армения', 'Popayán / Попаян',
        'Sincelejo / Синселехо', 'Palmira / Пальмира',
        'Riohacha / Риоача', 'Tunja / Тунха'],
        sea: ['Cartagena / Картахена', 'Buenaventura / Буэнавентура'], air: ['Bogotá / Богота (BOG)', 'Medellín / Медельин (MDE)', 'Cartagena / Картахена (CTG)'] },
    MY: { city: ['Kuala Lumpur / Куала-Лумпур', 'Johor Bahru / Джохор-Бару',
        'George Town (Penang) / Джорджтаун (Пенанг)', 'Ipoh / Ипох',
        'Kuching / Кучинг', 'Shah Alam / Шах-Алам',
        'Petaling Jaya / Петалинг-Джая', 'Klang / Кланг',
        'Kota Kinabalu / Кота-Кинабалу', 'Kajang / Каджанг',
        'Subang Jaya / Субанг-Джая', 'Seremban / Серембан',
        'Iskandar Puteri / Искандар-Путери', 'Kuantan / Куантан',
        'Sandakan / Сандакан', 'Malacca / Малакка',
        'Sungai Petani / Сунгай-Петани', 'Alor Setar / Алор-Сетар',
        'Miri / Мири', 'Kuala Terengganu / Куала-Теренгану',
        'Putrajaya / Путраджая', 'Sibu / Сибу', 'Tawau / Тавау',
        'Kota Bharu / Кота-Бару', 'Kangar / Кангар',
        'Labuan / Лабуан'],
        sea: ['Port Klang / Порт-Кланг (MYPKG)', 'Tanjung Pelepas / Танджунг-Пелепас (MYTPP)', 'Penang / Пенанг'],
        air: ['Kuala Lumpur / Куала-Лумпур (KUL)', 'Penang / Пенанг (PEN)', 'Johor Bahru / Джохор-Бару (JHB)'] },
    MX: { city: ['Mexico City / Мехико', 'Guadalajara / Гвадалахара',
        'Monterrey / Монтеррей', 'Puebla / Пуэбла', 'Tijuana / Тихуана',
        'León / Леон', 'Juárez / Сьюдад-Хуарес', 'Zapopan / Сапопан',
        'Ecatepec / Экатепек', 'Nezahualcóyotl / Несауалькойотль',
        'Querétaro / Керетаро', 'Chihuahua / Чиуауа',
        'Acapulco / Акапулько', 'Mérida / Мерида',
        'Mexicali / Мехикали', 'Aguascalientes / Агуаскальентес',
        'Tlalnepantla / Тлальнепантла', 'Saltillo / Сальтильо',
        'San Luis Potosí / Сан-Луис-Потоси', 'Hermosillo / Эрмосильо',
        'Culiacán / Кульякан', 'Cuernavaca / Куэрнавака',
        'Veracruz / Веракрус', 'Toluca / Толука', 'Cancún / Канкун',
        'Reynosa / Рейноса', 'Matamoros / Матаморос',
        'Morelia / Морелия', 'Xalapa / Халапа',
        'Tuxtla Gutiérrez / Тустла-Гутьеррес', 'Durango / Дуранго',
        'Mazatlán / Масатлан', 'Oaxaca / Оахака',
        'Pachuca / Пачука', 'Tepic / Тепик', 'Campeche / Кампече',
        'Villahermosa / Вильяэрмоса', 'Chetumal / Четумаль',
        'La Paz / Ла-Пас', 'Tampico / Тампико'],
        sea: ['Manzanillo / Мансанильо (MXZLO)', 'Veracruz / Веракрус (MXVER)', 'Lázaro Cárdenas / Ласаро-Карденас'],
        air: ['Mexico City / Мехико (MEX)', 'Guadalajara / Гвадалахара (GDL)', 'Monterrey / Монтеррей (MTY)', 'Cancún / Канкун (CUN)'] },
    NL: { city: ['Amsterdam / Амстердам', 'Rotterdam / Роттердам',
        'The Hague / Гаага', 'Utrecht / Утрехт',
        'Eindhoven / Эйндховен', 'Tilburg / Тилбург',
        'Groningen / Гронинген', 'Almere / Алмере',
        'Breda / Бреда', 'Nijmegen / Неймеген',
        'Enschede / Энсхеде', 'Haarlem / Харлем',
        'Arnhem / Арнем', 'Zaanstad / Занстад',
        'Amersfoort / Амерсфорт', 'Apeldoorn / Апелдорн',
        'Hoofddorp / Хоофддорп', 'Maastricht / Маастрихт',
        'Leiden / Лейден', 'Dordrecht / Дордрехт',
        'Zoetermeer / Зутермер', 'Zwolle / Зволле',
        'Deventer / Девентер', 'Delft / Делфт',
        'Alkmaar / Алкмар', 'Leeuwarden / Леуварден',
        'Sittard / Ситтард', 'Helmond / Хелмонд',
        'Oss / Осс', 'Roosendaal / Рузендал',
        'Hilversum / Хилверсюм', 'Heerlen / Херлен',
        'Venlo / Венло', 'Emmen / Эммен'],
        sea: ['Rotterdam / Роттердам (NLRTM)', 'Amsterdam / Амстердам (NLAMS)'], air: ['Amsterdam Schiphol / Амстердам Схипхол (AMS)', 'Eindhoven / Эйндховен (EIN)', 'Rotterdam / Роттердам (RTM)'] },
    NO: { city: ['Oslo / Осло', 'Bergen / Берген', 'Trondheim / Тронхейм',
        'Stavanger / Ставангер', 'Drammen / Драммен', 'Tromsø / Тромсё',
        'Fredrikstad / Фредрикстад', 'Sarpsborg / Сарпсборг',
        'Skien / Шиен', 'Kristiansand / Кристиансанн',
        'Ålesund / Олесунн', 'Sandefjord / Саннефьорд',
        'Tønsberg / Тёнсберг', 'Moss / Мосс', 'Bodø / Будё',
        'Arendal / Арендал', 'Larvik / Ларвик', 'Halden / Халден',
        'Hamar / Хамар', 'Lillehammer / Лиллехаммер',
        'Mo i Rana / Му', 'Hammerfest / Хаммерфест',
        'Alta / Алта', 'Kirkenes / Киркенес',
        'Honningsvåg / Хоннингсвог'],
        sea: ['Oslo / Осло', 'Bergen / Берген', 'Stavanger / Ставангер'], air: ['Oslo / Осло (OSL)', 'Bergen / Берген (BGO)', 'Trondheim / Тронхейм (TRD)'] },
    PK: { city: ['Karachi / Карачи', 'Lahore / Лахор',
        'Faisalabad / Фейсалабад', 'Rawalpindi / Равалпинди',
        'Multan / Мултан', 'Islamabad / Исламабад',
        'Peshawar / Пешавар', 'Quetta / Кветта',
        'Sialkot / Сиялкот', 'Bahawalpur / Бахавалпур',
        'Sargodha / Саргодха', 'Sukkur / Суккур',
        'Jhang / Джанг', 'Sheikhupura / Шейхупура',
        'Larkana / Ларкана', 'Gujranwala / Гуджранвала',
        'Kasur / Касур', 'Mardan / Мардан',
        'Rahim Yar Khan / Рахим-Яр-Хан', 'Hyderabad / Хайдарабад',
        'Mirpur Khas / Мирпур-Хас', 'Nawabshah / Навабшах',
        'Mingora / Мингора', 'Okara / Окара', 'Sahiwal / Сахивал',
        'Dera Ghazi Khan / Дера-Гази-Хан', 'Chiniot / Чиниот',
        'Gujrat / Гуджрат', 'Wah / Вах'],
        sea: ['Karachi / Карачи (PKKHI)', 'Port Qasim / Порт-Касим'], air: ['Karachi / Карачи (KHI)', 'Lahore / Лахор (LHE)', 'Islamabad / Исламабад (ISB)'] },
    PL: { city: ['Warsaw / Варшава', 'Kraków / Краков', 'Łódź / Лодзь',
        'Wrocław / Вроцлав', 'Poznań / Познань', 'Gdańsk / Гданьск',
        'Szczecin / Щецин', 'Lublin / Люблин', 'Białystok / Белосток',
        'Katowice / Катовице', 'Gdynia / Гдыня',
        'Częstochowa / Ченстохова', 'Radom / Радом', 'Toruń / Торунь',
        'Sosnowiec / Сосновец', 'Rzeszów / Жешув', 'Kielce / Кельце',
        'Gliwice / Гливице', 'Olsztyn / Ольштын', 'Zabrze / Забже',
        'Bielsko-Biała / Бельско-Бяла', 'Bytom / Бытом',
        'Zielona Góra / Зелёна-Гура', 'Rybnik / Рыбник',
        'Płock / Плоцк', 'Tarnów / Тарнув', 'Elbląg / Эльблонг',
        'Opole / Ополе', 'Wałbrzych / Валбжих', 'Włocławek / Влоцлавек',
        'Tychy / Тыхы', 'Koszalin / Кошалин', 'Kalisz / Калиш',
        'Legnica / Легница', 'Grudziądz / Грудзёндз',
        'Słupsk / Слупск', 'Jaworzno / Явожно',
        'Jastrzębie-Zdrój / Ястшембе-Здруй'],
        sea: ['Gdańsk / Гданьск (PLGDN)', 'Gdynia / Гдыня (PLGDY)', 'Szczecin / Щецин', 'Świnoujście / Свиноуйсьце'],
        air: ['Warsaw / Варшава (WAW)', 'Kraków / Краков (KRK)', 'Gdańsk / Гданьск (GDN)', 'Wrocław / Вроцлав (WRO)'] },
    PT: { city: ['Lisbon / Лиссабон', 'Porto / Порту',
        'Vila Nova de Gaia / Вила-Нова-ди-Гая', 'Amadora / Амадора',
        'Braga / Брага', 'Coimbra / Коимбра', 'Funchal / Фуншал',
        'Setúbal / Сетубал', 'Faro / Фару', 'Aveiro / Авейру',
        'Évora / Эвора', 'Almada / Алмада', 'Cascais / Кашкайш',
        'Sintra / Синтра', 'Loures / Лоуреш', 'Oeiras / Уэйраш',
        'Odivelas / Одивелаш', 'Maia / Майя', 'Matosinhos / Матозиньюш',
        'Guimarães / Гимарайнш', 'Viseu / Визеу', 'Leiria / Лейрия',
        'Barreiro / Баррейру', 'Águeda / Агеда', 'Bragança / Браганса',
        'Castelo Branco / Каштелу-Бранку', 'Viana do Castelo / Виана-ду-Каштелу',
        'Beja / Бежа', 'Portalegre / Порталегри', 'Santarém / Сантарен'],
        sea: ['Lisbon / Лиссабон', 'Porto Leixões / Порту Лейшоэс', 'Sines / Синиш'], air: ['Lisbon / Лиссабон (LIS)', 'Porto / Порту (OPO)', 'Faro / Фару (FAO)'] },
    RO: { city: ['Bucharest / Бухарест', 'Cluj-Napoca / Клуж-Напока',
        'Timișoara / Тимишоара', 'Iași / Яссы', 'Constanța / Констанца',
        'Brașov / Брашов', 'Craiova / Крайова', 'Galați / Галац',
        'Ploiești / Плоешти', 'Oradea / Орадя', 'Brăila / Брэила',
        'Arad / Арад', 'Pitești / Питешти', 'Sibiu / Сибиу',
        'Bacău / Бакэу', 'Târgu Mureș / Тыргу-Муреш',
        'Baia Mare / Бая-Маре', 'Buzău / Бузэу', 'Botoșani / Ботошани',
        'Satu Mare / Сату-Маре', 'Râmnicu Vâlcea / Рымнику-Вылча',
        'Suceava / Сучава', 'Piatra Neamț / Пьятра-Нямц',
        'Drobeta-Turnu Severin / Дробета-Турну-Северин',
        'Târgoviște / Тырговиште', 'Focșani / Фокшани',
        'Bistrița / Бистрица', 'Tulcea / Тулча',
        'Reșița / Решица', 'Călărași / Кэлэраши',
        'Slatina / Слатина', 'Alba Iulia / Алба-Юлия'],
        sea: ['Constanța / Констанца (ROCND)'], air: ['Bucharest / Бухарест (OTP)', 'Cluj-Napoca / Клуж-Напока (CLJ)', 'Timișoara / Тимишоара (TSR)'] },
    SA: { city: ['Riyadh / Эр-Рияд', 'Jeddah / Джидда', 'Mecca / Мекка',
        'Medina / Медина', 'Dammam / Даммам', 'Khobar / Эль-Хубар',
        'Tabuk / Табук', 'Buraidah / Бурайда',
        'Khamis Mushait / Хамис-Мушайт', 'Hail / Хаиль',
        'Najran / Наджран', 'Abha / Абха', 'Yanbu / Янбу',
        'Al-Kharj / Эль-Хардж', 'Taif / Эт-Таиф', 'Jubail / Эль-Джубайль',
        'Al-Hofuf / Эль-Хофуф', 'Hafar Al-Batin / Хафр-эль-Батин',
        'Arar / Арар', 'Jizan / Джизан', 'Sakaka / Сакака',
        'Bisha / Биша'],
        sea: ['Jeddah / Джидда (SAJED)', 'Dammam / Даммам (SADMM)', 'King Abdullah Port / Порт короля Абдаллы'],
        air: ['Riyadh / Эр-Рияд (RUH)', 'Jeddah / Джидда (JED)', 'Dammam / Даммам (DMM)'] },
    SG: { city: ['Singapore / Сингапур'], sea: ['Singapore / Сингапур (SGSIN)'], air: ['Singapore Changi / Сингапур Чанги (SIN)'] },
    SK: { city: ['Bratislava / Братислава', 'Košice / Кошице',
        'Prešov / Прешов', 'Žilina / Жилина', 'Banská Bystrica / Банска-Бистрица',
        'Nitra / Нитра', 'Trnava / Трнава', 'Trenčín / Тренчин',
        'Martin / Мартин', 'Poprad / Попрад', 'Prievidza / Привидза',
        'Zvolen / Зволен', 'Považská Bystrica / Поважска-Бистрица',
        'Michalovce / Михаловце', 'Nové Zámky / Нове-Замки',
        'Spišská Nová Ves / Спишска-Нова-Вес', 'Komárno / Комарно',
        'Levice / Левице', 'Humenné / Гуменне', 'Bardejov / Бардеёв'],
        sea: [], air: ['Bratislava / Братислава (BTS)', 'Košice / Кошице (KSC)'] },
    SI: { city: ['Ljubljana / Любляна', 'Maribor / Марибор', 'Celje / Целе',
        'Koper / Копер', 'Kranj / Крань', 'Velenje / Велене',
        'Novo Mesto / Ново-Место', 'Ptuj / Птуй', 'Trbovlje / Трбовле',
        'Kamnik / Камник', 'Jesenice / Есенице', 'Domžale / Домжале',
        'Škofja Loka / Шкофья-Лока', 'Murska Sobota / Мурска-Собота',
        'Postojna / Постойна', 'Slovenj Gradec / Словень-Градец',
        'Idrija / Идрия', 'Nova Gorica / Нова-Горица'],
        sea: ['Koper / Копер (SIKOP)'], air: ['Ljubljana / Любляна (LJU)', 'Maribor / Марибор (MBX)'] },
    TW: { city: ['Taipei / Тайбэй', 'New Taipei / Новый Тайбэй',
        'Kaohsiung / Гаосюн', 'Taichung / Тайчжун', 'Tainan / Тайнань',
        'Taoyuan / Таоюань', 'Hsinchu / Синьчжу', 'Keelung / Цзилун',
        'Chiayi / Цзяи', 'Changhua / Чжанхуа', 'Yunlin / Юньлинь',
        'Pingtung / Пиндун', 'Yilan / Илань', 'Hualien / Хуалянь',
        'Taitung / Тайдун', 'Miaoli / Мяоли', 'Nantou / Наньтоу',
        'Penghu / Пэнху', 'Kinmen / Цзиньмэнь', 'Lienchiang / Ляньцзян'],
        sea: ['Kaohsiung / Гаосюн (TWKHH)', 'Keelung / Цзилун (TWKEL)', 'Taichung / Тайчжун'], air: ['Taipei Taoyuan / Тайбэй Таоюань (TPE)', 'Kaohsiung / Гаосюн (KHH)'] },
    TH: { city: ['Bangkok / Бангкок', 'Nonthaburi / Нонтхабури',
        'Chiang Mai / Чиангмай', 'Pattaya / Паттайя', 'Phuket / Пхукет',
        'Khon Kaen / Кхонкэн', 'Hat Yai / Хатъяй', 'Udon Thani / Удонтхани',
        'Nakhon Ratchasima / Накхонратчасима', 'Surat Thani / Сураттхани',
        'Chiang Rai / Чианграй', 'Krabi / Краби', 'Rayong / Районг',
        'Ubon Ratchathani / Убонратчатхани', 'Songkhla / Сонгкхла',
        'Pak Kret / Паккрет', 'Si Racha / Сирача', 'Phra Pradaeng / Прапрадэнг',
        'Phitsanulok / Питсанулок', 'Sakon Nakhon / Саконнакхон',
        'Lampang / Лампанг', 'Lamphun / Лампхун', 'Trang / Транг',
        'Nakhon Sawan / Накхонсаван', 'Loei / Лей', 'Chumphon / Чумпхон'],
        sea: ['Laem Chabang / Лаем-Чабанг (THLCH)', 'Bangkok / Бангкок (THBKK)'], air: ['Bangkok Suvarnabhumi / Бангкок Суварнабхуми (BKK)', 'Bangkok Don Mueang / Бангкок Дон-Мыанг (DMK)', 'Phuket / Пхукет (HKT)'] },
    UZ: { city: ['Tashkent / Ташкент', 'Samarkand / Самарканд',
        'Bukhara / Бухара', 'Andijan / Андижан', 'Namangan / Наманган',
        'Fergana / Фергана', 'Nukus / Нукус', 'Karshi / Карши',
        'Margilan / Маргилан', 'Kokand / Коканд', 'Termez / Термез',
        'Urgench / Ургенч', 'Jizzakh / Джизак', 'Navoiy / Навои',
        'Chirchiq / Чирчик', 'Olmaliq / Алмалык', 'Angren / Ангрен',
        'Bekabad / Бекабад', 'Khiva / Хива', 'Shahrisabz / Шахрисабз'],
        sea: [], air: ['Tashkent / Ташкент (TAS)', 'Samarkand / Самарканд (SKD)', 'Bukhara / Бухара (BHK)'] },
    UA: { city: ['Kyiv / Киев', 'Kharkiv / Харьков', 'Odesa / Одесса',
        'Dnipro / Днипро', 'Lviv / Львов', 'Zaporizhzhia / Запорожье',
        'Kryvyi Rih / Кривой Рог', 'Mykolaiv / Николаев',
        'Mariupol / Мариуполь', 'Luhansk / Луганск',
        'Vinnytsia / Винница', 'Makiivka / Макеевка',
        'Sevastopol / Севастополь', 'Simferopol / Симферополь',
        'Kherson / Херсон', 'Poltava / Полтава',
        'Chernihiv / Чернигов', 'Cherkasy / Черкассы',
        'Khmelnytskyi / Хмельницкий', 'Chernivtsi / Черновцы',
        'Sumy / Сумы', 'Zhytomyr / Житомир', 'Rivne / Ровно',
        'Ivano-Frankivsk / Ивано-Франковск', 'Ternopil / Тернополь',
        'Lutsk / Луцк', 'Kropyvnytskyi / Кропивницкий',
        'Uzhhorod / Ужгород', 'Kamianets-Podilskyi / Каменец-Подольский',
        'Bila Tserkva / Белая Церковь', 'Berdiansk / Бердянск'],
        sea: ['Odesa / Одесса (UAODS)', 'Chornomorsk / Черноморск', 'Yuzhny / Южный'], air: ['Kyiv Boryspil / Киев Борисполь (KBP)', 'Lviv / Львов (LWO)', 'Odesa / Одесса (ODS)'] },
    FI: { city: ['Helsinki / Хельсинки', 'Espoo / Эспоо', 'Tampere / Тампере',
        'Vantaa / Вантаа', 'Turku / Турку', 'Oulu / Оулу',
        'Lahti / Лахти', 'Kuopio / Куопио', 'Jyväskylä / Ювяскюля',
        'Pori / Пори', 'Lappeenranta / Лаппеэнранта', 'Kotka / Котка',
        'Joensuu / Йоэнсуу', 'Hämeenlinna / Хямеэнлинна',
        'Vaasa / Вааса', 'Seinäjoki / Сейняйоки', 'Rovaniemi / Рованиеми',
        'Mikkeli / Миккели', 'Kokkola / Коккола', 'Salo / Сало',
        'Porvoo / Порвоо', 'Hyvinkää / Хювинкяа',
        'Lohja / Лохья', 'Järvenpää / Ярвенпяа',
        'Kerava / Керава', 'Tornio / Торнио', 'Kemi / Кеми',
        'Kajaani / Каяани', 'Imatra / Иматра'],
        sea: ['Helsinki / Хельсинки (FIHEL)', 'Kotka / Котка', 'Hamina / Хамина'], air: ['Helsinki / Хельсинки (HEL)', 'Tampere / Тампере (TMP)', 'Turku / Турку (TKU)'] },
    FR: { city: ['Paris / Париж', 'Marseille / Марсель', 'Lyon / Лион',
        'Toulouse / Тулуза', 'Nice / Ницца', 'Bordeaux / Бордо',
        'Lille / Лилль', 'Strasbourg / Страсбург', 'Nantes / Нант',
        'Rennes / Ренн', 'Reims / Реймс', 'Saint-Étienne / Сент-Этьен',
        'Toulon / Тулон', 'Le Havre / Гавр', 'Grenoble / Гренобль',
        'Dijon / Дижон', 'Angers / Анже', 'Nîmes / Ним',
        'Villeurbanne / Виллёрбан', 'Saint-Denis / Сен-Дени',
        'Le Mans / Ле-Ман', 'Aix-en-Provence / Экс-ан-Прованс',
        'Clermont-Ferrand / Клермон-Ферран', 'Brest / Брест',
        'Limoges / Лимож', 'Tours / Тур', 'Amiens / Амьен',
        'Perpignan / Перпиньян', 'Metz / Мец', 'Besançon / Безансон',
        'Orléans / Орлеан', 'Mulhouse / Мюлуз', 'Rouen / Руан',
        'Caen / Кан', 'Nancy / Нанси', 'Saint-Nazaire / Сен-Назер',
        'Avignon / Авиньон', 'Poitiers / Пуатье', 'Calais / Кале',
        'Dunkerque / Дюнкерк', 'La Rochelle / Ла-Рошель',
        'Cannes / Канны', 'Antibes / Антиб', 'Annecy / Анси',
        'Pau / По', 'Lorient / Лорьян', 'Versailles / Версаль',
        'Chambéry / Шамбери', 'Beauvais / Бове', 'Albi / Альби'],
        sea: ['Le Havre / Гавр (FRLEH)', 'Marseille / Марсель (FRMRS)', 'Dunkerque / Дюнкерк'],
        air: ['Paris CDG / Париж Шарль-де-Голль (CDG)', 'Paris Orly / Париж Орли (ORY)', 'Lyon / Лион (LYS)', 'Marseille / Марсель (MRS)', 'Nice / Ницца (NCE)'] },
    HR: { city: ['Zagreb / Загреб', 'Split / Сплит', 'Rijeka / Риека',
        'Osijek / Осиек', 'Zadar / Задар', 'Slavonski Brod / Славонски-Брод',
        'Pula / Пула', 'Karlovac / Карловац', 'Sisak / Сисак',
        'Varaždin / Вараждин', 'Šibenik / Шибеник',
        'Dubrovnik / Дубровник', 'Bjelovar / Беловар',
        'Velika Gorica / Велика-Горица', 'Vinkovci / Винковци',
        'Vukovar / Вуковар', 'Samobor / Самобор',
        'Koprivnica / Копривница', 'Đakovo / Джяково',
        'Požega / Пожега', 'Krapina / Крапина', 'Makarska / Макарска'],
        sea: ['Rijeka / Риека (HRRJK)', 'Ploče / Плоче', 'Split / Сплит'], air: ['Zagreb / Загреб (ZAG)', 'Split / Сплит (SPU)', 'Dubrovnik / Дубровник (DBV)'] },
    CZ: { city: ['Prague / Прага', 'Brno / Брно', 'Ostrava / Острава',
        'Plzeň / Пльзень', 'Liberec / Либерец', 'Olomouc / Оломоуц',
        'Ústí nad Labem / Усти-над-Лабем', 'Hradec Králové / Градец-Кралове',
        'Pardubice / Пардубице', 'Zlín / Злин', 'Havířov / Гавиржов',
        'Kladno / Кладно', 'Most / Мост', 'Karviná / Карвина',
        'Opava / Опава', 'Frýdek-Místek / Фридек-Мистек',
        'České Budějovice / Ческе-Будеёвице', 'Karlovy Vary / Карловы Вары',
        'Jihlava / Йиглава', 'Děčín / Дечин', 'Teplice / Теплице',
        'Chomutov / Хомутов', 'Třebíč / Тршебич', 'Tábor / Табор',
        'Znojmo / Зноймо', 'Příbram / Пршибрам',
        'Mladá Boleslav / Млада-Болеслав'],
        sea: [], air: ['Prague / Прага (PRG)', 'Brno / Брно (BRQ)', 'Ostrava / Острава (OSR)'] },
    CL: { city: ['Santiago / Сантьяго', 'Valparaíso / Вальпараисо',
        'Concepción / Консепсьон', 'La Serena / Ла-Серена',
        'Antofagasta / Антофагаста', 'Temuco / Темуко',
        'Rancagua / Ранкагуа', 'Talca / Талька', 'Iquique / Икике',
        'Arica / Арика', 'Puerto Montt / Пуэрто-Монт',
        'Coquimbo / Кокимбо', 'Osorno / Осорно', 'Calama / Калама',
        'Copiapó / Копьяпо', 'Punta Arenas / Пунта-Аренас',
        'Quillota / Кильота', 'Curicó / Курико', 'Valdivia / Вальдивия',
        'Chillán / Чильян', 'Linares / Линарес',
        'Los Ángeles / Лос-Анхелес', 'Castro / Кастро',
        'Coyhaique / Койайке'],
        sea: ['Valparaíso / Вальпараисо', 'San Antonio / Сан-Антонио', 'Iquique / Икике'], air: ['Santiago / Сантьяго (SCL)', 'Concepción / Консепсьон (CCP)'] },
    CH: { city: ['Zurich / Цюрих', 'Geneva / Женева', 'Basel / Базель',
        'Bern / Берн', 'Lausanne / Лозанна', 'Winterthur / Винтертур',
        'St. Gallen / Санкт-Галлен', 'Lucerne / Люцерн', 'Lugano / Лугано',
        'Biel/Bienne / Биль', 'Thun / Тун', 'Köniz / Кёниц',
        'La Chaux-de-Fonds / Ла-Шо-де-Фон', 'Schaffhausen / Шаффхаузен',
        'Fribourg / Фрибур', 'Chur / Кур', 'Neuchâtel / Невшатель',
        'Vernier / Вернье', 'Uster / Устер', 'Sion / Сьон',
        'Davos / Давос', 'Locarno / Локарно', 'Bellinzona / Беллинцона',
        'Solothurn / Золотурн', 'Zug / Цуг', 'Aarau / Аарау',
        'Interlaken / Интерлакен'],
        sea: [], air: ['Zurich / Цюрих (ZRH)', 'Geneva / Женева (GVA)', 'Basel / Базель (BSL)'] },
    SE: { city: ['Stockholm / Стокгольм', 'Gothenburg / Гётеборг',
        'Malmö / Мальмё', 'Uppsala / Уппсала', 'Linköping / Линчёпинг',
        'Västerås / Вестерос', 'Örebro / Эребру', 'Norrköping / Норрчёпинг',
        'Helsingborg / Хельсингборг', 'Jönköping / Йёнчёпинг',
        'Umeå / Умео', 'Lund / Лунд', 'Borås / Бурос',
        'Sundsvall / Сундсвалль', 'Gävle / Евле',
        'Eskilstuna / Эскильстуна', 'Halmstad / Хальмстад',
        'Karlstad / Карлстад', 'Växjö / Векшё', 'Södertälje / Сёдертелье',
        'Skellefteå / Шеллефтео', 'Kalmar / Кальмар',
        'Karlskrona / Карлскруна', 'Östersund / Эстерсунд',
        'Trollhättan / Тролльхеттан', 'Kiruna / Кируна',
        'Luleå / Лулео', 'Visby / Висбю'],
        sea: ['Gothenburg / Гётеборг (SEGOT)', 'Stockholm / Стокгольм', 'Helsingborg / Хельсингборг'], air: ['Stockholm Arlanda / Стокгольм Арланда (ARN)', 'Gothenburg / Гётеборг (GOT)', 'Malmö / Мальмё (MMX)'] },
    ZA: { city: ['Johannesburg / Йоханнесбург', 'Cape Town / Кейптаун',
        'Durban / Дурбан', 'Pretoria / Претория',
        'Port Elizabeth / Порт-Элизабет', 'Bloemfontein / Блумфонтейн',
        'East London / Ист-Лондон', 'Pietermaritzburg / Питермарицбург',
        'Witbank / Витбанк', 'Welkom / Велком', 'Rustenburg / Растенбург',
        'Soweto / Соуэто', 'Vereeniging / Феринихинг',
        'Kimberley / Кимберли', 'Polokwane / Полокване',
        'Nelspruit / Нелспрут', 'George / Джордж', 'Newcastle / Ньюкасл',
        'Pinetown / Пайнтаун', 'Krugersdorp / Крюгерсдорп',
        'Mafikeng / Мафикенг', 'Upington / Апингтон',
        'Stellenbosch / Стелленбос', 'Knysna / Книсна',
        'Mossel Bay / Мосселбай'],
        sea: ['Durban / Дурбан (ZADUR)', 'Cape Town / Кейптаун (ZACPT)', 'Port Elizabeth / Порт-Элизабет', 'Ngqura / Нгкура'],
        air: ['Johannesburg / Йоханнесбург (JNB)', 'Cape Town / Кейптаун (CPT)', 'Durban / Дурбан (DUR)'] },
  };
  // Топ-10 стран — основные хабы B2B-запчастей. Показываются первыми.
  const COUNTRIES_TOP = [
    { code: 'CN', flag: '🇨🇳', name: 'Китай' },
    { code: 'RU', flag: '🇷🇺', name: 'Россия' },
    { code: 'AE', flag: '🇦🇪', name: 'ОАЭ' },
    { code: 'TR', flag: '🇹🇷', name: 'Турция' },
    { code: 'DE', flag: '🇩🇪', name: 'Германия' },
    { code: 'KR', flag: '🇰🇷', name: 'Южная Корея' },
    { code: 'JP', flag: '🇯🇵', name: 'Япония' },
    { code: 'IN', flag: '🇮🇳', name: 'Индия' },
    { code: 'US', flag: '🇺🇸', name: 'США' },
    { code: 'IT', flag: '🇮🇹', name: 'Италия' },
  ];
  // «Все остальные» — полный список стран мира (ISO2), у которых есть
  // города в GeoNames-базе. Алфавитный порядок по русскому названию.
  const COUNTRIES_ALL = [
    { code: 'AU', flag: '🇦🇺', name: 'Австралия' },
    { code: 'AT', flag: '🇦🇹', name: 'Австрия' },
    { code: 'AZ', flag: '🇦🇿', name: 'Азербайджан' },
    { code: 'AX', flag: '🇦🇽', name: 'Аландские о-ва' },
    { code: 'AL', flag: '🇦🇱', name: 'Албания' },
    { code: 'DZ', flag: '🇩🇿', name: 'Алжир' },
    { code: 'AS', flag: '🇦🇸', name: 'Американское Самоа' },
    { code: 'AI', flag: '🇦🇮', name: 'Ангилья' },
    { code: 'AO', flag: '🇦🇴', name: 'Ангола' },
    { code: 'AD', flag: '🇦🇩', name: 'Андорра' },
    { code: 'AG', flag: '🇦🇬', name: 'Антигуа и Барбуда' },
    { code: 'AR', flag: '🇦🇷', name: 'Аргентина' },
    { code: 'AM', flag: '🇦🇲', name: 'Армения' },
    { code: 'AW', flag: '🇦🇼', name: 'Аруба' },
    { code: 'AF', flag: '🇦🇫', name: 'Афганистан' },
    { code: 'BS', flag: '🇧🇸', name: 'Багамы' },
    { code: 'BD', flag: '🇧🇩', name: 'Бангладеш' },
    { code: 'BB', flag: '🇧🇧', name: 'Барбадос' },
    { code: 'BH', flag: '🇧🇭', name: 'Бахрейн' },
    { code: 'BY', flag: '🇧🇾', name: 'Беларусь' },
    { code: 'BZ', flag: '🇧🇿', name: 'Белиз' },
    { code: 'BE', flag: '🇧🇪', name: 'Бельгия' },
    { code: 'BJ', flag: '🇧🇯', name: 'Бенин' },
    { code: 'BM', flag: '🇧🇲', name: 'Бермудские о-ва' },
    { code: 'BG', flag: '🇧🇬', name: 'Болгария' },
    { code: 'BO', flag: '🇧🇴', name: 'Боливия' },
    { code: 'BQ', flag: '🇧🇶', name: 'Бонэйр, Синт-Эстатиус и Саба' },
    { code: 'BA', flag: '🇧🇦', name: 'Босния и Герцеговина' },
    { code: 'BW', flag: '🇧🇼', name: 'Ботсвана' },
    { code: 'BR', flag: '🇧🇷', name: 'Бразилия' },
    { code: 'BN', flag: '🇧🇳', name: 'Бруней-Даруссалам' },
    { code: 'BF', flag: '🇧🇫', name: 'Буркина-Фасо' },
    { code: 'BI', flag: '🇧🇮', name: 'Бурунди' },
    { code: 'BT', flag: '🇧🇹', name: 'Бутан' },
    { code: 'VU', flag: '🇻🇺', name: 'Вануату' },
    { code: 'VA', flag: '🇻🇦', name: 'Ватикан' },
    { code: 'GB', flag: '🇬🇧', name: 'Великобритания' },
    { code: 'HU', flag: '🇭🇺', name: 'Венгрия' },
    { code: 'VE', flag: '🇻🇪', name: 'Венесуэла' },
    { code: 'VG', flag: '🇻🇬', name: 'Виргинские о-ва (Великобритания)' },
    { code: 'VI', flag: '🇻🇮', name: 'Виргинские о-ва (США)' },
    { code: 'TL', flag: '🇹🇱', name: 'Восточный Тимор' },
    { code: 'VN', flag: '🇻🇳', name: 'Вьетнам' },
    { code: 'GA', flag: '🇬🇦', name: 'Габон' },
    { code: 'HT', flag: '🇭🇹', name: 'Гаити' },
    { code: 'GY', flag: '🇬🇾', name: 'Гайана' },
    { code: 'GM', flag: '🇬🇲', name: 'Гамбия' },
    { code: 'GH', flag: '🇬🇭', name: 'Гана' },
    { code: 'GP', flag: '🇬🇵', name: 'Гваделупа' },
    { code: 'GT', flag: '🇬🇹', name: 'Гватемала' },
    { code: 'GN', flag: '🇬🇳', name: 'Гвинея' },
    { code: 'GW', flag: '🇬🇼', name: 'Гвинея-Бисау' },
    { code: 'GG', flag: '🇬🇬', name: 'Гернси' },
    { code: 'GI', flag: '🇬🇮', name: 'Гибралтар' },
    { code: 'HN', flag: '🇭🇳', name: 'Гондурас' },
    { code: 'HK', flag: '🇭🇰', name: 'Гонконг (САР)' },
    { code: 'GD', flag: '🇬🇩', name: 'Гренада' },
    { code: 'GL', flag: '🇬🇱', name: 'Гренландия' },
    { code: 'GR', flag: '🇬🇷', name: 'Греция' },
    { code: 'GE', flag: '🇬🇪', name: 'Грузия' },
    { code: 'GU', flag: '🇬🇺', name: 'Гуам' },
    { code: 'DK', flag: '🇩🇰', name: 'Дания' },
    { code: 'JE', flag: '🇯🇪', name: 'Джерси' },
    { code: 'DJ', flag: '🇩🇯', name: 'Джибути' },
    { code: 'DM', flag: '🇩🇲', name: 'Доминика' },
    { code: 'DO', flag: '🇩🇴', name: 'Доминиканская Республика' },
    { code: 'EG', flag: '🇪🇬', name: 'Египет' },
    { code: 'ZM', flag: '🇿🇲', name: 'Замбия' },
    { code: 'EH', flag: '🇪🇭', name: 'Западная Сахара' },
    { code: 'ZW', flag: '🇿🇼', name: 'Зимбабве' },
    { code: 'IL', flag: '🇮🇱', name: 'Израиль' },
    { code: 'ID', flag: '🇮🇩', name: 'Индонезия' },
    { code: 'JO', flag: '🇯🇴', name: 'Иордания' },
    { code: 'IQ', flag: '🇮🇶', name: 'Ирак' },
    { code: 'IR', flag: '🇮🇷', name: 'Иран' },
    { code: 'IE', flag: '🇮🇪', name: 'Ирландия' },
    { code: 'IS', flag: '🇮🇸', name: 'Исландия' },
    { code: 'ES', flag: '🇪🇸', name: 'Испания' },
    { code: 'YE', flag: '🇾🇪', name: 'Йемен' },
    { code: 'KP', flag: '🇰🇵', name: 'КНДР' },
    { code: 'CV', flag: '🇨🇻', name: 'Кабо-Верде' },
    { code: 'KZ', flag: '🇰🇿', name: 'Казахстан' },
    { code: 'KH', flag: '🇰🇭', name: 'Камбоджа' },
    { code: 'CM', flag: '🇨🇲', name: 'Камерун' },
    { code: 'CA', flag: '🇨🇦', name: 'Канада' },
    { code: 'QA', flag: '🇶🇦', name: 'Катар' },
    { code: 'KE', flag: '🇰🇪', name: 'Кения' },
    { code: 'CY', flag: '🇨🇾', name: 'Кипр' },
    { code: 'KG', flag: '🇰🇬', name: 'Киргизия' },
    { code: 'KI', flag: '🇰🇮', name: 'Кирибати' },
    { code: 'CC', flag: '🇨🇨', name: 'Кокосовые о-ва' },
    { code: 'CO', flag: '🇨🇴', name: 'Колумбия' },
    { code: 'KM', flag: '🇰🇲', name: 'Коморы' },
    { code: 'CG', flag: '🇨🇬', name: 'Конго - Браззавиль' },
    { code: 'CD', flag: '🇨🇩', name: 'Конго - Киншаса' },
    { code: 'CR', flag: '🇨🇷', name: 'Коста-Рика' },
    { code: 'CI', flag: '🇨🇮', name: 'Кот-д’Ивуар' },
    { code: 'CU', flag: '🇨🇺', name: 'Куба' },
    { code: 'KW', flag: '🇰🇼', name: 'Кувейт' },
    { code: 'CW', flag: '🇨🇼', name: 'Кюрасао' },
    { code: 'LA', flag: '🇱🇦', name: 'Лаос' },
    { code: 'LV', flag: '🇱🇻', name: 'Латвия' },
    { code: 'LS', flag: '🇱🇸', name: 'Лесото' },
    { code: 'LR', flag: '🇱🇷', name: 'Либерия' },
    { code: 'LB', flag: '🇱🇧', name: 'Ливан' },
    { code: 'LY', flag: '🇱🇾', name: 'Ливия' },
    { code: 'LT', flag: '🇱🇹', name: 'Литва' },
    { code: 'LI', flag: '🇱🇮', name: 'Лихтенштейн' },
    { code: 'LU', flag: '🇱🇺', name: 'Люксембург' },
    { code: 'MU', flag: '🇲🇺', name: 'Маврикий' },
    { code: 'MR', flag: '🇲🇷', name: 'Мавритания' },
    { code: 'MG', flag: '🇲🇬', name: 'Мадагаскар' },
    { code: 'YT', flag: '🇾🇹', name: 'Майотта' },
    { code: 'MO', flag: '🇲🇴', name: 'Макао (САР)' },
    { code: 'MW', flag: '🇲🇼', name: 'Малави' },
    { code: 'MY', flag: '🇲🇾', name: 'Малайзия' },
    { code: 'ML', flag: '🇲🇱', name: 'Мали' },
    { code: 'MV', flag: '🇲🇻', name: 'Мальдивы' },
    { code: 'MT', flag: '🇲🇹', name: 'Мальта' },
    { code: 'MA', flag: '🇲🇦', name: 'Марокко' },
    { code: 'MQ', flag: '🇲🇶', name: 'Мартиника' },
    { code: 'MH', flag: '🇲🇭', name: 'Маршалловы Острова' },
    { code: 'MX', flag: '🇲🇽', name: 'Мексика' },
    { code: 'MZ', flag: '🇲🇿', name: 'Мозамбик' },
    { code: 'MD', flag: '🇲🇩', name: 'Молдова' },
    { code: 'MC', flag: '🇲🇨', name: 'Монако' },
    { code: 'MN', flag: '🇲🇳', name: 'Монголия' },
    { code: 'MS', flag: '🇲🇸', name: 'Монтсеррат' },
    { code: 'MM', flag: '🇲🇲', name: 'Мьянма (Бирма)' },
    { code: 'NA', flag: '🇳🇦', name: 'Намибия' },
    { code: 'NR', flag: '🇳🇷', name: 'Науру' },
    { code: 'NP', flag: '🇳🇵', name: 'Непал' },
    { code: 'NE', flag: '🇳🇪', name: 'Нигер' },
    { code: 'NG', flag: '🇳🇬', name: 'Нигерия' },
    { code: 'NL', flag: '🇳🇱', name: 'Нидерланды' },
    { code: 'NI', flag: '🇳🇮', name: 'Никарагуа' },
    { code: 'NU', flag: '🇳🇺', name: 'Ниуэ' },
    { code: 'NZ', flag: '🇳🇿', name: 'Новая Зеландия' },
    { code: 'NC', flag: '🇳🇨', name: 'Новая Каледония' },
    { code: 'NO', flag: '🇳🇴', name: 'Норвегия' },
    { code: 'OM', flag: '🇴🇲', name: 'Оман' },
    { code: 'KY', flag: '🇰🇾', name: 'Острова Кайман' },
    { code: 'CK', flag: '🇨🇰', name: 'Острова Кука' },
    { code: 'PK', flag: '🇵🇰', name: 'Пакистан' },
    { code: 'PW', flag: '🇵🇼', name: 'Палау' },
    { code: 'PS', flag: '🇵🇸', name: 'Палестинские территории' },
    { code: 'PA', flag: '🇵🇦', name: 'Панама' },
    { code: 'PG', flag: '🇵🇬', name: 'Папуа — Новая Гвинея' },
    { code: 'PY', flag: '🇵🇾', name: 'Парагвай' },
    { code: 'PE', flag: '🇵🇪', name: 'Перу' },
    { code: 'PL', flag: '🇵🇱', name: 'Польша' },
    { code: 'PT', flag: '🇵🇹', name: 'Португалия' },
    { code: 'PR', flag: '🇵🇷', name: 'Пуэрто-Рико' },
    { code: 'RE', flag: '🇷🇪', name: 'Реюньон' },
    { code: 'RW', flag: '🇷🇼', name: 'Руанда' },
    { code: 'RO', flag: '🇷🇴', name: 'Румыния' },
    { code: 'SV', flag: '🇸🇻', name: 'Сальвадор' },
    { code: 'WS', flag: '🇼🇸', name: 'Самоа' },
    { code: 'SM', flag: '🇸🇲', name: 'Сан-Марино' },
    { code: 'ST', flag: '🇸🇹', name: 'Сан-Томе и Принсипи' },
    { code: 'SA', flag: '🇸🇦', name: 'Саудовская Аравия' },
    { code: 'MK', flag: '🇲🇰', name: 'Северная Македония' },
    { code: 'MP', flag: '🇲🇵', name: 'Северные Марианские о-ва' },
    { code: 'SC', flag: '🇸🇨', name: 'Сейшельские Острова' },
    { code: 'BL', flag: '🇧🇱', name: 'Сен-Бартелеми' },
    { code: 'MF', flag: '🇲🇫', name: 'Сен-Мартен' },
    { code: 'PM', flag: '🇵🇲', name: 'Сен-Пьер и Микелон' },
    { code: 'SN', flag: '🇸🇳', name: 'Сенегал' },
    { code: 'VC', flag: '🇻🇨', name: 'Сент-Винсент и Гренадины' },
    { code: 'KN', flag: '🇰🇳', name: 'Сент-Китс и Невис' },
    { code: 'LC', flag: '🇱🇨', name: 'Сент-Люсия' },
    { code: 'RS', flag: '🇷🇸', name: 'Сербия' },
    { code: 'SG', flag: '🇸🇬', name: 'Сингапур' },
    { code: 'SX', flag: '🇸🇽', name: 'Синт-Мартен' },
    { code: 'SY', flag: '🇸🇾', name: 'Сирия' },
    { code: 'SK', flag: '🇸🇰', name: 'Словакия' },
    { code: 'SI', flag: '🇸🇮', name: 'Словения' },
    { code: 'SB', flag: '🇸🇧', name: 'Соломоновы Острова' },
    { code: 'SO', flag: '🇸🇴', name: 'Сомали' },
    { code: 'SD', flag: '🇸🇩', name: 'Судан' },
    { code: 'SR', flag: '🇸🇷', name: 'Суринам' },
    { code: 'SL', flag: '🇸🇱', name: 'Сьерра-Леоне' },
    { code: 'TJ', flag: '🇹🇯', name: 'Таджикистан' },
    { code: 'TH', flag: '🇹🇭', name: 'Таиланд' },
    { code: 'TW', flag: '🇹🇼', name: 'Тайвань' },
    { code: 'TZ', flag: '🇹🇿', name: 'Танзания' },
    { code: 'TG', flag: '🇹🇬', name: 'Того' },
    { code: 'TO', flag: '🇹🇴', name: 'Тонга' },
    { code: 'TT', flag: '🇹🇹', name: 'Тринидад и Тобаго' },
    { code: 'TV', flag: '🇹🇻', name: 'Тувалу' },
    { code: 'TN', flag: '🇹🇳', name: 'Тунис' },
    { code: 'TM', flag: '🇹🇲', name: 'Туркменистан' },
    { code: 'UG', flag: '🇺🇬', name: 'Уганда' },
    { code: 'UZ', flag: '🇺🇿', name: 'Узбекистан' },
    { code: 'UA', flag: '🇺🇦', name: 'Украина' },
    { code: 'WF', flag: '🇼🇫', name: 'Уоллис и Футуна' },
    { code: 'UY', flag: '🇺🇾', name: 'Уругвай' },
    { code: 'FO', flag: '🇫🇴', name: 'Фарерские о-ва' },
    { code: 'FM', flag: '🇫🇲', name: 'Федеративные Штаты Микронезии' },
    { code: 'FJ', flag: '🇫🇯', name: 'Фиджи' },
    { code: 'PH', flag: '🇵🇭', name: 'Филиппины' },
    { code: 'FI', flag: '🇫🇮', name: 'Финляндия' },
    { code: 'FK', flag: '🇫🇰', name: 'Фолклендские о-ва' },
    { code: 'FR', flag: '🇫🇷', name: 'Франция' },
    { code: 'GF', flag: '🇬🇫', name: 'Французская Гвиана' },
    { code: 'PF', flag: '🇵🇫', name: 'Французская Полинезия' },
    { code: 'TF', flag: '🇹🇫', name: 'Французские Южные территории' },
    { code: 'HR', flag: '🇭🇷', name: 'Хорватия' },
    { code: 'CF', flag: '🇨🇫', name: 'Центрально-Африканская Республика' },
    { code: 'TD', flag: '🇹🇩', name: 'Чад' },
    { code: 'ME', flag: '🇲🇪', name: 'Черногория' },
    { code: 'CZ', flag: '🇨🇿', name: 'Чехия' },
    { code: 'CL', flag: '🇨🇱', name: 'Чили' },
    { code: 'CH', flag: '🇨🇭', name: 'Швейцария' },
    { code: 'SE', flag: '🇸🇪', name: 'Швеция' },
    { code: 'SJ', flag: '🇸🇯', name: 'Шпицберген и Ян-Майен' },
    { code: 'LK', flag: '🇱🇰', name: 'Шри-Ланка' },
    { code: 'EC', flag: '🇪🇨', name: 'Эквадор' },
    { code: 'GQ', flag: '🇬🇶', name: 'Экваториальная Гвинея' },
    { code: 'ER', flag: '🇪🇷', name: 'Эритрея' },
    { code: 'SZ', flag: '🇸🇿', name: 'Эсватини' },
    { code: 'EE', flag: '🇪🇪', name: 'Эстония' },
    { code: 'ET', flag: '🇪🇹', name: 'Эфиопия' },
    { code: 'GS', flag: '🇬🇸', name: 'Южная Георгия и Южные Сандвичевы о-ва' },
    { code: 'ZA', flag: '🇿🇦', name: 'Южно-Африканская Республика' },
    { code: 'SS', flag: '🇸🇸', name: 'Южный Судан' },
    { code: 'JM', flag: '🇯🇲', name: 'Ямайка' },
    { code: 'IM', flag: '🇮🇲', name: 'о-в Мэн' },
    { code: 'NF', flag: '🇳🇫', name: 'о-в Норфолк' },
    { code: 'CX', flag: '🇨🇽', name: 'о-в Рождества' },
    { code: 'SH', flag: '🇸🇭', name: 'о-в Св. Елены' },
    { code: 'PN', flag: '🇵🇳', name: 'о-ва Питкэрн' },
    { code: 'TC', flag: '🇹🇨', name: 'о-ва Тёркс и Кайкос' },
  ];
  let __suggDatalistCounter = 0;

  // Local i18n shorthand. window.t (из i18n.js) переводит ключ под текущим
  // языком; если i18n.js ещё не загружен — возвращаем fallback (русский).
  // Назван `tr`, чтобы не конфликтовать с локальными `const t = ...` внутри функций.
  const tr = (key, fallback) => (typeof window.t === 'function' ? window.t(key) : (fallback != null ? fallback : key));

  let state = {
    convId: null,
    ws: null,
    wsRetry: 0,
    streaming: false,
    currentBubble: null,
    _wsPending: null,   // in-flight текст при WS-отправке (для recovery при обрыве)
    config: null,
    convs: [],
    _lastCards: [],
    _lastActions: [],
    _intent: 'default',
  };

  // Persist active conversation id across page reloads so we don't spawn
  // a fresh "Без названия" chat every time the user refreshes.
  function setConvId(id) {
    state.convId = id || null;
    try {
      // sessionStorage: conversation_id — это идентификатор активной
      // сессии переписки, не должен переживать закрытие вкладки. Раньше
      // localStorage означал что после logout другой юзер мог в той же
      // вкладке унаследовать чужой conv_id (architectural leak).
      if (id) sessionStorage.setItem(CONV_KEY, id);
      else sessionStorage.removeItem(CONV_KEY);
    } catch(e){}
  }
  function getStoredConvId() {
    try { return sessionStorage.getItem(CONV_KEY); } catch(e) { return null; }
  }

  // ── Helpers ──────────────────────────────────────────────
  const $ = id => document.getElementById(id);
  const csrf = () => document.cookie.replace(/(?:(?:^|.*;\s*)csrftoken\s*=\s*([^;]*).*$)|^.*$/, '$1');
  const _escRaw = s => (s == null ? '' : String(s)).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  // i18n: переводим значение через словарь (ключ = русская строка). window.t()
  // возвращает оригинал, если перевода нет → данные, бэкенд-строки (уже на языке)
  // и HTML-разметка не страдают; переводятся только известные UI-литералы.
  const esc = s => _escRaw(window.t ? window.t(s == null ? '' : String(s)) : s);
  // Выбран ли русский интерфейс (выставляется и сервером в <html lang>, и setLanguage).
  const _isRuUI = () => (document.documentElement.getAttribute('lang') || 'ru').toLowerCase().slice(0, 2) === 'ru';
  // Имя позиции для колонки NAME. Только при ru-локали И наличии name_ru показываем
  // русское сверху + оригинал мелким снизу. Иначе — оригинал как есть.
  function specNameHtml(it) {
    const orig = esc(it.name || '');
    const ru = it.name_ru ? esc(it.name_ru) : '';
    if (ru && _isRuUI()) {
      return '<span class="spec-name">' + ru + '</span>'
        + '<span class="spec-name-oem" style="display:block;font-size:11px;color:#8a8a8a;font-family:ui-monospace,monospace;margin-top:1px;">' + orig + '</span>';
    }
    return '<span class="spec-name">' + orig + '</span>';
  }
  const fmtMoney = (v, c='USD') => {
    if (!v && v !== 0) return '—';
    const sym = {USD:'$', EUR:'€', RUB:'₽', CNY:'¥'}[c] || '';
    return sym + Number(v).toLocaleString('en-US', {maximumFractionDigits:0});
  };

  // Read-only действия (дашборды/очереди/списки/детали/аналитика) — их безопасно
  // авто-ретраить при обрыве канала: они ничего не пишут, задвоиться нечему.
  // Сюда НЕ входят мутации (pay_/ship_/advance_/respond_/upload_/confirm_/create_…).
  // Если действия тут нет — оно просто НЕ авто-ретраится (безопасная деградация).
  const READONLY_ACTIONS = new Set([
    'op_dashboard','op_queue','op_sla_breach','op_payments_dashboard','op_payments_stats',
    'op_customs_dashboard','op_logistics_stats','op_my_suppliers','op_kyb_queue','op_my_user_chats',
    'op_drawings_by_part','op_analytics_hub','op_hs_lookup','op_sanctions_check','op_my_suppliers',
    'seller_inbox','seller_pipeline','seller_dashboard','seller_warehouses','seller_drawings',
    'seller_analytics_hub','seller_catalog','seller_customers','seller_revenue',
    'get_orders','get_order_detail','get_my_deals','get_rfq_status','get_claims','get_balance',
    'get_buyer_discount','get_demand_report','get_analytics','track_order','my_kam','my_accruals',
    'kam_deals','support_home',
  ]);
  // Auth-формы: показ формы (БЕЗ confirmed) = read-only → таймаут+ретрай безопасны
  // (иначе зависший POST на РФ-канале крутит спиннер вечно — «стать поставщиком» не
  // грузилась). С confirmed=true это мутация (создание акк/логин) — НЕ ретраим.
  const AUTH_DISPLAY_ACTIONS = new Set([
    'start_registration', 'start_login', 'start_onboarding', 'switch_role_login',
  ]);

  async function api(path, opts={}) {
    // Канал браузер↔origin (РФ) нестабилен: ~10-15% запросов рвутся ИЛИ
    // зависают (без таймаута UI крутит спиннер вечно). Для idempotent
    // запросов (GET/HEAD) — до 3 попыток с таймаутом 8с и backoff'ом, плюс
    // ретрай на 5xx (транзиентный upstream). POST НЕ ретраим (риск двойного
    // выполнения: платёж/заказ/импорт) и без abort'а — одна попытка.
    const method = (opts.method || 'GET').toUpperCase();
    const idempotent = (method === 'GET' || method === 'HEAD');
    // retryNet: можно ли ретраить ПРИ ОБРЫВЕ КАНАЛА/5xx. GET/HEAD — всегда; POST —
    // только если вызывающий явно пометил действие read-only (opts.retryNetwork),
    // т.к. для read-only задвоиться нечему. Мутации (pay/ship/respond/upload) — нет.
    const retryNet = idempotent || !!opts.retryNetwork;
    // Таймаут на попытку: дефолт 8с (быстрые read-only). Для длинных AI-ответов
    // вызывающий передаёт opts.timeoutMs побольше (релейный путь идёт дольше).
    const timeoutMs = opts.timeoutMs || 8000;
    const attempts = 3;
    let lastErr;
    for (let i = 0; i < attempts; i++) {
      const ctrl = (retryNet && window.AbortController) ? new AbortController() : null;
      const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
      let res;
      try {
        res = await fetch(path, {
          headers: {'Content-Type':'application/json','X-CSRFToken': csrf(), ...(opts.headers||{})},
          ...opts,
          ...(ctrl ? {signal: ctrl.signal} : {}),
        });
      } catch (e) {
        if (timer) clearTimeout(timer);
        lastErr = e;
        // Обрыв канала: ретраим GET и read-only POST; мутирующий POST — нет
        // (запрос мог дойти и выполниться → риск двойного выполнения).
        if (retryNet && i < attempts - 1) { await new Promise(r => setTimeout(r, 500 + i*700)); continue; }
        throw e;
      }
      if (timer) clearTimeout(timer);
      if (res.ok) return res.json();
      // 52x от Cloudflare = origin НЕдостижим → запрос вообще не дошёл до приложения,
      // поэтому ретрай безопасен ДАЖЕ для мутирующего POST (двойного выполнения нет).
      // Прочие 5xx (500/502/503) — ретраим GET и read-only POST.
      const cfUnreachable = (res.status >= 520 && res.status <= 524);
      const canRetry = cfUnreachable || (retryNet && res.status >= 500);
      if (canRetry && i < attempts - 1) {
        lastErr = new Error(`${path} → ${res.status}`);
        await new Promise(r => setTimeout(r, 600 + i*900));
        continue;
      }
      const err = new Error(`${path} → ${res.status}`);
      err.status = res.status;
      throw err;
    }
    throw lastErr;
  }

  // ══════════════════════════════════════════════════════════
  // Sidebar toggle
  // ══════════════════════════════════════════════════════════
  function isMobile() { return window.innerWidth <= 768; }

  // ── Tiny "ding" via WebAudio (no external assets) ─────────────
  let _audioCtx = null;
  function notifBeep() {
    try {
      if (localStorage.getItem('cf_notif_sound') === '0') return; // user-muted
      _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      const ctx = _audioCtx;
      // Browsers require a user gesture to start audio; if not yet allowed, bail silently.
      if (ctx.state === 'suspended') { ctx.resume().catch(() => {}); }
      const t0 = ctx.currentTime;
      // Two short tones — a friendly "di-ding".
      [
        {f: 880, start: 0,    dur: 0.09, gain: 0.08},
        {f: 1320, start: 0.09, dur: 0.13, gain: 0.07},
      ].forEach(n => {
        const osc = ctx.createOscillator();
        const g = ctx.createGain();
        osc.frequency.value = n.f;
        osc.type = 'sine';
        g.gain.setValueAtTime(0, t0 + n.start);
        g.gain.linearRampToValueAtTime(n.gain, t0 + n.start + 0.01);
        g.gain.linearRampToValueAtTime(0, t0 + n.start + n.dur);
        osc.connect(g); g.connect(ctx.destination);
        osc.start(t0 + n.start);
        osc.stop(t0 + n.start + n.dur + 0.02);
      });
    } catch (e) { /* audio unsupported — silent */ }
  }
  window.toggleNotifSound = function(on) {
    localStorage.setItem('cf_notif_sound', on ? '1' : '0');
  };

  // ── Settings panel ────────────────────────────────────────────
  function applyDarkMode(on) {
    document.body.classList.toggle('dark-mode', !!on);
    localStorage.setItem('cf_dark_mode', on ? '1' : '0');
    // Синхронизируем чекбокс в настройках
    const cb = document.getElementById('settingDarkMode');
    if (cb) cb.checked = !!on;
  }
  // Глобальный toggle для top-bar кнопки 🌙/☀️
  window.toggleTheme = function() {
    const isDark = document.body.classList.contains('dark-mode');
    applyDarkMode(!isDark);
  };
  function applyLang(lang) {
    if (!lang) return;
    document.cookie = 'django_language=' + lang + '; path=/; max-age=' + (60*60*24*365);
    localStorage.setItem('cf_lang', lang);
    // 1) Сразу перерисовываем клиентские строки через window.setLanguage (из i18n.js)
    // 2) Сохраняем выбор в профиле через /api/set-language/
    // 3) reload() — чтобы серверные {% trans %} тоже переключились
    if (typeof window.setLanguage === 'function') {
      Promise.resolve(window.setLanguage(lang)).finally(function () {
        location.reload();
      });
    } else {
      location.reload();
    }
  }
  function loadSettings() {
    // Sound toggle
    const sndEl = document.getElementById('settingNotifSound');
    if (sndEl) sndEl.checked = localStorage.getItem('cf_notif_sound') !== '0';
    // Dark mode
    const darkEl = document.getElementById('settingDarkMode');
    const darkOn = localStorage.getItem('cf_dark_mode') === '1';
    if (darkEl) darkEl.checked = darkOn;
    if (darkOn) document.body.classList.add('dark-mode');
    // Lang
    const langEl = document.getElementById('settingLang');
    if (langEl) {
      const m = document.cookie.match(/django_language=([a-z-]+)/);
      langEl.value = (m && m[1]) || localStorage.getItem('cf_lang') || (document.documentElement.getAttribute('lang') || 'ru');
    }
  }
  window.onSettingChange = function(key, val) {
    if (key === 'sound') toggleNotifSound(val);
    else if (key === 'dark') applyDarkMode(val);
    else if (key === 'lang') applyLang(val);
  };
  window.toggleSettingsPanel = function(force) {
    const panel = document.getElementById('settingsPanel');
    if (!panel) return;
    const willOpen = force === undefined ? panel.hasAttribute('hidden') : !!force;
    if (willOpen) {
      panel.removeAttribute('hidden');
      setTimeout(() => document.addEventListener('click', _settingsOutside, true), 0);
    } else {
      panel.setAttribute('hidden', '');
      document.removeEventListener('click', _settingsOutside, true);
    }
  };
  function _settingsOutside(ev) {
    const panel = document.getElementById('settingsPanel');
    if (!panel) return;
    // Не закрывать клик по самой панели или по кнопке настроек
    if (panel.contains(ev.target) || ev.target.closest('.side-settings')) return;
    panel.setAttribute('hidden', '');
    document.removeEventListener('click', _settingsOutside, true);
  }

  // ── Realtime notification toast (WS push) ─────────────────────
  function showNotifToast(payload) {
    notifBeep();
    try {
      let host = document.getElementById('notifToastHost');
      if (!host) {
        host = document.createElement('div');
        host.id = 'notifToastHost';
        host.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none;';
        document.body.appendChild(host);
      }
      const t = document.createElement('div');
      const title = (payload && payload.title) || tr('card.notification');
      const body  = (payload && payload.body)  || '';
      const url   = (payload && payload.url)   || '';
      t.style.cssText = 'pointer-events:auto;background:#1d2330;color:#fff;padding:10px 14px;border-radius:10px;border:1px solid rgba(100,181,246,0.35);box-shadow:0 6px 24px rgba(0,0,0,.25);max-width:340px;font-size:13px;line-height:1.4;cursor:pointer;';
      t.innerHTML = '<div style="font-weight:600;margin-bottom:2px;">🔔 ' + esc(title) + '</div>' + (body ? '<div style="opacity:.85;">' + esc(body) + '</div>' : '');
      if (url) t.addEventListener('click', () => { try { location.href = url; } catch(e){} });
      host.appendChild(t);
      setTimeout(() => { t.style.transition = 'opacity .3s'; t.style.opacity = '0'; setTimeout(() => t.remove(), 320); }, 5000);
      // Bump bell badge + prepend to dropdown if user already opened it
      bumpBellBadge(+1);
      prependNotifItem(payload);
    } catch (e) { console.error('notif toast', e); }
  }

  // ── Notification bell + dropdown ──────────────────────────────
  const notif = { items: [], unread: 0, loaded: false, open: false, byKind: {} };

  // Какой kind уведомления «требует действия» в каком разделе (пилюле).
  // Бейдж на пилюле = сумма непрочитанных уведомлений сопоставленных kind'ов.
  const PILL_NOTIF_KINDS = {
    // покупатель
    get_my_deals: ['order'], get_orders: ['order'], get_rfq_status: ['rfq'], get_balance: ['payment'],
    // продавец
    seller_inbox: ['rfq', 'order', 'sla'], seller_warehouses: ['order'],
    // оператор / суброли
    op_queue: ['order', 'rfq'], op_sla_breach: ['sla'], op_payments_dashboard: ['payment'],
    op_payments_stats: ['payment'], op_customs_dashboard: ['sla'], get_claims: ['claim'],
    op_dashboard: ['sla', 'order'], op_my_user_chats: ['rfq', 'order'],
    // КАМ
    kam_deals: ['order'], seller_customers: ['rfq'],
  };
  function pillActionBadge(action) {
    const kinds = PILL_NOTIF_KINDS[action];
    if (!kinds) return 0;
    let n = 0;
    for (let i = 0; i < kinds.length; i++) n += (notif.byKind[kinds[i]] || 0);
    return n;
  }
  // HTML бейджа «требует действия» для пилюли (cls = pill-badge | pm-badge). Пусто, если 0.
  function pillBadgeHtml(action, cls) {
    const c = pillActionBadge(action);
    if (c <= 0) return '';
    return '<span class="' + cls + '" title="Требует действия">' + (c > 99 ? '99+' : c) + '</span>';
  }

  function setBellBadge(n) {
    notif.unread = Math.max(0, n|0);
    const el = document.getElementById('bellBadge');
    if (!el) return;
    if (notif.unread > 0) {
      el.textContent = notif.unread > 99 ? '99+' : String(notif.unread);
      el.style.display = '';
    } else {
      el.style.display = 'none';
    }
  }
  function bumpBellBadge(d) { setBellBadge(notif.unread + d); }

  function notifTimeAgo(iso) {
    if (!iso) return '';
    try {
      const t = new Date(iso).getTime();
      const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
      if (s < 60) return 'только что';
      if (s < 3600) return Math.floor(s/60) + ' мин';
      if (s < 86400) return Math.floor(s/3600) + ' ч';
      return Math.floor(s/86400) + ' д';
    } catch(e) { return ''; }
  }

  function renderNotifList() {
    const list = document.getElementById('notifList');
    if (!list) return;
    if (!notif.items.length) {
      list.innerHTML = '<div class="notif-empty">Нет уведомлений</div>';
      return;
    }
    list.innerHTML = notif.items.map(n =>
      // esc() для всех полей, включая id (защита от подмены типа в JSON)
      '<div class="notif-item' + (n.is_read ? '' : ' unread') + '" data-id="' + esc(n.id) + '" data-url="' + esc(n.url || '') + '">' +
        '<div class="notif-row">' +
          '<span class="notif-kind ' + esc(n.kind || 'info') + '">' + esc(n.kind || 'info') + '</span>' +
          '<span class="notif-time">' + esc(notifTimeAgo(n.created_at)) + '</span>' +
        '</div>' +
        '<div class="notif-title">' + esc(n.title || '') + '</div>' +
        (n.body ? '<div class="notif-body">' + esc(n.body) + '</div>' : '') +
      '</div>'
    ).join('');
  }

  function prependNotifItem(payload) {
    if (!payload || !payload.id) return;
    // Drop existing copy by id (in case server replays)
    notif.items = (notif.items || []).filter(x => x.id !== payload.id);
    const _k = payload.kind || 'info';
    notif.items.unshift({
      id: payload.id, kind: _k,
      title: payload.title || '', body: payload.body || '',
      url: payload.url || '', is_read: false,
      created_at: new Date().toISOString(),
    });
    if (notif.items.length > 50) notif.items.length = 50;
    // Живой бейдж «требует действия» на пилюлях: +1 к kind, перерисовать пилюли.
    notif.byKind[_k] = (notif.byKind[_k] || 0) + 1;
    try { refreshPills(); } catch (_) {}
    if (notif.open) renderNotifList();
  }

  async function loadNotifications() {
    const list = document.getElementById('notifList');
    if (list && !notif.loaded) {
      // Loading skeleton — пока ждём ответ. Раньше: пустой блок без feedback.
      list.innerHTML = '<div class="notif-skel"></div>'.repeat(3);
    }
    try {
      const data = await api('/api/assistant/notifications/?limit=20');
      notif.items = data.items || [];
      notif.loaded = true;
      notif.byKind = data.unread_by_kind || {};
      setBellBadge(data.unread_count || 0);
      renderNotifList();
      // Пилюли уже отрисованы в init() ДО загрузки уведомлений — перерисуем с бейджами.
      try { refreshPills(); } catch (_) {}
    } catch (e) {
      console.warn('loadNotifications failed', e);
      if (list) {
        list.innerHTML =
          '<div class="notif-error">⚠️ Не удалось загрузить уведомления. '
          + '<button type="button" class="notif-retry">Повторить</button></div>';
        const btn = list.querySelector('.notif-retry');
        if (btn) btn.addEventListener('click', () => {
          notif.loaded = false; loadNotifications();
        });
      }
    }
  }

  window.toggleNotifPanel = function() {
    const panel = document.getElementById('notifPanel');
    if (!panel) return;
    notif.open = panel.hasAttribute('hidden');
    if (notif.open) {
      panel.removeAttribute('hidden');
      if (!notif.loaded) loadNotifications(); else renderNotifList();
      // Close on outside click
      setTimeout(() => document.addEventListener('click', _notifOutside, true), 0);
    } else {
      panel.setAttribute('hidden', '');
      document.removeEventListener('click', _notifOutside, true);
    }
  };
  function _notifOutside(ev) {
    const panel = document.getElementById('notifPanel');
    const bell = document.getElementById('topBell');
    if (!panel || !bell) return;
    if (panel.contains(ev.target) || bell.contains(ev.target)) return;
    panel.setAttribute('hidden', '');
    notif.open = false;
    document.removeEventListener('click', _notifOutside, true);
  }

  async function markNotifRead(id) {
    try {
      const r = await api('/api/assistant/notifications/' + id + '/read/', {method:'POST', body: JSON.stringify({})});
      const it = notif.items.find(x => x.id === id);
      if (it && !it.is_read) {
        it.is_read = true;
        const k = it.kind || 'info';
        notif.byKind[k] = Math.max(0, (notif.byKind[k] || 0) - 1);
        try { refreshPills(); } catch (_) {}
      }
      setBellBadge(r.unread_count || 0);
      renderNotifList();
    } catch (e) { console.warn('markNotifRead', e); }
  }

  window.markAllNotifsRead = async function() {
    try {
      await api('/api/assistant/notifications/read-all/', {method:'POST', body: JSON.stringify({})});
      notif.items.forEach(x => x.is_read = true);
      notif.byKind = {};
      setBellBadge(0);
      try { refreshPills(); } catch (_) {}
      renderNotifList();
    } catch (e) { console.warn('markAllNotifsRead', e); }
  };

  // Click on a notification row → mark read + navigate (if url given)
  document.addEventListener('click', (ev) => {
    const item = ev.target.closest && ev.target.closest('.notif-item');
    if (!item) return;
    const id = parseInt(item.dataset.id, 10);
    const url = item.dataset.url || '';
    if (id) markNotifRead(id);
    if (url) { try { location.href = url; } catch(e){} }
  });

  // ── Клик по пилюле порта → выделить FOB-ячейку соответствующего режима ──
  // Чистый client-side: ищем матрицу в том же сообщении, находим строку
  // нужного mode (sea/air), подсвечиваем FOB-ячейку чёрной рамкой.
  // Повторный клик по той же пилюле → снимает выделение.
  document.addEventListener('click', (ev) => {
    const btn = ev.target.closest && ev.target.closest('[data-port-select]');
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    const mode = btn.dataset.portSelect;
    // Найти матрицу в том же сообщении
    const msg = btn.closest('.msg') || btn.closest('.card');
    if (!msg) return;
    const matrix = msg.querySelector('.sm-table, .sm-row, .sm-cell')?.closest('table, .sm-grid, div');
    // Снимаем прежнее выделение в этой матрице + с пилюль
    msg.querySelectorAll('.sm-cell-selected').forEach(c => c.classList.remove('sm-cell-selected'));
    const alreadyActive = btn.classList.contains('ob-port-active');
    msg.querySelectorAll('.ob-port-active').forEach(p => p.classList.remove('ob-port-active'));
    if (alreadyActive) return; // toggle off
    btn.classList.add('ob-port-active');
    // Находим строку соответствующего режима и её FOB-ячейку (первая cell)
    const rows = msg.querySelectorAll('.sm-row');
    rows.forEach(row => {
      const modeLabel = row.querySelector('.sm-mode')?.textContent || '';
      const matches =
        (mode === 'sea'  && /Морем/i.test(modeLabel)) ||
        (mode === 'air'  && /Авиа/i.test(modeLabel)) ||
        (mode === 'auto' && /Авто/i.test(modeLabel));
      if (matches) {
        const fobCell = row.querySelector('.sm-cell:not(.sm-cell-disabled)');
        if (fobCell) {
          fobCell.classList.add('sm-cell-selected');
          fobCell.scrollIntoView({behavior: 'smooth', block: 'center'});
        }
      }
    });
  });

  window.toggleSidebar = (force) => {
    const sb = $('sidebar');
    const open = force === undefined ? !sb.classList.contains('open') : force;
    sb.classList.toggle('open', open);
    if (!isMobile()) {
      try { localStorage.setItem(SB_KEY, open ? '1' : '0'); } catch(e){}
    }
  };

  function applyDefaultSidebar(hasHistory) {
    // Снимаем ранний html-класс (он открывал сайдбар до загрузки JS против мелькания);
    // дальше источник истины — класс .open на самом #sidebar.
    document.documentElement.classList.remove('cf-sb-open');
    if (isMobile()) {
      // Mobile: always closed by default
      $('sidebar').classList.remove('open');
      return;
    }
    // Desktop: persisted preference, or open if user has history
    const saved = localStorage.getItem(SB_KEY);
    let open;
    if (saved === '1') open = true;
    else if (saved === '0') open = false;
    // Нет явной настройки (новый визит / переход с главной): по умолчанию
    // РАЗВЁРНУТ — чтобы был виден переключатель ролей Покупатель/Продавец/Оператор
    // и было понятно, как войти как продавец. Исключение — вход через поиск с
    // главной (/chat/?q=...): там нужно место под результаты, оставляем по истории.
    else open = _ENTRY_SEARCH ? hasHistory : true;
    $('sidebar').classList.toggle('open', open);
  }

  // Click outside on mobile to close
  document.addEventListener('click', (e) => {
    if (!isMobile()) return;
    const sb = $('sidebar');
    if (!sb.classList.contains('open')) return;
    if (sb.contains(e.target) || e.target.closest('.top-burger')) return;
    sb.classList.remove('open');
  });

  // ══════════════════════════════════════════════════════════
  // State transitions: WELCOME ↔ CONV
  // ══════════════════════════════════════════════════════════
  function showConv() {
    $('welcomeStage').classList.add('hidden');
    $('convStage').classList.remove('hidden');
    // Юзер в conv-стейдже — снимаем sticky welcome, чтобы F5 возвращал
    // в эту conv (priority storedConv), а не выкидывал на welcome.
    try { sessionStorage.removeItem('cf_on_welcome'); } catch(_){}
  }
  function showWelcome() {
    document.documentElement.classList.remove('cf-restoring'); // показываем welcome — анти-мелькание больше не нужно
    $('welcomeStage').classList.remove('hidden');
    $('convStage').classList.add('hidden');
    $('streamInner').innerHTML = '';
    // Sticky welcome — при F5 остаёмся на welcome, не падаем в priority-3.
    // Снимается в openConv() когда юзер явно открывает conv.
    try { sessionStorage.setItem('cf_on_welcome', '1'); } catch(_){}
    // URL → чистый /chat/, без ?conv= и без ?new=1
    try {
      if (window.location.search) history.replaceState({}, '', '/chat/');
    } catch(_){}
  }

  // 🏠 Home — возврат к welcome stage, сохраняем conversation
  window.goHome = () => {
    showWelcome();
    // Скрыть notif/settings панели если открыты
    const np = document.getElementById('notifPanel');
    if (np) np.setAttribute('hidden', '');
    const sp = document.getElementById('settingsPanel');
    if (sp) sp.setAttribute('hidden', '');
  };

  // ══════════════════════════════════════════════════════════
  // Card renderers
  // ══════════════════════════════════════════════════════════
  const renderers = {
    // Ссылка с быстрым копированием (реф-ссылка, инвайт). Красивый бокс +
    // кнопки «Копировать» и «Поделиться» (native share на мобиле).
    copy_link(d) {
      _ensureCopyLinkCSS();
      const url = String(d.url || d.link || '');
      const title = d.title || 'Ссылка';
      const hint = d.hint || '';
      const ICON_COPY = '<svg class="cl-ic-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
      const ICON_DONE = '<svg class="cl-ic-done" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
      return `<div class="card cl-card">
        <div class="cl-title">${esc(title)}</div>
        <div class="cl-box">
          <span class="cl-url" title="${esc(url)}">${esc(url)}</span>
          <button type="button" class="cl-copy" data-copy="${esc(url)}" title="Скопировать" aria-label="Скопировать">${ICON_COPY}${ICON_DONE}</button>
        </div>
        ${hint ? `<div class="cl-hint">${esc(hint)}</div>` : ''}
      </div>`;
    },
    // Чертежи: папки (drop-target) + чертежи без папки (draggable). Inline-
    // создание папки и drag-n-drop — обработчики ниже (_initDrawingsDnD).
    drawings(d) {
      _ensureDrawingsCSS();
      const folders = d.folders || [];
      const ungrouped = d.ungrouped || [];
      const itemHtml = (r) => `
        <div class="dw-row" data-drawing-id="${esc(String(r.id))}">
          <div class="dw-item" draggable="true" data-drawing-id="${esc(String(r.id))}" data-url="${esc(r.url || '')}" title="Открыть · перетащите в папку">
            <span class="dw-grip">⠿</span>
            <span class="dw-ibody">
              <span class="dw-ititle">${esc(r.title || '')}</span>
              <span class="dw-isub">${esc(r.subtitle || '')}</span>
            </span>
            <span class="dw-link-btn" title="Привязать к позиции каталога">🔗</span>
          </div>
          <div class="dw-linkpanel">
            <input class="dw-linkinput" type="text" placeholder="Артикул или название позиции — напр. 6D102" autocomplete="off" oninput="window.__dwLinkInline&&window.__dwLinkInline(this)" onkeydown="if(event.key==='Enter'){event.preventDefault();window.__dwLinkInline&&window.__dwLinkInline(this,true);}"/>
            <div class="dw-results"></div>
          </div>
        </div>`;
      const fHtml = folders.map(f => {
        const proj = !!f.is_project;
        return `
        <div class="dw-folder${proj ? ' dw-folder-proj' : ''}" data-folder-id="${esc(String(f.id))}"${proj ? ' data-project="1"' : ''}>
          <div class="dw-fhead" title="${proj ? 'Папка проекта (чертежи загружены на странице проекта)' : 'Нажмите, чтобы развернуть · перетащите сюда чертёж'}">
            <span class="dw-fchev">▸</span>
            <span class="dw-fname">${proj ? '🗂' : '📁'} ${esc(f.name)}${proj ? ' <span class="dw-projtag">проект</span>' : ''}</span>
            <span class="dw-fcount">${esc(String(f.count))}</span>
          </div>
          <div class="dw-fbody">
            ${(f.drawings || []).map(itemHtml).join('') || '<div class="dw-empty dw-fempty">' + (proj ? 'Пусто' : 'Пусто — перетащите сюда чертёж') + '</div>'}
          </div>
        </div>`;
      }).join('');
      const empty = (!folders.length && !ungrouped.length)
        ? '<div class="dw-empty">Пока пусто — создайте папку или загрузите чертёж.</div>' : '';
      return `<div class="card dw-card">
        <div class="card-title">${esc(d.title || 'Мои чертежи')}</div>
        <div class="dw-newrow">
          <input class="dw-newinput" type="text" placeholder="Новая папка — напр. Ходовка Komatsu" autocomplete="off" maxlength="120"/>
          <button type="button" class="dw-newbtn">📁 Создать</button>
        </div>
        ${folders.length ? `<div class="dw-sec">Папки</div><div class="dw-folders">${fHtml}</div>` : ''}
        ${(folders.length || ungrouped.length) ? `<div class="dw-sec dw-nofolder" data-folder-id="" title="Перетащите сюда, чтобы убрать из папки">Без папки${ungrouped.length ? '' : ' — пусто'}</div>` : ''}
        ${ungrouped.length ? `<div class="dw-items">${ungrouped.map(itemHtml).join('')}</div>` : ''}
        ${empty}
      </div>`;
    },
    // Умный поиск позиции каталога для привязки к чертежу (live-swap .dw-results).
    drawing_link(d) {
      _ensureDrawingsCSS();
      const did = esc(String(d.drawing_id));
      const rows = (d.rows || []).map(r => `
        <div class="dw-result" data-action="bind_drawing" data-params='${esc(JSON.stringify(r.params || {}))}' data-label="${esc(r.title || '')}">
          <span class="dw-rtitle">${esc(r.title || '')}</span>
          ${r.subtitle ? `<span class="dw-rsub">${esc(r.subtitle)}</span>` : ''}
        </div>`).join('');
      let body;
      if (!d.searched) body = '<div class="dw-empty">Введите артикул или название позиции</div>';
      else if (!(d.rows || []).length) body = '<div class="dw-empty">Ничего не найдено</div>';
      else body = rows;
      return `<div class="card dw-card dw-link" data-drawing-id="${did}">
        <div class="card-title">🔗 Привязать «${esc(d.title || '')}» к позиции</div>
        <input class="dw-newinput dw-linkinput" type="text" placeholder="Артикул или название — напр. 6D102 или коленвал" autocomplete="off" value="${esc(d.q || '')}" oninput="window.__drawingLinkSearch&&window.__drawingLinkSearch(this,'${did}')" onkeydown="if(event.key==='Enter'){event.preventDefault();window.__drawingLinkSearch&&window.__drawingLinkSearch(this,'${did}',true);}"/>
        <div class="dw-results">${body}</div>
      </div>`;
    },
    // Операторский поиск чертежей по артикулу (live, swap .opd-results).
    op_drawings(d) {
      _ensureDrawingsCSS();
      const input = `<input class="dw-newinput opd-input" type="text" value="${esc(d.q || '')}" placeholder="Артикул (OEM) — напр. 6D102 или 5243" autocomplete="off" oninput="window.__opDrawingsSearch&&window.__opDrawingsSearch(this)" onkeydown="if(event.key==='Enter'){event.preventDefault();window.__opDrawingsSearch&&window.__opDrawingsSearch(this,true);}"/>`;
      let body;
      if (!d.searched) body = '<div class="dw-empty">Введите артикул — покажу, что нужно покупателям и что предлагают продавцы.</div>';
      else if (!(d.groups || []).some(g => (g.rows || []).length)) body = '<div class="dw-empty">По «' + esc(d.q || '') + '» чертежей не найдено.</div>';
      else body = (d.groups || []).filter(g => (g.rows || []).length).map(g =>
        '<div class="opd-group"><div class="opd-gtitle">' + esc(g.label) + ' (' + (g.rows || []).length + ')</div>'
        + (g.rows || []).map(r =>
            '<a class="dw-result" href="' + esc(r.url || '#') + '" target="_blank" rel="noopener" style="text-decoration:none;color:inherit">'
            + '<span class="dw-rtitle">' + esc(r.title || '') + '</span>'
            + (r.subtitle ? '<span class="dw-rsub">' + esc(r.subtitle) + '</span>' : '') + '</a>').join('')
        + '</div>').join('');
      return `<div class="card dw-card opd-card">
        <div class="card-title">📐 Чертежи</div>
        ${input}
        <div class="opd-results">${body}</div>
      </div>`;
    },
    product(d) {
      return `<div class="card">
        <div class="card-row">
          <div class="card-emoji">⚙️</div>
          <div class="card-info">
            <div class="card-title">${esc(d.article || '')} — ${esc(d.name || d.title || '')}</div>
            <div class="card-sub">${esc(d.brand || '')}${d.country ? ' · ' + esc(d.country) : ''}${d.category ? ' · ' + esc(d.category) : ''}</div>
          </div>
          <div class="card-price">${fmtMoney(d.price, d.currency)}</div>
        </div>
        <div class="card-meta">
          ${d.in_stock !== false ? `<span class="card-chip card-chip-green">${d.quantity ? d.quantity + ' ' + tr('stock.pcs') : tr('stock.in_stock')}</span>` : `<span class="card-chip card-chip-gray">${tr('stock.not_available')}</span>`}
          ${d.delivery_days ? `<span class="card-chip">${d.delivery_days} дн</span>` : ''}
          ${d.condition ? `<span class="card-chip card-chip-gray">${esc(d.condition)}</span>` : ''}
        </div>
      </div>`;
    },
    qr(d) {
      return `<div class="card qr-card">
        <div class="card-title">${esc(d.title || 'QR-код')}</div>
        ${d.subtitle ? `<div class="qr-sub">${esc(d.subtitle)}</div>` : ''}
        <div class="qr-img"><img src="${esc(d.image_url)}" alt="QR" loading="lazy"/></div>
        <div class="qr-payload">${esc(d.payload || '')}</div>
      </div>`;
    },
    price_breakdown(d) {
      const lines = (d.lines || []).map(l => {
        const sign = l.amount < 0 ? 'pb-neg' : '';
        return `<div class="pb-row ${sign}">
          <span class="pb-label">${esc(l.label)}</span>
          <span class="pb-amount">${fmtMoney(l.amount, d.currency || 'USD')}</span>
        </div>`;
      }).join('');
      const cur = d.currency || 'USD';
      return `<div class="card pb-card">
        <div class="card-title">${esc(d.title || tr('card.price_calc'))}</div>
        <div class="pb-rows">${lines}</div>
        <div class="pb-total">
          <span class="pb-total-label">Итого клиенту</span>
          <span class="pb-total-amount">${fmtMoney(d.total, cur)}</span>
        </div>
      </div>`;
    },
    draft(d) {
      const renderRow = (r) => {
        const wide = r.wide ? ' dr-row-wide' : '';
        return `<div class="dr-row${r.primary ? ' dr-primary' : ''}${wide}">
          <span class="dr-label">${esc(r.label || '')}</span>
          <span class="dr-value">${esc(String(r.value ?? '—'))}</span>
        </div>`;
      };
      // Поддержка групп: если d.groups задан — рендерим секциями, иначе плоско
      let body = '';
      if (d.groups && d.groups.length) {
        body = d.groups.map(g => `
          <div class="dr-group">
            ${g.title ? `<div class="dr-group-title">${esc(g.title)}</div>` : ''}
            <div class="dr-grid">${(g.rows || []).map(renderRow).join('')}</div>
          </div>`).join('');
      } else {
        body = `<div class="dr-grid">${(d.rows || []).map(renderRow).join('')}</div>`;
      }
      const warns = (d.warnings || []).map(w =>
        `<div class="dr-warning">${esc(w)}</div>`).join('');
      const confirmParams = JSON.stringify(d.confirm_params || {});
      const showActions = d.confirm_label && d.confirm_label !== '—';
      return `<div class="card dr-card">
        ${d.title ? `<div class="dr-head"><span class="dr-title">${esc(d.title)}</span></div>` : ''}
        ${body}
        ${warns ? `<div class="dr-warnings">${warns}</div>` : ''}
        ${showActions ? `<div class="dr-actions">
          <button class="act-btn dr-confirm" data-action="${esc(d.confirm_action || '')}" data-params='${esc(confirmParams)}' data-label="${esc(d.confirm_label || tr('common.confirm'))}">${esc(d.confirm_label || tr('common.confirm'))}</button>
          <button class="act-btn dr-cancel" type="button" onclick="window.cancelDraftCard&&window.cancelDraftCard(this)">${esc(d.cancel_label || tr('common.cancel'))}</button>
        </div>` : ''}
      </div>`;
    },
    ops_dashboard(d) {
      // Используем тот же стиль что и seller_revenue (.wal-card / .sr-stats / .wal-rows)
      // — единый язык карточек депозита/эскроу.
      const fmt = (v) => '$' + (Number(v)||0).toLocaleString('en-US', {maximumFractionDigits: 0});
      const splitAmount = (v) => {
        const s = (Number(v)||0).toFixed(2);
        const [w, dc] = s.split('.');
        return [Number(w).toLocaleString('en-US'), dc];
      };
      const [whole, dec] = splitAmount(d.hero_value);
      // KPI оператора. 3 режима:
      //  • s.details_rows → <details> с inline-раскрытием вниз
      //  • s.action       → клик → переход в чат на action
      //  • иначе          → статический тайл
      const stats = (d.stats || []).map(s => {
        const cls = s.tone === 'ok' ? 'wal-amt-in' : (s.tone === 'bad' ? 'wal-amt-out' : '');
        const inner = `
          <div class="sr-stat-lbl">${esc(s.label || '')}</div>
          <div class="sr-stat-val ${cls}">${esc(s.value || '')}</div>
          <div class="sr-stat-hint">${esc(s.sub || '')}</div>`;
        if (Array.isArray(s.details_rows)) {  // даже пустой → раскрываемый тайл (покажет «записей нет»), а не мёртвый статик
          const rowsHtml = s.details_rows.slice(0, 30).map(r => {
            const params = JSON.stringify(r.params || {});
            const click = r.action ? `data-action="${esc(r.action)}" data-params='${esc(params)}' role="button" tabindex="0" style="cursor:pointer"` : '';
            return `<div class="sr-det-row" ${click}>
              <span class="sr-det-time">${esc(r.left || '')}</span>
              <span class="sr-det-desc">${esc(r.title || '')}</span>
              <span class="sr-det-amt ${r.amount_class || ''}">${esc(r.amount || '')}</span>
            </div>`;
          }).join('');
          const more = s.details_rows.length > 30
            ? `<div class="sr-det-more">… ещё ${s.details_rows.length - 30}</div>` : '';
          const emptyMsg = s.details_empty || 'Записей нет';
          return `<details class="sr-stat sr-stat-expand">
            <summary class="sr-stat-summary">
              <div class="sr-stat-summary-inner">${inner}</div>
              <span class="sr-det-chevron">▾</span>
            </summary>
            <div class="sr-det-body">
              ${rowsHtml || `<div class="sr-det-empty">${esc(emptyMsg)}</div>`}
              ${more}
            </div>
          </details>`;
        }
        if (s.action) {
          const params = JSON.stringify({...(s.params || {}), _label: s.label || ''});
          return `<button type="button" class="sr-stat sr-stat-clickable"
            data-action="${esc(s.action)}" data-params='${esc(params)}'>${inner}</button>`;
        }
        return `<div class="sr-stat">${inner}</div>`;
      }).join('');
      // Список активных заказов в эскроу — те же стили что у транзакций
      const rows = (d.rows || []).map(r => {
        const params = JSON.stringify(r.params || {});
        const click = r.action ? `data-action="${esc(r.action)}" data-params='${esc(params)}' role="button" tabindex="0" style="cursor:pointer"` : '';
        return `<div class="wal-row" ${click}>
          <span class="wal-time">${esc(r.left || '')}</span>
          <span class="wal-desc">${esc(r.title || '')}</span>
          <span class="wal-amt wal-amt-out">${esc(r.amount || '')}</span>
        </div>`;
      }).join('');
      return `<div class="card wal-card">
        <div class="wal-head">
          <div class="wal-head-lbl">${esc(d.hero_label || 'Сейчас в эскроу')}</div>
          <div class="wal-head-amount">
            <span class="wal-head-ccy">$</span>${esc(whole)}<span class="wal-head-dec">.${esc(dec)}</span>
            <span class="wal-head-curr">${esc(d.currency || 'USD')}</span>
          </div>
        </div>
        ${stats ? `<div class="sr-stats">${stats}</div>` : ''}
        ${rows ? `<div class="wal-txs-head">${esc(d.rows_title || 'Активные заказы')}</div>
          <div class="wal-group"><div class="wal-rows">${rows}</div></div>` : ''}
      </div>`;
    },
    seller_revenue(d) {
      const fmt = (v) => '$' + (Number(v)||0).toLocaleString('en-US', {maximumFractionDigits: 0});
      const splitAmount = (v) => {
        const s = (Number(v)||0).toFixed(2);
        const [w, dc] = s.split('.');
        return [Number(w).toLocaleString('en-US'), dc];
      };
      const [whole, dec] = splitAmount(d.balance);
      const groups = {};
      (d.transactions || []).forEach(t => {
        const k = t.date;
        if (!groups[k]) groups[k] = [];
        groups[k].push(t);
      });
      const groupHtml = Object.keys(groups).map(date => {
        const rows = groups[date].map(t => {
          const sign = t.is_in ? '+' : '−';
          const cls  = t.is_in ? 'wal-amt-in' : 'wal-amt-out';
          // Order-ref как кликабельный chip (открывает get_order_detail).
          // ID извлекаем из строки (берём только цифры — формат "133" или "ORD-133").
          const refIdMatch = t.order_ref ? String(t.order_ref).match(/\d+/) : null;
          const refId = refIdMatch ? parseInt(refIdMatch[0], 10) : null;
          const ref = refId
            ? `<button type="button" class="wal-ref wal-ref-click act-btn" data-action="get_order_detail" data-params='{"order_id":${refId}}' data-label="Заказ ORD-${refId}">ORD-${refId}</button>`
            : '';
          return `<div class="wal-row">
            <span class="wal-time">${esc(t.time)}</span>
            <span class="wal-desc">${esc(t.label || '')} ${ref}</span>
            <span class="wal-amt ${cls}">${sign}${fmt(t.amount)}</span>
          </div>`;
        }).join('');
        return `<div class="wal-group">
          <div class="wal-date">${esc(date)}</div>
          <div class="wal-rows">${rows}</div>
        </div>`;
      }).join('');
      return `<div class="card wal-card">
        <div class="wal-head">
          <div class="wal-head-lbl">К выплате (на счёте)</div>
          <div class="wal-head-amount">
            <span class="wal-head-ccy">$</span>${esc(whole)}<span class="wal-head-dec">.${esc(dec)}</span>
            <span class="wal-head-curr">${esc(d.currency || 'USD')}</span>
          </div>
        </div>
        <div class="sr-stats">
          <div class="sr-stat card-clickable" data-action="get_orders" data-params='{"filter":"active"}' data-label="Заказы в работе"><div class="sr-stat-lbl">В работе</div><div class="sr-stat-val">${fmt(d.revenue_in_work)}</div><div class="sr-stat-hint">резерв оплачен, ждём 90%</div></div>
          <div class="sr-stat card-clickable" data-action="get_orders" data-params='{}' data-label="Заказы, ждём оплату"><div class="sr-stat-lbl">Ждём оплату</div><div class="sr-stat-val">${fmt(d.revenue_pending)}</div><div class="sr-stat-hint">${d.open_count} заказов</div></div>
          <div class="sr-stat card-clickable" data-action="get_analytics" data-params='{}' data-label="Аналитика выручки"><div class="sr-stat-lbl">Зачислено всего</div><div class="sr-stat-val wal-amt-in">${fmt(d.revenue_paid)}</div><div class="sr-stat-hint">за всё время</div></div>
        </div>
        ${groupHtml ? `<div class="wal-txs-head">Последние поступления</div>${groupHtml}` : '<div class="wal-empty">Поступлений пока не было.</div>'}
      </div>`;
    },
    onboarding_progress(d) {
      const cur = Number(d.current || 1);
      const total = Number(d.total || 5);
      const pct = Math.round(cur * 100 / total);
      const steps = (d.steps || []).map(s => {
        const icon = s.state === 'done' ? '✓'
                   : s.state === 'current' ? s.n
                   : s.n;
        return `<div class="obp-step obp-step-${esc(s.state)}">
          <div class="obp-step-dot">${esc(String(icon))}</div>
          <div class="obp-step-lbl">${esc(s.label)}</div>
        </div>`;
      }).join('');
      return `<div class="card obp-card">
        <div class="obp-head">
          <div class="obp-head-lbl">${esc(d.title || 'Onboarding')}</div>
          <div class="obp-head-status">${esc(d.status || '')}</div>
        </div>
        <div class="obp-progress">
          <div class="obp-progress-num"><span class="obp-progress-cur">${cur}</span>/${total}</div>
          <div class="obp-progress-bar"><div class="obp-progress-fill" style="width:${pct}%"></div></div>
          <div class="obp-progress-lbl">${esc(d.current_label || '')}</div>
        </div>
        <div class="obp-steps">${steps}</div>
      </div>`;
    },
    inbox(d) {
      const sections = (d.sections || []).map(s => {
        const rows = (s.rows || []).map(r => {
          const a = r.action;
          const btn = a
            ? `<button class="act-btn ib-btn" data-action="${esc(a.action)}" data-params='${esc(JSON.stringify(a.params || {}))}' data-label="${esc(a.label)}">${esc(a.label)}</button>`
            : '';
          // Вся строка кликабельна (не только кнопка) — открывает то же действие.
          // Клик по кнопке обрабатывается её ближайшим [data-action] (без дабл-триггера).
          const rowClick = a
            ? ` data-action="${esc(a.action)}" data-params='${esc(JSON.stringify(a.params || {}))}' data-label="${esc(r.title || a.label || '')}" role="button" tabindex="0"`
            : '';
          return `<div class="ib-row${a ? ' ib-row-click' : ''}"${rowClick}>
            <div class="ib-main">
              <div class="ib-title">${esc(r.title || '')}</div>
              <div class="ib-sub">${esc(r.subtitle || '')}</div>
            </div>
            ${btn}
          </div>`;
        }).join('');
        return `<div class="ib-section">
          <div class="ib-section-head">
            <span class="ib-section-icon">${esc(s.icon || '•')}</span>
            <span class="ib-section-title">${esc(s.title || '')}</span>
            <span class="ib-section-count">${(s.rows||[]).length}</span>
          </div>
          ${rows}
        </div>`;
      }).join('');
      return `<div class="card ib-card">
        <div class="card-title">${esc(d.title || tr('card.today'))}</div>
        ${sections}
      </div>`;
    },
    catalog(d) {
      const rows = (d.rows || []).map(r => {
        const status = r.is_active ? 'cat-active' : 'cat-archived';
        const ccy = r.currency || 'USD';
        const stockBadge = r.stock_qty > 0
          ? `<span class="cat-chip cat-chip-green cat-stock-badge">${r.stock_qty} шт</span>`
          : '<span class="cat-chip cat-chip-gray cat-stock-badge">нет в наличии</span>';
        const sold = r.sold_qty
          ? `<span class="cat-chip cat-chip-blue">${r.sold_qty} продано</span>`
          : '';
        const rev = r.revenue ? `<span class="cat-chip cat-chip-gray">${fmtMoney(r.revenue, ccy)}</span>` : '';
        const condBadge = r.condition ? `<span class="cat-chip cat-chip-gray">${esc(r.condition)}</span>` : '';
        const toggle = `<button class="act-btn cat-btn-mini" data-action="toggle_product" data-params='${esc(JSON.stringify({part_id: r.id}))}' data-label="Скрыть/показать">${r.is_active ? '🚫 Скрыть' : '✓ Активировать'}</button>`;

        // Раскрываемая «портянка» с полными данными позиции.
        // Все поля показываем, даже пустые (с «—»), чтобы продавец видел
        // что есть в карточке, а что надо дозаполнить.
        // Все поля детали редактируемые (data-field → имя параметра edit_product).
        // «Продано»/оборот — read-only (считается из заказов).
        const ti = (field, val) => `<input class="cat-edit-field" data-field="${field}" value="${esc(val != null && val !== '—' ? String(val) : '')}" />`;
        const ni = (field, val, step) => `<input class="cat-edit-field" type="number" min="0" step="${step || '0.01'}" data-field="${field}" value="${val != null ? esc(String(val)) : ''}" />`;
        const opts = (map, sel) => Object.keys(map).map(v => `<option value="${v}"${sel === v ? ' selected' : ''}>${esc(map[v])}</option>`).join('');
        const sel = (field, map, val) => `<select class="cat-edit-field" data-field="${field}">${opts(map, val)}</select>`;
        // GBP убран: бэкенд-FX знает только эти 4 валюты, иначе цена конвертится 1:1.
        const CCY = {USD: 'USD', EUR: 'EUR', RUB: 'RUB', CNY: 'CNY'};
        const details = `
          <div class="cat-details">
            <div><span class="cat-dl">Артикул (OEM):</span> ${ti('oem_number', r.article)}</div>
            <div><span class="cat-dl">Кросс-номера:</span> ${ti('cross_numbers', r.cross_numbers)}</div>
            <div><span class="cat-dl">Название:</span> ${ti('title', r.title)}</div>
            <div><span class="cat-dl">Бренд:</span> ${ti('brand', r.brand)}</div>
            <div><span class="cat-dl">Завод-производитель:</span> ${ti('manufacturer', r.manufacturer)}</div>
            <div><span class="cat-dl">Состояние:</span> ${sel('condition', {oem: 'OEM', aftermarket: 'Aftermarket', reman: 'REMAN'}, r.condition || 'oem')}</div>
            <div><span class="cat-dl">Наличие:</span> ${sel('availability', {in_stock: 'В наличии', backorder: 'Под заказ'}, r.availability || 'in_stock')}</div>
            <div class="cat-edit-line"><span class="cat-dl">Остаток:</span> <input class="cat-edit-field cat-edit-stock" type="number" min="0" step="1" data-field="stock_qty" value="${r.stock_qty || 0}" /> шт <span class="cat-edit-hint">&gt; 0 — в наличии</span></div>
            <div class="cat-edit-line"><span class="cat-dl">Цена EXW:</span> ${sel('currency', CCY, r.currency || 'USD')} <input class="cat-edit-field cat-edit-price" type="number" min="0" step="0.01" data-field="price" value="${r.price != null ? r.price : ''}" /></div>
            <div class="cat-edit-line"><span class="cat-dl">Цена FOB:</span> SEA ${ni('price_fob_sea', r.price_fob_sea)} · AIR ${ni('price_fob_air', r.price_fob_air)}</div>
            <div><span class="cat-dl">Морпорт:</span> ${ti('sea_port', r.sea_port)}</div>
            <div><span class="cat-dl">Аэропорт:</span> ${ti('air_port', r.air_port)}</div>
            <div><span class="cat-dl">Адрес склада:</span> ${ti('warehouse_address', r.warehouse)}</div>
            <div class="cat-edit-line"><span class="cat-dl">Вес:</span> ${ni('weight', r.weight_kg, '0.001')} кг</div>
            <div class="cat-edit-actions"><button class="cat-save-edit" data-part-id="${r.id}" data-ccy="${esc(ccy)}">💾 Сохранить</button> <span class="cat-edit-note">меняется вручную, на ваше усмотрение</span></div>
            <div><span class="cat-dl">Продано:</span> ${r.sold_qty || 0} шт · оборот ${fmtMoney(r.revenue || 0, ccy)}</div>
          </div>`;

        return `<details class="cat-row ${status}">
          <summary class="cat-row-summary">
            <div class="cat-row-main">
              <div class="cat-art">${esc(r.article || '')}</div>
              <div class="cat-name">${esc(r.title || '')}</div>
              <div class="cat-brand">${esc(r.brand || '')}${r.manufacturer && r.manufacturer !== r.brand ? ' · ' + esc(r.manufacturer) : ''}${r.warehouse_name ? ' · 📁 ' + esc(r.warehouse_name) : ''}</div>
            </div>
            <div class="cat-row-meta">
              <span class="cat-price">${fmtMoney(r.price, ccy)}</span>
              ${condBadge}${stockBadge}${sold}${rev}
            </div>
            <div class="cat-row-actions">${toggle}</div>
          </summary>
          ${details}
        </details>`;
      }).join('');
      const counter = (d.total_count && d.shown_end)
        ? `<span class="cat-counter">${d.offset + 1}–${d.shown_end} из ${d.total_count}</span>` : '';
      // Поиск по артикулу: warehouse_id передаём числом или null (без кавычек —
      // иначе сломается inline-обработчик внутри атрибута).
      const whArg = (d.warehouse_id === null || d.warehouse_id === undefined || d.warehouse_id === '')
        ? 'null' : Number(d.warehouse_id);
      const curQ = (d.filter && d.filter.q) ? esc(d.filter.q) : '';
      const searchBar = `<div class="cat-search" style="display:flex;gap:8px;margin:4px 0 12px;flex-wrap:wrap;">
        <input type="text" class="cat-search-input" value="${curQ}" placeholder="🔎 Поиск по артикулу / названию — фильтрует при наборе…"
          style="flex:1;min-width:180px;box-sizing:border-box;padding:8px 12px;border-radius:8px;border:1px solid rgba(128,128,128,0.35);background:rgba(128,128,128,0.08);color:inherit;font-size:14px;"
          oninput="window.__catalogLiveSearch && window.__catalogLiveSearch(this,${whArg})"
          onkeydown="if(event.key==='Enter'){event.preventDefault();window.__catalogLiveSearch&&window.__catalogLiveSearch(this,${whArg},true);}">
        <button class="cat-search-btn" onclick="window.__catalogLiveSearch&&window.__catalogLiveSearch(this.parentNode.querySelector('.cat-search-input'),${whArg},true);"
          style="padding:8px 16px;border-radius:8px;border:none;background:#2D7A3E;color:#fff;font-size:14px;cursor:pointer;white-space:nowrap;">Найти</button>
        ${curQ ? `<button class="cat-search-clear" title="Сбросить поиск" onclick="var i=this.parentNode.querySelector('.cat-search-input');i.value='';window.__catalogLiveSearch&&window.__catalogLiveSearch(i,${whArg},true);"
          style="padding:8px 12px;border-radius:8px;border:1px solid rgba(128,128,128,0.35);background:transparent;color:inherit;cursor:pointer;">✕</button>` : ''}
      </div>`;
      return `<div class="card cat-card">
        <div class="card-title">${esc(d.title || tr('card.catalog'))} ${counter}</div>
        ${searchBar}
        <div class="cat-rows">${rows || '<div class="cat-empty">Ничего не найдено</div>'}</div>
      </div>`;
    },
    warehouses(d) {
      // Если карточка встроена в каталог (compact=true) — рендерим
      // горизонтальную ленту чипов, чтобы не оттягивать каталог вниз.
      if (d.compact) {
        const active = d.active_id == null ? null : Number(d.active_id);
        const chips = (d.rows || []).map(r => {
          const flag = ({'TR':'🇹🇷','CN':'🇨🇳','RU':'🇷🇺','AE':'🇦🇪','NL':'🇳🇱','KZ':'🇰🇿'}[r.country_code] || '🌍');
          const wid = r.is_orphan ? 0 : r.id;
          const isActive = (active === wid);
          const stale = r.staleness && r.staleness !== 'unknown' && !r.is_orphan
            ? `<span class="wh-chip-stale wh-stale-${r.staleness}"></span>` : '';
          return `<button class="wh-chip${r.is_orphan ? ' wh-chip-orphan' : ''}${isActive ? ' wh-chip-active' : ''}" data-action="seller_catalog" data-params='${esc(JSON.stringify({warehouse_id: wid}))}' data-label="Открыть склад">
            <span class="wh-chip-flag">${flag}</span>
            <span class="wh-chip-name">${esc(r.name)}</span>
            <span class="wh-chip-n">${r.parts_count}</span>
            ${stale}
          </button>`;
        }).join('');
        const allActive = (active == null);
        const allBtn = `<button class="wh-chip wh-chip-all${allActive ? ' wh-chip-active' : ''}" data-action="seller_catalog" data-params='${esc(JSON.stringify({}))}' data-label="Все товары">
          <span class="wh-chip-flag">📦</span><span class="wh-chip-name">Все товары</span>
        </button>`;
        return `<div class="wh-bar">
          <div class="wh-bar-label">${esc(d.title || tr('card.warehouses'))}</div>
          <div class="wh-bar-chips">${allBtn}${chips}</div>
        </div>`;
      }
      const rows = (d.rows || []).map(r => {
        const ports = [r.sea_port, r.air_port].filter(Boolean).join(' / ') || '—';
        const flag = ({'TR':'🇹🇷','CN':'🇨🇳','RU':'🇷🇺','AE':'🇦🇪','NL':'🇳🇱','KZ':'🇰🇿'}[r.country_code] || '🌍');
        const wid = r.is_orphan ? 0 : r.id;
        // Вся карточка кликабельна — открывает каталог этого склада
        const openParams = JSON.stringify({warehouse_id: wid});
        // Иконки управления — приглушены, проявляются на hover карточки
        const renameIcon = !r.is_orphan
          ? `<button class="wh-icon" title="Переименовать" onclick="event.stopPropagation();window.renameWarehouse && window.renameWarehouse(${r.id}, ${JSON.stringify(r.name).replace(/"/g,'&quot;')})">✏️</button>`
          : '';
        const refreshIcon = !r.is_orphan
          ? `<button class="wh-icon" title="Обновить прайс" onclick="event.stopPropagation();window.refreshWarehousePrice && window.refreshWarehousePrice(${r.id}, ${JSON.stringify(r.name).replace(/"/g,'&quot;')})">🔄</button>`
          : '';
        const deleteIcon = !r.is_orphan
          ? `<button class="wh-icon wh-icon-danger" title="Удалить склад" onclick="event.stopPropagation();window.deleteWarehouse && window.deleteWarehouse(${r.id}, ${JSON.stringify(r.name).replace(/"/g,'&quot;')}, ${r.parts_count})">🗑</button>`
          : '';
        const staleBadge = r.stale_label && r.staleness !== 'unknown' && !r.is_orphan
          ? `<span class="wh-stale wh-stale-${r.staleness}" title="Последняя загрузка: ${esc(r.last_import || '—')}">${esc(r.stale_label)}</span>`
          : '';
        return `<div class="wh-row${r.is_orphan ? ' wh-orphan' : ''} wh-row-${r.staleness || ''}" data-action="seller_catalog" data-params='${esc(openParams)}' data-label="Открыть склад">
          <div class="wh-head">
            <span class="wh-flag">${flag}</span>
            <div class="wh-main">
              <div class="wh-name">${esc(r.name)}</div>
              <div class="wh-meta">${esc(ports)}${r.address ? ' · ' + esc(r.address.slice(0,80)) : ''}</div>
              ${staleBadge ? `<div class="wh-staleness">${staleBadge}</div>` : ''}
            </div>
            <div class="wh-stats">
              <span class="wh-count">${r.parts_count} поз.</span>
              ${r.currency ? `<span class="wh-ccy">${esc(r.currency)}</span>` : ''}
              ${refreshIcon}${renameIcon}${deleteIcon}
            </div>
          </div>
        </div>`;
      }).join('');
      return `<div class="card wh-card">
        <div class="card-title">${esc(d.title || tr('card.warehouses'))}</div>
        <div class="wh-rows">${rows || '<div class="cat-empty">Складов нет</div>'}</div>
      </div>`;
    },
    best_offers(d) {
      const rows = (d.rows || []).map((r, idx) => {
        const ccy = r.currency || 'USD';
        const rank = `<span class="bo-rank">${idx + 1}</span>`;
        const ratingBadge = `<span class="bo-rating bo-status-${r.status}" title="Статус: ${esc(r.status_badge)}">${esc(r.status_badge)} · ${(r.rating || 0).toFixed(0)}</span>`;
        const fobBits = [];
        if (r.price_fob_sea) fobBits.push(`SEA ${fmtMoney(r.price_fob_sea, ccy)}`);
        if (r.price_fob_air) fobBits.push(`AIR ${fmtMoney(r.price_fob_air, ccy)}`);
        const drilldown = `<button class="act-btn bo-btn" data-action="buyer_offer_compare" data-params='${esc(JSON.stringify({oem_number: r.oem_number}))}' data-label="Сравнить">🔍 Сравнить</button>`;
        return `<div class="bo-row">
          <div class="bo-head">
            ${rank}
            <div class="bo-main">
              <div class="bo-oem">${esc(r.oem_number || '')}</div>
              <div class="bo-title">${esc(r.title || '')}</div>
              <div class="bo-brand">${esc(r.brand || '')}</div>
            </div>
            <div class="bo-meta">
              <span class="bo-price">${fmtMoney(r.price, ccy)}</span>
              ${ratingBadge}
            </div>
          </div>
          <div class="bo-sub">
            <span class="bo-supplier">${esc(r.supplier_label)}</span>
            ${fobBits.length ? `<span class="bo-fob">${fobBits.join(' · ')}</span>` : ''}
            ${r.sea_port || r.air_port ? `<span class="bo-port">${esc([r.sea_port, r.air_port].filter(Boolean).join(' / '))}</span>` : ''}
            ${r.condition ? `<span class="bo-cond">${esc(r.condition)}</span>` : ''}
          </div>
          <div class="bo-actions">${drilldown}</div>
        </div>`;
      }).join('');
      return `<div class="card bo-card">
        <div class="card-title">${esc(d.title || tr('card.best_offers'))}</div>
        <div class="bo-rows">${rows || '<div class="cat-empty">Нет предложений</div>'}</div>
      </div>`;
    },
    offer_compare(d) {
      const rows = (d.rows || []).map((r, idx) => {
        const ccy = r.currency || 'USD';
        const shipSea = r.ship_sea_cost ? fmtMoney(r.ship_sea_cost, 'USD') + (r.ship_sea_days ? ` · ${r.ship_sea_days}д` : '') : '—';
        const shipAir = r.ship_air_cost ? fmtMoney(r.ship_air_cost, 'USD') + (r.ship_air_days ? ` · ${r.ship_air_days}д` : '') : '—';
        const landed = r.landed_cost ? `<b>${fmtMoney(r.landed_cost, ccy)}</b>` : '—';
        const bestMode = r.ship_best_mode === 'air' ? '✈️' : (r.ship_best_mode === 'sea' ? '🚢' : '');
        return `<tr class="oc-row oc-status-${r.status}">
          <td class="oc-rank">${idx + 1}</td>
          <td class="oc-supplier">${esc(r.supplier_label)}<br><span class="oc-status">${esc(r.status_badge)}</span></td>
          <td class="oc-rating">${(r.rating || 0).toFixed(0)}</td>
          <td class="oc-price">${fmtMoney(r.price, ccy)}</td>
          <td>${shipSea}</td>
          <td>${shipAir}</td>
          <td class="oc-landed">${landed} ${bestMode}</td>
          <td>${esc(r.condition || '—')}</td>
          <td>${r.stock || 0}</td>
          <td class="oc-score">${((r.score || 0) * 100).toFixed(0)}</td>
        </tr>`;
      }).join('');
      return `<div class="card oc-card">
        <div class="card-title">${esc(d.title || tr('card.comparison'))}</div>
        <div class="oc-legend">🟢 Надёжный · 🟡 Песочница · 🟠 Рисковый — рейтинг 0–100 · Доставка считается по весу/габаритам · Landed = EXW + лучшая доставка</div>
        <div class="oc-scroll"><table class="oc-table">
          <thead><tr>
            <th>#</th><th>Поставщик</th><th>Рейтинг</th><th>EXW</th>
            <th>🚢 Доставка</th><th>✈️ Доставка</th>
            <th>Landed</th>
            <th>Условие</th><th>Остаток</th><th>Score</th>
          </tr></thead>
          <tbody>${rows || '<tr><td colspan="10" class="cat-empty">Поставщиков нет</td></tr>'}</tbody>
        </table></div>
      </div>`;
    },
    audit_timeline(d) {
      const events = (d.events || []).map(e => {
        const change = (e.before != null && e.after != null)
          ? `<span class="atl-before">${esc(e.before)}</span><span class="atl-arrow">→</span><span class="atl-after">${esc(e.after)}</span>`
          : (e.delta ? `<span class="atl-after">${esc(e.delta)}</span>` : '');
        return `<div class="atl-row atl-tone-${esc(e.tone || 'gray')}">
          <div class="atl-icon">${esc(e.icon || '•')}</div>
          <div class="atl-body">
            <div class="atl-line1">
              <span class="atl-title">${esc(e.title || '')}</span>
              ${change}
            </div>
            <div class="atl-line2">
              <span class="atl-actor atl-actor-${esc(e.actor_color || 'gray')}">${esc(e.actor_role_label || '')}</span>
              <span class="atl-actor-name">${esc(e.actor || '')}</span>
              <span class="atl-when">${esc(e.when_short || '')}</span>
            </div>
          </div>
        </div>`;
      }).join('');
      return `<div class="card atl-card">
        <div class="card-title">${esc(d.title || tr('card.history'))}</div>
        <div class="atl-list">${events || '<div class="atl-empty">Событий пока нет</div>'}</div>
      </div>`;
    },
    list(d) {
      const rows = (d.rows || d.items || []).map(r => {
        // Badge может быть строкой (старый формат) или объектом {label, tone}.
        let badge = '';
        if (r.badge) {
          if (typeof r.badge === 'object') {
            badge = `<span class="ls-badge ls-badge-${esc(r.badge.tone || 'info')}">${esc(r.badge.label || '')}</span>`;
          } else {
            badge = `<span class="ls-badge">${esc(r.badge)}</span>`;
          }
        }
        const isClickable = !!(r.url || r.action);
        // tone — окрашивает левый бордер строки (warn/ok/info/bad).
        const toneCls = r.tone ? ` ls-tone-${esc(r.tone)}` : '';
        const cls = (isClickable ? 'ls-row ls-link' : 'ls-row') + toneCls;
        let attrs = '';
        if (r.action) {
          attrs = `class="${cls}" data-action="${esc(r.action)}" data-params='${esc(JSON.stringify(r.params || {}))}' data-label="${esc(r.title || r.action)}"`;
        } else if (r.url) {
          attrs = `class="${cls}" onclick="window.open('${esc(r.url)}','_blank','noopener')"`;
        } else {
          attrs = `class="${cls}"`;
        }
        // Если это чеклист (checkbox: True) — обрабатываем toggle ИНЛАЙН
        // (без re-render всей карточки + не скроллим вниз).
        if (r.checkbox) {
          const checkedAttr = r.checked ? ' ls-checkbox-on' : '';
          const titleDone = r.checked ? ' ls-title-done' : '';
          const subDone = r.checked ? 'Проверено' : 'Кликнуть → отметить как проверено';
          const inlineHandler = r.action
            ? `onclick="event.preventDefault();event.stopPropagation();window.toggleChecklistRow&&window.toggleChecklistRow(this,'${esc(r.action)}','${esc(JSON.stringify(r.params || {}))}')"`
            : '';
          return `<div class="ls-row ls-link ls-row-check${toneCls}" ${inlineHandler}>
            <span class="ls-checkbox${checkedAttr}" aria-hidden="true">${r.checked ? '✓' : ''}</span>
            <div class="ls-main">
              <div class="ls-title${titleDone}">${esc(r.title || '')}</div>
              <div class="ls-sub">${esc(subDone)}</div>
            </div>
          </div>`;
        }
        // Опциональный мини-прогресс под subtitle (для supplier_tracks).
        const pct = (typeof r.progress_pct === 'number')
          ? Math.max(0, Math.min(100, r.progress_pct)) : null;
        const progressHtml = (pct !== null) ? `
          <div class="ls-progress"><div class="ls-progress-fill" style="width:${pct}%"></div></div>
          ${r.progress_meta ? `<div class="ls-progress-meta">${esc(r.progress_meta)}</div>` : ''}
        ` : '';
        // Опциональные stages-пилюли (для заказов в поставке).
        const stagesHtml = (Array.isArray(r.stages) && r.stages.length)
          ? `<div class="ls-stages">${r.stages.map(s =>
              `<div class="ls-stage${s.done ? ' done' : ''}" title="${esc(s.label||'')}">${esc(s.label||'')}</div>`
            ).join('')}</div>`
          : '';
        return `<div ${attrs}>
          <div class="ls-main">
            <div class="ls-title">${esc(r.title || '')}</div>
            <div class="ls-sub">${esc(r.subtitle || '')}</div>
            ${progressHtml}
            ${stagesHtml}
          </div>
          ${badge}
        </div>`;
      }).join('');
      // Collapsible mode: карточка свёрнута, разворачивается по клику на title.
      // Activated если d.collapsible === true.
      if (d.collapsible) {
        const collapsedInit = (d.collapsed !== false);  // default = collapsed
        const stateAttr = collapsedInit ? 'data-collapsed="1"' : 'data-collapsed="0"';
        const chev = collapsedInit ? '▸' : '▾';
        const itemsCount = (d.rows || d.items || []).length;
        const countBadge = itemsCount ? ` <span class="ls-coll-count">${itemsCount}</span>` : '';
        return `<div class="card ls-card ls-collapsible" ${stateAttr}>
          <div class="card-title ls-coll-head" role="button" tabindex="0"
               onclick="window.__toggleCollapsibleCard&amp;&amp;window.__toggleCollapsibleCard(this)"
               onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();window.__toggleCollapsibleCard&amp;&amp;window.__toggleCollapsibleCard(this);}">
            <span class="ls-coll-chev">${chev}</span>
            <span class="ls-coll-title">${esc(d.title || tr('card.list'))}</span>${countBadge}
          </div>
          <div class="ls-rows ls-coll-body" ${collapsedInit ? 'style="display:none;"' : ''}>${rows || '<div class="ls-empty">Пусто</div>'}</div>
        </div>`;
      }
      return `<div class="card ls-card">
        <div class="card-title">${esc(d.title || tr('card.list'))}</div>
        <div class="ls-rows">${rows || '<div class="ls-empty">Пусто</div>'}</div>
      </div>`;
    },
    quote_form(d) {
      // Квот-карточка как Excel/Stripe checkout: шапка с контекстом RFQ,
      // живой ИТОГО в чёрной полосе, табличный список позиций (артикул /
      // товар / кол-во / цена / сумма), сервис-поля внизу, submit.
      // d: {
      //   rfq_id, mode_badge, urgency_label, urgency_tone,
      //   customer_name, company_name, request_text,
      //   items: [{rfq_item_id, article, title, brand, quantity, unit_price, currency}],
      //   delivery_days, valid_days, message,
      //   parent_quote_id, direction,
      // }
      const items = d.items || [];
      const rfqId = String(d.rfq_id || '');
      const sv = !!d.seller_view;
      // Бейдж надёжности продавца «Надёжный · 90.0» → label + рейтинг отдельно.
      const _bp = String(d.seller_badge || 'Надёжный · 90').split(' · ');
      const supBadge = _bp[0] || 'Надёжный', supRating = _bp[1] || '';
      // Позиции в форме данных карточки заказа (spec_results).
      const specItems = items.map((it) => ({
        // status НЕ 'not_found' — иначе строка без OEM рендерится как
        // «— нет предложений —». Наличие задаём через stock_label/class.
        status: 'in_stock',
        stock_label: it.in_catalog ? 'В наличии' : 'нет в каталоге',
        stock_class: it.in_catalog ? 'in' : 'no',
        in_catalog: !!it.in_catalog,   // для кнопки «Только мои позиции»
        id: it.article || '',
        name: it.title || '',
        brand: it.brand || '',
        price: Number(it.unit_price || 0),
        qty: Number(it.quantity || 0),
        weight: (+it.weight) ? ((+it.weight).toFixed(3).replace(/0+$/, '').replace(/\.$/, '')) + ' кг' : '',
        currency: it.currency || 'USD',
        supplier_status: sv ? (d.seller_status || 'trusted') : '',
        supplier_status_badge: sv ? supBadge : '',
        supplier_rating: sv ? supRating : '',
        ship_mode: d.shipping_mode || '',   // способ доставки → колонка «Доставка»
        rfq_item_id: it.rfq_item_id,
        // Подсказки котировки: рыночный ориентир, цена из каталога, аналог.
        market_usd: it.market_usd,
        market_lo: it.market_lo,
        market_hi: it.market_hi,
        catalog_usd: it.catalog_usd,
        analog: it.analog || null,
      }));
      const nCat = items.filter((it) => it.in_catalog).length;
      const total = items.reduce((s, it) => s + Number(it.quantity || 0) * Number(it.unit_price || 0), 0);
      const meta = [
        {label: 'RFQ', value: '#' + rfqId},
        {label: 'Режим', value: String(d.mode_badge || 'Стандарт')},
        {label: 'Срочность', value: String(d.urgency_label || 'Standard')},
        {label: 'Покупатель', value: String(d.customer_name || 'Покупатель')},
        {label: 'Куда', value: String(d.destination || '\u{1F1F7}\u{1F1FA} Россия (импорт)')},
        {label: 'Котировка действует', value: (d.valid_days || 7) + ' дн'},
      ];
      if (d.created_at) meta.push({label: 'Создан', value: String(d.created_at)});
      // Рендерим ТЕМ ЖЕ рендером, что и заказ (spec_results) — гарантированно
      // одинаковая вёрстка; единственное отличие — колонка Price редактируемая.
      return this.spec_results({
        title: '\u{1F4AC} Котировка по RFQ #' + rfqId,
        title_meta: items.length + ' позиций' + (d.request_text ? ' · ' + d.request_text : ''),
        meta: meta,
        found: nCat,
        analogue: items.length,
        not_found: items.length - nCat,
        kpi_labels: sv ? ['В каталоге', 'Всего', 'Нет в каталоге'] : null,
        items: specItems,
        shipping_mode: d.shipping_mode || '',
        total: total,
        currency: 'USD',
        foot_info: items.length + ' позиций · обновляется при редактировании цен',
        editable_price: true,
        qf: {
          rfq_id: rfqId,
          delivery_days: d.delivery_days || 14,
          valid_days: d.valid_days || 7,
          parent_quote_id: d.parent_quote_id || '',
          direction: d.direction || 'seller_to_buyer',
          speed: d.seller_speed || null,   // честная метрика скорости ответа
          rfq_age: d.rfq_age || '',
        },
      });
    },
    smart_question(d) {
      // Безопасный рендер questionnaire-вопроса. Все значения экранируются.
      // d: {question, options:[…], default, placeholder, q_idx, total, field,
      //     apply_as, suggestions_source: 'sea'|'air'|'city'|'',
      //     country_picker: bool, shipment_country: 'CN'|'RU'|...}
      // render:'select' → комбобокс на нашем кастомном автокомплите (.pl-ac):
      // можно сразу печатать (substring-фильтр), а аккуратный скроллящийся
      // попап .pl-ac-pop показывает список (нативный <datalist> растягивался
      // на весь экран и не скроллился). Любое введённое значение принимается.
      const useSelect = (d.render === 'select') && (d.options || []).length;
      const chips = (useSelect ? '' : (d.options || []).map(opt =>
        `<button class="sq-chip" type="button" data-answer="${esc(String(opt))}">${esc(String(opt))}</button>`
      ).join(''));
      let comboClass = '';
      let comboExtra = '';
      if (useSelect) {
        // Список опций кладём в data-атрибут — провайдер _acItemsFor читает его.
        const optsAttr = (d.options || []).map(o => String(o)).join('|');
        comboClass = ' pl-ac';
        comboExtra = ' data-ac-kind="manuf" data-ac-options="' + esc(optsAttr) + '" autocomplete="off"';
      }
      const idx = Number(d.q_idx || 0);
      const total = Number(d.total || 1);
      const defVal = d.default != null ? String(d.default) : '';
      const placeholder = d.placeholder || 'или впишите свой вариант...';
      const sugSrc = String(d.suggestions_source || '');
      const selectedCountry = String(d.shipment_country || '');
      // Динамический placeholder для port-вопросов: если страна выбрана и
      // есть наши подсказки для неё — подменяем общую "выберите из списка"
      // на конкретные примеры из этой страны.
      let dynamicPlaceholder = '';
      if ((sugSrc === 'sea' || sugSrc === 'air') && selectedCountry
          && PORTS_PLACEHOLDER_BY_COUNTRY[selectedCountry]
          && PORTS_PLACEHOLDER_BY_COUNTRY[selectedCountry][sugSrc]) {
        dynamicPlaceholder = PORTS_PLACEHOLDER_BY_COUNTRY[selectedCountry][sugSrc];
      }
      // Фильтруем подсказки по выбранной стране, если она есть и для неё
      // в SUGGEST_BY_COUNTRY есть список. Иначе fallback на общемировой.
      let sugList = [];
      if (sugSrc) {
        if (selectedCountry && SUGGEST_BY_COUNTRY[selectedCountry]
            && SUGGEST_BY_COUNTRY[selectedCountry][sugSrc]) {
          sugList = SUGGEST_BY_COUNTRY[selectedCountry][sugSrc];
        } else {
          sugList = SUGGEST_BY_SOURCE[sugSrc] || [];
        }
      }
      let dlAttr = '';
      let dlHtml = '';
      if (sugList && sugList.length) {
        const dlId = 'sq-dl-' + (++__suggDatalistCounter);
        dlAttr = ' list="' + esc(dlId) + '"';
        dlHtml = '<datalist id="' + esc(dlId) + '">'
          + sugList.map(v => '<option value="' + esc(String(v)) + '"></option>').join('')
          + '</datalist>';
      }
      // Country + City picker: рендерим два <select> + текстовое поле для
      // улицы. Город заполняется на основе выбранной страны (если есть в
      // SUGGEST_BY_COUNTRY) + опция «Другой...» для свободного ввода.
      // На submit (см. ниже) три части склеиваются как
      // "{улица}, {город}, {страна}" → warehouse_address.
      let countryHtml = '';
      let cityHtml = '';
      let streetInputDl = dlAttr;     // для city-source — если есть город, datalist не нужен
      let useCustomPlaceholder = placeholder;
      if (d.country_picker) {
        const optTop = COUNTRIES_TOP.map(c =>
          '<option value="' + esc(c.code) + '"'
          + (c.code === selectedCountry ? ' selected' : '')
          + '>' + esc(c.flag + ' ' + c.name) + '</option>'
        ).join('');
        const optAll = COUNTRIES_ALL.map(c =>
          '<option value="' + esc(c.code) + '"'
          + (c.code === selectedCountry ? ' selected' : '')
          + '>' + esc(c.flag + ' ' + c.name) + '</option>'
        ).join('');
        const placeholderOpt = '<option value=""' + (selectedCountry ? '' : ' selected')
          + '>— выберите страну —</option>';
        countryHtml = '<select class="sq-country">'
          + placeholderOpt
          + '<optgroup label="Основные">' + optTop + '</optgroup>'
          + '<optgroup label="Все остальные">' + optAll + '</optgroup>'
          + '</select>';
        // City — combobox (input + datalist). Юзер либо выбирает из подсказок,
        // либо свободно вписывает свой город. При смене страны datalist
        // перерисовывается. Для стран без списка остаётся пустой datalist —
        // юзер просто печатает свой город.
        const cityDlId = 'sq-city-dl-' + (++__suggDatalistCounter);
        let cityDlOpts = '';
        if (selectedCountry && SUGGEST_BY_COUNTRY[selectedCountry]
            && SUGGEST_BY_COUNTRY[selectedCountry].city) {
          cityDlOpts = SUGGEST_BY_COUNTRY[selectedCountry].city.map(c =>
            '<option value="' + esc(String(c)) + '"></option>'
          ).join('');
        }
        cityHtml = '<input class="sq-city" type="text" list="' + esc(cityDlId)
          + '" placeholder="город (или впишите свой)"/>'
          + '<datalist id="' + esc(cityDlId) + '">' + cityDlOpts + '</datalist>';
        // Когда city picker есть — основное поле это улица, datalist не нужен.
        streetInputDl = '';
        useCustomPlaceholder = 'улица, № дома';
      }
      return `<div class="smart-q-card card"
          data-q-idx="${esc(String(idx))}"
          data-field="${esc(String(d.field || ''))}"
          data-apply-as="${esc(String(d.apply_as || 'constant'))}">
        <div class="sq-step">${idx + 1} / ${total}</div>
        <div class="sq-q">${esc(String(d.question || ''))}</div>
        ${d.hint ? `<div class="sq-hint" style="font-size:13px;line-height:1.4;opacity:.72;margin:-2px 0 10px;">${esc(String(d.hint))}</div>` : ''}
        ${chips ? `<div class="sq-chips">${chips}</div>` : ''}
        <div class="sq-input-row">
          ${countryHtml}
          ${cityHtml}
          <input class="sq-input${comboClass}" type="text" placeholder="${esc(dynamicPlaceholder || useCustomPlaceholder)}" value="${esc(defVal)}"${streetInputDl}${comboExtra}/>
          ${d.country_picker ? '' : dlHtml}
          <button class="sq-submit" type="button">→</button>
          ${d.skippable ? '<button class="sq-skip" type="button">Пропустить</button>' : ''}
        </div>
      </div>`;
    },
    seller_status(d) {
      // Дашборд-карточка статуса поставщика: tier + рейтинг прогресс-бар,
      // 3 hero-метрики, вторичные метрики, аудит-строка.
      // d: {company_name, tier, tier_tone, score, score_max,
      //     verified_label, risk_label, risk_tone, jurisdiction, verified_at,
      //     hero:[{label, value, tone, sub}], secondary:[{label, value, sub}]}
      const pct = Math.max(0, Math.min(100, Number(d.score) || 0));
      const tierTone = String(d.tier_tone || 'info');
      const riskTone = String(d.risk_tone || 'info');
      const heroHtml = (d.hero || []).map(h => `
        <div class="ss-hero-cell ss-tone-${esc(String(h.tone || 'info'))}">
          <div class="ss-hero-label">${esc(h.label || '')}</div>
          <div class="ss-hero-value">${esc(String(h.value ?? '—'))}</div>
          ${h.sub ? `<div class="ss-hero-sub">${esc(h.sub)}</div>` : ''}
        </div>`).join('');
      const secondaryHtml = (d.secondary || []).map(s => `
        <div class="ss-sec-row">
          <div class="ss-sec-label">${esc(s.label || '')}</div>
          <div class="ss-sec-value">${esc(String(s.value ?? '—'))}</div>
          ${s.sub ? `<div class="ss-sec-sub">${esc(s.sub)}</div>` : ''}
        </div>`).join('');
      return `<div class="card ss-card">
        <div class="ss-head">
          <div class="ss-head-l">
            <div class="ss-head-cap">Статус поставщика</div>
            <div class="ss-head-company">${esc(d.company_name || '—')}</div>
          </div>
          <div class="ss-head-r">
            ${d.verified_label ? `<div class="ss-badge ss-badge-ok">${esc(d.verified_label)}</div>` : ''}
            ${d.risk_label ? `<div class="ss-badge ss-badge-${esc(riskTone)}">${esc(d.risk_label)}</div>` : ''}
          </div>
        </div>
        <div class="ss-tier">
          <div class="ss-tier-row">
            <div>
              <div class="ss-tier-cap">Tier</div>
              <div class="ss-tier-name ss-tone-${esc(tierTone)}">${esc(d.tier || '—')}</div>
            </div>
            <div class="ss-tier-score">
              <span class="ss-tier-score-val">${Math.round(pct)}</span>
              <span class="ss-tier-score-max"> / ${esc(String(d.score_max || 100))}</span>
            </div>
          </div>
          <div class="ss-bar"><div class="ss-bar-fill ss-tone-${esc(tierTone)}" style="width:${pct}%"></div></div>
        </div>
        ${heroHtml ? `<div class="ss-hero">${heroHtml}</div>` : ''}
        ${secondaryHtml ? `<div class="ss-secondary">${secondaryHtml}</div>` : ''}
        ${(d.verified_at || d.jurisdiction) ? `<div class="ss-footer">
          ${d.jurisdiction ? `<span>🌍 ${esc(d.jurisdiction)}</span>` : ''}
          ${d.verified_at ? `<span>📅 верифицирована ${esc(d.verified_at)}</span>` : ''}
        </div>` : ''}
      </div>`;
    },
    tier_progress(d) {
      // Прогресс по тиру скидки: текущая скидка крупно, прогресс-бар,
      // лестница уровней с подсветкой текущего.
      // d: {title, current:{discount_pct, label, turnover_text},
      //     progress:{pct, current_text, target_text, gap_text, next_label},
      //     tiers:[{label, discount_pct, threshold_text, state}], // state: 'done'|'current'|'next'|'future'
      //     footer_text}
      const c = d.current || {};
      const p = d.progress || {};
      const pct = Math.max(0, Math.min(100, Number(p.pct) || 0));
      const tiersHtml = (d.tiers || []).map(t => {
        const state = t.state || 'future';
        const marker = state === 'done' ? '✓'
                     : state === 'current' ? '●'
                     : '○';
        return `<div class="tp-tier tp-tier-${esc(state)}">
          <div class="tp-tier-mark">${marker}</div>
          <div class="tp-tier-name">${esc(t.label)}</div>
          <div class="tp-tier-disc">${esc(t.discount_pct)}</div>
          <div class="tp-tier-thr">${esc(t.threshold_text)}</div>
        </div>`;
      }).join('');
      const progressBlock = p.pct !== undefined ? `
        <div class="tp-progress">
          <div class="tp-progress-head">
            <span>${esc(p.current_text || '')}</span>
            <span class="tp-progress-target">→ ${esc(p.target_text || '')}</span>
          </div>
          <div class="tp-bar"><div class="tp-bar-fill" style="width:${pct}%"></div></div>
          <div class="tp-progress-foot">${esc(p.gap_text || '')}${p.next_label ? ' до <b>' + esc(p.next_label) + '</b>' : ''}</div>
        </div>` : '';
      return `<div class="card tp-card">
        <div class="tp-head">
          <div class="tp-head-label">${esc(d.title || 'Auto-discount')}</div>
        </div>
        <div class="tp-hero">
          <div class="tp-hero-side">
            <div class="tp-hero-cap">Текущая скидка</div>
            <div class="tp-hero-pct">${esc(c.discount_pct || '0%')}</div>
            <div class="tp-hero-tier">${esc(c.label || '')}</div>
          </div>
          <div class="tp-hero-side tp-hero-side-right">
            <div class="tp-hero-cap">Годовой оборот</div>
            <div class="tp-hero-amount">${esc(c.turnover_text || '')}</div>
          </div>
        </div>
        ${progressBlock}
        ${tiersHtml ? `<div class="tp-tiers">
          <div class="tp-tiers-cap">Лестница тиров</div>
          ${tiersHtml}
        </div>` : ''}
        ${d.footer_text ? `<div class="tp-footer">${esc(d.footer_text)}</div>` : ''}
      </div>`;
    },
    invoice(d) {
      // Бухгалтерский invoice-документ. Стиль — официальный счёт на оплату:
      // белый лист, шапка с эмитентом, номер/дата справа, табличные секции,
      // подвал. Похоже на Stripe invoice / банковский payment slip.
      // d: {ref, amount_text, expires_text, issuer:{name,subtitle},
      //     meta:[{label,value}], sections:[{title, rows:[{label,value,copy,hint,warn,mono}]}],
      //     notes}
      const sectionsHtml = (d.sections || []).map(sec => {
        const rows = (sec.rows || []).map(r => {
          const copyBtn = r.copy
            ? `<button class="iv-copy" type="button" data-copy="${esc(String(r.value))}" title="Скопировать">⎘</button>`
            : '';
          const hint = r.hint ? `<div class="iv-hint">${esc(r.hint)}</div>` : '';
          const valCls = 'iv-value' + (r.mono ? ' iv-mono' : '') + (r.warn ? ' iv-warn' : '');
          return `<tr class="iv-row">
            <td class="iv-label">${esc(r.label)}</td>
            <td class="${valCls}">
              <span>${esc(String(r.value))}</span>${copyBtn}
              ${hint}
            </td>
          </tr>`;
        }).join('');
        return `<div class="iv-section">
          <div class="iv-section-bar">${esc(sec.title)}</div>
          <table class="iv-table">${rows}</table>
        </div>`;
      }).join('');
      const notes = (d.notes || []).map(n =>
        `<li>${esc(n)}</li>`
      ).join('');
      const issuer = d.issuer || {};
      const meta = (d.meta || []).map(m =>
        `<div class="iv-meta-row">
          <span class="iv-meta-label">${esc(m.label)}</span>
          <span class="iv-meta-value">${esc(String(m.value))}</span>
        </div>`
      ).join('');
      return `<div class="card iv-doc">
        <div class="iv-header">
          <div class="iv-issuer">
            <div class="iv-issuer-mark">🏛</div>
            <div>
              <div class="iv-issuer-name">${esc(issuer.name || 'Consolidator Parts')}</div>
              <div class="iv-issuer-sub">${esc(issuer.subtitle || 'B2B parts marketplace')}</div>
            </div>
          </div>
          <div class="iv-doc-meta">
            <div class="iv-doc-type">${esc(d.doc_type || 'INVOICE')}</div>
            ${meta}
          </div>
        </div>
        <div class="iv-total">
          <div class="iv-total-label">К ОПЛАТЕ</div>
          <div class="iv-total-amount">${esc(d.amount_text || '')}</div>
          ${d.expires_text ? `<div class="iv-total-expires">${esc(d.expires_text)}</div>` : ''}
        </div>
        ${d.ref ? `<div class="iv-ref-band">
          <div class="iv-ref-side">
            <div class="iv-ref-cap">PAYMENT REFERENCE</div>
            <div class="iv-ref-code">${esc(d.ref)}</div>
          </div>
          <div class="iv-ref-btns" style="display:flex;gap:8px;align-items:center;justify-content:flex-end;">
            <button class="iv-ref-copy" type="button" data-copy="${esc(d.ref)}" title="Скопировать">Копировать</button>
            ${d.pdf_url ? `<a class="iv-ref-copy iv-ref-dl" href="${esc(d.pdf_url)}" download title="Скачать инвойс в PDF" style="text-decoration:none;">⬇ Скачать</a>` : ''}
          </div>
          ${d.ref_warning ? `<div class="iv-ref-warn">⚠️ ${esc(d.ref_warning)}</div>` : ''}
        </div>` : ''}
        ${sectionsHtml}
        ${notes ? `<div class="iv-footer">
          <div class="iv-footer-title">Примечания</div>
          <ul class="iv-footer-notes">${notes}</ul>
        </div>` : ''}
        <div class="iv-stamp">Документ сформирован автоматически и действителен без печати и подписи · ${esc(d.stamp_meta || 'Consolidator Parts')}</div>
      </div>`;
    },
    faq(d) {
      // Accordion на нативных <details>. Item:
      //   {q, a}                                 — простой текст
      //   {q, rows: [{title, subtitle}]}         — вертикальный список
      //   {q, rows: [...], layout: 'grid'}       — горизонтальная сетка
      const items = (d.items || []).map(it => {
        let body;
        if (Array.isArray(it.rows) && it.rows.length) {
          const layoutCls = it.layout === 'grid' ? 'faq-rows faq-rows-grid' : 'faq-rows';
          body = `<div class="${layoutCls}">${it.rows.map(r => `
            <div class="faq-row">
              <div class="faq-row-title">${esc(String(r.title || ''))}</div>
              ${r.subtitle ? `<div class="faq-row-sub">${esc(String(r.subtitle))}</div>` : ''}
            </div>`).join('')}</div>`;
        } else {
          const ans = String(it.a || it.answer || '');
          body = esc(ans).replace(/\n/g, '<br>');
        }
        return `<details class="faq-item">
          <summary>${esc(it.q || it.question || '')}</summary>
          <div class="faq-ans">${body}</div>
        </details>`;
      }).join('');
      return `<div class="card faq-card">
        <div class="card-title">${esc(d.title || 'FAQ')}</div>
        <div class="faq-list">${items || '<div class="ls-empty">Пусто</div>'}</div>
      </div>`;
    },
    kpi_grid(d) {
      // KPI-ячейка может быть кликабельной если у item есть `action`/`params` или `url`.
      // Visually: добавляем класс `.kpi-cell-clickable` + cursor:pointer + handler.
      // Авто-навигация по подписи ячейки (роль-зависимо), если backend не задал
      // явный action/url → KPI-карточки кликабельны во всём проекте.
      const _krole = String(window.__pillRole || 'buyer').replace(/^operator.*/, 'operator');
      const _KPI_NAV = {
        seller: [[/каталог|товар/i,'seller_warehouses'],[/sla|поведенческ/i,'get_sla_report'],
          [/заказ|сделк|выполнен|отгруз/i,'get_orders'],[/депозит|баланс/i,'get_balance'],
          [/оборот|выручк|средн|чек|рейтинг/i,'seller_analytics_hub'],[/рекламац|претенз/i,'get_claims'],
          [/rfq|запрос|срочн/i,'seller_inbox'],[/внешн|верифик|kyb|оценк/i,'start_onboarding']],
        buyer: [[/заказ|сделк|поставк/i,'get_orders'],[/депозит|баланс/i,'get_balance'],
          [/rfq|запрос|котиров/i,'get_my_deals'],[/скидк|эконом/i,'get_buyer_discount'],
          [/менеджер|kam/i,'my_kam'],[/рекламац|претенз/i,'get_claims']],
        operator: [[/очеред|queue/i,'op_queue'],[/sla|просроч|срыв/i,'op_sla_breach'],
          [/рекламац|претенз|claim/i,'get_claims'],[/kyb|верифик/i,'op_kyb_queue'],
          [/платеж|эскроу|escrow/i,'op_payments_dashboard'],[/таможн|customs/i,'op_customs_dashboard'],
          [/логист|доставк/i,'op_logistics_stats'],[/поставщик|supplier/i,'op_my_suppliers']],
      };
      const _navFor = (label) => {
        for (const [re, act] of (_KPI_NAV[_krole] || [])) if (re.test(label || '')) return act;
        return null;
      };
      const items = (d.kpis || d.items || []).map(k => {
        if (!k.action && !k.url) { const _a = _navFor(k.label); if (_a) k.action = _a; }
        const inner = `
          <div class="kpi-value">${esc(String(k.value ?? '—'))}</div>
          <div class="kpi-label">${esc(k.label || '')}</div>
          ${k.sub ? `<div class="kpi-sub">${esc(k.sub)}</div>` : ''}`;
        if (k.action) {
          // S4 fix: action/params могут содержать апострофы → ломали inline onclick='…'
          // и открывали XSS. Кладём в data-* атрибуты с esc(); делегированный
          // listener на .kpi-cell-clickable[data-action] (см. ниже) подхватит.
          const paramsJson = JSON.stringify({...(k.params || {}), _label: k.label || ''});
          return `<button type="button" class="kpi-cell kpi-cell-clickable"
            data-action="${esc(k.action)}"
            data-params='${esc(paramsJson)}'>${inner}</button>`;
        }
        if (k.url) {
          return `<a class="kpi-cell kpi-cell-clickable" href="${esc(k.url)}">${inner}</a>`;
        }
        return `<div class="kpi-cell">${inner}</div>`;
      }).join('');
      // data-count → CSS подстраивает число колонок под фактическое количество
      // ячеек, чтобы не было пустых клеток в конце строки.
      const itemCount = (d.kpis || d.items || []).length;
      // Монохромная линия-прогресс (ЧБ): горизонтальная полоса до значения,
      // слева подпись, справа %. Цвета — из CSS (адаптируются к теме).
      let progress = '';
      if (d.progress) {
        const pct = Math.max(0, Math.min(100, Number(d.progress.value) || 0));
        const center = d.progress.center != null ? d.progress.center : pct + '%';
        progress = `<div class="kpi-line">
          <div class="kpi-line-head"><span class="kpi-line-lbl">${esc(d.progress.label || '')}</span><span class="kpi-line-val">${esc(String(center))}</span></div>
          <div class="kpi-line-track"><div class="kpi-line-fill" style="width:${pct}%"></div></div>
        </div>`;
      }
      return `<div class="card kpi-card">
        <div class="card-title">${esc(d.title || 'KPI')}</div>
        <div class="kpi-grid" data-count="${itemCount}">${items}</div>
        ${progress}
      </div>`;
    },
    form(d) {
      const fields = (d.fields || []).map(f => {
        const val = (f.value !== undefined ? f.value : f.default) || '';
        const req = f.required ? 'required' : '';
        const lbl = `<span class="fm-label">${esc(f.label || f.name)}${f.required ? ' <span class="fm-req">*</span>' : ''}</span>`;
        // select — настоящий <select> с options
        if (f.type === 'select') {
          const opts = (f.options || []).map(o => {
            const v = o.value !== undefined ? o.value : o;
            const lab = o.label !== undefined ? o.label : o;
            const sel = String(v) === String(val) ? 'selected' : '';
            return `<option value="${esc(v)}" ${sel}>${esc(lab)}</option>`;
          }).join('');
          return `<label class="fm-row">${lbl}
            <select class="fm-input fm-select" name="${esc(f.name)}" ${req}>
              ${opts}
            </select>
          </label>`;
        }
        // textarea — многострочный
        if (f.type === 'textarea') {
          return `<label class="fm-row">${lbl}
            <textarea class="fm-input fm-textarea" name="${esc(f.name)}" rows="${f.rows || 4}" placeholder="${esc(f.placeholder || '')}" ${req}>${esc(val)}</textarea>
          </label>`;
        }
        // обычный input (text/number/email/password/...)
        const ro = f.readonly ? 'readonly' : '';
        const hintHtml = f.hint ? `<div class="fm-hint">${esc(f.hint)}</div>` : '';
        return `<label class="fm-row">${lbl}
          <input class="fm-input${f.readonly ? ' fm-input-readonly' : ''}" name="${esc(f.name)}" type="${esc(f.type || 'text')}" value="${esc(val)}" placeholder="${esc(f.placeholder || '')}" ${req} ${ro} autocomplete="off"/>
          ${hintHtml}
        </label>`;
      }).join('');
      const fixed = JSON.stringify(d.fixed_params || {});
      return `<div class="card fm-card" data-form-action="${esc(d.submit_action || '')}" data-fixed='${esc(fixed)}'>
        <div class="card-title">${esc(d.title || tr('card.enter_data'))}</div>
        <div class="fm-fields">${fields}</div>
        <div class="fm-actions">
          <button type="button" class="act-btn fm-submit">${esc(d.submit_label || tr('common.send'))}</button>
        </div>
      </div>`;
    },
    int_methods(d) {
      // Карточка способов интеграции (CSV/XLSX, Google Sheets, REST API).
      const methods = (d.methods || []).map(m => {
        const stCls = m.status === 'soon' ? 'im-st-soon'
                     : m.status === 'recommended' ? 'im-st-recommended'
                     : m.status === 'active' ? 'im-st-active' : 'im-st-default';
        const stLabel = m.status === 'soon' ? tr('tag.coming_soon')
                       : m.status === 'recommended' ? tr('tag.recommended')
                       : m.status === 'active' ? tr('tag.active') : '';
        const stBadge = stLabel ? `<span class="im-status ${stCls}">${esc(stLabel)}</span>` : '';
        const icon = m.icon || '◇';
        // Secondary action (вторичная кнопка над main, например «Создать копию шаблона»)
        let secHtml = '';
        if (m.secondary) {
          if (m.secondary.url) {
            // Кнопка с двумя действиями: open + copy URL (на случай если
            // preview-режим Claude блочит docs.google.com — копируем
            // URL в clipboard и пользователь открывает сам).
            const u = m.secondary.url;
            secHtml = `<div class="im-sec-row">
              <a class="im-secondary" href="${esc(u)}" target="_blank" rel="noopener">${esc(m.secondary.label || '↗')}</a>
              <button class="im-copy-btn" data-copy-url="${esc(u)}" title="Скопировать ссылку">📋</button>
            </div>`;
          } else if (m.secondary.action) {
            secHtml = `<button class="im-secondary act-btn" data-action="${esc(m.secondary.action)}" data-params='${esc(JSON.stringify(m.secondary.params || {}))}' data-label="${esc(m.secondary.label || '')}">${esc(m.secondary.label || '↗')}</button>`;
          }
        }
        // Inline-форма: hint + input + primary submit
        let formHtml = '';
        if (m.input) {
          const inpName = m.input.name || 'value';
          const fixed = JSON.stringify((m.primary && m.primary.params) || {});
          const submitAction = (m.primary && m.primary.action) || '';
          formHtml = `${m.hint ? `<div class="im-hint">${esc(m.hint)}</div>` : ''}
            <div class="im-form fm-card" data-form-action="${esc(submitAction)}" data-fixed='${esc(fixed)}'>
              <input class="fm-input im-input" name="${esc(inpName)}" type="${esc(m.input.type || 'text')}" placeholder="${esc(m.input.placeholder || '')}" autocomplete="off"/>
              <button type="button" class="act-btn fm-submit im-primary">${esc((m.primary && m.primary.label) || 'OK')}</button>
            </div>`;
        } else if (m.primary && !m.disabled) {
          // Просто primary-кнопка
          if (m.primary.action) {
            formHtml = `<button class="im-primary act-btn" data-action="${esc(m.primary.action)}" data-params='${esc(JSON.stringify(m.primary.params || {}))}' data-label="${esc(m.primary.label || '')}">${esc(m.primary.label || 'OK')}</button>`;
          } else if (m.primary.url) {
            formHtml = `<a class="im-primary" href="${esc(m.primary.url)}" target="_blank" rel="noopener">${esc(m.primary.label || '↗')}</a>`;
          }
        }
        const disabledCls = m.disabled ? ' im-card-disabled' : '';
        // Features list (буллеты — для smart-карточки)
        const featuresHtml = (m.features && m.features.length)
          ? `<ul class="im-features">${m.features.map(f => `<li>${esc(f)}</li>`).join('')}</ul>`
          : '';
        return `<div class="im-card${disabledCls}">
          <div class="im-head">
            <div class="im-icon">${esc(icon)}</div>
            <div class="im-title-wrap">
              <div class="im-title">${esc(m.title || '')}</div>
              ${stBadge}
            </div>
          </div>
          <div class="im-desc">${esc(m.description || '')}</div>
          ${featuresHtml}
          ${secHtml}
          ${formHtml}
        </div>`;
      }).join('');
      return `<div class="card im-block">
        <div class="im-block-title">${esc(d.title || tr('card.integration'))}</div>
        <div class="im-list">${methods}</div>
      </div>`;
    },
    order_timeline(d) {
      const stages = (d.stages || []).map((s, i) => {
        const cls = s.state === 'done' ? 'tl-done'
                   : s.state === 'current' ? 'tl-current' : 'tl-pending';
        const dot = s.state === 'done' ? '●'
                   : s.state === 'current' ? '◆' : '○';
        return `<div class="tl-stage ${cls}">
          <span class="tl-dot">${dot}</span>
          <span class="tl-label">${esc(s.label)}</span>
        </div>`;
      }).join('');

      const next = d.next_action;
      const nextHtml = next
        ? `<button class="tl-cta act-btn" data-action="${esc(next.action)}" data-params='${esc(JSON.stringify(next.params||{}))}' data-label="${esc(next.label)}">${esc(next.label)}</button>`
        : '';

      const pct = d.progress_pct || 0;
      const totalLine = d.total ? `${fmtMoney(d.total, d.currency)}` : '';
      const reserveLine = d.reserve_amount
        ? ` · резерв ${fmtMoney(d.reserve_amount, d.currency)}`
        : '';

      return `<div class="card tl-card">
        <div class="tl-head">
          <div>
            <div class="card-title">${esc(d.title || ('Заказ ORD-' + d.order_id))}</div>
            <div class="tl-status">${esc(d.status_label || '')}</div>
          </div>
          <div class="tl-total">${totalLine}<span class="tl-reserve">${esc(reserveLine)}</span></div>
        </div>
        <div class="tl-progress-wrap">
          <div class="tl-progress"><div class="tl-progress-fill" style="width:${pct}%"></div></div>
          <div class="tl-progress-pct">${pct}%</div>
        </div>
        <div class="tl-stages">${stages}</div>
        ${nextHtml ? `<div class="tl-actions">${nextHtml}</div>` : ''}
      </div>`;
    },
    raw_html(d) {
      // ⚠️ ВНИМАНИЕ: trust boundary. Этот renderer ВЫВОДИТ HTML БЕЗ ЭСКЕЙПА.
      // Использовать ТОЛЬКО для контента, сформированного фронтендом
      // (статичные инструкции, sticky-карточки, готовые формы).
      // НИКОГДА не использовать для ответов AI/бэкенда — туда заведи
      // отдельный type с фиксированной схемой и esc(). Сервер-сайд
      // продьюсеров raw_html нет (проверено grep'ом по backend) — если
      // появится, это критическая регрессия безопасности.
      return d.html || '';
    },
    output_file(d) {
      // Карточка готового файла (как у claude.ai): иконка + имя + размер +
      // кнопки «Открыть» (side panel) / «Скачать».
      var name = esc(d.filename || 'pricelist.xlsx');
      var size = d.size_kb ? d.size_kb + ' KB' : '';
      var url = esc(d.download_url || '');
      var importId = esc(String(d.import_id || ''));
      return '<div class="card of-card" data-import-id="' + importId + '" data-filename="' + name + '" data-download="' + url + '">'
        + '<div class="of-icon">📊</div>'
        + '<div class="of-info">'
        +   '<div class="of-name">' + name + '</div>'
        +   '<div class="of-meta">Spreadsheet · XLSX' + (size ? ' · ' + size : '') + '</div>'
        + '</div>'
        + '<a class="of-btn of-btn-primary" href="' + url + '" download="' + name + '" target="_blank" rel="noopener">⬇ Скачать</a>'
        + '<button class="of-btn of-open-preview">↗ Открыть</button>'
        + '</div>';
    },
    table_preview(d) {
      const headers = (d.headers || []).map(h => `<th>${esc(h)}</th>`).join('');
      const rows = (d.rows || []).map(row => {
        const cells = (row || []).map(c => `<td>${esc((c == null ? '' : String(c)).slice(0, 240))}</td>`).join('');
        return `<tr>${cells}</tr>`;
      }).join('');
      return `<div class="card tp-card">
        <div class="card-title">${esc(d.title || tr('card.preview'))}</div>
        <div class="tp-scroll"><table class="tp-table">
          <thead><tr>${headers}</tr></thead>
          <tbody>${rows}</tbody>
        </table></div>
        ${d.foot ? `<div class="tp-foot">${esc(d.foot)}</div>` : ''}
      </div>`;
    },
    seller_queue(d) {
      const sections = (d.sections || []).map(s => {
        const orders = (s.orders || []).map(o => {
          // Позиции — в той же стилистике, что у покупателя (spec-tbl).
          // Колонки для продавца: Stock | # | ID | Name | Brand | Cond | Цена | Кол-во | Вес | Сумма
          const condLabel = (c) => {
            if (c === 'oem') return '<span class="spec-cond-oem">OEM</span>';
            if (c === 'analogue') return '<span class="spec-cond-an">Аналог</span>';
            return esc(c || '');
          };
          const items = `<div class="spec-tbl-wrap"><table class="spec-tbl"><thead><tr>
            <th>Stock</th><th>#</th><th>ID</th><th>Name</th><th>Brand</th><th>Condition</th><th>Цена</th><th>Кол-во</th><th>Вес</th><th>Сумма</th>
          </tr></thead><tbody>${(o.items || []).map((it, idx) => `
            <tr>
              <td><span class="spec-stk in"><span class="spec-stk-dot"></span>${it.stock > 0 ? tr('stock.in_stock') : tr('stock.on_order')}</span></td>
              <td class="spec-row-num">${idx+1}</td>
              <td><a class="spec-id-link">${esc(it.article)}</a></td>
              <td><div class="spec-name-cell"><span class="spec-name">${esc(it.name)}</span></div></td>
              <td>${esc(it.brand || '')}</td>
              <td>${condLabel(it.condition)}</td>
              <td class="spec-price">${fmtMoney(it.unit_price, 'USD')}</td>
              <td>${it.qty}</td>
              <td>${esc(it.weight || '—')}</td>
              <td class="spec-price">${fmtMoney(it.subtotal, 'USD')}</td>
            </tr>`).join('')}</tbody></table></div>`;
          const btnAct = s.btn_action || 'advance_order';
          // Только qr/upload триггеры блокируют переход. button-триггеры
          // auto-закрываются самим действием advance (одно подтверждение).
          // Чек-лист берём per-order: зависит от incoterm (FOB/CIP/DDP).
          const checklist = o.checklist || s.checklist || [];
          const doneIds = new Set(o.triggers_done || []);
          const blockingMissing = checklist.filter(t =>
            ['qr', 'upload'].includes(t.type) && !doneIds.has(t.id)
          );
          const allDone = blockingMissing.length === 0;
          const triggerDisabled = !allDone;
          // Inline advance — без переключения чат-сообщений и скролла вниз.
          const advOnclick = `event.stopPropagation();event.preventDefault();window.sqAdvance&&window.sqAdvance(this, ${o.id}, '${esc(btnAct)}');`;
          const blockingTotal = checklist.filter(t => ['qr','upload'].includes(t.type)).length;
          const blockingDone = blockingTotal - blockingMissing.length;
          const advBtn = s.btn
            ? `<button class="act-btn sq-btn${triggerDisabled ? ' sq-btn-locked' : ''}" type="button" data-checklist-total="${blockingTotal}" data-done-count="${blockingDone}" ${triggerDisabled ? `disabled title="Загрузите документы и QR-сканы перед переходом"` : ''} onclick="${advOnclick}">${esc(s.btn)}${triggerDisabled ? ` 🔒 ${blockingDone}/${blockingTotal}` : ''}</button>`
            : '';
          const openOnclick = `event.stopPropagation();event.preventDefault();window.quickAction&&window.quickAction('get_order_detail',{order_id:${o.id}});`;
          const openBtn = `<button class="act-btn sq-btn sq-open" type="button" onclick="${openOnclick}">🔍 Открыть</button>`;
          // Для статуса «Ожидает оплаты» — добавляем кнопку отмены и показываем дедлайн если установлен
          let cancelBtn = '';
          let deadlineChip = '';
          if (s.status === 'pending') {
            cancelBtn = `<button class="act-btn sq-btn sq-cancel" type="button" onclick="event.stopPropagation();window.sellerCancelPending && window.sellerCancelPending(${o.id}, ${o.subtotal});">🗑 Отменить</button>`;
            if (o.payment_deadline) {
              try {
                const dl = new Date(o.payment_deadline);
                const hrs = Math.max(0, Math.round((dl - Date.now()) / 36e5));
                deadlineChip = `<span class="sq-deadline" title="Дедлайн: ${dl.toLocaleString('ru-RU')}">⏰ ${hrs}ч</span>`;
              } catch(_){}
            }
          }
          // Компактная строка: №, статус, действие, total. Позиции — раскрытие по клику.
          return `<details class="sq-order">
            <summary class="sq-order-head">
              <span class="sq-chev">▸</span>
              <span class="sq-order-num">Заказ #${esc(o.id)}</span>
              <span class="sq-status-chip">${esc(s.short_label || s.label.replace(/^[^a-zа-я]+/i, '').split('—')[0].trim())}${deadlineChip}</span>
              ${o.incoterm ? `<span class="sq-incoterm sq-incoterm-${esc(o.incoterm.toLowerCase())}" title="Базис поставки ${esc(o.incoterm)}">${esc(o.incoterm)}</span>` : ''}
              <span class="sq-order-items">${o.items.length} поз.</span>
              <span class="sq-order-sub">${fmtMoney(o.subtotal, 'USD')}</span>
              <span class="sq-order-actions">${advBtn}${cancelBtn}${openBtn}</span>
            </summary>
            <div class="sq-order-body">
              <div class="sq-buyer">Покупатель: ${esc(o.buyer || '—')}</div>
              ${o.stage_meta && (o.stage_meta.trigger || o.stage_meta.actor || o.stage_meta.sla) ? `<div class="sq-stage-info" style="margin-bottom:10px;">
                ${o.stage_meta.trigger ? `<div class="sq-stage-row"><span class="sq-stage-tag">⚡ Триггер</span> ${esc(o.stage_meta.trigger)}</div>` : ''}
                ${o.stage_meta.actor ? `<div class="sq-stage-row"><span class="sq-stage-tag">👤 Вы</span> ${esc(o.stage_meta.actor)}</div>` : ''}
                ${o.stage_meta.sla ? `<div class="sq-stage-row"><span class="sq-stage-tag">⏱ SLA</span> ${esc(o.stage_meta.sla)}</div>` : ''}
                ${o.stage_meta.next_actor ? `<div class="sq-stage-row"><span class="sq-stage-tag">▶ Дальше</span> ${esc(o.stage_meta.next_actor)}</div>` : ''}
              </div>` : ''}
              ${checklist.length ? `<div class="sq-checklist">
                <div class="sq-checklist-title">⚡ Триггеры этапа (${doneIds.size}/${checklist.length}):</div>
                ${checklist.map(t => {
                  const done = doneIds.has(t.id);
                  const isAuto = t.type === 'auto';
                  const icon = ({qr:'📷', upload:'📎', button:'✓', auto:'🤖'})[t.type] || '✓';
                  const clickCode = `event.stopPropagation();event.preventDefault();window.sqTrigger&&window.sqTrigger(this, ${o.id}, '${esc(s.status)}', '${esc(t.id)}');`;
                  const cls = `sq-trigger${done ? ' sq-trigger-done' : ''}${isAuto ? ' sq-trigger-auto' : ''}`;
                  const autoBadge = isAuto && done ? '<span class="sq-trigger-auto-tag">авто</span>' : '';
                  return `<button class="${cls}" type="button" ${(done || isAuto) ? 'disabled' : `onclick="${clickCode}"`}>
                    <span class="sq-trigger-mark">${done ? '☑' : '☐'}</span>
                    <span class="sq-trigger-icon">${icon}</span>
                    <span class="sq-trigger-label">${esc(t.label)}</span>
                    ${autoBadge}
                  </button>`;
                }).join('')}
              </div>` : ''}
              ${items}
            </div>
          </details>`;
        }).join('');
        // Триггер этапа + требуемые документы + SLA — из ТЗ «Этапы ЛК»
        const stageInfo = (s.trigger || s.docs?.length || s.sla)
          ? `<div class="sq-stage-info">
              ${s.trigger ? `<div class="sq-stage-row"><span class="sq-stage-tag">⚡ Триггер</span> ${esc(s.trigger)}</div>` : ''}
              ${s.actor ? `<div class="sq-stage-row"><span class="sq-stage-tag">👤 Исполнитель</span> ${esc(s.actor)}</div>` : ''}
              ${s.docs?.length ? `<div class="sq-stage-row"><span class="sq-stage-tag">📎 Документы</span> ${s.docs.map(esc).join(' · ')}</div>` : ''}
              ${s.sla ? `<div class="sq-stage-row"><span class="sq-stage-tag">⏱ SLA</span> ${esc(s.sla)}</div>` : ''}
            </div>`
          : '';
        return `<details class="sq-section" open>
          <summary class="sq-section-head">
            <span class="sq-chev">▸</span>
            <span class="sq-section-label">${esc(s.label)}</span>
            <span class="sq-section-meta">${s.orders_count} зак. · ${s.items_count} поз. · ${fmtMoney(s.amount, 'USD')}</span>
          </summary>
          ${stageInfo}
          ${orders}
        </details>`;
      }).join('');
      // Архивные секции — отгруженные заказы (без действий, только инфо).
      const archiveSections = (d.archive_sections || []).map(s => {
        const rows = (s.orders || []).map(o => `
          <div class="sq-arc-row">
            <span class="sq-order-num">Заказ #${esc(o.id)}</span>
            <span class="sq-status-chip">${esc(s.short_label || '')}</span>
            ${o.incoterm ? `<span class="sq-incoterm sq-incoterm-${esc(o.incoterm.toLowerCase())}">${esc(o.incoterm)}</span>` : ''}
            <span class="sq-arc-actor">📍 ${esc(o.current_actor || '')}</span>
            <span class="sq-order-items">${o.items.length} поз.</span>
            <span class="sq-order-sub">${fmtMoney(o.subtotal, 'USD')}</span>
            <button class="act-btn sq-btn sq-open" type="button" onclick="event.stopPropagation();event.preventDefault();window.quickAction&&window.quickAction('track_order',{order_id:${o.id}});">📍 Трекинг</button>
          </div>`).join('');
        return `<details class="sq-section sq-archive">
          <summary class="sq-section-head sq-archive-head">
            <span class="sq-chev">▸</span>
            <span class="sq-section-label">${esc(s.label)}</span>
            <span class="sq-section-meta">${s.orders_count} зак. · ${fmtMoney(s.amount, 'USD')}</span>
          </summary>
          <div class="sq-arc-list">${rows}</div>
        </details>`;
      }).join('');
      const archiveBlock = (d.archive_sections && d.archive_sections.length)
        ? `<div class="sq-archive-wrap">
            <div class="sq-archive-title">${esc(d.archive_title || '📤 Уже отгружено')}</div>
            ${archiveSections}
          </div>`
        : '';
      return `<div class="card sq-card">
        <div class="sq-head">
          <div class="card-title">📦 ${esc(d.title || tr('card.seller_queue'))}</div>
          <div class="sq-total">${d.total_orders} активных заказа(ов)</div>
        </div>
        ${sections || '<div class="sq-empty">Очередь пуста.</div>'}
        ${archiveBlock}
      </div>`;
    },
    tracking(d) {
      const stages = (d.stages || []).map(s => {
        const cls = s.state === 'done' ? 'tk-done' : s.state === 'current' ? 'tk-current' : 'tk-pending';
        const dot = s.state === 'done' ? '●' : s.state === 'current' ? '◆' : '○';
        return `<div class="tk-stage ${cls}">
          <span class="tk-dot">${dot}</span>
          <span class="tk-label">${esc(s.label)}</span>
          ${s.eta ? `<span class="tk-eta">${esc(s.eta)}</span>` : ''}
        </div>`;
      }).join('');
      const tl = (d.timeline || []).map(t =>
        `<div class="tk-event"><span class="tk-when">${esc(t.when)}</span><span class="tk-text">${esc(t.text)}</span></div>`
      ).join('') || '<div class="tk-empty">Событий пока нет.</div>';
      const trackingLine = d.tracking_number
        ? `<div class="tk-tracking">📍 ${esc(d.carrier || tr('common.carrier'))} · <span class="tk-track-num">${esc(d.tracking_number)}</span></div>`
        : '';
      const nextLine = d.next_event
        ? `<div class="tk-next">🔜 <b>${esc(d.next_actor || tr('common.next'))}</b> ${esc(d.next_event)}</div>`
        : '';
      return `<div class="card tk-card">
        <div class="tk-head">
          <div>
            <div class="card-title">${esc(d.title || ('Заказ #' + d.order_id))}</div>
            <div class="card-sub">${esc(d.current_label || '')}</div>
            ${trackingLine}
          </div>
          <div class="tk-total">${fmtMoney(d.total, d.currency)}</div>
        </div>
        ${nextLine}
        <div class="tk-progress-wrap">
          <div class="tk-progress"><div class="tk-progress-fill" style="width:${Math.max(0, Math.min(100, Number(d.progress_pct) || 0))}%"></div></div>
          <div class="tk-progress-meta">
            <span>${(Number(d.current_idx) || 0) + 1} из ${Number(d.total_stages) || 0}</span>
            <span>ETA: <b>${esc(d.eta_delivery || '—')}</b> · ${Number(d.days_left) || 0} дн.</span>
          </div>
        </div>
        ${(() => {
          // Алерт о расхождении: кто-то уехал, кто-то ещё в производстве.
          const a = d.divergence_alert;
          if (!a) return '';
          const btn = (cta) => cta
            ? `<button type="button" class="tk-div-btn"
                data-action="${esc(cta.action)}" data-params='${esc(JSON.stringify(cta.params||{}))}'
              >${esc(cta.label)}</button>` : '';
          return `<div class="tk-divergence">
            <div class="tk-div-title">${esc(a.title)}</div>
            <div class="tk-div-body">${esc(a.body)}</div>
            <div class="tk-div-cta">${btn(a.cta_consolidate)}${btn(a.cta_split)}</div>
          </div>`;
        })()}
        <div class="tk-stages">${stages}</div>
        <div class="tk-tl-head">История</div>
        <div class="tk-timeline">${tl}</div>
      </div>`;
    },
    order(d) {
      // Цвет чипа: сначала по payment_status (оплата важнее status'а), потом
      // по status_code. awaiting_reserve → orange, всё оплаченное (reserve_paid/
      // mid_paid/customs_paid/paid) → green, refund_* → gray.
      const payCls = ({
        awaiting_reserve: 'orange', pending: 'orange',
        reserve_paid: 'green', mid_paid: 'green', customs_paid: 'green', paid: 'green',
        refund_pending: 'gray', refunded: 'gray'
      })[d.payment_status];
      const cls = payCls || ({pending:'orange', shipped:'green', completed:'green', cancelled:'gray'})[d.status_code] || '';
      const oid = d.id || d.number;
      // Единый вид: клик по любой order-карточке открывает ПОЛНУЮ карточку
      // заказа (таблица позиций) через get_order_detail, а не трекинг.
      const clickAttrs = oid
        ? ` data-action="get_order_detail" data-params='${esc(JSON.stringify({order_id: parseInt(String(oid).replace(/\D/g, ''), 10) || oid}))}' role="button" tabindex="0" title="Открыть заказ"`
        : '';
      // Состав отправки по странам — показываем после выбора способа доставки
      const ob = d.origin_breakdown || [];
      const modeLabel = {sea:'🚢 Морем', air:'✈️ Авиа', auto:'🚚 Авто'}[d.shipping_mode] || d.shipping_mode || '';
      let originBlock = '';
      if (ob.length >= 1 && d.shipping_mode) {
        const rows = ob.map(o => `
          <tr>
            <td>${o.flag} ${esc(o.name)}<div class="ob-ports">${o.ports.map(esc).join(', ')}</div></td>
            <td class="ob-num">${o.count}</td>
            <td class="ob-num">${o.weight_kg.toFixed(1)} кг</td>
            <td class="ob-num">${fmtMoney(o.cargo, 'USD')}</td>
            <td class="ob-num">${o.freight > 0 ? '$' + Math.round(o.freight).toLocaleString() : '—'}<div class="ob-days">${o.days ? '~'+o.days+'д' : ''}</div></td>
          </tr>`).join('');
        originBlock = `<div class="ob-block">
          <div class="ob-title">📦 Состав отправки · ${esc(modeLabel)} · ${esc(d.incoterm || '')}</div>
          <table class="ob-table">
            <thead><tr><th>Откуда</th><th>Поз.</th><th>Вес</th><th>Cargo</th><th>Фрахт</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
          ${ob.length >= 2
            ? `<div class="ob-hint">⚠️ ${ob.length} отправки = ${ob.length} коносамента. Для FOB придётся забирать ${ob.length === 2 ? 'из обоих портов' : 'из ' + ob.length + ' портов'}.</div>`
            : '<div class="ob-hint">✓ Единая отправка из одного порта.</div>'}
        </div>`;
      }
      // Кнопка отмены — только для заказов с неоплаченным резервом
      const orderIdInt = parseInt(String(d.id || d.number).replace(/\D/g, ''), 10) || 0;
      const cancelBtn = d.can_cancel
        ? `<button class="act-btn ord-cancel" type="button" title="Удалить неоплаченный заказ" onclick="event.stopPropagation();window.cancelOrderPrompt && window.cancelOrderPrompt(${orderIdInt}, '${esc(d.number || ('ORD-' + d.id))}', ${d.total || 0});">🗑 Отменить</button>`
        : '';
      return `<div class="card card-clickable"${clickAttrs}>
        <div class="card-row">
          <div class="card-emoji">📦</div>
          <div class="card-info">
            <div class="card-title">${esc(d.number || ('Order #' + d.id))}</div>
            <div class="card-sub">${esc(d.customer || '')}${d.created_at ? ' · ' + esc(d.created_at) : ''}</div>
          </div>
          <div class="card-price">${fmtMoney(d.total, d.currency)}</div>
        </div>
        <div class="card-meta">
          <span class="card-chip card-chip-${cls}">${esc(d.status || '')}</span>
          ${cancelBtn}
        </div>
        ${originBlock}
      </div>`;
    },
    // Компактный список RFQ: одна строка на запрос. Клик → раскрывает
    // полную spec_results карточку через get_rfq_status(rfq_id).
    // Карточка подтверждения заказа сразу после quick_order (confirmed).
    // 3 чётких блока: ДЕНЬГИ · СТАТУС · ДЕЙСТВИЯ. Без freight-жаргона.
    order_confirm(d) {
      const m = d.money || {};
      const steps = d.status_steps || [];
      const stepsHtml = steps.map((s, i) => {
        const cls = s.state === 'current' ? 'oc-step oc-step-current'
                  : s.state === 'done' ? 'oc-step oc-step-done'
                  : 'oc-step oc-step-next';
        const dot = s.state === 'done' ? '✓' : s.state === 'current' ? '●' : (i + 1);
        return `<div class="${cls}"><span class="oc-step-dot">${dot}</span><span class="oc-step-label">${esc(s.label)}</span></div>`;
      }).join('');
      const moneyOK = m.wallet_enough !== false;
      // Advanced секция — сворачиваемая, для тех кто хочет копнуть в фрахт.
      const adv = d.advanced || {};
      const advBd = adv.components || {};
      const advHasData = adv.shipping_total > 0 || (adv.origin_breakdown && adv.origin_breakdown.length);
      const advHtml = advHasData ? `
        <details class="oc-advanced">
          <summary>Технические детали (для оператора) ▾</summary>
          <div class="oc-adv-body">
            ${adv.items_subtotal ? `<div class="oc-adv-row"><span>Товары (EXW)</span><span>${fmtMoney(adv.items_subtotal, 'USD')}</span></div>` : ''}
            ${adv.shipping_total ? `<div class="oc-adv-row"><span>Доставка (логистика)</span><span>${fmtMoney(adv.shipping_total, 'USD')}</span></div>` : ''}
            ${advBd.freight > 0 ? `<div class="oc-adv-row oc-adv-sub"><span>· Фрахт</span><span>${fmtMoney(advBd.freight, 'USD')}</span></div>` : ''}
            ${advBd.insurance > 0 ? `<div class="oc-adv-row oc-adv-sub"><span>· Страховка</span><span>${fmtMoney(advBd.insurance, 'USD')}</span></div>` : ''}
            ${advBd.duty > 0 ? `<div class="oc-adv-row oc-adv-sub"><span>· Пошлина</span><span>${fmtMoney(advBd.duty, 'USD')}</span></div>` : ''}
            ${advBd.vat > 0 ? `<div class="oc-adv-row oc-adv-sub"><span>· НДС 22%</span><span>${fmtMoney(advBd.vat, 'USD')}</span></div>` : ''}
            ${advBd.last_mile > 0 ? `<div class="oc-adv-row oc-adv-sub"><span>· Доставка до двери</span><span>${fmtMoney(advBd.last_mile, 'USD')}</span></div>` : ''}
            ${advBd.clearance > 0 ? `<div class="oc-adv-row oc-adv-sub"><span>· Таможенное оформление</span><span>${fmtMoney(advBd.clearance, 'USD')}</span></div>` : ''}
            ${adv.shipping_missing ? `<div class="oc-adv-row oc-adv-warn">⚠ Доставка не рассчитана для ${adv.shipping_missing} поз.</div>` : ''}
          </div>
        </details>
      ` : '';
      return `<div class="card oc-card">
        <div class="oc-head">
          <div class="oc-head-left">
            <div class="oc-head-num">✓ Заказ #${esc(d.number || d.id)} принят</div>
            <div class="oc-head-meta">${esc(d.items_count || 0)} позиций · доставка ${esc(d.shipping_mode_label || '—')} · базис ${esc(d.incoterm || '—')}</div>
          </div>
          <div class="oc-head-total">${fmtMoney(d.total, d.currency || 'USD')}</div>
        </div>

        <div class="oc-section">
          <div class="oc-section-title">💰 Деньги</div>
          <div class="oc-money">
            <div class="oc-money-row oc-money-now">
              <div class="oc-money-lbl">Сейчас списано</div>
              <div class="oc-money-val">${fmtMoney(m.reserve_now, 'USD')}<span class="oc-money-pct">${m.reserve_pct || 10}% резерв</span></div>
            </div>
            <div class="oc-money-row">
              <div class="oc-money-lbl">На депозите</div>
              <div class="oc-money-val oc-money-${moneyOK ? 'ok' : 'bad'}">${fmtMoney(m.wallet_balance, 'USD')}${moneyOK ? '' : ' <span class="oc-money-warn">недостаточно</span>'}</div>
            </div>
            <div class="oc-money-row">
              <div class="oc-money-lbl">К доплате позже</div>
              <div class="oc-money-val">${fmtMoney(m.remaining_to_pay, 'USD')}<span class="oc-money-pct">${esc(m.remaining_when || 'после отгрузки')}</span></div>
            </div>
          </div>
        </div>

        <div class="oc-section">
          <div class="oc-section-title">📍 Статус и срок</div>
          <div class="oc-steps">${stepsHtml}</div>
          ${d.eta_min_days ? `<div class="oc-eta">Срок ${esc(d.eta_label || 'до доставки')}: <b>${esc(d.eta_min_days)}–${esc(d.eta_max_days)} ${(d.eta_max_days === 1 ? 'день' : (d.eta_max_days < 5 ? 'дня' : 'дней'))}</b></div>` : ''}
        </div>

        ${advHtml}
      </div>`;
    },

    rfq_list(d) {
      const rows = (d.rows || []).map(r => {
        const urg = '';
        const statusKey = (r.status || '').toLowerCase();
        // amber = нужно действие (жёлтый), good = всё работает (зелёный),
        // new = только создан (синий), wait = архив/нейтральный (серый)
        const statusClass = statusKey === 'good'   ? 'rl-st-good'
                          : statusKey === 'amber' || statusKey === 'warn' ? 'rl-st-amber'
                          : statusKey === 'decide' ? 'rl-st-amber'  // legacy decide → amber
                          : statusKey === 'new'    ? 'rl-st-new'
                          :                          'rl-st-wait';
        const isPendingOrder = r.kind === 'order_pending';
        const isOrder = isPendingOrder || r.kind === 'order';
        const hasQuotes = (r.quotes_count || 0) > 0;
        // Order (любой) → открывает get_order_detail (полная карточка с табл).
        // RFQ → get_rfq_status. «×»: order → cancel_order, rfq → cancel_rfq.
        const openAction = isOrder ? 'get_order_detail' : 'get_rfq_status';
        const openParams = isOrder ? `{"order_id":${r.id}}` : `{"rfq_id":${r.id}}`;
        const delHandler = isOrder
          ? `window.deleteOrderRowInline(this,${r.id});`
          : `window.deleteRfqRowInline(this,${r.id});`;
        const metaText = isPendingOrder
          ? `${esc(r.items_count)} поз`
          : `${esc(r.items_count)} поз · <strong class="${hasQuotes ? 'rl-meta-good' : ''}">${esc(r.quotes_count)} котир</strong>`;
        const stageLine = r.stage ? `<div class="rl-stage ${r.stage_alert ? 'rl-stage-alert' : ''}">${esc(r.stage)}</div>` : '';
        const emoji = isOrder ? '📦' : '📋';
        // Тон левой полосы и аватара — по статусу
        const toneClass = statusClass.replace('rl-st-', 'rlc-tone-');
        const fmtAmt = (v) => (Number(v)||0).toLocaleString('en-US', {maximumFractionDigits: 0});
        const subParts = [];
        if (r.customer_name) subParts.push(esc(r.customer_name));
        if (r.date_str) subParts.push(`<span class="rlc-date">${esc(r.date_str)}</span>`);
        const subline = subParts.join(' <span class="rlc-dot">·</span> ');
        const amountHtml = (typeof r.amount === 'number' && r.amount)
          ? `<div class="rlc-amt-block">
               <div class="rlc-amt">$${fmtAmt(r.amount)}</div>
               ${r.items_count ? `<div class="rlc-items">${esc(r.items_count)} поз</div>` : ''}
             </div>` : '';
        return `<div class="rl-row-wrap">
          <button type="button" class="rl-card-row ${toneClass}"
              data-action="${openAction}" data-params='${openParams}' data-label="Открыть ${esc(r.number)}">
            <div class="rlc-stripe"></div>
            <div class="rlc-avatar">${emoji}</div>
            <div class="rlc-body">
              <div class="rlc-num">${esc(r.number)}</div>
              ${subline ? `<div class="rlc-sub">${subline}</div>` : ''}
              <div class="rlc-pill ${statusClass}">${esc(r.status_label || r.status)}</div>
            </div>
            ${amountHtml}
            <div class="rlc-arrow">›</div>
          </button>
          <button type="button" class="rl-row-del"
              title="${isPendingOrder ? 'Отменить заказ' : 'Удалить RFQ'} ${esc(r.number)}"
              onclick="event.stopPropagation();${delHandler}">×</button>
        </div>`;
      }).join('');
      // Collapsible head: клик по заголовку → разворот/схлоп списка строк.
      // По умолчанию развёрнуты (как было), но можно свернуть кликом.
      const collapsedInit = !!d.collapsed;  // default = expanded (false)
      const stateAttr = collapsedInit ? 'data-collapsed="1"' : 'data-collapsed="0"';
      const chev = collapsedInit ? '▸' : '▾';
      return `<div class="card rl-card rl-collapsible" ${stateAttr}>
        <div class="rl-head rl-coll-head" role="button" tabindex="0"
             onclick="window.__toggleCollapsibleCard&amp;&amp;window.__toggleCollapsibleCard(this)"
             onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();window.__toggleCollapsibleCard&amp;&amp;window.__toggleCollapsibleCard(this);}">
          <span class="rl-coll-chev">${chev}</span>
          <span class="rl-head-title">${esc(d.title || 'RFQ')}</span>
          <span class="rl-head-count">${(d.rows || []).length}</span>
        </div>
        <div class="rl-list rl-coll-body" ${collapsedInit ? 'style="display:none;"' : ''}>${rows}</div>
      </div>`;
    },

    rfq(d) {
      // Rich inline RFQ card. Информативность ≥ удалённой /chat/rfq/<id>/ страницы.
      const rid = d.id || d.number;
      const fmtMoney = (v) => {
        if (v == null) return '—';
        if (Math.abs(v) >= 1000000) return '$' + (v/1000000).toFixed(1) + 'M';
        if (Math.abs(v) >= 1000) return '$' + Math.round(v/1000) + 'K';
        return '$' + Math.round(v).toLocaleString('en-US');
      };
      const urgencyClass = d.urgency === 'critical' ? 'rcard-urg-critical'
                          : d.urgency === 'urgent' ? 'rcard-urg-urgent' : '';
      const modeBadge = d.mode ? `<span class="rcard-mode-badge">${esc(d.mode)}</span>` : '';
      const urgencyBadge = d.urgency_label
        ? `<span class="rcard-urg-badge ${urgencyClass}">${esc(d.urgency_label)}</span>` : '';

      // Meta-строка: показываем только SLA-дедлайн если он есть и срочный.
      // «только что» и «N поставщ.» не несут пользы — убраны.
      const metaBits = [];
      if (d.time_left_label && d.time_left_overdue) {
        metaBits.push(`<span class="rcard-meta-bad">⚠ ${esc(d.time_left_label)}</span>`);
      }
      const metaRow = metaBits.length
        ? `<div class="rcard-meta-row">${metaBits.join('<span class="rcard-meta-sep">·</span>')}</div>` : '';

      // ── Progress block: 2 бара (сорсинг + котировки) + бюджет vs лучшая ──
      const srcDone = (d.sourcing_pct ?? 0) >= 100;
      const quoteHasOne = (d.quotes_count ?? 0) > 0;
      const deltaPct = d.delta_pct;
      const deltaTone = deltaPct == null ? '' : (deltaPct <= 0 ? 'rcard-delta-good' : 'rcard-delta-bad');
      const deltaText = deltaPct == null ? '' :
        (deltaPct <= 0 ? `↓ ${Math.abs(deltaPct)}%` : `↑ ${deltaPct}%`);
      const progressBlock = `
        <div class="rcard-progress">
          <div class="rcard-bar-row">
            <div class="rcard-bar-lbl">
              <span>Ответили поставщиков</span>
              <span class="rcard-bar-val ${quoteHasOne ? 'rcard-bar-val-good' : ''}">${esc(d.quotes_count ?? 0)} из ${esc(d.sent_count ?? 0)}</span>
            </div>
            <div class="rcard-bar"><div class="rcard-bar-fill ${quoteHasOne ? 'rcard-bar-fill-good' : ''}" style="width:${esc(d.quoting_pct ?? 0)}%"></div></div>
          </div>
          <div class="rcard-money">
            <div class="rcard-money-cell">
              <div class="rcard-money-lbl">Бюджет (estimate)</div>
              <div class="rcard-money-val">${fmtMoney(d.budget_usd)}</div>
            </div>
            <div class="rcard-money-arrow">${d.best_quote_usd != null ? '→' : ''}</div>
            <div class="rcard-money-cell ${d.best_quote_usd != null ? 'rcard-money-best' : 'rcard-money-empty'}">
              <div class="rcard-money-lbl">Лучшая котировка</div>
              <div class="rcard-money-val">${d.best_quote_usd != null ? fmtMoney(d.best_quote_usd) : '—'} ${deltaText ? `<span class="rcard-delta ${deltaTone}">${deltaText}</span>` : ''}</div>
            </div>
          </div>
        </div>
      `;

      // ── Items: мини-карточки ──
      // Каждая позиция = 2 строки: верх (артикул + qty + цена), низ (название).
      // Для несматченных «название» = borderless input, рамка появляется
      // только при focus. Выглядит как обычный текст-плейсхолдер.
      const itemsTable = (d.items_preview && d.items_preview.length) ? `
        <div class="rcard-items">
          <div class="rcard-items-head">
            <span>Позиции (${esc(d.items_count ?? d.items_preview.length)})</span>
          </div>
          <div class="rcard-items-list">
            ${d.items_preview.map(it => {
              const artEsc = (it.article||'').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
              const nameLine = it.match
                ? `<div class="rcard-it-name rcard-it-name-matched">${esc((it.match||'').substring(0,100))}${it.brand ? ` <span class="rcard-it-brand">· ${esc(it.brand)}</span>` : ''}</div>`
                : `<div class="rcard-it-fields" data-article="${artEsc}">
                     <input type="text" class="rcard-it-input rcard-it-input-name"  placeholder="название…"          data-field="name"  onkeydown="if(event.key==='Enter'){event.preventDefault();window.submitPartFields(this);}">
                     <input type="text" class="rcard-it-input rcard-it-input-brand" placeholder="бренд"              data-field="brand" onkeydown="if(event.key==='Enter'){event.preventDefault();window.submitPartFields(this);}">
                     <input type="text" class="rcard-it-input rcard-it-input-model" placeholder="модель техники"     data-field="model" onkeydown="if(event.key==='Enter'){event.preventDefault();window.submitPartFields(this);}" onblur="window.submitPartFields(this,true)">
                   </div>`;
              const priceBit = it.est_usd != null
                ? `<span class="rcard-it-price">${fmtMoney(it.est_usd)}</span>`
                : `<span class="rcard-it-price rcard-it-price-none">—</span>`;
              return `
              <div class="rcard-it">
                <div class="rcard-it-top">
                  <span class="rcard-it-art">${esc(it.article || '—')}</span>
                  <span class="rcard-it-meta">
                    <span class="rcard-it-qty">×${esc(it.qty)}</span>
                    ${priceBit}
                  </span>
                </div>
                ${nameLine}
              </div>`;
            }).join('')}
          </div>
          ${(d.items_count > d.items_preview.length) ? `<div class="rcard-items-more">… ещё ${d.items_count - d.items_preview.length} ${(d.items_count - d.items_preview.length) === 1 ? 'позиция' : 'позиций'}</div>` : ''}
        </div>
      ` : '';

      // ── CTAs ──
      const ctaCompare = (d.quotes_count > 0)
        ? `<button class="rcard-cta act-btn" data-action="compare_quotes" data-params='{"rfq_id":${parseInt(rid,10)}}' data-label="Сравнить котировки">📊 Сравнить котировки (${d.quotes_count})</button>`
        : '';
      const ctaProposal = (d.quotes_count > 0)
        ? `<button class="rcard-cta-ghost act-btn" data-action="generate_proposal" data-params='{"rfq_id":${parseInt(rid,10)}}' data-label="Скачать КП">📥 КП в PDF</button>`
        : '';
      const ctaAsk = `<button class="rcard-cta-ghost act-btn" data-action="ask_about_rfq" data-params='{"rfq_id":${parseInt(rid,10)}}' data-label="Спросить оператора">💬 Оператору</button>`;

      return `<div class="card rcard">
        <div class="rcard-head">
          <span class="rcard-emoji">📋</span>
          <div class="rcard-head-text">
            <div class="rcard-title">RFQ #${esc(d.number || d.id)} ${urgencyBadge} ${modeBadge}</div>
            <div class="rcard-sub">${esc((d.description || d.status_label || '').substring(0,140))}</div>
          </div>
          <span class="rcard-status">${esc(d.status_label || d.status || 'new')}</span>
        </div>
        ${metaRow}
        ${progressBlock}
        ${itemsTable}
        <div class="rcard-actions">${ctaCompare}${ctaProposal}${ctaAsk}</div>
      </div>`;
    },
    supplier_tracks(d) {
      // Каждый поставщик = полноценная shipment-карточка: 📦 + имя + статус,
      // meta-блок (Сумма/Оплата/Состав/В текущей стадии/SLA/ETA), «Дальше:», большие stages-пилюли,
      // плюс per-supplier выбор «ждать всех vs отправлять отдельно».
      const renderDecision = (dec) => {
        if (!dec || !Array.isArray(dec.buttons) || !dec.buttons.length) return '';
        const btns = dec.buttons.map(b => {
          const cls = 'st-dec-btn act-btn' + (b.active ? ' st-dec-btn-active' : '');
          const paramsJson = JSON.stringify(b.params || {}).replace(/"/g, '&quot;');
          const check = b.active ? '<span class="st-dec-check">✓</span>' : '';
          return `<button class="${cls}" data-action="${esc(b.action)}" data-params="${paramsJson}" data-label="${esc(b.label)}">${check}<span class="st-dec-ico">${esc(b.icon||'')}</span> ${esc(b.label)}</button>`;
        }).join('');
        return `<div class="st-decision st-decision-row"><div class="st-dec-row">${btns}</div></div>`;
      };
      const blocks = (d.items || []).map(it => {
        const metaHtml = (it.meta || []).map(m =>
          `<div class="spec-meta-item"><div class="spec-meta-lbl">${esc(m.lbl||'')}</div><div class="spec-meta-val">${esc(m.val||'')}</div></div>`
        ).join('');
        const actorHtml = (it.next_actor && it.next_actor !== '—') ? `
          <div class="ship-next"><span class="ship-next-lbl">Дальше:</span> <b>${esc(it.next_actor)}</b> ${esc(it.next_event||'')}</div>` : '';
        const stagesHtml = (it.stages || []).map(s =>
          `<div class="stage${s.done ? ' done' : ''}">${esc(s.label)}</div>`
        ).join('');
        // Кнопки по партии (напр. «📦 Состав партии») — позиции этого поставщика.
        const actionsHtml = (it.actions || []).map(a => {
          const pj = JSON.stringify(a.params || {}).replace(/"/g, '&quot;');
          return `<button class="st-dec-btn act-btn" data-action="${esc(a.action)}" data-params="${pj}" data-label="${esc(a.label)}">${esc(a.label)}</button>`;
        }).join('');
        return `<div class="st-block">
          <div class="card-row st-block-head">
            <div class="card-emoji">📦</div>
            <div class="card-info">
              <div class="card-title">${esc(it.supplier||'')}</div>
              <div class="card-sub">${esc(it.stage_label||'')}</div>
            </div>
          </div>
          ${metaHtml ? `<div class="spec-meta">${metaHtml}</div>` : ''}
          ${actorHtml}
          ${stagesHtml ? `<div class="stages">${stagesHtml}</div>` : ''}
          ${actionsHtml ? `<div class="st-decision st-decision-row"><div class="st-dec-row">${actionsHtml}</div></div>` : ''}
          ${renderDecision(it.decision)}
        </div>`;
      }).join('');
      return `<div class="card st-card">
        <div class="card-title st-card-title">${esc(d.title || 'По поставщикам')}</div>
        <div class="st-blocks">${blocks}</div>
      </div>`;
    },
    shipment(d) {
      // Два ряда карточек: сверху «план», снизу «факт» (разделены отступом).
      const planRow = (d.stages || []).map(s => {
        const plan = (typeof s.days === 'number' && s.days > 0) ? `план ${s.days}д` : '—';
        return `<div class="stage${s.done ? ' done' : ''}"><span class="stage-name">${esc(s.label)}</span><span class="stage-plan">${plan}</span></div>`;
      }).join('');
      const factRow = (d.stages || []).map(s => {
        let txt, cls;
        if (s.actual != null) {
          const over = (typeof s.days === 'number') && s.actual > s.days;
          cls = over ? 'stage-fact-over' : (s.state === 'current' ? 'stage-fact-cur' : 'stage-fact-ok');
          txt = (s.state === 'current' ? 'идёт ' : 'факт ') + s.actual + 'д';
        } else { cls = 'stage-fact-none'; txt = 'факт —'; }
        return `<div class="stage stage-fact-cell"><span class="stage-fact ${cls}">${esc(txt)}</span></div>`;
      }).join('');
      const stagesBlock = (d.stages && d.stages.length)
        ? `<div class="stages stages-plan">${planRow}</div><div class="stages stages-fact">${factRow}</div>` : '';
      const fmt = (v) => (Number(v)||0).toLocaleString('en-US', {maximumFractionDigits: 0});
      // Метаблок «важной информации» — деньги, оплата, ETA, кто держит мяч,
      // перевозчик/трек-номер (последние два — только оператор).
      const meta = [];
      if (d.total) meta.push({lbl:'Сумма', val:`$${fmt(d.total)}`});
      if (d.payment_status_label) meta.push({lbl:'Оплата', val:esc(d.payment_status_label)});
      if (d.positions) {
        const wPart = d.weight_kg ? ` · ${d.weight_kg} кг` : '';
        meta.push({lbl:'Состав', val:`${d.positions} поз · ${d.qty_total||0} шт${wPart}`});
      }
      if (typeof d.days_in_stage === 'number') {
        meta.push({lbl:'В текущей стадии', val:`${d.days_in_stage} дн`});
      }
      if (d.deadline) {
        const planTxt = d.total_planned_days ? ` · план ${d.total_planned_days} дн` : '';
        meta.push({lbl:'Крайняя дата', val:`${esc(d.deadline)}${planTxt}`});
      }
      if (d.sla_label) {
        meta.push({lbl:'Срок этапа', val:esc(d.sla_label)});
      }
      if (d.eta) meta.push({lbl:'ETA', val:esc(d.eta)});
      if (d.carrier) meta.push({lbl:'Перевозчик', val:esc(d.carrier)});
      if (d.tracking_number) meta.push({lbl:'Трек', val:esc(d.tracking_number)});
      const metaHtml = meta.length
        ? `<div class="spec-meta">${meta.map(m =>
            `<div class="spec-meta-item"><div class="spec-meta-lbl">${m.lbl}</div><div class="spec-meta-val">${m.val}</div></div>`
          ).join('')}</div>` : '';
      const actorHtml = d.next_actor && d.next_actor !== '—' ? `
        <div class="ship-next">
          <span class="ship-next-lbl">Дальше:</span>
          <b>${esc(d.next_actor)}</b> ${esc(d.next_event || '')}
        </div>` : '';
      const partsRows = (d.parts || []).map(p =>
        `<div class="sh-part">
          <div class="sh-part-name">${esc(p.supplier)}</div>
          <div class="sh-part-stage">${esc(p.stage_label || '')}</div>
          <div class="sh-part-amt">$${fmt(p.amount)} <span class="sh-part-pct">· ${p.amount_pct||0}%</span></div>
        </div>`
      ).join('');
      const partsHtml = (d.parts && d.parts.length >= 2)
        ? `<div class="sh-parts-head">📦 По партиям / поставщикам — ${d.parts.length}</div>
           <div class="sh-parts">${partsRows}</div>` : '';
      return `<div class="card">
        <div class="card-row">
          <div class="card-emoji">🚢</div>
          <div class="card-info">
            <div class="card-title">Заказ ORD-${esc(d.order_id)}</div>
            <div class="card-sub">${esc(d.status_label || d.status || '')}</div>
          </div>
        </div>
        ${metaHtml}
        ${actorHtml}
        ${partsHtml}
        ${stagesBlock}
      </div>`;
    },
    wallet(d) {
      // Чистый ledger: шапка с балансом + 30д статистика + операции
      // сгруппированные по дате. Без иконок, без банковской «кредитки».
      const fmt = (v) => (Number(v)||0).toLocaleString('en-US', {maximumFractionDigits: 0});
      const splitAmount = (v) => {
        const s = (Number(v)||0).toFixed(2);
        const [whole, dec] = s.split('.');
        return [Number(whole).toLocaleString('en-US'), dec];
      };
      const [whole, dec] = splitAmount(d.balance);
      // Группируем по дате
      const groups = {};
      (d.transactions || []).forEach(t => {
        const k = t.date;
        if (!groups[k]) groups[k] = [];
        groups[k].push(t);
      });
      const groupHtml = Object.keys(groups).map(date => {
        const rows = groups[date].map(t => {
          const sign = t.is_in ? '+' : '−';
          const cls  = t.is_in ? 'wal-amt-in' : 'wal-amt-out';
          const refIdMatch = t.order_ref ? String(t.order_ref).match(/\d+/) : null;
          const refId = refIdMatch ? parseInt(refIdMatch[0], 10) : null;
          const ref = refId
            ? `<button type="button" class="wal-ref wal-ref-click act-btn" data-action="get_order_detail" data-params='{"order_id":${refId}}' data-label="Заказ ORD-${refId}">ORD-${refId}</button>`
            : '';
          return `
            <div class="wal-row">
              <span class="wal-time">${esc(t.time)}</span>
              <span class="wal-desc">${esc(t.label || '')} ${ref}</span>
              <span class="wal-amt ${cls}">${sign}$${fmt(t.amount)}</span>
            </div>`;
        }).join('');
        return `<div class="wal-group">
          <div class="wal-date">${esc(date)}</div>
          <div class="wal-rows">${rows}</div>
        </div>`;
      }).join('');
      return `<div class="card wal-card">
        <div class="wal-head">
          <div class="wal-head-lbl">Депозит на счёте</div>
          <div class="wal-head-amount">
            <span class="wal-head-ccy">$</span>${esc(whole)}<span class="wal-head-dec">.${esc(dec)}</span>
            <span class="wal-head-curr">${esc(d.currency || 'USD')}</span>
          </div>
        </div>
        ${groupHtml ? `<div class="wal-txs-head">Последние операции</div>${groupHtml}` : '<div class="wal-empty">Движений ещё не было.</div>'}
      </div>`;
    },
    supplier(d) {
      return `<div class="card">
        <div class="card-row">
          <div class="card-emoji">🏭</div>
          <div class="card-info">
            <div class="card-title">${esc(d.name)}</div>
            <div class="card-sub">${d.kpi ? Object.entries(d.kpi).map(([k,v]) => `${k}: ${v}`).join(' · ') : ''}</div>
          </div>
        </div>
      </div>`;
    },
    comparison(d) {
      const headers = (d.headers || []).map(h => `<th>${esc(h)}</th>`).join('');
      const rows = (d.rows || []).map(r =>
        `<tr>${r.map(cell => `<td>${esc(String(cell))}</td>`).join('')}</tr>`
      ).join('');
      return `<div class="card"><table class="ctable"><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table></div>`;
    },
    chart(d) {
      const items = d.items || [];
      const max = Math.max(...items.map(i => i.value || 0)) || 1;
      const bars = items.map(i =>
        `<div class="chart-bar" style="height:${(i.value/max*100)|0}%;${i.color ? 'background:'+i.color : ''}"><div class="chart-bar-label">${esc(i.label)}</div></div>`
      ).join('');
      return `<div class="card">
        <div class="card-title" style="margin-bottom:8px;">${esc(d.title || '')}</div>
        <div class="chart-bars">${bars}</div>
      </div>`;
    },
    file(d) {
      return `<div class="card">
        <div class="card-row">
          <div class="card-emoji">📎</div>
          <div class="card-info">
            <div class="card-title">${esc(d.name || tr('card.file'))}</div>
            <div class="card-sub">${esc(d.size || '')}</div>
          </div>
        </div>
      </div>`;
    },
    table(d) { return renderers.comparison(d); },

    // ── Bar chart: SVG-бары с подписями ──
    // Раньше «chart» рендерил только KPI-цифры, без визуальных пропорций.
    // bar_chart рисует горизонтальные бары — заметно нагляднее для
    // распределений (статусы, месяцы, поставщики).
    bar_chart(d) {
      const items = d.items || [];
      const color = d.color || '#64B5F6';
      if (!items.length) return '';
      const maxV = Math.max(1, ...items.map(i => Number(i.value) || 0));
      const barW = 100;  // % от контейнера
      const rows = items.map(it => {
        const v = Number(it.value) || 0;
        const pct = Math.max(2, Math.round((v / maxV) * barW));
        return `<div class="bch-row">
          <div class="bch-label">${esc(it.label || '')}</div>
          <div class="bch-track">
            <div class="bch-fill" style="width:${pct}%;background:${esc(color)}"></div>
          </div>
          <div class="bch-val">${esc(String(it.value))}</div>
        </div>`;
      }).join('');
      return `<div class="card bch-card">
        <div class="card-title bch-title">${esc(d.title || '')}</div>
        <div class="bch-body">${rows}</div>
      </div>`;
    },

    // ── Spec results: KPIs + detailed table + footer ──
    // Helper для матрицы выбора 3 mode × 3 incoterm внутри spec_results
    // не используется напрямую (вызывается через renderShippingMatrix ниже)
    _shipping_matrix_stub() { return ''; },
    shipping_options(d) {
      const rows = (d.rows || []).map(r => {
        const selectedCls = r.selected ? ' so-row-selected' : '';
        return `<div class="so-row${selectedCls}" data-action="shipping_apply" data-params='${esc(JSON.stringify({order_id: d.order_id, mode: r.mode, incoterm: r.incoterm}))}' data-label="Применить ${esc(r.mode_label)} ${esc(r.incoterm)}">
          <div class="so-head">
            <span class="so-mode">${esc(r.mode_label)}</span>
            <span class="so-inc">${esc(r.incoterm)}</span>
            ${r.selected ? '<span class="so-active-tag">текущий</span>' : ''}
          </div>
          <div class="so-desc">${esc(r.incoterm_desc)}</div>
          <div class="so-meta">
            <span class="so-ship">Доставка: ${fmtMoney(r.shipping, d.currency || 'USD')}${r.days ? ` · ~${r.days}д` : ''}</span>
            <span class="so-landed">Landed: <b>${fmtMoney(r.landed, d.currency || 'USD')}</b></span>
          </div>
        </div>`;
      }).join('');
      return `<div class="card so-card">
        <div class="card-title">${esc(d.title || tr('card.shipping'))}</div>
        <div class="so-rows">${rows || '<div class="cat-empty">Нет доступных вариантов доставки</div>'}</div>
      </div>`;
    },
    spec_results(d) {
      const stkClass = (s) => ({in_stock:'in', backorder:'back', not_found:'no'})[s] || 'in';
      const stkLabel = (s) => ({in_stock:tr('stock.in_stock'), backorder:'Backorder', not_found:'—'})[s] || s;
      const condLabel = (c) => {
        if (c === 'oem') return '<span class="spec-cond-oem">OEM</span>';
        if (c === 'analogue') return '<span class="spec-cond-an">Аналог</span>';
        return esc(c || '');
      };
      // Для buyer бэк не присылает supplier_status_badge — скрываем колонку
      // «Поставщик» полностью (для оператора/админа она показывается).
      const showSupplierCol = (d.items || []).some(it => it.supplier_status_badge);
      // Иконка колонки «Доставка» по способу перевозки: море 🚢 / авиа ✈️ / авто 🚚.
      // (Раньше всегда был грузовик — даже когда груз идёт морем.)
      const _shipMode = d.shipping_mode || ((d.items || []).find(it => it.ship_mode) || {}).ship_mode || '';
      const shipIcon = {sea: '🚢', air: '✈️', auto: '🚚'}[_shipMode] || '📦';
      // Режим котировки: продавец заполняет цену/тип/срок. Тогда у таблицы свой
      // компактный набор колонок (Тип и Срок — отдельными колонками, без Weight/
      // Поставщик/Доставка), чтобы Name не сжимался и ничего не «ехало».
      const qfMode = !!d.editable_price;
      const rows = (d.items || []).map((it, idx) => {
        if (it.status === 'not_found' && !it.id) {
          return `<tr><td><span class="spec-stk no"><span class="spec-stk-dot"></span>—</span></td>
            <td class="spec-row-num">${idx+1}</td>
            <td colspan="5" class="spec-empty-row" style="text-align:left;">— нет предложений —</td>
            <td>${esc(it.qty || '')}</td><td></td><td></td><td></td></tr>`;
        }
        // Доставка: текстовые лейблы, не эмодзи. Клик → таймлайн отгрузки
        // (track_shipment) — оператор сразу видит где груз и что дальше.
        let shipCell = '—';
        if (it.ship_cost || it.ship_mode || it.ship_days) {
          const modeLbl = {sea: 'море', air: 'авиа', auto: 'авто'}[it.ship_mode] || '';
          const cost = it.ship_cost ? ` ${fmtMoney(it.ship_cost, 'USD')}` : '';
          const days = it.ship_days ? ` · ~${it.ship_days}д` : '';
          const inner = `${modeLbl}${cost}${days}`.trim() || '—';
          if (it.order_id) {
            const p = esc(JSON.stringify({order_id: it.order_id}));
            shipCell = `<button type="button" class="ship-link"
              data-action="track_shipment" data-params='${p}' data-label="Открыть таймлайн"
              title="Клик — открыть трекинг отгрузки">${inner}</button>`;
          } else {
            shipCell = inner;
          }
        }
        // Поставщик: бейдж статуса + рейтинг + кнопка drill-down (если есть альт-офферы)
        const statusCls = {trusted:'sp-trusted', sandbox:'sp-sandbox', risky:'sp-risky'}[it.supplier_status] || '';
        let supplierCell;
        if (it.supplier_status_badge) {
          const badgeInner = `${esc(it.supplier_status_badge)} · ${it.supplier_rating}${it.alt_offers > 0 ? ` <span class="sp-alt">+${it.alt_offers}</span>` : ''}`;
          // Если operator (есть supplier_id) — делаем кликабельным «связаться»
          if (it.supplier_id) {
            const pAttr = esc(JSON.stringify({seller_id: it.supplier_id, seller_username: it.supplier_username || ''}));
            supplierCell = `<button type="button" class="sp-badge sp-badge-clickable ${statusCls}"
              data-action="contact_supplier" data-params='${pAttr}' data-label="Связаться с поставщиком"
              title="Клик — связаться с поставщиком ${esc(it.supplier_username || '')}">${badgeInner}</button>`;
          } else {
            supplierCell = `<span class="sp-badge ${statusCls}" title="Рейтинг ${it.supplier_rating}/100${it.alt_offers > 0 ? ' · клик по строке для сравнения' : ''}">${badgeInner}</span>`;
          }
        } else {
          supplierCell = '—';
        }
        // Вся строка кликабельна — раскрывает inline-список поставщиков
        // прямо в таблице (без перехода в отдельную карточку).
        const clickable = it.status === 'in_stock' && it.id && (it.alt_suppliers || []).length > 0;
        const rowAttrs = clickable
          ? ` class="spec-row-clickable spec-row-toggle" data-oem="${esc(it.id)}" data-row-idx="${idx}" title="Клик — все поставщики этой позиции"`
          : '';
        // Детали — отдельная мини-таблица внутри одной table-row, прилипшая
        // sticky к левому краю видимой области. Width задаём через JS по
        // wrap.clientWidth, чтобы блок всегда влезал в видимую часть карточки.
        let detailRow = '';
        if (clickable) {
          const suppliers = it.alt_suppliers || [];
          const supRows = suppliers.map((s, i) => {
            const statusCls = ({trusted:'sp-trusted',sandbox:'sp-sandbox',risky:'sp-risky'})[s.status] || '';
            return `<tr class="${s.is_primary ? 'as-row as-primary' : 'as-row'}">
              <td class="as-rank">${i + 1}</td>
              <td class="as-label">${esc(s.label)}</td>
              <td><span class="sp-badge ${statusCls}">${esc(s.status_badge)}</span></td>
              <td class="as-num">${s.rating}</td>
              <td class="as-num as-price">${fmtMoney(s.price, s.currency)}</td>
              <td class="as-cond">${esc(s.condition || '')}</td>
              <td class="as-num">${s.stock || '—'}</td>
              <td class="as-wh" title="${esc(s.warehouse || '')}">${esc(s.warehouse || '—')}</td>
              <td class="as-num as-score">${s.score != null ? s.score : '—'}</td>
            </tr>`;
          }).join('');
          detailRow = `<tr class="spec-detail-row" data-detail-for="${idx}" style="display:none;">
            <td colspan="10" class="spec-detail-cell">
              <div class="as-block">
                <div class="as-title">🔍 ${suppliers.length} поставщик${suppliers.length === 1 ? '' : (suppliers.length < 5 ? 'а' : 'ов')} по OEM <b>${esc(it.id)}</b></div>
                <table class="as-table">
                  <thead><tr>
                    <th class="as-col-rank">#</th><th class="as-col-label">Поставщик</th><th class="as-col-status">Статус</th><th class="as-col-rating">Рейтинг</th><th class="as-col-price">Цена EXW</th><th class="as-col-cond">Состояние</th><th class="as-col-stock">Остаток</th><th class="as-col-wh">Склад</th><th class="as-col-score">Score</th>
                  </tr></thead>
                  <tbody>${supRows}</tbody>
                </table>
              </div>
            </td>
          </tr>`;
        }
        // ── Маркер режима подбора per-row (ТЗ §4) ──
        // auto = зелёная точка, semi = жёлтый ⚠, manual = красный ✕
        const MODE_DOT = {
          auto:   '<span class="spec-mode-dot spec-mode-dot-auto" title="AUTO · готово к покупке">●</span>',
          semi:   '<span class="spec-mode-dot spec-mode-dot-semi" title="SEMI · нужно подтверждение оператора">⚠</span>',
          manual: '<span class="spec-mode-dot spec-mode-dot-manual" title="MANUAL · нет в каталоге, нужна рассылка">✕</span>',
        };
        const modeDot = MODE_DOT[it.item_mode] || '';
        const freshHint = (it.is_fresh === false && it.freshness_days != null)
          ? ` <span class="spec-stale" title="Данные устарели на ${it.freshness_days} дн — попадает в SEMI">⏰${it.freshness_days}д</span>` : '';
        // Per-позиционные поля котировки (только когда форма редактируемая)
        const qfEditable = d.editable_price && it.rfq_item_id != null;
        const condSel = qfEditable
          ? `<select class="qf-item-input qf-cond" name="cond_${esc(String(it.rfq_item_id))}" title="OEM или аналог" style="width:90px;max-width:100%;font-size:13px;font-weight:700;padding:4px 5px;border-radius:5px;"><option value="oem"${(it.condition||'oem')==='oem'?' selected':''}>OEM</option><option value="analog"${it.condition==='analog'?' selected':''}>Аналог</option></select>`
          : '';
        const leadInp = qfEditable
          ? `<input class="qf-item-input qf-lead" type="number" min="1" name="lead_${esc(String(it.rfq_item_id))}" placeholder="дней" value="${it.delivery_days!=null?esc(String(it.delivery_days)):''}" title="Срок поставки этой позиции (дн)" style="width:82px;max-width:100%;font-size:13px;font-weight:700;padding:4px 5px;border-radius:5px;" />`
          : '';
        // Рыночный ориентир по OEM — под полем цены (продавцу: не продешевить).
        const qfMktHint = (it.market_usd != null)
          ? `<div class="qf-mkt" title="Ориентир рыночной цены по этому OEM из каталога">рынок ≈ $${esc(String(it.market_usd))}${(it.market_lo != null && it.market_hi != null && it.market_hi > it.market_lo) ? ` · ${esc(String(it.market_lo))}–${esc(String(it.market_hi))}` : ''}</div>`
          : '';
        const priceCellQf = (d.editable_price && it.rfq_item_id != null)
          ? `<div class="qf-price-wrap"><span class="qf-currency">${esc(it.currency || 'USD')}</span><input class="qf-price-input" type="number" step="0.01" min="0" name="price_${esc(String(it.rfq_item_id))}" data-qty="${Number(it.qty) || 0}" data-in-catalog="${it.in_catalog ? '1' : '0'}" value="${Number(it.price || 0).toFixed(2)}" /></div>${qfMktHint}`
          : fmtMoney(it.price, it.currency || 'USD');
        // Чип «аналог из вашего каталога» — для позиций, которых нет как OEM.
        const qfAnalogChip = (it.analog && it.analog.price_usd != null)
          ? `<button type="button" class="qf-analog-chip" data-analog-price="${esc(String(it.analog.price_usd))}" data-analog-article="${esc(it.analog.article || '')}" title="Ваш аналог ${esc(it.analog.article || '')} в наличии — подставить цену и тип «Аналог»">🔄 аналог: ${esc(it.analog.article || '—')} · $${esc(String(Math.round(it.analog.price_usd)))}</button>`
          : '';
        if (qfMode) {
          // Форма котировки: Тип (OEM/Аналог) и Срок — отдельными колонками.
          // Weight / Поставщик / Доставка убраны (продавец сам поставщик, вес — из
          // спеки покупателя, способ доставки общий) → Name получает много места.
          const condFallback = it.condition === 'analog' ? 'Аналог' : (it.condition === 'oem' ? 'OEM' : '—');
          return `<tr${rowAttrs}>
            <td><span class="spec-stk ${it.stock_class || stkClass(it.status)}"><span class="spec-stk-dot"></span>${esc(it.stock_label != null ? it.stock_label : stkLabel(it.status))}</span></td>
            <td class="spec-row-num">${modeDot}${idx+1}</td>
            <td><a class="spec-id-link">${esc(it.id || '')}</a></td>
            <td><div class="spec-name-cell">${specNameHtml(it)}${it.tag ? `<span class="spec-mini-tag">${esc(it.tag)}</span>` : ''}${freshHint}</div>${qfAnalogChip}</td>
            <td>${esc(it.brand || '')}</td>
            <td class="qf-cond-cell">${condSel || condFallback}</td>
            <td class="spec-price">${priceCellQf}</td>
            <td>${esc(it.qty || '')}</td>
            <td class="spec-ship qf-lead-cell">${leadInp || (it.delivery_days != null ? esc(String(it.delivery_days)) + ' дн' : '—')}</td>
          </tr>${detailRow}`;
        }
        return `<tr${rowAttrs}>
          <td><span class="spec-stk ${it.stock_class || stkClass(it.status)}"><span class="spec-stk-dot"></span>${esc(it.stock_label != null ? it.stock_label : stkLabel(it.status))}</span></td>
          <td class="spec-row-num">${modeDot}${idx+1}</td>
          <td><a class="spec-id-link">${esc(it.id || '')}</a></td>
          <td><div class="spec-name-cell">${specNameHtml(it)}${it.tag ? `<span class="spec-mini-tag">${esc(it.tag)}</span>` : ''}${freshHint}</div></td>
          <td>${esc(it.brand || '')}</td>
          <td class="spec-price">${(d.editable_price && it.rfq_item_id != null)
            ? `<div class="qf-price-wrap"><span class="qf-currency">${esc(it.currency || 'USD')}</span><input class="qf-price-input" type="number" step="0.01" min="0" name="price_${esc(String(it.rfq_item_id))}" data-qty="${Number(it.qty) || 0}" value="${Number(it.price || 0).toFixed(2)}" /></div>`
            : fmtMoney(it.price, it.currency || 'USD')}</td>
          <td>${esc(it.qty || '')}</td>
          <td>${esc(it.weight || '')}</td>
          ${showSupplierCol ? `<td>${supplierCell}</td>` : ''}
          <td class="spec-ship">${shipCell}${leadInp}</td>
        </tr>${detailRow}`;
      }).join('');
      const detailBlocks = '';

      const moreLink = d.more_count
        ? `<div class="spec-more">... ${d.more_count} ещё · <a href="#" onclick="return false;">раскрыть полный список</a></div>`
        : '';

      const found = d.found || 0;
      const analogue = d.analogue || 0;
      const notFound = d.not_found || 0;
      const offers = d.offers_count;
      const sellers = d.sellers_count;
      const bestMix = d.best_mix;

      const subParts = [];
      if (offers != null) subParts.push(`${offers} предложений`);
      if (sellers != null) subParts.push(`${sellers} поставщиков`);
      if (bestMix != null) subParts.push(`best mix ${fmtMoney(bestMix, d.currency || 'USD')}`);

      // ── Бейдж режима подбора (ТЗ §4) ──
      const MODE_BADGE = {
        auto:   {label: '⚡ AUTO',          cls: 'spec-mode-auto',   hint: 'найдено в каталоге · Надёжные поставщики · свежие данные'},
        semi:   {label: '⏳ SEMI',          cls: 'spec-mode-semi',   hint: 'нужно подтверждение оператора (~15 мин)'},
        manual: {label: '📨 MANUAL',        cls: 'spec-mode-manual', hint: 'позиции нет в каталоге — нужна рассылка поставщикам'},
        mixed:  {label: '🔀 MIXED',         cls: 'spec-mode-mixed',  hint: 'часть позиций auto, часть требует операторской работы'},
      };
      const mb = MODE_BADGE[d.card_mode] || null;
      const modeBadge = mb ? `<span class="spec-mode-badge ${mb.cls}" title="${esc(mb.hint)}">${esc(mb.label)}</span>` : '';
      const modeBreakdown = d.card_mode === 'mixed'
        ? `<span class="spec-mode-mini">${d.auto_count||0} auto · ${d.semi_count||0} semi · ${d.manual_count||0} manual</span>`
        : '';
      // Опциональный meta-блок (для заказов: статус/оплата/покупатель/резерв)
      const metaRows = (d.meta && d.meta.length) ? `
        <div class="spec-meta">
          ${d.meta.map(m => `<div class="spec-meta-item"><span class="spec-meta-lbl">${esc(m.label||'')}</span><span class="spec-meta-val">${esc(m.value||'')}</span></div>`).join('')}
        </div>` : '';
      // Котировка (d.qf) рендерится этим же рендером: добавляем класс qf-card,
      // дата-атрибуты RFQ, кастомные подписи KPI и подвал-форму с кнопкой.
      const _kpiL = d.kpi_labels || ['Found', 'Analogue', 'Not found'];
      const _qfAttrs = d.qf
        ? ` data-rfq-id="${esc(String(d.qf.rfq_id || ''))}" data-parent-quote-id="${esc(String(d.qf.parent_quote_id || ''))}" data-direction="${esc(String(d.qf.direction || 'seller_to_buyer'))}"`
        : '';
      return `<div class="card spec${d.qf ? ' qf-card' : ''}"${_qfAttrs}>
        <div class="spec-head">
          <div class="spec-head-row">
            <div class="spec-title">${esc(d.title || tr('card.match_results'))} ${modeBadge}</div>
            <div class="spec-title-meta">${d.title_meta != null ? esc(String(d.title_meta)) : esc(subParts.join(' · '))} ${modeBreakdown}</div>
          </div>
        </div>
        ${metaRows}
        <div class="spec-kpis">
          <div class="spec-kpi"><div class="spec-kpi-num green">${found}</div><div class="spec-kpi-label">${esc(_kpiL[0])}</div></div>
          <div class="spec-kpi"><div class="spec-kpi-num amber">${analogue}</div><div class="spec-kpi-label">${esc(_kpiL[1])}</div></div>
          <div class="spec-kpi"><div class="spec-kpi-num red">${notFound}</div><div class="spec-kpi-label">${esc(_kpiL[2])}</div></div>
        </div>
        <div class="spec-tbl-wrap">
          <table class="spec-tbl spec-tbl-fixed">
            <colgroup>${qfMode
              ? '<col style="width:11%"><col style="width:4%"><col style="width:13%"><col style="width:19%"><col style="width:12%"><col style="width:10%"><col style="width:13%"><col style="width:6%"><col style="width:12%">'
              : (showSupplierCol
              ? '<col style="width:12%"><col style="width:3%"><col style="width:12%"><col style="width:11%"><col style="width:13%"><col style="width:11%"><col style="width:5%"><col style="width:8%"><col style="width:13%"><col style="width:12%">'
              : '<col style="width:12%"><col style="width:3%"><col style="width:13%"><col style="width:13%"><col style="width:17%"><col style="width:11%"><col style="width:6%"><col style="width:9%"><col style="width:16%">')}</colgroup>
            <thead><tr>${qfMode
              ? '<th>Stock</th><th>#</th><th>ID</th><th>Name</th><th>Brand</th><th>Тип</th><th>Price</th><th>Qty</th><th>Срок</th>'
              : `<th>Stock</th><th>#</th><th>ID</th><th>Name</th><th>Brand</th><th>Price</th><th>Qty</th><th>Weight</th>${showSupplierCol ? '<th>Поставщик</th>' : ''}<th>${shipIcon} Доставка${d.dest_country ? ' → ' + esc(d.dest_country) : ''}</th>`}
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        <div class="spec-details-host">${detailBlocks}</div>
        ${moreLink}
        <div class="spec-foot">
          <div class="spec-foot-info">${
            (d.editable_price && (d.items || []).some(it => it.in_catalog) && (d.items || []).some(it => !it.in_catalog))
              ? `<button class="qf-keep-mine" type="button" title="Оставить только позиции из вашего каталога — у остальных очистится цена; нажмите ещё раз, чтобы вернуть">📦 Только мои позиции</button>`
              : (esc(d.foot_info || '') + (d.shipping_matrix ? ' · доставка ↓' : ''))
          }</div>
          <div class="spec-foot-total"${d.qf ? ' data-total' : ''}>${d.total != null ? fmtMoney(d.total, d.currency || 'USD') : ''}</div>
        </div>
        ${renderShippingMatrix(d)}
        ${d.qf ? _qfFooter(d) : ''}
      </div>`;
    },

    // ── Supplier top: ranked list of 3 suppliers ──
    supplier_top(d) {
      const rows = (d.suppliers || []).map((s, idx) => {
        const rankClass = idx === 0 ? 'gold' : '';
        const stars = s.rating ? `<span class="stop-stars">★ ${esc(s.rating)}</span>` : '';
        return `<div class="stop-row">
          <div class="stop-rank ${rankClass}">${idx+1}</div>
          <div class="stop-info">
            <div><span class="stop-name">${esc(s.name)}</span>${stars}</div>
            <div class="stop-meta">${esc(s.coverage || '')}${s.lead_time ? ' · ср. лидтайм ' + esc(s.lead_time) : ''}${s.note ? ' · ' + esc(s.note) : ''}</div>
          </div>
          <div>
            <div class="stop-price-label">${esc(s.price_label || 'total')}</div>
            <div class="stop-price">${fmtMoney(s.total, s.currency || 'USD')}</div>
          </div>
        </div>`;
      }).join('');
      return `<div class="card stop">${rows}</div>`;
    },
  };
  // Экспорт для inline DOM-апдейтов (например после sqAdvance —
  // перерисовать карточку pipeline'а на месте без переотрисовки чата).
  window.__cardRenderers = renderers;

  // Поиск по каталогу (по артикулу/OEM). Вызывается из поля в карточке catalog.
  // warehouseId: число (склад), 0 (без склада), null (искать по всем складам).
  // Живой поиск по каталогу: фильтрует ПРИ НАБОРЕ (debounce 400мс) и обновляет
  // список ВНУТРИ текущей карточки — без новых сообщений в чате, поле сохраняет
  // фокус. immediate=true (Enter/кнопка) — без задержки.
  var _catSearchTimer = null;
  window.__catalogLiveSearch = function(inp, warehouseId, immediate) {
    if (!inp) return;
    var card = inp.closest('.cat-card');
    if (!card) return;
    var q = (inp.value || '').trim();
    clearTimeout(_catSearchTimer);
    var run = function() {
      var params = { q: q };
      if (warehouseId !== null && warehouseId !== undefined && warehouseId !== '') {
        params.warehouse_id = warehouseId;
      }
      fetch('/api/assistant/action/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
        credentials: 'same-origin',
        body: JSON.stringify({ action: 'seller_catalog', params: params }),
      }).then(function(r){ return r.json(); }).then(function(resp){
        // Гонка: пока ждали ответ, юзер мог изменить запрос — игнорим устаревший.
        if ((inp.value || '').trim() !== q) return;
        var cards = (resp && resp.cards) || [];
        var cat = cards.find(function(c){ return c.type === 'catalog'; });
        var rowsEl = card.querySelector('.cat-rows');
        if (!rowsEl) return;
        if (cat) {
          var tmp = document.createElement('div');
          tmp.innerHTML = window.__cardRenderers.catalog(cat.data);
          var nr = tmp.querySelector('.cat-rows');
          rowsEl.innerHTML = nr ? nr.innerHTML : '';
          // обновляем счётчик в заголовке
          var title = card.querySelector('.card-title');
          var oldC = card.querySelector('.cat-counter');
          if (oldC) oldC.remove();
          var nc = tmp.querySelector('.cat-counter');
          if (nc && title) title.insertAdjacentHTML('beforeend', ' ' + nc.outerHTML);
        } else if (q) {
          rowsEl.innerHTML = '<div class="cat-empty"></div>';
          rowsEl.firstChild.textContent = 'Ничего не найдено по запросу «' + q + '»';
        }
        // q === '' и нет catalog-карточки (бэкенд отдал список складов) — не трогаем.
      }).catch(function(){});
    };
    if (immediate) { run(); } else { _catSearchTimer = setTimeout(run, 400); }
  };
  // Совместимость со старым именем (на случай stale-разметки).
  window.__catalogSearch = function(inp, warehouseId) {
    return window.__catalogLiveSearch(inp, warehouseId, true);
  };

  // Inline-сохранение цены/остатка товара из карточки каталога (edit_product).
  // Наличие управляется остатком: > 0 → «в наличии». IDOR-safe — бэкенд фильтрует
  // Part по seller=user. Сводку строки обновляем на месте (без перерисовки списка).
  document.addEventListener('click', function(e) {
    var btn = e.target && e.target.closest && e.target.closest('.cat-save-edit');
    if (!btn) return;
    e.preventDefault();
    var row = btn.closest('.cat-row');
    if (!row) return;
    var params = { part_id: btn.dataset.partId };
    row.querySelectorAll('.cat-edit-field').forEach(function(inp){
      if (inp.dataset.field) params[inp.dataset.field] = inp.value;
    });
    btn.disabled = true; btn.textContent = '⏳ Сохраняю…';
    fetch('/api/assistant/action/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      credentials: 'same-origin',
      body: JSON.stringify({ action: 'edit_product', params: params }),
    }).then(function(r){ return r.json(); }).then(function(resp){
      btn.disabled = false;
      if (resp && resp.error) { btn.textContent = '💾 Сохранить'; if (window.toast) window.toast('❌ ' + resp.error, 3000); return; }
      // Обновляем сводку строки на месте: артикул / название / цена / наличие.
      var setTxt = function(s, v){ var el = row.querySelector(s); if (el && v != null && v !== '') el.textContent = v; };
      setTxt('.cat-art', params.oem_number);
      setTxt('.cat-name', params.title);
      var ccy = params.currency || btn.dataset.ccy || 'USD';
      var nPrice = parseFloat(params.price);
      var sym = ({USD:'$', EUR:'€', RUB:'₽', CNY:'¥', GBP:'£'})[ccy] || '';
      var money = isNaN(nPrice) ? '—'
        : (sym ? sym + Math.round(nPrice).toLocaleString('en-US')
               : Math.round(nPrice).toLocaleString('en-US') + ' ' + ccy);
      var priceEl = row.querySelector('.cat-price');
      if (priceEl) priceEl.textContent = money;
      var nStock = Math.max(0, parseInt(params.stock_qty, 10) || 0);
      var badge = row.querySelector('.cat-stock-badge');
      if (badge) {
        if (nStock > 0) { badge.className = 'cat-chip cat-chip-green cat-stock-badge'; badge.textContent = nStock + ' шт'; }
        else { badge.className = 'cat-chip cat-chip-gray cat-stock-badge'; badge.textContent = 'нет в наличии'; }
      }
      btn.textContent = '✓ Сохранено';
      setTimeout(function(){ btn.textContent = '💾 Сохранить'; }, 1500);
      if (window.toast) window.toast('✓ Сохранено', 2000);
    }).catch(function(){
      btn.disabled = false; btn.textContent = '💾 Сохранить';
      if (window.toast) window.toast('Ошибка сохранения', 3000);
    });
  });

  // S3 — whitelist допустимых типов карточек. Источник истины: ключи `renderers`.
  // Любой неизвестный тип (включая случай когда AI/бэкенд вернул мусор) —
  // отбрасываем тихо, не дампим в DOM. Раньше fallback renderUnknownCard
  // выводил произвольное содержимое — потенциальный XSS-вектор и
  // дезориентирующий UX.
  const ALLOWED_CARD_TYPES = new Set(Object.keys(renderers));
  // Типы, которые ТОЛЬКО фронтенд имеет право конструировать (raw HTML и пр.).
  // renderCards() блокирует их при `from_server: true` пометке — это
  // защита от того, что backend случайно начнёт продьюсить XSS-векторы.
  const FRONTEND_ONLY_TYPES = new Set(['raw_html']);

  function renderCards(cards, opts) {
    if (!cards || !cards.length) return '';
    const fromServer = !!(opts && opts.fromServer);
    return '<div class="cards">' + cards.map(c => {
      if (!c || !ALLOWED_CARD_TYPES.has(c.type)) {
        console.warn('renderCards: type not in whitelist —', c && c.type);
        return '';
      }
      if (fromServer && FRONTEND_ONLY_TYPES.has(c.type)) {
        console.error('renderCards: SECURITY — backend tried to use frontend-only type:', c.type);
        return '';
      }
      try { return renderers[c.type](c.data || {}); }
      catch(err) {
        console.error('Card renderer crashed for', c.type, err, c.data);
        return '';
      }
    }).join('') + '</div>';
  }

  // Fallback renderer for unknown/broken card types — dumps key-value pairs
  // Виджет матрицы 3 mode × 3 incoterm внутри карточки spec_results.
  // По клику на вариант формируется quick_order с выбранными params.
  // Форма ввода адреса доставки и места прибытия для CIP/DDP.
  // Без этих данных нельзя корректно посчитать пошлину/НДС/last-mile.
  function renderDeliveryForm(d) {
    if (!d.product_ids && !d.orig_articles) return '';
    // Передаём ОРИГИНАЛЬНЫЕ OEM-артикулы (не Part IDs), иначе повторный
    // search_parts не найдёт позиции — ID не равны OEM.
    const articlesJson = esc(JSON.stringify(d.orig_articles || d.product_ids.map(String)));
    const qtyJson = d.product_quantities ? esc(JSON.stringify(d.product_quantities)) : '';
    // Страны прибытия (ЕАЭС/СНГ) → города. Сначала выбираем СТРАНУ, затем город.
    // Страна для расчёта пошлины/НДС берётся из префикса кода (RUMOW → RU), а если
    // город вписан вручную без кода — из явно выбранной страны (select).
    const arrivalByCountry = {
      RU: {flag:'🇷🇺', name:'Россия', cities:['RUMOW — Москва','RULED — Санкт-Петербург','RUARH — Архангельск','RUMMK — Мурманск','RUKGD — Калининград','RUNVS — Новороссийск','RUROV — Ростов-на-Дону','RUKRR — Краснодар','RUAER — Сочи','RUKZN — Казань','RUUFA — Уфа','RUKUF — Самара','RUGOJ — Нижний Новгород','RUEKB — Екатеринбург','RUPEE — Пермь','RUOVB — Новосибирск','RUKJA — Красноярск','RUIKT — Иркутск','RUVVO — Владивосток','RUKHV — Хабаровск']},
      KZ: {flag:'🇰🇿', name:'Казахстан', cities:['KZALA — Алматы','KZAST — Астана','KZKGF — Караганда','KZCIT — Шымкент']},
      BY: {flag:'🇧🇾', name:'Беларусь', cities:['BYMSQ — Минск','BYGME — Гомель','BYBQT — Брест']},
      AM: {flag:'🇦🇲', name:'Армения', cities:['AMEVN — Ереван','AMLWN — Гюмри']},
      KG: {flag:'🇰🇬', name:'Киргизия', cities:['KGFRU — Бишкек','KGOSS — Ош']},
      UZ: {flag:'🇺🇿', name:'Узбекистан', cities:['UZTAS — Ташкент','UZSKD — Самарканд']},
    };
    // Прочие страны (помимо ЕАЭС): название → ISO-2. Города — свободный ввод.
    const otherCountries = [
      ['AZ','Азербайджан'],['GE','Грузия'],['TJ','Таджикистан'],['TM','Туркменистан'],['MD','Молдова'],['UA','Украина'],
      ['CN','Китай'],['TR','Турция'],['AE','ОАЭ'],['IR','Иран'],['IN','Индия'],['PK','Пакистан'],['MN','Монголия'],
      ['KR','Южная Корея'],['JP','Япония'],['VN','Вьетнам'],['TH','Таиланд'],['ID','Индонезия'],['MY','Малайзия'],['SG','Сингапур'],
      ['DE','Германия'],['FR','Франция'],['IT','Италия'],['ES','Испания'],['GB','Великобритания'],['PL','Польша'],['NL','Нидерланды'],
      ['CZ','Чехия'],['FI','Финляндия'],['SE','Швеция'],['RS','Сербия'],['BG','Болгария'],['RO','Румыния'],['HU','Венгрия'],['GR','Греция'],['AT','Австрия'],
      ['US','США'],['CA','Канада'],['BR','Бразилия'],['MX','Мексика'],
      ['EG','Египет'],['ZA','ЮАР'],['SA','Саудовская Аравия'],['IL','Израиль'],['QA','Катар'],['KW','Кувейт'],['OM','Оман'],['BH','Бахрейн'],['IQ','Ирак'],['JO','Иордания'],
      ['AU','Австралия'],
    ];
    const order = ['RU','KZ','BY','AM','KG','UZ'];
    // Полный список стран для автоподсказки + маппинг название→ISO-2.
    const nameToCC = {};
    const countryEntries = []; // [cc, flag, name]
    order.forEach(cc => { const c = arrivalByCountry[cc]; countryEntries.push([cc, c.flag, c.name]); nameToCC[c.name.toLowerCase()] = cc; });
    otherCountries.forEach(([cc, name]) => { countryEntries.push([cc, '', name]); nameToCC[name.toLowerCase()] = cc; });
    const countryOpts = countryEntries.map(([cc, flag, name]) =>
      `<option value="${esc(name)}">${flag ? flag + ' ' : ''}${esc(name)}</option>`).join('');
    const curPortVal = d.arrival_port || '';
    const curCC = (curPortVal.match(/^([A-Z]{2})/) || [])[1] || 'RU';
    const curCountryName = (countryEntries.find(e => e[0] === curCC) || [null, '', 'Россия'])[2];
    const cityList = (arrivalByCountry[curCC] || {cities: []}).cities;
    const portOpts = cityList.map(p => `<option value="${esc(p)}"></option>`).join('');
    const curPort = curPortVal ? `value="${esc(curPortVal)}"` : '';
    const curAddr = d.delivery_address ? esc(d.delivery_address) : '';
    const uid = (window.__dfUid = (window.__dfUid || 0) + 1);
    const dlId = 'df-port-list-' + uid;
    const ccId = 'df-country-list-' + uid;
    const mapJson = esc(JSON.stringify(arrivalByCountry));
    const nameMapJson = esc(JSON.stringify(nameToCC));
    return `<div class="df-block" data-articles='${articlesJson}' ${qtyJson ? `data-qty='${qtyJson}'` : ''} data-arrival='${mapJson}' data-countries='${nameMapJson}'>
      <div class="df-title">📍 Куда доставить?</div>
      <div class="df-hint">Укажите <b>страну и город</b> → в таблице ниже появятся цены <b>CIP и DDP</b>. Полный адрес до двери нужен только для <b>DDP</b> — попросим при выборе.</div>
      <div class="df-row">
        <label class="df-lbl">Страна <span class="df-opt">(для CIP/DDP)</span></label>
        <input class="df-input df-country" type="text" list="${ccId}" value="${esc(curCountryName)}" placeholder="Начните вводить страну…" autocomplete="off" oninput="window.dfCountryChange && window.dfCountryChange(this, false)" onchange="window.dfCountryChange && window.dfCountryChange(this, true)" />
        <datalist id="${ccId}">${countryOpts}</datalist>
      </div>
      <div class="df-row">
        <label class="df-lbl">Город / место прибытия <span class="df-opt">(для CIP/DDP)</span></label>
        <input class="df-input df-port" type="text" list="${dlId}" placeholder="Напр.: Москва" ${curPort} onkeydown="if(event.key==='Enter'){event.preventDefault(); var b=this.closest('.df-block').querySelector('.df-submit'); b&&b.click();}" />
        <datalist id="${dlId}">${portOpts}</datalist>
      </div>
      <div class="df-row">
        <label class="df-lbl">Адрес доставки <span class="df-opt">(улица, дом · для DDP)</span></label>
        <textarea class="df-input df-addr" rows="2" placeholder="Напр.: ул. Профсоюзная 84, корп. 5, офис 12">${curAddr}</textarea>
      </div>
      <div class="df-hint" style="margin:2px 0 8px;">Заполнили поля? Нажмите кнопку — цены <b>CIP/DDP</b> сразу появятся в таблице ниже ↓</div>
      <button class="df-submit act-btn" type="button" onclick="window.calcShipping && window.calcShipping(this)">
        🧮 Рассчитать цены CIP / DDP →
      </button>
    </div>`;
  }

  function renderShippingMatrix(d) {
    if (!d.shipping_matrix || !d.product_ids) return '';
    // По шагам: пока не ввели адрес (страну+город) и не посчитали — показываем
    // ТОЛЬКО форму «Куда доставить?», без заголовка «Выберите базис» и без
    // матрицы FOB/CIP/DDP. Базисы появляются после расчёта (cip_available).
    if (!d.cip_available) {
      return `<div class="sm-block">${renderDeliveryForm(d)}</div>`;
    }
    const dest = d.dest_country || '';
    const descs = d.incoterm_descs || {};
    const incoterms = ['FOB', 'CIP', 'DDP'];
    const incShort = {
      FOB: 'самовывоз из порта',
      CIP: 'до вашего порта',
      DDP: 'до двери, all-in',
    };
    const headerCells = incoterms.map(inc =>
      `<th title="${esc(descs[inc] || '')}">
        <div class="sm-inc-name">${inc}</div>
        <div class="sm-inc-sub">${esc(incShort[inc] || '')}</div>
      </th>`
    ).join('');
    const rows = (d.shipping_matrix || []).map(m => {
      const cells = m.options.map(opt => {
        if (opt.available === false) {
          // Различаем ДВЕ причины недоступности:
          //  1) место/адрес ещё не указаны → подсказываем заполнить форму (клик ↓);
          //  2) место указано (cip/ddp_available), но по направлению нет тарифа на
          //     фрахт → честно пишем «нет тарифа», иначе после расчёта кажется,
          //     что ничего не изменилось (всё ещё «укажите место прибытия»).
          let hint, needForm = false;
          if (opt.incoterm === 'CIP') {
            if (!d.cip_available) { hint = 'укажите место прибытия'; needForm = true; }
            else hint = 'нет тарифа на фрахт';
          } else if (opt.incoterm === 'DDP') {
            if (!d.cip_available) { hint = 'укажите место прибытия'; needForm = true; }
            else hint = 'нет тарифа на фрахт';
          } else {
            hint = 'недоступно';
          }
          const clickAttr = needForm
            ? ' onclick="window.dfFocus && window.dfFocus(this)" style="cursor:pointer;"'
            : '';
          return `<td class="sm-cell sm-cell-disabled" title="${esc(hint)}"${clickAttr}>
            <div class="sm-landed sm-na-mark">—</div>
            <div class="sm-ship">${esc(hint)}${needForm ? ' ↑' : ''}</div>
          </td>`;
        }
        const params = {
          product_ids: d.product_ids,
          mode: m.mode, incoterm: opt.incoterm,
        };
        if (dest) params.dest_country = dest;
        if (d.delivery_address) params.delivery_address = d.delivery_address;
        if (d.arrival_port) params.arrival_port = d.arrival_port;
        if (d.product_quantities) params.product_quantities = d.product_quantities;
        const shipBadge = opt.incoterm === 'FOB'
          ? '<div class="sm-ship">самовывоз · $0</div>'
          : `<div class="sm-ship">+${fmtMoney(opt.ship, 'USD')} ship</div>`;
        // DDP (до двери) при заказе требует полный адрес — клик идёт через
        // dfPickDDP: если адрес не заполнен, просим его, иначе оформляем.
        // FOB/CIP адрес до двери не нужен — обычный quick_order.
        const cellAttrs = opt.incoterm === 'DDP'
          ? `data-params='${esc(JSON.stringify(params))}' data-label="Купить ${esc(m.mode_label)} DDP" onclick="window.dfPickDDP && window.dfPickDDP(this)" style="cursor:pointer;"`
          : `data-action="quick_order" data-params='${esc(JSON.stringify(params))}' data-label="Купить ${esc(m.mode_label)} ${opt.incoterm}"`;
        return `<td class="sm-cell" ${cellAttrs}>
          <div class="sm-landed">${fmtMoney(opt.landed, d.currency || 'USD')}</div>
          ${shipBadge}
        </td>`;
      }).join('');
      return `<tr class="sm-row">
        <td class="sm-mode">${esc(m.mode_label)}<div class="sm-days">${m.days ? `~${m.days}д` : ''}</div></td>
        ${cells}
      </tr>`;
    }).join('');
    const originsBadge = (d.origins || []).map(o =>
      `<span class="sm-origin">${o.flag} ${esc(o.name)}${o.count > 1 ? ` × ${o.count}` : ''}</span>`
    ).join('');
    const arrow = dest ? ` → ${esc(dest)}` : ' → ?';
    const ob = d.origin_breakdown || [];
    const expandable = ob.length >= 1;
    const routeLine = originsBadge
      ? `<div class="sm-route${expandable ? ' sm-route-expandable' : ''}"${expandable ? ` onclick="this.classList.toggle('sm-route-open'); const t=this.nextElementSibling; if(t&&t.classList.contains('ob-block')) t.style.display = t.style.display==='block'?'none':'block';"` : ''}>${expandable ? '<span class="sm-chev">▸</span> ' : ''}Откуда: ${originsBadge}${arrow}${d.filter_origin ? ` <span class="sm-filter-badge">фильтр: только ${esc(d.filter_origin)}</span>` : ''}</div>`
      : '';
    // Разворачиваемая таблица по странам — скрыта до клика на "Откуда".
    // Каждая страна — отдельный <details>, чтобы можно было раскрыть
    // конкретные позиции и оценить риски (что именно едет из Китая и т.п.).
    let originTable = '';
    if (expandable) {
      const blocks = ob.map(o => {
        const itemsList = (o.items || []).map(it => `
          <li><span class="ob-it-oem">${esc(it.oem)}</span>${it.title ? ` · <span class="ob-it-title">${esc(it.title)}</span>` : ''}<span class="ob-it-meta">${it.weight_kg ? ` · ${it.weight_kg.toFixed(1)}кг` : ''} · ${fmtMoney(it.cargo, 'USD')}</span></li>`).join('');
        // Кликабельные пилюли портов: клик ВИЗУАЛЬНО выделяет FOB-ячейку
        // соответствующего режима в матрице. Без API-вызовов, без перегенерации
        // карточки. Чистый client-side selection state.
        const portLabel = (p) => {
          if (typeof p === 'string') return `<div class="ob-port">${esc(p)}</div>`;
          const META = {
            sea:  {type: 'Морской порт',  purpose: 'контейнер',     days: '25–40 дней', mode: 'sea'},
            air:  {type: 'Авиа терминал', purpose: 'срочно',         days: '5–10 дней',  mode: 'air'},
            both: {type: 'Морской + авиа', purpose: 'оба варианта',   days: '5–40 дней',  mode: 'sea'},
          };
          const meta = META[p.type] || META.sea;
          return `<button class="ob-port ob-port-${p.type || 'sea'}" type="button"
              data-port-select="${esc(meta.mode)}"
              data-port-code="${esc(p.code)}"
              title="Выделить FOB для ${esc(meta.type)} ${esc(p.code)}">
            <span class="ob-port-seg ob-port-type">${esc(meta.type)}</span>
            <span class="ob-port-sep">·</span>
            <span class="ob-port-seg ob-port-code">${esc(p.code)}</span>
            <span class="ob-port-sep">·</span>
            <span class="ob-port-seg ob-port-purpose">${esc(meta.purpose)}</span>
            <span class="ob-port-sep">·</span>
            <span class="ob-port-seg ob-port-days">${esc(meta.days)}</span>
          </button>`;
        };
        const portsRendered = (o.ports || []).map(portLabel).join('');
        return `<details class="ob-country">
          <summary>
            <span class="ob-c-flag">${o.flag}</span>
            <span class="ob-c-name">${esc(o.name)}</span>
            <span class="ob-c-ports">${portsRendered}</span>
            <span class="ob-c-stat"><b>${o.count}</b> поз.</span>
            <span class="ob-c-stat">${o.weight_kg.toFixed(1)} кг</span>
            <span class="ob-c-stat">${fmtMoney(o.cargo, 'USD')}</span>
            ${o.freight_sea > 0 ? `<span class="ob-c-stat">$${Math.round(o.freight_sea).toLocaleString()} sea${o.days_sea ? ` · ~${o.days_sea}д` : ''}</span>` : ''}
            <span class="ob-c-expand">Показать позиции ▾</span>
          </summary>
          ${itemsList ? `<ul class="ob-items">${itemsList}</ul>` : ''}
        </details>`;
      }).join('');
      originTable = `<div class="ob-block" style="display:none;">
        ${blocks}
        ${ob.length >= 2 && !d.filter_origin
          ? '<div class="ob-hint">💡 Несколько origin = несколько коносаментов. Клик по стране — раскрыть позиции для оценки рисков.</div>'
          : '<div class="ob-hint">💡 Клик по стране — раскрыть позиции для оценки рисков.</div>'}
      </div>`;
    }
    const title = dest
      ? `🚚 Выберите способ и базис${arrow}`
      : '🚚 Выберите базис (FOB — самовывоз без доплат)';
    const form = (!d.cip_available || !d.delivery_address) ? renderDeliveryForm(d) : '';
    return `<div class="sm-block">
      <div class="sm-title">${title}</div>
      ${routeLine}
      ${originTable}
      ${form}
      <table class="sm-table">
        <thead><tr><th>Способ / срок</th>${headerCells}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="sm-legend">
        <div class="sm-legend-row"><b>FOB</b> — Free On Board: ${esc(descs.FOB || '')}</div>
        <div class="sm-legend-row"><b>CIP</b> — Carriage & Insurance Paid: ${esc(descs.CIP || '')}</div>
        <div class="sm-legend-row"><b>DDP</b> — Delivered Duty Paid: ${esc(descs.DDP || '')}</div>
      </div>
      <div class="sm-hint">Клик по ячейке = выбрать базис и оформить. Для <b>CIP/DDP</b> сначала укажите страну и город выше ↑ (для DDP попросим адрес до двери).</div>
    </div>`;
  }

  function renderUnknownCard(type, data) {
    const rows = Object.entries(data || {})
      .filter(([k, v]) => v != null && typeof v !== 'object')
      .map(([k, v]) => `<div style="display:flex;gap:8px;padding:3px 0;font-size:12px;"><span style="color:rgba(0,0,0,0.55);min-width:90px;">${esc(k)}:</span><span>${esc(String(v))}</span></div>`)
      .join('');
    return `<div class="card">
      <div class="card-title" style="margin-bottom:8px;">${esc(type)} <span style="font-weight:400;color:rgba(0,0,0,0.45);font-size:11px;">(обновите страницу — Cmd+Shift+R)</span></div>
      ${rows}
    </div>`;
  }

  function renderActions(actions) {
    if (!actions || !actions.length) return '';
    return '<div class="actions">' + actions.map(a => {
      // style: primary (главный зелёный/чёрный CTA), warn (янтарный SEMI),
      // soft (мягкий outline для MANUAL), default (обычная чёрная кнопка)
      const styleCls = a.style ? ` act-btn-${esc(a.style)}` : '';
      return `<button class="act-btn${styleCls}" data-action="${esc(a.action)}" data-params='${esc(JSON.stringify(a.params || {}))}' data-label="${esc(a.label)}">${esc(a.label)}</button>`;
    }).join('') + '</div>';
  }

  // Brand mark SVG (8-facet asterisk from official Логобук)
  const STAR_SVG_WHITE = '<svg viewBox="0 0 74.1 74.1" fill="#fff"><polygon points="5.38 46.64 2.24 54.63 17.29 69.68 17.3 69.69 21.44 66.22 24.75 42.14 5.38 46.64"/><polygon points="21.44 66.22 24.87 74.1 46.16 74.1 46.64 68.72 31.95 49.35 21.44 66.22"/><polygon points="46.64 68.72 54.63 71.86 69.69 56.8 66.22 52.66 42.14 49.35 46.64 68.72"/><polygon points="68.71 27.45 71.86 19.47 56.8 4.41 52.65 7.87 49.35 31.95 68.71 27.45"/><polygon points="74.1 27.94 68.71 27.45 49.35 42.14 66.22 52.66 74.1 49.23 74.1 27.94"/><polygon points="52.65 7.87 49.23 0 27.93 0 27.45 5.38 42.14 24.75 52.65 7.87"/><polygon points="27.45 5.38 19.47 2.24 4.41 17.3 7.87 21.44 31.95 24.75 27.45 5.38"/><polygon points="7.87 21.44 0 24.87 0 46.16 5.38 46.64 24.75 31.95 7.87 21.44"/></svg>';
  const STAR_SVG_BLACK = STAR_SVG_WHITE.replace(/fill="#fff"/, 'fill="#1a1a1a"');
  const STAR_SVG = STAR_SVG_WHITE;  // default

  function avatar(role) {
    if (role === 'user') {
      const initial = ((state.config && state.config.user_name || 'U')[0] || 'U').toUpperCase();
      return `<div class="msg-avatar msg-avatar-user">${initial}</div>`;
    }
    if (role === 'action') return '<div class="msg-avatar msg-avatar-act">▸</div>';
    return `<div class="msg-avatar msg-avatar-bot">${STAR_SVG_BLACK}</div>`;
  }

  function authorLabel(role) {
    if (role === 'user') return state.config ? state.config.user_name : tr('common.you');
    if (role === 'action') return tr('common.action_done');
    return 'Consolidator';
  }

  // ══════════════════════════════════════════════════════════
  // Working indicator
  // Лейблы — i18n-ключи; messages() резолвит их в массивы строк под
  // текущим языком на каждом запросе (язык мог поменяться).
  // ══════════════════════════════════════════════════════════
  function workingMessages(category) {
    const cat = WORKING_KEYS[category] ? category : 'default';
    return WORKING_KEYS[cat].map(k => tr(k));
  }
  const WORKING_KEYS = {
    search:    ['working.search.0',    'working.search.1',    'working.search.2',    'working.search.3'],
    rfq:       ['working.rfq.0',       'working.rfq.1',       'working.rfq.2'],
    orders:    ['working.orders.0',    'working.orders.1',    'working.orders.2'],
    shipment:  ['working.shipment.0',  'working.shipment.1',  'working.shipment.2'],
    budget:    ['working.budget.0',    'working.budget.1',    'working.budget.2'],
    analytics: ['working.analytics.0', 'working.analytics.1', 'working.analytics.2'],
    claim:     ['working.claim.0',     'working.claim.1'],
    sla:       ['working.sla.0',       'working.sla.1'],
    suppliers: ['working.suppliers.0', 'working.suppliers.1'],
    default:   ['working.default.0',   'working.default.1',   'working.default.2', 'working.default.3'],
  };

  function pickIntent(text) {
    const t = (text || '').toLowerCase();
    if (/(search|find_|искать|найти|подобрать|катал|запчаст|товар|оем|oem|brand|hydraulic|cylinder|filter)/i.test(t)) return 'search';
    if (/(rfq|котировк|запрос)/.test(t)) return 'rfq';
    if (/(order|заказ)/i.test(t)) return 'orders';
    if (/(shipment|track|трекинг|отгрузк|доставк)/.test(t)) return 'shipment';
    if (/(budget|бюджет|расход|оплат)/.test(t)) return 'budget';
    if (/(analytic|аналитик|отчёт|метрик)/.test(t)) return 'analytics';
    if (/(claim|рекламац|жалоб)/.test(t)) return 'claim';
    if (/(sla|просрочк)/.test(t)) return 'sla';
    if (/(supplier|поставщик|seller)/i.test(t)) return 'suppliers';
    return 'default';
  }

  let workingTimer = null;

  function addTyping(intentHint, minimal=false) {
    showConv();
    const intent = intentHint || 'default';
    const messages = workingMessages(intent);
    const wrap = document.createElement('div');
    wrap.className = 'msg';
    wrap.id = 'typingMsg';
    // Минимальный режим (для FAST_ACTIONS): только три точки без вертящейся
    // звёздочки и без подсказки «ищу позиции в каталоге…». Раньше fast-actions
    // вообще не показывали индикатор, и при дольше 200-300мс юзер думал
    // «не работает».
    if (minimal) {
      wrap.innerHTML = `${avatar('assistant')}
        <div class="msg-body">
          <div class="working working-min">
            <span class="working-dots"><span></span><span></span><span></span></span>
          </div>
        </div>`;
    } else {
      wrap.innerHTML = `${avatar('assistant')}
        <div class="msg-body">
          <div class="working">
            <div class="working-logo">${STAR_SVG_BLACK}</div>
            <span class="working-text" id="workingText">${esc(messages[0])}</span>
          </div>
        </div>`;
    }
    $('streamInner').appendChild(wrap);
    scrollBottom();
    if (minimal) return;  // dots анимируются через CSS, без интервала

    let idx = 0;
    if (workingTimer) clearInterval(workingTimer);
    workingTimer = setInterval(() => {
      idx = (idx + 1) % messages.length;
      const el = $('workingText');
      if (!el) { clearInterval(workingTimer); workingTimer = null; return; }
      el.style.opacity = 0;
      setTimeout(() => { el.textContent = messages[idx]; el.style.opacity = 1; }, 200);
    }, 1800);
  }

  function removeTyping() {
    if (workingTimer) { clearInterval(workingTimer); workingTimer = null; }
    const t = $('typingMsg');
    if (t) t.remove();
  }

  // ══════════════════════════════════════════════════════════
  // Messages
  // ══════════════════════════════════════════════════════════
  function renderContextRefs(refs) {
    if (!refs || !refs.length) return '';
    const fileIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
    const items = refs.slice(0, 8).map(r => {
      const label = r.title || r.id || '—';
      const typeLabel = (r.type || '').toUpperCase();
      return `<span class="ctx-ref">${fileIcon}${typeLabel ? `<span class="ctx-ref-label">${esc(typeLabel)}</span>` : ''}${esc(label)}</span>`;
    }).join('');
    return `<div class="ctx-refs">${items}</div>`;
  }

  function addMessage(role, content, cards=[], actions=[], contextRefs=[], messageId=null, suggestions=[], contextualActions=[], fromServer=true) {
    showConv();
    const wrap = document.createElement('div');
    wrap.className = 'msg msg-' + role;
    if (messageId) wrap.dataset.messageId = messageId;
    const isAi = role === 'assistant';
    // ВАЖНО: cards ВЫШЕ текста для AI-сообщений. Главный ответ (таблица
    // цен / результат / форма) должен быть сразу виден, а текст-комментарий
    // («Проверил 13 артикулов…») — как подпись снизу. Для user/action
    // сообщений порядок не важен (карточек обычно нет).
    wrap.innerHTML = `
      ${avatar(role)}
      <div class="msg-body">
        <div class="msg-author">${esc(authorLabel(role))}</div>
        <div class="msg-cards"></div>
        <div class="msg-content${role === 'action' ? ' msg-action-tag' : ''}${isAi ? ' msg-content-ai' : ''}"></div>
        <div class="msg-refs"></div>
        <div class="msg-actions"></div>
        <div class="msg-ctx-actions"></div>
        <div class="msg-suggestions"></div>
      </div>
    `;
    const cEl = wrap.querySelector('.msg-content');
    if (isAi && (content || '').trim()) {
      cEl.innerHTML = linkifyEntities(content || '');
      // FIX: 💬-иконка и chat-bubble стиль текста — ТОЛЬКО когда нет cards.
      // Иначе текст под карточкой выглядит как «второе сообщение» (визуальный
      // дубль): user думает что бот ответил дважды.
      const hasCards = Array.isArray(cards) && cards.length > 0;
      if (!hasCards) cEl.classList.add('msg-has-text');
      else cEl.classList.add('msg-caption');  // компактный текст-caption под cards
    } else {
      cEl.textContent = content || '';
    }
    wrap.querySelector('.msg-refs').innerHTML = renderContextRefs(contextRefs);
    wrap.querySelector('.msg-cards').innerHTML = renderCards(cards, {fromServer: fromServer});
    wrap.querySelector('.msg-actions').innerHTML = renderActions(actions);
    wrap.querySelector('.msg-ctx-actions').innerHTML = renderContextualActions(contextualActions);
    wrap.querySelector('.msg-suggestions').innerHTML = renderSuggestions(suggestions);
    $('streamInner').appendChild(wrap);
    // Скролл к НАЧАЛУ нового AI-сообщения (а не к концу). Карточки с таблицами
    // огромные — если скроллить к низу, юзер видит только последние строки,
    // а главное (KPI, таблица цен, заголовок) уходит за верх экрана.
    const isAiLong = (role === 'assistant') && (cards && cards.length > 0);
    if (isAiLong) {
      // Многоступенчатый scroll: layout у таблиц/изображений могут произойти
      // позже (после fonts/cards/images), поэтому скроллим в несколько тиков
      // чтобы вернуть верх сообщения на верх viewport.
      const scrollToTopOfWrap = () => {
        const s = $('stream');
        if (!s) return;
        // Вычисляем offset wrap-сообщения внутри streamInner.
        // wrap.offsetTop относительно offsetParent (streamInner). Минус малый
        // отступ чтобы не упирался в самый край.
        // Большой отступ сверху (60px) — чтобы аватар/имя «Consolidator»
        // и немного «воздуха» сверху были видны, а карточка не «впивалась»
        // в верхний край viewport.
        const off = wrap.offsetTop - 60;
        s.scrollTo({top: Math.max(0, off), behavior: 'smooth'});
      };
      // 3 попытки: сразу, после rAF, и через 300мс (post-layout). Самая
      // поздняя «выиграет» если что-то ещё двигало контент.
      scrollToTopOfWrap();
      requestAnimationFrame(scrollToTopOfWrap);
      setTimeout(scrollToTopOfWrap, 300);
    } else {
      scrollBottom();
      requestAnimationFrame(() => {
        try {
          wrap.scrollIntoView({ block: "end", behavior: "smooth" });
        } catch (e) { /* старые браузеры — fallback на scrollBottom */ }
      });
    }
    return wrap;
  }

  function renderContextualActions(items) {
    if (!items || !items.length) return '';
    const btns = items.map(a =>
      `<button class="act-btn ctx-btn" data-action="${esc(a.action)}" data-params='${esc(JSON.stringify(a.params || {}))}' data-label="${esc(a.label)}">${esc(a.label)}</button>`
    ).join('');
    return `<div class="ctx-row">
      <span class="ctx-label">💡 Также можете:</span>
      ${btns}
    </div>`;
  }

  // Превращает упоминания сущностей в кликабельные ссылки на карточки.
  // Поддерживаемые форматы:
  //   «заказ #123», «#ORD-123», «order #123»  → track_order(123)
  //   «RFQ #45», «RFQ-45»                       → rfq_detail / get_rfq_status
  function linkifyEntities(text) {
    let html = esc(text);
    // Заказ #N — самое частое
    html = html.replace(/(?<![\w-])(заказ|order|зак\.)\s*#?\s*(\d{1,7})\b/gi,
      (full, kw, id) => `<span class="entity-link" data-action="track_order" data-params='{"order_id":${id}}'>${full}</span>`);
    // RFQ #N
    html = html.replace(/(?<![\w-])RFQ\s*[#-]?\s*(\d{1,7})\b/gi,
      (full, id) => `<span class="entity-link" data-action="rfq_detail" data-params='{"rfq_id":${id}}'>${full}</span>`);
    // Просто #N — последний фолбек, если идёт сразу после слов «заказ/order» уже обработано
    return html;
  }

  // Раскрытие списка поставщиков inline — detail-row внутри spec-таблицы.
  document.addEventListener('click', (e) => {
    const toggleRow = e.target.closest('tr.spec-row-toggle');
    if (!toggleRow) return;
    if (e.target.closest('.sp-compare, .act-btn, a[href], button')) return;
    const idx = toggleRow.dataset.rowIdx;
    const tbody = toggleRow.parentElement;
    const detail = tbody.querySelector(`tr.spec-detail-row[data-detail-for="${idx}"]`);
    if (!detail) return;
    const open = detail.style.display === 'table-row';
    detail.style.display = open ? 'none' : 'table-row';
    toggleRow.classList.toggle('spec-row-open', !open);
    if (!open) {
      const wrap = toggleRow.closest('.spec-tbl-wrap');
      const block = detail.querySelector('.as-block');
      if (wrap && block) block.style.width = wrap.clientWidth + 'px';
    }
    e.preventDefault();
    e.stopPropagation();
  });

  // Делегируем клик по clickable card → 1) data-href навигация, 2) data-action quickAction
  document.addEventListener('click', (e) => {
    // 1. Чистая навигация (RFQ карточки и т.п.)
    const navCard = e.target.closest('.card-clickable[data-href]');
    if (navCard && navCard.dataset.href) {
      window.location.href = navCard.dataset.href;
      return;
    }
    // 2. Action-карточки (order → track_order, KPI-ячейки, sidebar pills и т.п.)
    const target = e.target.closest('.entity-link, .card-clickable[data-action], .kpi-cell-clickable[data-action], .side-history-btn[data-action]');
    if (!target) return;
    const action = target.dataset.action;
    if (!action) return;
    let params = {};
    try { params = JSON.parse(target.dataset.params || '{}'); } catch(_){}
    params._label = (target.querySelector('.card-title')?.textContent || target.textContent || '').trim().slice(0, 80);
    if (typeof quickAction === 'function') quickAction(action, params);
  });
  // Поддержка клавиатуры (Enter/Space) для clickable cards
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const target = e.target.closest && e.target.closest('.card-clickable[data-action], .card-clickable[data-href]');
    if (!target) return;
    e.preventDefault();
    target.click();
  });

  function renderSuggestions(suggestions) {
    if (!suggestions || !suggestions.length) return '';
    // Поддерживаем два формата:
    //   1. String  → text-chip (отправляется как сообщение → /chat/ → Claude).
    //                Для случаев, где контекст неоднозначен (свободный follow-up).
    //   2. {label, action, params} → action-chip (прямой /action/, без LLM).
    //                Для случаев под карточкой сущности — контекст однозначен,
    //                ответ детерминирован.
    // Линия подсказок — без эмодзи: срезаем ведущие emoji/символы у каждого чипа.
    const stripEmoji = (s) => String(s == null ? '' : s)
      .replace(/^(?:[\p{Extended_Pictographic}←-⇿⬀-⯿️‍]\s*)+/u, '').trim();
    const chips = suggestions.map(s => {
      if (s && typeof s === 'object' && s.action) {
        const paramsJson = JSON.stringify(s.params || {});
        return `<button class="sg-chip sg-chip-action" type="button"
          data-action="${esc(s.action)}"
          data-params='${esc(paramsJson)}'>${esc(stripEmoji(s.label || s.action))}</button>`;
      }
      return `<button class="sg-chip" type="button" data-text="${esc(s)}">${esc(stripEmoji(s))}</button>`;
    }).join('');
    return `<div class="sg-row">
      <span class="sg-label">Также можете:</span>
      ${chips}
    </div>`;
  }
  // ── Quote form (qf-card): подвал-форма (срок/действует/коммент + submit) ──
  function _qfFooter(d) {
    const q = d.qf || {};
    const n = (d.items || []).length;
    const totRound = '$' + Number(d.total || 0).toLocaleString('en-US', {maximumFractionDigits: 0});
    // Честная полоса скорости: возраст RFQ + реальная медиана/перцентиль продавца.
    // Без фальшивых наград — посыл «ответь, пока запрос активен».
    const sp = q.speed || null;
    const speedBits = [];
    if (q.rfq_age) speedBits.push('⏱ ' + esc(q.rfq_age));
    if (sp && sp.median_min != null) {
      speedBits.push('ваша скорость ответа ' + esc(sp.median_label)
        + (sp.faster_than_pct != null ? ' · быстрее ' + esc(String(sp.faster_than_pct)) + '% продавцов' : ''));
    }
    const speedStrip = (speedBits.length)
      ? `<div class="qf-speed"><div class="qf-speed-line">${speedBits.join(' · ')}</div><div class="qf-speed-note">Ответ в течение 1 ч поднимает ваш рейтинг, а рейтинг влияет на ранжирование предложений — и на продажи. Ответ позже 24 ч снижает рейтинг.</div></div>`
      : '';
    return `${speedStrip}<div class="qf-aux">
        <div class="qf-aux-row"><label>Срок поставки (дней)</label>
          <input class="qf-aux-input" name="delivery_days" type="number" min="1" value="${esc(String(q.delivery_days || 14))}" /></div>
        <div class="qf-aux-row"><label>Котировка действует (дней)</label>
          <input class="qf-aux-input" name="valid_days" type="number" min="1" value="${esc(String(q.valid_days || 7))}" /></div>
      </div>
      <div class="qf-actions">
        <button class="qf-cancel" type="button" data-action="seller_inbox" data-params="{}">Отмена</button>
        <button class="qf-submit" type="button" data-rfq-id="${esc(String(q.rfq_id || ''))}">
          📨 Отправить котировку · <span data-submit-count>${n}</span> поз · <span data-submit-total>${totRound}</span>
        </button>
      </div>`;
  }
  // ── Quote form (qf-card): live-total + submit ────────────────
  function _qfRecalc(card) {
    let total = 0;
    let cnt = 0;
    // Считаем по полям ввода напрямую (строки рендерятся как у заказа, без .qf-row).
    card.querySelectorAll('.qf-price-input').forEach(inp => {
      const qty = Number(inp.dataset.qty || 0);
      const price = Number(inp.value || 0);
      const lineTotal = qty * price;
      cnt += 1;
      total += lineTotal;
      const row = inp.closest('tr');
      const tdTotal = row && row.querySelector('[data-line-total]');
      if (tdTotal) tdTotal.textContent = lineTotal.toFixed(2);
    });
    // Обновляем ВСЕ [data-total] (живая «Сумма» в мете + подвал).
    card.querySelectorAll('[data-total]').forEach(el => {
      el.textContent = '$' + total.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    });
    const sCount = card.querySelector('[data-submit-count]');
    if (sCount) sCount.textContent = cnt;
    const sTotal = card.querySelector('[data-submit-total]');
    if (sTotal) sTotal.textContent = '$' + total.toLocaleString('en-US', {maximumFractionDigits: 0});
  }
  document.addEventListener('input', (e) => {
    const inp = e.target && e.target.closest && e.target.closest('.qf-price-input');
    if (!inp) return;
    // Вернул цену исключённой позиции → снимаем приглушение (можно дать цену на остальные).
    const _row = inp.closest('tr');
    if (_row && _row.classList.contains('qf-row-excluded') && inp.value !== '') _row.classList.remove('qf-row-excluded');
    const card = inp.closest('.qf-card');
    if (card) _qfRecalc(card);
  });
  // «📦 Только мои позиции»: очистить цену у позиций НЕ из каталога продавца → они
  // не уйдут в котировку (submit пропускает пустые). Свои остаются с ценой (она уже
  // подставлена из каталога при рендере). Цену исключённым можно вернуть вручную.
  document.addEventListener('click', (e) => {
    const btn = e.target && e.target.closest && e.target.closest('.qf-keep-mine');
    if (!btn) return;
    e.preventDefault();
    const card = btn.closest('.qf-card');
    if (!card) return;
    // Тогл: первое нажатие — очистить не-мои позиции (запомнив цены), повторное —
    // вернуть всё обратно.
    if (btn.dataset.active !== '1') {
      let cleared = 0, kept = 0;
      card.querySelectorAll('.qf-price-input').forEach(inp => {
        if (inp.dataset.inCatalog === '0') {
          inp.dataset.origPrice = inp.value;   // запомним для возврата
          inp.value = '';
          const row = inp.closest('tr');
          if (row) row.classList.add('qf-row-excluded');
          cleared += 1;
        } else { kept += 1; }
      });
      btn.dataset.active = '1';
      btn.classList.add('qf-keep-mine-on');
      _qfRecalc(card);
      if (window.toast) window.toast('📦 Оставлено ваших: ' + kept + ' · исключено: ' + cleared + ' (нажмите ещё раз — вернуть)', 2800);
    } else {
      let restored = 0;
      card.querySelectorAll('.qf-price-input').forEach(inp => {
        const row = inp.closest('tr');
        // Возвращаем только те, что всё ещё исключены (если юзер вписал цену вручную — не трогаем).
        if (inp.dataset.inCatalog === '0' && row && row.classList.contains('qf-row-excluded')) {
          if (inp.dataset.origPrice != null) inp.value = inp.dataset.origPrice;
          row.classList.remove('qf-row-excluded');
          restored += 1;
        }
      });
      btn.dataset.active = '0';
      btn.classList.remove('qf-keep-mine-on');
      _qfRecalc(card);
      if (window.toast) window.toast('↩ Возвращено позиций: ' + restored, 2000);
    }
  });
  document.addEventListener('click', (e) => {
    const btn = e.target && e.target.closest && e.target.closest('.qf-submit');
    if (!btn) return;
    e.preventDefault();
    const card = btn.closest('.qf-card');
    if (!card) return;
    if (card.dataset._submitting === '1') return;
    card.dataset._submitting = '1';
    btn.disabled = true;
    btn.style.opacity = '0.6';
    const params = {
      rfq_id:           card.dataset.rfqId,
      confirmed:        true,
      parent_quote_id:  card.dataset.parentQuoteId || '',
      direction:        card.dataset.direction || 'seller_to_buyer',
    };
    card.querySelectorAll('.qf-price-input').forEach(inp => {
      params[inp.name] = inp.value;
    });
    card.querySelectorAll('.qf-aux-input, .qf-aux-textarea').forEach(inp => {
      params[inp.name] = inp.value;
    });
    // Per-позиционные: срок (lead_<id>) + тип OEM/аналог (cond_<id>)
    card.querySelectorAll('.qf-item-input').forEach(inp => {
      if (inp.value !== '' && inp.value != null) params[inp.name] = inp.value;
    });
    if (typeof quickAction === 'function') quickAction('submit_quote', params);
  });

  // Применить аналог из каталога продавца к позиции (чип «🔄 аналог»).
  document.addEventListener('click', (e) => {
    const chip = e.target && e.target.closest && e.target.closest('.qf-analog-chip');
    if (!chip) return;
    e.preventDefault();
    const card = chip.closest('.qf-card');
    const row = chip.closest('tr');
    if (!row) return;
    const price = Number(chip.dataset.analogPrice || 0);
    const priceInp = row.querySelector('.qf-price-input');
    if (priceInp && price > 0) priceInp.value = price.toFixed(2);
    const condSel = row.querySelector('.qf-cond');
    if (condSel) condSel.value = 'analog';
    if (card) _qfRecalc(card);
    chip.classList.add('qf-analog-applied');
    chip.textContent = '✓ аналог применён: ' + (chip.dataset.analogArticle || '');
    if (window.toast) window.toast('🔄 Аналог подставлен · тип «Аналог»', 2400);
  });

  // Делегированный обработчик copy-кнопок (invoice card)
  document.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('[data-copy]');
    if (!btn) return;
    e.preventDefault();
    const txt = btn.dataset.copy || '';
    const flash = () => {
      btn.classList.add('cl-copied');  // иконка copy→галочка (CSS) либо просто подсветка
      const lbl = btn.dataset.copiedLabel;
      let old = null;
      if (lbl) { old = btn.innerHTML; btn.textContent = lbl; }
      setTimeout(() => {
        btn.classList.remove('cl-copied');
        if (old != null) btn.innerHTML = old;
      }, 1400);
    };
    const ok = () => { flash(); if (window.toast) window.toast('Скопировано', 1500); };
    if (navigator.clipboard) {
      navigator.clipboard.writeText(txt).then(ok).catch(() => {
        if (window.toast) window.toast('Не удалось скопировать', 2000);
      });
    } else {
      // Фолбэк для незащищённого контекста / старых браузеров
      try {
        const ta = document.createElement('textarea');
        ta.value = txt; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); ta.remove(); ok();
      } catch (_) { if (window.toast) window.toast('Не удалось скопировать', 2000); }
    }
  });

  // Делегированный обработчик «Поделиться» (native share на мобиле, иначе copy)
  document.addEventListener('click', (e) => {
    const sb = e.target.closest && e.target.closest('[data-share-url]');
    if (!sb) return;
    e.preventDefault();
    const url = sb.dataset.shareUrl || '';
    const text = sb.dataset.shareText || '';
    if (navigator.share) {
      navigator.share({ title: 'Consolidator Parts', text, url }).catch(() => {});
    } else if (navigator.clipboard) {
      navigator.clipboard.writeText(url).then(() => {
        if (window.toast) window.toast('Ссылка скопирована', 1500);
      });
    }
  });

  // Делегированный обработчик для sg-chip:
  //   - data-action → прямой quickAction (без /chat/, без Claude)
  //   - data-text   → heroQuick (через инпут → /chat/ → fast_path или Claude)
  document.addEventListener('click', (e) => {
    const chip = e.target.closest && e.target.closest('.sg-chip');
    if (!chip) return;
    e.preventDefault();
    if (chip.dataset.action) {
      let params = {};
      try { params = JSON.parse(chip.dataset.params || '{}'); } catch(_){}
      params._label = (chip.textContent || '').trim().slice(0, 80);
      if (typeof quickAction === 'function') quickAction(chip.dataset.action, params);
      return;
    }
    const txt = chip.dataset.text || '';
    if (window.heroQuick) window.heroQuick(txt);
  });

  // Положить текст в input при клике на chip
  // Лёгкий toast в стиле приложения — мини-уведомление снизу-справа.
  window.toast = (text, ttl=2400) => {
    let host = document.getElementById('app-toast-host');
    if (!host) {
      host = document.createElement('div');
      host.id = 'app-toast-host';
      document.body.appendChild(host);
    }
    const el = document.createElement('div');
    el.className = 'app-toast';
    el.textContent = text;
    host.appendChild(el);
    setTimeout(() => el.classList.add('app-toast-show'), 10);
    setTimeout(() => {
      el.classList.remove('app-toast-show');
      setTimeout(() => el.remove(), 200);
    }, ttl);
  };

  // Кастомный confirm-модал в стиле приложения — Promise<bool>.
  // Заменяет браузерный confirm() который вылазит у самой верхней
  // адресной строки и выглядит чужеродно.
  window.appConfirm = (opts) => {
    const {title=tr('card.confirm_action'), message='', danger=false,
           okLabel=tr('common.confirm'), cancelLabel=tr('common.cancel')} = (opts || {});
    return new Promise((resolve) => {
      const back = document.createElement('div');
      back.className = 'app-confirm-back';
      back.innerHTML =
        '<div class="app-confirm">'
        + '<div class="app-confirm-title">' + esc(title) + '</div>'
        + '<div class="app-confirm-msg">' + esc(message).replace(/\n/g,'<br>') + '</div>'
        + '<div class="app-confirm-actions">'
        +   '<button class="app-confirm-cancel">' + esc(cancelLabel) + '</button>'
        +   '<button class="app-confirm-ok' + (danger ? ' app-confirm-danger' : '') + '">' + esc(okLabel) + '</button>'
        + '</div></div>';
      document.body.appendChild(back);
      const close = (v) => { back.remove(); resolve(v); };
      back.querySelector('.app-confirm-cancel').addEventListener('click', () => close(false));
      back.querySelector('.app-confirm-ok').addEventListener('click', () => close(true));
      back.addEventListener('click', (e) => { if (e.target === back) close(false); });
      const esc_handler = (e) => {
        if (e.key === 'Escape') { document.removeEventListener('keydown', esc_handler); close(false); }
      };
      document.addEventListener('keydown', esc_handler);
      setTimeout(() => back.querySelector('.app-confirm-ok')?.focus(), 30);
    });
  };

  // Смена страны прибытия → перезаполняем список городов под выбранную страну
  // и чистим поле города. «Другая страна» → пустой список (свободный ввод).
  // Клик по неактивной ячейке CIP/DDP («укажите место прибытия ↓») — прокрутить
  // к форме адреса и сфокусировать поле страны. Интуитивно: увидел цены →
  // выбрал CIP/DDP → появилась/подсветилась форма «Куда доставить?».
  window.dfFocus = (cell) => {
    const block = cell.closest('.sm-block');
    const form = block && block.querySelector('.df-block');
    if (!form) return;
    try { form.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (e) {}
    const inp = form.querySelector('.df-country') || form.querySelector('.df-port');
    if (inp) setTimeout(() => { try { inp.focus(); } catch (e) {} }, 350);
  };

  // Клик по DDP-ячейке (доставка до двери). Цена уже посчитана, но для оформления
  // нужен полный адрес. Нет адреса → прокрутить к форме, сфокусировать поле адреса
  // и попросить вписать. Есть адрес → оформляем заказ (quick_order) с ним.
  window.dfPickDDP = (cell) => {
    let params = {};
    try { params = JSON.parse(cell.dataset.params || '{}'); } catch (e) {}
    // Гость не может оформить без аккаунта → сразу к контекстной регистрации
    // (quick_order у анона = карточка «создать аккаунт и оплатить» + resume).
    // Адрес до двери соберём после входа — гостю заполнять его рано. Иначе
    // dfPickDDP прокручивал вверх к форме, и клик выглядел «без действия».
    if (!window.IS_AUTHENTICATED) {
      window.quickAction && window.quickAction('quick_order', params);
      return;
    }
    // Адрес уже известен (введён ранее → при cip_available && delivery_address
    // форма доставки скрыта) → оформляем сразу, форму не ищем. Без этого клик по
    // DDP «проваливался»: формы нет → dfPickDDP молча выходил, доставка «не выбиралась».
    if (params.delivery_address) {
      window.quickAction && window.quickAction('quick_order', params);
      return;
    }
    const smBlock = cell.closest('.sm-block');
    const form = smBlock && smBlock.querySelector('.df-block');
    // Формы нет и адреса нет — клик не должен быть «мёртвым»: уходим в quick_order
    // (бэкенд запросит адрес до двери для DDP / покажет нужный экран).
    if (!form) { window.quickAction && window.quickAction('quick_order', params); return; }
    const addrEl = form.querySelector('.df-addr');
    const addr = (addrEl && addrEl.value || '').trim();
    const order = (a) => { params.delivery_address = a; window.quickAction && window.quickAction('quick_order', params); };
    // Адрес уже есть → оформляем сразу (без повторного выбора базиса/пересчёта).
    if (addr) { order(addr); return; }
    // Адреса нет → финализация прямо в форме: фокус на поле адреса + зелёная
    // кнопка «Оформить DDP». Базис заново выбирать НЕ нужно — цена уже известна.
    try { form.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (e) {}
    if (addrEl) setTimeout(() => { try { addrEl.focus(); } catch (e) {} }, 350);
    let btn = form.querySelector('.df-ddp-finalize');
    const calcBtn = form.querySelector('.df-submit:not(.df-ddp-finalize)');
    if (!btn) {
      btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'df-submit act-btn df-ddp-finalize';
      btn.style.cssText = 'background:rgba(46,160,67,0.18);border:1px solid rgba(46,160,67,0.6);font-weight:600;margin-top:8px;display:block;';
      if (calcBtn && calcBtn.parentNode) calcBtn.parentNode.insertBefore(btn, calcBtn.nextSibling);
      else form.appendChild(btn);
    }
    // После выбора DDP кнопка «Рассчитать/Считаем…» избыточна (цена уже известна)
    // и путает — видны две кнопки. Прячем её, оставляя одну CTA «Оформить DDP».
    if (calcBtn) calcBtn.style.display = 'none';
    const landed = (cell.querySelector('.sm-landed') && cell.querySelector('.sm-landed').textContent || '').trim();
    btn.textContent = '✅ Оформить DDP до двери' + (landed ? ' · ' + landed : '');
    btn.onclick = () => {
      const a = (addrEl && addrEl.value || '').trim();
      if (!a) { window.toast && window.toast('🚪 Впишите адрес доставки до двери', 3000); addrEl && addrEl.focus(); return; }
      order(a);
    };
    window.toast && window.toast('🚪 Впишите адрес и нажмите «Оформить DDP» — базис менять не нужно', 4000);
  };

  window.dfCountryChange = (inp, commit) => {
    const block = inp.closest('.df-block');
    if (!block) return;
    let nameMap = {}, cityMap = {};
    try { nameMap = JSON.parse(block.dataset.countries || '{}'); } catch (e) {}
    try { cityMap = JSON.parse(block.dataset.arrival || '{}'); } catch (e) {}
    const cc = nameMap[(inp.value || '').trim().toLowerCase()] || '';
    const cities = (cityMap[cc] || {}).cities || [];
    const cityInp = block.querySelector('.df-port');
    const dl = cityInp ? document.getElementById(cityInp.getAttribute('list')) : null;
    if (dl) dl.innerHTML = cities.map(c => `<option value="${esc(c)}"></option>`).join('');
    // commit (выбор из списка / blur) — чистим город и обновляем подсказку-placeholder.
    // oninput (печать) — только обновляем список городов, не трогаем поле.
    if (cityInp && commit) {
      cityInp.value = '';
      const firstCity = cities[0] ? (cities[0].split('—')[1] || '').trim() : '';
      cityInp.placeholder = cities.length ? ('Напр.: ' + (firstCity || 'город')) : 'Город прибытия';
    }
  };

  window.calcShipping = (btn) => {
    const block = btn.closest('.df-block');
    if (!block) return;
    const port = (block.querySelector('.df-port')?.value || '').trim();
    const addr = (block.querySelector('.df-addr')?.value || '').trim();
    if (!port) {
      window.toast && window.toast('⚠️ Укажите место прибытия для расчёта CIP/DDP', 3000);
      return;
    }
    // Страна: приоритет — явно выбранная страна (название → ISO-2 по карте),
    // затем префикс кода города ("RUMOW — Москва" → "RU").
    const head = port.split(/\s+/)[0] || '';
    const fromPrefix = (head.length >= 2 && /^[A-Za-z]{2}/.test(head)) ? head.slice(0,2).toUpperCase() : '';
    let nameMap = {};
    try { nameMap = JSON.parse(block.dataset.countries || '{}'); } catch (e) {}
    const countryRaw = (block.querySelector('.df-country')?.value || '').trim();
    const selCC = nameMap[countryRaw.toLowerCase()]
      || (/^[A-Za-z]{2}$/.test(countryRaw) ? countryRaw.toUpperCase() : '');
    const country = selCC || fromPrefix || '';
    let articles = []; let qty = null;
    try { articles = JSON.parse(block.dataset.articles || '[]'); } catch(e){}
    try { qty = block.dataset.qty ? JSON.parse(block.dataset.qty) : null; } catch(e){}
    const params = {
      articles: articles,
      dest_country: country,
      delivery_address: addr,
      arrival_port: port,
    };
    if (qty) params.quantities = qty;
    const _origText = btn.textContent;
    btn.disabled = true; btn.textContent = '⏳ Считаем…';
    quickAction('search_parts', params);
    // Результат приходит НОВОЙ карточкой со свежей формой; эту (старую) кнопку
    // возвращаем в исходный вид, чтобы она не зависала на «⏳ Считаем…».
    setTimeout(() => { try { btn.disabled = false; btn.textContent = _origText; } catch (e) {} }, 2500);
  };

  // Inline-выполнение триггера: без новых сообщений в чате и скролла.
  // Кнопка в чек-листе → отмечает себя «done», апдейтит счётчик и
  // разблокирует «Подтвердить», если все триггеры закрыты.
  window.sqTrigger = async (btn, orderId, status, triggerId) => {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.dataset._busy = '1';
    try {
      const r = await fetch('/api/assistant/action/', {
        method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},
        body: JSON.stringify({action:'complete_trigger',
          params:{order_id: orderId, status, trigger_id: triggerId}}),
      });
      const j = await r.json();
      if (!r.ok || j.error) {
        btn.disabled = false; delete btn.dataset._busy;
        window.toast && window.toast('❌ ' + (j.error || tr('common.error')), 3000);
        return;
      }
      // Помечаем триггер как выполненный
      btn.classList.add('sq-trigger-done');
      const mark = btn.querySelector('.sq-trigger-mark');
      if (mark) mark.textContent = '☑';
      // Считаем сколько закрыто в этом ордере и апдейтим advance-кнопку
      const orderRow = btn.closest('.sq-order');
      const advBtn = orderRow?.querySelector('.sq-btn:not(.sq-open):not(.sq-cancel)');
      const total = parseInt(advBtn?.dataset.checklistTotal || '0', 10);
      const done = orderRow.querySelectorAll('.sq-trigger.sq-trigger-done').length;
      if (advBtn) {
        advBtn.dataset.doneCount = done;
        if (done >= total) {
          advBtn.disabled = false;
          advBtn.classList.remove('sq-btn-locked');
          // Убрать "🔒 X/Y" — оставить только базовый label.
          advBtn.textContent = advBtn.textContent.replace(/\s*🔒\s*\d+\/\d+\s*/, '');
        } else {
          advBtn.textContent = advBtn.textContent.replace(/🔒\s*\d+\/\d+/, `🔒 ${done}/${total}`);
        }
      }
      // Заголовок чек-листа: обновить N/M
      const title = orderRow.querySelector('.sq-checklist-title');
      if (title && total) title.textContent = `⚡ Триггеры этапа (${done}/${total}):`;
    } catch(e) {
      btn.disabled = false; delete btn.dataset._busy;
      window.toast && window.toast('❌ Сетевая ошибка', 3000);
    }
  };

  // Inline-переход на следующий статус. После успешного перехода
  // перерисовываем всю .sq-card свежими данными — заказ переезжает
  // в следующую секцию визуально без скролла и без нового сообщения в чате.
  window.sqAdvance = async (btn, orderId, action) => {
    if (btn.disabled) return;
    btn.disabled = true;
    const origText = btn.textContent;
    btn.textContent = '⏳ ...';
    const card = btn.closest('.sq-card');
    const scrollY = window.scrollY;
    const streamScroll = document.getElementById('stream')?.scrollTop;
    try {
      const r = await fetch('/api/assistant/action/', {
        method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},
        body: JSON.stringify({action, params:{order_id: orderId}}),
      });
      const j = await r.json();
      if (!r.ok || j.error) {
        btn.disabled = false; btn.textContent = origText;
        window.toast && window.toast('❌ ' + (j.error || (j.text || '').slice(0, 100) || 'Ошибка'), 4000);
        return;
      }
      const status = (j.text || '').match(/«([^»]+)»/)?.[1] || 'следующий этап';
      // Получаем свежую версию pipeline и перерисовываем КОНКРЕТНУЮ карточку
      const r2 = await fetch('/api/assistant/action/', {
        method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},
        body: JSON.stringify({action:'seller_pipeline', params:{}}),
      });
      const j2 = await r2.json();
      const cardData = (j2.cards || []).find(c => c.type === 'seller_queue');
      if (cardData && card && typeof window.renderSellerQueueInto === 'function') {
        window.renderSellerQueueInto(card, cardData.data);
      } else if (cardData && card) {
        // Fallback — простой DOM-replace через renderers (если экспортирован)
        const renderers = window.__cardRenderers;
        if (renderers && renderers.seller_queue) {
          const newHtml = renderers.seller_queue(cardData.data);
          const wrap = document.createElement('div');
          wrap.innerHTML = newHtml;
          card.replaceWith(wrap.firstElementChild);
        }
      }
      // Возвращаем скролл (renderers могли изменить высоту)
      if (streamScroll != null) document.getElementById('stream').scrollTop = streamScroll;
      else window.scrollTo(0, scrollY);
      window.toast && window.toast(`✓ Заказ #${orderId} → ${status}`, 2500);
    } catch(e) {
      btn.disabled = false; btn.textContent = origText;
      window.toast && window.toast('❌ Сетевая ошибка', 3000);
    }
  };

  // Отмена draft-карточки (кнопка «Отмена» внутри dr-card).
  // Заменяет всю карточку на короткую заметку «↩︎ Действие отменено».
  // Текст берётся из i18n, поэтому работает на всех языках.
  window.cancelDraftCard = (btnEl) => {
    if (!btnEl) return;
    const card = btnEl.closest('.dr-card');
    if (!card) return;
    const note = document.createElement('div');
    note.className = 'dr-cancelled-note';
    note.textContent = tr('common.cancelled_note');
    card.replaceWith(note);
  };

  // Продавец отменяет неоплаченный заказ (если резерв не пришёл)
  window.sellerCancelPending = async (orderId, total) => {
    const ok = await window.appConfirm({
      title: `Отменить заказ #${orderId}?`,
      message: `Заказ на $${Number(total).toLocaleString('en-US')} будет удалён. Покупатель получит уведомление об отмене. Это используют если резерв не был оплачен в срок.`,
      danger: true,
      okLabel: '🗑 Отменить заказ',
      cancelLabel: tr('common.do_not_cancel'),
    });
    if (!ok) return;
    try {
      const r = await fetch('/api/assistant/action/', {
        method:'POST',
        headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},
        credentials:'same-origin',
        body: JSON.stringify({action:'seller_cancel_pending', params:{order_id: orderId}}),
      });
      const j = await r.json();
      if (!r.ok || j.error) {
        window.toast && window.toast('❌ Ошибка: ' + (j.error || r.status), 3000);
        return;
      }
      // Убираем строку заказа из DOM
      document.querySelectorAll('.sq-order').forEach(det => {
        const num = det.querySelector('.sq-order-num')?.textContent || '';
        if (num.includes('#' + orderId)) {
          det.style.transition = 'opacity 0.25s, transform 0.25s';
          det.style.opacity = '0';
          det.style.transform = 'scale(0.96)';
          setTimeout(() => det.remove(), 250);
        }
      });
      window.toast && window.toast(`✓ Заказ #${orderId} отменён`, 2000);
    } catch(e) {
      window.toast && window.toast('❌ Сетевая ошибка', 3000);
    }
  };

  // Подтверждение и отмена неоплаченного заказа
  window.cancelOrderPrompt = async (orderId, number, total) => {
    const ok = await window.appConfirm({
      title: `Отменить заказ ${number}?`,
      message: `Заказ на $${Number(total).toLocaleString('en-US')} будет удалён. Это безопасно — резерв ещё не списан с депозита.`,
      danger: true,
      okLabel: '🗑 Отменить заказ',
      cancelLabel: tr('common.do_not_cancel'),
    });
    if (!ok) return;
    // Прямой fetch вместо quickAction — чтобы дождаться ответа и убрать
    // карточку отменённого заказа из DOM в текущем списке.
    try {
      const r = await fetch('/api/assistant/action/', {
        method:'POST',
        headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},
        credentials:'same-origin',
        body: JSON.stringify({action:'cancel_order', params:{order_id: orderId}}),
      });
      const j = await r.json();
      if (!r.ok || j.error) {
        window.toast && window.toast('❌ Ошибка: ' + (j.error || r.status), 3000);
        return;
      }
      // Убираем все карточки этого заказа из текущего DOM
      document.querySelectorAll('.ord-cancel').forEach(btn => {
        const onclickStr = btn.getAttribute('onclick') || '';
        if (onclickStr.includes(`(${orderId},`)) {
          const card = btn.closest('.card');
          if (card) {
            card.style.transition = 'opacity 0.25s, transform 0.25s';
            card.style.opacity = '0';
            card.style.transform = 'scale(0.96)';
            setTimeout(() => card.remove(), 250);
          }
        }
      });
      window.toast && window.toast(`✓ ${number} отменён`, 2000);
    } catch (e) {
      window.toast && window.toast('❌ Сетевая ошибка', 3000);
    }
  };

  window.refreshWarehousePrice = async (wid, name) => {
    // Открываем загрузку прайса с подсказкой, что обновляется конкретный
    // склад. Импорт сам найдёт существующий склад по ports+address
    // и обновит цены в нём (вместо создания нового).
    addMessage('assistant',
      `🔄 Обновление прайса склада «${name}»\n\nЗагрузите новый файл — мы найдём существующий склад по портам/адресу и обновим цены позиций. Старые позиции сохранятся.`,
      [], [
        {label:'📤 Выбрать файл прайса', action:'upload_pricelist', params:{warehouse_hint: wid}},
        {label:'↩️ Отмена', action:'seller_warehouses', params:{}},
      ]);
  };

  window.deleteWarehouse = async (wid, name, partsCount) => {
    const msg = partsCount > 0
      ? `${partsCount} позиций будут перемещены в «без склада». Это действие нельзя отменить — но позиции сохранятся.`
      : `Склад пуст, позиций в нём нет.`;
    const ok = await window.appConfirm({
      title: `Удалить склад «${name}»?`,
      message: msg,
      danger: true,
      okLabel: '🗑 Удалить',
      cancelLabel: tr('common.cancel'),
    });
    if (!ok) return;
    try {
      const r = await fetch('/api/assistant/action/', {
        method:'POST',
        headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},
        credentials:'same-origin',
        body: JSON.stringify({action:'seller_warehouses',
                              params:{warehouse_id: wid, action: 'delete'}}),
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      // Удаляем строку склада из DOM во всех карточках чата, чтобы клик
      // по ней не пытался снова открыть несуществующий склад.
      document.querySelectorAll(`.wh-row[data-params*='"warehouse_id":${wid}']`).forEach(row => {
        // Анимация исчезновения
        row.style.transition = 'opacity 0.2s, transform 0.2s';
        row.style.opacity = '0';
        row.style.transform = 'scale(0.96)';
        setTimeout(() => row.remove(), 200);
      });
      // Лёгкий toast вместо отдельного сообщения в чате
      window.toast && window.toast(`✓ Склад «${name}» удалён`);
    } catch(e) {
      window.toast && window.toast('⚠️ Не удалось удалить: ' + (e.message || e), 4000);
    }
  };

  window.renameWarehouse = async (wid, oldName) => {
    const next = prompt(tr('prompt.rename_warehouse'), oldName || '');
    if (!next || !next.trim() || next.trim() === oldName) return;
    try {
      const r = await fetch('/api/assistant/action/', {
        method:'POST',
        headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},
        credentials:'same-origin',
        body: JSON.stringify({action:'seller_warehouses',
                              params:{warehouse_id: wid, rename_to: next.trim()}}),
      });
      const j = await r.json();
      addMessage('assistant', j.text || '✓ Переименовано', j.cards || [], j.actions || []);
    } catch(e) {
      addMessage('assistant', '⚠️ Не удалось переименовать: ' + (e.message || e));
    }
  };

  window.heroQuick = (text) => {
    const target = $('welcomeStage').classList.contains('hidden') ? $('input') : $('heroInput');
    if (target) {
      target.value = text;
      target.focus();
    }
  };

  function appendStream(text) {
    if (!state.currentBubble) {
      removeTyping();
      const wrap = document.createElement('div');
      wrap.className = 'msg msg-assistant';
      wrap.innerHTML = `${avatar('assistant')}<div class="msg-body"><div class="msg-author">Consolidator</div><div class="msg-cards"></div><div class="msg-content msg-content-ai"></div><div class="msg-refs"></div><div class="msg-actions"></div><div class="msg-ctx-actions"></div><div class="msg-suggestions"></div></div>`;
      $('streamInner').appendChild(wrap);
      state.currentBubble = wrap;
    }
    const el = state.currentBubble.querySelector('.msg-content');
    el.textContent += text;
    if ((el.textContent || '').trim()) el.classList.add('msg-has-text');
    scrollBottom();
  }

  function finishStream(cards, actions, refs, authoritativeText, contextualActions, suggestions) {
    removeTyping();
    if (!state.currentBubble) return;
    const contentEl = state.currentBubble.querySelector('.msg-content');
    let finalText;
    if (authoritativeText != null) {
      finalText = authoritativeText;
    } else {
      finalText = (contentEl.textContent || '')
        .replace(/\[card:\w+\]/g, '')
        .replace(/:::(?:actions|product|rfq|order|shipment|supplier|comparison|chart|file|table|spec_results|supplier_top)[\s\S]*?:::/g, '')
        .trim();
    }
    const hasCards = Array.isArray(cards) && cards.length > 0;
    if (finalText) {
      contentEl.innerHTML = linkifyEntities(finalText);
      // Same as addMessage: caption-стиль когда есть cards, чтобы не выглядело
      // как «два сообщения подряд». Bubble + 💬 только для чистого text-ответа.
      contentEl.classList.remove('msg-has-text', 'msg-caption');
      if (!hasCards) contentEl.classList.add('msg-has-text');
      else contentEl.classList.add('msg-caption');
    } else {
      contentEl.textContent = '';
    }
    state.currentBubble.querySelector('.msg-refs').innerHTML = renderContextRefs(refs || []);
    state.currentBubble.querySelector('.msg-cards').innerHTML = renderCards(cards, {fromServer: true});
    state.currentBubble.querySelector('.msg-actions').innerHTML = renderActions(actions);
    const ctxEl = state.currentBubble.querySelector('.msg-ctx-actions');
    if (ctxEl) ctxEl.innerHTML = renderContextualActions(contextualActions || []);
    const sgEl = state.currentBubble.querySelector('.msg-suggestions');
    if (sgEl) sgEl.innerHTML = renderSuggestions(suggestions || []);
    // После рендера больших карточек скроллим к началу AI-сообщения,
    // чтобы юзер видел заголовок/таблицу сверху, а не упирался в самый низ.
    if (cards && cards.length > 0) {
      const bubble = state.currentBubble;
      const scrollToTop = () => {
        const s = $('stream');
        if (!s || !bubble) return;
        // Большой отступ сверху (60px) — чтобы аватар/имя «Consolidator»
        // и немного «воздуха» сверху были видны, а карточка не «впивалась»
        // в верхний край viewport.
        const off = bubble.offsetTop - 60;
        s.scrollTo({top: Math.max(0, off), behavior: 'smooth'});
      };
      scrollToTop();
      requestAnimationFrame(scrollToTop);
      setTimeout(scrollToTop, 300);
    }
    state.currentBubble = null;
    state.streaming = false;
    $('sendBtn').disabled = false;
    $('heroSendBtn').disabled = false;
  }

  function scrollBottom() {
    setTimeout(() => {
      const s = $('stream');
      if (s) s.scrollTop = s.scrollHeight;
    }, 30);
  }

  // ══════════════════════════════════════════════════════════
  // WebSocket
  // ══════════════════════════════════════════════════════════
  function connectWS() {
    if (state.ws && state.ws.readyState <= 1) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const path = state.convId ? `/ws/assistant/${state.convId}/` : '/ws/assistant/';
    try { state.ws = new WebSocket(proto + '//' + location.host + path); } catch(e) { return; }

    state.ws.onopen = () => { state.wsRetry = 0; };
    state.ws.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.type === 'connected') {
          setConvId(d.conversation_id);
          loadConvList();
        } else if (d.type === 'thinking') {
          if (!$('typingMsg')) addTyping(state._intent);
        } else if (d.type === 'stream') {
          removeTyping();
          appendStream(d.content);
        } else if (d.type === 'context') {
          state._lastRefs = d.refs || [];
        } else if (d.type === 'cards') {
          state._lastCards = d.cards || [];
          state._lastActions = d.actions || [];
          state._lastCtxActions = d.contextual_actions || [];
          state._lastSuggestions = d.suggestions || [];
          state._lastText = d.text;
        } else if (d.type === 'done') {
          state._wsPending = null;   // ответ дошёл целиком — восстанавливать нечего
          // Auto-attach «← Назад» + «🏠 Главная» если backend не дал свою навигацию
          // + дефолтные suggestions если бэкенд вернул пустой список
          const ctxActs = ensureHomeNav(state._lastCtxActions || []);
          const sugs = ensureSuggestions(state._lastSuggestions || []);
          finishStream(state._lastCards, state._lastActions, state._lastRefs || d.refs, state._lastText, ctxActs, sugs);
          state._lastCards = []; state._lastActions = []; state._lastRefs = []; state._lastText = null;
          state._lastCtxActions = []; state._lastSuggestions = [];
        } else if (d.type === 'error') {
          state._wsPending = null;   // серверная ошибка, не обрыв — не до-запрашиваем
          finishStream([], []);
          addMessage('assistant', '⚠️ ' + d.message);
        } else if (d.type === 'notification') {
          showNotifToast(d.payload || {});
        } else if (d.type === 'order_update') {
          // Live-обновление: если seller/buyer/operator сейчас в shipment-
          // чате этого ORD — перезагружаем conv (там уже свежее системное
          // сообщение с обновлённым timeline). Иначе toast «Обновление по
          // ORD-N», клик → переход в чат.
          const inChat = state.convId && d.conversation_id === state.convId;
          if (inChat) {
            // Перезагружаем messages текущего чата
            try { loadConv(state.convId, {silent: true}); } catch(_){}
          } else {
            showNotifToast({
              title: '📦 Обновление по ORD-' + d.order_id,
              body: 'Открыть чат сделки →',
              url: d.conversation_id ? '/chat/' : null,
            });
          }
          loadConvList();  // bump «непрочитанных» в sidebar
        } else if (d.type === 'operator_alert') {
          showNotifToast({
            title: (d.event === 'sla_semi_overdue'   ? '⚠️ SEMI просрочен' :
                    d.event === 'sla_manual_overdue' ? '⚠️ MANUAL >48ч' :
                    d.event === 'sla_breach'         ? '🔥 SLA breach' :
                    d.event === 'claim_opened'       ? '🛡 Открыт claim' :
                    'Алерт оператору'),
            body: 'Откройте «Алерты оператора» в сайдбаре',
          });
          loadConvList();
        }
      } catch(e){ console.error(e); }
    };
    state.ws.onclose = (ev) => {
      if (ev.code === 4401) return;
      // Обрыв WS ПОСРЕДИ ответа (не дошёл 'done'): иначе остался бы зависший
      // индикатор + заблокированная кнопка. Тихо до-запрашиваем тот же текст
      // по HTTP (api сам ретраит обрыв) — пользователь обрыва не видит.
      if (state.streaming && state._wsPending) {
        const _t = state._wsPending; state._wsPending = null;
        _recoverInterruptedStream(_t);
      }
      state.wsRetry++;
      const delay = Math.min(1000 * Math.pow(2, state.wsRetry), 30000);
      setTimeout(connectWS, delay);
    };
  }

  // Навигация/teardown ВО ВРЕМЯ активного стрима (открыли другой чат, новый чат,
  // сменили роль, загрузили файл): осознанно бросаем in-flight сообщение —
  // сбрасываем флаги, снимаем индикатор, разблокируем ввод. КРИТИЧНО: без сброса
  // _wsPending отложенный onclose старого сокета запустил бы ложный recovery и
  // влепил бы устаревший ответ в уже другой чат (+ перетёр convId).
  function _abandonStream() {
    state._wsPending = null;
    state.streaming = false;
    state.currentBubble = null;
    try { removeTyping(); } catch (_) {}
    const sb = $('sendBtn'), hb = $('heroSendBtn');
    if (sb) sb.disabled = false;
    if (hb) hb.disabled = false;
  }

  // Обрыв WS на полпути. КРИТИЧНО: consumers.py материализует генератор ЦЕЛИКОМ
  // ДО стрима, поэтому к моменту обрыва ответ (вкл. возможный create_rfq) УЖЕ
  // сохранён в БД. Раньше recovery делал повторный POST /chat/ → ЗАНОВО гнал
  // AI-пайплайн → второй create_rfq, второй AI-кредит, дубль user+assistant.
  // Теперь: сначала ПЕРЕЗАГРУЖАЕМ разговор (GET, idempotent, api сам ретраит),
  // и если ответ уже в БД — рендерим его БЕЗ повторного запуска пайплайна.
  async function _recoverInterruptedStream(text) {
    // Сразу показываем индикатор: чинит «зависший пустой экран» на ~90с,
    // пока GET тянет сохранённый ответ (intent — как при отправке).
    removeTyping();
    if (state.currentBubble) { try { state.currentBubble.remove(); } catch(_){} state.currentBubble = null; }
    addTyping(state._intent || null);
    try {
      // Перезагружаем разговор. Если последняя пара = user(text) → assistant,
      // значит ответ уже сохранён — рендерим его и выходим БЕЗ POST.
      const conv = await api('/api/assistant/conversations/' + state.convId + '/');
      const msgs = (conv && conv.messages) || [];
      const last = msgs[msgs.length - 1];
      const prev = msgs[msgs.length - 2];
      const saved = last && last.role === 'assistant'
        && prev && prev.role === 'user'
        && (prev.content || '').trim() === text.trim();
      if (saved) {
        removeTyping();
        // Сериализатор сообщений отдаёт id/role/content/cards/actions/context_refs
        // (suggestions/contextual_actions в нём нет — как и в openConv), поэтому
        // навигацию/подсказки достраиваем дефолтами, как при обычной загрузке.
        const _ctx = ensureHomeNav(last.contextual_actions || []);
        const _sugs = ensureSuggestions(last.suggestions || []);
        addMessage('assistant', last.content, last.cards || [], last.actions || [],
          last.context_refs || [], last.id || last.message_id || null, _sugs, _ctx);
        loadConvList();
        return;
      }
      // Ответа в БД нет (редкий ранний обрыв — до материализации генератора):
      // тогда безопасно до-запросить по HTTP (пайплайн ещё не отработал).
      const r = await api('/api/assistant/chat/', {
        method: 'POST',
        body: JSON.stringify({conversation_id: state.convId, message: text}),
        retryNetwork: true, timeoutMs: 30000,
      });
      removeTyping();
      setConvId(r.conversation_id);
      const _ctx = ensureHomeNav(r.contextual_actions || []);
      const _sugs = ensureSuggestions(r.suggestions || []);
      addMessage('assistant', r.response, r.cards, r.actions, r.context_refs || [], r.message_id || null, _sugs, _ctx);
      loadConvList();
    } catch (e) {
      removeTyping();
      addMessage('assistant', '⚠️ Соединение прервалось — нажмите «Повторить».',
        [], [{action: '__resend_chat', params: {text: text}, label: '🔄 Повторить'}]);
    } finally {
      state.streaming = false;
      $('sendBtn').disabled = false;
      $('heroSendBtn').disabled = false;
    }
  }

  // Ждём открытия WS до ms миллисекунд (для устойчивой отправки первого сообщения:
  // WS стримит чанками и реже рвётся на РФ-канале, чем длинный одиночный HTTP-POST).
  function _waitWsOpen(ms) {
    return new Promise(resolve => {
      if (state.ws && state.ws.readyState === 1) return resolve(true);
      const t0 = Date.now();
      const iv = setInterval(() => {
        if (state.ws && state.ws.readyState === 1) { clearInterval(iv); resolve(true); }
        else if (Date.now() - t0 > ms) { clearInterval(iv); resolve(false); }
      }, 80);
    });
  }

  // ══════════════════════════════════════════════════════════
  // Send & actions
  // ══════════════════════════════════════════════════════════
  // Зеркало server-side fast_path: чистая вставка OEM-кодов без слов.
  // Если совпало — показываем МИНИМАЛЬНЫЙ индикатор (3 точки) вместо
  // «Анализирую запрос…» — это не LLM, это код-поиск, должен быть быстро.
  function isFastPathOemPaste(text) {
    if (!text) return false;
    const OEM_RE = /^[A-Za-z0-9][A-Za-z0-9\-/.]{3,18}$/;
    const QTY_TRAIL_RE = /^\d+(?:[\.,]\d+)?(?:шт|pcs|x|×)?$/i;
    const lines = text.split(/[\n,;]+/);
    let oemCount = 0;
    let foreignWords = 0;
    for (const line of lines) {
      const tokens = line.split(/\s+/).map(t => t.replace(/^\.+|\.+$/g, '').trim()).filter(Boolean);
      for (const tok of tokens) {
        if (OEM_RE.test(tok) && /\d/.test(tok) && !/^\d+$/.test(tok)) { oemCount++; continue; }
        if (QTY_TRAIL_RE.test(tok) || /^\d+$/.test(tok.replace(/^[xX×]/, ''))) continue;
        // Слово 2+ букв = «человеческий» запрос (вопрос), не чистая вставка.
        let alphaChars = 0;
        for (const ch of tok) if (/[a-zA-Zа-яА-Я]/.test(ch)) alphaChars++;
        if (alphaChars >= 2) foreignWords++;
      }
    }
    return oemCount >= 1 && foreignWords === 0;
  }

  async function send(fromHero) {
    const inp = fromHero ? $('heroInput') : $('input');
    const text = inp.value.trim();
    if (!text || state.streaming) return;

    const intent = pickIntent(text);
    state._intent = intent;
    addMessage('user', text);
    inp.value = '';
    inp.style.height = 'auto';
    state.streaming = true;
    $('sendBtn').disabled = true;
    $('heroSendBtn').disabled = true;
    setTimeout(() => $('input').focus(), 100);

    // Fast-path детекция: чистая вставка OEM — минимальный индикатор.
    const minimalTyping = isFastPathOemPaste(text);

    addTyping(intent, minimalTyping);
    // Первое сообщение после загрузки часто застаёт WS ещё не открытым → раньше
    // падало в длинный HTTP-POST, который рвётся на нестабильном РФ-канале
    // («Соединение прервалось — Повторить»). Ждём WS до 2.5с (стримит чанками —
    // устойчивее), и только если не поднялся — HTTP-fallback.
    if (!(state.ws && state.ws.readyState === 1)) {
      if (!state.ws || state.ws.readyState >= 2) connectWS();
      await _waitWsOpen(2500);
    }
    if (state.ws && state.ws.readyState === 1) {
      // Запоминаем in-flight текст: если WS оборвётся ДО 'done', onclose тихо
      // до-запросит этот же текст по HTTP (без зависшего спиннера).
      state._wsPending = text;
      state.ws.send(JSON.stringify({type:'message', content:text}));
    } else {
      try {
        const r = await api('/api/assistant/chat/', {
          method:'POST',
          body: JSON.stringify({conversation_id: state.convId, message: text}),
          // Этот POST — ПЕРВИЧНАЯ отправка (WS не поднялся), а не recovery: ответа
          // в БД ещё нет, ретрай обрыва канала здесь корректен (до 3 попыток с
          // backoff внутри api()) — иначе на нестабильном РФ-канале мгновенно
          // вылетала бы карточка «Соединение прервалось». ВНИМАНИЕ: чат-текст МОЖЕТ
          // мутировать (create_rfq материализует RFQ прямо из текста), поэтому
          // ретрай тут безопасен лишь до первого успешного ответа сервера — обрыв
          // на этапе fetch означает, что пайплайн ещё не отработал. 30с под
          // медленный AI-ответ через релей (8с-дефолт его бы рубил).
          retryNetwork: true,
          timeoutMs: 30000,
        });
        removeTyping();
        setConvId(r.conversation_id);
        // ВСЕГДА добавляем «← Назад» + «🏠 Главная» в контекстные действия,
        // даже на HTTP-fallback пути. Юзер требует эти кнопки в КАЖДОМ ответе.
        const _ctx = ensureHomeNav(r.contextual_actions || []);
        const _sugs = ensureSuggestions(r.suggestions || []);
        addMessage('assistant', r.response, r.cards, r.actions, r.context_refs || [], r.message_id || null, _sugs, _ctx);
        state.streaming = false;
        $('sendBtn').disabled = false;
        $('heroSendBtn').disabled = false;
        loadConvList();
      } catch(e) {
        removeTyping();
        // Транзиент (обрыв канала / 5xx / 52x от Cloudflare) — действие не выполнилось,
        // показываем понятное сообщение + «Повторить» (re-send того же текста), а не сырое «→ 521».
        const _m = (e && e.message) || '';
        const _transient = /Failed to fetch|NetworkError|network|load failed|abort/i.test(_m)
          || (e && e.status >= 500) || /→ 5\d\d$/.test(_m);
        if (_transient) {
          addMessage('assistant', '⚠️ Соединение прервалось — нажмите «Повторить».',
            [], [{action: '__resend_chat', params: {text: text}, label: '🔄 Повторить'}]);
        } else {
          addMessage('assistant', '⚠️ ' + e.message);
        }
        state.streaming = false;
        $('sendBtn').disabled = false;
        $('heroSendBtn').disabled = false;
      }
    }
  }

  // Hero button: send if input has text, else voice
  window.heroAction = () => {
    const text = $('heroInput').value.trim();
    if (text) send(true);
    else toggleVoice();
  };

  // Update hero button icon based on input
  function updateHeroIcon() {
    const text = $('heroInput').value.trim();
    const btn = $('heroSendBtn');
    if (text) {
      btn.classList.add('send');
      btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
    } else {
      btn.classList.remove('send');
      btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/><path d="M19 10v2a7 7 0 01-14 0v-2M12 19v4M8 23h8"/></svg>';
    }
  }

  // Whitelist «фастовых» actions — просто DB read, без AI/RAG/external API.
  // Для них спиннер вообще не показывается: открытие должно быть моментальным.
  // Если action не в этом списке (search_parts с эмбеддингом, generate_proposal,
  // analyze_spec, kb_search) — спиннер появится через 600ms.
  const FAST_ACTIONS = new Set([
    // Reads
    'get_orders', 'get_order_detail', 'track_order', 'track_shipment',
    'get_rfq_status', 'rfq_detail', 'view_rfq_quotes', 'view_quote',
    'get_balance', 'get_budget', 'get_analytics', 'get_supply_report', 'get_savings', 'get_buyer_discount', 'recent_activity',
    'seller_analytics_hub', 'seller_executive_report',
    // Deposit top-up flow
    'topup_wallet', 'start_topup', 'submit_topup', 'confirm_topup_paid',
    'cancel_topup', 'list_topups',
    'get_demand_report', 'get_sla_report', 'get_claims',
    // Support Hub — все pure-DB
    'support_home', 'kb_faq', 'my_verifications', 'my_bonuses',
    'compare_products', 'compare_suppliers', 'top_suppliers',
    // Seller cabinet reads
    'seller_dashboard', 'seller_finance', 'seller_rating', 'seller_pipeline',
    'seller_inbox', 'seller_catalog', 'seller_drawings', 'seller_team',
    'seller_integrations', 'seller_reports', 'seller_qr', 'seller_logistics',
    'seller_negotiations',
    // Operator/admin reads
    'op_dashboard', 'op_queue', 'op_sla_breach', 'op_order_detail',
    'op_analytics_hub',
    'op_topup_queue', 'op_confirm_topup', 'op_reject_topup',
    'op_logistics_stats', 'op_payments_stats', 'op_payments_dashboard',
    'op_kyb_queue', 'op_kyb_review', 'op_kyb_check', 'op_kyb_clarify',
    'op_customs_dashboard', 'op_hs_lookup', 'op_calc_duty',
    'op_certs_check', 'op_sanctions_check',
    'admin_dashboard', 'admin_gmv', 'admin_users', 'admin_user_detail',
    'admin_moderation_queue', 'admin_catalog_review', 'admin_platform_settings',
    // Onboarding wizard step rendering
    'start_onboarding', 'kyb_status', 'update_kyb_contacts',
    'submit_company_info', 'submit_legal_address',
    'submit_bank', 'submit_director', 'submit_for_review',
    // Notification settings
    'notif_prefs', 'notif_set_email', 'notif_set_kinds', 'notif_link_telegram',
    // Auth
    'list_api_tokens',
    // Open-card preview steps (DraftCard step1)
    'pay_reserve', 'pay_final', 'confirm_delivery',
    'op_assign', 'op_add_note', 'op_resolve_dispute',
    'op_hs_assign', 'op_cert_upload', 'op_customs_release',
    'admin_ban_user', 'admin_unban_user',
    'create_api_token', 'revoke_api_token',
    'setup_2fa', 'verify_2fa', 'disable_2fa',
    'submit_quote', 'respond_to_counter', 'mark_quote_final',
    'accept_quote', 'counter_offer', 'decline_quote', 'send_rfq_to_suppliers',
    // Operator misc
    'audit_log', 'notifications', 'generate_qr',
    // Misc
    'open_url', 'topup_wallet',
  ]);

  // Quick action from pills/cards
  // ── Concurrency guard for action calls ────────────────────────
  // Защищает от двойного клика (мульти-открытия одного и того же действия).
  // Inline `onclick="quickAction(...)"` обходит .act-btn delegated handler,
  // поэтому централизуем guard здесь. Ключ — action+params hash.
  const _inflightActions = new Set();
  function _actionKey(action, params) {
    // Стабильный ключ: action + sorted JSON params (без _label/_url, они UI-only)
    const p = {...(params || {})};
    delete p._label;
    delete p._url;
    const keys = Object.keys(p).sort();
    const norm = {};
    for (const k of keys) norm[k] = p[k];
    return action + '|' + JSON.stringify(norm);
  }
  function _markBusy(el, busy) {
    // Guard: el может быть document/text-node — у них нет dataset/classList.
    if (!el || el.nodeType !== 1 || !el.classList || !el.dataset) return;
    if (busy) {
      el.dataset._loading = '1';
      el.setAttribute('aria-busy', 'true');
      el.classList.add('is-loading');
    } else {
      delete el.dataset._loading;
      el.removeAttribute('aria-busy');
      el.classList.remove('is-loading');
    }
  }

  // Actions, после которых надо перерисовать видимый seller_inbox/queue
  const REFRESH_INBOX_TRIGGERS = new Set([
    'ship_order', 'advance_order', 'complete_trigger',
    'op_confirm_topup', 'op_reject_topup',
    'submit_quote', 'accept_quote', 'decline_quote',
  ]);

  async function refreshVisibleSellerInbox() {
    // Ищем последнюю видимую inbox/queue карточку в чате
    const targets = [];
    document.querySelectorAll('.cards > .card').forEach(card => {
      // inbox = list-card с секциями (data-attr нет, ищем по структуре)
      // или sq-card. Проще — найти message-cards-wrap'ы и проверить data
      // по api'шке. Делаем универсально: повторно вызываем seller_inbox
      // и заменяем innerHTML последнего message-cards там где была карточка.
    });
    try {
      const r = await api('/api/assistant/action/', {
        method: 'POST',
        body: JSON.stringify({conversation_id: state.convId, action: 'seller_inbox', params: {}}),
      });
      // Берём свежие карточки и подменяем содержимое последней msg-cards
      // которая принадлежит ранее-рендеренному seller_inbox.
      // Простой эвристик: ищем msg где есть .sq-section или text "Срочные задачи".
      const allMsgs = document.querySelectorAll('.msg .msg-cards');
      let lastInboxEl = null;
      allMsgs.forEach(el => {
        const txt = el.textContent || '';
        if (txt.includes('Срочные задачи') || txt.includes('К отгрузке (оплачено')
            || el.querySelector('.sq-section') || el.querySelector('.ls-card')) {
          lastInboxEl = el;
        }
      });
      if (lastInboxEl && Array.isArray(r.cards)) {
        lastInboxEl.innerHTML = renderCards(r.cards, {fromServer: true});
      }
    } catch (err) {
      console.warn('refreshVisibleSellerInbox failed:', err);
    }
  }

  window.quickAction = async (action, params) => {
    params = params || {};
    params._label = params._label || action;
    // Повторная отправка сообщения после обрыва канала (кнопка «Повторить» под чат-ошибкой).
    if (action === '__resend_chat') {
      try { const _i = $('input') || $('heroInput'); if (_i) { _i.value = params.text || ''; send(!!($('heroInput') && !$('input'))); } } catch (_) {}
      return;
    }
    // Старт уточняющих вопросов по товарам — ТОЛЬКО после того как юзер
    // заполнил общие поля поставщика и нажал «Продолжить». Последовательно.
    if (action === '__pl_start_questions') {
      try { const _qs = window.__pendingSmartQs || []; if (_qs.length && typeof showNextSmartQuestion === 'function') showNextSmartQuestion(_qs, 0); } catch (_) {}
      return;
    }
    // Spec-action: open OS file picker → multipart POST → re-render KYB doc card.
    // Не уходит на чат-endpoint — это локальная операция загрузки.
    if (action === '_upload_kyb_file') {
      const kind = params.kind || 'dealership';
      const accept = params.accept || '.pdf,.png,.jpg,.jpeg';
      const inp = document.createElement('input');
      inp.type = 'file'; inp.accept = accept; inp.style.display = 'none';
      inp.onchange = async () => {
        const f = inp.files && inp.files[0];
        if (!f) { inp.remove(); return; }
        const fd = new FormData();
        fd.append('file', f);
        try {
          const r = await fetch('/api/assistant/kyb/doc/' + encodeURIComponent(kind) + '/', {
            method: 'POST',
            headers: {'X-CSRFToken': csrf()},
            credentials: 'same-origin',
            body: fd,
          });
          if (!r.ok && r.status !== 201) {
            let msg = 'HTTP ' + r.status;
            try { const j = await r.json(); if (j && j.error) msg = j.error; } catch(_){}
            alert('Не удалось загрузить: ' + msg);
            return;
          }
          // Перерендерим карточку — вызовом upload_kyb_doc, чтобы показать новый статус
          window.quickAction('upload_kyb_doc', {kind});
        } catch(err) {
          alert('Ошибка загрузки: ' + (err && err.message ? err.message : err));
        } finally {
          inp.remove();
        }
      };
      document.body.appendChild(inp);
      inp.click();
      return;
    }
    // Source element для visual feedback. Ищем ближайший clickable element от
    // event.target — НЕ currentTarget (currentTarget = document когда listener
    // делегирован, у document нет dataset, что приводило к TypeError).
    const ev = (typeof window.event !== 'undefined') ? window.event : null;
    let srcEl = null;
    if (ev && ev.target && typeof ev.target.closest === 'function') {
      srcEl = ev.target.closest('button,a,[data-action],[data-href],.pill,.kpi-cell-clickable,.ls-row,.card-clickable');
    }
    // Per-action debounce: same payload, parallel call → reject.
    const key = _actionKey(action, params);
    if (_inflightActions.has(key)) return;
    // Per-element debounce: clicked element still loading → reject.
    if (srcEl && srcEl.dataset && srcEl.dataset._loading === '1') return;
    _inflightActions.add(key);
    _markBusy(srcEl, true);
    // Navigation shortcut: open URL directly (no AI round-trip, no new chat).
    // Sources of _url:
    //   1. Explicit params._url (e.g. "Перейти в кабинет")
    //   2. Backward-compat: legacy "Открыть RFQ"/"Открыть заказ" buttons that
    //      pre-date this fix shipped without _url. Synthesize one from the id
    //      and the action's label, so old chat history keeps working.
    // Navigation: только internal /chat/* — все «старые» URL (cabinet) игнорируются,
    // вся работа идёт внутри chat-first.
    let url = params._url;
    if (url) {
      url = url
        .replace(/^\/buyer\/rfqs\/(\d+)\/?$/, '/chat/rfq/$1/')
        .replace(/^\/rfq\/(\d+)\/?$/, '/chat/rfq/$1/')
        .replace(/^\/buyer\/orders\/(\d+)\/?$/, '/chat/')
        .replace(/^\/seller\/rfqs\/(\d+)\/?$/, '/chat/rfq/$1/');
      if (url.startsWith('/chat/')) {
        window.location.href = url;
        return;
      }
      // Все не-/chat/ URL — это или PDF/файлы, или внешка. Открываем в новой вкладке,
      // чтобы пользователь не уходил из чата.
      const isFile = /\.(pdf|xlsx?|csv|docx?|zip|png|jpe?g)(\?|$)/i.test(url);
      if (isFile) { window.open(url, '_blank', 'noopener'); return; }
      // Иначе — не уходим, превращаем в обычный action call (если action есть).
      if (!action) return;
    }
    // Не пишем ярлык кнопки в чат — это UI affordance, а не сообщение юзера.
    // Открываем conv view (чтобы welcome-stage не моргал).
    //
    // Спиннер: для фастовых actions (просто DB read) — мини-индикатор
    // через 150ms (если ответ <150мс — не моргает, если дольше — видно
    // что обрабатываем). Для AI-actions — полный typing через 250ms.
    // Раньше fast-actions вообще не показывали feedback → юзер думал
    // «висит, не работает» (см. жалобу про долгий get_order_detail с 9 поз).
    showConv();
    const isFast = FAST_ACTIONS.has(action);
    // FAST_ACTIONS = pure DB-read (≤200ms). Не показываем спиннер вообще —
    // он мигает и создаёт ощущение «висит». Если действие неожиданно
    // зависнет дольше 800ms — покажем минималку (защита от «мертвого UI»).
    const typingDelay = isFast
      ? setTimeout(() => addTyping("loading", true), 800)
      : setTimeout(() => addTyping(pickIntent(action)), 250);
    try {
      const r = await api('/api/assistant/action/', {
        method:'POST',
        body: JSON.stringify({conversation_id: state.convId, action, params}),
        // read-only действие → можно авто-ретраить обрыв канала (не задвоится).
        // Auth-формы без confirmed — тоже показ формы (read-only) → ретраим, чтобы
        // зависший POST не крутил спиннер вечно.
        retryNetwork: READONLY_ACTIONS.has(action)
          || (AUTH_DISPLAY_ACTIONS.has(action) && !params.confirmed),
      });
      if (typingDelay) clearTimeout(typingDelay);
      removeTyping();
      setConvId(r.conversation_id || state.convId);
      const ctxActs = ensureHomeNav(r.contextual_actions || []);
      // no_suggestions → не подставляем дефолтные подсказки (экраны, где
      // рекомендации не к месту, например отчёт об ошибках импорта).
      const sugs = r.no_suggestions ? [] : ensureSuggestions(r.suggestions || []);
      addMessage('assistant', r.text, r.cards, r.actions, r.context_refs || [], r.message_id || null, sugs, ctxActs);
      loadConvList();
      // Хук авто-reload (after start_registration / start_login success).
      // Backend ставит _post_action="reload" чтобы фронт автоматически
      // перезагрузил чат — юзер увидит свои данные / правильную роль / пиллы.
      if (r._post_action === 'reload') {
        // FIX: после смены аккаунта (login/register/switch_role) идём на
        // ?new=1 — это форсирует welcome (priority 4 в init), а не последнюю
        // conv предыдущего юзера. Также чистим cf_active_conv в обоих
        // storage'ах (sessionStorage и localStorage).
        const isAuthAction = action === 'switch_role_login'
          || action === 'start_login'
          || action === 'start_registration';
        if (isAuthAction) {
          try { localStorage.removeItem('cf_active_conv'); } catch(_) {}
          try { sessionStorage.removeItem('cf_active_conv'); } catch(_) {}
          setTimeout(() => { window.location.href = '/chat/?new=1'; }, 900);
        } else {
          setTimeout(() => window.location.reload(), 900);
        }
      }
      // Инвалидация inbox/pipeline после state-changing seller-action'ов:
      // отгрузил / двинул статус / финансист подтвердил пополнение — все
      // ранее отрендеренные карточки seller_inbox / seller_queue должны
      // перестроиться, иначе юзер видит «вчерашний» список и пытается
      // отгрузить заказ, который уже в транзите.
      if (REFRESH_INBOX_TRIGGERS.has(action)) {
        refreshVisibleSellerInbox();
      }
    } catch(err) {
      if (typingDelay) clearTimeout(typingDelay);
      removeTyping();
      // Транзиентный сбой: обрыв канала ИЛИ 5xx/52x (Cloudflare не достучался до
      // origin / upstream-сбой). Действие при этом не выполнилось — показываем
      // понятное сообщение + «Повторить» вместо сырого «/api/... → 521».
      const _msg = (err && err.message) || '';
      const _net = /Failed to fetch|NetworkError|network|load failed|abort/i.test(_msg);
      const _status = (err && err.status) || 0;
      const _transient = _net || _status >= 500 || /→ 5\d\d$/.test(_msg);
      if (_transient) {
        addMessage('assistant',
          '⚠️ Соединение прервалось — нажмите «Повторить».',
          [], [{action: action, params: params, label: '🔄 Повторить'}]);
      } else {
        addMessage('assistant', '⚠️ ' + err.message);
      }
    } finally {
      _inflightActions.delete(key);
      _markBusy(srcEl, false);
    }
  };

  // Добавить «← Назад» и «🏠 Главная» в contextual_actions если нет своей навигации.
  // Helper для quickAction и WS-handler. Кнопки всегда в конце каждого ответа.
  function ensureHomeNav(ctxActs) {
    ctxActs = Array.isArray(ctxActs) ? [...ctxActs] : [];
    const hasBack = ctxActs.some(a =>
      a.action === 'go_back'
      || (a.label || '').includes('Назад')
      || (a.label || '').startsWith('←')
    );
    const hasHome = ctxActs.some(a =>
      a.action === 'go_home'
      || (a.label || '').includes('Главная')
      || (a.label || '').startsWith('🏠')
    );
    if (!hasBack) ctxActs.push({action: 'go_back', label: '← Назад'});
    if (!hasHome) ctxActs.push({action: 'go_home', label: '🏠 Главная'});
    return ctxActs;
  }

  // Дефолтные подсказки, если backend не вернул свои — чтобы блок suggestions
  // никогда не был пустым в конце ответа.
  function ensureSuggestions(sugs) {
    if (Array.isArray(sugs) && sugs.length) return sugs;
    const role = String(window.__pillRole || 'buyer');
    // Подсказки по бизнес-логике роли (прямые action'ы — не зависят от ИИ-роутинга).
    if (role === 'seller') {
      // Уникальные AI-инсайты, которых НЕТ в основном меню: рыночный спрос,
      // рейтинг, скорость по SLA. Помогают зарабатывать больше и расти.
      return [{label: '📊 Спрос на рынке', action: 'get_demand_report', params: {}},
              {label: '⭐ Мой рейтинг', action: 'seller_rating', params: {}},
              {label: '⚡ Моя скорость', action: 'get_sla_report', params: {}}];
    }
    // Оператор (вкл. суб-роли operator_logist/customs/payment/manager):
    // оркестратор сделок — очередь, SLA, рекламации, аналитика. RFQ поднимает
    // покупатель, поэтому «Создать RFQ» оператору здесь не предлагаем.
    if (/^operator/.test(role)) {
      return [{label: 'Очередь сделок', action: 'op_queue',         params: {}},
              {label: 'Нарушения SLA',  action: 'op_sla_breach',    params: {}},
              {label: 'Рекламации',     action: 'get_claims',       params: {}},
              {label: 'Аналитика',      action: 'op_analytics_hub', params: {}}];
    }
    // Покупатель: заказы → создать RFQ → аналитика → поставщики.
    return [{label: 'Покажи мои заказы', action: 'get_orders', params: {}},
            'Создать RFQ', 'Аналитика за месяц', 'Список поставщиков'];
  }
  // Special action handler: go_home — без round-trip к серверу
  // + reload_page (после регистрации/логина в чате — перезагрузка).
  // + open_url (универсальный переход).
  const _origQuickAction = window.quickAction;
  window.quickAction = (action, params) => {
    if (action === 'go_home') { goHome(); return; }
    if (action === 'go_back') {
      // Если мы внутри подстраницы чата (/chat/project/<id>, /chat/rfq/<id>
      // и т.п.) — уводим на главную /chat/. Иначе уже на /chat/ — просто
      // скроллим наверх / показываем home. НЕ дёргаем history.back() —
      // он непредсказуем (прыгает на любую посещённую страницу).
      const p = window.location.pathname;
      if (p !== '/chat/' && p !== '/chat') {
        window.location.href = '/chat/';
      } else {
        goHome();
      }
      return;
    }
    if (action === 'reload_page') { window.location.reload(); return; }
    if (action === 'open_url') {
      // Бэкенд шлёт ссылку как `_url` (доминирующая конвенция), часть мест — `url`.
      // Принимаем оба, иначе переход молча не срабатывает («не открывается»).
      const url = params && (params._url || params.url);
      if (url) window.location.href = url;
      return;
    }
    return _origQuickAction(action, params);
  };

  // Auto-trigger action from URL: /chat/?action=start_registration&role=seller
  // Удобно для CTA с лендинга: "Стать поставщиком" → сразу форма регистрации в чате.
  (function autoTriggerFromUrl() {
    const p = new URLSearchParams(window.location.search);
    // Ссылка-приглашение в команду компании: /chat/?join_team=<token>
    const joinTeam = p.get('join_team');
    if (joinTeam) {
      history.replaceState(null, '', window.location.pathname);
      setTimeout(() => {
        try { window.quickAction('accept_team_invite', { token: joinTeam }); }
        catch (e) { console.warn('join_team trigger failed', e); }
      }, 500);
      return;
    }
    // Ссылка-приглашение заказчика: /chat/?invite_customer=<token>
    const inviteCustomer = p.get('invite_customer');
    if (inviteCustomer) {
      history.replaceState(null, '', window.location.pathname);
      setTimeout(() => {
        try { window.quickAction('accept_customer_invite', { token: inviteCustomer }); }
        catch (e) { console.warn('invite_customer trigger failed', e); }
      }, 500);
      return;
    }
    // Реферальная ссылка KAM: /chat/?ref=<signed>. Сохраняем токен, чтобы
    // применить после регистрации, и пробуем привязать сразу.
    const ref = p.get('ref');
    if (ref) {
      try { localStorage.setItem('kamRef', ref); } catch (e) {}
      history.replaceState(null, '', window.location.pathname);
      setTimeout(() => {
        try { window.quickAction('accept_referral', { code: ref }); }
        catch (e) { console.warn('ref trigger failed', e); }
      }, 500);
      return;
    }
    // Отложенный реферал: пользователь зарегистрировался → применяем один раз.
    try {
      const pendingRef = localStorage.getItem('kamRef');
      if (pendingRef && window.IS_AUTHENTICATED) {
        localStorage.removeItem('kamRef');
        setTimeout(() => {
          try { window.quickAction('accept_referral', { code: pendingRef }); }
          catch (e) { console.warn('pending ref failed', e); }
        }, 700);
      }
    } catch (e) {}
    // Поиск с лендинга: /chat/?q=<парт-номера> → заполнить hero и запустить поиск.
    const q = p.get('q');
    if (q && q.trim()) {
      history.replaceState(null, '', window.location.pathname);
      setTimeout(() => {
        try {
          const inp = $('heroInput');
          if (inp) { inp.value = q; if (typeof updateHeroIcon === 'function') updateHeroIcon(); }
          send(true);
        } catch (e) { console.warn('q search trigger failed', e); }
      }, 450);
      return;
    }
    const a = p.get('action');
    if (!a) return;
    // Очищаем query чтобы при перезагрузке action не запускался повторно.
    const cleanUrl = window.location.pathname;
    history.replaceState(null, '', cleanUrl);
    // Передаём все остальные query-params как params экшена
    // (period, project, role, и т.д. — для get_analytics, KPI-карточек и пр.)
    const params = {};
    for (const [k, v] of p.entries()) {
      if (k === 'action' || k === 'conv') continue;
      const n = parseInt(v, 10);
      params[k] = (String(n) === v && !isNaN(n)) ? n : v;
    }
    // Подождём пока инициализируется UI и quickAction
    setTimeout(() => {
      try { window.quickAction(a, params); } catch (e) { console.warn('auto-trigger failed', e); }
    }, 400);
  })();

  // Перехват «assistant ответил с _post_action=reload» — для случая, когда
  // фронт получил это поле в data, но action рендерится как кнопка.
  // (Не обязательно — кнопка reload_page уже работает, оставлено на будущее.)

  // Enter в текстовом поле формы = подтвердить (нажать .fm-submit). Работает
  // во ВСЕХ формах (.fm-card): создание папки, приглашение, любые input'ы.
  // textarea не трогаем (Enter = перенос строки), Shift+Enter и IME-композицию
  // (китайский/японский ввод) пропускаем.
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
    const inp = e.target;
    if (!inp || !inp.classList || !inp.classList.contains('fm-input')) return;
    if (inp.tagName === 'TEXTAREA' || inp.tagName === 'SELECT') return;
    const card = inp.closest('.fm-card');
    const submit = card && card.querySelector('.fm-submit');
    if (!submit || submit.disabled) return;
    e.preventDefault();
    submit.click();
  });

  // ── Карточка ссылки с копированием (type=copy_link) ──
  function _ensureCopyLinkCSS() {
    if (document.getElementById('cl-css')) return;
    const s = document.createElement('style');
    s.id = 'cl-css';
    s.textContent =
      '.cl-card{display:flex;flex-direction:column;gap:8px}'
      + '.cl-title{font-weight:700;font-size:14px}'
      + '.cl-box{position:relative}'
      + '.cl-url{display:block;padding:11px 46px 11px 13px;'
      +   'border-radius:10px;background:rgba(128,128,128,.12);border:1px solid rgba(128,128,128,.22);'
      +   'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;line-height:1.3;'
      +   'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;user-select:all;cursor:text}'
      + '.cl-copy{position:absolute;top:50%;right:6px;transform:translateY(-50%);'
      +   'width:32px;height:32px;display:inline-flex;align-items:center;justify-content:center;'
      +   'border:none;border-radius:8px;background:transparent;color:inherit;opacity:.55;'
      +   'cursor:pointer;transition:opacity .15s,background .15s}'
      + '.cl-copy:hover{opacity:1;background:rgba(128,128,128,.2)}'
      + '.cl-copy:active{transform:translateY(-50%) scale(.9)}'
      + '.cl-copy svg{width:16px;height:16px;pointer-events:none}'
      + '.cl-ic-copy{display:block}'
      + '.cl-ic-done{display:none;color:#2e9e5b}'
      + '.cl-copy.cl-copied{opacity:1}'
      + '.cl-copy.cl-copied .cl-ic-copy{display:none}'
      + '.cl-copy.cl-copied .cl-ic-done{display:block}'
      + '.cl-hint{font-size:12px;opacity:.6;line-height:1.35}';
    document.head.appendChild(s);
  }

  // ── Карточка чертежей (type=drawings): inline-папки + drag-n-drop ──
  function _ensureDrawingsCSS() {
    if (document.getElementById('dw-css')) return;
    const s = document.createElement('style');
    s.id = 'dw-css';
    s.textContent =
      '.dw-card{display:flex;flex-direction:column;gap:8px}'
      + '.dw-newrow{display:flex;gap:6px}'
      + '.dw-newinput{flex:1;padding:8px 10px;border-radius:8px;border:1px solid rgba(0,0,0,.14);font:inherit;background:#fff}'
      + '.dw-newbtn{padding:8px 14px;border-radius:8px;border:none;background:#1a1a1a;color:#fff;font:inherit;cursor:pointer;white-space:nowrap}'
      + '.dw-newbtn:hover{opacity:.9}'
      + '.dw-sec{font-size:11px;font-weight:700;opacity:.5;margin:10px 2px 2px;text-transform:uppercase;letter-spacing:.05em}'
      + '.dw-folders{display:flex;flex-direction:column;gap:6px;margin-top:2px}'
      + '.dw-folder{border-radius:10px;border:1px dashed rgba(0,0,0,.2);background:rgba(0,0,0,.02);transition:.12s;overflow:hidden}'
      + '.dw-folder.dw-over{border-color:#2e7d32;background:rgba(46,125,50,.14);border-style:solid}'
      + '.dw-fhead{display:flex;align-items:center;gap:8px;padding:10px 12px;cursor:pointer}'
      + '.dw-fhead:hover{background:rgba(0,0,0,.04)}'
      + '.dw-fchev{font-size:11px;opacity:.55;transition:.15s}'
      + '.dw-folder.dw-open .dw-fchev{transform:rotate(90deg)}'
      + '.dw-fname{font-weight:600;flex:1}'
      + '.dw-fcount{font-size:12px;opacity:.7;background:rgba(0,0,0,.08);border-radius:10px;padding:1px 8px;min-width:18px;text-align:center}'
      + '.dw-folder-proj{border-style:solid;border-color:rgba(100,120,200,.35);background:rgba(100,120,200,.05)}'
      + '.dw-projtag{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:#5b6cc0;background:rgba(100,120,200,.16);border-radius:6px;padding:1px 6px;margin-left:6px;vertical-align:middle}'
      + '.dw-fbody{display:none;flex-direction:column;gap:6px;padding:0 8px 8px}'
      + '.dw-folder.dw-open .dw-fbody{display:flex}'
      + '.dw-fempty{padding:6px 4px}'
      + '.dw-nofolder{border:1px dashed transparent;border-radius:8px;transition:.12s}'
      + '.dw-nofolder.dw-over{border-color:#2e7d32;background:rgba(46,125,50,.12)}'
      + '.dw-items{display:flex;flex-direction:column;gap:6px;margin-top:4px}'
      + '.dw-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:10px;border:1px solid rgba(0,0,0,.08);background:#fff;cursor:pointer}'
      + '.dw-item:hover{background:rgba(0,0,0,.03)}'
      + '.dw-item.dw-dragging{opacity:.4}'
      + '.dw-grip{cursor:grab;opacity:.4;font-size:16px;user-select:none}'
      + '.dw-ibody{display:flex;flex-direction:column;min-width:0;flex:1}'
      + '.dw-ititle{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
      + '.dw-isub{font-size:12px;opacity:.65}'
      + '.dw-empty{opacity:.6;padding:8px 2px}'
      + '.dw-link-btn{cursor:pointer;opacity:.5;font-size:15px;padding:2px 7px;border-radius:6px;align-self:center}'
      + '.dw-link-btn:hover{opacity:1;background:rgba(0,0,0,.07)}'
      + '.dw-row{display:flex;flex-direction:column;border-radius:10px;overflow:hidden}'
      + '.dw-row.dw-open{box-shadow:0 0 0 1px rgba(46,125,50,.4)}'
      + '.dw-linkpanel{display:none;flex-direction:column;gap:6px;padding:8px 10px 10px;background:rgba(46,125,50,.05)}'
      + '.dw-row.dw-open .dw-linkpanel{display:flex}'
      + '.dw-linkinput{padding:7px 10px;border-radius:8px;border:1px solid rgba(0,0,0,.14);font:inherit;background:#fff}'
      + '.dw-results{display:flex;flex-direction:column;gap:5px;margin-top:6px;max-height:340px;overflow:auto}'
      + '.dw-result{display:flex;flex-direction:column;padding:8px 10px;border-radius:8px;border:1px solid rgba(0,0,0,.08);background:#fff;cursor:pointer}'
      + '.dw-result:hover{background:rgba(46,125,50,.08);border-color:#2e7d32}'
      + '.dw-rtitle{font-weight:600}'
      + '.dw-rsub{font-size:12px;opacity:.6}'
      + '.opd-card{gap:12px;padding:16px 18px 18px}'
      + '.opd-input{padding:12px 14px;font-size:15px;border-radius:10px}'
      + '.opd-results{display:flex;flex-direction:column;gap:14px;margin-top:4px}'
      + '.opd-results .dw-empty{padding:6px 2px 4px}'
      + '.opd-group{display:flex;flex-direction:column;gap:6px}'
      + '.opd-gtitle{font-size:12px;font-weight:700;opacity:.6;text-transform:uppercase;letter-spacing:.03em}'
      // ── Тёмная тема: исходный блок весь на светлых цветах (#fff / rgba(0,0,0)),
      //    из-за чего светлый текст оказывался на белом фоне (нечитаемо). Инвертируем.
      + 'body.dark-mode .dw-item,body.dark-mode .dw-result{background:rgba(255,255,255,.05);border-color:rgba(255,255,255,.1)}'
      + 'body.dark-mode .dw-item:hover{background:rgba(255,255,255,.09)}'
      + 'body.dark-mode .dw-result:hover{background:rgba(46,125,50,.22);border-color:#4caf50}'
      + 'body.dark-mode .dw-newinput,body.dark-mode .dw-linkinput{background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.14);color:#e8e8e8}'
      + 'body.dark-mode .dw-newbtn{background:rgba(255,255,255,.16);color:#fff}'
      + 'body.dark-mode .dw-folder{border-color:rgba(255,255,255,.18);background:rgba(255,255,255,.03)}'
      + 'body.dark-mode .dw-fhead:hover{background:rgba(255,255,255,.06)}'
      + 'body.dark-mode .dw-fcount{background:rgba(255,255,255,.12)}'
      + 'body.dark-mode .dw-link-btn:hover{background:rgba(255,255,255,.12)}'
      + 'body.dark-mode .dw-projtag{background:rgba(120,140,220,.28);color:#c5cdf5}'
      + 'body.dark-mode .dw-folder-proj{border-color:rgba(140,160,230,.4);background:rgba(120,140,220,.08)}';
    document.head.appendChild(s);
  }

  let _dwDragId = null;
  async function _refreshDrawings(dwCard, action, params) {
    try {
      const r = await api('/api/assistant/action/', {
        method: 'POST',
        body: JSON.stringify({conversation_id: state.convId, action, params}),
      });
      const card = (r.cards || []).find(c => c.type === 'drawings');
      if (card && dwCard && dwCard.isConnected) {
        const tmp = document.createElement('div');
        tmp.innerHTML = renderers.drawings(card.data || {});
        const fresh = tmp.firstElementChild;
        if (fresh) dwCard.replaceWith(fresh);
      }
    } catch (e) { /* оставляем старую карточку */ }
  }
  function _dwCreateFolder(card, inp) {
    const name = ((inp && inp.value) || '').trim();
    if (!name) { if (inp) inp.focus(); return; }
    _refreshDrawings(card, 'create_drawing_folder', {name});
  }

  // Инлайн-поиск позиции под чертежом (без перехода на новую карточку):
  // рендерит результаты прямо в .dw-results панели этого чертежа.
  let _dwInlineTimer = null;
  window.__dwLinkInline = function(inp, immediate) {
    if (!inp) return;
    const row = inp.closest('.dw-row');
    const panel = inp.closest('.dw-linkpanel');
    if (!row || !panel) return;
    const did = row.dataset.drawingId;
    const q = (inp.value || '').trim();
    clearTimeout(_dwInlineTimer);
    const run = function() {
      api('/api/assistant/action/', {
        method: 'POST',
        body: JSON.stringify({conversation_id: state.convId, action: 'link_drawing', params: {drawing_id: did, q}}),
      }).then(function(resp) {
        if ((inp.value || '').trim() !== q) return; // гонка
        const c = (resp.cards || []).find(x => x.type === 'drawing_link');
        const resEl = panel.querySelector('.dw-results');
        if (!resEl) return;
        const rows = (c && c.data && c.data.rows) || [];
        if (!c || !c.data.searched) { resEl.innerHTML = '<div class="dw-empty">Введите артикул или название</div>'; return; }
        if (!rows.length) { resEl.innerHTML = '<div class="dw-empty">Ничего не найдено</div>'; return; }
        resEl.innerHTML = rows.map(function(r) {
          const oem = String((r.params || {}).oem || '');
          return '<div class="dw-result" data-oem="' + esc(oem) + '" data-drawing-id="' + esc(String(did)) +
                 '"><span class="dw-rtitle">' + esc(r.title || '') + '</span>' +
                 (r.subtitle ? '<span class="dw-rsub">' + esc(r.subtitle) + '</span>' : '') + '</div>';
        }).join('');
      }).catch(function() {});
    };
    if (immediate) run(); else _dwInlineTimer = setTimeout(run, 350);
  };

  // Умный поиск позиции для привязки чертежа: live-swap .dw-results.
  let _dwLinkTimer = null;
  window.__drawingLinkSearch = function(inp, drawingId, immediate) {
    if (!inp) return;
    const card = inp.closest('.dw-link');
    if (!card) return;
    const q = (inp.value || '').trim();
    clearTimeout(_dwLinkTimer);
    const run = function() {
      api('/api/assistant/action/', {
        method: 'POST',
        body: JSON.stringify({conversation_id: state.convId, action: 'link_drawing', params: {drawing_id: drawingId, q}}),
      }).then(function(resp) {
        if ((inp.value || '').trim() !== q) return; // гонка
        const c = (resp.cards || []).find(x => x.type === 'drawing_link');
        const resEl = card.querySelector('.dw-results');
        if (!resEl || !c) return;
        const tmp = document.createElement('div');
        tmp.innerHTML = renderers.drawing_link(c.data || {});
        const nr = tmp.querySelector('.dw-results');
        resEl.innerHTML = nr ? nr.innerHTML : '';
      }).catch(function() {});
    };
    if (immediate) run(); else _dwLinkTimer = setTimeout(run, 350);
  };

  // Операторский live-поиск чертежей по артикулу: swap .opd-results.
  let _opdTimer = null;
  window.__opDrawingsSearch = function(inp, immediate) {
    if (!inp) return;
    const card = inp.closest('.opd-card');
    if (!card) return;
    const q = (inp.value || '').trim();
    clearTimeout(_opdTimer);
    const run = function() {
      api('/api/assistant/action/', {
        method: 'POST',
        body: JSON.stringify({conversation_id: state.convId, action: 'op_drawings_by_part', params: {q}}),
      }).then(function(resp) {
        if ((inp.value || '').trim() !== q) return;
        const c = (resp.cards || []).find(x => x.type === 'op_drawings');
        const res = card.querySelector('.opd-results');
        if (!res || !c) return;
        const tmp = document.createElement('div');
        tmp.innerHTML = renderers.op_drawings(c.data || {});
        const nr = tmp.querySelector('.opd-results');
        res.innerHTML = nr ? nr.innerHTML : '';
      }).catch(function() {});
    };
    if (immediate) run(); else _opdTimer = setTimeout(run, 300);
  };

  document.addEventListener('dragstart', (e) => {
    const item = e.target.closest && e.target.closest('.dw-item');
    if (!item) return;
    _dwDragId = item.dataset.drawingId;
    item.classList.add('dw-dragging');
    try { e.dataTransfer.setData('text/plain', _dwDragId); e.dataTransfer.effectAllowed = 'move'; } catch (_) {}
  });
  const _DW_DROP = '.dw-folder, .dw-nofolder';
  document.addEventListener('dragend', (e) => {
    const item = e.target.closest && e.target.closest('.dw-item');
    if (item) item.classList.remove('dw-dragging');
    document.querySelectorAll('.dw-over').forEach(el => el.classList.remove('dw-over'));
    _dwDragId = null;
  });
  document.addEventListener('dragover', (e) => {
    const tgt = e.target.closest && e.target.closest(_DW_DROP);
    if (!tgt || !_dwDragId || tgt.dataset.project) return; // папки проектов — read-only
    e.preventDefault();
    try { e.dataTransfer.dropEffect = 'move'; } catch (_) {}
    tgt.classList.add('dw-over');
  });
  document.addEventListener('dragleave', (e) => {
    const tgt = e.target.closest && e.target.closest(_DW_DROP);
    if (tgt && !tgt.contains(e.relatedTarget)) tgt.classList.remove('dw-over');
  });
  document.addEventListener('drop', (e) => {
    const tgt = e.target.closest && e.target.closest(_DW_DROP);
    if (!tgt || tgt.dataset.project) return; // в папку проекта не кладём вручную
    e.preventDefault();
    tgt.classList.remove('dw-over');
    let did = _dwDragId;
    try { did = did || e.dataTransfer.getData('text/plain'); } catch (_) {}
    _dwDragId = null;
    if (!did) return;
    // data-folder-id="" у зоны «Без папки» → снять из папки
    _refreshDrawings(tgt.closest('.dw-card'), 'move_drawing', {drawing_id: did, folder_id: tgt.dataset.folderId || ''});
  });
  // Разворачивание/сворачивание папки по клику на заголовок
  document.addEventListener('click', (e) => {
    const head = e.target.closest && e.target.closest('.dw-fhead');
    if (!head) return;
    const folder = head.closest('.dw-folder');
    if (folder) folder.classList.toggle('dw-open');
  });
  // Клик по чертежу → открыть файл (но не по кнопке 🔗, и drag не вызывает click)
  document.addEventListener('click', (e) => {
    if (e.target.closest && e.target.closest('.dw-link-btn')) return;
    const item = e.target.closest && e.target.closest('.dw-item');
    if (!item) return;
    const url = item.dataset.url;
    if (url) window.open(url, '_blank', 'noopener');
  });
  // 🔗 → раскрыть/скрыть инлайн-поиск позиции под чертежом (без новой карточки)
  document.addEventListener('click', (e) => {
    const lb = e.target.closest && e.target.closest('.dw-link-btn');
    if (!lb) return;
    const row = lb.closest('.dw-row');
    if (!row) return;
    const open = row.classList.toggle('dw-open');
    if (open) { const inp = row.querySelector('.dw-linkinput'); if (inp) setTimeout(() => inp.focus(), 0); }
  });
  // Выбор позиции в инлайн-поиске → привязать и обновить карточку НА МЕСТЕ
  document.addEventListener('click', (e) => {
    const res = e.target.closest && e.target.closest('.dw-result');
    if (!res || res.dataset.action) return; // старые drawing_link-карточки — общий обработчик
    const card = res.closest('.dw-card');
    if (!card) return;
    _refreshDrawings(card, 'bind_drawing', {drawing_id: res.dataset.drawingId, oem: res.dataset.oem || ''});
  });
  // Inline-создание папки: кнопка «Создать» или Enter в поле
  document.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('.dw-newbtn');
    if (!btn) return;
    const card = btn.closest('.dw-card');
    _dwCreateFolder(card, card && card.querySelector('.dw-newinput'));
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.isComposing) return;
    const inp = e.target;
    if (!inp || !inp.classList || !inp.classList.contains('dw-newinput')) return;
    e.preventDefault();
    _dwCreateFolder(inp.closest('.dw-card'), inp);
  });

  // Click handler for action buttons inside messages
  document.addEventListener('click', async (e) => {
    // 1. Submit-кнопка inline-формы (карточка type=form)
    const submit = e.target.closest('.fm-submit');
    if (submit) {
      const card = submit.closest('.fm-card');
      if (!card) return;
      const action = card.dataset.formAction;
      const fixed = JSON.parse(card.dataset.fixed || '{}');
      const params = {...fixed};
      card.querySelectorAll('.fm-input').forEach(inp => {
        if (inp.required && !inp.value.trim()) inp.classList.add('fm-error');
        else inp.classList.remove('fm-error');
        if (inp.value.trim()) params[inp.name] = inp.value.trim();
      });
      const missing = card.querySelectorAll('.fm-input.fm-error').length;
      if (missing) return;
      // Запоминаем оригинальный текст, чтобы вернуть кнопку в рабочее
      // состояние после ответа сервера (раньше кнопка зависала в '…'
      // если сервер вернул ошибку валидации — юзер не мог повторить).
      const originalText = submit.textContent;
      submit.disabled = true;
      submit.textContent = '…';
      params._label = card.querySelector('.card-title')?.textContent || action;
      try {
        await quickAction(action, params);
      } finally {
        // Карточка могла быть заменена новой (например той же формой
        // с per-field errors) — тогда submit уже не в DOM и трогать не надо.
        if (submit.isConnected) {
          submit.disabled = false;
          submit.textContent = originalText;
        }
      }
      return;
    }
    // 2a. Copy-URL кнопка (для случая когда preview блочит external link)
    const copyBtn = e.target.closest('.im-copy-btn[data-copy-url]');
    if (copyBtn) {
      e.preventDefault();
      const url = copyBtn.dataset.copyUrl;
      const orig = copyBtn.textContent;
      navigator.clipboard?.writeText(url).then(() => {
        copyBtn.textContent = '✓';
        setTimeout(() => { copyBtn.textContent = orig; }, 1500);
      }).catch(() => {
        window.prompt(tr('prompt.copy_link'), url);
      });
      return;
    }
    // 2. Обычные action-кнопки и любой [data-action] (например, ls-row)
    const btn = e.target.closest('.act-btn,[data-action]');
    if (!btn) return;
    // Debounce — защита от двойного клика и дубля event-listener'ов.
    // Без этого «Трекинг» / любая action-кнопка могла выстрелить 2 раза.
    if (btn.dataset._busy === '1') return;
    btn.dataset._busy = '1';
    setTimeout(() => { delete btn.dataset._busy; }, 800);
    const action = btn.dataset.action;
    const params = JSON.parse(btn.dataset.params || '{}');
    params._label = btn.dataset.label;
    quickAction(action, params);
  });

  // ══════════════════════════════════════════════════════════
  // Sidebar conversations + projects
  // ══════════════════════════════════════════════════════════
  const DOT_BG = {
    green:'#22c55e', orange:'#f97316', blue:'#3b82f6',
    purple:'#a855f7', red:'#ef4444', gray:'#9ca3af',
  };

  async function loadConvList(attempt = 0) {
    // 4 состояния списка: loading / empty / error / loaded.
    // Раньше: пустая ловля catch — юзер вообще не понимал что произошло
    // (тихая ошибка вместо «нет связи»).
    const wrap = document.getElementById('convList');
    if (wrap && (!state.convs || !state.convs.length)) {
      // Loading skeleton (3 серых полоски). Показываем только если списка
      // ещё нет (не моргаем при перерисовке после ответа сервера).
      wrap.innerHTML =
        '<div class="conv-skel"></div>'.repeat(3);
    }
    try {
      // cache:'no-store' + keepalive=false — не переиспользуем «подвисшее»
      // keep-alive соединение dev-сервера (источник редких Failed to fetch).
      const r = await fetch('/api/assistant/conversations/', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      state.convs = data.results || data;
      renderConvList();
    } catch (err) {
      // Транзиентный сбой соединения: на dev-сервере (single-worker Daphne)
      // параллельные HTTP + WS-апгрейд иногда роняют одно соединение
      // («Failed to fetch»); на проде — флапающая сеть. Авто-ретрай с
      // бэкоффом, скелетоны остаются. Только потом — UI ошибки с «Повторить».
      if (attempt < 4) {
        setTimeout(() => loadConvList(attempt + 1), 300 * (attempt + 1));
        return;
      }
      console.warn('loadConvList failed', err);
      if (wrap) {
        wrap.innerHTML =
          '<div class="conv-error">⚠️ Не удалось загрузить список. '
          + '<button type="button" class="conv-retry">Повторить</button></div>';
        const btn = wrap.querySelector('.conv-retry');
        if (btn) btn.addEventListener('click', () => loadConvList());
      }
    }
  }

  // ── Role toggle (Покупатель / Поставщик / Оператор) ───────
  const ROLE_TABS = ['buyer', 'seller', 'operator'];

  function paintRoleToggle(activeRole) {
    document.querySelectorAll('#roleToggle .role-tab').forEach(b => {
      b.classList.toggle('active', b.dataset.role === activeRole);
    });
  }

  // PIVOT 2026-05-27: role-toggle = смена аккаунта через chat-форму с паролем.
  // Каждая роль = отдельный аккаунт, переключение = логин с паролем.
  async function setRole(newRole) {
    try { setConvId(null); } catch (_) {}
    try { showWelcome(); } catch (_) {}
    _abandonStream();   // бросаем in-flight стрим: иначе onclose даст ложный recovery
    if (state.ws) { try { state.ws.close(); } catch(e){} }
    try {
      await window.quickAction('switch_role_login', {role: newRole});
    } catch (err) {
      console.warn('switch_role_login failed', err);
      alert('Не удалось переключить роль: ' + (err && err.message ? err.message : err));
    }
  }

  document.addEventListener('click', (e) => {
    const tab = e.target.closest('#roleToggle .role-tab');
    if (!tab) return;
    const newRole = tab.dataset.role;
    if (!ROLE_TABS.includes(newRole)) return;
    // ── Анонимный режим: продавец / оператор — отдельные сущности,
    // отдельные кабинеты, отдельные входы. На каждый клик чистим
    // историю чата (новый «conversation»), чтобы flows не наслаивались.
    if (!window.IS_AUTHENTICATED) {
      paintRoleToggle(newRole);
      try { window.newChat(); } catch (_) {}
      // Дополнительно очищаем поток, т.к. quickAction добавляет в конец.
      try {
        const stream = document.getElementById('streamInner');
        if (stream) stream.innerHTML = '';
      } catch (_) {}
      if (newRole === 'seller') {
        // Поставщик — регистрация (форма + KYB после)
        setTimeout(() => { try { window.quickAction('start_registration', {role: 'seller'}); }
          catch (err) { console.warn('seller flow failed', err); } }, 50);
        return;
      }
      if (newRole === 'operator') {
        // Оператор — только вход, регистрации нет (заводит админ)
        setTimeout(() => { try { window.quickAction('start_login', {role: 'operator'}); }
          catch (err) { console.warn('operator flow failed', err); } }, 50);
        return;
      }
      // Покупатель — welcome с pills уже отрисован newChat()
      return;
    }
    if (state.config && state.config.role === newRole) return;
    setRole(newRole);
  });

  // Welcome screen + quick-pills адаптивны под роль.
  // Каждый pill хранит translation-key (`tkey`) + emoji, лейбл вычисляется
  // в applyRoleWelcome() через window.t() под текущим языком.
  const ROLE_WELCOME = {
    buyer: {
      titleKey: 'welcome.buyer.title',
      subKey:   'welcome.buyer.subtitle',
      pills: [
        {tkey:'pill.my_orders',     emoji:'📦', action:'get_my_deals',       params:{}},
        {tkey:'pill.deposit',       emoji:'💰', action:'get_balance',        params:{}},
        {tkey:'pill.auto_discount', emoji:'🎯', action:'get_buyer_discount', params:{}},
        {tkey:'pill.support',       emoji:'🎧', action:'support_home',       params:{}},
      ],
    },
    // Гость (аноним): исходные 2 пилюли главной — «Мои заказы» и «Открытые RFQ».
    // Совпадают со статичным fallback в index.html (no-JS) — нет «прыжка» состава
    // при загрузке. Клик у анонима ведёт на логин (заказов/RFQ ещё нет).
    guest: {
      titleKey: 'welcome.h1',
      subKey:   'welcome.buyer.subtitle',
      pills: [
        {tkey: 'pill.my_orders', sub: 'купил, едет',   emoji: '📦', action: 'get_orders',     params: {}},
        {tkey: 'pill.open_rfq',  sub: 'жду котировок', emoji: '📋', action: 'get_rfq_status', params: {}},
      ],
    },
    seller: {
      titleKey: 'welcome.seller.title',
      subKey:   'welcome.seller.subtitle',
      pills: [
        // Главная пилюля — единый inbox (вкл. RFQ, отгрузки, подтверждения, SLA).
        // Дубли «🚚 К отгрузке» и «📋 Новые RFQ» убраны — клик на секцию внутри
        // 🔥 Срочного раскрывает полный список соответствующей категории.
        // «🔥 Срочное» объединено в «📋 Мои сделки» (лиды + воронка на одном экране).
        {tkey:'pill.my_deals',         emoji:'📋', action:'get_my_deals',      params:{}},
        {tkey:'pill.upload_price',     emoji:'📤', action:'upload_pricelist',  params:{}},
        {tkey:'pill.my_products',      emoji:'📦', action:'seller_warehouses', params:{}},
        {tkey:'pill.drawings',         emoji:'📐', action:'seller_drawings',   params:{}},
        {tkey:'pill.deposit',          emoji:'💰', action:'get_balance',       params:{}},
        {tkey:'pill.verification',     emoji:'🛡', action:'start_onboarding',  params:{}},
        {tkey:'pill.analytics',        emoji:'📊', action:'seller_analytics_hub', params:{}},
        {tkey:'pill.support', emoji:'🎧', action:'support_home',  params:{}},
      ],
    },
    operator: {
      titleKey: 'welcome.operator.title',
      subKey:   'welcome.operator.subtitle',
      pills: [
        {tkey:'pill.overview',         emoji:'🎛', action:'op_dashboard',          params:{}},
        // Единая очередь сделок (RFQ + Order). RFQ-специфичная очередь
        // (op_rfq_queue) убрана — это была дублирующая навигация.
        {tkey:'pill.queue',            emoji:'📋', action:'op_queue',              params:{}},
        {tkey:'pill.payments_escrow',  emoji:'💰', action:'op_payments_dashboard', params:{}},
        {tkey:'pill.customs',          emoji:'🛂', action:'op_customs_dashboard',  params:{}},
        {tkey:'pill.logistics',        emoji:'🚚', action:'op_logistics_stats',    params:{}},
        {tkey:'pill.my_suppliers',     emoji:'🏭', action:'op_my_suppliers',       params:{}},
        {tkey:'pill.kyb_suppliers',    emoji:'🛡', action:'op_kyb_queue',          params:{}},
        {tkey:'pill.claims',           emoji:'🧾', action:'get_claims',            params:{}},
        {tkey:'pill.my_user_chats',    emoji:'📂', action:'op_my_user_chats',     params:{}},
        {tkey:'pill.drawings', emoji:'📐', action:'op_drawings_by_part',   params:{}},
        {tkey:'pill.analytics',        emoji:'📊', action:'op_analytics_hub',     params:{}},
        // «Поддержка» убрана: оператор САМ — поддержка, ему не нужен контакт в саппорт.
      ],
    },
    // Суб-роли оператора: УРЕЗАННЫЙ набор — только свой профиль + базовая навигация.
    // Полный доступ ко всем разделам — только у general `operator` (manager).
    operator_logist: {
      titleKey: 'welcome.operator_logist.title',
      subKey:   'welcome.operator_logist.subtitle',
      pills: [
        {tkey:'pill.logistics',  emoji:'🚚', action:'op_logistics_stats', params:{}},
        {tkey:'pill.overview',   emoji:'🎛', action:'op_dashboard',       params:{}},
        {tkey:'pill.queue',      emoji:'📋', action:'op_queue',           params:{filter:'open'}},
      ],
    },
    operator_customs: {
      titleKey: 'welcome.operator_customs.title',
      subKey:   'welcome.operator_customs.subtitle',
      pills: [
        {tkey:'pill.customs_summary', emoji:'🛂', action:'op_customs_dashboard', params:{}},
        {tkey:'pill.hs_code',         emoji:'🔎', action:'op_hs_lookup',         params:{}},
        {tkey:'pill.sanctions',       emoji:'🚫', action:'op_sanctions_check',   params:{}},
        {tkey:'pill.at_customs',      emoji:'📋', action:'op_queue',             params:{filter:'open'}},
      ],
    },
    operator_payment: {
      titleKey: 'welcome.operator_payment.title',
      subKey:   'welcome.operator_payment.subtitle',
      pills: [
        {tkey:'pill.escrow',           emoji:'💰', action:'op_payments_dashboard', params:{}},
        {tkey:'pill.payments_stats',   emoji:'💳', action:'op_payments_stats',     params:{}},
        {tkey:'pill.awaiting_reserve', emoji:'⏳', action:'op_queue',              params:{filter:'awaiting_reserve'}},
        {tkey:'pill.refunds',          emoji:'💸', action:'op_queue',              params:{filter:'refund'}},
      ],
    },
    // KAM (operator_manager) = КОММЕРЧЕСКИЙ кабинет: аккаунты, спрос, выгода.
    // БЕЗ исполнения (эскроу/таможня/KYB/логистика/рекламации) — это зона оператора.
    operator_manager: {
      titleKey: 'welcome.operator_manager.title',
      subKey:   'welcome.operator_manager.subtitle',
      pills: [
        {tkey:'pill.customers',       emoji:'👥', action:'seller_customers',     params:{}},
        {tkey:'pill.my_deals',        emoji:'📋', action:'kam_deals',            params:{}},
        {tkey:'pill.accruals',        emoji:'💰', action:'my_accruals',          params:{}},
        {tkey:'pill.invite',          emoji:'📨', action:'invite_customer',      params:{}},
        {tkey:'pill.analytics',       emoji:'📊', action:'op_analytics_hub',      params:{}},
        {tkey:'pill.my_user_chats',   emoji:'📂', action:'op_my_user_chats',     params:{}},
        {tkey:'pill.support',         emoji:'🎧', action:'support_home',          params:{}},
      ],
    },
    admin: {
      titleKey: 'welcome.admin.title',
      subKey:   'welcome.admin.subtitle',
      pills: [
        {tkey:'pill.overview',       emoji:'🛡', action:'admin_dashboard',         params:{}},
        {tkey:'pill.market_twin',    emoji:'🌐', action:'admin_market_twin',       params:{}},
        {tkey:'pill.customs_data',   emoji:'🛂', action:'admin_customs',           params:{}},
        {tkey:'pill.gmv',            emoji:'📈', action:'admin_gmv',               params:{}},
        {tkey:'pill.users',          emoji:'👥', action:'admin_users',             params:{}},
        {tkey:'pill.moderation',     emoji:'🚨', action:'admin_moderation_queue',  params:{}},
        {tkey:'pill.catalog',        emoji:'📦', action:'admin_catalog_review',    params:{}},
        {tkey:'pill.settings_admin', emoji:'🛠', action:'admin_platform_settings', params:{}},
      ],
    },
  };

  // ============================================================
  //  ПИЛЮЛИ: кастомизация (закреп/откреп, мастер-меню, drag, своя пилюля)
  //  Хранение — localStorage per role (мгновенно, без сервера).
  // ============================================================
  const ROLE_RU = {
    buyer: 'Покупатель', seller: 'Продавец', operator: 'Оператор',
    operator_manager: 'KAM', operator_logist: 'Логист',
    operator_customs: 'Таможня', operator_payment: 'Платежи', admin: 'Администратор',
  };
  // v2: после объединения «🔥 Срочное» → «📋 Мои сделки» сбрасываем старый
  // сохранённый порядок пилюль (иначе «Мои сделки» уезжает в конец списка).
  const PILL_KEY = (role) => `pillPrefs:v3:${role}`;
  function loadPillPrefs(role) {
    try { return JSON.parse(localStorage.getItem(PILL_KEY(role))) || {}; } catch (e) { return {}; }
  }
  function savePillPrefs(role, prefs) {
    try { localStorage.setItem(PILL_KEY(role), JSON.stringify(prefs)); } catch (e) {}
  }
  function pillId(action, params) { return action + '#' + JSON.stringify(params || {}); }
  // Доступно ли действие роли (по allowlist с бэкенда). Нет данных → не фильтруем.
  function __pillAllowed(role, action) {
    const ra = window.__roleActions;
    if (!ra) return true;
    const allow = ra[role];
    if (!allow) return true;
    return allow.indexOf('*') >= 0 || allow.indexOf(action) >= 0;
  }
  function _normPill(b, srcRole) {
    return {
      id: pillId(b.action, b.params),
      emoji: b.emoji || '•',
      text: (b.label != null ? b.label : (b.tkey ? tr(b.tkey) : b.action)),
      sub: (b.sub != null ? b.sub : (b.subKey ? tr(b.subKey) : '')),
      action: b.action, params: b.params || {}, srcRole: srcRole || null,
    };
  }
  // Опциональные пилюли: не входят в стандартный набор, но доступны для закрепа
  // через «Все пилюли». Напр. «Мой менеджер» убран из дефолта покупателя,
  // но его можно вернуть (иначе он бы исчез из каталога совсем).
  const EXTRA_PILLS = [
    {tkey:'pill.my_kam', emoji:'👤', action:'my_kam', params:{}, _src:'buyer'},
  ];
  // Полный каталог пилюль по ВСЕМ кабинетам (дедуп по action+params).
  function globalPillCatalog() {
    const seen = {}, out = [];
    // 'guest' — псевдороль анонимной главной (Мои заказы/Открытые RFQ); её пилюли
    // НЕ должны протекать в каталог «Все пилюли» залогиненных (там источник 'guest'
    // непереведён + лишние дубликаты). Гостю мастер-меню всё равно не показывается.
    Object.keys(ROLE_WELCOME).filter(r => r !== 'guest').forEach(r => (ROLE_WELCOME[r].pills || []).forEach(b => {
      const p = _normPill(b, r);
      if (seen[p.id]) return; seen[p.id] = 1; out.push(p);
    }));
    EXTRA_PILLS.forEach(b => {
      const p = _normPill(b, b._src || null);
      if (seen[p.id]) return; seen[p.id] = 1; out.push(p);
    });
    return out;
  }
  // Каталог пилюль для роли = её дефолтные + пользовательские (custom).
  function rolePillCatalog(role) {
    const base = (ROLE_WELCOME[role] || ROLE_WELCOME.buyer).pills.map(b => _normPill(b, role));
    const custom = (loadPillPrefs(role).custom || []).map(c => _normPill(c, null));
    const seen = {}; const out = [];
    base.concat(custom).forEach(p => { if (!seen[p.id]) { seen[p.id] = 1; out.push(p); } });
    return out;
  }
  // Итоговое состояние: видимые (упорядоченные) + доступные для закрепа.
  function pillState(role) {
    const cat = rolePillCatalog(role);
    const global = globalPillCatalog();
    const prefs = loadPillPrefs(role);
    const hidden = new Set(prefs.hidden || []);
    const order = prefs.order || [];
    const byId = {}; cat.forEach(p => byId[p.id] = p);
    const visible = [], used = new Set();
    order.forEach(id => { if (byId[id] && !hidden.has(id)) { visible.push(byId[id]); used.add(id); } });
    cat.forEach(p => { if (!used.has(p.id) && !hidden.has(p.id)) { visible.push(p); used.add(p.id); } });
    const seen = new Set(visible.map(p => p.id)), avail = [];
    // В каталог «Все пилюли» — только то, что роль реально может выполнить
    // (по allowlist роли с бэкенда); иначе клик дал бы «нет прав».
    cat.concat(global).forEach(p => {
      if (!seen.has(p.id) && __pillAllowed(role, p.action)) { seen.add(p.id); avail.push(p); }
    });
    return { visible, avail, cat, global, prefs, byId };
  }

  function applyRoleWelcome(role) {
    _ensurePillCSS();
    // Гость (аноним) на дефолтном экране покупателя → урезанный guest-набор.
    // Вкладки «Продавец»/«Оператор» по-прежнему показывают превью своих кабинетов.
    if (!window.IS_AUTHENTICATED && (role === 'buyer' || !role)) role = 'guest';
    const cfg = ROLE_WELCOME[role] || ROLE_WELCOME.buyer;
    const t = $('welcomeTitle'), s = $('welcomeSubtitle'), p = $('welcomePills');
    if (t) t.textContent = tr(cfg.titleKey);
    if (s) s.innerHTML = tr(cfg.subKey);
    if (!p) return;
    window.__pillRole = role;
    // Гостевой экран: равные по ширине пилюли по центру, без «+» (анониму нечего настраивать).
    const isGuest = (role === 'guest');
    p.classList.toggle('wp-guest', isGuest);
    const pills = pillState(role).visible;
    const html = pills.map(b => {
      const label = `${b.emoji} ${b.text}`;
      const params = { ...(b.params || {}), _label: label };
      const subHtml = b.sub ? ` <span class="pill-sub">${esc(b.sub)}</span>` : '';
      // Бейдж «требует действия» на ГЛАВНОЙ не показываем — только в меню пилюль.
      // «×» скрыт; проявляется только в режиме редактирования (долгое нажатие).
      return `<button class="pill" type="button" data-pid="${esc(b.id)}"
        onclick='quickAction(${JSON.stringify(b.action)}, ${JSON.stringify(params)})'>
        <span class="pill-del" aria-label="Убрать" title="Убрать">×</span>
        <span class="pill-txt">${esc(label)}${subHtml}</span>
      </button>`;
    }).join('');
    const master = isGuest ? '' : `<button class="pill-add" type="button" aria-label="Добавить пилюлю"
        title="Меню пилюль: добавить, закрепить, переставить"
        onclick="window.__openPillMaster&&window.__openPillMaster()">+</button>`;
    p.innerHTML = html + master;
    wirePillEditMode();
  }

  function refreshPills() {
    if (window.__pillRole) applyRoleWelcome(window.__pillRole);
    if (document.getElementById('pm-overlay')) renderPillMaster();
  }

  // --- iOS-стиль: долгое нажатие → режим правки (×) + перетаскивание для порядка ---
  let __pillEditing = false, __lpTimer = null, __lpStart = null, __dragPill = null, __dragging = false;
  let __dragOff = { x: 0, y: 0 }, __pillPlaceholder = null;
  let __lastRemoved = null, __undoTimer = null;
  function __enterPillEdit() {
    const el = $('welcomePills'); if (!el) return;
    __pillEditing = true; el.classList.add('pills-editing');
    document.addEventListener('click', __pillEditOutside, true);
    document.addEventListener('keydown', __pillEditKey);
    if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) {} }
  }
  function __exitPillEdit() {
    const el = $('welcomePills'); __pillEditing = false;
    if (el) el.classList.remove('pills-editing');
    document.removeEventListener('click', __pillEditOutside, true);
    document.removeEventListener('keydown', __pillEditKey);
  }
  function __pillEditOutside(e) {
    const el = $('welcomePills');
    if (el && !el.contains(e.target)) __exitPillEdit();
  }
  function __pillEditKey(e) { if (e.key === 'Escape') __exitPillEdit(); }
  function __savePillOrderFromDOM() {
    const el = $('welcomePills'); if (!el) return;
    const ids = [...el.querySelectorAll('.pill[data-pid]')].map(n => n.getAttribute('data-pid'));
    const role = window.__pillRole, prefs = loadPillPrefs(role);
    prefs.order = ids; savePillPrefs(role, prefs);
  }
  // Поднять пилюлю: фиксируем её под курсором, на месте — пустой слот.
  function __startPillDrag(e) {
    const dp = __dragPill; if (!dp) return;
    const r = dp.getBoundingClientRect();
    __dragOff = { x: e.clientX - r.left, y: e.clientY - r.top };
    __pillPlaceholder = document.createElement('span');
    __pillPlaceholder.className = 'pill-placeholder';
    __pillPlaceholder.style.width = r.width + 'px';
    __pillPlaceholder.style.height = r.height + 'px';
    dp.parentNode.insertBefore(__pillPlaceholder, dp);
    dp.style.width = r.width + 'px'; dp.style.height = r.height + 'px';
    dp.style.left = r.left + 'px'; dp.style.top = r.top + 'px';
    document.body.appendChild(dp);          // fixed → следует за курсором, не обрезается
    dp.classList.add('pill-dragging');
    __dragging = true;
  }
  // Опустить пилюлю на место слота, зафиксировать порядок.
  function __endPillDrag() {
    const dp = __dragPill;
    if (dp && __pillPlaceholder) {
      __pillPlaceholder.parentNode.insertBefore(dp, __pillPlaceholder);
      __pillPlaceholder.remove();
      dp.classList.remove('pill-dragging');
      dp.style.left = dp.style.top = dp.style.width = dp.style.height = '';
      __savePillOrderFromDOM();
    }
    __pillPlaceholder = null; __dragPill = null; __dragging = false;
  }
  function wirePillEditMode() {
    const el = $('welcomePills'); if (!el || el.__pillEditWired) return; el.__pillEditWired = true;
    const cancelLP = () => { clearTimeout(__lpTimer); __lpTimer = null; };
    // Начало взаимодействия с пилюлей.
    el.addEventListener('pointerdown', (e) => {
      const pill = e.target.closest('.pill');
      if (!pill || pill.classList.contains('pill-master') || e.target.closest('.pill-del')) return;
      __lpStart = { x: e.clientX, y: e.clientY, pill };
      if (__pillEditing) {
        __dragPill = pill; __dragging = false;   // в режиме правки — готовы тащить
      } else {
        clearTimeout(__lpTimer);
        __lpTimer = setTimeout(() => {            // зажать ~0.5с → режим правки
          __enterPillEdit();
          if (__lpStart) { __dragPill = __lpStart.pill; __dragging = false; }
        }, 480);
      }
    });
    // Движение: либо отмена long-press (скролл), либо перетаскивание.
    document.addEventListener('pointermove', (e) => {
      if (!__lpStart) return;
      const dx = e.clientX - __lpStart.x, dy = e.clientY - __lpStart.y;
      if (!__pillEditing) {                       // ещё ждём долгое нажатие
        if (Math.abs(dx) > 10 || Math.abs(dy) > 10) cancelLP();
        return;
      }
      if (!__dragPill) return;
      if (!__dragging) {
        if (Math.abs(dx) > 6 || Math.abs(dy) > 6) __startPillDrag(e);
        else return;
      }
      e.preventDefault();                         // не скроллим во время drag
      // Пилюля едет за курсором.
      __dragPill.style.left = (e.clientX - __dragOff.x) + 'px';
      __dragPill.style.top = (e.clientY - __dragOff.y) + 'px';
      // Слот переезжает туда, где курсор — остальные расступаются.
      const node = document.elementFromPoint(e.clientX, e.clientY);
      const t = node && node.closest && node.closest('.pill[data-pid]');
      if (t && t !== __dragPill && __pillPlaceholder) {
        const r = t.getBoundingClientRect();
        const before = e.clientX < r.left + r.width / 2;
        t.parentNode.insertBefore(__pillPlaceholder, before ? t : t.nextSibling);
      }
    }, { passive: false });
    // Конец взаимодействия.
    const endPointer = () => {
      cancelLP(); __lpStart = null;
      if (__dragging) __endPillDrag();
      else { __dragPill = null; }
    };
    document.addEventListener('pointerup', endPointer);
    document.addEventListener('pointercancel', endPointer);
    // Перехват кликов (capture): «×» убирает пилюлю; в режиме правки тап по телу
    // не запускает действие (и не считается, если был drag); «+» — выходит и открывает меню.
    el.addEventListener('click', (e) => {
      const del = e.target.closest('.pill-del');
      if (del) {
        e.stopPropagation(); e.preventDefault();
        const pill = del.closest('.pill'); if (pill) __pillRemove(pill.getAttribute('data-pid'));
        return;
      }
      if (__pillEditing) {
        if (e.target.closest('.pill-add')) { __exitPillEdit(); return; }
        e.stopPropagation(); e.preventDefault();
      }
    }, true);
  }

  // --- Операции над пилюлями ---
  window.__pillUnpin = function (el) {
    const pill = el.closest('.pill'); if (!pill) return;
    __pillDoUnpin(pill.getAttribute('data-pid'));
  };
  // order ВСЕГДА хранит полную последовательность видимых пилюль — чтобы
  // закреп/добавление шли в конец, а не «прыгали» в начало.
  function __pillDoUnpin(id) {
    const role = window.__pillRole;
    const ids = pillState(role).visible.map(p => p.id).filter(x => x !== id);
    const prefs = loadPillPrefs(role);
    prefs.hidden = (prefs.hidden || []); if (!prefs.hidden.includes(id)) prefs.hidden.push(id);
    prefs.order = ids;
    savePillPrefs(role, prefs); refreshPills();
  }
  function __pillPin(id) {
    const role = window.__pillRole, st = pillState(role);
    const p = st.byId[id] || st.global.find(x => x.id === id); if (!p) return;
    const prefs = loadPillPrefs(role);
    prefs.hidden = (prefs.hidden || []).filter(x => x !== id);
    if (!st.cat.some(x => x.id === id)) {  // пилюля из другого кабинета → в custom
      prefs.custom = (prefs.custom || []);
      prefs.custom.push({ emoji: p.emoji, label: p.text, action: p.action, params: p.params });
    }
    const ids = st.visible.map(x => x.id).filter(x => x !== id); ids.push(id);
    prefs.order = ids;
    savePillPrefs(role, prefs); refreshPills();
  }
  function __pillMove(id, dir) {
    const role = window.__pillRole;
    const ids = pillState(role).visible.map(p => p.id);
    const i = ids.indexOf(id), j = i + dir;
    if (i < 0 || j < 0 || j >= ids.length) return;
    ids.splice(j, 0, ids.splice(i, 1)[0]);
    const prefs = loadPillPrefs(role); prefs.order = ids; savePillPrefs(role, prefs); refreshPills();
  }
  function __pillReorderDrop(dragId, targetId) {
    if (dragId === targetId) return;
    const role = window.__pillRole;
    const ids = pillState(role).visible.map(p => p.id);
    const from = ids.indexOf(dragId); if (from < 0) return;
    ids.splice(from, 1);
    let to = ids.indexOf(targetId); if (to < 0) to = ids.length; else to += 1;
    ids.splice(to, 0, dragId);
    const prefs = loadPillPrefs(role); prefs.order = ids; savePillPrefs(role, prefs); refreshPills();
  }
  function __pillRemove(id) {
    const role = window.__pillRole, st = pillState(role);
    const p = st.byId[id];
    const idx = st.visible.findIndex(x => x.id === id);
    const isCustom = !((ROLE_WELCOME[role] || ROLE_WELCOME.buyer).pills || []).some(b => pillId(b.action, b.params) === id);
    // Запоминаем для «Вернуть» (случайно убрали).
    if (p) __lastRemoved = { role, id, idx: idx < 0 ? 9999 : idx, isCustom,
      p: { emoji: p.emoji, label: p.text, action: p.action, params: p.params } };
    const ids = st.visible.map(x => x.id).filter(x => x !== id);
    const prefs = loadPillPrefs(role);
    if (isCustom) prefs.custom = (prefs.custom || []).filter(c => pillId(c.action, c.params) !== id);
    prefs.hidden = (prefs.hidden || []); if (!isCustom && !prefs.hidden.includes(id)) prefs.hidden.push(id);
    prefs.order = ids;
    savePillPrefs(role, prefs); refreshPills();
    if (p) __showPillUndo(p.text);
  }
  // Вернуть последнюю убранную пилюлю — на её прежнее место.
  function __pillRestoreLast() {
    const lr = __lastRemoved; if (!lr) return;
    const role = lr.role;
    let prefs = loadPillPrefs(role);
    prefs.hidden = (prefs.hidden || []).filter(x => x !== lr.id);
    if (lr.isCustom) {
      prefs.custom = (prefs.custom || []);
      if (!prefs.custom.some(c => pillId(c.action, c.params) === lr.id)) prefs.custom.push(lr.p);
    }
    savePillPrefs(role, prefs);
    // Вставляем на прежний индекс.
    prefs = loadPillPrefs(role);
    const vis = pillState(role).visible.map(x => x.id).filter(x => x !== lr.id);
    const at = Math.max(0, Math.min(vis.length, lr.idx));
    vis.splice(at, 0, lr.id);
    prefs.order = vis; savePillPrefs(role, prefs);
    __lastRemoved = null; __hidePillUndo(); refreshPills();
  }
  // Снэкбар «Вернуть» (как на iPhone после удаления).
  function __showPillUndo(name) {
    _ensurePillCSS();
    __hidePillUndo();
    const bar = document.createElement('div');
    bar.id = 'pill-undo'; bar.className = 'pill-undo';
    bar.innerHTML = `<span class="pu-txt">Пилюля «${esc(name || '')}» убрана</span>`
      + `<button type="button" class="pu-btn">Вернуть</button>`;
    bar.querySelector('.pu-btn').onclick = () => __pillRestoreLast();
    document.body.appendChild(bar);
    requestAnimationFrame(() => bar.classList.add('show'));
    __undoTimer = setTimeout(__hidePillUndo, 6000);
  }
  function __hidePillUndo() {
    if (__undoTimer) { clearTimeout(__undoTimer); __undoTimer = null; }
    const bar = document.getElementById('pill-undo');
    if (bar) { bar.classList.remove('show'); setTimeout(() => bar.remove(), 200); }
  }
  function __pillAddCustom(emoji, label, action, params) {
    const role = window.__pillRole, st = pillState(role), prefs = loadPillPrefs(role);
    prefs.custom = (prefs.custom || []);
    prefs.custom.push({ emoji: emoji || '✨', label: label || action, action, params: params || {} });
    const id = pillId(action, params || {});
    prefs.hidden = (prefs.hidden || []).filter(x => x !== id);
    const ids = st.visible.map(x => x.id).filter(x => x !== id); ids.push(id);
    prefs.order = ids;
    savePillPrefs(role, prefs); refreshPills();
  }
  function __pillResetRole() {
    const role = window.__pillRole;
    try { localStorage.removeItem(PILL_KEY(role)); } catch (e) {}
    refreshPills();
  }

  // --- Мастер-меню (модалка) ---
  let __pmConfirmId = null, __pmAddOpen = false, __pmDragId = null;
  function _ensurePillCSS() {
    if (document.getElementById('pill-css')) return;
    const s = document.createElement('style'); s.id = 'pill-css';
    s.textContent =
      '.pill{position:relative;-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}'
      + '.pill-add{width:40px;height:40px;border-radius:50%;border:1px dashed rgba(0,0,0,.28);'
      + 'background:rgba(255,255,255,.45);font-size:22px;line-height:1;color:rgba(0,0,0,.5);cursor:pointer;'
      + 'display:inline-flex;align-items:center;justify-content:center;vertical-align:middle;padding:0;'
      + 'transition:background .15s,border-color .15s,color .15s,transform .1s}'
      + '.pill-add:hover{background:#fff;border-color:rgba(0,0,0,.45);color:#000}'
      + '.pill-add:active{transform:scale(.92)}'
      + '.pill-del{position:absolute;top:-6px;right:-6px;width:18px;height:18px;border-radius:50%;'
      + 'background:rgba(70,70,70,.5);color:#fff;font-size:13px;line-height:17px;text-align:center;font-weight:600;'
      + 'opacity:0;transform:scale(.4);transition:opacity .15s,transform .15s,background .15s;'
      + 'pointer-events:none;box-shadow:0 1px 3px rgba(0,0,0,.3);z-index:4}'
      + '.pills-editing .pill-del{opacity:.45;transform:scale(1);pointer-events:auto}'
      + '.pills-editing .pill-del:hover{opacity:1;background:#e0245e}'
      + '.pills-editing .pill{animation:pilljiggle .42s ease-in-out infinite;touch-action:none}'
      + '.pill-dragging{position:fixed!important;margin:0!important;z-index:9999;pointer-events:none;'
      + 'animation:none!important;opacity:.96;transform:scale(1.08);transition:transform .1s;'
      + 'box-shadow:0 12px 30px rgba(0,0,0,.34)}'
      + '.pill-placeholder{display:inline-flex;vertical-align:middle;border-radius:999px;box-sizing:border-box;'
      + 'background:rgba(0,0,0,.05);border:1px dashed rgba(0,0,0,.2)}'
      + '@keyframes pilljiggle{0%{transform:rotate(-.5deg)}50%{transform:rotate(.5deg)}100%{transform:rotate(-.5deg)}}'
      + '.pm-overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:100000;animation:pmfade .12s ease}'
      + '@keyframes pmfade{from{opacity:0}to{opacity:1}}'
      + '.pm-box{background:#fff;color:#1a1a1a;width:min(94vw,560px);max-height:86vh;overflow:auto;border-radius:16px;padding:18px 18px 22px;box-shadow:0 16px 50px rgba(0,0,0,.4)}'
      + '.pm-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}'
      + '.pm-title{font-weight:700;font-size:17px}.pm-sub{font-size:12px;opacity:.5;margin-bottom:6px}'
      + '.pm-x{border:none;background:rgba(0,0,0,.06);width:30px;height:30px;border-radius:8px;cursor:pointer;font-size:16px}'
      + '.pm-sec{font-size:11px;text-transform:uppercase;letter-spacing:.04em;opacity:.5;margin:14px 0 6px;font-weight:600}'
      + '.pm-row{display:flex;align-items:center;gap:8px;padding:7px 9px;border:1px solid rgba(0,0,0,.09);border-radius:10px;margin-bottom:6px;background:#fff}'
      + '.pm-row.dragover{border-color:#2563eb;background:rgba(37,99,235,.07)}'
      + '.pm-grip{opacity:.35;cursor:grab;font-size:13px;user-select:none}'
      + '.pm-emoji{font-size:15px;width:20px;text-align:center}'
      + '.pm-lbl{flex:1;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
      + '.pm-src{font-size:10px;opacity:.45;border:1px solid rgba(0,0,0,.14);border-radius:6px;padding:1px 5px;white-space:nowrap}'
      + '.pm-badge{min-width:18px;height:18px;padding:0 5px;box-sizing:border-box;border-radius:9px;background:#e0245e;color:#fff;font-size:11px;font-weight:700;line-height:18px;text-align:center}'
      + '.pm-act{border:none;background:rgba(0,0,0,.06);border-radius:7px;min-width:28px;height:28px;cursor:pointer;font-size:13px;padding:0 7px}'
      + '.pm-act:hover{background:rgba(0,0,0,.12)}.pm-act.danger:hover{background:rgba(220,38,38,.16)}'
      + '.pm-act.pin{background:rgba(37,99,235,.1)}.pm-act.pin:hover{background:rgba(37,99,235,.2)}'
      + '.pm-search{width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid rgba(0,0,0,.16);border-radius:9px;margin-bottom:8px;font:inherit}'
      + '.pm-form{border:1px dashed rgba(0,0,0,.22);border-radius:10px;padding:11px;margin-top:8px;display:grid;gap:8px}'
      + '.pm-form input,.pm-form select{padding:8px 10px;border:1px solid rgba(0,0,0,.16);border-radius:8px;font:inherit;width:100%;box-sizing:border-box}'
      + '.pm-frow{display:flex;gap:8px}.pm-frow .pm-emoji-in{flex:0 0 64px}'
      + '.pm-confirm{color:#dc2626;font-size:12px;margin-right:2px}'
      + '.pm-empty{opacity:.4;font-size:13px;padding:6px 2px}'
      + '.pm-addbtn{border:none;background:#2563eb;color:#fff;border-radius:9px;padding:9px 14px;cursor:pointer;font:inherit;font-weight:600}'
      + '.pm-reset{border:1px solid rgba(0,0,0,.14);background:#fff;color:#1a1a1a;cursor:pointer;font:inherit;font-size:13px;font-weight:600;border-radius:9px;margin-top:6px;padding:9px 14px;width:100%}'
      + '.pm-reset:hover{background:rgba(0,0,0,.04)}'
      + '.pm-hint{font-size:12px;opacity:.6;line-height:1.4;margin:2px 0 8px}'
      + '.pm-row.pm-missing{border-color:rgba(37,99,235,.4);background:rgba(37,99,235,.05)}'
      + '.pm-missing .pm-act.pin{min-width:auto;font-weight:600;color:#2563eb}'
      + '.pill-undo{position:fixed;left:50%;bottom:22px;transform:translate(-50%,16px);z-index:100001;'
      + 'display:flex;align-items:center;gap:14px;background:#1f2937;color:#fff;padding:11px 14px 11px 16px;'
      + 'border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.35);opacity:0;transition:opacity .2s,transform .2s;'
      + 'max-width:92vw;font-size:14px}'
      + '.pill-undo.show{opacity:1;transform:translate(-50%,0)}'
      + '.pill-undo .pu-txt{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:60vw}'
      + '.pill-undo .pu-btn{border:none;background:transparent;color:#7dd3fc;font:inherit;font-weight:700;cursor:pointer;padding:4px 6px;white-space:nowrap}'
      + '.pill-undo .pu-btn:hover{color:#bae6fd}'
      // ── dark-mode: модал/строки/инпуты были белыми (белое-на-белом) ──
      + 'body.dark-mode .pm-box{background:#1f1f1f;color:#f0f0f0}'
      + 'body.dark-mode .pm-row{background:rgba(255,255,255,.04);border-color:rgba(255,255,255,.10)}'
      + 'body.dark-mode .pm-form{border-color:rgba(255,255,255,.18)}'
      + 'body.dark-mode .pm-form input,body.dark-mode .pm-form select,body.dark-mode .pm-search{background:#2a2a2a;color:#f0f0f0;border-color:rgba(255,255,255,.16)}'
      + 'body.dark-mode .pm-reset{background:rgba(255,255,255,.06);color:#f0f0f0;border-color:rgba(255,255,255,.16)}'
      + 'body.dark-mode .pm-reset:hover{background:rgba(255,255,255,.12)}'
      + 'body.dark-mode .pm-x,body.dark-mode .pm-act{background:rgba(255,255,255,.08);color:#f0f0f0}'
      + 'body.dark-mode .pm-act:hover{background:rgba(255,255,255,.16)}'
      + 'body.dark-mode .pm-src{border-color:rgba(255,255,255,.18)}'
      + 'body.dark-mode .pill-add{background:rgba(255,255,255,.06);color:rgba(255,255,255,.6);border-color:rgba(255,255,255,.28)}'
      + 'body.dark-mode .pill-add:hover{background:rgba(255,255,255,.14);color:#fff;border-color:rgba(255,255,255,.5)}'
      + 'body.dark-mode .pill-placeholder{background:rgba(255,255,255,.05);border-color:rgba(255,255,255,.2)}';
    document.head.appendChild(s);
  }
  window.__openPillMaster = function () {
    _ensurePillCSS();
    if (document.getElementById('pm-overlay')) return;
    const ov = document.createElement('div'); ov.id = 'pm-overlay'; ov.className = 'pm-overlay';
    ov.innerHTML = `<div class="pm-box" id="pm-box"></div>`;
    ov.addEventListener('click', (e) => { if (e.target === ov) __closePillMaster(); });
    document.body.appendChild(ov);
    __pmConfirmId = null; __pmAddOpen = false;
    renderPillMaster();
  };
  function __closePillMaster() {
    const ov = document.getElementById('pm-overlay'); if (ov) ov.remove();
    __pmConfirmId = null; __pmAddOpen = false;
  }
  function renderPillMaster() {
    const box = document.getElementById('pm-box'); if (!box) return;
    const role = window.__pillRole, st = pillState(role);
    const roleName = ROLE_RU[role] || role;
    // Пилюли из стандартного набора, которых сейчас НЕТ на экране = что убрали.
    const visIds = new Set(st.visible.map(p => p.id));
    const missingDefaults = (ROLE_WELCOME[role] || ROLE_WELCOME.buyer).pills
      .map(b => _normPill(b, role)).filter(p => !visIds.has(p.id));
    const missingBlock = missingDefaults.length ? (
      `<div class="pm-sec">Убрано из стандартных (${missingDefaults.length})</div>`
      + `<div class="pm-hint">Похоже, вот это вы убирали — нажмите «Вернуть».</div>`
      + missingDefaults.map(p => `<div class="pm-row pm-missing">
          <span class="pm-emoji">${esc(p.emoji)}</span>
          <span class="pm-lbl">${esc(p.text)}</span>${pillBadgeHtml(p.action, 'pm-badge')}
          <button class="pm-act pin" data-pm="pin" data-pid="${esc(p.id)}" title="Вернуть">＋ Вернуть</button>
        </div>`).join('')
    ) : '';
    const defIds = new Set((ROLE_WELCOME[role] || ROLE_WELCOME.buyer).pills.map(b => pillId(b.action, b.params)));
    const pinnedRows = st.visible.map((p) => {
      const isCustom = !defIds.has(p.id);
      const confirming = (__pmConfirmId === p.id);
      // Одна кнопка: «−» убрать с экрана (встроенная, обратимо) / «🗑» удалить (своя).
      const del = confirming
        ? `<span class="pm-confirm">Удалить?</span>
           <button class="pm-act danger" data-pm="del-yes" data-pid="${esc(p.id)}" title="Да">✓</button>
           <button class="pm-act" data-pm="del-no" title="Отмена">✕</button>`
        : (isCustom
            ? `<button class="pm-act danger" data-pm="del" data-pid="${esc(p.id)}" title="Удалить пилюлю">🗑</button>`
            : `<button class="pm-act" data-pm="unpin" data-pid="${esc(p.id)}" title="Убрать с экрана">−</button>`);
      return `<div class="pm-row" draggable="true" data-pid="${esc(p.id)}">
        <span class="pm-grip" title="Перетащите, чтобы переставить">⠿</span>
        <span class="pm-emoji">${esc(p.emoji)}</span>
        <span class="pm-lbl">${esc(p.text)}</span>${pillBadgeHtml(p.action, 'pm-badge')}
        ${del}
      </div>`;
    }).join('') || `<div class="pm-empty">Нет закреплённых пилюль</div>`;

    const availRows = st.avail.map(p => `
      <div class="pm-row pm-avail-row" data-text="${esc((p.emoji + ' ' + p.text).toLowerCase())}">
        <span class="pm-emoji">${esc(p.emoji)}</span>
        <span class="pm-lbl">${esc(p.text)}</span>${pillBadgeHtml(p.action, 'pm-badge')}
        ${p.srcRole && p.srcRole !== role ? `<span class="pm-src">${esc(ROLE_RU[p.srcRole] || p.srcRole)}</span>` : ''}
        <button class="pm-act pin" data-pm="pin" data-pid="${esc(p.id)}" title="Закрепить">＋</button>
      </div>`).join('') || `<div class="pm-empty">Все пилюли уже на экране</div>`;

    const actionOpts = st.global.filter(p => __pillAllowed(role, p.action)).map(p =>
      `<option value="${esc(p.id)}">${esc(p.emoji + ' ' + p.text)} — ${esc(ROLE_RU[p.srcRole] || p.srcRole || '')}</option>`
    ).join('');
    const addForm = __pmAddOpen ? `
      <div class="pm-form">
        <div class="pm-frow">
          <input id="pm-add-emoji" class="pm-emoji-in" placeholder="✨" maxlength="3" value="✨">
          <input id="pm-add-label" placeholder="Название пилюли">
        </div>
        <select id="pm-add-action"><option value="" disabled>— действие пилюли —</option>${actionOpts}</select>
        <div class="pm-frow">
          <button class="pm-addbtn" data-pm="add-save" style="flex:1">Добавить на экран</button>
          <button class="pm-act" data-pm="add-cancel" style="height:auto;padding:0 14px">Отмена</button>
        </div>
      </div>` : `<button class="pm-addbtn" data-pm="add-open" style="margin-top:8px">➕ Своя пилюля</button>`;

    box.innerHTML = `
      <div class="pm-head">
        <div class="pm-title">Пилюли</div>
        <button class="pm-x" data-pm="close" title="Закрыть">✕</button>
      </div>
      <div class="pm-sub">Кабинет: ${esc(roleName)} · перетащите ⠿ чтобы переставить · «−» чтобы убрать</div>
      <div class="pm-hint">Случайно убрали пилюлю? Верните её в разделе ниже или восстановите стандартный набор.</div>
      ${missingBlock}
      <div class="pm-sec">На экране (${st.visible.length})</div>
      <div id="pm-pinned">${pinnedRows}</div>
      ${addForm}
      <div class="pm-sec">Все пилюли — закрепить (${st.avail.length})</div>
      <input class="pm-search" id="pm-search" placeholder="Поиск пилюли по всем кабинетам…">
      <div id="pm-avail">${availRows}</div>
      <div class="pm-sec">Стандартный набор</div>
      <div class="pm-hint">${((ROLE_WELCOME[role] || ROLE_WELCOME.buyer).pills || []).length} пилюль по умолчанию для кабинета «${esc(roleName)}». Вернёт всё как было — порядок и состав.</div>
      <button class="pm-reset" data-pm="reset">↺ Восстановить стандартный набор</button>`;
    wirePillMaster();
  }
  function wirePillMaster() {
    const box = document.getElementById('pm-box'); if (!box) return;
    box.onclick = (e) => {
      const btn = e.target.closest('[data-pm]'); if (!btn) return;
      const pm = btn.getAttribute('data-pm'), id = btn.getAttribute('data-pid');
      if (pm === 'close') return __closePillMaster();
      if (pm === 'unpin') { __pillDoUnpin(id); return; }
      if (pm === 'pin') { __pillPin(id); return; }
      if (pm === 'up') { __pillMove(id, -1); return; }
      if (pm === 'down') { __pillMove(id, 1); return; }
      if (pm === 'del') { __pmConfirmId = id; renderPillMaster(); return; }
      if (pm === 'del-no') { __pmConfirmId = null; renderPillMaster(); return; }
      if (pm === 'del-yes') { __pmConfirmId = null; __pillRemove(id); return; }
      if (pm === 'add-open') { __pmAddOpen = true; renderPillMaster(); return; }
      if (pm === 'add-cancel') { __pmAddOpen = false; renderPillMaster(); return; }
      if (pm === 'reset') { __pmConfirmId = null; __pillResetRole(); return; }
      if (pm === 'add-save') {
        const emoji = (document.getElementById('pm-add-emoji') || {}).value || '✨';
        const label = (document.getElementById('pm-add-label') || {}).value || '';
        const sel = document.getElementById('pm-add-action');
        const chosenId = sel && sel.value;
        if (!chosenId) { sel && sel.focus(); return; }
        const src = globalPillCatalog().find(p => p.id === chosenId);
        if (!src) return;
        __pmAddOpen = false;
        __pillAddCustom(emoji.trim(), label.trim() || src.text, src.action, src.params);
        return;
      }
    };
    // Живой поиск по доступным пилюлям (без перерендера — фокус сохраняется).
    const search = document.getElementById('pm-search');
    if (search) search.oninput = () => {
      const q = search.value.trim().toLowerCase();
      document.querySelectorAll('#pm-avail .pm-avail-row').forEach(r => {
        r.style.display = (!q || (r.getAttribute('data-text') || '').includes(q)) ? '' : 'none';
      });
    };
    // Drag-and-drop перестановка закреплённых.
    box.querySelectorAll('#pm-pinned .pm-row').forEach(row => {
      row.addEventListener('dragstart', (e) => { __pmDragId = row.getAttribute('data-pid'); e.dataTransfer.effectAllowed = 'move'; });
      row.addEventListener('dragover', (e) => { e.preventDefault(); row.classList.add('dragover'); });
      row.addEventListener('dragleave', () => row.classList.remove('dragover'));
      row.addEventListener('drop', (e) => {
        e.preventDefault(); row.classList.remove('dragover');
        const target = row.getAttribute('data-pid');
        if (__pmDragId && target) __pillReorderDrop(__pmDragId, target);
        __pmDragId = null;
      });
    });
  }

  async function loadProjects() {
    const el = $('projectsList');
    if (!el) return;
    try {
      const data = await api('/api/assistant/projects/');
      const list = data.projects || [];
      if (!list.length) {
        el.innerHTML = `<div class="side-item" style="color:rgba(0,0,0,0.4);">Нет проектов</div>`;
        return;
      }
      el.innerHTML = list.map(p => {
        const dot = DOT_BG[p.dot_color] || DOT_BG.green;
        const pidStr = String(p.id).replace(/'/g, "&#39;");
        const nameStr = String(p.name || '').replace(/'/g, "&#39;").replace(/"/g, '&quot;');
        return `<a href="/chat/project/${esc(p.id)}/" class="side-item side-item-proj" data-project-id="${esc(p.id)}" data-project-name="${esc(p.name)}" style="text-decoration:none;">
          <span class="side-item-dot" style="background:${dot};"></span>
          <span class="side-item-text">${esc(p.name)}</span>
          <span class="side-item-meta">${esc(p.chats || 0)}</span>
          <button class="side-item-del" type="button" title="Удалить проект" aria-label="Удалить" onclick="event.preventDefault();event.stopPropagation();window.__deleteProject&amp;&amp;window.__deleteProject('${pidStr}','${nameStr}');return false;">×</button>
        </a>`;
      }).join('');
    } catch(e){
      // leave demo items as fallback
    }
  }

  // Аккуратная модалка вместо нативного prompt(). Возвращает Promise<string|null>.
  function _ensureUiPromptCSS() {
    if (document.getElementById('uiprompt-css')) return;
    const s = document.createElement('style');
    s.id = 'uiprompt-css';
    s.textContent =
      '.uip-overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:99999;animation:uipfade .12s ease}'
      + '@keyframes uipfade{from{opacity:0}to{opacity:1}}'
      + '.uip-box{background:#fff;color:#1a1a1a;width:min(92vw,420px);border-radius:14px;padding:18px;box-shadow:0 14px 44px rgba(0,0,0,.38);animation:uippop .14s ease}'
      + '@keyframes uippop{from{transform:translateY(6px) scale(.98);opacity:.6}to{transform:none;opacity:1}}'
      + '.uip-title{font-weight:700;font-size:16px;margin-bottom:4px}'
      + '.uip-note{font-size:12.5px;line-height:1.5;color:#374151;background:rgba(37,99,235,.06);border:1px solid rgba(37,99,235,.14);border-radius:9px;padding:9px 11px;margin:8px 0 2px}'
      + 'body.dark-mode .uip-note{color:#cbd5e1;background:rgba(255,255,255,.05);border-color:rgba(255,255,255,.12)}'
      + '.uip-label{font-size:12px;opacity:.6;margin:6px 0 4px}'
      + '.uip-input{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:9px;border:1px solid rgba(0,0,0,.18);background:#fff;color:#1a1a1a;font:inherit;outline:none;margin-top:6px}'
      + '.uip-input:focus{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.15)}'
      + '.uip-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}'
      + '.uip-btn{padding:9px 18px;border-radius:9px;border:none;font:inherit;cursor:pointer}'
      + '.uip-cancel{background:rgba(0,0,0,.07);color:#1a1a1a}.uip-cancel:hover{background:rgba(0,0,0,.12)}'
      + '.uip-ok{background:#2563eb;color:#fff}.uip-ok:hover{opacity:.92}'
      // ── dark-mode: был белый модал/инпут (белое-на-белом) ──
      + 'body.dark-mode .uip-box{background:#1f1f1f;color:#f0f0f0}'
      + 'body.dark-mode .uip-input{background:#2a2a2a;color:#f0f0f0;border-color:rgba(255,255,255,.18)}'
      + 'body.dark-mode .uip-cancel{background:rgba(255,255,255,.08);color:#f0f0f0}'
      + 'body.dark-mode .uip-cancel:hover{background:rgba(255,255,255,.14)}';
    document.head.appendChild(s);
  }
  window.uiPrompt = function(opts) {
    opts = opts || {};
    _ensureUiPromptCSS();
    return new Promise((resolve) => {
      const ov = document.createElement('div');
      ov.className = 'uip-overlay';
      ov.innerHTML =
        '<div class="uip-box" role="dialog" aria-modal="true">'
        + '<div class="uip-title"></div>'
        + (opts.note ? '<div class="uip-note"></div>' : '')
        + (opts.label ? '<div class="uip-label"></div>' : '')
        + '<input class="uip-input" type="text" autocomplete="off"/>'
        + '<div class="uip-actions">'
        + '<button type="button" class="uip-btn uip-cancel"></button>'
        + '<button type="button" class="uip-btn uip-ok"></button>'
        + '</div></div>';
      ov.querySelector('.uip-title').textContent = opts.title || 'Введите значение';
      if (opts.note) ov.querySelector('.uip-note').textContent = opts.note;
      if (opts.label) ov.querySelector('.uip-label').textContent = opts.label;
      const inp = ov.querySelector('.uip-input');
      inp.placeholder = opts.placeholder || '';
      inp.value = opts.value || '';
      ov.querySelector('.uip-cancel').textContent = opts.cancelLabel || 'Отмена';
      ov.querySelector('.uip-ok').textContent = opts.okLabel || 'OK';
      let done = false;
      const close = (val) => {
        if (done) return; done = true;
        document.removeEventListener('keydown', onKey, true);
        ov.remove();
        resolve(val);
      };
      const submit = () => close((inp.value || '').trim());
      ov.querySelector('.uip-ok').addEventListener('click', submit);
      ov.querySelector('.uip-cancel').addEventListener('click', () => close(null));
      ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(null); });
      const onKey = (e) => {
        if (e.key === 'Escape') { e.preventDefault(); close(null); }
        else if (e.key === 'Enter' && document.activeElement === inp) { e.preventDefault(); submit(); }
      };
      document.addEventListener('keydown', onKey, true);
      document.body.appendChild(ov);
      setTimeout(() => inp.focus(), 30);
    });
  };

  // «+ Новый проект» в сайдбаре → аккуратная модалка → POST → reload → переход.
  // Памятка «зачем проект» — по роли (покупатель/продавец/оператор).
  function _projectCreateHint() {
    var r = (state.config && state.config.role) || 'buyer';
    if (r.indexOf('operator') === 0) {
      var sub = r.replace('operator_', '').replace('operator', 'manager') || 'manager';
      var ON = {
        manager: '🎛 Проект — это сделка / консолидированная поставка. Контракты, таможня, логистика, платежи — вся поставка от RFQ до доставки; видны и покупатель, и продавцы.',
        logist:  '🚚 Проект — это поставка с фокусом на доставке. Соберите логистику (BL/CMR, маршрут) и статус таможни — все отгрузки сделки под контролем.',
        customs: '🛂 Проект — это поставка с фокусом на растаможке. Декларации, HS-коды, инвойсы, сертификаты — таможня по всей сделке в одном месте.',
        payment: '💳 Проект — это поставка с фокусом на финансах. Инвойсы, эскроу, акты, выплаты — деньги по сделке: оплата покупателя и payout продавцам.',
      };
      return {note: ON[sub] || ON.manager, placeholder: 'Напр. Сделка Урал Q3'};
    }
    var base = (r === 'seller') ? 'seller' : 'buyer';
    var NOTE = {
      buyer: '📦 Проект — это ваша закупка под технику или объект. Загрузите парк техники, историю закупок и чертежи — AI точнее подберёт запчасти и соберёт RFQ в контексте проекта.',
      seller: '🏷 Проект — это ваше товарное направление (сегмент). Соберите прайс, чертежи, сертификаты и фото по нему — быстрее формируете КП по входящим RFQ, и покупатель больше доверяет.',
    };
    var PH = {buyer: 'Напр. Парк Komatsu — Ковдор', seller: 'Напр. Ходовка Komatsu'};
    return {note: NOTE[base], placeholder: PH[base]};
  }
  async function createProjectFromSidebar() {
    const hint = _projectCreateHint();
    const name = await window.uiPrompt({
      title: 'Новый проект', note: hint.note, label: 'Название проекта',
      placeholder: hint.placeholder, okLabel: 'Создать',
    });
    if (!name) return;
    try {
      const r = await fetch('/api/assistant/projects/', {
        method: 'POST',
        headers: {'Content-Type':'application/json','X-CSRFToken': csrf()},
        body: JSON.stringify({name}),
      });
      if (!r.ok) {
        alert('Не удалось создать проект (HTTP ' + r.status + ')');
        return;
      }
      const data = await r.json();
      if (data && data.id) {
        window.location.href = '/chat/project/' + data.id + '/';
      } else {
        await loadProjects();
      }
    } catch(e) {
      alert('Ошибка: ' + (e && e.message ? e.message : e));
    }
  }
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('#sideProjects .side-section-add');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    createProjectFromSidebar();
  });

  // Глобальный helper для inline-onclick — самый надёжный способ обойти
  // конкуренцию click-handler'ов с <a href> в разных браузерах.
  window.__deleteProject = async function(pid, name) {
    if (!pid) return;
    if (!window.confirm(`Удалить проект «${name || 'проект'}»?`)) return;
    try {
      const r = await fetch('/api/assistant/projects/' + pid + '/', {
        method: 'DELETE',
        headers: {'X-CSRFToken': csrf()},
        credentials: 'same-origin',
      });
      if (!r.ok && r.status !== 204) {
        alert('Не удалось удалить (HTTP ' + r.status + ')');
        return;
      }
      await loadProjects();
    } catch(err) {
      alert('Ошибка: ' + (err && err.message ? err.message : err));
    }
  };



  // Category icons → визуальное отделение admin/purchase/support от обычных
  const CATEGORY_ICON = {
    admin:    '🛡',
    purchase: '🛒',
    support:  '🎧',
    general:  '💬',
  };

  // Группировка чатов по датам (ChatGPT/Linear-style).
  function _bucketForDate(d, now) {
    if (!d) return 'older';
    const ms = now - d;
    const dStart = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const nStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const daysAgo = Math.floor((nStart - dStart) / 86400000);
    if (daysAgo <= 0) return 'today';
    if (daysAgo === 1) return 'yesterday';
    if (daysAgo <= 7) return 'week';
    if (daysAgo <= 30) return 'month';
    return 'older';
  }

  const _BUCKET_LABELS = {
    today: tr('bucket.today'),
    yesterday: tr('bucket.yesterday'),
    week: tr('bucket.week'),
    month: tr('bucket.month'),
    older: tr('bucket.older'),
  };
  const _BUCKET_ORDER = ['today', 'yesterday', 'week', 'month', 'older'];

  function renderConvList(filter='') {
    const f = filter.toLowerCase();
    const list = state.convs.filter(c => !f || (c.title||'').toLowerCase().includes(f));
    const clearBtn = $('clearHistoryBtn');
    if (clearBtn) clearBtn.style.display = (state.convs && state.convs.length) ? '' : 'none';
    if (!list.length) {
      $('convList').innerHTML = '<div class="side-item-stack"><div class="side-item-stack-meta">' + tr('Нет чатов') + '</div></div>';
      return;
    }
    const now = new Date();
    const buckets = {today:[], yesterday:[], week:[], month:[], older:[]};
    for (const c of list.slice(0, 60)) {
      const d = c.updated_at ? new Date(c.updated_at) : null;
      buckets[_bucketForDate(d, now)].push(c);
    }
    const renderItem = (c) => {
      const date = c.updated_at ? new Date(c.updated_at) : null;
      const meta = date ? relativeTime(date) : '';
      const lastMeta = c.last_message ? c.last_message.content.substring(0, 40) : meta;
      const icon = CATEGORY_ICON[c.category || 'general'] || '💬';
      const cid = esc(c.id);
      return `<div class="side-item-stack ${c.id === state.convId ? 'active' : ''}" data-conv-id="${cid}" onclick="openConv('${cid}')" oncontextmenu="return openConvCtxMenu(event,'${cid}')">
        <div class="side-item-stack-content">
          <div class="side-item-stack-title"><span class="conv-cat-icon" title="${esc(c.category || 'general')}">${icon}</span>${esc(c.title || tr('card.untitled'))}</div>
          <div class="side-item-stack-meta">${esc(meta)} ${lastMeta && lastMeta !== meta ? '· ' + esc(lastMeta) : ''}</div>
        </div>
        <button class="side-item-stack-more" type="button" title="Действия" onclick="event.stopPropagation();openConvCtxMenu(event,'${cid}');return false;" aria-label="Действия">⋯</button>
      </div>`;
    };
    const html = _BUCKET_ORDER
      .filter(k => buckets[k].length)
      .map(k => `<div class="conv-bucket"><div class="conv-bucket-label">${_BUCKET_LABELS[k]}</div>${buckets[k].map(renderItem).join('')}</div>`)
      .join('');
    $('convList').innerHTML = html;
  }

  // ── Контекст-меню чата (rename/delete) ───────────────────
  let _ctxConvId = null;

  window.openConvCtxMenu = function(ev, convId) {
    ev.preventDefault();
    ev.stopPropagation();
    const menu = $('convCtxMenu');
    if (!menu) return false;
    _ctxConvId = convId;
    // Подсвечиваем кнопку «⋯» у активного элемента
    document.querySelectorAll('.side-item-stack-more.open').forEach(b => b.classList.remove('open'));
    const item = document.querySelector(`.side-item-stack[data-conv-id="${convId}"] .side-item-stack-more`);
    if (item) item.classList.add('open');
    // Позиционирование
    menu.hidden = false;
    const rect = menu.getBoundingClientRect();
    const w = rect.width || 200, h = rect.height || 80;
    let x, y;
    if (ev.clientX || ev.clientY) {
      x = ev.clientX; y = ev.clientY;
    } else {
      const tr = (ev.currentTarget || ev.target).getBoundingClientRect();
      x = tr.right; y = tr.bottom;
    }
    // не выходить за границы окна
    x = Math.min(x, window.innerWidth - w - 8);
    y = Math.min(y, window.innerHeight - h - 8);
    menu.style.left = Math.max(8, x) + 'px';
    menu.style.top  = Math.max(8, y) + 'px';
    return false;
  };

  function closeConvCtxMenu() {
    const menu = $('convCtxMenu');
    if (menu) menu.hidden = true;
    document.querySelectorAll('.side-item-stack-more.open').forEach(b => b.classList.remove('open'));
    _ctxConvId = null;
  }

  // Глобальные обработчики для закрытия меню
  document.addEventListener('click', (e) => {
    const menu = $('convCtxMenu');
    if (!menu || menu.hidden) return;
    if (!menu.contains(e.target)) closeConvCtxMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeConvCtxMenu();
  });
  // Делегирование кликов внутри меню
  document.addEventListener('click', (e) => {
    const item = e.target.closest('#convCtxMenu .ctx-menu-item');
    if (!item) return;
    const action = item.dataset.action;
    const cid = _ctxConvId;
    closeConvCtxMenu();
    if (!cid) return;
    const conv = (state.convs || []).find(c => c.id === cid);
    const title = conv ? (conv.title || tr('card.untitled')) : '';
    if (action === 'rename') renameConv(cid, title);
    else if (action === 'delete') deleteConv(cid, title);
  });

  // Переименование чата
  window.renameConv = async (id, currentTitle) => {
    const v = prompt(tr('prompt.rename_chat'), currentTitle || '');
    if (v === null) return;
    const t = v.trim();
    if (!t) return;
    if (t === currentTitle) return;
    try {
      const res = await fetch('/api/assistant/conversations/' + id + '/', {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf()},
        credentials: 'same-origin',
        body: JSON.stringify({title: t.slice(0, 200)}),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      // обновить state и перерисовать
      const i = (state.convs || []).findIndex(c => c.id === id);
      if (i >= 0) state.convs[i] = {...state.convs[i], title: data.title || t};
      renderConvList($('convSearch') ? $('convSearch').value : '');
    } catch(e) {
      alert('Не удалось переименовать чат: ' + e.message);
    }
  };

  // Сабмит 3 полей (название/бренд/модель техники) из inline-формы.
  // Юзер заполнил хоть что-то и нажал Enter → шлём в чат структурно,
  // Toggle для collapsible cards (ls-card и rl-card): разворачивает/сворачивает body.
  // Ищем ближайшую карточку с классом .ls-collapsible или .rl-collapsible.
  window.__toggleCollapsibleCard = (headEl) => {
    if (!headEl) return;
    const card = headEl.closest('.ls-collapsible, .rl-collapsible');
    if (!card) return;
    const body = card.querySelector('.ls-coll-body, .rl-coll-body');
    const chev = card.querySelector('.ls-coll-chev, .rl-coll-chev');
    const isCollapsed = card.getAttribute('data-collapsed') === '1';
    if (isCollapsed) {
      card.setAttribute('data-collapsed', '0');
      if (body) body.style.display = '';
      if (chev) chev.textContent = '▾';
    } else {
      card.setAttribute('data-collapsed', '1');
      if (body) body.style.display = 'none';
      if (chev) chev.textContent = '▸';
    }
  };

  // Inline-toggle для checklist-строки: визуально мгновенно переключаем
  // галочку + fire-and-forget POST на backend (без перезагрузки карточки).
  window.toggleChecklistRow = async (rowEl, action, paramsJson) => {
    if (!rowEl) return;
    const cb = rowEl.querySelector('.ls-checkbox');
    const title = rowEl.querySelector('.ls-title');
    const sub = rowEl.querySelector('.ls-sub');
    if (!cb) return;
    const willCheck = !cb.classList.contains('ls-checkbox-on');
    cb.classList.toggle('ls-checkbox-on', willCheck);
    cb.textContent = willCheck ? '✓' : '';
    if (title) title.classList.toggle('ls-title-done', willCheck);
    if (sub)   sub.textContent = willCheck ? 'Проверено' : 'Кликнуть → отметить как проверено';
    // Шлём backend silently — результат игнорируем
    try {
      let params = {};
      try { params = JSON.parse(paramsJson || '{}'); } catch(_) {}
      await api('/api/assistant/action/', {
        method: 'POST',
        body: JSON.stringify({action, params, silent: true}),
      });
    } catch (e) { /* silent */ }
  };

  // оператор/AI получает максимум контекста.
  // silentIfEmpty=true (вызов из blur модели) — не шлём если всё пусто.
  window.submitPartFields = (anyInput, silentIfEmpty) => {
    if (!anyInput) return;
    const wrap = anyInput.closest('.rcard-it-fields');
    if (!wrap || wrap.dataset.submitted === '1') return;
    const get = (f) => {
      const el = wrap.querySelector(`[data-field="${f}"]`);
      return el ? (el.value || '').trim() : '';
    };
    const name  = get('name');
    const brand = get('brand');
    const model = get('model');
    if (!name && !brand && !model) return;  // пусто — игнор
    wrap.dataset.submitted = '1';
    const art = wrap.dataset.article || '';
    // Собираем человекочитаемую строку
    const parts = [];
    if (name)  parts.push(name);
    if (brand) parts.push(`бренд: ${brand}`);
    if (model) parts.push(`для ${model}`);
    const summary = parts.join(' · ');
    // Заменяем форму на текст-подтверждение
    const span = document.createElement('div');
    span.className = 'rcard-it-name rcard-it-name-matched';
    span.innerHTML = '✓ ' + summary.replace(/[<>&"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
    wrap.replaceWith(span);
    // Отправляем в чат
    const chatInp = document.getElementById('input');
    if (chatInp) {
      chatInp.value = `Позиция ${art}: ${summary}`;
      const btn = document.getElementById('sendBtn');
      if (btn) btn.click();
    }
  };

  // Inline-удаление строки в списке RFQ: убираем строку из DOM, обновляем
  // счётчик, fire-and-forget POST к cancel_rfq. Никакой новой карточки в чат.
  window.deleteRfqRowInline = async (btn, rfqId) => {
    const wrap = btn.closest('.rl-row-wrap');
    if (!wrap) return;
    // Анимация исчезновения
    wrap.style.transition = 'opacity 0.18s, transform 0.18s, max-height 0.22s, padding 0.22s';
    wrap.style.overflow = 'hidden';
    wrap.style.maxHeight = wrap.offsetHeight + 'px';
    // Принудительный reflow чтобы transition сработал
    void wrap.offsetHeight;
    wrap.style.opacity = '0';
    wrap.style.transform = 'translateX(8px)';
    wrap.style.maxHeight = '0';
    wrap.style.paddingTop = '0';
    wrap.style.paddingBottom = '0';
    // Обновляем счётчик в шапке (rl-head-count) в той же карточке
    const card = wrap.closest('.rl-card');
    if (card) {
      const cntEl = card.querySelector('.rl-head-count');
      if (cntEl) {
        const n = parseInt(cntEl.textContent || '0', 10);
        if (!isNaN(n) && n > 0) cntEl.textContent = String(n - 1);
      }
    }
    // Через 220мс убираем элемент из DOM полностью
    setTimeout(() => { try { wrap.remove(); } catch(_){} }, 240);
    // Параллельно шлём cancel на бэк (fire-and-forget — UI не ждёт)
    try {
      await fetch('/api/assistant/action/', {
        method: 'POST',
        headers: {'Content-Type':'application/json','X-CSRFToken': csrf()},
        credentials: 'same-origin',
        body: JSON.stringify({
          action: 'cancel_rfq',
          params: {rfq_id: rfqId, silent: true},
          conversation_id: state.convId,
        }),
      });
    } catch(e) { /* молча — UI уже обновился */ }
  };

  // Inline-отмена заказа: убираем строку, fire-and-forget cancel_order.
  window.deleteOrderRowInline = async (btn, orderId) => {
    const wrap = btn.closest('.rl-row-wrap');
    if (!wrap) return;
    wrap.style.transition = 'opacity 0.18s, transform 0.18s, max-height 0.22s, padding 0.22s';
    wrap.style.overflow = 'hidden';
    wrap.style.maxHeight = wrap.offsetHeight + 'px';
    void wrap.offsetHeight;
    wrap.style.opacity = '0';
    wrap.style.transform = 'translateX(8px)';
    wrap.style.maxHeight = '0';
    wrap.style.paddingTop = '0';
    wrap.style.paddingBottom = '0';
    const card = wrap.closest('.rl-card');
    if (card) {
      const cntEl = card.querySelector('.rl-head-count');
      if (cntEl) {
        const n = parseInt(cntEl.textContent || '0', 10);
        if (!isNaN(n) && n > 0) cntEl.textContent = String(n - 1);
      }
    }
    setTimeout(() => { try { wrap.remove(); } catch(_){} }, 240);
    try {
      await fetch('/api/assistant/action/', {
        method: 'POST',
        headers: {'Content-Type':'application/json','X-CSRFToken': csrf()},
        credentials: 'same-origin',
        body: JSON.stringify({
          action: 'cancel_order',
          params: {order_id: orderId, silent: true},
          conversation_id: state.convId,
        }),
      });
    } catch(e) { /* silent */ }
  };

  // Удаление одного чата (soft delete)
  window.deleteConv = async (id, title) => {
    const t = (title || '').trim() || 'этот чат';
    if (!confirm(`Удалить «${t}»?\nЧат и его история будут скрыты.`)) return;
    try {
      const res = await fetch('/api/assistant/conversations/' + id + '/', {
        method: 'DELETE',
        headers: {'X-CSRFToken': csrf()},
        credentials: 'same-origin',
      });
      if (!res.ok && res.status !== 204) throw new Error('HTTP ' + res.status);
      // если удалили активный — уйти на welcome
      if (state.convId === id) {
        try { sessionStorage.removeItem('cf_active_conv'); } catch(_){}
        state.convId = null;
        if (typeof showWelcome === 'function') showWelcome();
        else if ($('streamInner')) $('streamInner').innerHTML = '';
      }
      // убрать из state и перерисовать сразу (optimistic update)
      state.convs = (state.convs || []).filter(c => c.id !== id);
      renderConvList($('convSearch') ? $('convSearch').value : '');
      // Дополнительно: перезагружаем список с сервера — чтобы исключить
      // случаи, когда WS-handler или другой action вернул удалённую запись
      // обратно в state из-за гонки.
      try { await loadConvList(); } catch(_){}
    } catch(e) {
      alert('Не удалось удалить чат: ' + e.message);
    }
  };

  // Массовое удаление всей истории
  window.clearAllHistory = async () => {
    const n = (state.convs || []).length;
    if (!n) return;
    if (!confirm(`Удалить всю историю поисков (${n} чатов)?\nЭто действие нельзя отменить.`)) return;
    const ids = state.convs.map(c => c.id);
    let failed = 0;
    await Promise.all(ids.map(async (id) => {
      try {
        const res = await fetch('/api/assistant/conversations/' + id + '/', {
          method: 'DELETE',
          headers: {'X-CSRFToken': csrf()},
          credentials: 'same-origin',
        });
        if (!res.ok && res.status !== 204) failed++;
      } catch(_) { failed++; }
    }));
    try { localStorage.removeItem('cf_active_conv'); } catch(_){}
    state.convId = null;
    state.convs = [];
    if (typeof showWelcome === 'function') showWelcome();
    else if ($('streamInner')) $('streamInner').innerHTML = '';
    renderConvList();
    if (failed) alert(`Не удалось удалить ${failed} чат(ов)`);
  };

  function relativeTime(date) {
    const now = new Date();
    const diff = (now - date) / 1000;
    if (diff < 60) return 'только что';
    if (diff < 3600) return Math.floor(diff/60) + ' мин назад';
    if (diff < 86400) return Math.floor(diff/3600) + ' ч назад';
    if (diff < 604800) return Math.floor(diff/86400) + ' дн назад';
    return date.toLocaleDateString('ru-RU', {day:'2-digit', month:'short'});
  }

  window.filterConvs = renderConvList;

  window.openConv = async (id) => {
    // Юзер явно открыл conv — снимаем sticky welcome, чтобы F5 теперь
    // показывал эту conv, а не welcome.
    try { sessionStorage.removeItem('cf_on_welcome'); } catch(_){}
    setConvId(id);
    showConv();
    $('streamInner').innerHTML = '';
    _abandonStream();   // бросаем in-flight стрим: иначе onclose даст ложный recovery
    if (state.ws) { try { state.ws.close(); } catch(e){} }
    try {
      const data = await api('/api/assistant/conversations/' + id + '/');
      // Дедуп идентичных action+response пар. Если юзер кликал «Депозит» /
      // «Обновить» N раз → в БД N action'ов + N откликов с одинаковыми cards.
      // Рендерим ТОЛЬКО последнюю пару (свежий timestamp), скрываем все
      // более ранние action-сообщения и их одинаковые ответы.
      const allMessages = data.messages || [];

      // Fingerprint assistant по cards (text может содержать меняющийся timestamp)
      const respFp = (m) => {
        if (m.role !== 'assistant') return null;
        const cardsKey = (m.cards || []).map(c => {
          const d = (c && c.data) || {};
          return (c.type || '') + '|' + (d.title || '');
        }).join('~');
        return cardsKey || null;
      };
      // Fingerprint action по action-name + params
      const actFp = (m) => {
        if (m.role !== 'action') return null;
        try {
          const a = (m.actions || [])[0] || {};
          const aName = a.action || '';
          if (!aName) return null;
          const p = a.params || {};
          // только значимые ключи (не сериализуем сложные объекты)
          const pKey = Object.keys(p).filter(k => !k.startsWith('_')).sort()
            .map(k => k + '=' + p[k]).join('&');
          return aName + '?' + pKey;
        } catch(_) { return null; }
      };

      // Найдём индексы ПОСЛЕДНЕГО occurrence для action и для response
      const lastActIdx = new Map();
      const lastRespIdx = new Map();
      allMessages.forEach((m, i) => {
        const af = actFp(m);
        if (af) lastActIdx.set(af, i);
        const rf = respFp(m);
        if (rf) lastRespIdx.set(rf, i);
      });

      const seen = new Set();
      allMessages.forEach((m, i) => {
        const mid = m.id || m.message_id;
        if (mid && seen.has(mid)) return;
        if (mid) seen.add(mid);
        const af = actFp(m);
        if (af && lastActIdx.get(af) !== i) return;   // не последний action
        const rf = respFp(m);
        if (rf && lastRespIdx.get(rf) !== i) return;  // не последний response
        addMessage(m.role, m.content, m.cards, m.actions, m.context_refs);
      });
    } catch(e){}
    // Контент разговора отрисован — только теперь снимаем спиннер/анти-мелькание,
    // чтобы между спиннером и кабинетом не было пустого тёмного экрана.
    document.documentElement.classList.remove('cf-restoring');
    connectWS();
    renderConvList($('convSearch').value);
    if (isMobile()) toggleSidebar(false);
  };

  window.newChat = () => {
    setConvId(null);
    showWelcome();
    _abandonStream();   // бросаем in-flight стрим: иначе onclose даст ложный recovery
    if (state.ws) { try { state.ws.close(); } catch(e){} }
    connectWS();
    renderConvList();
    if (isMobile()) toggleSidebar(false);
    setTimeout(() => $('heroInput').focus(), 100);
  };

  // ══════════════════════════════════════════════════════════
  // Voice + file
  // ══════════════════════════════════════════════════════════
  let recog = null;
  let mediaRec = null;
  let recordedChunks = [];

  window.toggleVoice = async () => {
    // Если уже идёт серверная запись — остановить и отправить
    if (mediaRec && mediaRec.state === 'recording') {
      mediaRec.stop();
      return;
    }
    // Web Speech API — если есть, используем (бесплатно, on-device)
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SR) {
      if (recog) { recog.stop(); recog = null; return; }
      recog = new SR();
      recog.lang = document.documentElement.lang === 'en' ? 'en-US' : 'ru-RU';
      recog.interimResults = true;
      recog.onresult = (e) => {
        const text = Array.from(e.results).map(r => r[0].transcript).join('');
        const target = $('welcomeStage').classList.contains('hidden') ? $('input') : $('heroInput');
        target.value = text;
        if (typeof updateHeroIcon === 'function') updateHeroIcon();
      };
      recog.onend = () => { recog = null; };
      recog.start();
      return;
    }
    // Fallback: пишем через MediaRecorder и шлём на сервер для Whisper
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      alert('Голосовой ввод не поддерживается этим браузером');
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio: true});
      mediaRec = new MediaRecorder(stream, {mimeType: 'audio/webm'});
      recordedChunks = [];
      mediaRec.ondataavailable = (e) => { if (e.data.size > 0) recordedChunks.push(e.data); };
      mediaRec.onstop = async () => {
        stream.getTracks().forEach(t => t.stop());
        const blob = new Blob(recordedChunks, {type: 'audio/webm'});
        recordedChunks = [];
        const fd = new FormData();
        fd.append('audio', blob, 'voice.webm');
        try {
          const res = await fetch('/api/assistant/transcribe-audio/', {
            method: 'POST',
            headers: {'X-CSRFToken': csrf()},
            body: fd, credentials: 'same-origin',
          });
          const d = await res.json();
          if (d.error && !d.text) {
            alert(d.error);
            return;
          }
          const target = $('welcomeStage').classList.contains('hidden') ? $('input') : $('heroInput');
          target.value = d.text || '';
          if (typeof updateHeroIcon === 'function') updateHeroIcon();
        } catch(err) {
          alert('Не удалось расшифровать: ' + err.message);
        }
      };
      mediaRec.start();
    } catch(err) {
      alert('Доступ к микрофону отклонён: ' + (err.message || err));
    }
  };

  // XHR-обёртка для upload c прогрессом + abort + network-error handler.
  // fetch() не отдаёт upload.onprogress, поэтому идём через XMLHttpRequest.
  function _uploadWithProgress(url, formData, {onProgress, onSuccess, onError}) {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url, true);
    xhr.setRequestHeader('X-CSRFToken', csrf());
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const pct = Math.round((e.loaded / e.total) * 100);
      onProgress && onProgress(pct, e.loaded, e.total);
    };
    xhr.onload = () => {
      let data; try { data = JSON.parse(xhr.responseText); } catch { data = {}; }
      if (xhr.status >= 200 && xhr.status < 300) onSuccess && onSuccess(data);
      else onError && onError(new Error(data.error || ('HTTP ' + xhr.status)));
    };
    xhr.onerror = () => onError && onError(new Error('сеть недоступна'));
    xhr.ontimeout = () => onError && onError(new Error('таймаут загрузки'));
    xhr.timeout = 120000;  // 2 минуты на большой xlsx
    xhr.send(formData);
    return xhr;
  }

  function uploadSpec(file) {
    showConv();
    addMessage('user', '📎 ' + file.name + ' (' + Math.round(file.size/1024) + ' KB)');
    // Сообщение-плейсхолдер с прогресс-баром (обновляется по ходу загрузки).
    // HTML задаём напрямую в .msg-content — addMessage экранирует контент (linkifyEntities).
    const pending = addMessage('assistant', 'Загружаю файл…');
    const _spEl = pending && pending.querySelector && pending.querySelector('.msg-content');
    if (_spEl) _spEl.innerHTML =
      'Загружаю файл… <span class="upl-pct">0%</span><div class="upl-bar"><div class="upl-bar-fill" style="width:0%"></div></div>';
    const fd = new FormData();
    fd.append('file', file);
    if (state.convId) fd.append('conversation_id', state.convId);

    _uploadWithProgress('/api/assistant/upload-spec/', fd, {
      onProgress: (pct) => {
        if (!pending) return;
        const txtEl = pending.querySelector('.upl-pct');
        const barEl = pending.querySelector('.upl-bar-fill');
        if (txtEl) txtEl.textContent = pct + '%';
        if (barEl) barEl.style.width = pct + '%';
        // Когда загрузка завершилась — меняем подпись на «Парсю файл…»
        if (pct >= 100) {
          const tEl = pending.querySelector('.msg-content') || pending;
          // Не перетираем содержимое полностью — точно знаем структуру плейсхолдера
          const html = 'Парсю файл и ищу артикулы в каталоге…';
          if (tEl) tEl.innerHTML = html;
        }
      },
      onSuccess: (data) => {
        if (pending && pending.parentNode) pending.remove();
        if (data.error) {
          addMessage('assistant', '⚠️ ' + data.error);
          return;
        }
        if (data.conversation_id) {
          setConvId(data.conversation_id);
          _abandonStream();   // бросаем in-flight стрим: иначе onclose даст ложный recovery
          if (state.ws) { try { state.ws.close(); } catch(e){} }
          connectWS();
        }
        addMessage('assistant', data.text || 'Готово.',
                   data.cards || [], data.actions || [], [],
                   data.message_id || null, data.suggestions || []);
        renderConvList();
      },
      onError: (err) => {
        if (pending && pending.parentNode) pending.remove();
        addMessage('assistant', '⚠️ Не удалось обработать файл: ' + (err.message || err));
      },
    });
  }

  // Smart Price Import — трёхэтапный flow:
  // 1) POST файл → AI-маппинг (tool use) → показываем маппинг + вопросы
  // 2) Оператор отвечает на вопросы / подтверждает → commit
  // 3) Backend применяет формулы + импортирует → результат
  // Морпорты с UN/LOCODE (5-буквенный международный код) — по странам.
  // Юзер выбирает страну, потом получает фильтрованный список портов.
  // Известные бренды-аналоги (зеркало KNOWN_ANALOG_BRANDS в pricelist.py).
  // Нужно только для сводки «Ваши ответы»: показать, будет ли завод виден
  // покупателю. Реальное решение по видимости принимает бэкенд.
  var KNOWN_ANALOG_BRANDS_JS = {};
  ['itr','berco','carraro','dana','denso','bosch','bosch rexroth','rexroth',
   'perkins','etp','cummins','deutz','donaldson','fleetguard','skf','nok',
   'ntn','nsk','parker','kyb','kayaba','mahle','mann-filter','mann','garrett',
   'holset','wabco','sachs','hengst','eaton','vickers','lincoln','fleetpride',
   'federal-mogul'].forEach(function(b){ KNOWN_ANALOG_BRANDS_JS[b] = 1; });

  var PORTS_BY_COUNTRY = {
    AE: {
      flag: '🇦🇪', name: 'ОАЭ',
      sea: [
        {code: 'AEJEA', name: 'Jebel Ali', city: 'Дубай'},
        {code: 'AEPRA', name: 'Port Rashid', city: 'Дубай'},
        {code: 'AEKHL', name: 'Khor Fakkan', city: 'Шарджа'},
        {code: 'AESHJ', name: 'Hamriyah Port', city: 'Шарджа'},
        {code: 'AEKLF', name: 'Khalifa Port', city: 'Абу-Даби'},
        {code: 'AEFJR', name: 'Port of Fujairah', city: 'Фуджейра'},
        {code: 'AEMZD', name: 'Mina Zayed', city: 'Абу-Даби'},
        {code: 'AEAJM', name: 'Ajman Port', city: 'Аджман'},
      ],
      air: [
        {code: 'DXB', name: 'Dubai International', city: 'Дубай'},
        {code: 'DWC', name: 'Al Maktoum International', city: 'Дубай'},
        {code: 'AUH', name: 'Zayed International', city: 'Абу-Даби'},
        {code: 'SHJ', name: 'Sharjah International', city: 'Шарджа'},
        {code: 'RKT', name: 'Ras Al Khaimah', city: 'Рас-эль-Хайма'},
      ],
    },
    CN: {
      flag: '🇨🇳', name: 'Китай',
      sea: [
        {code: 'CNSHA', name: 'Shanghai (Yangshan)', city: '上海 Шанхай'},
        {code: 'CNWAI', name: 'Shanghai Waigaoqiao', city: '上海 Шанхай'},
        {code: 'CNNGB', name: 'Ningbo-Zhoushan', city: '宁波 Нинбо'},
        {code: 'CNYTN', name: 'Yantian', city: '深圳 Шэньчжэнь'},
        {code: 'CNSKU', name: 'Shekou', city: '深圳 Шэньчжэнь'},
        {code: 'CNCWN', name: 'Chiwan', city: '深圳 Шэньчжэнь'},
        {code: 'CNNSA', name: 'Nansha', city: '广州 Гуанчжоу'},
        {code: 'CNHUA', name: 'Huangpu', city: '广州 Гуанчжоу'},
        {code: 'CNTAO', name: 'Qingdao Qianwan', city: '青岛 Циндао'},
        {code: 'CNTXG', name: 'Tianjin Xingang', city: '天津 Тяньцзинь'},
        {code: 'CNDLC', name: 'Dalian DCT', city: '大连 Далянь'},
        {code: 'CNXMN', name: 'Xiamen Haicang', city: '厦门 Сямэнь'},
        {code: 'HKHKG', name: 'Hong Kong Kwai Tsing', city: '香港 Гонконг'},
        {code: 'CNLYG', name: 'Lianyungang', city: '连云港'},
        {code: 'CNFUZ', name: 'Fuzhou', city: '福州 Фучжоу'},
      ],
      air: [
        {code: 'PVG', name: 'Pudong International', city: '上海 Шанхай'},
        {code: 'PEK', name: 'Capital International', city: '北京 Пекин'},
        {code: 'PKX', name: 'Daxing International', city: '北京 Пекин'},
        {code: 'CAN', name: 'Baiyun International', city: '广州 Гуанчжоу'},
        {code: 'SZX', name: "Bao'an International", city: '深圳 Шэньчжэнь'},
        {code: 'HKG', name: 'Hong Kong International', city: '香港 Гонконг'},
        {code: 'HGH', name: 'Xiaoshan International', city: '杭州 Ханчжоу'},
        {code: 'CTU', name: 'Tianfu International', city: '成都 Чэнду'},
        {code: 'KMG', name: 'Changshui International', city: '昆明 Куньмин'},
        {code: 'XIY', name: 'Xianyang International', city: '西安 Сиань'},
      ],
    },
    RU: {
      flag: '🇷🇺', name: 'Россия',
      sea: [
        // Дальний Восток
        {code: 'RUVVO', name: 'ВМТП', city: 'Владивосток'},
        {code: 'RUVRP', name: 'ВМРП (рыбный)', city: 'Владивосток'},
        {code: 'RUPRV', name: 'Первомайский терминал', city: 'Владивосток'},
        {code: 'RUVCT', name: 'ВКТ (контейнерный)', city: 'Владивосток'},
        {code: 'RUVYP', name: 'Восточный', city: 'Находка'},
        {code: 'RUNJK', name: 'НМТП', city: 'Находка'},
        {code: 'RUZRB', name: 'Зарубино', city: 'Хасанский р-н'},
        {code: 'RUPSE', name: 'Посьет', city: 'Хасанский р-н'},
        {code: 'RUSLA', name: 'Славянка', city: 'Приморский край'},
        {code: 'RUBKM', name: 'Большой Камень', city: 'Приморский край'},
        {code: 'RUVNN', name: 'Ванино', city: 'Хабаровский край'},
        {code: 'RUSOV', name: 'Советская Гавань', city: 'Хабаровский край'},
        // Юг
        {code: 'RUNVS', name: 'НМТП', city: 'Новороссийск'},
        {code: 'RUSHX', name: 'Шесхарис', city: 'Новороссийск'},
        {code: 'RUNUT', name: 'НУТЭП', city: 'Новороссийск'},
        {code: 'RUTMN', name: 'Порт Тамань', city: 'Краснодарский край'},
        {code: 'RUTUA', name: 'Туапсе', city: 'Краснодарский край'},
        {code: 'RUTMK', name: 'Темрюк', city: 'Краснодарский край'},
        {code: 'RUKVK', name: 'Кавказ', city: 'Краснодарский край'},
        {code: 'RUROV', name: 'Ростов-на-Дону', city: 'Дон'},
        // Балтика
        {code: 'RULED', name: 'Большой порт', city: 'Санкт-Петербург'},
        {code: 'RUFCT', name: 'ПКТ (контейнерный)', city: 'Санкт-Петербург'},
        {code: 'RUPLP', name: 'Петролеспорт', city: 'Санкт-Петербург'},
        {code: 'RUBRO', name: 'Бронка', city: 'Санкт-Петербург'},
        {code: 'RUULU', name: 'Усть-Луга', city: 'Ленинградская обл.'},
        {code: 'RUUL2', name: 'Усть-Луга ЮГ-2', city: 'Ленинградская обл.'},
        {code: 'RUKGD', name: 'КМТП', city: 'Калининград'},
        {code: 'RUBLT', name: 'Балтийск', city: 'Калининград'},
        // Север
        {code: 'RUMMK', name: 'ММТП', city: 'Мурманск'},
        {code: 'RUARH', name: 'Архангельск', city: 'Архангельск'},
        {code: 'RUKDA', name: 'Кандалакша', city: 'Мурманская обл.'},
      ],
      air: [
        {code: 'SVO', name: 'Шереметьево', city: 'Москва'},
        {code: 'DME', name: 'Домодедово', city: 'Москва'},
        {code: 'VKO', name: 'Внуково', city: 'Москва'},
        {code: 'LED', name: 'Пулково', city: 'Санкт-Петербург'},
        {code: 'VVO', name: 'Владивосток', city: 'Владивосток'},
        {code: 'KJA', name: 'Емельяново', city: 'Красноярск'},
        {code: 'OVB', name: 'Толмачёво', city: 'Новосибирск'},
        {code: 'KZN', name: 'Казань', city: 'Казань'},
        {code: 'KHV', name: 'Хабаровск', city: 'Хабаровск'},
        {code: 'EKB', name: 'Кольцово', city: 'Екатеринбург'},
        {code: 'AER', name: 'Сочи', city: 'Сочи'},
      ],
    },
    NL: {
      flag: '🇳🇱', name: 'Нидерланды',
      sea: [
        {code: 'NLRTM', name: 'Rotterdam (Maasvlakte II)', city: 'Роттердам'},
        {code: 'NLRTA', name: 'Rotterdam APMT/RWG/ECT', city: 'Роттердам'},
        {code: 'NLAMS', name: 'Amsterdam Westpoort', city: 'Амстердам'},
        {code: 'NLVLS', name: 'Vlissingen', city: 'Влиссинген'},
      ],
      air: [
        {code: 'AMS', name: 'Schiphol', city: 'Амстердам'},
        {code: 'EIN', name: 'Eindhoven', city: 'Эйндховен'},
        {code: 'RTM', name: 'Rotterdam The Hague', city: 'Роттердам'},
      ],
    },
    TR: {
      flag: '🇹🇷', name: 'Турция',
      sea: [
        {code: 'TRAMB', name: 'Ambarli (Marport/Kumport)', city: 'Стамбул'},
        {code: 'TRHAY', name: 'Haydarpaşa', city: 'Стамбул'},
        {code: 'TRMER', name: 'MIP International', city: 'Мерсин'},
        {code: 'TRIZM', name: 'Aliaga Nemrut Bay', city: 'Измир'},
        {code: 'TRGEB', name: 'Gebze (Yilport/Evyap)', city: 'Коджаэли'},
        {code: 'TRDER', name: 'Derince', city: 'Коджаэли'},
        {code: 'TRSAN', name: 'Iskenderun', city: 'Хатай'},
      ],
      air: [
        {code: 'IST', name: 'Istanbul Airport', city: 'Стамбул'},
        {code: 'SAW', name: 'Sabiha Gökçen', city: 'Стамбул'},
        {code: 'ESB', name: 'Esenboğa', city: 'Анкара'},
        {code: 'ADB', name: 'Adnan Menderes', city: 'Измир'},
        {code: 'AYT', name: 'Antalya', city: 'Анталья'},
      ],
    },
    KZ: {
      flag: '🇰🇿', name: 'Казахстан',
      sea: [
        {code: 'KZAKT', name: 'Морской торговый порт', city: 'Актау'},
        {code: 'KZKUR', name: 'Курык (мультимодальный)', city: 'Курык'},
      ],
      air: [
        {code: 'ALA', name: 'Almaty International', city: 'Алматы'},
        {code: 'NQZ', name: 'Astana International', city: 'Астана'},
        {code: 'CIT', name: 'Shymkent', city: 'Шымкент'},
        {code: 'KGF', name: 'Sary-Arka', city: 'Караганда'},
        {code: 'AKX', name: 'Aktobe', city: 'Актобе'},
      ],
    },
  };

  // Flatten в формат "AEJEA · Jebel Ali · Дубай · 🇦🇪 ОАЭ" — для datalist
  // (searchable by code, name, city, country)
  function _flattenPorts(kind) {
    var out = [];
    for (var cc in PORTS_BY_COUNTRY) {
      var country = PORTS_BY_COUNTRY[cc];
      (country[kind] || []).forEach(function(p) {
        out.push(p.code + ' · ' + p.name + ' · ' + p.city
               + ' · ' + country.flag + ' ' + country.name);
      });
    }
    return out;
  }
  window.getPortsByCountry = function(cc, kind) {
    var country = PORTS_BY_COUNTRY[cc];
    if (!country) return _flattenPorts(kind);
    return (country[kind] || []).map(function(p) {
      return p.code + ' · ' + p.name + ' · ' + p.city
           + ' · ' + country.flag + ' ' + country.name;
    });
  };

  var SEA_PORTS = _flattenPorts('sea');
  var AIR_PORTS = _flattenPorts('air');

  // LEGACY (для совместимости, можно удалить позже)
  var _OLD_SEA_PORTS = [
    // 🇦🇪 ОАЭ
    'Jebel Ali · Container Terminal 1-4 (Дубай, ОАЭ)',
    'Port Rashid (Дубай, ОАЭ)',
    'Khor Fakkan · East Coast Terminal (Шарджа, ОАЭ)',
    'Hamriyah · Port (Шарджа, ОАЭ)',
    'Khalifa Port · Khalifa Bin Salman (Абу-Даби, ОАЭ)',
    'Fujairah · Port of Fujairah (ОАЭ)',
    // 🇨🇳 Китай
    'Shanghai · Yangshan (Deep Water) / 上海洋山 (КНР)',
    'Shanghai · Waigaoqiao / 上海外高桥 (КНР)',
    'Ningbo-Zhoushan / 宁波舟山 (Нинбо, КНР)',
    'Shenzhen · Yantian / 深圳盐田 (КНР)',
    'Shenzhen · Shekou / 深圳蛇口 (КНР)',
    'Shenzhen · Chiwan / 深圳赤湾 (КНР)',
    'Guangzhou · Nansha / 广州南沙 (КНР)',
    'Guangzhou · Huangpu / 广州黄埔 (КНР)',
    'Qingdao · Qianwan / 青岛前湾 (КНР)',
    'Tianjin · Xingang / 天津新港 (КНР)',
    'Dalian · DCT / 大连集装箱 (КНР)',
    'Xiamen · Haicang / 厦门海沧 (КНР)',
    'Hong Kong · Kwai Tsing / 香港葵青 (Гонконг)',
    'Lianyungang / 连云港 (КНР)',
    // 🇷🇺 Россия — Дальний Восток
    'ВМТП · Владивосток Морской Торговый Порт (РФ)',
    'ВМРП · Владивосток Рыбный Порт (РФ)',
    'Первомайский терминал · Владивосток (РФ)',
    'ВКТ · Владивостокский Контейнерный Терминал (РФ)',
    'Восточный (порт Восточный, Находка) (РФ)',
    'Восточная Стивидорная Компания · Находка (РФ)',
    'НМТП · Находкинский Морской Торговый Порт (РФ)',
    'Зарубино (Хасанский р-н, РФ)', 'Посьет (РФ)',
    'Славянка (РФ)', 'Большой Камень (РФ)',
    'Ванино (Хабаровский край, РФ)', 'Советская Гавань (РФ)',
    // 🇷🇺 Россия — Юг
    'НМТП · Новороссийский Морской Торговый Порт (РФ)',
    'Шесхарис · Новороссийск (РФ)',
    'НУТЭП · Новороссийск (РФ)',
    'Тамань (Краснодарский край, РФ)',
    'Ростов-на-Дону (РФ)', 'Туапсе (РФ)', 'Темрюк (РФ)',
    'Кавказ (порт Кавказ, РФ)',
    // 🇷🇺 Россия — Балтика
    'Большой порт Санкт-Петербург (РФ)',
    'ПКТ · Первый Контейнерный Терминал · СПб (РФ)',
    'Петролеспорт · СПб (РФ)',
    'Бронка · СПб (РФ)',
    'Морской рыбный порт · СПб (РФ)',
    'Усть-Луга (Многопрофильный, РФ)',
    'Усть-Луга · ЮГ-2 (РФ)',
    'Калининград · КМТП (РФ)',
    'Балтийск (РФ)', 'Светлый (РФ)',
    // 🇷🇺 Россия — Север
    'Мурманск · ММТП (РФ)', 'Архангельск (РФ)',
    'Кандалакша (РФ)',
    // 🇳🇱 Нидерланды
    'Rotterdam · Maasvlakte II (NL)',
    'Rotterdam · APMT / RWG / ECT (NL)',
    'Amsterdam · Westpoort (NL)',
    // 🇹🇷 Турция
    'Стамбул · Ambarli (Marport/Kumport) (Турция)',
    'Стамбул · Haydarpaşa (Турция)',
    'Mersin · MIP International Port (Турция)',
    'Izmir · Aliaga Nemrut Bay (Турция)',
    'Kocaeli · Gebze (Yilport, Evyap) (Турция)',
    'Kocaeli · Derince (Турция)',
    // 🇰🇿 Казахстан (Каспий)
    'Актау · Морской торговый порт (KZ)',
    'Курык · мультимодальный (KZ)',
  ];
  // (старые legacy-списки AIR_PORTS/SEA_PORTS заменены на новые
  // PORTS_BY_COUNTRY с UN/LOCODE кодами выше)
  var WAREHOUSE_HUBS = [
    // 🇦🇪 ОАЭ
    'Jebel Ali Free Zone (JAFZA), Дубай', 'Dubai South Logistics, Дубай',
    'DAFZA · Dubai Airport FZ, Дубай',
    // 🇨🇳 Китай
    'Yiwu / 义乌 (Иу, КНР)', 'Guangzhou / 广州 (Гуанчжоу, КНР)',
    'Shenzhen Qianhai / 前海 (Шэньчжэнь, КНР)',
    'Shanghai Waigaoqiao FTZ / 外高桥 (Шанхай, КНР)',
    'Tianjin Binhai / 滨海 (Тяньцзинь, КНР)',
    // 🇷🇺 Россия
    'Москва', 'Санкт-Петербург', 'Владивосток',
    'Новосибирск', 'Екатеринбург', 'Казань', 'Калининград',
    // 🇳🇱 Нидерланды
    'Rotterdam (NL)', 'Amsterdam (NL)',
    // 🇹🇷 Турция
    'Стамбул (TR)', 'Mersin (TR)',
    // 🇰🇿 Казахстан
    'Алматы', 'Астана', 'Шымкент',
  ];
  var FIELD_SUGGESTIONS = {
    sea_port: SEA_PORTS,
    air_port: AIR_PORTS,
    warehouse_address: WAREHOUSE_HUBS,
  };

  var __pendingImport = null; // {import_id, mapping, questions, transform_rules, constants}

  async function uploadPricelist(file) {
    showConv();
    addMessage('user', '📎 ' + file.name + ' (' + Math.round(file.size/1024) + ' KB)');
    const pending = addMessage('assistant', 'Читаю файл и подбираю маппинг колонок…');

    // Side panel: спиннер с прогрессом во время чтения файла.
    // Когда mapping готов — заменяем спиннер на превью исходного файла
    // (заголовки + первые строки). Не исчезает, юзер видит результат.
    try {
      var spPanel = document.getElementById('sidePreview');
      var spNameEl = document.getElementById('sidePreviewName');
      var spMetaEl = document.getElementById('sidePreviewMeta');
      var spBodyEl = document.getElementById('sidePreviewBody');
      if (spPanel && spBodyEl) {
        if (spNameEl) spNameEl.textContent = file.name;
        if (spMetaEl) spMetaEl.textContent = (file.size > 1024*1024
          ? (file.size / (1024*1024)).toFixed(1) + ' MB'
          : Math.round(file.size/1024) + ' KB');
        spBodyEl.innerHTML =
          '<div class="opx-gen-loading">'
          + '<div class="opx-gen-spinner"></div>'
          + '<div class="opx-gen-message">Анализирую файл…</div>'
          + '<div class="opx-gen-counter">распознаю заголовки и тип данных</div>'
          + '</div>';
        spPanel.hidden = false;
      }
    } catch(e) {}

    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await fetch('/api/assistant/upload-pricelist/', {
        method: 'POST',
        headers: {'X-CSRFToken': csrf()},
        body: fd, credentials: 'same-origin',
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      if (pending && pending.parentNode) pending.remove();

      // Заменяем спиннер на превью исходного файла (headers + sample rows)
      try {
        var spBody = document.getElementById('sidePreviewBody');
        if (spBody && data.headers) {
          var hdrs = (data.headers || []).filter(function(h){ return String(h||'').trim(); });
          var headerHtml = hdrs.map(function(h){ return '<th>' + esc(h) + '</th>'; }).join('');
          var rowsHtml = (data.sample_rows || []).map(function(row){
            var cells = (row || []).slice(0, hdrs.length).map(function(c){
              var v = (c == null ? '' : String(c)).slice(0, 60);
              return '<td>' + esc(v) + '</td>';
            }).join('');
            return '<tr>' + cells + '</tr>';
          }).join('');
          var noteHtml = '<div class="opx-note">📂 Ваш файл · '
            + hdrs.length + ' колонок'
            + (data.total_rows ? ' · ' + data.total_rows + ' позиций' : '')
            + '</div>';
          spBody.innerHTML = noteHtml
            + '<table><thead><tr>' + headerHtml + '</tr></thead>'
            + '<tbody>' + rowsHtml + '</tbody></table>';
        }
      } catch(e) {}

      const headers = (data.headers || []).filter(function(h) { return String(h || '').trim(); });
      const sug = data.suggested_mapping || {};
      const stdFields = data.std_fields || [];
      const questions = data.questions || [];
      const transformRules = data.transform_rules || {};
      const constants = data.constants || {};

      // Сохраняем для commit
      __pendingImport = {
        import_id: data.import_id,
        mapping: Object.assign({}, sug),
        transform_rules: transformRules,
        constants: constants,
        smart_answers: {},  // {field: value} от smart questionnaire
      };

      // Таблица маппинга — показываем только замапленные из файла
      var mapRows = [];
      var unmapped = [];
      var defaultFields = [];
      var fromFile = 0;
      var fromDefault = 0;
      // Supplier-wide логистика обязана быть одинаковой на всю загрузку.
      // Если колонка замаплена но во всех sample-строках пуста — игнорируем
      // её как «из файла» и просим юзера ввести в форме общих полей.
      var SUPPLIER_WIDE_KEYS = ['sea_port', 'air_port', 'warehouse_address'];
      var sampleRows = data.sample_rows || [];
      function columnHasData(colName) {
        var idx = headers.indexOf(colName);
        if (idx < 0) return false;
        for (var i = 0; i < sampleRows.length; i++) {
          var row = sampleRows[i];
          if (idx < row.length && String(row[idx] == null ? '' : row[idx]).trim()) return true;
        }
        return false;
      }
      stdFields.forEach(function(f) {
        var src = sug[f.key] || '';
        var srcLabel = '';
        var st = '';
        if (src && src.startsWith('fix:')) {
          srcLabel = '= ' + src.slice(4);
          st = '·';
          fromDefault++;
          defaultFields.push(f);
        } else if (src && headers.includes(src)
                    && (SUPPLIER_WIDE_KEYS.indexOf(f.key) < 0 || columnHasData(src))) {
          srcLabel = '← ' + src;
          st = '✓';
          fromFile++;
        } else if (f.required) {
          srcLabel = '—';
          st = '⚠️';
          unmapped.push(f.label);
        } else {
          srcLabel = f['default'] ? '= ' + f['default'] : '—';
          st = '·';
          fromDefault++;
          defaultFields.push(f);
        }
        var rule = transformRules[f.key];
        if (rule && rule.formula) {
          srcLabel += '  ⚙ ' + rule.formula;
        }
        // Показываем только из файла + обязательные незамапленные
        if (st === '✓' || st === '⚠️') {
          mapRows.push([f.label, srcLabel, st]);
        }
      });

      var cards = [];

      // ── ИНСТРУКЦИЯ В НАЧАЛЕ — как всё работает ──
      // Юзеры на первом импорте не знают чего ожидать. Объясняем
      // ключевые правила сразу: одна страна, Q=1, AI оценка, и т.п.
      cards.push({type:'raw_html', data:{html:
        '<div class="card import-intro">'
        + '<div class="ii-title">📘 Как работает загрузка прайса</div>'
        + '<div class="ii-sub">Несколько шагов — и ваш файл превратится в готовые карточки маркетплейса.</div>'
        + '<div class="ii-steps">'
        +   '<div class="ii-step"><span class="ii-n">1</span>'
        +     '<div><b>🔍 Распознаём колонки в вашем файле</b><br>'
        +       '<span class="ii-hint">Системный словарь поддерживает заголовки на 5 языках. '
        +       'Если что-то непонятное — подключаем AI.</span></div></div>'
        +   '<div class="ii-step"><span class="ii-n">2</span>'
        +     '<div><b>💬 Семь коротких вопросов</b><br>'
        +       '<span class="ii-hint">Бренд, тип товара, наличие, завод-производитель, '
        +       'наценки FOB SEA и FOB AIR. Можно отвечать тапом по подсказке.</span></div></div>'
        +   '<div class="ii-step"><span class="ii-n">3</span>'
        +     '<div><b>📋 Общие поля поставщика</b><br>'
        +       '<span class="ii-hint">🌍 Страна отправления, адрес склада, морпорт и аэропорт. '
        +       'Подсказки с международными кодами UN/LOCODE.</span></div></div>'
        +   '<div class="ii-step"><span class="ii-n">4</span>'
        +     '<div><b>📊 Готовый XLSX в формате маркетплейса</b><br>'
        +       '<span class="ii-hint">Открывается справа в превью — можно проверить и скачать.</span></div></div>'
        +   '<div class="ii-step"><span class="ii-n">5</span>'
        +     '<div><b>📥 Загрузка в каталог</b><br>'
        +       '<span class="ii-hint">Перед записью в базу — итоговая сводка: '
        +       'что взято из файла, что вы указали, какие правила применены.</span></div></div>'
        + '</div>'
        + '<div class="ii-rules">'
        +   '<div class="ii-rule-title">⚠️ Что важно знать</div>'
        +   '<ul>'
        +     '<li><b>Одна загрузка — одна страна отправления.</b> '
        +     'Для разных стран — отдельные файлы.</li>'
        +     '<li><b>Количество по умолчанию — 1.</b> '
        +     'Если в файле нет колонки Quantity, считаем цену за единицу.</li>'
        +     '<li><b>Пустые поля остаются пустыми.</b> '
        +     'Мы не угадываем — что отсутствует в источнике, можно дозаполнить позже в каталоге.</li>'
        +     '<li><b>Профиль поставщика сохраняется.</b> '
        +     'При следующей загрузке такого же файла мы спросим меньше.</li>'
        +   '</ul>'
        + '</div>'
        + '</div>'
      }});
      // 1. Превью «как ляжет в базу»
      if (data.mapped_preview && (data.mapped_preview.rows || []).length) {
        cards.push({type:'table_preview', data:{
          title: '✅ Как ляжет в базу (первые строки)',
          headers: data.mapped_preview.headers,
          rows: data.mapped_preview.rows,
        }});
      }
      // 2. Таблица маппинга (только из файла)
      if (mapRows.length > 0) {
        cards.push({type:'table_preview', data:{
          title: '🔗 Маппинг колонок',
          headers: ['Поле платформы', 'Источник из файла', ''],
          rows: mapRows,
        }});
      }

      // Базовое короткое приветствие — мгновенно после upload.
      // AI-разговорный intro подтянется async через smart-questions.
      var intro;
      var rowsInfo = data.total_rows ? data.total_rows + ' позиций' : (headers.length + ' колонок');
      if (data.from_profile) {
        intro = '📋 Файл прочитан · ' + rowsInfo + '\n🧠 Профиль: ' + (data.profile_name || 'auto');
      } else {
        intro = '📋 Файл прочитан · ' + rowsInfo + ' · ' + fromFile + ' полей из файла';
      }
      if (unmapped.length) {
        intro += '\n⚠️ Не найдено: ' + unmapped.join(', ');
      }
      // Бренды из колонки — подтверждение что мультибренд-прайс распознан.
      var brands = data.detected_brands || [];
      if (brands.length) {
        var shown = brands.slice(0, 12).join(', ');
        intro += '\n🏷 Бренды из колонки (' + brands.length + '): ' + shown
               + (brands.length > 12 ? ' и др.' : '')
               + ' — берём для каждой позиции свой, спрашивать не буду.';
      }

      // 3. Раскрываемая секция supplier-wide дефолтов.
      // Сюда попадают ТОЛЬКО поля, не покрытые smart questionnaire
      // (brand, condition, availability, manufacturer, manufacturer_visible,
      //  price_fob_* — задаются в чате через вопросы).
      // Здесь — логистические поля поставщика и валюта.
      // Порядок важен для визуального баланса грида 2 колонки:
      // Морпорт + Аэропорт парой, потом Адрес склада full-width,
      // потом Валюта одна (она короткая, выглядит ок)
      // warehouse_address ставим первым (он "адрес склада EXW" — улица),
      // потом порты которые юзер ближе подбирает к складу, потом валюта.
      // Между country и warehouse в рендере вставляется отдельное поле
      // "Город" (см. cityRowHtml ниже) для подсказок и приоритизации портов.
      var SUPPLIER_WIDE_ORDER = ['warehouse_address', 'sea_port', 'air_port', 'currency'];
      var supplierWideFields = SUPPLIER_WIDE_ORDER
        .map(function(key) {
          return defaultFields.find(function(f){ return f.key === key; });
        })
        .filter(function(f) { return !!f; });
      var perPartCount = defaultFields.length - supplierWideFields.length;

      // Селектор страны нужен ТОЛЬКО для фильтрации портов в form'е.
      // Если оба порта (sea_port и air_port) уже заполнены — страна
      // выводится из ISO-кода порта и селектор лишний шум.
      function hasPortValue(key) {
        // Уже заполнено через колонку файла (есть данные)?
        var src = sug[key] || '';
        if (src && headers.includes(src) && columnHasData(src)) return true;
        // Через fix:VALUE?
        if (src.startsWith && src.startsWith('fix:') && src.slice(4).trim()) return true;
        // Через constants?
        if ((constants[key] || '').toString().trim()) return true;
        return false;
      }
      var portsReady = hasPortValue('sea_port') && hasPortValue('air_port');
      var needsCountrySelector = !portsReady && supplierWideFields.some(function(f){
        return f.key === 'sea_port' || f.key === 'air_port';
      });
      if (supplierWideFields.length > 0) {
        var dfHtml = supplierWideFields.map(function(f) {
          // Если поле в форме потому что колонка файла пустая — val=''
          // (а не имя колонки), чтобы placeholder подсказывал формат.
          var rawSrc = sug[f.key] || '';
          var val;
          if (rawSrc.startsWith && rawSrc.startsWith('fix:')) {
            val = rawSrc.slice(4);
          } else if (rawSrc && headers.includes(rawSrc)
                      && SUPPLIER_WIDE_KEYS.indexOf(f.key) >= 0
                      && !columnHasData(rawSrc)) {
            val = '';  // колонка пустая → дать юзеру ввести с нуля
          } else if (rawSrc && !headers.includes(rawSrc)) {
            val = rawSrc;
          } else {
            val = f['default'] || '';
          }
          var inp = '';
          if (f.enum_values && f.enum_values.length) {
            var opts = f.enum_values.map(function(o) {
              var sel = (String(o) === String(val)) ? ' selected' : '';
              return '<option value="' + esc(o) + '"' + sel + '>' + esc(o) + '</option>';
            }).join('');
            inp = '<select class="pl-df-input" data-field="' + esc(f.key) + '">' + opts + '</select>';
          } else if (f.key === 'sea_port' || f.key === 'air_port') {
            // Морпорт / Аэропорт. Кастомный автокомплит (см. ниже _initAC):
            // нативный <datalist> в Chrome фильтрует по ПРЕФИКСУ значения,
            // поэтому "Beijing / Пекин" не находится при наборе "Пек".
            // data-ac-kind="sea|air" → провайдер отдаёт порты текущей страны
            // отсортированные по близости к складу.
            // type="search" + readonly (снимается на focusin) подавляют
            // нативный Chrome address-autofill, который иначе всплывает
            // поверх нашего попапа.
            inp = '<input class="pl-df-input pl-port-input pl-ac" data-field="' + esc(f.key)
              + '" data-ac-kind="' + (f.key === 'sea_port' ? 'sea' : 'air') + '"'
              + ' type="search" value="' + esc(val) + '" autocomplete="off"'
              + ' autocorrect="off" spellcheck="false" data-lpignore="true" readonly'
              + ' placeholder="Код или название порта"/>';
          } else if (f.key === 'warehouse_address') {
            // Адрес склада EXW — улица + № дома. Город указывается отдельно
            // выше (см. cityRowHtml), на commit мы склеиваем
            // "{улица}, {город}, {страна}" в полное warehouse_address.
            inp = '<input class="pl-df-input pl-wh-street" data-field="' + esc(f.key)
              + '" type="text" value="' + esc(val) + '" autocomplete="off"'
              + ' placeholder="улица, № дома"/>';
          } else {
            // Подсказки по списку складов
            var sugg = FIELD_SUGGESTIONS[f.key] || null;
            if (sugg) {
              var listId = 'sugg_' + f.key;
              var suggOpts = sugg.map(function(s){
                return '<option value="' + esc(s) + '"/>';
              }).join('');
              inp = '<input class="pl-df-input" data-field="' + esc(f.key)
                + '" type="text" value="' + esc(val) + '" autocomplete="off"'
                + ' list="' + listId + '"/>'
                + '<datalist id="' + listId + '">' + suggOpts + '</datalist>';
            } else {
              inp = '<input class="pl-df-input" data-field="' + esc(f.key) + '" type="text" value="' + esc(val) + '" autocomplete="off"/>';
            }
          }
          return '<div class="pl-df-row"><span class="pl-df-label">' + esc(f.label) + '</span>' + inp + '</div>';
        }).join('');
        var perPartNote = '';
        if (perPartCount > 0) {
          // Никаких AI-угадаек по умолчанию — то что не пришло в файле
          // грузится пустым, юзер дозаполняет в каталоге per-part.
          var noun = (perPartCount === 1) ? 'поле' :
                     (perPartCount >= 2 && perPartCount <= 4) ? 'поля' : 'полей';
          var verb = (perPartCount === 1) ? 'не передаётся' : 'не передаются';
          perPartNote =
            '<div class="pl-df-note">'
            +   perPartCount + ' ' + noun + ' ' + verb + ' в файле'
            +   ' — загрузятся пустыми, можно отредактировать в каталоге позже.'
            + '</div>';
        }
        // Один селектор «Страна отправления» сверху — он управляет
        // фильтрацией морпорт/аэропорт и подсказкой для адреса склада.
        // Правило: одна загрузка = одна страна. Если разные — отдельные файлы.
        var topCountryOpts = '<option value="">— выбрать страну —</option>'
          + Object.keys(PORTS_BY_COUNTRY).map(function(cc){
            var c = PORTS_BY_COUNTRY[cc];
            return '<option value="' + cc + '">' + esc(c.flag + ' ' + c.name) + '</option>';
          }).join('');
        // Страна + Город — в ОДНОЙ строке горизонтально (pl-ship-combo).
        // Город — combobox с кастомным AC (data-ac-kind="city"): поиск по
        // 155k городов GeoNames через backend, рус+лат+транслит+опечатки.
        // id/for/name НЕ содержат "city"/"address" + readonly → Chrome не
        // показывает свой address-autofill поверх нашего попапа.
        var cityNameSalt = 'pl_field_' + Math.random().toString(36).slice(2, 10);
        var countryFieldHtml =
          '<div class="pl-ship-field">'
          + '<label class="pl-ship-flabel" for="shipment_country">🌍 Страна отправления</label>'
          + '<select class="pl-df-input pl-ship-country" id="shipment_country">'
          +   topCountryOpts
          + '</select>'
          + '</div>';
        var cityFieldHtml =
          '<div class="pl-ship-field">'
          + '<label class="pl-ship-flabel">Город</label>'
          + '<input class="pl-df-input pl-city-input pl-ac" type="search"'
          +   ' data-ac-kind="city" placeholder="Например: Shanghai, Стамбул…"'
          +   ' name="' + cityNameSalt + '" autocomplete="off" autocorrect="off"'
          +   ' spellcheck="false" data-lpignore="true" data-form-type="other" readonly/>'
          + '</div>';
        var comboHtml =
          '<div class="pl-ship-combo">'
          + (needsCountrySelector ? countryFieldHtml : '')
          + cityFieldHtml
          + '</div>'
          + '<div class="pl-ship-combo-hint">Одна страна на всю загрузку. '
          + 'Порты и склад фильтруются по выбранной стране.</div>';
        cards.push({type:'raw_html', data:{
          html: '<div class="card pl-defaults-card">'
            + '<details class="pl-defaults-details" open>'
            + '<summary class="pl-defaults-summary">📎 ' + supplierWideFields.length + ' общих полей поставщика — нажмите чтобы изменить</summary>'
            + comboHtml
            + '<div class="pl-df-grid">' + dfHtml + '</div>'
            + perPartNote
            + '</details></div>',
        }});
      }

      var actions = [
        {action: '__pricelist_commit', label: '📥 Подтвердить и загрузить',
         params: {import_id: data.import_id, _has_questions: questions.length > 0 ? '1' : '0'}},
        {action: '__pricelist_cancel', label: 'Отменить',
         params: {import_id: data.import_id}},
      ];

      // Для вопросов добавляем select-элементы inline в params
      questions.forEach(function(q) {
        var defVal = q['default'] || (q.options && q.options.length ? q.options[0] : '');
        actions[0].params['q__' + q.field] = defVal;
      });

      // Если questionnaire pending — сразу показываем форму без вопросов,
      // и параллельно подтягиваем умные вопросы async (не блокируя upload).
      // Когда придут — отрендерим их перед commit-кнопкой.
      if (data.smart_questions_pending) {
        // fromServer=false — все карты в этом сообщении (preview-table,
        // mapping-table, import-intro, pl-defaults-card) сгенерированы
        // КЛИЕНТОМ из upload-response, не пришли с бэка как готовые карты.
        // Без явного false security-check (FRONTEND_ONLY_TYPES) тихо
        // дропает raw_html-карты (intro + supplier-wide form).
        var bigMsg = addMessage('assistant', intro, cards, [], [], null, [], [], false);
        // Кастомный AC сам подтягивает список при фокусе на инпут —
        // bootstrap datalist'ов больше не нужен.
        // Скроллим к НАЧАЛУ нового сообщения чтобы юзер увидел
        // инструкцию сверху, а не сразу прыгнул вниз к чему-то.
        setTimeout(function() {
          if (bigMsg && bigMsg.scrollIntoView) {
            bigMsg.scrollIntoView({block:'start', behavior:'smooth'});
          }
        }, 100);
        var thinking = addMessage('assistant', '💭 Подбираю уточняющие вопросы…', [], []);
        fetch('/api/assistant/upload-pricelist/' + data.import_id + '/smart-questions/', {
          credentials: 'same-origin',
        }).then(function(r){ return r.json(); }).then(function(sq){
          if (thinking && thinking.parentNode) thinking.remove();
          // Сохраняем позицию скролла — не дёргаем юзера вниз
          var stream = document.getElementById('stream');
          var savedScroll = stream ? stream.scrollTop : 0;
          var qs = sq.questions || [];
          if (qs.length) {
            // СНАЧАЛА общие поля поставщика (карточка выше), ПОТОМ уточнения по
            // товарам. Не заваливаем юзера вопросами сразу — стартуем их только
            // по кнопке «Продолжить», чтобы поток был последовательным.
            window.__pendingSmartQs = qs;
            var nWord = (qs.length === 1 ? 'деталь'
                        : (qs.length >= 2 && qs.length <= 4 ? 'детали' : 'деталей'));
            addMessage('assistant',
              '👆 Сначала заполните **общие поля поставщика** выше — страна, склад, морпорт/аэропорт. '
              + 'Это одно на весь прайс. Готово — нажмите кнопку, и я уточню ещё ' + qs.length + ' ' + nWord + ' по товарам.',
              [], [
              {action: '__pl_start_questions', label: '✅ Общие поля готовы — продолжить →'},
            ]);
          } else {
            if (sq.intro) addMessage('assistant', sq.intro, [], []);
            addMessage('assistant', '✨ Готово, можно загружать.', [], [
              {action: '__pricelist_commit', label: '📥 Загрузить',
               params: {import_id: data.import_id}},
            ]);
          }
          // Восстанавливаем позицию — пусть юзер сам доскроллит вниз
          if (stream) {
            setTimeout(function(){ stream.scrollTop = savedScroll; }, 10);
          }
        }).catch(function(){
          if (thinking && thinking.parentNode) thinking.remove();
          var stream = document.getElementById('stream');
          var savedScroll = stream ? stream.scrollTop : 0;
          addMessage('assistant', '✨ Готово, можно загружать.', [], [
            {action: '__pricelist_commit', label: '📥 Загрузить',
             params: {import_id: data.import_id}},
          ]);
          if (stream) setTimeout(function(){ stream.scrollTop = savedScroll; }, 10);
        });
      } else {
        var smartQs = data.smart_questions || [];
        if (smartQs.length) {
          addMessage('assistant', intro, cards, []);
          showNextSmartQuestion(smartQs, 0);
        } else {
          addMessage('assistant', intro, cards, actions);
        }
      }
      // Если из профиля поставщика подтянулись sea_port/air_port —
      // определяем страну из кода порта (первые 2 символа = ISO-код)
      // и автоматически выбираем её в селекторе. Иначе юзер видит
      // непустые порты при пустой стране — выглядит сломанно.
      setTimeout(function() {
        var top = document.getElementById('shipment_country');
        if (!top || top.value) return;
        var seaInp = document.querySelector('.pl-df-input[data-field="sea_port"]');
        var airInp = document.querySelector('.pl-df-input[data-field="air_port"]');
        var sample = (seaInp && seaInp.value) || (airInp && airInp.value) || '';
        if (!sample) return;
        var code = sample.split(/[\s·]/)[0];
        if (code.length < 2) return;
        var cc = code.slice(0, 2).toUpperCase();
        if (!PORTS_BY_COUNTRY[cc]) return;
        top.value = cc;
        // Список портов/городов подтянется при фокусе на AC-инпут.
      }, 50);
    } catch (err) {
      if (pending && pending.parentNode) pending.remove();
      try { closeSidePreview(); } catch(e) {}
      addMessage('assistant', '⚠️ Не удалось прочитать прайс: ' + (err.message || err));
    }
  }

  // Commit маппинга с формулами и ответами на вопросы
  window.__pricelist_commit_handler = async function(params) {
    // Single-flight: один импорт за сессию. Блокируем повторные клики
    // на кнопках «Загрузить» пока текущий идёт.
    if (window.__importInFlight) {
      window.toast && window.toast('⏳ Импорт уже идёт, дождитесь завершения', 3000);
      return;
    }
    window.__importInFlight = true;
    // Дизейблим все кнопки запуска импорта на странице
    var lockedBtns = Array.from(document.querySelectorAll('[data-action="__pricelist_commit"]'));
    lockedBtns.forEach(b => { b.disabled = true; b.style.opacity = '0.45'; b.style.cursor = 'not-allowed'; });
    var imp = __pendingImport || {};
    var importId = params.import_id || imp.import_id;
    var mapping = Object.assign({}, imp.mapping || {});
    var transformRules = imp.transform_rules || {};
    var constants = Object.assign({}, imp.constants || {});

    // Собираем mapping из params (legacy col__ формат)
    Object.keys(params).forEach(function(k) {
      if (k.startsWith('col__') && params[k] && params[k] !== '__sep__') {
        mapping[k.slice(5)] = params[k];
      }
    });

    // Собираем ответы на вопросы → constants
    Object.keys(params).forEach(function(k) {
      if (k.startsWith('q__') && params[k]) {
        constants[k.slice(3)] = params[k];
      }
    });

    // Собираем значения из раскрываемой секции дефолтов
    document.querySelectorAll('.pl-df-input').forEach(function(el) {
      var field = el.dataset.field;
      var val = el.value;
      if (field && val !== undefined) {
        constants[field] = val;
        mapping[field] = 'fix:' + val;
      }
    });

    // ── Обязательная логистика: страна + город + адрес + порты ──────
    // Считываем компоненты ДО склейки warehouse_address. Без полного блока
    // загрузку не пропускаем (даже если на все вопросы мастера ответили).
    var _cityInp = document.querySelector('.pl-city-input');
    var _countrySel = document.querySelector('.pl-ship-country, #shipment_country');
    var _street = String(constants.warehouse_address || '').trim();  // pl-df-input склад = улица
    var _city = _cityInp ? String(_cityInp.value || '').trim() : '';
    var _countryName = '';
    if (_countrySel) {
      if (_countrySel.tagName === 'SELECT') {
        var _opt = _countrySel.options[_countrySel.selectedIndex];
        _countryName = (_opt && _opt.value) ? (_opt.text || '').replace(/^[\s\S]*?\s/, '') : '';
      } else {
        _countryName = String(_countrySel.value || '').trim();
      }
    }
    function _fixVal(key) {
      var v = (constants && constants[key] ? String(constants[key]).trim() : '');
      if (!v) {
        var m = (mapping && mapping[key]) || '';
        if (typeof m === 'string' && m.indexOf('fix:') === 0) v = m.slice(4).trim();
      }
      return v;
    }
    var _sea = _fixVal('sea_port');
    var _air = _fixVal('air_port');
    var missingSW = [];
    if (!_countryName) missingSW.push('страна отправления');
    if (!_city)        missingSW.push('город');
    if (!_street)      missingSW.push('адрес склада EXW');
    if (!_sea)         missingSW.push('морпорт отправления');
    if (!_air)         missingSW.push('аэропорт отправления');
    if (missingSW.length) {
      lockedBtns.forEach(b => { b.disabled = false; b.style.opacity = ''; b.style.cursor = ''; });
      addMessage('assistant',
        '❗ Без логистики загрузку продолжить нельзя. Заполните: ' + missingSW.join(', ') +
        '. Это в блоке «📎 общих полей поставщика» сверху.');
      return;
    }

    // Все поля есть → склеиваем полный warehouse_address (улица, город, страна).
    var _wfull = [_street, _city, _countryName].filter(function(p){ return p; }).join(', ');
    constants.warehouse_address = _wfull;
    mapping.warehouse_address = 'fix:' + _wfull;

    // Собираем юзерские правки AI оценок (review-таблица)
    var aiOverrides = {};
    document.querySelectorAll('.pl-ai-row').forEach(function(row) {
      var oem = row.dataset.oem;
      if (!oem) return;
      var fields = {};
      row.querySelectorAll('.pl-ai-input').forEach(function(inp) {
        var f = inp.dataset.field;
        var v = parseFloat(inp.value);
        if (f && !isNaN(v)) fields[f] = v;
      });
      if (Object.keys(fields).length) aiOverrides[oem] = fields;
    });

    showConv();
    var pending = addMessage('assistant', '📥 Импортирую прайс… 0 строк');

    // Импорт идёт в ФОНЕ на сервере: commit отвечает 202 сразу, иначе длинный
    // запрос (100K+ строк) рвёт Cloudflare на ~100s → «Failed to fetch».
    // Промис резолвится поллером прогресса, когда фоновый импорт завершён.
    var importResultResolve, importResultReject;
    var importResultPromise = new Promise(function(rs, rj){
      importResultResolve = rs; importResultReject = rj;
    });
    var importSettled = false;

    // Polling прогресса импорта каждые 500ms (+ детект завершения)
    var importPollTimer = setInterval(async function() {
      try {
        var pr = await fetch('/api/assistant/upload-pricelist/' + importId + '/import-progress/', {
          credentials: 'same-origin',
        });
        if (!pr.ok) return;
        var pdata = await pr.json();
        if (pending && pdata.current !== undefined && !pdata.done) {
          var cEl = pending.querySelector('.msg-content');
          if (cEl) {
            var phaseMap = {
              parsing: 'Читаю файл',
              matching: 'Сопоставляю с базой',
              writing: 'Записываю в каталог',
            };
            var phase = phaseMap[pdata.phase] || 'Импортирую прайс';
            var totalPart = pdata.total ? (' / ' + pdata.total) : '';
            cEl.textContent = '📥 ' + phase + '… ' + pdata.current + totalPart + ' строк';
          }
        }
        // Завершение фонового импорта → резолвим/реджектим промис.
        if (!importSettled && (pdata.done || pdata.status === 'imported' || pdata.status === 'failed')) {
          importSettled = true;
          clearInterval(importPollTimer);
          var rd = pdata.result || {};
          if (rd.ok === false || pdata.status === 'failed') {
            importResultReject(new Error(rd.error || 'ошибка импорта на сервере'));
          } else {
            importResultResolve(rd);
          }
        }
      } catch(e) {}
    }, 500);

    try {
      var res = await fetch('/api/assistant/upload-pricelist/' + importId + '/commit/', {
        method: 'POST',
        headers: {'Content-Type':'application/json', 'X-CSRFToken': csrf()},
        body: JSON.stringify({
          mapping: mapping,
          transform_rules: transformRules,
          constants: constants,
          ai_estimates_override: aiOverrides,
        }),
        credentials: 'same-origin',
      });
      var startData = await res.json();
      if (!res.ok) {
        importSettled = true;
        clearInterval(importPollTimer);
        if (pending && pending.parentNode) pending.remove();
        var blockingErrors = {
          warehouse_address_required: 'Укажите адрес склада отгрузки.',
          brand_required: 'Не удалось определить бренд.',
        };
        if (blockingErrors[startData.error]) {
          addMessage('assistant',
            '❗ ' + (startData.message || blockingErrors[startData.error]));
          __pendingImport = null;
          return;
        }
        throw new Error(startData.message || startData.error || ('HTTP ' + res.status));
      }
      // 202 принят — ждём, пока поллер принесёт результат фонового импорта.
      var data = await importResultPromise;
      if (pending && pending.parentNode) pending.remove();

      var created = data.created || 0;
      var updated = data.updated || 0;
      var failed  = data.failed  || 0;
      var aiCount = data.ai_estimated_count || 0;

      var parts = [];
      if (created) parts.push('✅ Создано ' + created);
      if (updated) parts.push('🔄 Обновлено ' + updated);
      // Заголовок об успехе — только если что-то реально загрузилось.
      var msg = '';
      if (created || updated) {
        msg = '✅ Загрузка прошла успешно!\n';
      }
      msg += parts.join(' · ') + ' позиций.';
      if (created || updated) {
        msg += '\n📦 Все позиции уже в разделе «Мои товары».';
      }
      if (failed) {
        // Это НЕ поломка импорта — успешные строки уже в базе.
        // Просто N строк с битыми данными (пустой OEM/название/цена)
        // пропущены и доступны к просмотру отдельно.
        msg += '\nℹ️ ' + failed + ' ' + (failed === 1 ? 'строка пропущена' : 'строк пропущено')
             + ' — битые данные (пустой артикул, название или цена).';
      }
      var merged = data.merged_duplicates || 0;
      if (merged) {
        msg += '\n🧩 Объединено ' + merged + ' дублирующих'
             + (merged === 1 ? 'ся строки' : ' строк') + ' с одинаковым OEM и ценой — '
             + 'одна позиция с MAX(Qty).';
      }
      var conflicts = data.price_conflicts || 0;
      if (conflicts) {
        msg += '\nℹ️ ' + conflicts + ' артикул' + (conflicts === 1 ? '' : 'ов')
             + ' с разными ценами — загружены как отдельные позиции (варианты прайса). '
             + 'Покупатель увидит оба варианта в каталоге.';
      }

      // Категоризированный отчёт о незаполненных полях.
      var refCount = data.reference_enriched || 0;
      var smartAns = (__pendingImport && __pendingImport.smart_answers) || {};
      var smartFields = Object.keys(smartAns);
      function filterAnswered(list) {
        return (list || []).filter(function(m){
          return smartFields.indexOf(m.key) < 0;
        });
      }
      var missMand  = filterAnswered(data.missing_mandatory);
      var missBonus = filterAnswered(data.missing_rating_bonus);
      var missOpt   = filterAnswered(data.missing_optional);

      if (smartFields.length) {
        var smartParts = smartFields.map(function(k){
          return k + '=' + smartAns[k].value;
        });
        msg += '\n\n✨ Применены ваши ответы: ' + smartParts.join(', ') + '.';
      }
      if (missMand.length) {
        msg += '\n\n❗ Обязательно заполнить: '
             + missMand.map(function(m){return m.label;}).join(', ') + '.';
      }
      if (missBonus.length) {
        msg += '\n\n⭐ Повысит рейтинг карточки: '
             + missBonus.map(function(m){return m.label;}).join(', ') + '.';
        if (refCount > 0) {
          msg += '\n✨ Подтянули ' + refCount + ' позиций из эталонной базы.';
        } else {
          msg += '\nЗаполните в каталоге чтобы поднять карточки в выдаче — '
               + 'или пропустите, можно добавить позже.';
        }
      }
      if (missOpt.length) {
        msg += '\n\nℹ️ Не пришло из файла: '
             + missOpt.map(function(m){return m.label;}).join(', ') + '.'
             + ' Можно дозаполнить позже в каталоге.';
      }
      var sources = [];
      if (refCount > 0) sources.push('✨ ' + refCount + ' позиций обогащены из эталонной базы');
      if (aiCount > 0) sources.push('🤖 ' + aiCount + ' AI-оценкой');
      if (sources.length) msg += '\n\n' + sources.join(' · ') + '.';

      var btns = [];
      if (failed > 0) {
        btns.push({action: 'pricelist_show_errors', label: '🔎 Показать пропущенные',
                   params: {import_id: importId}});
      }
      btns.push({action: 'seller_warehouses', label: '📦 Мои товары', params: {}});
      addMessage('assistant', msg, [], btns);
      __pendingImport = null;
    } catch (err) {
      clearInterval(importPollTimer);
      if (pending && pending.parentNode) pending.remove();
      addMessage('assistant', '⚠️ Не удалось импортировать: ' + (err.message || err));
    } finally {
      // Отпускаем single-flight lock и разблокируем кнопки
      window.__importInFlight = false;
      lockedBtns.forEach(b => { b.disabled = false; b.style.opacity = ''; b.style.cursor = ''; });
    }
  };

  window.__pricelist_cancel_handler = async function(params) {
    try {
      await fetch('/api/assistant/upload-pricelist/' + params.import_id + '/cancel/', {
        method: 'POST',
        headers: {'X-CSRFToken': csrf()},
        credentials: 'same-origin',
      });
    } catch(e){}
    addMessage('assistant', 'Импорт отменён.');
    __pendingImport = null;
  };

  // AI-оценка с auto-start + polling прогресс-баром.
  // Один POST запускает работу на сервере, параллельно поллим
  // /ai-estimate-progress/ каждые 500ms и обновляем bar.
  async function startAiEstimate(card) {
    var importId = card.dataset.importId;
    if (!importId) return;
    if (card.dataset.started === '1') return;  // уже запущено
    card.dataset.started = '1';

    var fillEl = card.querySelector('.pl-ai-progress-fill');
    var counterEl = card.querySelector('.pl-ai-progress-counter');
    var statusEl = card.querySelector('.pl-ai-progress-status');

    function setProgress(current, total) {
      var pct = total > 0 ? Math.round(100 * current / total) : 0;
      if (fillEl) fillEl.style.width = pct + '%';
      if (counterEl) counterEl.textContent = current + ' / ' + total;
    }

    if (statusEl) statusEl.textContent = '🧠 запускаю AI...';

    // Polling прогресса
    var pollTimer = setInterval(async function() {
      try {
        var pr = await fetch('/api/assistant/upload-pricelist/' + importId + '/ai-estimate-progress/', {
          credentials: 'same-origin',
        });
        if (!pr.ok) return;
        var pdata = await pr.json();
        if (pdata.total > 0) {
          setProgress(pdata.current, pdata.total);
          if (statusEl) statusEl.textContent = '🧠 AI оценивает...';
        }
      } catch(e) {}
    }, 500);

    try {
      var res = await fetch('/api/assistant/upload-pricelist/' + importId + '/ai-estimate/', {
        method: 'POST',
        headers: {'X-CSRFToken': csrf()},
        credentials: 'same-origin',
      });
      var data = await res.json();
      clearInterval(pollTimer);
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      setProgress(data.estimated, data.total);
      var msg = '✅ AI оценил ' + data.estimated + ' позиций';
      if (data.truncated) msg += ' (первые ' + data.total + ', остальные с дефолтами)';
      if (statusEl) {
        statusEl.textContent = msg;
        statusEl.style.color = 'rgba(46,125,50,0.95)';
      }
      if (fillEl) fillEl.style.background = 'rgba(46,125,50,0.7)';

      // Review-таблица: показываем образец оценок с editable полями.
      // Юзер видит что AI намерил, может поправить — изменения уйдут
      // в commit как ai_estimates_override.
      if (data.review_sample && data.review_sample.length) {
        renderAiReview(card, importId, data.review_sample, data.low_confidence_count);
      }
    } catch (err) {
      clearInterval(pollTimer);
      if (statusEl) {
        statusEl.textContent = '⚠️ ' + (err.message || err);
        statusEl.style.color = 'rgba(232,92,13,0.85)';
      }
    }
  }

  // Пояснительная записка — что произойдёт при загрузке (по правилам)
  function buildImportExplanation(imp, genData) {
    var smartAns = imp.smart_answers || {};
    var constants = imp.constants || {};
    var rules = imp.transform_rules || {};
    var mapping = imp.mapping || {};

    function _val(field) {
      if (smartAns[field] && smartAns[field].value) return smartAns[field].value;
      if (constants[field]) return constants[field];
      var m = mapping[field];
      if (m && m.indexOf && m.indexOf('fix:') === 0) return m.slice(4);
      return null;
    }
    function _formulaPct(field) {
      var r = rules[field];
      if (!r || !r.formula) return null;
      var m = r.formula.match(/\+\s*([\d.]+)\s*\//);
      return m ? '+' + m[1] + '%' : r.formula;
    }
    var fromFile = [];
    Object.keys(mapping).forEach(function(k) {
      var v = mapping[k];
      if (v && (!v.indexOf || v.indexOf('fix:') !== 0)) fromFile.push(k);
    });

    var countryEl = document.getElementById('shipment_country');
    var country = countryEl && countryEl.value ? countryEl.value : null;

    var items = [];

    items.push({icon: '📂', label: 'Из файла', value:
      'Колонки: <b>' + fromFile.join(', ') + '</b>'
    });

    var brand = _val('brand');
    var cond = _val('condition');
    var avail = _val('availability');
    var manuf = _val('manufacturer');
    if (brand || cond || avail || manuf) {
      var ansParts = [];
      if (brand) ansParts.push('Бренд — <b>' + esc(brand) + '</b>');
      if (cond) ansParts.push('Тип товара — <b>' + esc(cond) + '</b>');
      if (avail) ansParts.push('Наличие — <b>' + esc(avail) + '</b>');
      if (manuf) {
        var _known = KNOWN_ANALOG_BRANDS_JS[manuf.trim().toLowerCase()];
        ansParts.push('Завод — <b>' + esc(manuf) + '</b>'
          + (_known ? ' (известный бренд — виден покупателю)'
                    : ' (известный бренд покажем покупателю, частную марку скроем)'));
      }
      items.push({icon: '✋', label: 'Ваши ответы', value: ansParts.join(' · ')});
    }

    var seaPct = _formulaPct('price_fob_sea');
    var airPct = _formulaPct('price_fob_air');
    if (seaPct || airPct) {
      items.push({icon: '📐', label: 'Наценки FOB', value:
        (seaPct ? 'SEA = EXW × (1 ' + esc(seaPct) + ')' : '')
        + (seaPct && airPct ? ' · ' : '')
        + (airPct ? 'AIR = EXW × (1 ' + esc(airPct) + ')' : '')
      });
    }

    if (country) {
      var c = PORTS_BY_COUNTRY[country];
      items.push({icon: '🌍', label: 'Страна отправления', value:
        (c ? c.flag + ' ' + c.name : country)
        + ' <span class="ie-hint">— порты и склад из этой страны</span>'
      });
    }

    var aiCount = (imp.ai_estimates_count || (window.__lastAiEstimateCount || 0));
    if (aiCount > 0) {
      items.push({icon: '🤖', label: 'AI оценил',
        value: '<b>' + aiCount + '</b> позиций — вес и габариты по описанию'});
    }

    items.push({icon: '⚙️', label: 'Количество (Quantity)', value:
      'По умолчанию <b>1</b> — если в файле нет, считаем цену за единицу'
    });

    items.push({icon: '📭', label: 'Пустые значения', value:
      'Если в источнике пусто или 0 — оставляем пусто. Можно дозаполнить позже в каталоге.'
    });

    var rows = items.map(function(it) {
      return '<div class="ie-row">'
        + '<span class="ie-icon">' + it.icon + '</span>'
        + '<div class="ie-content">'
        +   '<div class="ie-label">' + esc(it.label) + '</div>'
        +   '<div class="ie-value">' + it.value + '</div>'
        + '</div>'
        + '</div>';
    }).join('');

    return '<div class="card import-explain">'
      + '<div class="ie-title">📋 Что попадёт в каталог</div>'
      + '<div class="ie-sub">Сводка перед записью в базу. Проверьте и подтвердите.</div>'
      + rows
      + '<div class="ie-rule">'
      + '⚠️ <b>Правило:</b> одна загрузка — одна страна отправления. '
      + 'Если у вас прайсы из разных стран, загружайте их отдельными файлами.'
      + '</div>'
      + '</div>';
  }

  // Генерация выходного XLSX-файла как у Claude.ai —
  // юзер видит карточку с готовым файлом, может скачать или
  // загрузить в каталог.
  async function generateAndShowOutputFile() {
    if (!__pendingImport) return;
    var imp = __pendingImport;

    // Открываем side panel сразу с loading-индикатором,
    // фоном поллим прогресс генерации.
    showSidePreviewLoading('Готовлю файл в формате маркетплейса…');
    var progressPoller = setInterval(async function() {
      try {
        var pr = await fetch('/api/assistant/upload-pricelist/' + imp.import_id + '/generate-output-progress/', {
          credentials: 'same-origin',
        });
        if (!pr.ok) return;
        var p = await pr.json();
        updateSidePreviewLoading(p.current || 0);
      } catch(e) {}
    }, 400);

    var thinking = addMessage('assistant', '🔧 Готовлю файл в формате маркетплейса…', [], []);
    try {
      // Подтягиваем все ответы пользователя
      var constants = Object.assign({}, imp.constants || {});
      document.querySelectorAll('.pl-df-input').forEach(function(el) {
        var f = el.dataset.field; var v = el.value;
        if (f && v !== undefined) constants[f] = v;
      });
      // ── Обязательная логистика (та же проверка что в commit) ─────────
      // Без полного блока НЕ генерируем файл — иначе WarehouseAddress
      // заполнится placeholder'ом «— выбрать страну —».
      var _gCityInp = document.querySelector('.pl-city-input');
      var _gCountrySel = document.querySelector('.pl-ship-country, #shipment_country');
      var _gStreet = String(constants.warehouse_address || '').trim();
      var _gCity = _gCityInp ? String(_gCityInp.value || '').trim() : '';
      var _gCountry = '';
      if (_gCountrySel) {
        if (_gCountrySel.tagName === 'SELECT') {
          var _gOpt = _gCountrySel.options[_gCountrySel.selectedIndex];
          // только если реально выбрана страна (value не пустой) — иначе
          // text это placeholder «— выбрать страну —».
          _gCountry = (_gOpt && _gOpt.value) ? (_gOpt.text || '').replace(/^[\s\S]*?\s/, '') : '';
        } else {
          _gCountry = String(_gCountrySel.value || '').trim();
        }
      }
      var _gSea = String(constants.sea_port || '').trim();
      var _gAir = String(constants.air_port || '').trim();
      var _gMissing = [];
      if (!_gCountry) _gMissing.push('страна отправления');
      if (!_gCity)    _gMissing.push('город');
      if (!_gStreet)  _gMissing.push('адрес склада EXW');
      if (!_gSea)     _gMissing.push('морпорт отправления');
      if (!_gAir)     _gMissing.push('аэропорт отправления');
      if (_gMissing.length) {
        clearInterval(progressPoller);
        if (thinking && thinking.parentNode) thinking.remove();
        try { closeSidePreview(); } catch(e) {}
        // Кнопка повтора: юзер дозаполняет блок логистики сверху и жмёт её.
        addMessage('assistant',
          '❗ Без логистики продолжить нельзя. Заполните в блоке «📎 общих полей '
          + 'поставщика» сверху: ' + _gMissing.join(', ') + '. Потом нажмите «Сгенерировать файл».',
          [], [{action: '__pricelist_retry_generate', label: '🔄 Сгенерировать файл'}]);
        return;
      }
      // Все поля есть → склейка полного warehouse_address.
      constants.warehouse_address = [_gStreet, _gCity, _gCountry]
        .filter(function(p){ return p; }).join(', ');
      var aiOverrides = {};
      document.querySelectorAll('.pl-ai-row').forEach(function(row) {
        var oem = row.dataset.oem;
        if (!oem) return;
        var fields = {};
        row.querySelectorAll('.pl-ai-input').forEach(function(inp) {
          var f = inp.dataset.field; var v = parseFloat(inp.value);
          if (f && !isNaN(v)) fields[f] = v;
        });
        if (Object.keys(fields).length) aiOverrides[oem] = fields;
      });
      var res = await fetch('/api/assistant/upload-pricelist/' + imp.import_id + '/generate-output/', {
        method: 'POST',
        headers: {'Content-Type':'application/json', 'X-CSRFToken': csrf()},
        body: JSON.stringify({
          mapping: imp.mapping,
          transform_rules: imp.transform_rules,
          constants: constants,
          ai_estimates_override: aiOverrides,
        }),
        credentials: 'same-origin',
      });
      var data = await res.json();
      clearInterval(progressPoller);
      if (thinking && thinking.parentNode) thinking.remove();
      if (!res.ok) {
        closeSidePreview();
        throw new Error(data.error || ('HTTP ' + res.status));
      }
      var sizeKB = Math.round((data.size || 0) / 1024);
      openSidePreview(imp.import_id, data.filename, data.download_url);

      // Пояснительная записка — что именно произойдёт по правилам
      var explanationHtml = buildImportExplanation(imp, data);
      addMessage('assistant',
        '✨ Готово! Я подготовил файл в формате маркетплейса.',
        [
          {type:'output_file', data:{
            filename: data.filename,
            size_kb: sizeKB,
            download_url: data.download_url,
            import_id: imp.import_id,
          }},
          {type:'raw_html', data:{html: explanationHtml}},
        ],
        [
          {action: '__pricelist_commit', label: '📥 Загрузить в каталог',
           params: {import_id: imp.import_id}},
          {action: '__pricelist_cancel', label: 'Отменить',
           params: {import_id: imp.import_id}},
        ],
      );
    } catch (err) {
      clearInterval(progressPoller);
      if (thinking && thinking.parentNode) thinking.remove();
      closeSidePreview();
      addMessage('assistant', '⚠️ Не удалось сгенерировать файл: ' + (err.message || err),
        [], [
        {action: '__pricelist_commit', label: '📥 Загрузить в каталог',
         params: {import_id: imp.import_id}},
      ]);
    }
  }

  // Loading state в side panel пока генерится XLSX
  window.showSidePreviewLoading = function(message) {
    var panel = document.getElementById('sidePreview');
    var body = document.getElementById('sidePreviewBody');
    var nameEl = document.getElementById('sidePreviewName');
    var metaEl = document.getElementById('sidePreviewMeta');
    if (!panel || !body) return;
    if (nameEl) nameEl.textContent = 'Генерирую файл…';
    if (metaEl) metaEl.textContent = 'XLSX';
    body.innerHTML =
      '<div class="opx-gen-loading">'
      + '<div class="opx-gen-spinner"></div>'
      + '<div class="opx-gen-message">' + esc(message) + '</div>'
      + '<div class="opx-gen-counter" id="opxGenCounter">обработано 0 строк</div>'
      + '<div class="opx-gen-progress-bar"><div class="opx-gen-progress-fill" id="opxGenFill"></div></div>'
      + '</div>';
    panel.hidden = false;
  };

  window.updateSidePreviewLoading = function(current) {
    var counter = document.getElementById('opxGenCounter');
    var fill = document.getElementById('opxGenFill');
    if (counter) counter.textContent = 'обработано ' + current.toLocaleString('ru') + ' строк';
    if (fill) {
      // Псевдо-прогресс: чем больше current — тем ближе к 95% (никогда не 100%
      // пока не пришёл финальный ответ).
      var pct = Math.min(95, Math.log10(Math.max(current, 1)) * 18);
      fill.style.width = pct + '%';
    }
  };

  // ── Claude.ai-style smart questionnaire ────────────────────────
  // Показывает вопросы ПО ОДНОМУ: текст + chip-кнопки + ввод.
  // Ответы накапливаются в __pendingImport.smart_answers и
  // применяются при commit как constants или transform_rules.

  function showNextSmartQuestion(questions, idx) {
    if (idx >= questions.length) {
      // Все вопросы пройдены — генерим выходной XLSX (как у claude.ai)
      // и показываем downloadable карточку + кнопку Загрузить в каталог.
      generateAndShowOutputFile();
      return;
    }
    var q = questions[idx];
    // Безопасный рендер через зарегистрированный type 'smart_question' —
    // вместо raw_html (удалён ради S3/XSS). Все поля экранируются в
    // рендерере, поэтому здесь просто передаём data как есть.
    var stream = document.getElementById('stream');
    var savedScroll = (idx === 0 && stream) ? stream.scrollTop : null;
    // shipment_country пробрасываем в каждый вопрос — для country_picker
    // используется как preselected value, для остальных supplier-wide
    // используется чтобы отфильтровать datalist портов/городов по стране.
    // __pendingImport объявлен в IIFE-scope (не на window) — обращаемся
    // напрямую без window. префикса, иначе всегда был бы пустой curCountry.
    var curCountry = '';
    try {
      var c = (__pendingImport && __pendingImport.constants) || {};
      curCountry = c.shipment_country || '';
    } catch (e) {}
    var qMsg = addMessage('assistant', '', [{
      type: 'smart_question',
      data: {
        question:           q.question || '',
        hint:               q.hint || '',
        render:             q.render || '',
        options:            q.options || [],
        'default':          q['default'] || '',
        placeholder:        q.placeholder || '',
        q_idx:              idx,
        total:              questions.length,
        field:              q.field || '',
        apply_as:           q.apply_as || 'constant',
        suggestions_source: q.suggestions_source || '',
        skippable:          !!q.skippable,
        country_picker:     !!q.country_picker,
        shipment_country:   curCountry,
      },
    }], []);
    if (savedScroll !== null && stream) {
      setTimeout(function(){ stream.scrollTop = savedScroll; }, 10);
    } else if (idx > 0 && qMsg && qMsg.scrollIntoView) {
      // Последующие вопросы — скроллим чтобы был виден следующий шаг
      setTimeout(function() {
        qMsg.scrollIntoView({block:'end', behavior:'smooth'});
      }, 50);
    }
    // Сохраняем контекст для обработчиков
    window.__smartQuestions = questions;
    window.__smartQuestionIdx = idx;
  }

  function applySmartAnswer(field, applyAs, value) {
    if (!__pendingImport) return;
    if (!value || !value.trim()) return;
    value = value.trim();
    __pendingImport.smart_answers = __pendingImport.smart_answers || {};
    __pendingImport.smart_answers[field] = {value: value, apply_as: applyAs};
    if (applyAs === 'formula') {
      // «+15%» → 15 → формула price_exw * (1 + 15/100) для поля field
      var pctMatch = String(value).match(/([\d.]+)/);
      if (pctMatch) {
        var pct = parseFloat(pctMatch[1]);
        if (!isNaN(pct)) {
          __pendingImport.transform_rules = __pendingImport.transform_rules || {};
          __pendingImport.transform_rules[field] = {
            type: 'formula',
            formula: 'price_exw * (1 + ' + pct + ' / 100)',
          };
        }
      }
    } else {
      // constant — прямое значение
      __pendingImport.constants = __pendingImport.constants || {};
      __pendingImport.constants[field] = value;
      // Синхронизируем с формой дефолтов (если такое поле есть в pl-df-input),
      // иначе defaults-section перезатрёт наш smart answer.
      var dfInput = document.querySelector('.pl-df-input[data-field="' + field + '"]');
      if (dfInput) {
        // Для <select> добавим option если такого нет
        if (dfInput.tagName === 'SELECT') {
          var found = false;
          for (var i = 0; i < dfInput.options.length; i++) {
            if (dfInput.options[i].value === value) { found = true; break; }
          }
          if (!found) {
            var opt = document.createElement('option');
            opt.value = value; opt.textContent = value;
            dfInput.appendChild(opt);
          }
        }
        dfInput.value = value;
      }
    }
  }

  document.addEventListener('click', function(e) {
    // Клик по chip
    var chip = e.target.closest('.sq-chip');
    if (chip) {
      e.preventDefault();
      e.stopPropagation();
      var card = chip.closest('.smart-q-card');
      var answer = chip.dataset.answer || '';
      finalizeSmartAnswer(card, answer);
      return;
    }
    // Клик «→» submit
    var submit = e.target.closest('.sq-submit');
    if (submit) {
      e.preventDefault();
      e.stopPropagation();
      var card = submit.closest('.smart-q-card');
      var input = card.querySelector('.sq-input');
      var countrySel = card.querySelector('.sq-country');
      var citySel = card.querySelector('.sq-city');
      // Сохраняем страну ОТДЕЛЬНО (shipment_country constant) если есть picker.
      if (countrySel && countrySel.value) {
        applySmartAnswer('shipment_country', 'constant', countrySel.value);
      }
      // Если в карточке есть city picker → собираем {улица, город, страна}
      // как полный warehouse_address. Иначе обычное значение из input
      // (для render:'select' это комбобокс — берём то, что в поле).
      var finalValue = input ? input.value : '';
      if (citySel) {
        var street = (input && input.value || '').trim();
        var city = String(citySel.value || '').trim();
        var countryName = '';
        if (countrySel) {
          if (countrySel.tagName === 'SELECT') {
            var opt = countrySel.options[countrySel.selectedIndex];
            countryName = (opt && opt.text || '').replace(/^[\s\S]*?\s/, '');  // strip flag + space
          } else {
            countryName = String(countrySel.value || '').trim();  // custom input
          }
        }
        var parts = [street, city, countryName].filter(function(p){ return p && p.length; });
        finalValue = parts.join(', ');
      }
      finalizeSmartAnswer(card, finalValue);
      return;
    }
    // Skip (рендерится только для вопросов с skippable=true)
    var skip = e.target.closest('.sq-skip');
    if (skip) {
      e.preventDefault();
      e.stopPropagation();
      var skipCard = skip.closest('.smart-q-card');
      finalizeSmartAnswer(skipCard, '');
      return;
    }
  });

  // При смене страны в smart-question карточке:
  //   1) "__custom__" — юзер хочет вписать свою страну → заменяем
  //      <select> на <input type="text"> с тем же классом sq-country,
  //      submit handler работает на .value одинаково.
  //   2) Иначе — перерисовываем datalist городов под выбранную страну
  //      и очищаем поле города.
  document.addEventListener('change', function(e) {
    var sel = e.target;
    if (!sel || !sel.classList || !sel.classList.contains('sq-country')) return;
    var card = sel.closest('.smart-q-card');
    if (!card) return;
    if (sel.tagName === 'SELECT' && sel.value === '__custom__') {
      var inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'sq-country';
      inp.placeholder = 'введите страну';
      sel.parentNode.replaceChild(inp, sel);
      inp.focus();
      // Очищаем подсказки городов — для произвольной страны их нет.
      var cityInpEl = card.querySelector('.sq-city');
      if (cityInpEl) {
        cityInpEl.value = '';
        var dlIdEl = cityInpEl.getAttribute('list');
        var dlEl = dlIdEl ? card.querySelector('#' + CSS.escape(dlIdEl)) : null;
        if (dlEl) dlEl.innerHTML = '';
      }
      return;
    }
    var cityInp = card.querySelector('.sq-city');
    if (!cityInp) return;
    var dlId = cityInp.getAttribute('list');
    var dl = dlId ? card.querySelector('#' + CSS.escape(dlId)) : null;
    if (!dl) return;
    var code = sel.value;
    var cities = (code && SUGGEST_BY_COUNTRY[code] && SUGGEST_BY_COUNTRY[code].city) || [];
    dl.innerHTML = cities.map(function(c){
      var safe = String(c).replace(/[<>"']/g, '');
      return '<option value="' + safe + '"></option>';
    }).join('');
    cityInp.value = '';  // старый город из другой страны — сбрасываем
  });

  // Enter в input → submit
  document.addEventListener('keydown', function(e) {
    if (e.key !== 'Enter') return;
    var input = e.target.closest('.sq-input');
    if (!input) return;
    // Combobox (.pl-ac): если попап открыт и стрелками подсвечен пункт —
    // отдаём Enter автокомплиту (он выберет пункт), не сабмитим раньше времени.
    if (input.classList.contains('pl-ac') && _acInput === input
        && _acPop && _acPop.style.display !== 'none' && _acActiveIdx >= 0) {
      return;
    }
    e.preventDefault();
    var card = input.closest('.smart-q-card');
    var countrySel = card.querySelector('.sq-country');
    var citySel = card.querySelector('.sq-city');
    if (countrySel && countrySel.value) {
      applySmartAnswer('shipment_country', 'constant', countrySel.value);
    }
    var finalValue = input.value;
    if (citySel) {
      var street = (input.value || '').trim();
      var city = String(citySel.value || '').trim();
      var countryName = '';
      if (countrySel) {
        var opt = countrySel.options[countrySel.selectedIndex];
        countryName = (opt && opt.text || '').replace(/^[\s\S]*?\s/, '');
      }
      var parts = [street, city, countryName].filter(function(p){ return p && p.length; });
      finalValue = parts.join(', ');
    }
    finalizeSmartAnswer(card, finalValue);
  });

  function finalizeSmartAnswer(card, value) {
    if (!card) return;
    var field = card.dataset.field;
    var applyAs = card.dataset.applyAs;
    var idx = parseInt(card.dataset.qIdx, 10);
    if (value && value.trim()) applySmartAnswer(field, applyAs, value);
    // Сохраняем оригинальный вопрос, показываем выбранный ответ
    var qText = card.querySelector('.sq-q');
    var qHtml = qText ? qText.outerHTML : '';
    card.classList.add('sq-card-done');
    var label = value && value.trim() ? value : '(пропущено)';
    card.innerHTML = qHtml + '<div class="sq-answer">✓ ' + esc(label) + '</div>';
    setTimeout(function() {
      showNextSmartQuestion(window.__smartQuestions || [], idx + 1);
    }, 250);
  }

  // Review-таблица AI оценок — collaborative correction
  function renderAiReview(card, importId, sampleItems, lowConfCount) {
    var existing = card.querySelector('.pl-ai-review');
    if (existing) existing.remove();

    var hdr = '<div class="pl-ai-review-hdr">🔍 Проверьте оценки AI'
      + (lowConfCount > 0
          ? ' · <span class="pl-ai-lowconf">⚠️ ' + lowConfCount + ' с низкой уверенностью</span>'
          : '')
      + '<div class="pl-ai-review-sub">Поправьте если что-то не так — мы запомним. AI оценил по описанию, но человек точнее.</div>'
      + '</div>';
    var rows = sampleItems.map(function(it) {
      var lowCls = it.confidence < 0.6 ? ' pl-ai-row-lowconf' : '';
      var confPct = Math.round(it.confidence * 100);
      return '<tr class="pl-ai-row' + lowCls + '" data-oem="' + esc(it.oem) + '">'
        + '<td class="pl-ai-cell-title">'
        +   '<div class="pl-ai-oem">' + esc(it.oem) + '</div>'
        +   '<div class="pl-ai-name">' + esc(it.title) + '</div>'
        + '</td>'
        + '<td><input class="pl-ai-input" data-field="weight_kg" type="number" step="0.1" value="' + it.weight_kg + '"/> кг</td>'
        + '<td><input class="pl-ai-input" data-field="length_cm" type="number" step="1" value="' + it.length_cm + '"/></td>'
        + '<td><input class="pl-ai-input" data-field="width_cm"  type="number" step="1" value="' + it.width_cm  + '"/></td>'
        + '<td><input class="pl-ai-input" data-field="height_cm" type="number" step="1" value="' + it.height_cm + '"/></td>'
        + '<td class="pl-ai-conf">' + confPct + '%</td>'
        + '</tr>';
    }).join('');
    var html = '<div class="pl-ai-review">'
      + hdr
      + '<table class="pl-ai-table">'
      +   '<thead><tr><th>Позиция</th><th>Вес</th><th>Д, см</th><th>Ш, см</th><th>В, см</th><th>AI</th></tr></thead>'
      +   '<tbody>' + rows + '</tbody>'
      + '</table>'
      + '</div>';
    card.insertAdjacentHTML('beforeend', html);
  }

  // Фильтрация datalist портов при выборе страны.
  // Бизнес-логика: страна отправления одна для морпорта И аэропорта,
  // поэтому селекторы синхронизируются.
  // Сортировка портов по близости к городу/адресу склада. Юзер набирает
  // city + street → выделяем значащие слова (длина ≥3, без цифр/знаков),
  // считаем сколько из них встречается в названии порта. Порты с
  // максимальным совпадением — сверху, остальные в исходном порядке.
  // Cosmetic боост: «Shanghai» в адресе → Shanghai (CNSHA) и Pudong (PVG)
  // оба содержат «shanghai» → выше Ningbo/Qingdao по стране.
  function _proximityScore(port, keywords) {
    if (!keywords.length) return 0;
    var p = String(port).toLowerCase();
    var s = 0;
    for (var i = 0; i < keywords.length; i++) {
      if (p.indexOf(keywords[i]) >= 0) s++;
    }
    return s;
  }
  function _sortPortsByProximity(ports, city, address) {
    var text = ((city || '') + ' ' + (address || '')).toLowerCase();
    var keywords = text.split(/[\s,.\-/№#"'()]+/)
      .filter(function(w){ return w.length >= 3 && !/^\d+$/.test(w); });
    if (!keywords.length) return ports.slice();
    return ports.slice().sort(function(a, b) {
      return _proximityScore(b, keywords) - _proximityScore(a, keywords);
    });
  }
  function _portHintsFromCard(card) {
    if (!card) card = document;
    var cityEl = card.querySelector('.pl-city-input');
    var addrEl = card.querySelector('.pl-wh-street, .pl-df-input[data-field="warehouse_address"]');
    return {
      city: cityEl ? String(cityEl.value || '').trim() : '',
      address: addrEl ? String(addrEl.value || '').trim() : '',
    };
  }
  // ── Провайдеры items для кастомного автокомплита ──────────────────
  // Город по стране (плоский список). Возвращает [] если страна не выбрана.
  function _getCityItems(cc) {
    return (cc && SUGGEST_BY_COUNTRY[cc] && SUGGEST_BY_COUNTRY[cc].city) || [];
  }
  // Код порта из строки автокомплита: "CNSHA · Shanghai · …" → "CNSHA".
  function _portCode(s) {
    return String(s).split('·')[0].trim();
  }
  // Порты по стране, отсортированные по близости к складу:
  //  1) если известны координаты склада (_warehouseCoords) — РЕАЛЬНОЕ гео-
  //     расстояние (haversine) до каждого порта по PORT_COORDS. Шэньян →
  //     Далянь сверху, Шанхай ниже.
  //  2) иначе fallback на текстовую близость (совпадение названия города).
  function _getPortItems(cc, kind, hints) {
    var items = cc ? getPortsByCountry(cc, kind) : (kind === 'sea' ? SEA_PORTS : AIR_PORTS);
    if (_warehouseCoords) {
      var wc = _warehouseCoords;
      var withDist = items.map(function(s){
        var c = PORT_COORDS[_portCode(s)];
        var d = c ? _haversineKm(wc[0], wc[1], c[0], c[1]) : 1e9;  // без коорд → в конец
        return { s: s, d: d };
      });
      withDist.sort(function(a, b){ return a.d - b.d; });
      return withDist.map(function(x){ return x.s; });
    }
    if (hints && (hints.city || hints.address)) {
      items = _sortPortsByProximity(items, hints.city, hints.address);
    }
    return items;
  }
  // Возвращает items для конкретного AC-инпута по его data-ac-kind.
  function _acItemsFor(input) {
    var kind = input.getAttribute('data-ac-kind');
    var card = input.closest('.pl-defaults-card');
    var top = card ? card.querySelector('#shipment_country') : document.getElementById('shipment_country');
    var cc = top ? top.value : '';
    if (kind === 'city') return _getCityItems(cc);
    if (kind === 'sea' || kind === 'air') {
      return _getPortItems(cc, kind, _portHintsFromCard(card));
    }
    if (kind === 'manuf') {
      // Статический список брендов из data-атрибута (рендерится в мастере).
      var raw = input.getAttribute('data-ac-options') || '';
      return raw ? raw.split('|') : [];
    }
    return [];
  }

  // ── Кастомный автокомплит (substring, двуязычный) ─────────────────
  // Нативный <datalist> в Chrome фильтрует по ПРЕФИКСУ значения, поэтому
  // "Beijing / Пекин" не находится при наборе "Пек". Свой попап ищет
  // подстроку в любой части строки, на любом языке. Один глобальный
  // элемент попапа переиспользуется всеми AC-инпутами через делегирование.
  var _acPop = null;        // DOM-элемент попапа
  var _acInput = null;      // активный input
  var _acActiveIdx = -1;    // индекс подсвеченного пункта (для стрелок)

  function _acEnsurePop() {
    if (_acPop) return _acPop;
    _acPop = document.createElement('div');
    _acPop.className = 'pl-ac-pop';
    _acPop.style.display = 'none';
    document.body.appendChild(_acPop);
    // Клик по пункту (mousedown — раньше blur, чтобы не закрылось до клика).
    _acPop.addEventListener('mousedown', function(e) {
      var item = e.target.closest('.pl-ac-item');
      if (!item || !_acInput) return;
      e.preventDefault();
      var inp = _acInput;
      inp.value = item.getAttribute('data-val') || '';
      // Если выбрали город — запоминаем его координаты для гео-сортировки портов.
      if (inp.getAttribute('data-ac-kind') === 'city') _maybeSetWarehouseCoords(inp.value);
      inp.dispatchEvent(new Event('input', {bubbles: true}));
      _acHide();
    });
    return _acPop;
  }
  function _acHide() {
    if (_acPop) _acPop.style.display = 'none';
    _acInput = null;
    _acActiveIdx = -1;
  }
  function _acPosition(input) {
    var r = input.getBoundingClientRect();
    _acPop.style.position = 'fixed';
    _acPop.style.left = r.left + 'px';
    _acPop.style.top = (r.bottom + 2) + 'px';
    _acPop.style.width = Math.max(r.width, 240) + 'px';
  }
  // Рисует попап из готового массива строк для конкретного input.
  function _acShowItems(input, items) {
    var pop = _acEnsurePop();
    if (!items || !items.length) { if (_acInput === input) _acHide(); return; }
    _acActiveIdx = -1;
    pop.innerHTML = items.slice(0, 60).map(function(s){
      return '<div class="pl-ac-item" data-val="' + esc(String(s)) + '">'
        + esc(String(s)) + '</div>';
    }).join('');
    _acInput = input;
    _acPosition(input);
    pop.style.display = 'block';
  }
  // Город ищется через backend (GeoNames ≈32k городов, поиск en+ru).
  // Порты — локальные списки (их немного, они полные). Debounce запросов.
  var _geoTimer = null, _geoSeq = 0;
  function _acRender(input) {
    _acEnsurePop();
    var q = String(input.value || '').trim().toLowerCase();
    var kind = input.getAttribute('data-ac-kind');
    if (kind === 'city') {
      // мгновенно — локальные топ-города (если есть), потом дополним с сервера
      var local = _acItemsFor(input);
      var localMatched = q
        ? local.filter(function(s){ return String(s).toLowerCase().indexOf(q) >= 0; })
        : local;
      _acShowItems(input, localMatched);
      // backend-поиск: полнота 32k городов
      var card = input.closest('.pl-defaults-card');
      var top = card ? card.querySelector('#shipment_country') : document.getElementById('shipment_country');
      var cc = top ? (top.value || '') : '';
      var seq = ++_geoSeq;
      clearTimeout(_geoTimer);
      _geoTimer = setTimeout(function(){
        fetch('/api/assistant/geo-cities/?q=' + encodeURIComponent(q) + '&cc=' + encodeURIComponent(cc), {credentials:'same-origin'})
          .then(function(r){ return r.json(); })
          .then(function(d){
            if (seq !== _geoSeq || _acInput !== input) return;  // устарел/потерял фокус
            // Сервер отдаёт [{n, lat, lng}]. Запоминаем координаты каждого
            // города в _cityCoordsMap → при выборе склада узнаем его lat/lng
            // и сортируем порты по реальному расстоянию.
            var serverCities = (d.cities || []).map(function(o){
              if (o && typeof o === 'object') {
                if (o.lat != null && o.lng != null) {
                  _cityCoordsMap[String(o.n).toLowerCase()] = [o.lat, o.lng];
                }
                return o.n;
              }
              return o;  // на случай старого формата (строка)
            });
            var merged = [], seen = {};
            localMatched.concat(serverCities).forEach(function(s){
              var keyName = String(s).split(' / ')[0].trim().toLowerCase();
              if (seen[keyName]) return;
              seen[keyName] = 1; merged.push(s);
            });
            _acShowItems(input, merged);
          })
          .catch(function(){ /* оставляем локальные */ });
      }, 180);
      return;
    }
    if (kind === 'sea' || kind === 'air') {
      var pcard = input.closest('.pl-defaults-card');
      var ptop = pcard ? pcard.querySelector('#shipment_country') : document.getElementById('shipment_country');
      var pcc = ptop ? (ptop.value || '') : '';
      // 1) МГНОВЕННО — крупные хабы страны (локальный PORTS_BY_COUNTRY),
      //    осмысленный выпадающий список сразу при фокусе/наборе.
      var localPorts = _acItemsFor(input);
      var localMatchedP = q
        ? localPorts.filter(function(s){ return String(s).toLowerCase().indexOf(q) >= 0; })
        : localPorts;
      if (localMatchedP.length) _acShowItems(input, localMatchedP);
      // 2) Если известны координаты склада — подменяем на РЕАЛЬНЫЕ ближайшие
      //    порты из полной базы (geo-ports, все 244 страны, по расстоянию).
      if (_warehouseCoords) {
        var coordQ = '&lat=' + _warehouseCoords[0] + '&lng=' + _warehouseCoords[1];
        var pseq = ++_geoSeq;
        clearTimeout(_geoTimer);
        _geoTimer = setTimeout(function(){
          fetch('/api/assistant/geo-ports/?cc=' + encodeURIComponent(pcc)
                + '&kind=' + kind + '&q=' + encodeURIComponent(q) + coordQ,
                {credentials:'same-origin'})
            .then(function(r){ return r.json(); })
            .then(function(d){
              if (pseq !== _geoSeq || _acInput !== input) return;
              var ports = (d.ports || []).map(function(p){ return p.label; });
              if (ports.length) _acShowItems(input, ports);
            })
            .catch(function(){});
        }, 150);
      } else if (!localMatchedP.length && pcc) {
        // Нет ни координат, ни локальных хабов (страна вне топ-10) — берём
        // geo-ports без сортировки (хоть какой-то список реальных портов).
        var pseq2 = ++_geoSeq;
        clearTimeout(_geoTimer);
        _geoTimer = setTimeout(function(){
          fetch('/api/assistant/geo-ports/?cc=' + encodeURIComponent(pcc)
                + '&kind=' + kind + '&q=' + encodeURIComponent(q),
                {credentials:'same-origin'})
            .then(function(r){ return r.json(); })
            .then(function(d){
              if (pseq2 !== _geoSeq || _acInput !== input) return;
              _acShowItems(input, (d.ports || []).map(function(p){ return p.label; }));
            })
            .catch(function(){});
        }, 150);
      }
      return;
    }
    // Прочее — локальный список, substring-фильтр.
    var all = _acItemsFor(input);
    var matched = q
      ? all.filter(function(s){ return String(s).toLowerCase().indexOf(q) >= 0; })
      : all;
    _acShowItems(input, matched);
  }

  // focusin → открыть попап, НО readonly НЕ снимаем (это ключ к подавлению
  // нативного Chrome address-autofill: он не всплывает над readonly-полем).
  // Клик по пункту нашего попапа пишет value программно — readonly не мешает.
  document.addEventListener('focusin', function(e) {
    var input = e.target.closest && e.target.closest('.pl-ac');
    if (!input) return;
    _acRender(input);
  });
  // keydown с печатаемым символом → снимаем readonly синхронно (символ
  // успевает ввестись). Chrome autofill уже не покажется — он триггерит
  // только на focus/click, а к моменту печати фокус давно установлен.
  document.addEventListener('keydown', function(e) {
    var input = e.target.closest && e.target.closest('.pl-ac');
    if (!input || !input.hasAttribute('readonly')) return;
    // Снимаем только для вводящих клавиш (буквы/цифры/Backspace), не для Tab/Esc.
    if (e.key && (e.key.length === 1 || e.key === 'Backspace' || e.key === 'Delete')) {
      input.removeAttribute('readonly');
    }
  });
  // input → перефильтровать (и обновить позицию)
  document.addEventListener('input', function(e) {
    var input = e.target.closest && e.target.closest('.pl-ac');
    if (input) { _acRender(input); }
  });
  // клик вне попапа и вне инпута → закрыть
  document.addEventListener('mousedown', function(e) {
    if (_acPop && _acPop.contains(e.target)) return;
    if (e.target.closest && e.target.closest('.pl-ac')) return;
    _acHide();
  });
  // навигация стрелками + Enter/Escape в активном AC-инпуте
  document.addEventListener('keydown', function(e) {
    if (!_acInput || !_acPop || _acPop.style.display === 'none') return;
    if (e.target !== _acInput) return;
    var items = _acPop.querySelectorAll('.pl-ac-item');
    if (!items.length) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      _acActiveIdx += (e.key === 'ArrowDown' ? 1 : -1);
      if (_acActiveIdx < 0) _acActiveIdx = items.length - 1;
      if (_acActiveIdx >= items.length) _acActiveIdx = 0;
      items.forEach(function(it, i){ it.classList.toggle('pl-ac-active', i === _acActiveIdx); });
      items[_acActiveIdx].scrollIntoView({block: 'nearest'});
    } else if (e.key === 'Enter') {
      if (_acActiveIdx >= 0 && items[_acActiveIdx]) {
        e.preventDefault();
        _acInput.value = items[_acActiveIdx].getAttribute('data-val') || '';
        if (_acInput.getAttribute('data-ac-kind') === 'city') _maybeSetWarehouseCoords(_acInput.value);
        _acInput.dispatchEvent(new Event('input', {bubbles: true}));
        _acHide();
      }
    } else if (e.key === 'Escape') {
      _acHide();
    }
  });
  // скролл/resize → перепозиционировать открытый попап
  window.addEventListener('scroll', function(){ if (_acInput) _acPosition(_acInput); }, true);
  window.addEventListener('resize', function(){ if (_acInput) _acPosition(_acInput); });

  // Кнопка «Очистить» в баннере «из прошлой загрузки»: сбрасывает порты,
  // склад и страну до пустых значений, чтобы юзер заполнил с нуля.
  document.addEventListener('click', function(e) {
    var btn = e.target.closest('.pl-clear-profile');
    if (!btn) return;
    e.preventDefault();
    var card = btn.closest('.pl-defaults-card');
    if (!card) return;
    var top = card.querySelector('#shipment_country');
    if (top) top.value = '';
    card.querySelectorAll('.pl-df-input[data-field]').forEach(function(inp) {
      var f = inp.dataset.field;
      if (f === 'sea_port' || f === 'air_port' || f === 'warehouse_address') {
        inp.value = '';
      }
    });
    var cityInp = card.querySelector('.pl-city-input');
    if (cityInp) cityInp.value = '';
    var banner = btn.closest('.pl-from-profile');
    if (banner) banner.remove();
  });
  // Смена страны → очищаем город и порты (старые значения из другой страны).
  // Списки items подтянутся сами при следующем фокусе на AC-инпут.
  document.addEventListener('change', function(e) {
    var sel = e.target.closest('.pl-ship-country, .pl-port-country');
    if (!sel) return;
    var cc = sel.value;
    var top = document.getElementById('shipment_country');
    if (top && top !== sel) top.value = cc;
    var card = sel.closest('.pl-defaults-card') || document;
    var cityInp = card.querySelector('.pl-city-input');
    if (cityInp) cityInp.value = '';
    card.querySelectorAll('.pl-port-input').forEach(function(inp) {
      inp.value = '';
    });
    _warehouseCoords = null;  // координаты склада сбрасываем — город сменится
  });

  // Side preview panel (как у claude.ai) — открывается по клику
  // на карточку файла или кнопку «↗ Открыть».
  window.openSidePreview = async function(importId, filename, downloadUrl) {
    var panel = document.getElementById('sidePreview');
    var body = document.getElementById('sidePreviewBody');
    var nameEl = document.getElementById('sidePreviewName');
    var metaEl = document.getElementById('sidePreviewMeta');
    var dlEl = document.getElementById('sidePreviewDownload');
    if (!panel || !body) return;
    if (nameEl) nameEl.textContent = filename || 'Файл';
    if (metaEl) metaEl.textContent = 'XLSX';
    if (dlEl) { dlEl.href = downloadUrl || '#'; dlEl.setAttribute('download', filename || ''); }
    // Если panel уже открыт с loading-стейтом (от генерации) — не мигаем
    // плоским «Загружаю превью…», оставляем спиннер. Иначе показываем
    // красивый loading с спиннером.
    var hasGenLoading = body.querySelector('.opx-gen-loading');
    if (!hasGenLoading) {
      body.innerHTML =
        '<div class="opx-gen-loading">'
        + '<div class="opx-gen-spinner"></div>'
        + '<div class="opx-gen-message">Загружаю превью…</div>'
        + '</div>';
    }
    panel.hidden = false;
    try {
      var res = await fetch('/api/assistant/upload-pricelist/' + importId + '/output-preview/', {
        credentials: 'same-origin',
      });
      var html = await res.text();
      var match = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
      body.innerHTML = match ? match[1] : html;
    } catch (e) {
      body.innerHTML = '<div class="opx-gen-loading"><div class="opx-gen-message">⚠️ Ошибка: ' + (e.message || e) + '</div></div>';
    }
  };
  window.closeSidePreview = function() {
    var panel = document.getElementById('sidePreview');
    if (!panel) return;
    panel.hidden = true;
    var body = document.getElementById('sidePreviewBody');
    if (body) body.innerHTML = '';
  };

  // Drag-to-resize side panel — ручка слева тянется в любую сторону
  (function() {
    var panel = document.getElementById('sidePreview');
    var resizer = document.getElementById('sidePreviewResizer');
    if (!panel || !resizer) return;

    // Восстанавливаем сохранённую ширину
    try {
      var saved = parseInt(localStorage.getItem('cf_side_preview_width'), 10);
      if (saved && saved > 320) panel.style.width = saved + 'px';
    } catch(e){}

    var dragging = false;
    var startX = 0;
    var startWidth = 0;

    resizer.addEventListener('mousedown', function(e) {
      dragging = true;
      startX = e.clientX;
      startWidth = panel.getBoundingClientRect().width;
      panel.classList.add('resizing');
      resizer.classList.add('active');
      document.body.classList.add('side-preview-resizing');
      e.preventDefault();
    });

    document.addEventListener('mousemove', function(e) {
      if (!dragging) return;
      // Панель справа: тянем влево → шире, вправо → уже
      var delta = startX - e.clientX;
      var newWidth = startWidth + delta;
      var minW = 320;
      var maxW = window.innerWidth - 200;
      if (newWidth < minW) newWidth = minW;
      if (newWidth > maxW) newWidth = maxW;
      panel.style.width = newWidth + 'px';
    });

    document.addEventListener('mouseup', function() {
      if (!dragging) return;
      dragging = false;
      panel.classList.remove('resizing');
      resizer.classList.remove('active');
      document.body.classList.remove('side-preview-resizing');
      try {
        var w = parseInt(panel.style.width, 10);
        if (w) localStorage.setItem('cf_side_preview_width', String(w));
      } catch(e){}
    });

    // Double-click — сброс ширины к дефолту (50vw)
    resizer.addEventListener('dblclick', function() {
      panel.style.width = '50vw';
      try { localStorage.removeItem('cf_side_preview_width'); } catch(e){}
    });
  })();

  document.addEventListener('click', function(e) {
    var btn = e.target.closest('.of-open-preview');
    if (!btn) return;
    e.preventDefault(); e.stopPropagation();
    var card = btn.closest('.of-card');
    if (!card) return;
    openSidePreview(
      card.dataset.importId,
      card.dataset.filename,
      card.dataset.download,
    );
  });

  // Esc закрывает side panel
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      var p = document.getElementById('sidePreview');
      if (p && !p.hidden) closeSidePreview();
    }
  });

  // Запуск AI-оценки по нажатию кнопки (НЕ auto-start — пусть
  // юзер сам решает нужен ли ему AI). По клику показываем
  // progress area и стартуем.
  document.addEventListener('click', function(e) {
    var btn = e.target.closest('.pl-ai-start-btn');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    var card = btn.closest('.pl-ai-progress-card');
    if (!card) return;
    btn.style.display = 'none';
    var area = card.querySelector('.pl-ai-progress-area');
    if (area) area.style.display = 'block';
    startAiEstimate(card);
  });

  // Перехватываем спец-actions в quickAction'е до отправки на /api/assistant/action/
  const _origQuickActionForPricelist = window.quickAction;
  window.quickAction = (action, params) => {
    if (action === '__pricelist_commit') return window.__pricelist_commit_handler(params);
    if (action === '__pricelist_cancel') return window.__pricelist_cancel_handler(params);
    if (action === '__pricelist_retry_generate') return generateAndShowOutputFile();
    if (action === '__download_url' && params && params.url) {
      var a = document.createElement('a');
      a.href = params.url; a.download = '';
      document.body.appendChild(a); a.click(); a.remove();
      return;
    }
    // Спец-action «открыть file-picker» — отдельная кнопка в карточке
    // upload_pricelist (после клика на pill).
    if (action === '__open_file_picker') {
      const fi = $('fileInput');
      if (fi) {
        fi.accept = (params && params.accept) || '.xlsx,.xls,.csv,.tsv,.txt';
        fi.click();
      }
      return;
    }
    // Пикер чертежа: ставим режим, файл уйдёт в uploadDrawing (не в прайс).
    if (action === '__open_drawing_picker') {
      window.__drawingUpload = true;
      const fi = $('fileInput');
      if (fi) {
        fi.accept = '.pdf,.dwg,.dxf,.step,.stp,.iges,.igs,.stl,.png,.jpg,.jpeg';
        fi.click();
      }
      return;
    }
    // Переключение роли — server-side action /api/assistant/role/
    if (action === '_switch_role') {
      const newRole = (params && params.role) || 'seller';
      if (typeof setRole === 'function') {
        setRole(newRole);
      } else {
        // fallback — full reload через cookie API
        fetch('/api/assistant/role/', {
          method:'POST',
          headers:{'Content-Type':'application/json','X-CSRFToken': csrf()},
          credentials:'same-origin',
          body: JSON.stringify({role: newRole}),
        }).then(() => location.reload());
      }
      return;
    }
    return _origQuickActionForPricelist(action, params);
  };

  // Универсальный handler выбора файла — file-picker или drag-n-drop
  function handleSelectedFile(file) {
    if (!file) return;
    // Режим загрузки чертежа (выставлен __open_drawing_picker) — приоритетно.
    if (window.__drawingUpload) {
      window.__drawingUpload = false;
      uploadDrawing(file);
      return;
    }
    // Seller'у грузим прайс, остальным — спецификацию
    const role = (state.config && state.config.role) || 'buyer';
    if (role === 'seller') {
      uploadPricelist(file);
    } else {
      uploadSpec(file);
    }
  }

  // Загрузка чертежа продавцом → POST /api/assistant/drawings/upload/ (multipart).
  function uploadDrawing(file) {
    showConv();
    // Живой прогресс-бар с процентами (как при загрузке спеки) — большой PDF грузится
    // долго, без индикатора кажется, что всё зависло.
    const sizeKb = file.size ? ' · ' + Math.round(file.size / 1024) + ' KB' : '';
    const pending = addMessage('assistant', '📐 Загружаю чертёж…');
    // addMessage прогоняет текст через linkifyEntities (экранирует HTML), поэтому
    // прогресс-бар задаём innerHTML напрямую в .msg-content — иначе теги видны текстом.
    const _cEl = pending && pending.querySelector && pending.querySelector('.msg-content');
    if (_cEl) _cEl.innerHTML =
      '📐 Загружаю чертёж «' + esc(file.name) + '»' + sizeKb + ' <span class="upl-pct">0%</span>'
      + '<div class="upl-bar"><div class="upl-bar-fill" style="width:0%"></div></div>';
    const fd = new FormData();
    fd.append('file', file);
    _uploadWithProgress('/api/assistant/drawings/upload/', fd, {
      onProgress: (pct) => {
        if (!pending) return;
        const txtEl = pending.querySelector('.upl-pct');
        const barEl = pending.querySelector('.upl-bar-fill');
        if (txtEl) txtEl.textContent = pct + '%';
        if (barEl) barEl.style.width = pct + '%';
        if (pct >= 100) {
          const tEl = pending.querySelector('.msg-content') || pending;
          if (tEl) tEl.innerHTML = '📐 Обрабатываю чертёж…';
        }
      },
      onSuccess: (data) => {
        if (pending && pending.parentNode) pending.remove();
        if (!data || !data.ok) {
          addMessage('assistant', '⚠️ Не удалось загрузить чертёж: ' + ((data && data.error) || 'ошибка'));
          return;
        }
        addMessage('assistant', '✅ Чертёж «' + (data.title || file.name) + '» загружен ('
          + (data.file_format || '').toUpperCase() + '). Он в разделе «Без папки» — '
          + 'привяжите позицию через 🔗 и при желании перетащите в папку. '
          + '🛡 Статус: на модерации у оператора.');
        // Показать обновлённую карточку чертежей (новый — в «Без папки»).
        if (window.quickAction) window.quickAction('seller_drawings', {});
      },
      onError: (err) => {
        if (pending && pending.parentNode) pending.remove();
        addMessage('assistant', '⚠️ Не удалось загрузить чертёж: ' + (err.message || err));
      },
    });
  }

  // Drag-n-drop файла прямо в окно чата (без необходимости жать скрепку)
  let _dragDepth = 0;  // считаем nested dragenter/leave чтобы overlay не моргал
  function _hasFiles(e) {
    const types = e.dataTransfer?.types || [];
    return Array.from(types).includes('Files');
  }
  function _showDropOverlay() {
    let ov = document.getElementById('dndOverlay');
    if (ov) { ov.style.display = 'flex'; return; }
    ov = document.createElement('div');
    ov.id = 'dndOverlay';
    ov.innerHTML = `
      <div class="dnd-box">
        <div class="dnd-icon">📎</div>
        <div class="dnd-title">Бросьте файл сюда</div>
        <div class="dnd-sub">.xlsx · .csv · .pdf · до 20 МБ</div>
      </div>`;
    document.body.appendChild(ov);
  }
  function _hideDropOverlay() {
    const ov = document.getElementById('dndOverlay');
    if (ov) ov.style.display = 'none';
    _dragDepth = 0;
  }
  document.addEventListener('dragenter', (e) => {
    if (!_hasFiles(e)) return;
    _dragDepth += 1;
    _showDropOverlay();
  });
  document.addEventListener('dragleave', (e) => {
    if (!_hasFiles(e)) return;
    _dragDepth -= 1;
    if (_dragDepth <= 0) _hideDropOverlay();
  });
  document.addEventListener('dragover', (e) => {
    if (!_hasFiles(e)) return;
    e.preventDefault();  // обязательно — иначе drop не сработает
    e.dataTransfer.dropEffect = 'copy';
  });
  document.addEventListener('drop', (e) => {
    if (!_hasFiles(e)) return;
    e.preventDefault();
    _hideDropOverlay();
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) handleSelectedFile(file);
  });

  $('fileInput').addEventListener('change', (e) => {
    handleSelectedFile(e.target.files[0]);
    e.target.value = '';
  });

  // Change-listener для form-select: спец-значения
  //   __sep__   — это разделитель, не выбор → откатываем на пустое
  //   __custom__ — спросить через prompt() и сохранить как fix:VALUE
  document.addEventListener('change', (e) => {
    const sel = e.target;
    if (!sel || sel.tagName !== 'SELECT' || !sel.classList.contains('fm-select')) return;
    if (sel.value === '__sep__') {
      sel.value = '';
      return;
    }
    if (sel.value === '__custom__') {
      const lbl = sel.closest('.fm-row')?.querySelector('.fm-label')?.textContent?.trim() || sel.name;
      const v = window.prompt(`Введите своё значение для «${lbl}» (применится ко всем строкам):`, '');
      if (v && v.trim()) {
        const val = 'fix:' + v.trim();
        // Добавляем option-самописец и выбираем его
        let custom = sel.querySelector(`option[value="${val.replace(/"/g, '&quot;')}"]`);
        if (!custom) {
          custom = document.createElement('option');
          custom.value = val;
          custom.textContent = 'Своё: ' + v.trim();
          sel.appendChild(custom);
        }
        sel.value = val;
      } else {
        sel.value = '';
      }
    }
  });

  async function recognizePhoto(file) {
    showConv();
    addMessage('user', '📷 ' + file.name);
    const pending = addMessage('assistant', 'Распознаю шильду…');
    try {
      const fd = new FormData();
      fd.append('photo', file);
      const res = await fetch('/api/assistant/recognize-photo/', {
        method: 'POST',
        headers: {'X-CSRFToken': csrf()},
        body: fd, credentials: 'same-origin',
      });
      const data = await res.json();
      if (pending && pending.parentNode) pending.remove();
      if (data.error) {
        addMessage('assistant', '⚠️ ' + data.error);
        return;
      }
      const t = data.text || '';
      let recognized = t;
      try {
        const j = JSON.parse(t.replace(/^```json\s*/, '').replace(/```$/, ''));
        const parts = [];
        if (j.brand) parts.push('Бренд: ' + j.brand);
        if (j.model) parts.push('Модель: ' + j.model);
        if (j.part_number) parts.push('Артикул: ' + j.part_number);
        if (j.serial) parts.push('Серийный: ' + j.serial);
        if (j.notes) parts.push(j.notes);
        recognized = parts.join('\n') || t;
        // Если есть артикул — сразу предложим search_parts
        if (j.part_number) {
          addMessage('assistant', '✓ Распознал:\n' + recognized,
            [], [{label: '🔍 Найти ' + j.part_number, action: 'search_parts',
                  params: {query: j.part_number}}]);
          return;
        }
      } catch(_){}
      addMessage('assistant', '✓ Распознал:\n' + recognized);
    } catch(err) {
      if (pending && pending.parentNode) pending.remove();
      addMessage('assistant', '⚠️ Ошибка распознавания: ' + (err.message || err));
    }
  }

  $('photoInput').addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    recognizePhoto(file);
    e.target.value = '';
  });

  // ══════════════════════════════════════════════════════════
  // Init
  // ══════════════════════════════════════════════════════════
  async function init() {
    try {
      state.config = await api('/api/assistant/widget-config/');
      const name = state.config.user_name || 'User';
      const initial = name[0].toUpperCase();
      $('sideUserName').textContent = name;
      $('sideUserRole').textContent = (state.config.role || '').replace('operator_', '').replace(/_/g, ' ');
      $('sideAvatar').textContent = initial;
      { const _ta = $('topAvatar'); if (_ta) _ta.textContent = initial; }
      // Активная вкладка role-toggle
      const r = state.config.role || 'buyer';
      const uiRole = r.startsWith('operator') ? 'operator' : (r === 'seller' ? 'seller' : 'buyer');
      paintRoleToggle(uiRole);
      applyRoleWelcome(state.config.role);
      await Promise.all([loadConvList(), loadProjects(), loadNotifications()]);
      applyDefaultSidebar(state.convs.length > 0);
      loadSettings();
    } catch(e) {
      console.warn('Init failed:', e);
      applyDefaultSidebar(false);
    }

    // Keep-alive: держим HTTP(S)-соединение тёплым. Иначе после ~75с простоя
    // (nginx keepalive_timeout) первый клик ловит холодный TLS-хэндшейк до
    // Cloudflare — замеряли ~9с против ~0.6с на тёплом соединении. Лёгкий
    // GET раз в 50с (только когда вкладка видима) переустанавливать соединение
    // не даёт, и любой клик идёт по уже открытому каналу.
    if (!window.__cfKeepAlive) {
      window.__cfKeepAlive = setInterval(function() {
        if (document.visibilityState !== 'visible') return;
        fetch('/robots.txt', { cache: 'no-store', credentials: 'same-origin' })
          .catch(function(){});
      }, 50000);
    }

    // Conversation resolution priority — симметричная sticky-логика:
    //   • Был на welcome → F5 → welcome (sessionStorage.cf_on_welcome=1)
    //   • Был в conv     → F5 → та же conv (sessionStorage.cf_active_conv=ID)
    // Через закрытие вкладки sessionStorage очищается → следующий init
    // показывает welcome.
    try {
      const params = new URLSearchParams(window.location.search);
      const urlConv = params.get('conv');
      const storedConv = getStoredConvId();
      const validIds = new Set((state.convs || []).map(c => c.id));
      // ?new=1 — принудительно welcome-screen, не загружать последний conv
      // (с landing «массовый поиск» приходим именно с этим флагом).
      const forceNew = params.get('new') === '1';
      // FIX: «sticky welcome» — если юзер оказался на welcome (через ?new=1),
      // запоминаем это в sessionStorage. При F5/Cmd+Shift+R остаёмся на
      // welcome, не падая в priority-3 (last conv).
      const stickyWelcome = (() => {
        try { return sessionStorage.getItem('cf_on_welcome') === '1'; }
        catch(_) { return false; }
      })();
      let target = null;
      if (forceNew) {
        // forceNew=1 (после смены роли / клик welcome) → чистим, ставим sticky welcome
        try { sessionStorage.removeItem('cf_active_conv'); } catch(_){}
        try { localStorage.removeItem('cf_active_conv'); } catch(_){}
        try { sessionStorage.setItem('cf_on_welcome', '1'); } catch(_){}
        history.replaceState({}, '', '/chat/');
      } else if (urlConv && validIds.has(urlConv)) {
        target = urlConv;
        try { sessionStorage.removeItem('cf_on_welcome'); } catch(_){}
      } else if (stickyWelcome) {
        // Был на главной → при F5 остаёмся на главной (а не уходим в last conv).
        target = null;
      } else if (storedConv && validIds.has(storedConv)) {
        // F5 в conv → возвращаемся в эту же conv.
        target = storedConv;
      } else if (state.convs && state.convs.length) {
        // Свежее открытие вкладки без storedConv — fallback на последнюю conv.
        // Если хотите всегда welcome — закройте вкладку и откройте заново
        // ИЛИ кликните «+ Новый чат».
        target = state.convs[0].id;
      }
      if (target) {
        await window.openConv(target);
        // Если есть ?run=<action> — выполняем после загрузки conv
        const runAction = params.get('run');
        if (runAction) {
          const actionParams = {};
          for (const [k, v] of params.entries()) {
            if (k === 'run' || k === 'conv') continue;
            // Числовые значения (rfq_id, order_id, quote_id) парсим в int
            const n = parseInt(v, 10);
            actionParams[k] = (String(n) === v && !isNaN(n)) ? n : v;
          }
          setTimeout(() => quickAction(runAction, actionParams), 150);
          // Очистим url чтобы при F5 не повторялось
          history.replaceState({}, '', '/chat/');
        }
        return;
      }
      // Сюда дошли = разговор не восстанавливаем, остаёмся на welcome → раскрываем
      // его (анти-мелькание было нужно только на время попытки восстановления).
      document.documentElement.classList.remove('cf-restoring');
      // Welcome stage — но если ?run= задан, тоже выполняем
      const runAction = params.get('run');
      if (runAction) {
        connectWS();
        const actionParams = {};
        for (const [k, v] of params.entries()) {
          if (k === 'run') continue;
          const n = parseInt(v, 10);
          actionParams[k] = (String(n) === v && !isNaN(n)) ? n : v;
        }
        setTimeout(() => quickAction(runAction, actionParams), 200);
        history.replaceState({}, '', '/chat/');
        updateHeroIcon();
        return;
      }
    } catch(e){ console.warn('conv resolve failed', e); document.documentElement.classList.remove('cf-restoring'); }
    connectWS();
    setTimeout(() => $('heroInput').focus(), 200);
    updateHeroIcon();
  }

  // Auto-grow textareas + update hero icon
  document.addEventListener('input', (e) => {
    if (e.target.id === 'heroInput') {
      e.target.style.height = 'auto';
      e.target.style.height = Math.min(e.target.scrollHeight, 240) + 'px';
      updateHeroIcon();
    } else if (e.target.id === 'input') {
      e.target.style.height = 'auto';
      e.target.style.height = Math.min(e.target.scrollHeight, 160) + 'px';
    }
  });

  window.send = send;
  window.onKey = (e, fromHero) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(fromHero); }
  };

  // Resize handler — reapply mobile vs desktop sidebar logic
  let lastIsMobile = isMobile();
  window.addEventListener('resize', () => {
    const m = isMobile();
    if (m !== lastIsMobile) {
      lastIsMobile = m;
      if (m) $('sidebar').classList.remove('open');
      else applyDefaultSidebar(state.convs.length > 0);
    }
  });

  document.addEventListener('DOMContentLoaded', init);
})();
