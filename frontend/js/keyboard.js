/* Phone keyboard: keep the shell inside the *visible* box so a text field is
   never covered. iOS reports the keyboard in three different ways, depending
   on Safari vs. a home-screen app and the iOS version: visualViewport shrinks
   and fires events; the whole window shrinks (innerHeight) without any
   visualViewport event; or nothing is reported at all. So: measure on every
   signal, poll for a second after a field gets focus, and when nothing moved
   on iOS, assume the usual keyboard height (learned from earlier
   measurements, else 46 % of the screen). Everything is exposed as
   --vvh / --vvtop on <html> and body.kb; frames tell the shell when their
   fields get focus and get the visible box back. */
(function () {
  "use strict";
  const NH = (window.NH = window.NH || {});
  const K = (NH.keyboard = NH.keyboard || {});
  const isIOS = /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const standalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || navigator.standalone === true;
  const FIELD = /^(INPUT|TEXTAREA|SELECT)$/;
  const isField = (el) => !!el && FIELD.test(el.tagName) && !/^(checkbox|radio|button|submit|file|range|color)$/.test(el.type || "") && !el.readOnly;
  K.isIOS = isIOS; K.standalone = standalone;
  K.state = { h: window.innerHeight, top: 0, kb: false, guessed: false, learned: 0, full: window.innerHeight };

  K.watch = function (opts) {
    opts = opts || {};
    if (window.innerWidth > 760 || K._on) return K.state;
    K._on = true;
    const vv = window.visualViewport;
    const root = document.documentElement.style;
    const st = K.state;
    try { st.learned = +localStorage.getItem("nh_kbh") || 0; } catch (e) { /* ignore */ }
    let frameTyping = false, timers = [];
    const typing = () => frameTyping || isField(document.activeElement);
    let probe = null;
    const reflow = () => {  // iOS home-screen apps sometimes keep the shrunken window after the keyboard: force a re-measure
      if (!probe) { probe = document.createElement("div"); probe.style.cssText = "position:fixed;inset:0;visibility:hidden;pointer-events:none"; document.body.appendChild(probe); }
      probe.style.display = "none"; void probe.offsetHeight; probe.style.display = "";
    };
    function fit(guess) {
      const vh = vv ? Math.round(vv.height) : window.innerHeight;
      const ih = window.innerHeight;
      const t = typing();
      let h = Math.min(vh, ih), top = vv ? Math.max(0, Math.round(vv.offsetTop)) : 0, kb = false, guessed = false;
      if (!t) {
        st.full = Math.max(st.full, ih);
        top = 0;
        if (window.scrollY) window.scrollTo(0, 0);
      } else {
        kb = h < st.full - 80;
        if (kb) { const k = st.full - h; if (k > 120 && k !== st.learned) { st.learned = k; try { localStorage.setItem("nh_kbh", String(k)); } catch (e) { /* ignore */ } } }
        else if (isIOS && (guess || st.guessed)) { h = Math.min(h, ih - (st.learned || Math.round(ih * 0.46))); kb = true; guessed = true; }
        if (window.scrollY && !top) window.scrollTo(0, 0);  // the page itself never scrolls; fixed boxes follow --vvtop instead
      }
      Object.assign(st, { h, top, kb, guessed, typing: t, vh, ih });
      root.setProperty("--vvh", h + "px");
      root.setProperty("--vvtop", top + "px");
      document.body.classList.toggle("kb", kb);
      if (opts.frames) opts.frames().forEach((f) => { try { f.contentWindow.postMessage({ nh: "vv", kb, h, top }, location.origin); } catch (e) { /* ignore */ } });
      if (opts.onFit) opts.onFit(st);
      if (K._dbg) K.debug(true);
    }
    const burst = (guessAtEnd) => {  // iOS reports the new box a beat (or three) after focus
      timers.forEach(clearTimeout);
      timers = [60, 180, 320, 500, 760].map((ms, i, arr) => setTimeout(() => {
        if (!guessAtEnd && isIOS && standalone && i === 2 && !typing() && window.innerHeight < st.full - 80) reflow();
        fit(guessAtEnd && i === arr.length - 1);
      }, ms));
    };
    if (vv) { vv.addEventListener("resize", () => fit(false)); vv.addEventListener("scroll", () => fit(false)); }
    window.addEventListener("resize", () => fit(false));
    window.addEventListener("orientationchange", () => { st.full = 0; burst(false); });
    document.addEventListener("focusin", (e) => { if (isField(e.target)) burst(true); }, true);
    document.addEventListener("focusout", (e) => { if (isField(e.target)) burst(false); }, true);
    window.addEventListener("message", (e) => {
      if (e.origin !== location.origin || !e.data || e.data.nh !== "focus") return;
      frameTyping = !!e.data.on;
      burst(frameTyping);
    });
    K.fit = fit;
    fit(false);
    return st;
  };

  /* A page inside the shell's iframe: tell the shell about its fields, never let its own document scroll,
     and call back when the shell has re-measured the visible box. */
  K.embed = function (onFit) {
    if (window.parent === window) return;
    document.body.classList.add("embed");
    const tell = (on) => { try { window.parent.postMessage({ nh: "focus", on }, location.origin); } catch (e) { /* ignore */ } };
    const unscroll = () => { if (window.scrollY || (document.scrollingElement && document.scrollingElement.scrollTop)) window.scrollTo(0, 0); };
    document.addEventListener("focusin", (e) => { if (isField(e.target)) { tell(true); setTimeout(unscroll, 350); } }, true);
    document.addEventListener("focusout", (e) => { if (isField(e.target)) { tell(false); setTimeout(unscroll, 350); } }, true);
    window.addEventListener("message", (e) => { if (e.origin === location.origin && e.data && e.data.nh === "vv") { unscroll(); if (onFit) onFit(e.data); } });
    window.addEventListener("resize", () => { unscroll(); if (onFit) onFit(null); });
  };

  /* Five taps on the page title show what the phone reports (to diagnose a device I cannot see). */
  K.debug = function (on) {
    let el = document.getElementById("nh-kbdbg");
    if (!on) { if (el) el.remove(); K._dbg = false; return; }
    K._dbg = true;
    if (!el) { el = document.createElement("div"); el.id = "nh-kbdbg"; el.style.cssText = "position:fixed;left:8px;top:60px;z-index:9999;background:rgba(17,24,39,.85);color:#fff;font:11px/1.4 monospace;padding:6px 8px;border-radius:8px;pointer-events:none;white-space:pre"; document.body.appendChild(el); }
    const s = K.state, vv = window.visualViewport;
    el.textContent = `ios ${isIOS ? 1 : 0} standalone ${standalone ? 1 : 0} ua ${(navigator.userAgent.match(/OS (\d+_\d+)/) || [])[1] || "?"}\ninner ${window.innerHeight} vv ${vv ? Math.round(vv.height) : "-"} top ${vv ? Math.round(vv.offsetTop) : "-"} screen ${window.screen.height}\nfull ${s.full} h ${s.h} kb ${s.kb ? 1 : 0}${s.guessed ? " (guess)" : ""} learned ${s.learned} typing ${s.typing ? 1 : 0} scrollY ${window.scrollY}`;
  };
  K.tapToDebug = function (el) {
    let taps = 0, last = 0;
    el.addEventListener("click", () => { const now = Date.now(); taps = now - last < 600 ? taps + 1 : 1; last = now; if (taps >= 5) { taps = 0; K.debug(!K._dbg); } });
  };
})();
