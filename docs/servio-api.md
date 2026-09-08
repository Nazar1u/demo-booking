# HMS Servio (SmartSpot) — контракт API віджета бронювання

Результат Фази 1. Знято 2026-09-08 з живого віджета на
`https://riverwood.com.ua/booking/` (Playwright + Chrome), плюс статичний
розбір `https://smartspot.servio.support/ServioQR/js/bookingPage.js`
(1.4 МБ, webpack-бандл).

Усі виклики під час розвідки були **read-only**. `/book`, `/make-payment`,
`/verify` жорстко блокувались на рівні route-перехоплення, тож жодної броні
не створено.

## Базові параметри

| Параметр | Значення |
|---|---|
| Base URL | `https://smartspot.servio.support/ServioQR/hms/api` |
| `companyKey` | `6DFA7A01-9E5E-4087-8073-1E0254672737` (tenant готелю) |
| `hotelID` | `161` (внутрішній id) |
| `apiHotelID` | `1` (він же `currentHotelApiId` у localStorage віджета) |
| Валюта | `980` (UAH, ISO-4217 numeric) |
| Мова | `uk` (доступні `uk, en, ru, kk`) |

## Аутентифікація

**API-ключа немає.** Жодного `Authorization`, жодного підпису. Достатньо знати
`companyKey`. Віджет додає до кожного запиту лише два заголовки
(функція `jr()` у бандлі):

```
X-Language: uk
X-Servio-SessionId: <32 hex>     # клієнтський, генерується самим віджетом
```

`X-Servio-SessionId` живе в `localStorage` як `ServioAppSessionId`. Це не токен
автентифікації — просто кореляційний id сесії. Для бекенда достатньо
генерувати `uuid4().hex` на сесію користувача.

`Authorization: Bearer` існує, але тільки для особистого кабінету
(`/hms/cabinet/*`) — нам не потрібен.

## CORS і серверні виклики

- `access-control-allow-origin: *` на всіх відповідях → з браузера кликати
  технічно можна
- **Перевірено емпірично:** `POST /rooms` з сервера **без** `Origin`, `Referer`
  і браузерного `User-Agent` віддає ті самі 11 типів номерів. Перевірки
  `Referer` немає

**Рішення: усі виклики йдуть через Django як проксі.** Не через технічну
необхідність, а тому що так є логування кожного виклику, немає залежності від
чужої CORS-політики, і ми не тягнемо чужий контракт у фронтенд.

## Формат відповіді — читати уважно

Єдиний конверт на всі ендпоінти:

```json
{ "isError": false, "message": "Операція пройшла успішно", "data": ... }
```

> **HTTP-статус завжди 200 — навіть на помилках.** Помилка сигналізується
> `isError: true`, текст у `message` (локалізований за `X-Language`), `data: null`.
> Клієнт **не має** орієнтуватися на код відповіді.

Перевірені формати помилок (`tests/fixtures/servio/rooms-error-*.json`):

| Ситуація | HTTP | `isError` | `message` |
|---|---|---|---|
| `dateDeparture` < `dateArrival` | 200 | `true` | `Невірна дата` |
| Дати в минулому | 200 | `true` | `Невірна дата` |
| Невідомий `companyKey` | 200 | `true` | `Компанія не знайдена` |
| Немає вільних номерів | 200 | **`false`** | `Операція пройшла успішно`, `data: []` |
| `payment-info` з чужим `account` | 200 | `true` | `Виникла помилка при отриманні рахунків` |

Останній рядок важливий: **відсутність доступності — не помилка**, а порожній
масив. Обробляти окремо від `isError`.

## Ендпоінти

Повний список з бандла:

