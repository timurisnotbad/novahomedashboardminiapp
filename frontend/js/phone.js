/* Phone mask for every <input type="tel">: "+" is added by itself, the country
   code is recognised and the number is split into groups — +998 90 123-45-67.
   The field keeps the formatted text; the server strips everything but digits. */
(() => {
  const CC = {"1":[3,3,4],"7":[3,3,2,2],"20":[2,4,4],"27":[2,3,4],"30":[3,3,4],"31":[1,4,4],"32":[3,2,2,2],"33":[1,2,2,2,2],"34":[3,3,3],"36":[2,3,4],"39":[3,3,4],"40":[3,3,3],"41":[2,3,2,2],"43":[3,3,4],"44":[4,6],"45":[2,2,2,2],"46":[2,3,2,2],"47":[3,2,3],"48":[3,3,3],"49":[3,3,4],"51":[3,3,3],"52":[3,3,4],"54":[3,3,4],"55":[2,5,4],"56":[1,4,4],"57":[3,3,4],"58":[3,3,4],"60":[2,3,4],"61":[1,4,4],"62":[3,3,4],"63":[3,3,4],"64":[2,3,4],"65":[4,4],"66":[2,3,4],"81":[2,4,4],"82":[2,4,4],"84":[2,3,4],"86":[3,4,4],"90":[3,3,2,2],"91":[5,5],"92":[3,3,4],"93":[2,3,4],"94":[2,3,4],"95":[1,3,4],"98":[3,3,4],"212":[3,3,3],"213":[3,2,2,2],"216":[2,3,3],"218":[2,3,4],"234":[3,3,4],"254":[3,3,3],"255":[3,3,3],"256":[3,3,3],"351":[3,3,3],"352":[3,3,3],"353":[2,3,4],"354":[3,4],"355":[2,3,4],"356":[4,4],"357":[2,6],"358":[2,3,4],"359":[2,3,4],"370":[3,5],"371":[2,3,3],"372":[3,4],"373":[2,3,3],"374":[2,3,3],"375":[2,3,2,2],"376":[3,3],"377":[2,2,2,2],"380":[2,3,2,2],"381":[2,3,4],"385":[2,3,4],"386":[2,3,3],"387":[2,3,3],"389":[2,3,3],"420":[3,3,3],"421":[3,3,3],"852":[4,4],"853":[4,4],"855":[2,3,3],"856":[2,3,3],"880":[4,6],"886":[3,3,3],"960":[3,4],"961":[2,3,3],"962":[1,4,4],"963":[3,3,3],"964":[3,3,4],"965":[4,4],"966":[2,3,4],"967":[3,3,3],"968":[4,4],"970":[3,3,3],"971":[2,3,4],"972":[2,3,4],"973":[4,4],"974":[4,4],"975":[2,3,3],"976":[2,3,3],"977":[3,3,4],"992":[2,3,2,2],"993":[2,3,3],"994":[2,3,2,2],"995":[3,2,2,2],"996":[3,3,3],"998":[2,3,2,2]};
  function split(d) { for (const n of [3, 2, 1]) { const c = d.slice(0, n); if (CC[c]) return [c, d.slice(n), CC[c]]; } return ["", d, [3, 3, 4]]; }
  function format(raw, final) {
    let d = String(raw || "").replace(/\D/g, "");
    if (!d) return "";
    if (d.length === 11 && d[0] === "8") d = "7" + d.slice(1);           // Russian 8-xxx
    if (final && d.length === 9) d = "998" + d;                          // local Uzbek number (90 123 45 67), when typing is finished
    const [cc, rest, pat] = split(d);
    if (!cc) return "+" + d;
    const parts = []; let i = 0;
    for (let k = 0; k < pat.length && i < rest.length; k++) { parts.push(rest.slice(i, i + pat[k])); i += pat[k]; }
    if (i < rest.length) parts[parts.length - 1] = (parts[parts.length - 1] || "") + rest.slice(i);
    const g = parts.filter(Boolean);
    return "+" + cc + (g.length ? " " + g[0] + (g.length > 1 ? " " + g.slice(1).join("-") : "") : "");
  }
  function attach(inp) {
    if (inp.dataset.phoneMask) return;
    inp.dataset.phoneMask = "1";
    inp.setAttribute("inputmode", "tel"); inp.setAttribute("autocomplete", "tel");
    const apply = (final) => { const v = format(inp.value, final); if (v !== inp.value) inp.value = v; };
    inp.addEventListener("input", (e) => { if (e.inputType && e.inputType.startsWith("delete")) return; apply(false); });
    inp.addEventListener("blur", () => apply(true));
    inp.addEventListener("paste", () => setTimeout(() => apply(true), 0));
    if (inp.value) apply(true);
  }
  function scan(root) { (root.querySelectorAll ? root.querySelectorAll("input[type=tel]") : []).forEach(attach); if (root.matches && root.matches("input[type=tel]")) attach(root); }
  const mo = new MutationObserver((muts) => muts.forEach((m) => m.addedNodes.forEach((n) => { if (n.nodeType === 1) scan(n); })));
  const start = () => { scan(document); mo.observe(document.body, { childList: true, subtree: true }); };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
  window.NHPhone = { format: (v) => format(v, true), digits: (v) => String(v || "").replace(/\D/g, "") };
})();
