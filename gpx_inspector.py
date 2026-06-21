#!/usr/bin/env python3
"""Render any GPX file or directory as an interactive Leaflet/OSM map.

Tracks and routes get click-to-pin elevation profiles (per-segment distance and
gain, Savitzky-Golay smoothing); waypoints render in a higher pane so they stay
clickable over dense tracks. Serves locally or emits a standalone HTML page.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET


def namespace_prefix(tag: str) -> str:
    return tag.split("}", 1)[0] + "}" if tag.startswith("{") else ""


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def round_coord(value: float, precision: int | None) -> float:
    return round(value, precision) if precision is not None else value


def child_text(el: ET.Element, ns: str, tag: str) -> str | None:
    c = el.find(ns + tag)
    return c.text.strip() if c is not None and c.text and c.text.strip() else None


def update_bbox(bbox: list[float] | None, lon: float, lat: float) -> list[float]:
    if bbox is None:
        return [lon, lat, lon, lat]
    return [min(bbox[0], lon), min(bbox[1], lat), max(bbox[2], lon), max(bbox[3], lat)]


def merge_bbox(a: list[float] | None, b: list[float] | None) -> list[float] | None:
    if a is None:
        return list(b) if b else None
    if b is None:
        return a
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def coords_from_points(points, ns: str, precision: int | None) -> list[list[float]]:
    """[lon, lat] (or [lon, lat, ele]) for a sequence of <trkpt>/<rtept> elements."""
    out: list[list[float]] = []
    for p in points:
        lon, lat = parse_float(p.get("lon")), parse_float(p.get("lat"))
        if lon is None or lat is None:
            continue
        lon, lat = round_coord(lon, precision), round_coord(lat, precision)
        ele_el = p.find(ns + "ele")
        ele = parse_float(ele_el.text) if ele_el is not None and ele_el.text else None
        out.append([lon, lat] if ele is None else [lon, lat, round(ele, 1)])
    return out


EARTH_M_PER_DEG = 111320.0  # metres per degree of latitude


def _point_seg_dist(px, py, ax, ay, bx, by) -> float:
    """Perpendicular distance from point to segment a-b (all in metres)."""
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def rdp(coords: list[list[float]], eps_m: float) -> list[list[float]]:
    """Ramer-Douglas-Peucker simplification of a [lon, lat, ...] polyline.

    `eps_m` is the tolerance in metres: a point is dropped only if it lies
    within eps_m of the line through its retained neighbours. Distances use a
    local equirectangular projection (accurate at trail scale). Kept points keep
    their full coordinate, elevation included. Iterative (explicit stack), so it
    is safe on very long, dense tracks.
    """
    n = len(coords)
    if eps_m <= 0 or n < 3:
        return coords
    lat0 = sum(c[1] for c in coords) / n
    klon = EARTH_M_PER_DEG * math.cos(math.radians(lat0))
    xy = [(c[0] * klon, c[1] * EARTH_M_PER_DEG) for c in coords]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = xy[i]
        bx, by = xy[j]
        dmax, idx = 0.0, -1
        for k in range(i + 1, j):
            d = _point_seg_dist(xy[k][0], xy[k][1], ax, ay, bx, by)
            if d > dmax:
                dmax, idx = d, k
        if dmax > eps_m:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [c for c, k in zip(coords, keep) if k]


def line_feature(fid, name, ftype, lines, file) -> dict:
    bbox = None
    for line in lines:
        for lon, lat, *_ in line:
            bbox = update_bbox(bbox, lon, lat)
    feat = {
        "type": "Feature",
        "geometry": {"type": "MultiLineString", "coordinates": lines},
        "properties": {
            "id": fid,
            "name": name,
            "kind": ftype,
            "file": file,
            "segments": len(lines),
            "points": sum(len(c) for c in lines),
        },
    }
    if bbox:
        feat["bbox"] = bbox
    return feat


def point_feature(fid, name, file, coord, group) -> dict:
    props = {"id": fid, "name": name, "kind": "waypoint", "file": file}
    if group:
        props["group"] = group
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coord},
        "properties": props,
        "bbox": [coord[0], coord[1], coord[0], coord[1]],
    }


def _simplify_line(
    coords: list[list[float]], simplify: float, stats: dict
) -> list[list[float]]:
    stats["raw"] += len(coords)
    kept = rdp(coords, simplify)
    stats["kept"] += len(kept)
    return kept


def _tracks_from(root, ns, path, precision, simplify, stats) -> list[dict]:
    feats = []
    for i, trk in enumerate(root.findall(ns + "trk")):
        lines = []
        for seg in trk.iter(ns + "trkseg"):
            c = coords_from_points(seg.findall(ns + "trkpt"), ns, precision)
            if len(c) >= 2:
                lines.append(_simplify_line(c, simplify, stats))
        if lines:
            name = child_text(trk, ns, "name") or path.stem
            feats.append(
                line_feature(f"{path.name}#trk{i}", name, "track", lines, path.name)
            )
    return feats


def _routes_from(root, ns, path, precision, simplify, stats) -> list[dict]:
    feats = []
    for i, rte in enumerate(root.findall(ns + "rte")):
        c = coords_from_points(rte.findall(ns + "rtept"), ns, precision)
        if len(c) >= 2:
            name = child_text(rte, ns, "name") or f"{path.stem} (route)"
            feats.append(
                line_feature(
                    f"{path.name}#rte{i}",
                    name,
                    "route",
                    [_simplify_line(c, simplify, stats)],
                    path.name,
                )
            )
    return feats


def _waypoints_from(root, ns, path, precision, group_name) -> list[dict]:
    feats = []
    for i, wpt in enumerate(root.findall(ns + "wpt")):
        coords = coords_from_points([wpt], ns, precision)
        if coords:
            name = child_text(wpt, ns, "name") or f"{path.stem} wpt {i + 1}"
            feats.append(
                point_feature(
                    f"{path.name}#wpt{i}", name, path.name, coords[0], group_name
                )
            )
    return feats


def features_from_file(
    path: Path, precision: int | None, simplify: float, stats: dict
) -> list[dict]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        print(f"skip {path.name}: {exc}", file=sys.stderr)
        return []
    ns = namespace_prefix(root.tag)
    meta = root.find(ns + "metadata")
    group_name = (
        child_text(meta, ns, "name") if meta is not None else None
    ) or path.stem
    return (
        _tracks_from(root, ns, path, precision, simplify, stats)
        + _routes_from(root, ns, path, precision, simplify, stats)
        + _waypoints_from(root, ns, path, precision, group_name)
    )


def encode_delta_quantized(coords: list[list[float]]) -> dict | list:
    """Encode coordinates as delta + quantized integers.

    Coerces the line to a single dimensionality: elevation is kept only when
    *every* point carries it, otherwise the line is encoded as bare lon/lat. This
    keeps the decoder's fixed n_dims stride valid for arbitrary GPX where some
    points may lack <ele> (mixed dims would otherwise IndexError or corrupt the
    decode).
    """
    if not coords or len(coords) < 2 or not isinstance(coords[0], list):
        return coords

    SCALE = 1000000
    n_dims = 3 if all(len(c) >= 3 for c in coords) else 2
    base = coords[0][:n_dims]
    deltas = []
    prev = [base[0] * SCALE, base[1] * SCALE] + list(base[2:])

    for c in coords[1:]:
        curr = [c[0] * SCALE, c[1] * SCALE] + list(c[2:n_dims])
        for i in range(n_dims):
            deltas.append(int(round(curr[i] - prev[i])))
        prev = curr

    return {"base": base, "deltas": deltas, "n_dims": n_dims}


def build_geojson(inputs: list[Path], precision: int | None, simplify: float) -> dict:
    feats: list[dict] = []
    stats = {"raw": 0, "kept": 0}
    for p in inputs:
        feats.extend(features_from_file(p, precision, simplify, stats))
    if simplify > 0 and stats["raw"]:
        dropped = stats["raw"] - stats["kept"]
        print(
            f"RDP simplify ε={simplify:g} m: {stats['raw']} -> {stats['kept']} line points "
            f"({100 * dropped / stats['raw']:.0f}% fewer)",
            file=sys.stderr,
        )
    bbox = None
    for feat in feats:
        bbox = merge_bbox(bbox, feat.pop("bbox", None))

    bare_feats = []
    for feat in feats:
        p = feat["properties"]
        geom = feat["geometry"]
        coords = geom["coordinates"]
        encoded_coords = (
            [coords]
            if geom["type"] == "Point"
            else [encode_delta_quantized(line) for line in coords]
        )
        bare_feats.append(
            {
                **{
                    k: p.get(k)
                    for k in (
                        "id",
                        "name",
                        "kind",
                        "file",
                        "segments",
                        "points",
                        "group",
                    )
                },
                "coords": encoded_coords,
            }
        )

    collection = {"features": bare_feats}
    if bbox:
        collection["bbox"] = bbox
    return collection


PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>GPX inspector</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  html, body { margin: 0; height: 100%; font: 13px/1.4 system-ui, sans-serif; }
  #map { position: absolute; inset: 0 0 0 280px; }
  #side { position: absolute; left: 0; top: 0; bottom: 0; width: 280px;
          box-sizing: border-box; padding: 8px; overflow-y: auto;
          border-right: 1px solid #ccc; background: #fafafa; }
  #side h1 { font-size: 14px; margin: 4px 0 8px; }
  #q { width: 100%; box-sizing: border-box; padding: 4px; margin-bottom: 6px; }
  .item { padding: 3px 4px; cursor: pointer; border-radius: 3px;
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .item:hover { background: #e8e8e8; }
  .item.sel { background: #ffe08a; }
  .tag { color: #fff; font-size: 9px; padding: 0 4px; border-radius: 6px;
         margin-right: 5px; vertical-align: 1px; }
  .tag.track { background: #1f6feb; } .tag.route { background: #9c36b5; }
  .tag.waypoint { background: #2b8a3e; }
  #toggle { position: absolute; top: 8px; right: 8px; z-index: 1001;
            width: 34px; height: 34px; padding: 0; cursor: pointer;
            border: 1px solid #ccc; border-radius: 4px; background: #fff;
            font-size: 16px; line-height: 32px;
            box-shadow: 0 1px 4px rgba(0,0,0,0.2); }
  body.collapsed #side { display: none; }
  body.collapsed #map { left: 0; }
  body.collapsed #profile { left: 0; }
  #profile { position: absolute; left: 280px; right: 0; bottom: 0; height: 150px;
             box-sizing: border-box; padding: 6px 10px; display: none; z-index: 600;
             background: rgba(255,255,255,0.96); border-top: 1px solid #ccc; }
  #profhead { font-size: 12px; margin-bottom: 2px; padding-right: 70px; }
  #smoothbox { position: absolute; right: 10px; top: 6px; font-size: 11px;
               color: #555; user-select: none; cursor: pointer; }
  #profsvg { width: 100%; height: 112px; display: block; cursor: crosshair; }
  #probe { float: right; color: #e8590c; }
</style>
</head>
<body>
<button id="toggle" title="Toggle list">✕</button>
<div id="side">
  <h1>__COUNT__ items</h1>
  <input id="q" placeholder="filter by name..."/>
  <div id="list"></div>
</div>
<div id="map"></div>
<div id="profile">
  <div id="profhead"></div>
  <label id="smoothbox"><input type="checkbox" id="smoothcb" checked/> smooth</label>
  <svg id="profsvg" preserveAspectRatio="none"></svg>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const RAW_DATA = __GEOJSON__;

function decodeCoord(encoded) {
  if (!encoded || !encoded.base) return encoded;
  const SCALE = 1000000, base = encoded.base, deltas = encoded.deltas || [], nDims = encoded.n_dims;
  const result = [base];
  const prev = new Array(nDims);
  prev[0] = base[0] * SCALE;
  prev[1] = base[1] * SCALE;
  for (let k = 2; k < nDims; k++) prev[k] = (base[k] || 0);

  for (let i = 0; i < deltas.length; i += nDims) {
    const curr = new Array(nDims);
    for (let j = 0; j < nDims; j++) {
      const v = prev[j] + (deltas[i + j] || 0);
      curr[j] = j < 2 ? v / SCALE : v;
      prev[j] = v;
    }
    result.push(curr);
  }
  return result;
}

const HTML_ESC = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => HTML_ESC[c]);
}

// Separate tracks and waypoints for z-order control (waypoints on top)
const DATA_TRACKS = {features: [], bbox: RAW_DATA.bbox};
const DATA_WAYPOINTS = {features: []};
for (const f of RAW_DATA.features) {
  const coords = f.kind === 'waypoint' ? decodeCoord(f.coords[0]) : f.coords.map(decodeCoord);
  const feat = {type:'Feature', geometry: f.kind==='waypoint'?{type:'Point',coordinates:coords}:{type:'MultiLineString',coordinates:coords},
                properties:{id:f.id,name:f.name,kind:f.kind,file:f.file,segments:f.segments,points:f.points,group:f.group}};
  (f.kind === 'waypoint' ? DATA_WAYPOINTS : DATA_TRACKS).features.push(feat);
}

if (window.innerWidth < 600) document.body.classList.add('collapsed');

const map = L.map('map');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

// One pane holding a single shared canvas (see renderer note below).
map.createPane('tracks').style.zIndex = 400;

// "Show my location" — needs a secure context (https/file/localhost).
let meMarker = null, meCircle = null;
const LocateCtl = L.Control.extend({
  options: { position: 'topleft' },
  onAdd: function () {
    const a = L.DomUtil.create('a', 'leaflet-bar leaflet-control');
    a.href = '#'; a.title = 'Show my location'; a.textContent = '◉';
    a.style.cssText = 'width:30px;height:30px;line-height:30px;text-align:center;font-size:18px;background:#fff;cursor:pointer';
    L.DomEvent.on(a, 'click', e => {
      L.DomEvent.stop(e);
      map.locate({ setView: true, maxZoom: 16, enableHighAccuracy: true });
    });
    return a;
  }
});
map.addControl(new LocateCtl());
map.on('locationfound', e => {
  const r = e.accuracy / 2;
  if (meMarker) { meMarker.setLatLng(e.latlng); meCircle.setLatLng(e.latlng).setRadius(r); }
  else {
    meMarker = L.circleMarker(e.latlng, MY_LOC).addTo(map).bindPopup('You are here (±' + Math.round(e.accuracy) + ' m)');
    meCircle = L.circle(e.latlng, { radius: r, ...MY_LOC_CIRCLE }).addTo(map);
  }
});
map.on('locationerror', e => alert('Location unavailable: ' + e.message));

const BASE = { color: '#1f6feb', weight: 2, opacity: 0.6 };
const ROUTE = { color: '#9c36b5', weight: 2, opacity: 0.6, dashArray: '6 4' };
const HOVER = { color: '#e8590c', weight: 4, opacity: 0.85 };
const HILITE = { color: '#e8590c', weight: 4, opacity: 1 };
const WPT = { radius: 4, color: '#2b8a3e', weight: 1, fillColor: '#69db7c', fillOpacity: 0.9 };
const WPT_TOL = 18;                          // waypoint hit-slop; effective radius = WPT.radius + WPT_TOL
const wptClickTol = () => WPT_TOL;           // shared so all markers reuse one function
const WPT_SEL = { radius: 6, color: '#c92a2a', weight: 2, fillColor: '#ff8787', fillOpacity: 1 };
const MY_LOC = { radius: 6, color: '#1971c2', weight: 2, fillColor: '#4dabf7', fillOpacity: 1 };
const MY_LOC_CIRCLE = { color: '#1971c2', weight: 1, opacity: 0.4, fillOpacity: 0.1 };
const layers = {};
let hovered = null, selected = null, selectedGroup = null;

function baseStyle(p) {
  return p.kind === 'waypoint' ? WPT : p.kind === 'route' ? ROUTE : BASE;
}

const featureHandler = (f, layer) => {
  const p = f.properties;
  layers[p.id] = layer;
  const extra = p.kind === 'waypoint' ? '' : `<br>${p.points} pts, ${p.segments} seg(s)`;
  layer.bindPopup(`<b>${esc(p.name)}</b>${p.kind === 'waypoint' ? '' : `<br><i>${p.kind}</i>`}${extra}`);
  if (p.kind !== 'waypoint') {
    layer.on('mouseover', () => {
      // Single hover owner: reset any prior hovered track in case its mouseout
      // was missed (overlapping polylines / bringToFront reordering swallow it),
      // otherwise stuck-orange neighbours look co-selected.
      if (hovered && hovered !== p.id && hovered !== selected && layers[hovered])
        layers[hovered].setStyle(baseStyle(layers[hovered].feature.properties));
      hovered = p.id;
      if (p.id !== selected) layer.setStyle(HOVER).bringToFront();
    });
    layer.on('mouseout', () => { if (hovered === p.id) hovered = null; if (p.id !== selected) layer.setStyle(baseStyle(p)); });
  }
  layer.on('click', e => {
    if (p.kind === 'waypoint') { select(p.id, false); return; }
    // Waypoints win overlaps: a hovered track is brought to front (drawn last),
    // so a click on top of a waypoint would otherwise select the track.
    const w = waypointNear(e.latlng);
    if (w) { select(w, false); return; }
    select(p.id, false);
    pinAt(nearestIndexToLatLng(e.latlng));
  });
};

// Nearest waypoint within its hit radius of a click, or null — lets waypoints
// take selection priority over tracks drawn above them.
function waypointNear(latlng) {
  const cp = map.latLngToLayerPoint(latlng);
  let best = null, bestD = Infinity;
  groupWaypoints.eachLayer(l => {
    const d = map.latLngToLayerPoint(l.getLatLng()).distanceTo(cp);
    if (d < bestD) { bestD = d; best = l.feature.properties.id; }
  });
  return bestD <= WPT.radius + WPT_TOL ? best : null;
}

// Single shared canvas renderer gives every line/marker invisible hit-slop via
// `tolerance` (SVG ignores it — canvas is required). It must be ONE canvas, not
// one per pane: separate canvases each cover the whole map and the top one
// swallows clicks meant for the layer below. With a single canvas all layers
// hit-test together and the topmost (last-drawn) layer under the cursor wins, so
// waypoints (added last) stay clickable over dense tracks. Tracks get ~11 px
// (modest — avoid grabbing the wrong trail in overlapping clusters).
const hitRenderer = L.canvas({ pane: 'tracks', tolerance: 10 });

const groupTracks = L.geoJSON(DATA_TRACKS, {
  renderer: hitRenderer, style: f => baseStyle(f.properties), onEachFeature: featureHandler
}).addTo(map);

// circleMarkers from pointToLayer do not inherit the geoJSON renderer, so set it
// explicitly. The per-marker tolerance override gives waypoints a fatter hit
// radius (the touch-target ideal) while tracks keep the renderer's 10.
const groupWaypoints = L.geoJSON(DATA_WAYPOINTS, {
  pointToLayer: (f, latlng) => {
    const m = L.circleMarker(latlng, { ...WPT, renderer: hitRenderer });
    m._clickTolerance = wptClickTol;
    return m;
  },
  onEachFeature: featureHandler
}).addTo(map);

if (DATA_TRACKS.bbox) {
  map.fitBounds([[DATA_TRACKS.bbox[1], DATA_TRACKS.bbox[0]], [DATA_TRACKS.bbox[3], DATA_TRACKS.bbox[2]]], { padding: [20, 20] });
} else {
  const bounds = groupTracks.getBounds().extend(groupWaypoints.getBounds());
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [20, 20] });
  else map.setView([0, 0], 2);
}

function markRow(key) {
  document.querySelectorAll('.item').forEach(el => el.classList.toggle('sel', el.dataset.key === key));
}

function clearSelection() {
  if (selected && layers[selected])
    layers[selected].setStyle(baseStyle(layers[selected].feature.properties));
  selected = null;
  // Clear any lingering hover so a stuck-orange neighbour doesn't survive a new selection.
  if (hovered && layers[hovered]) {
    layers[hovered].setStyle(baseStyle(layers[hovered].feature.properties));
    hovered = null;
  }
  if (selectedGroup) {
    for (const id of selectedGroup.ids) if (layers[id]) layers[id].setStyle(WPT);
    selectedGroup = null;
  }
}

function select(id, zoom) {
  clearSelection();
  selected = id;
  const layer = layers[id];
  if (!layer) return;
  const p = layer.feature.properties;
  layer.setStyle(p.kind === 'waypoint' ? WPT_SEL : HILITE);
  if (p.kind === 'waypoint' && layer.bringToFront) layer.bringToFront();
  if (zoom) {
    if (layer.getBounds) map.fitBounds(layer.getBounds(), { padding: [40, 40] });
    else map.setView(layer.getLatLng(), Math.max(map.getZoom(), 15));
  }
  markRow(id);
  if (p.kind === 'waypoint') hideProfile(); else showProfile(id);
}

// A grouped waypoint collection: highlight + fit all its markers, no profile.
function selectGroup(e, zoom) {
  clearSelection();
  selectedGroup = e;
  const lls = [];
  for (const id of e.ids) {
    const layer = layers[id];
    if (!layer) continue;
    layer.setStyle(WPT_SEL);
    if (layer.bringToFront) layer.bringToFront();
    lls.push(layer.getLatLng());
  }
  if (zoom && lls.length) map.fitBounds(L.latLngBounds(lls), { padding: [40, 40] });
  markRow(e.key);
  hideProfile();
}

// --- elevation profile ------------------------------------------------------
const profile = document.getElementById('profile');
const profhead = document.getElementById('profhead');
const profsvg = document.getElementById('profsvg');
const smoothbox = document.getElementById('smoothbox');
const smoothcb = document.getElementById('smoothcb');
let hoverMarker = null, pinMarker = null, smoothOn = true;

function hideProfile() {
  profile.style.display = 'none';
  if (pinMarker) { map.removeLayer(pinMarker); pinMarker = null; }
}

function haversine(a, b) {
  const R = 6371000, toR = Math.PI / 180;
  const dLat = (b[1] - a[1]) * toR, dLon = (b[0] - a[0]) * toR;
  const h = Math.sin(dLat / 2) ** 2 +
            Math.cos(a[1] * toR) * Math.cos(b[1] * toR) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

const W = 1000, H = 100, pad = 3;
const STEP_M = 10;                     // uniform resample step
const SG_HALF = 4, SG_DEGREE = 2;      // Savitzky-Golay half-window / degree (~80 m)
const HAMPEL_HALF = 3, HAMPEL_K = 3;   // despike half-window / MAD threshold
const GAIN_DEADBAND_M = 1.5;           // ignore wiggles below this in the ↑/↓ totals

function median(a) {
  const s = a.slice().sort((x, y) => x - y), m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
function solveLinear(A, b) {
  const n = b.length, M = A.map((row, i) => row.concat(b[i]));
  for (let c = 0; c < n; c++) {
    let p = c;
    for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
    if (Math.abs(M[p][c]) < 1e-12) return null;
    [M[c], M[p]] = [M[p], M[c]];
    for (let r = 0; r < n; r++) {
      if (r === c) continue;
      const f = M[r][c] / M[c][c];
      for (let k = c; k <= n; k++) M[r][k] -= f * M[c][k];
    }
  }
  return M.map((row, i) => row[n] / row[i]);
}
function polyCentre(X, Y, deg) {  // Savitzky-Golay: fitted value at the centre (x=0)
  deg = Math.min(deg, X.length - 1);
  if (deg < 1) return null;
  const sz = deg + 1;
  const ATA = Array.from({ length: sz }, () => new Array(sz).fill(0));
  const ATy = new Array(sz).fill(0);
  for (let r = 0; r < X.length; r++) {
    const pw = [1];
    for (let p = 1; p < sz; p++) pw.push(pw[p - 1] * X[r]);
    for (let a = 0; a < sz; a++) {
      ATy[a] += pw[a] * Y[r];
      for (let b = 0; b < sz; b++) ATA[a][b] += pw[a] * pw[b];
    }
  }
  const sol = solveLinear(ATA, ATy);
  return sol ? sol[0] : null;
}
function resampleUniform(xs, ys, step) {
  const vx = [], vy = [];
  for (let i = 0; i < ys.length; i++) if (ys[i] != null) { vx.push(xs[i]); vy.push(ys[i]); }
  if (vx.length < 2) return null;
  const total = vx[vx.length - 1];
  const m = Math.max(2, Math.floor(total / step) + 1);
  const gx = new Array(m), gz = new Array(m);
  let j = 0;
  for (let i = 0; i < m; i++) {
    const x = i === m - 1 ? total : i * step;
    while (j < vx.length - 2 && vx[j + 1] < x) j++;
    const t = vx[j + 1] > vx[j] ? (x - vx[j]) / (vx[j + 1] - vx[j]) : 0;
    gx[i] = x; gz[i] = vy[j] + t * (vy[j + 1] - vy[j]);
  }
  return { gx, gz };
}
function hampelU(z, half, k) {
  const n = z.length, out = z.slice();
  for (let i = 0; i < n; i++) {
    const w = z.slice(Math.max(0, i - half), Math.min(n, i + half + 1));
    const m = median(w), mad = median(w.map(v => Math.abs(v - m)));
    if (mad > 0 && Math.abs(z[i] - m) > k * 1.4826 * mad) out[i] = m;
  }
  return out;
}
function sgU(z, half, degree) {
  const n = z.length, out = z.slice();
  for (let i = 0; i < n; i++) {
    const lo = Math.max(0, i - half), hi = Math.min(n - 1, i + half);
    const X = [], Y = [];
    for (let j = lo; j <= hi; j++) { X.push(j - i); Y.push(z[j]); }
    const v = polyCentre(X, Y, degree);
    if (v != null) out[i] = v;
  }
  return out;
}
function smoothGainLoss(z, deadband) {
  let gain = 0, loss = 0, ref = null;
  for (let i = 0; i < z.length; i++) {
    if (ref == null) { ref = z[i]; continue; }
    const d = z[i] - ref;
    if (d > deadband) { gain += d; ref = z[i]; }
    else if (d < -deadband) { loss += -d; ref = z[i]; }
  }
  return { gain, loss };
}
function rawGainLoss(ys) {
  let gain = 0, loss = 0, prev = null;
  for (const y of ys) {
    if (y == null) continue;
    if (prev != null) { const d = y - prev; if (d > 0) gain += d; else loss -= d; }
    prev = y;
  }
  return { gain, loss };
}

function showProfile(id) {
  const f = layers[id].feature;
  profile.style.display = 'block';
  profsvg._data = null;
  if (pinMarker) { map.removeLayer(pinMarker); pinMarker = null; }
  // Flatten segments but record where each begins: distance and elevation gain
  // must never bridge the gap between disjoint track segments (a multi-<trkseg>
  // track would otherwise gain phantom km + elevation across the connector).
  const pts = [], xs = [], ys = [], segStart = [];
  let dist = 0, hasEle = false, eMin = Infinity, eMax = -Infinity;
  for (const seg of f.geometry.coordinates) {
    for (let k = 0; k < seg.length; k++) {
      const c = seg[k];
      if (pts.length && k > 0) dist += haversine(pts[pts.length - 1], c);
      segStart.push(k === 0);
      pts.push(c); xs.push(dist);
      const e = c.length > 2 ? c[2] : null;
      ys.push(e);
      if (e != null) { hasEle = true; if (e < eMin) eMin = e; if (e > eMax) eMax = e; }
    }
  }
  const n = pts.length;
  if (!hasEle) {
    profhead.textContent = f.properties.name + ' — no elevation data';
    smoothbox.style.display = 'none';
    profsvg.innerHTML = '';
    return;
  }
  smoothbox.style.display = '';
  // Per-segment ranges over the flattened arrays.
  const ranges = [];
  let s = 0;
  for (let i = 1; i < n; i++) if (segStart[i]) { ranges.push([s, i]); s = i; }
  ranges.push([s, n]);
  // Gain/loss and smoothing are computed per segment and summed, so the gap
  // between segments contributes neither distance nor elevation change.
  let rawGain = 0, rawLoss = 0, smoothGain = 0, smoothLoss = 0;
  const smoothSegs = [];
  for (const [a, b] of ranges) {
    const rg = rawGainLoss(ys.slice(a, b));
    rawGain += rg.gain; rawLoss += rg.loss;
    const off = xs[a], segXs = [], segYs = [];
    for (let j = a; j < b; j++) { segXs.push(xs[j] - off); segYs.push(ys[j]); }
    const rs = resampleUniform(segXs, segYs, STEP_M);
    if (!rs) continue;
    const zz = sgU(hampelU(rs.gz, HAMPEL_HALF, HAMPEL_K), SG_HALF, SG_DEGREE);
    smoothSegs.push({ gx: rs.gx.map(x => x + off), gz: zz });
    const sg = smoothGainLoss(zz, GAIN_DEADBAND_M);
    smoothGain += sg.gain; smoothLoss += sg.loss;
  }
  profsvg._data = { xs, ys, pts, n, segStart, total: dist || 1, dist, eMin, eMax,
                    name: f.properties.name, smoothSegs,
                    rawGain, rawLoss, smoothGain, smoothLoss,
                    cursor: null, pinline: null, probe: null, pin: null, probeIdx: null };
  drawProfile();
}

function drawProfile() {
  const data = profsvg._data;
  if (!data) return;
  const { xs, ys, segStart, total, dist, eMin, eMax, name, smoothSegs } = data;
  const range = (eMax - eMin) || 1;
  const py = e => H - pad - ((e - eMin) / range) * (H - 2 * pad);
  let gain, loss, fill_d = '', stroke_d = '';
  if (smoothOn && smoothSegs.length) {
    gain = data.smoothGain; loss = data.smoothLoss;
    let sFirst = true;
    for (const sg of smoothSegs) {
      let x0, x1;
      for (let i = 0; i < sg.gx.length; i++) {
        const sx = (sg.gx[i] / total * W).toFixed(1), sy = py(sg.gz[i]).toFixed(1);
        fill_d += (i ? ' L ' : ' M ') + sx + ' ' + sy;
        stroke_d += (sFirst ? ' M ' : ' L ') + sx + ' ' + sy;
        if (i === 0) { x0 = sx; sFirst = false; }
        x1 = sx;
      }
      if (x0 !== undefined) fill_d += ` L ${x1} ${H} L ${x0} ${H} Z `;
    }
  } else {
    gain = data.rawGain; loss = data.rawLoss;
    let pen = false, rx0 = null, prevX = null;
    for (let i = 0; i < ys.length; i++) {
      if (ys[i] == null || (segStart[i] && pen)) {
        if (pen) { fill_d += ` L ${prevX} ${H} L ${rx0} ${H} Z `; pen = false; }
        if (ys[i] == null) continue;
      }
      const cx = (xs[i] / total * W).toFixed(1), cy = py(ys[i]).toFixed(1);
      fill_d += pen ? ` L ${cx} ${cy}` : ` M ${cx} ${cy}`;
      stroke_d += (pen || rx0 !== null) ? ` L ${cx} ${cy}` : ` M ${cx} ${cy}`;
      if (!pen) rx0 = cx;
      prevX = cx; pen = true;
    }
    if (pen) fill_d += ` L ${prevX} ${H} L ${rx0} ${H} Z`;
  }
  const fmt = x => Math.round(x).toLocaleString();
  profhead.innerHTML =
    `<b>${esc(name)}</b> — ${(dist / 1000).toFixed(2)} km · ↑${fmt(gain)} m ↓${fmt(loss)} m` +
    ` · min ${fmt(eMin)} / max ${fmt(eMax)} m<span id="probe"></span>`;
  profsvg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  profsvg.innerHTML =
    `<path d="${fill_d}" fill="#1f6feb22" stroke="none"/>` +
    `<path d="${stroke_d}" fill="none" stroke="#1f6feb" stroke-width="1.2"/>` +
    `<line id="cursor" x1="0" y1="0" x2="0" y2="${H}" stroke="#e8590c" stroke-width="1" style="display:none"/>` +
    `<line id="pinline" x1="0" y1="0" x2="0" y2="${H}" stroke="#c92a2a" stroke-width="1.2" stroke-dasharray="3 2" style="display:none"/>`;
  data.cursor = profsvg.querySelector('#cursor');
  data.pinline = profsvg.querySelector('#pinline');
  data.probe = document.getElementById('probe');
  if (data.pin != null && data.ys[data.pin] != null) {
    const x = (data.xs[data.pin] / total) * W;
    data.pinline.setAttribute('x1', x); data.pinline.setAttribute('x2', x);
    data.pinline.style.display = '';
  }
  const pi = data.probeIdx;
  if (pi != null && data.ys[pi] != null)
    data.probe.textContent = `  ${(data.xs[pi] / 1000).toFixed(2)} km · ${Math.round(data.ys[pi])} m`;
}

smoothcb.addEventListener('change', () => { smoothOn = smoothcb.checked; drawProfile(); });

// Pointer over the profile -> nearest point by distance (x-axis is distance).
function profIndex(e) {
  const data = profsvg._data;
  if (!data) return null;
  const rect = profsvg.getBoundingClientRect();
  const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
  const target = frac * data.total;
  let i = 0;
  while (i < data.n - 1 && data.xs[i + 1] < target) i++;
  if (i < data.n - 1 && target - data.xs[i] > data.xs[i + 1] - target) i++;
  if (data.ys[i] == null) {
    let j = i; while (j < data.n && data.ys[j] == null) j++;
    if (j < data.n) i = j; else return null;
  }
  return i;
}

profsvg.addEventListener('mousemove', e => {
  const data = profsvg._data;
  const i = profIndex(e);
  if (i == null) return;
  const x = (data.xs[i] / data.total) * W;
  data.cursor.setAttribute('x1', x); data.cursor.setAttribute('x2', x);
  data.cursor.style.display = '';
  data.probeIdx = i;
  data.probe.textContent = `  ${(data.xs[i] / 1000).toFixed(2)} km · ${Math.round(data.ys[i])} m`;
  const ll = [data.pts[i][1], data.pts[i][0]];
  if (!hoverMarker)
    hoverMarker = L.circleMarker(ll, { radius: 5, color: '#e8590c', weight: 2, fillColor: '#fff', fillOpacity: 1 }).addTo(map);
  else hoverMarker.setLatLng(ll);
});

// Pin the elevation line + map marker at point index i (shared by profile
// clicks and map-track clicks, so the two stay in sync).
function pinAt(i) {
  const data = profsvg._data;
  if (!data || i == null || data.ys[i] == null) return;
  data.pin = i;
  data.probeIdx = i;
  const x = (data.xs[i] / data.total) * W;
  data.pinline.setAttribute('x1', x); data.pinline.setAttribute('x2', x);
  data.pinline.style.display = '';
  const ll = [data.pts[i][1], data.pts[i][0]];
  const label = `${(data.xs[i] / 1000).toFixed(2)} km · ${Math.round(data.ys[i])} m`;
  data.probe.textContent = '  ' + label;
  if (!pinMarker)
    pinMarker = L.circleMarker(ll, { radius: 6, color: '#c92a2a', weight: 2, fillColor: '#ff6b6b', fillOpacity: 1 }).addTo(map);
  else pinMarker.setLatLng(ll);
  pinMarker.bindPopup(label).openPopup();
}

// Map a clicked map location back to the nearest track point index.
function nearestIndexToLatLng(ll) {
  const data = profsvg._data;
  if (!data) return null;
  let best = null, bestD = Infinity;
  for (let i = 0; i < data.n; i++) {
    if (data.ys[i] == null) continue;
    const dx = data.pts[i][0] - ll.lng, dy = data.pts[i][1] - ll.lat;
    const d = dx * dx + dy * dy;
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}

profsvg.addEventListener('click', e => {
  const i = profIndex(e);
  if (i == null) return;
  pinAt(i);
  map.panTo([profsvg._data.pts[i][1], profsvg._data.pts[i][0]]);
});

profsvg.addEventListener('mouseleave', () => {
  if (hoverMarker) { map.removeLayer(hoverMarker); hoverMarker = null; }
  if (profsvg._data && profsvg._data.cursor) profsvg._data.cursor.style.display = 'none';
});

// --- list -------------------------------------------------------------------
const list = document.getElementById('list');
const feats = [...DATA_TRACKS.features, ...DATA_WAYPOINTS.features];
// Only tag kinds when the set is mixed; a single-kind set (e.g. all tracks)
// would just repeat the same label on every row.
const showTags = new Set(feats.map(f => f.properties.kind)).size > 1;
// Waypoints from the same file collapse into one list entry (the whole
// collection); clicking it fits all its markers. Tracks/routes stay individual.
const listModel = [];
const groups = {};
for (const f of feats) {
  const p = f.properties;
  if (p.kind === 'waypoint') {
    let g = groups[p.file];
    if (!g) {
      g = groups[p.file] = { key: 'wptgrp:' + p.file, group: true,
                             name: p.group || p.file, kind: 'waypoint', ids: [] };
      listModel.push(g);
    }
    g.ids.push(p.id);
  } else {
    listModel.push({ key: p.id, group: false, name: p.name, kind: p.kind });
  }
}
listModel.sort((a, b) => a.name.localeCompare(b.name));
function render(filter) {
  list.replaceChildren();
  const frag = document.createDocumentFragment();
  for (const e of listModel) {
    if (filter && !e.name.toLowerCase().includes(filter)) continue;
    const el = document.createElement('div');
    el.className = 'item';
    el.dataset.key = e.key;
    // Re-render drops the DOM, so restore the highlight from current state.
    if (e.group ? (selectedGroup && e.key === selectedGroup.key) : (e.key === selected))
      el.classList.add('sel');
    if (showTags && e.kind !== 'track') {
      const tag = document.createElement('span');
      tag.className = 'tag ' + e.kind;
      tag.textContent = e.kind === 'waypoint' ? 'wpt' : e.kind;
      el.append(tag, document.createTextNode(e.name));
    } else {
      el.textContent = e.name;
    }
    el.onclick = () => e.group ? selectGroup(e, true) : select(e.key, true);
    frag.appendChild(el);
  }
  list.appendChild(frag);
}
render('');
document.getElementById('q').addEventListener('input', e =>
  render(e.target.value.trim().toLowerCase()));

const toggle = document.getElementById('toggle');
const syncToggle = () =>
  toggle.textContent = document.body.classList.contains('collapsed') ? '☰' : '✕';
syncToggle();
toggle.addEventListener('click', () => {
  document.body.classList.toggle('collapsed');
  syncToggle();
  map.invalidateSize();
});
</script>
</body>
</html>
"""