| Метод | Шлях | Призначення | Потрібен нам |
|---|---|---|---|
| GET | `/company/{companyKey}` | налаштування tenant, мови | так |
| GET | `/hotels/{companyKey}` | список готелів + усі налаштування | так |
| POST | `/rooms` | **пошук доступності** | так |
| POST | `/variant-rooms` | варіанти розміщення | ні |
| POST | `/book` | **створення броні** | так |
| POST | `/make-payment` | **ініціація оплати** | так |
| GET | `/payment-info` | рахунки по броні | можливо |
| POST | `/verify`, `/check-verification` | верифікація контактів | ні (вимкнена) |
| POST | `/parse-document`, `/save-client-documents` | pre-check-in | ні (вимкнений) |
| GET | `/get-client-documents`, `/get-diia-document` | документи клієнта | ні |

### GET `/company/{companyKey}`

Фікстура: `tests/fixtures/servio/company.json`.
Віддає `displayName`, `settings` (перемикачі UI), `availableLanguages`.
Для нас майже не потрібен — беремо звідси лише перелік мов.

### GET `/hotels/{companyKey}`

Фікстура: `tests/fixtures/servio/hotels.json` (69 КБ).
`data` — масив готелів; у нас один. Важливі поля з `data[0].settings`:

| Поле | Значення | Наслідок для нас |
|---|---|---|
| `isPaymentEnabled` | `true` | оплата увімкнена |
| `paymentMethod` | `Оплата на сайті` | — |
| `paymentDisplayMode` | `PaymentIframe` | платіжка вбудовується в iframe |
| `guestLimit` | `10` | максимум гостей на номер |
| `minChildAge` / `maxChildAge` | `0` / `12` | межі для `childrenAges` |
| `defaultCheckInTime` / `checkInTimes` | `14:00` / `["15:00"]` | `timeArrival` |
| `defaultCheckOutTime` / `checkOutTimes` | `12:00` / `["12:00"]` | `timeDeparture` |
| `hotelCurrency` | `980` | UAH |
| `isGuestEmailRequired` | `true` | email обов'язковий |
| `isGuestPhoneNumberRequired` | `true` | телефон обов'язковий |
| `isContactVerificationRequired` | `false` | SMS-верифікація не потрібна |
| `askGuestCountryOfResidence` | `true`, дефолт `ua` | `countryOfResidence` |
| `isPromoCodeAllowed` | `true` | промокоди є (нам не потрібні) |
| `isReservationCancellationEnabled` | `false` | скасування через API немає |
| `displayPrecheckin` | `false` | pre-check-in вимкнений |
| `agreements.*` | усі `false` | чекбокси згод не показуються |

### POST `/rooms` — пошук доступності

Фікстури: `rooms-request.json`, `rooms-response.json`.

**Запит** (реальний, знятий на 10–12 березня 2027):

```json
{
  "companyKey": "6DFA7A01-9E5E-4087-8073-1E0254672737",
  "hotelID": 161,
  "promoCode": null,
  "dateArrival": "2027-03-10",
  "dateDeparture": "2027-03-12",
  "timeArrival": "14:00",
  "timeDeparture": "12:00",
  "rooms": [{ "adults": 2, "children": 0, "childrenAges": [], "index": 0 }],
  "currency": 980
}
```

Дати — `YYYY-MM-DD` (у бандлі: `date.toISOString().split('T')[0]`).
`rooms[]` — масив, бо віджет умів бронювати кілька номерів за раз
(кнопка «Додати номер»). Нам достатньо одного елемента з `index: 0`.

**Відповідь:** `data` — масив типів номерів. Значущі поля:

```
id                        внутрішній id типу номера  -> Booking.room_type_id
apiID                     id у HMS
name                      назва                      -> Booking.room_name
description               HTML-опис
images[]                  {url, urlCompressed, urlResized, position}
freeRoomsCount            вільних номерів на ці дати -> залишок
roomsCount                всього номерів цього типу
mainPlacesCount           основних місць
nearestDateToReservation  найближча доступна дата
hasBath/hasShower/...     зручності, *BedCount       -> картка номера
contractConditions[]      тарифи (у Riverwood рівно один)
```

