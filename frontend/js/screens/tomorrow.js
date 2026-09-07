/* "Tomorrow" screen — same layout as Today, without the debtors block. */
(function () {
  const NH = (window.NH = window.NH || {});
  NH.screens = NH.screens || {};
  NH.screens.tomorrow = {
    skeleton: () => NH.screens.today.skeleton(),
    render: (data) => NH.screens.today.render(data, { hideDebtors: true, dayWord: "завтра" }),
  };
})();
