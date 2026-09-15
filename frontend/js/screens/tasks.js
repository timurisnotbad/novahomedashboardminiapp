/* "Задачи" screen — per-apartment + general to-do, Reminders-style. */
(function () {
  const NH = (window.NH = window.NH || {});
  const { esc, icon } = NH.ui;

  function skeleton() {
    return `<div class="addfield"><div class="addfield__top"><span class="plus">${icon("plus")}</span></div></div>
      ${NH.ui.skeletonList(4)}`;
  }

  function taskRow(t, showApt) {
    const done = t.status === "done";
    const next = done ? "open" : "done";
    const cls = done ? "c-done" : t.overdue ? "c-red" : t.due_today ? "c-orange" : "";

    let apt = "";
    if (showApt) {
      apt = t.apartment
        ? `<span class="tag-apt">${esc(t.apartment)}</span>`
        : `<span class="tag-gen">Общая</span>`;
    }
    let due = "";
    if (t.deadline) {
      if (t.overdue) due = `<span class="due red">${esc(t.deadline_human)} · просрочено</span>`;
      else if (t.due_today) due = `<span class="due orange">сегодня</span>`;
      else due = `<span class="due">${esc(t.deadline_human)}</span>`;
    }
    const by = t.created_by ? `<span class="due">${esc(t.created_by)}</span>` : "";
    const meta = apt || due || by ? `<div class="task__meta">${apt}${due}${by}</div>` : "";
    return `
      <div class="li">
        <button class="circle ${cls}" data-task-toggle data-id="${t.id}" data-status="${next}" aria-label="Готово">${done ? icon("check") : ""}</button>
        <div class="li__main"><div class="task__t ${done ? "done" : ""}">${esc(t.title)}</div>${meta}</div>
        <button class="task-del" data-task-del data-id="${t.id}" aria-label="Удалить">${icon("x")}</button>
      </div>`;
  }

  function addForm(apartments) {
    const opts =
      `<option value="">Общая (без квартиры)</option>` +
      apartments.map((a) => `<option value="${esc(a)}">${esc(a)}</option>`).join("");
    return `
      <div class="addfield">
        <div class="addfield__top">
          <button class="plus" data-task-add aria-label="Добавить">${icon("plus")}</button>
          <input id="task-title" class="title" type="text" maxlength="200" placeholder="Новая задача…" />
        </div>
        <div class="addfield__row">
          <select id="task-apt">${opts}</select>
        </div>
        <div class="addfield__row2">
          <button type="button" class="pk-field" id="task-deadline" data-pick="date" data-value="">
            ${icon("cal")}<span class="pk-field__v">Дата</span>
          </button>
          <button type="button" class="pk-field" id="task-deadline-time" data-pick="time" data-value="">
            ${icon("clock")}<span class="pk-field__v">Время</span>
          </button>
        </div>
      </div>`;
  }

  function render(data) {
    data = data || {};
    const apartments = data.apartments || [];
    const tasks = data.tasks || [];
    const open = tasks.filter((t) => t.status === "open");
    const done = tasks.filter((t) => t.status === "done");

    const todayList = open.filter((t) => t.overdue || t.due_today);
    const rest = open.filter((t) => !t.overdue && !t.due_today);

    const byApt = {};
    const order = [];
    rest.filter((t) => t.apartment).forEach((t) => {
      if (!byApt[t.apartment]) { byApt[t.apartment] = []; order.push(t.apartment); }
      byApt[t.apartment].push(t);
    });
    const general = rest.filter((t) => !t.apartment);

    let html = addForm(apartments);

    if (!open.length) html += NH.ui.empty("Задач пока нет — добавьте первую сверху");

    if (todayList.length) {
      html += `<div class="group"><div class="group__h">${icon("tasks")} На сегодня и просрочено <span class="count">${todayList.length}</span></div>`;
      html += `<div class="list">${todayList.map((t) => taskRow(t, true)).join("")}</div></div>`;
    }

    order.forEach((apt) => {
      html += `<div class="group"><div class="group__h">${icon("home")} ${esc(apt)}</div>`;
      html += `<div class="list">${byApt[apt].map((t) => taskRow(t, false)).join("")}</div></div>`;
    });

    if (general.length) {
      html += `<div class="group"><div class="group__h">${icon("tasks")} Общие задачи</div>`;
      html += `<div class="list">${general.map((t) => taskRow(t, false)).join("")}</div></div>`;
    }

    if (done.length) {
      html += `<details class="task-done-wrap"><summary class="group__h" style="padding-left:22px">${icon("check")} Выполненные · ${done.length}</summary>`;
      html += `<div class="group"><div class="list">${done.map((t) => taskRow(t, true)).join("")}</div></div></details>`;
    }

    return html;
  }

  NH.screens = NH.screens || {};
  NH.screens.tasks = { render, skeleton };
})();
