/* Keep the chat pinned to the bottom while messages arrive.
 *
 * Why this exists: our replies are sent as ONE long Markdown block (tables of 15+ fee rows), and
 * Chainlit's built-in assistant_message_autoscroll does not reliably follow that, so the user had to
 * scroll down by hand after every answer.
 *
 * Rule: follow ONLY while the reader is already near the bottom. If they scrolled up to read
 * something, never yank them back (that is the behaviour people hate).
 */
(function () {
  "use strict";

  var NEAR_BOTTOM_PX = 160;

  function scrollable(el) {
    var style = window.getComputedStyle(el);
    return /(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 20;
  }

  function findScroller() {
    var main = document.querySelector("main");
    if (!main) return null;
    if (scrollable(main)) return main;
    var found = null;
    var nodes = main.querySelectorAll("*");
    for (var i = 0; i < nodes.length; i++) {
      if (scrollable(nodes[i])) found = nodes[i];   // deepest scrollable wins
    }
    return found;
  }

  var stick = true;        // follow the stream while the user stays at the bottom
  var bound = null;        // element currently carrying the scroll listener
  var scheduled = false;

  function bind(el) {
    if (!el || el === bound) return;
    bound = el;
    el.addEventListener("scroll", function () {
      stick = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;
    }, { passive: true });
  }

  function tick() {
    scheduled = false;
    var el = findScroller();
    if (!el) return;
    bind(el);
    if (stick) el.scrollTop = el.scrollHeight;
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(tick);
  }

  function start() {
    schedule();
    new MutationObserver(schedule).observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true
    });
    window.addEventListener("popstate", schedule);   // switching chats swaps the scroller
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
