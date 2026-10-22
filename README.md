# eBay Live Tools

A desktop app for eBay Live sellers and editors: automatically downloads the
replay once a live event ends, extracts pinned-auction overlays and short
clips from it, and - while an event is actually live - shows a real-time chat
and auction overlay you can capture in OBS.

## Download and install (macOS)

1. Go to the [Releases page](https://github.com/programming-ny/Live-Tools/releases)
   and download the `.zip` under the latest release.
2. Unzip it (double-click the `.zip` if your browser didn't already), and
   drag **eBay Live Tools.app** wherever you'd like to keep it (e.g. your
   Applications folder).
3. Double-click to launch it. **This app isn't signed**, so the first time
   you open it, macOS will refuse and say it's "damaged" or "can't be
   opened." To get past that one-time warning:
   - Right-click (or Control-click) the app → **Open** → **Open** again in
     the dialog that pops up, **or**
   - Go to **System Settings → Privacy & Security**, scroll down, and click
     **"Open Anyway"** next to the message about eBay Live Tools.
4. Your default browser opens automatically to the app's UI. The app keeps
   running in the background (there's no Dock window) - to quit it, right-
   click its Dock icon and choose **Quit**, or Cmd+Q while it's the active
   app.

Downloaded replays, clip exports, and overlay exports are saved to
`~/Downloads/eBay Live Tools` by default. You can change this from the
**⚙ Settings** menu in the top-right corner of the app, which also shows the
installed version and a **Check for Updates** button.

## Features

### Live Replay Downloader
Paste an eBay Live event URL and click **Start monitoring**. The app polls
the event page on the interval you set, shows LIVE/ENDED status and logs in
real time, and automatically starts downloading the replay as soon as the
event ends and a replay manifest becomes available - no need to keep
checking back yourself.

### Download Queue
Add multiple event URLs to a queue to have them monitored and downloaded one
after another, instead of babysitting a single event at a time.

### Export Auction Overlay
Extracts the pinned auctions from an event into a separate video file with a
blue background, for editors to key against. Runs automatically after a
replay finishes downloading - or paste an event URL here to (re-)export
manually, e.g. with different calibration. Works even for an event you never
downloaded a replay of, rendering directly from the event's own data instead
(calibration fields are ignored in that case).

### Download Clip
Extracts short clips by timestamp from an event's replay. Reuses an
already-downloaded file if you have one; otherwise it clips directly from
the live replay manifest - either way, no full download of the whole event
is needed just to grab a clip.

### Chat and Auction Monitor
A live, browser-source-friendly overlay for events that are currently live:
the current auction card (title/price/winner/countdown) plus a live chat
panel, updating in real time while the event is live. Paste an event URL
into this section and click **Start watching**; each watched event then
gives you:
- **Open preview** - the overlay page with chat font-size controls visible.
- **Copy OBS URL** - the same page with controls hidden, for a clean
  browser-source capture in OBS.

Card visibility is pin-driven, not timer-based: it appears the moment an
item is pinned ("starting soon"), moves through live bidding once the
auction starts, and - once the auction ends - stays up showing the winner's
name until the item is actually unpinned.

**OBS tip:** uncheck "Shutdown source when not visible" on the browser
source, or the connection will keep dropping/reconnecting whenever the scene
isn't active. The overlay has a transparent background by default.

**Viewing full-screen on iOS:** Safari has no reliable in-page fullscreen
API for arbitrary web pages. To get a chrome-free view there, open the
overlay URL in Safari, tap the Share icon → **Add to Home Screen**, then
open it from the home screen icon instead of Safari - it launches in
"standalone" mode with no URL bar or tab bar.

## Notes

- Detection relies on scraping eBay's page HTML/JSON for status markers and
  replay manifest URLs, so it may need updating if eBay changes their page
  structure.
- Only one monitoring job (outside the queue) runs at a time.
- There's no authentication on this app - it's meant to run locally on your
  own machine, not be exposed to the internet.

## Running from source (for development)

```bash
cd ebay-live-downloader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Then open http://localhost:8000. This runs the exact same app as the
packaged `.app`, just from source - useful for development, or for running
on Linux/Windows where the packaged build isn't available.