`contractConditions[]` — це і є оффер, який користувач вибирає:

```
id                 -> contractConditionID у /book
apiID
name               "Стандартний тариф"
apiPriceListID     -> apiPriceListID у /book
totalPrice         сума за весь період      -> Booking.price
totalRackRatePrice ціна без скидки
currency           980
paidTypes[]        [100, 300, 200] — допустимі типи оплати
services[]         { name, serviceSystemCode: "dwelling",
                     priceDates: [{ price, rackRatePrice, date }] }  ціна по днях
saleRestrictions   closedToSale / closedToArrive / minStay / maxStay /
                   earlyReservation / minPay / percentageMinPay
                   — кожне з { hasRestrictions, message }
```

**`saleRestrictions` треба перевіряти перед показом оффера** — інакше
користувач вибере тариф, який API відхилить на кроці `/book`.

Фактична видача на 10–12 березня 2027 (2 дорослих) — 11 типів, усі доступні:

| id | apiID | вільно | Номер | Ціна за 2 ночі, UAH |
|---|---|---|---|---|
| 1373 | 2 | 9 | Стандарт Дабл, вид на ліс | 16 200 |
| 1374 | 3 | 9 | Стандарт Дабл, вид на озеро | 16 200 |
| 1375 | 4 | 7 | Стандарт Твін, вид на ліс | 16 200 |
| 1376 | 5 | 7 | Стандарт Твін, вид на озеро | 16 200 |
| 1377 | 6 | 2 | Сімейний | 20 200 |
| 1378 | 7 | 2 | Люкс | 19 700 |
| 1379 | 8 | 3 | Шале для 2 осіб, 1 спальня | 20 400 |
| 1380 | 9 | 2 | Шале на 3 спальні з кухнею | 47 200 |
| 1381 | 10 | 3 | Шале для 4 осіб, 2 спальні | 31 000 |
| 1382 | 11 | 7 | Шале для 4 осіб, 2 спальні + вітальня | 35 000 |
| 1383 | 12 | 5 | Шале для 2 осіб, 1 спальня + вітальня | 22 400 |

Усі тарифи — `contractConditionID: 107`, `apiPriceListID: 2`.

### POST `/book` — створення броні

✅ **Перевірено живим викликом** 2026-09-08 (бронь `0000049452`,
10–12 березня 2027). Payload, відновлений раніше зі статичного розбору
бандла, виявився правильним — Servio прийняв його без змін.

Одне уточнення, яке далося лише живою спробою: телефон валідується на
стороні Servio. Перша спроба впала з `isError: true`,
`message: "Невірний формат номера телефону"` — наша власна регулярка
пропускає більше форматів, ніж приймає готель.

Додатковий заголовок: `UTM-Marks: <JSON>` (utm-параметри; можна `{}`).

```json
{
  "companyKey": "6DFA7A01-…",
  "hotelID": 161,
  "promoCode": null,
  "paidType": 200,
  "currency": 980,

  "contactFullName": "Ім'я Прізвище",
  "contactEmail": "guest@example.com",
  "contactPhone": "+380…",
  "contactInfo": "",
  "clientInfo": "",
  "comment": "",

  "roomNightsToApply": 0,
  "loyaltyMagneticCardID": 0,
  "loyaltyMagneticCardNumber": "",
  "guestTravelsByCar": false,
  "carNumber": "",

  "rooms": [
    {
      "roomTypeID": 1373,
      "dateArrival": "2027-03-10",
      "dateDeparture": "2027-03-12",
      "timeArrival": "14:00",
      "timeDeparture": "12:00",
      "adults": 2,
      "children": 0,
      "childrenAges": [],
      "apiPriceListID": 2,
      "contractConditionID": 107,
      "guestFullName": "Ім'я Прізвище",
      "countryOfResidence": "ua"
    }
  ],

  "processingPersonalDataConsent": null,
  "hotelRulesConsent": null,
  "publicOfferConsent": null,
  "additionalServices": [],
  "requestConsultation": false
}
```

