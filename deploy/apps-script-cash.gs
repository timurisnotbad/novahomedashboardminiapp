/**
 * Nova Home — «Оплаты»: запись поступлений из CRM/бота в таблицу.
 *
 * Установка (один раз):
 *   1. Таблица → Расширения → Apps Script → добавить файл «cash.gs», вставить этот код.
 *   2. Вверху вписать тот же TOKEN, что в .env (SHEET_API_TOKEN).
 *      Если в проекте уже есть функция doPost — не дублируйте её: вызовите из своей
 *      `return cashHandle(JSON.parse(e.postData.contents));` когда action начинается с «cash_».
 *   3. Развернуть → Управление развёртываниями → новая версия (доступ: «Все»).
 *      URL веб-приложения /exec = SHEET_API_URL в .env.
 *
 * Листы: «Операции» (10 колонок: Год | мес | Дата | Апартамент | Статья | Сумма | Сумма $ |
 * Метод оплаты / счет | Валюта | Комментарии), «Валюты» (таблица «Методы оплат» и ячейки
 * «Текущий курс UZS/$» и т. п.), «Долг между Аппартами».
 */
var TOKEN = "ВПИШИТЕ_SHEET_API_TOKEN";
var OPS_SHEET = "Операции";
var FX_SHEET = "Валюты";
var DEBT_SHEET = "Долг между Аппартами";
// «Сумма $» (колонка G): пусто — записывать число из CRM; либо формула с {row}, например
// "=F{row}/VLOOKUP(I{row};Валюты!$A:$B;2;0)" — тогда CRM число не пишет, считает таблица.
var USD_FORMULA = "";

function doPost(e) {
  var body = {};
  try { body = JSON.parse(e.postData.contents || "{}"); } catch (err) { return _json({ error: "bad json" }); }
  return cashHandle(body);
}

function cashHandle(body) {
  if (!body || body.token !== TOKEN) return _json({ error: "forbidden" });
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    switch (body.action) {
      case "cash_meta": return _json(cashMeta());
      case "cash_add": return _json(cashAdd(body.sheet || OPS_SHEET, body.rows || []));
      case "cash_update": return _json(cashUpdate(body.sheet || OPS_SHEET, body.row, body.values || []));
      case "cash_debt": return _json(cashDebt(body.sheet || DEBT_SHEET, body.row || []));
      default: return _json({ error: "unknown action " + body.action });
    }
  } catch (err) {
    return _json({ error: String(err && err.message || err) });
  } finally {
    lock.releaseLock();
  }
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function _sheet(name) {
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(name);
  if (!sh) throw new Error("нет листа «" + name + "»");
  return sh;
}

/** Last row that has data in columns A–F (formulas further down the sheet are not «data»). */
function _lastDataRow(sh) {
  var max = sh.getLastRow();
  if (max < 1) return 0;
  var vals = sh.getRange(1, 1, max, 6).getValues();
  for (var r = vals.length - 1; r >= 0; r--) {
    var row = vals[r];
    if (String(row[0]).trim() !== "" || String(row[2]).trim() !== "" || String(row[5]).trim() !== "") return r + 1;
  }
  return 0;
}

function _toDate(v) {
  var m = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(String(v || "").trim());
  return m ? new Date(Number(m[3]), Number(m[2]) - 1, Number(m[1])) : v;
}

function _prepare(values, rowNum) {
  var v = values.slice(0, 10);
  while (v.length < 10) v.push("");
  v[2] = _toDate(v[2]);
  if (USD_FORMULA) v[6] = USD_FORMULA.replace(/\{row\}/g, rowNum);
  return v;
}

/** Append rows after the last filled one; returns their row numbers. */
function cashAdd(sheetName, rows) {
  var sh = _sheet(sheetName);
  var start = _lastDataRow(sh) + 1;
  var out = [];
  var data = rows.map(function (r, i) { out.push(start + i); return _prepare(r, start + i); });
  if (data.length) sh.getRange(start, 1, data.length, 10).setValues(data);
  SpreadsheetApp.flush();
  return { rows: out };
}

function cashUpdate(sheetName, row, values) {
  if (!row) throw new Error("no row");
  var sh = _sheet(sheetName);
  sh.getRange(Number(row), 1, 1, 10).setValues([_prepare(values, Number(row))]);
  SpreadsheetApp.flush();
  return { ok: true, row: Number(row) };
}

/** «Долг между Аппартами»: дата | кв. с деньгами | кв., которой должны | $ | комментарий. */
function cashDebt(sheetName, row) {
  var sh = _sheet(sheetName);
  var start = _lastDataRow(sh) + 1;
  var v = row.slice();
  v[0] = _toDate(v[0]);
  sh.getRange(start, 1, 1, v.length).setValues([v]);
  SpreadsheetApp.flush();
  return { ok: true, row: start };
}

/** Methods (name → currency) and rates from «Валюты». */
function cashMeta() {
  var sh = _sheet(FX_SHEET);
  var vals = sh.getDataRange().getValues();
  var methods = [], rates = {};
  for (var r = 0; r < vals.length; r++) {
    for (var c = 0; c < vals[r].length; c++) {
      var cell = String(vals[r][c] || "").trim();
      if (!cell) continue;
      // «Методы оплат»: the table below — name | currency, until the first empty name
      if (/^метод[ыа]? оплат/i.test(cell) && !methods.length) {
        for (var rr = r + 1; rr < vals.length; rr++) {
          var name = String(vals[rr][c] || "").trim();
          if (!name) break;
          var cur = "";
          for (var cc = c + 1; cc <= c + 3 && cc < vals[rr].length; cc++) {
            var x = String(vals[rr][cc] || "").trim().toUpperCase();
            if (/^[A-Z]{3}$/.test(x)) { cur = x; break; }
          }
          if (/^(метод|название|счет|счёт)/i.test(name) && !cur) continue; // a header row
          if (cur) methods.push({ name: name, currency: cur });
        }
      }
      // «Текущий курс UZS/$»: the number to the right (or below)
      var km = /курс\s*([A-Za-z]{3})\s*\/?\s*\$?/i.exec(cell);
      if (km) {
        var code = km[1].toUpperCase(), num = null;
        for (var c2 = c + 1; c2 <= c + 3 && c2 < vals[r].length; c2++) { if (typeof vals[r][c2] === "number") { num = vals[r][c2]; break; } }
        if (num === null && r + 1 < vals.length && typeof vals[r + 1][c] === "number") num = vals[r + 1][c];
        if (num) rates[code] = num;
      }
    }
  }
  return { methods: methods, rates: rates };
}
