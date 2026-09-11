# Использование кэшированных данных

Если в `visualize` передан `cache_id`, соответствующие данные автоматически доступны внутри iframe через:

```javascript
const data = window.getCachedData();
```

Правила:

- Всегда получай dataset через `getCachedData()`.
- Не вставляй исходные данные повторно в HTML или JavaScript.
- Не передавай данные аргументом в `visualize`.
- Вызывай `getCachedData()` только внутри исполняемого `<script>`.
- Функция возвращает исходное значение: объект, массив, строку или `null`.
- Перед построением графика проверь структуру полученных данных.
- Если данные отсутствуют или имеют неожиданную структуру, покажи в визуализации понятное сообщение об ошибке.
- При использовании внешней библиотеки сначала подключи её отдельным `<script src="...">`, а затем добавь `<script>` с вызовом `getCachedData()` и построением графика.

Пример:

```html
<div id="chart"></div>

<script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.3/plotly.min.js"></script>
<script>
  const data = window.getCachedData();

  if (!Array.isArray(data) || data.length === 0) {
    document.getElementById("chart").textContent =
      "Нет данных для визуализации";
  } else {
    Plotly.newPlot("chart", [{
      x: data.map(row => row.category),
      y: data.map(row => row.value),
      type: "bar"
    }]);
  }
</script>
```

Если `cache_id` не передан, `getCachedData()` не используется: необходимые небольшие данные можно сформировать непосредственно в HTML/JavaScript.