Поля згод — `null`, бо в налаштуваннях готелю всі `agreements.*` вимкнені.
`paidType: 200` — віджет надсилає саме це значення (є серед `paidTypes`
тарифу `[100, 300, 200]`).

**Відповідь** (`data`) — фактична, з живого виклику:

```
apiReservationID     "0000049452" — рядок із провідними нулями, не число!
totalAmount          16200.0
currency             980
hotelID
rooms[]              { apiReservationID, roomTypeID, roomTypeApiID,
                       contractConditionID, dateArrival, dateDeparture,
                       adults, children, guestFullName, roomNightsApplied }
paymentInfo          ← найважливіше, див. нижче
```

> **`/book` уже віддає `paymentInfo`.** Тобто окремий GET `/payment-info`
> між `/book` і `/make-payment` **не обов'язковий** — усе потрібне для
> платежу приходить одразу. Ми лишили GET як запасний шлях, якщо готель
> колись перестане вкладати `paymentInfo` у відповідь.

`paymentInfo` (15 полів, значуще):

```
services[]           рахунки до оплати; кожен несе customerAccount
                     (= apiReservationID), price, quantity, taxRate,
                     taxAmount, originalPrice, apiServiceID,
                     serviceProviderID, serviceSystemCode: "dwelling",
                     priceDates[] з розбивкою по днях
paymentServices[]    [{ "paymentService": 4 }] — саме цей сервіс у Riverwood
useIFrame            false
isPaymentEnabled     true
totalAmount          16200.0
accountName          імʼя гостя
hotelName            "Riverwood"
companyKey
paymentPartsQuantity 0
isReservationCancellationEnabled  false
```

**`paymentService: 4`** знімає невідомість із таблиці нижче: за гілками
бандла `4` — це звичайний редірект на `data.url`, без платіжного віджета і
без підпису мерчанта. `useIFrame: false` це підтверджує.

Практичний наслідок: `paymentService` треба **передавати явно** у
`/make-payment`. Ми спершу надсилали `null` — віджет так не робить.

### POST `/make-payment` — оплата

⚠️ **Відповідає HTTP 307.** Це поклало першу реальну бронь: `httpx` за
замовчуванням редіректи не слідує, і клієнт отримав тіло редіректу замість
JSON (`ServioProtocolError`). У браузері цього не видно — `fetch()` слідує
редіректам сам.

Виправлення — `follow_redirects=True` у клієнті. Для POST це безпечно саме
тому, що код 307: він зберігає метод і тіло, а оригінальний запит сервером
ще не обробляється. Був би 302 — httpx перетворив би POST на GET.

Заголовок `UTM-Marks` теж потрібен.

> **Порядок кроків.** Оплата йде не одразу після `/book`, а в три виклики:
> `/book` → `/payment-info` (звідси беруться `services` і перелік
> `paymentServices`) → `/make-payment`. Це видно з call-site у бандлі: payload
> платежу збирається з `_billInfoModel.paymentInfo`, а `services` фільтруються
> по `customerAccount == apiReservationID`.

**Запит** (форма з двох call-site у бандлі):

```json
{
  "companyKey": "6DFA7A01-…",
  "account": "<apiReservationID>",
  "hotelID": 161,
  "currency": 980,
  "accountName": "Ім'я Прізвище",
  "email": "guest@example.com",
  "phone": "+380…",
  "services": [ { "customerAccount": "…", "total": 16200, "priceDates": [] } ],
  "paymentPartsQuantity": null,
  "useIFrame": false,
  "paymentService": null,
  "promocode": null
}
```

`paymentService` — конкретний сервіс з `paymentInfo.paymentServices[]`;
`null` означає «за замовчуванням». `useIFrame` віджет ставить у `true` лише
для `paymentDisplayMode == "PaymentModalIframe"`.