def display_count(geojson: dict) -> int:
    """Sidebar rows: one per track/route, one per waypoint file (waypoints group by file)."""
    wpt_files: set[str] = set()
    lines = 0
    for f in geojson["features"]:
        if f["kind"] == "waypoint":
            wpt_files.add(f["file"])
        else:
            lines += 1
    return lines + len(wpt_files)


def render_page(geojson: dict) -> bytes:
    n = display_count(geojson)
    # Escape characters that could break out of the <script> context or be
    # reinterpreted as HTML (e.g. a GPX name containing "</script>"). These chars
    # only ever occur inside JSON string values, where \uXXXX escapes are valid.
    payload = (
        json.dumps(geojson, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    body = PAGE.replace("__COUNT__", str(n)).replace("__GEOJSON__", payload)
    return body.encode("utf-8")


def make_handler(html: bytes, gz_html: bytes) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — name fixed by BaseHTTPRequestHandler API
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
            payload = gz_html if accepts_gzip else html
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if accepts_gzip:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # quiet
            pass

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Inspect any GPX file(s) on a Leaflet map."
    )
    ap.add_argument(
        "input", nargs="?", default=".", help="GPX file or directory (default: .)"
    )
    ap.add_argument("-p", "--port", type=int, default=8000, help="port (default: 8000)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser")
    ap.add_argument(
        "-o",
        "--out",
        metavar="FILE",
        help="write a standalone HTML file and exit (no server)",
    )
    ap.add_argument(
        "--precision",
        type=int,
        default=6,
        help="decimal places for coordinates; -1 for full precision (default: 6)",
    )
    ap.add_argument(
        "--simplify",
        type=float,
        default=3.0,
        metavar="METRES",
        help="Ramer-Douglas-Peucker tolerance in metres; 0 disables (default: 3)",
    )
    args = ap.parse_args()

    path = Path(args.input)
    if path.is_dir():
        inputs = sorted(
            p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".gpx"
        )
    elif path.is_file():
        inputs = [path]
    else:
        print(f"error: no such file or directory: {path}", file=sys.stderr)
        return 1
    if not inputs:
        print(f"no .gpx files in {path}", file=sys.stderr)
        return 1

    precision = None if args.precision < 0 else args.precision
    geojson = build_geojson(inputs, precision, args.simplify)
    n = display_count(geojson)
    if not n:
        print(f"no tracks, routes, or waypoints found in {path}", file=sys.stderr)
        return 1
    html = render_page(geojson)

    if args.out:
        Path(args.out).write_bytes(html)
        print(
            f"wrote {n} items to {args.out} ({len(html) / 1024:.0f} KB) — open with file://"
        )
        return 0

    gz_html = gzip.compress(html, compresslevel=6)
    url = f"http://localhost:{args.port}/"
    try:
        server = ThreadingHTTPServer(
            ("127.0.0.1", args.port), make_handler(html, gz_html)
        )
    except OSError as exc:
        print(f"error: cannot serve on port {args.port}: {exc}", file=sys.stderr)
        return 1
    print(f"serving {n} items from {path} at {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
