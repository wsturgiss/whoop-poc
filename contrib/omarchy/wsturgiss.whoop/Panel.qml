import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// whoop-poc bar widget. Reads only the local API (whoop-poc serve, API.md):
//   GET  /v1/status   every refreshIntervalSec (2 s while a live session runs,
//                     5 s while a sync drains)
//   GET  /v1/summary  when the strap's newest frame passes the summary's
//                     cursor, when the panel opens, and at least every 15 min
//   POST /v1/sync, /v1/live/start, /v1/live/stop from the panel's buttons
// Left click opens the panel, middle click opens the whoop-poc TUI, right
// click starts a sync.
Panel {
  id: root
  moduleName: "wsturgiss.whoop"
  ipcTarget: "wsturgiss.whoop"
  // The IPC target is declared below so it can carry sync and refresh.
  manageIpc: false

  readonly property int apiPort: setting("apiPort", 8787)
  readonly property string apiBase: "http://127.0.0.1:" + apiPort
  readonly property int refreshSec: setting("refreshIntervalSec", 15)
  readonly property int staleMinutes: setting("staleMinutes", 60)
  readonly property bool showSleep: setting("showSleep", true) === true
  readonly property int liveMinutes: setting("liveMinutes", 30)
  readonly property int maxResponseChars: 2000000

  property bool apiUp: true
  property var status: null
  property var summary: null
  property double summaryFetchedAt: 0
  property bool summaryLoading: false
  property bool actionBusy: false
  property string actionText: ""

  readonly property var barInfo: Model.barState(apiUp, status, summary, staleMinutes, showSleep)
  readonly property var rows: Model.detailRows(status, summary)
  readonly property bool liveOn: Model.liveRunning(status) !== null
  readonly property bool syncOn: Model.syncRunning(status)

  // --- HTTP ---------------------------------------------------------------

  // Sends one request; calls done(code, doc). code 0 means no answer
  // (connection refused or the watchdog aborted it). doc is null when the
  // body is not JSON or too large.
  function request(method, path, body, watchdog, done) {
    var xhr = new XMLHttpRequest()
    var finished = false
    xhr.onreadystatechange = function() {
      if (xhr.readyState !== XMLHttpRequest.DONE || finished) return
      finished = true
      // A plugin reload can destroy the watchdog while a request is in flight.
      if (!watchdog || typeof watchdog.stop !== "function") return
      watchdog.stop()
      var doc = null
      var text = xhr.responseText || ""
      if (text.length > 0 && text.length <= root.maxResponseChars) {
        try { doc = JSON.parse(text) } catch (e) { doc = null }
      }
      done(xhr.status, doc)
    }
    watchdog.abortTarget = xhr
    watchdog.restart()
    xhr.open(method, root.apiBase + path)
    if (method === "POST") {
      xhr.setRequestHeader("Content-Type", "application/json")
      xhr.send(JSON.stringify(body || {}))
    } else {
      xhr.send()
    }
  }

  function pollStatus() {
    if (statusWatchdog.running) return
    request("GET", "/v1/status", null, statusWatchdog, function(code, doc) {
      if (code === 200 && doc && doc.schema === "whoop-poc/status@1") {
        root.apiUp = true
        root.status = doc
        if (Model.summaryDue(doc, root.summary, root.summaryFetchedAt, Date.now(), 900)) root.fetchSummary(false)
      } else {
        root.apiUp = false
      }
    })
  }

  // force: fetch even if the last one is recent (the panel just opened).
  function fetchSummary(force) {
    if (summaryWatchdog.running) return
    if (!force && Date.now() - summaryFetchedAt < 60000 && summary !== null) return
    summaryLoading = true
    request("GET", "/v1/summary", null, summaryWatchdog, function(code, doc) {
      root.summaryLoading = false
      root.summaryFetchedAt = Date.now()
      if (code === 200 && doc && typeof doc.schema === "string" && doc.schema.indexOf("whoop-poc/snapshot@") === 0) {
        root.summary = doc
      }
    })
  }

  function post(path, body) {
    if (actionBusy) return
    actionBusy = true
    actionText = ""
    request("POST", path, body, actionWatchdog, function(code, doc) {
      root.actionBusy = false
      root.actionText = Model.actionMessage(code, doc)
      root.pollStatus()
    })
  }

  function startSync() { post("/v1/sync", {}) }
  function startLive() { post("/v1/live/start", { minutes: liveMinutes }) }
  function stopLive() { post("/v1/live/stop", {}) }
  function openTui() {
    if (root.bar) root.bar.run("omarchy-launch-or-focus-tui whoop-poc")
    close()
  }
  function startApi() {
    if (root.bar) root.bar.run("systemctl --user start whoop-api")
    actionText = "Starting whoop-api…"
    apiRetry.restart()
  }

  // omarchy-shell wsturgiss.whoop <open|close|toggle|sync|refresh>
  ShellIpc {
    target: "wsturgiss.whoop"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function sync(): void { root.startSync() }
    function refresh(): void { root.pollStatus(); root.fetchSummary(true) }
  }

  RequestWatchdog { id: statusWatchdog; interval: 3000 }
  RequestWatchdog { id: summaryWatchdog; interval: 120000; onTriggered: root.summaryLoading = false }
  RequestWatchdog { id: actionWatchdog; interval: 10000 }

  Timer {
    interval: Model.pollInterval(root.apiUp, root.status, root.refreshSec)
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.pollStatus()
  }

  Timer { id: apiRetry; interval: 1500; onTriggered: root.pollStatus() }

  onOpenedChanged: if (opened) { pollStatus(); fetchSummary(true) }
  onApiUpChanged: if (!actionBusy) actionText = ""

  // --- bar ----------------------------------------------------------------

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  TextMetrics {
    id: labelMetrics
    font.family: root.bar ? root.bar.fontFamily : Style.font.family
    font.pixelSize: Style.bar.iconFont
    text: root.barInfo.text
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barInfo.icon
    labelText: vertical ? "" : root.barInfo.text
    slotSize: vertical ? Style.bar.iconSlot : Style.bar.iconSlot + labelMetrics.advanceWidth + Style.space(4)
    dimmed: root.barInfo.state === "offline" || root.barInfo.state === "stale" || root.barInfo.state === "loading"
    active: root.barInfo.state === "live"
    tooltipText: root.opened ? "" : root.barInfo.tooltip
    onPressed: function(b) {
      if (b === Qt.MiddleButton) root.openTui()
      else if (b === Qt.RightButton) root.startSync()
      else root.toggle()
    }
  }

  // --- panel --------------------------------------------------------------

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight)

    content: PanelKeyCatcher {
      anchors.fill: parent
      implicitHeight: column.implicitHeight
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(12)

        // Hero: icon · title / sync line · HR
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, heroBpm.implicitHeight)

          PlainText {
            id: heroIcon
            text: root.barInfo.icon
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: heroBpm.left
            anchors.rightMargin: Style.space(10)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            PlainText {
              text: "Whoop"
              font.pixelSize: Style.font.title
              font.bold: true
              width: parent.width
              elide: Text.ElideRight
            }
            PlainText {
              text: (root.apiUp ? Model.syncLine(root.status) : "API not running").toUpperCase()
              opacity: 0.7
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              width: parent.width
              elide: Text.ElideRight
            }
          }

          PlainText {
            id: heroBpm
            text: root.barInfo.state === "offline" ? "—" : root.barInfo.text.split(" ")[0]
            font.pixelSize: Style.font.displayLarge
            font.bold: true
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
          }
        }

        PlainText {
          visible: text !== ""
          text: Model.liveLine(root.status)
          font.pixelSize: Style.font.body
          font.bold: true
          color: root.bar ? root.bar.urgent : Commons.Color.urgent
          width: parent.width
          elide: Text.ElideRight
        }

        PanelSeparator { foreground: root.bar ? root.bar.foreground : Commons.Color.foreground }

        Column {
          width: parent.width
          spacing: Style.spacing.labelGap
          visible: root.apiUp

          Repeater {
            model: root.rows
            Row {
              required property var modelData
              width: column.width
              spacing: Style.space(8)
              PlainText {
                id: rowLabel
                text: modelData.label
                visible: modelData.label !== ""
                width: visible ? Style.space(96) : 0
                opacity: 0.6
                font.pixelSize: Style.font.bodySmall
              }
              PlainText {
                text: modelData.value !== "" ? modelData.value : "—"
                width: parent.width - (rowLabel.visible ? rowLabel.width + parent.spacing : 0)
                horizontalAlignment: Text.AlignRight
                elide: Text.ElideRight
                font.pixelSize: Style.font.bodySmall
              }
            }
          }

          PlainText {
            visible: root.summary === null
            text: root.summaryLoading ? "Loading summary…" : "No summary yet"
            opacity: 0.6
            font.pixelSize: Style.font.bodySmall
          }
        }

        PlainText {
          visible: !root.apiUp
          width: parent.width
          wrapMode: Text.Wrap
          text: "Nothing answers on 127.0.0.1:" + root.apiPort + ". The whoop-api user unit runs `whoop-poc serve`; install it with contrib/install.sh --api."
          opacity: 0.7
          font.pixelSize: Style.font.bodySmall
        }

        PanelSeparator { foreground: root.bar ? root.bar.foreground : Commons.Color.foreground }

        Row {
          id: actions
          width: parent.width
          spacing: Style.space(6)
          readonly property int count: root.apiUp ? 3 : 2
          readonly property real cellWidth: (width - spacing * (count - 1)) / count

          ActionButton {
            visible: root.apiUp
            text: root.syncOn ? "Syncing" : "Sync"
            iconText: Model.ICON_SYNC
            enabled: !root.actionBusy && !root.syncOn
            onClicked: root.startSync()
          }
          ActionButton {
            visible: root.apiUp
            text: root.liveOn ? "Stop live" : "Live " + root.liveMinutes + "m"
            iconText: Model.ICON_HEART_PULSE
            active: root.liveOn
            enabled: !root.actionBusy
            onClicked: root.liveOn ? root.stopLive() : root.startLive()
          }
          ActionButton {
            visible: !root.apiUp
            text: "Start API"
            iconText: "\u{f040a}"
            onClicked: root.startApi()
          }
          ActionButton {
            text: "Open TUI"
            iconText: "\u{f018d}"
            onClicked: root.openTui()
          }
        }

        PlainText {
          visible: text !== ""
          text: root.actionBusy ? "…" : root.actionText
          opacity: 0.7
          width: parent.width
          wrapMode: Text.Wrap
          font.pixelSize: Style.font.caption
        }
      }
    }
  }

  component PlainText: Text {
    textFormat: Text.PlainText
    color: root.bar ? root.bar.foreground : Commons.Color.foreground
    font.family: root.bar ? root.bar.fontFamily : Style.font.family
  }

  component ActionButton: Button {
    width: actions.cellWidth
    iconSize: Style.font.title
    fontSize: Style.font.bodySmall
    foreground: root.bar ? root.bar.foreground : Commons.Color.foreground
    fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
    horizontalPadding: Style.spacing.controlPaddingX
    verticalPadding: Style.spacing.controlPaddingY + Style.space(2)
    bordered: true
    opacity: enabled ? 1 : 0.5
  }

  // Aborts the request it was last handed if it hasn't finished in time.
  component RequestWatchdog: Timer {
    property var abortTarget: null
    repeat: false
    onTriggered: if (abortTarget) abortTarget.abort()
  }
}
