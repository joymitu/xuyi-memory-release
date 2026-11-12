/**
 * 续忆 · 浏览器半侧：左侧栏加一个「续忆」入口，点开在主区域整页显示续忆（日记本 / TA的记忆 / 设置）。
 * 引导从装好那一刻开始：续忆自己的插件详情页上有「打开续忆」按钮；DSH 加载时如果还没设置完，
 * 自动切到续忆页面（每次加载至多一次，用户离开后不再打扰），引导就在那里一步步走。
 * 做法与腾讯 QQ 插件的「QQ 机器人」入口相同：ctx.inject(['layout']) 后，往 sidebar.panellist
 * 注册图标行，往 main 注册同一个 id 的整页面板。
 * 日记本本身是宿主侧 /xuyi/ 路由提供的页面（转发给续忆的本地看板），这里只负责入口和外框。
 * 老宿主没有 layout 服务时，退回会话页头的按钮。
 */
window.__ModuleLoader__.load({
  id: "dsh-xuyi-memory",
  factory: function (require) {
    var module = { exports: {} };
    var exports = module.exports;
    var React = require("react");
    var ReactDOM = require("react-dom");
    var h = React.createElement;
    var PANEL_ID = "xuyi-diary";
    var QQ_PANEL_ID = "im-qqbot";   // 腾讯 QQ 插件左侧栏页面的 id（扫码在那里）
    var HEADER_SLOTS = ["conversation.session.header.actions", "conversation.session.header.views.actions"];
    var PACKAGE_NAME = "dsh-xuyi-memory";   // 插件详情页按包名匹配，只在续忆自己的详情页出现按钮
    var LABEL = "续忆";

    var hostCtx = null;
    var hostLayout = null;

    // 引导里点「去扫码」：切到 QQ 机器人页；没有 layout 服务时打开 QQ 插件的详情页。
    function openQqPlugin() {
      try {
        if (hostLayout && typeof hostLayout.selectPanel === "function") { hostLayout.selectPanel(QQ_PANEL_ID); return true; }
      } catch (ignored) {}
      try {
        var nav = hostCtx && typeof hostCtx.get === "function" ? hostCtx.get("pluginNavigation") : null;
        if (nav && typeof nav.openBundle === "function") { nav.openBundle("@tencent-connect/dsh-qqbot"); return true; }
      } catch (ignored) {}
      return false;
    }

    function onMessage(event) {
      if (!event || !event.data || event.data.type !== "xuyi-open-qq") return;
      var opened = openQqPlugin();
      if (event.source && typeof event.source.postMessage === "function") {
        event.source.postMessage({ type: "xuyi-open-qq-result", opened: opened }, "*");
      }
    }

    function diaryFrame(style) {
      return h("iframe", { src: "/xuyi/", title: LABEL,
        style: Object.assign({ border: 0, width: "100%", height: "100%", display: "block", background: "#f4f1ea" }, style) });
    }

    // 主区域整页：日记本。
    function DiaryPage() {
      return h("div", { "data-xuyi-diary-page": "true", style: { width: "100%", height: "100%", minHeight: 0, overflow: "hidden" } },
        diaryFrame());
    }

    // 左侧栏图标：一本带书签的本子；currentColor 跟随侧栏配色与选中态，宿主传 size（展开 18 / 收起 16）。
    function DiarySidebarIcon(props) {
      var size = (props && props.size) || 16;
      return h("svg", { width: size, height: size, viewBox: "0 0 24 24", fill: "none", "aria-hidden": true },
        h("path", { d: "M6 3.5h10.5A1.5 1.5 0 0 1 18 5v14.5a1 1 0 0 1-1 1H6.5A2.5 2.5 0 0 1 4 18V5.5a2 2 0 0 1 2-2Z",
          stroke: "currentColor", strokeWidth: 1.6, strokeLinejoin: "round" }),
        h("path", { d: "M4 18a2.5 2.5 0 0 1 2.5-2.5H18", stroke: "currentColor", strokeWidth: 1.6 }),
        h("path", { d: "M8.5 7.5h6M8.5 10.5h4", stroke: "currentColor", strokeWidth: 1.6, strokeLinecap: "round" }));
    }

    // ---- 兜底：老宿主没有 layout 服务时，会话页头的按钮 + 整屏浮层 ----
    var buttonStyle = { display: "inline-flex", alignItems: "center", gap: "5px", padding: "5px 9px", borderRadius: "8px",
      border: "1px solid rgba(160,150,130,.45)", background: "transparent", color: "inherit", font: "inherit",
      cursor: "pointer", whiteSpace: "nowrap", flexShrink: 0 };
    var closeStyle = { position: "absolute", top: "14px", right: "18px", zIndex: 2, padding: "9px 18px", borderRadius: "999px",
      border: "1px solid rgba(35,38,45,.18)", background: "#fffdf9", color: "#23262d", font: "14px system-ui,sans-serif",
      cursor: "pointer", boxShadow: "0 2px 10px rgba(35,38,45,.12)" };

    function DiaryButton() {
      var state = React.useState(false);
      var open = state[0], setOpen = state[1];
      React.useEffect(function () {
        if (!open) return undefined;
        function onClose(event) {
          if (event && event.data && event.data.type === "xuyi-diary-close") setOpen(false);
        }
        function onKey(event) {
          if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); }
        }
        window.addEventListener("message", onClose);
        window.addEventListener("keydown", onKey, true);
        return function () {
          window.removeEventListener("message", onClose);
          window.removeEventListener("keydown", onKey, true);
        };
      }, [open]);
      var button = h("button", { type: "button", title: LABEL, "aria-label": "打开续忆", "data-xuyi-diary": "true",
        style: buttonStyle, onClick: function () { setOpen(true); } },
        h("span", { "aria-hidden": "true" }, "📔"), LABEL);
      if (!open) return button;
      var overlay = h("div", { role: "dialog", "aria-modal": "true", "aria-label": LABEL,
        style: { position: "fixed", inset: 0, zIndex: 9999, background: "#f4f1ea" } },
        diaryFrame(),
        h("button", { type: "button", style: closeStyle, title: "关闭（Esc）", onClick: function () { setOpen(false); } }, "关闭 ×"));
      return h(React.Fragment, null, button, ReactDOM.createPortal(overlay, document.body));
    }

    function OpenXuyiAction(props) {
      var subject = props && props.subject;
      if (!subject || (subject.kind !== "bundle" && subject.kind !== "row")) return null;
      if (!subject.pkg || subject.pkg.name !== props.packageName) return null;
      if (subject.kind === "row" && subject.row && subject.row.moduleName !== props.packageName) return null;
      return h("button", { type: "button", onClick: props.onOpen, style: Object.assign({}, buttonStyle, { fontSize: "13px" }) }, "打开续忆");
    }

    // 还没设置完（没装 QQ 插件 / 没选人设 / 还没聊过）就自动切到续忆页面，引导在那里。
    // 每次 DSH 加载至多切一次；续忆的后台还没起来时隔几秒再看，最多看 6 次。
    function watchSetup(layout) {
      if (typeof window.fetch !== "function") return null;
      var fetch = window.fetch.bind(window);
      var stopped = false, tries = 0, timer = null;
      var opened = false;
      try { opened = window.sessionStorage.getItem("xuyi-auto-opened") === "1"; } catch (ignored) {}
      function tick() {
        if (stopped || opened || tries++ >= 6) return;
        function readState(path) {
          return fetch(path, { cache: "no-store" }).then(function (res) {
            return res.ok ? res.json() : null;
          });
        }
        Promise.all([readState("/xuyi/setup/state"),
          readState("/qqbot/im-qqbot/bind").catch(function () { return null; })]).then(function (states) {
          if (stopped || opened) return;
          var state = states[0], binding = states[1];
          if (!state) { timer = setTimeout(tick, 5000); return; }
          var done = binding && binding.phase === "bound"
            && state.qq && state.qq.installed && state.persona && state.persona.configured
            && state.health && state.health.ok;
          if (done) return;
          try { layout.selectPanel(PANEL_ID); opened = true; window.sessionStorage.setItem("xuyi-auto-opened", "1"); }
          catch (error) { timer = setTimeout(tick, 3000); }
        }).catch(function () { timer = setTimeout(tick, 5000); });
      }
      timer = setTimeout(tick, 1500);
      return function () { stopped = true; if (timer) clearTimeout(timer); };
    }

    function registerHeaderButton(slots) {
      var name = typeof slots.has === "function" && !slots.has(HEADER_SLOTS[0]) ? HEADER_SLOTS[1] : HEADER_SLOTS[0];
      slots.inject(name, function () {
        return slots.register({ name: name, id: "xuyi-diary", order: 60, label: function () { return LABEL; } }, DiaryButton);
      });
    }

    function apply(ctx) {
      try {
        if (!ctx || !ctx.slots || typeof ctx.slots.inject !== "function") return;
        hostCtx = ctx;
        window.addEventListener("message", onMessage);
        if (typeof ctx.inject !== "function") { registerHeaderButton(ctx.slots); return; }
        ctx.inject(["layout"], function (pageCtx) {
          var slots = pageCtx && pageCtx.slots;
          var layout = pageCtx && pageCtx.layout;
          if (!slots || !layout) return undefined;
          hostLayout = layout;
          var stopMain = slots.inject("main", function () {
            return slots.register({ name: "main", key: PANEL_ID }, DiaryPage);
          });
          var stopSidebar = slots.inject("sidebar.panellist", function () {
            return slots.register({ name: "sidebar.panellist", id: PANEL_ID, order: 20, label: LABEL }, DiarySidebarIcon);
          });
          // 续忆自己的插件详情页：装好、点「启用」的地方就有「打开续忆」。
          var stopDetail = typeof slots.inject === "function" ? slots.inject("plugins.detail.actions", function () {
            return slots.register({ name: "plugins.detail.actions", id: "xuyi-open", order: 20,
              inject: function () { return { packageName: PACKAGE_NAME, onOpen: function () { layout.selectPanel(PANEL_ID); } }; } },
              OpenXuyiAction);
          }) : null;
          var stopWatch = watchSetup(layout);
          return function () {
            if (hostLayout === layout) hostLayout = null;
            if (typeof stopDetail === "function") stopDetail();
            if (typeof stopWatch === "function") stopWatch();
            if (typeof stopSidebar === "function") stopSidebar();
            if (typeof stopMain === "function") stopMain();
          };
        });
      } catch (error) {
        try { console.warn("[dsh-xuyi-memory] 续忆入口注册失败（记忆功能不受影响）：", error); } catch (ignored) {}
      }
    }

    exports.apply = apply;
    exports.inject = ["slots"];
    return module.exports;
  }
});
