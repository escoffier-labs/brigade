(() => {
  const failures = [];
  const check = (condition, message) => {
    if (!condition) failures.push(message);
  };
  const heading = document.querySelector("h1")?.textContent.trim() || "";
  const view = heading.endsWith("Machines") ? "machines" : heading.endsWith("Repos") ? "repos" : null;
  const viewport = window.innerWidth;
  const documentWidth = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
  check(view !== null, "expected an actual Machines or Repos board fixture");
  check(viewport === 320, `expected viewport 320 CSS pixels, got ${viewport}`);
  check(documentWidth <= viewport, `document overflows: ${documentWidth} > ${viewport}`);

  const tables = [...document.querySelectorAll("table.data-table")];
  check(tables.length === (view === "machines" ? 2 : 1), `unexpected populated table count: ${tables.length}`);
  const wrappers = new Set();
  const measurements = tables.map((table) => {
    const rows = [...table.querySelectorAll("tbody > tr")];
    check(rows.length === 2 && rows.every((row) => row.getClientRects().length > 0),
      `${table.id}: expected two visible populated rows, got ${rows.length}`);
    check(rows.every((row) => row.cells.length === (view === "machines" ? 6 : 5)),
      `${table.id}: unexpected table cell semantics`);
    const wrapper = table.closest(".table-wrap");
    check(wrapper !== null && table.parentElement === wrapper, `${table.id}: missing own scroll wrapper`);
    if (!wrapper) return { id: table.id, rows: rows.length, wrapper: null };
    check(wrapper.querySelectorAll("table").length === 1 && !wrappers.has(wrapper),
      `${table.id}: wrapper must belong to this table alone`);
    wrappers.add(wrapper);
    const clientWidth = wrapper.clientWidth;
    const scrollWidth = wrapper.scrollWidth;
    const tableWidth = table.getBoundingClientRect().width;
    check(["auto", "scroll"].includes(getComputedStyle(wrapper).overflowX),
      `${table.id}: wrapper does not allow horizontal scrolling`);
    check(clientWidth > 0 && tableWidth > clientWidth && scrollWidth > clientWidth,
      `${table.id}: long populated table is not wider than its wrapper`);
    const originalScrollLeft = wrapper.scrollLeft;
    let scrolled = 0;
    try {
      wrapper.scrollLeft = 0;
      wrapper.scrollLeft = Math.min(64, scrollWidth - clientWidth);
      scrolled = wrapper.scrollLeft;
      check(scrolled > 0, `${table.id}: scrollLeft did not advance (${scrolled})`);
    } finally {
      wrapper.scrollLeft = originalScrollLeft;
    }
    return { id: table.id, rows: rows.length, clientWidth, scrollWidth, tableWidth, scrolled };
  });
  const text = tables.map((table) => table.textContent).join(" ");
  for (const label of ["alpha", "bravo"]) {
    check(text.includes(`synthetic-repo-${label}-`) && text.includes(`synthetic-seat-${label}-`) &&
      text.includes(`synthetic-active-${label}`), `missing populated content for ${label}`);
    if (view === "machines") {
      check(text.includes(`synthetic-completed-${label}`), `missing completed run for ${label}`);
    }
  }
  check(document.querySelectorAll("table.data-table .fleet-state-succeeded").length === 2,
    "expected two completed outcomes in populated tables");
  const result = { status: failures.length ? "FAIL" : "PASS", view, viewport, documentWidth, tables: measurements, failures };
  if (failures.length) throw new Error(`fleet board layout FAIL: ${JSON.stringify(result)}`);
  return result;
})()