> **Розбіжність з ТЗ.** У завданні крок описаний як «перехід на оплату —
> посилання віддає той самий API». Насправді єдиного «посилання на оплату» не
> існує: `/make-payment` віддає `paymentServiceID`, і далі поведінка залежить
> від нього.

Гілки з бандла (`Mr.makePayment`):

| `paymentServiceID` | Що робить віджет |
|---|---|
| `2`, `5` | **UPC**: `new UpcPayment({merchant:{id, terminalId, signature}}).pay({orderID, purchaseTime, totalAmount, checkoutURL, currency, locale})` — iframe |
| `3,4,6,9,10,12,15,17,18` | `document.location.href = data.url` — звичайний редірект |
| `11` | Redsys: авто-POST форма з `Ds_SignatureVersion`, `Ds_MerchantParameters`, `Ds_Signature` на `data.apiUrl` |
| `14` | Monobank: `data.url` в iframe або редірект |
| `16` | `data.checkout_url` |
| `13` | заявка створена, без редіректу |
| `1` | нічого |

Який `paymentServiceID` у Riverwood — **невідомо до першої реальної броні**.
Налаштування `paymentDisplayMode: "PaymentIframe"` натякає на UPC (2/5), але це
не доказ.

**Наслідок для реалізації:** у моделі зберігаємо не лише `payment_url`, а весь
`raw_response` від `/make-payment`. Крок 4 обробляє два випадки: є `url`/
`checkout_url` → редірект; інакше (UPC/Redsys) → показуємо сторінку з даними
платежу. Оскільки за ТЗ статус оплати не моніторимо, для здачі достатньо
довести користувача до платіжної форми і зберегти бронь.

### GET `/payment-info`

```
/payment-info?companyKey={key}&account={account}&currency=980[&promocode=…]
```

`account` — ідентифікатор рахунку з броні (`apiReservationID`). З чужим
`account` віддає `isError: true`. **Обов'язковий крок перед
`/make-payment`:** звідси беруться `services` і доступні `paymentServices`.

## Що це означає для реалізації

1. **Проксі через Django.** Ключів немає, CORS відкритий, `Referer` не
   перевіряється — серверні виклики працюють як є.
2. **Ніколи не орієнтуватися на HTTP-код.** Тільки `isError`. Для клієнта —
   власний тип помилки з `message` як текстом для користувача.
3. **`data: []` — не помилка**, а «немає доступності».
4. **Оффер = `(roomTypeID, contractConditionID, apiPriceListID)`.** Ці три
   значення треба протягнути з кроку 2 у крок 4 незмінними.
5. **Перевіряти `saleRestrictions`** перед показом оффера.
6. **Зберігати сирі payload'и** — контракт недокументований, і `/make-payment`
   поки не спостережений живим.
7. **Ідемпотентності немає.** Повторний `/book` створить другу бронь → жодних
   retry на `/book` і `/make-payment`.
8. **Ціни у форматі `float`, сума за весь період** — `totalPrice`; розбивка по
   днях у `services[].priceDates[]`.

## Не перевірене

| Питання | Коли з'ясується |
|---|---|
| Реальний контракт `/book` | Фаза 7, одна бронь на березень 2027 |
| `paymentServiceID` готелю і форма `/make-payment` | тоді ж |
| Точний формат `paymentInfo` у відповіді `/book` | тоді ж |
| Рейт-лімітування | не зустрічали; таймаути все одно обов'язкові |

## Як відтворити розвідку

Скрипти лежать у scratchpad сесії (не в репо, бо це разова робота):
`recon1.py` — пошук точки входу, `recon2.py` — завантаження `/booking/`,
`recon3.py` — прокрутка календаря на березень 2027 + `POST /rooms`.
Стек: Playwright з `channel="chrome"` (локального Docker/Node немає, системний
Chrome використовується напряму).
