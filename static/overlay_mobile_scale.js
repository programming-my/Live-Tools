// Keeps the overlay's fixed 1080x1920 design scaling as one proportioned
// unit (not reflowing) to fit whatever size the browser/OBS source actually
// renders at - "contain" behavior: shrink/grow to the limiting dimension,
// letting overlay_mobile.css's body flexbox center the result. Past a
// landscape aspect ratio, overlay_mobile.css's own media query takes over
// and reflows the layout instead - this script still runs there too, since
// the landscape rules reuse the same --overlay-scale variable for sizing.
const OVERLAY_DESIGN_WIDTH = 1080;
const OVERLAY_DESIGN_HEIGHT = 1920;

function applyOverlayScale() {
  const scale = Math.min(
    window.innerWidth / OVERLAY_DESIGN_WIDTH,
    window.innerHeight / OVERLAY_DESIGN_HEIGHT,
  );
  document.documentElement.style.setProperty("--overlay-scale", scale);
}

window.addEventListener("resize", applyOverlayScale);
applyOverlayScale();
