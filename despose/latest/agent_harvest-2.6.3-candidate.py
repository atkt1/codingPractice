"""Deterministic browser toolbox for the field automation agent (v2.6.3).

Location: commit as scripts/agent_harvest.py in d365-test-automation
(next to app_cdp_connect.py). The agent REQUIRES this file — there is no
embedded-JS fallback.

Connects to the persistent Chrome over CDP (localhost:9222), targets the D365
tab by EXACT hostname (never guesses, host is not overridable), runs ONE
action, prints ONE JSON object to stdout, never navigates on its own, never
opens/closes tabs, never closes the browser.

Architecture: the poll loop, frame targeting, and every click / type / select
use NATIVE Playwright actions (trusted input, built-in actionability:
attached, visible, stable, enabled, not occluded). JavaScript is only used to
READ the page (harvest, readiness, option enumeration).

Commands (always from the repo root, always the venv):
  .venv/bin/python scripts/agent_harvest.py version
  .venv/bin/python scripts/agent_harvest.py scan "Life Events" "Add Goal"
  .venv/bin/python scripts/agent_harvest.py scan                          # readiness only
  .venv/bin/python scripts/agent_harvest.py probe "[data-id='x'][aria-label='Category']"
  .venv/bin/python scripts/agent_harvest.py click "[data-id='tablist-tab_lifeevents']"
  .venv/bin/python scripts/agent_harvest.py fill "[data-id='name.fieldControl'] input" "My appointment"
  .venv/bin/python scripts/agent_harvest.py press Escape
  .venv/bin/python scripts/agent_harvest.py state "[data-id='category.fieldControl']"
  .venv/bin/python scripts/agent_harvest.py pick "[data-id='category.fieldControl']" "Retirement"
  .venv/bin/python scripts/agent_harvest.py pick "[data-id='contact.fieldControl']" "Test ClientB" --type "Test Client"

Common options: --frame <name-or-url-substring> to target one iframe.
scan options: --quiet-ms (default 2000), --load-ms (default 15000).

The nav commands read/extend field/test_data/navigation_paths.yaml and need no
browser at all (they never connect to CDP):
  .venv/bin/python scripts/agent_harvest.py nav list
  .venv/bin/python scripts/agent_harvest.py nav add --feature "Life Events / Goals" \
      --path "Contact record → Life Events & Goals tab" \
      --anchor "field/actions/life_events_actions.py" --aliases "life event,goal" \
      --source exploration
Append-only and deduped by feature: an identical row reports ALREADY_PRESENT, a
different path for a known feature reports PATH_CONFLICT and changes nothing.

Every output JSON has "action" and "status":
  ok            action ran and its claim holds
  failed        action could not be performed (code says why)
  ambiguous     more than one candidate — refine, don't guess
  indeterminate action dispatched but the page navigated mid-flight;
                verify the expected effect with scan before deciding
Exit codes: 0 = a result JSON was produced (any status), 1 = infra failure
(bad args, CDP down, no/ambiguous target tab) — stdout still carries JSON.

"Execution context was destroyed" during an action means the page NAVIGATED
while the JS ran — reported as status indeterminate; run scan next and check
the expected effect before retrying.
"""

import argparse
import json
import pathlib
import re
import signal
import sys
import time
from datetime import date
from urllib.parse import urlparse

# `version` and `nav` never touch the browser and must keep working when Playwright is
# unusable. A top-level import raised before argument parsing, so a broken install produced
# a traceback and NO JSON at all — breaking the one-JSON-per-invocation contract outright.
try:
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeoutError
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_IMPORT_ERROR = None
except Exception as _import_exc:   # noqa: BLE001 - any import failure must stay recoverable
    class PWError(Exception):
        """Stand-in so `except PWError` still parses; never raised without Playwright."""

    class PWTimeoutError(PWError):
        """Stand-in for the Playwright timeout type."""

    sync_playwright = None
    PLAYWRIGHT_IMPORT_ERROR = str(_import_exc)

VERSION = "2.6.3"
PER_TERM_VIS_CAP = 8        # visible matches kept per term, per frame
PER_TERM_HID_CAP = 2        # hidden matches kept per term, per frame (unchanged from 2.3.0)
GLOBAL_MATCH_CAP = 40       # page-wide ceiling, applied after frames are merged
STARTUP_BUDGET_S = 30       # watchdog for Playwright driver startup, before any page work
NAV_PATHS_FILE = "field/test_data/navigation_paths.yaml"
CDP_URL = "http://localhost:9222"
TARGET_HOST = "fieldcrm-test3.crm.dynamics.com"
CDP_TIMEOUT_MS = 5000
ACTION_TIMEOUT_MS = 3000
LOGIN_URL_RE = re.compile(r"login\.microsoftonline\.com|login\.live\.com|adfs|/signin", re.I)
GENERATED_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}|_\d{4,}|[A-Za-z]\d{6,}")

# ---------------------------------------------------------------- JS (read-only)

# Readiness of ONE document (no iframe recursion — Python walks frames).
READY_JS = r"""
() => ({
  readyState: document.readyState,
  hasPassword: !!document.querySelector('input[type="password"]'),
  busySpinner: ['[aria-busy="true"]', '[role="progressbar"]', '.ms-crm-Loading', '.pa-loading'].some(sel =>
    [...document.querySelectorAll(sel)].some(el => {
      const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0;
    }))
})
"""

# Harvest ONE document. Matching is EXACT or narrowly DECORATED — never substring: a
# substring test made `scan "Task"` return four left-nav items that merely CONTAIN the word,
# report termsFound, and let the agent click the wrong control. Broad containers still match
# on their OWN attributes only (2.3.0's proven guard — their textContent is the whole subtree).
# Per-term caps are filled by round-robin over (region, family) buckets so a crowded grid
# cannot starve the command bar. Selectors are built for survivors only.
HARVEST_JS = r"""
(({ terms, perTermCap, hiddenCap }) => {
  const NBSP = /\u00a0/g;
  const norm = v => String(v == null ? '' : v).replace(NBSP, ' ').replace(/\s+/g, ' ').trim().toLowerCase();
  // A trailing parenthetical is decoration ONLY when it is SHAPED like a keyboard shortcut:
  // modifiers joined by '+' then a key, or a bare function key. Matching the modifier letters
  // anywhere inside the parens would turn 'Delete (all shifts)' into a match for 'Delete' —
  // substring matching through the back door.
  const SHORTCUT = /\s*\((?:(?:ctrl|alt|shift|cmd|command|option|win|meta)\s*\+\s*)+[^)\s+]{1,14}\)\s*$|\s*\(f[0-9]{1,2}\)\s*$/i;
  // Punctuation is stripped on BOTH sides of the shortcut: one order alone loses either
  // 'Save... (Ctrl+S)' or 'Save (Ctrl+S).'.
  const PUNCT = /[.!:;\u2026]+$/;
  const canon = v => norm(v).replace(PUNCT, '').replace(SHORTCUT, '').replace(PUNCT, '').trim();

  // A value matches a term when norm(value) === norm(term) OR canon(value) === canon(term).
  // The two lookups are UNIONED, never exclusive. With terms ['Save', 'Save (Ctrl+S)'] and
  // <button aria-label="Save (Ctrl+S)">, an exclusive test satisfies the decorated term and
  // reports the plain one MISSING. Values are Sets because one DOM value can satisfy several
  // requested terms at once.
  const EXACT = new Map(), CANON = new Map();
  const put = (m, k, v) => { if (!k) return; const s = m.get(k); if (s) s.add(v); else m.set(k, new Set([v])); };
  const keys = [];
  let maxLen = 0;
  for (const raw of terms) {
    const k = norm(raw);
    if (!k || keys.indexOf(k) !== -1) continue;
    keys.push(k);
    put(EXACT, k, k);
    // Decoration is stripped from the DOM's label, never from the caller's term. Only an
    // UNDECORATED term joins the canonical map, so `Save` still finds `Save (Ctrl+S)` while an
    // explicitly requested `Save (Ctrl+S)` never matches a plain `Save` — the more specific
    // term stays the more specific query.
    const ck = canon(raw);
    if (ck === k) put(CANON, ck, k);
    if (k.length > maxLen) maxLen = k.length;
  }
  const stats = {};
  for (const k of keys) stats[k] = { total: 0, hidden: 0 };
  if (!keys.length) return { matches: [], stats };

  const lookup = v => {
    const found = new Map();
    const n = norm(v);
    if (!n) return found;
    const e = EXACT.get(n);
    if (e) for (const k of e) found.set(k, 'exact');
    const cv = canon(v);
    if (cv !== n) {          // only a DECORATED value falls back to the canonical map
      const c = CANON.get(cv);
      if (c) for (const k of c) if (!found.has(k)) found.set(k, 'decorated');
    }
    return found;
  };

  const QUERY = 'button,a,input,textarea,select,label,li,[role],[aria-label],[data-id],' +
    '[data-testid],[data-automation-id],[data-automationid],[title],[placeholder]';
  // BOTH automation-id spellings occur in D365 and the agent file promises scan reports the
  // real one. Collecting only the hyphenated form made an unhyphenated control unfindable.
  const ATTRS = ['data-testid', 'data-automation-id', 'data-automationid', 'data-id', 'aria-label',
    'name', 'placeholder', 'title'];
  const BROAD = new Set(['main','application','document','form','region','tabpanel','grid','treegrid','rowgroup',
    'group','presentation','none','list','table','toolbar','navigation','complementary','banner','contentinfo',
    'tree','listbox','menu','menubar','tablist','dialog','alertdialog','radiogroup','search','feed','article']);
  const esc = v => String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"');

  // Nearest landmark wins — a single upward walk, not one closest() per region. Testing
  // 'command' before 'overlay' would file a submenu inside a menubar as command.
  const LANDMARK = { toolbar: 'command', menubar: 'command', menu: 'overlay', listbox: 'overlay',
    dialog: 'overlay', alertdialog: 'overlay', navigation: 'navigation', tree: 'navigation',
    grid: 'grid', treegrid: 'grid', rowgroup: 'grid', form: 'form' };
  const regionOf = el => {
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const rl = ((n.getAttribute && n.getAttribute('role')) || '').toLowerCase();
      if (rl && LANDMARK[rl]) return LANDMARK[rl];
      const tg = n.tagName ? n.tagName.toLowerCase() : '';
      if (tg === 'nav') return 'navigation';
      if (tg === 'table') return 'grid';
      if (tg === 'form') return 'form';
    }
    return 'content';
  };
  const ACTION_ROLES = new Set(['button','menuitem','menuitemcheckbox','menuitemradio','tab','link',
    'option','treeitem','switch','checkbox','radio']);
  const FIELD_ROLES = new Set(['textbox','combobox','searchbox','spinbutton','slider','listbox']);
  const DATA_ROLES = new Set(['gridcell','cell','row','rowheader','columnheader']);
  const familyOf = (role, tag) => {
    const r = (role || '').toLowerCase();
    if (ACTION_ROLES.has(r)) return 'action';
    if (FIELD_ROLES.has(r)) return 'field';
    if (DATA_ROLES.has(r)) return 'data';
    if (tag === 'button' || tag === 'a') return 'action';
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return 'field';
    if (tag === 'li' || tag === 'label') return 'wrapper';
    return 'other';
  };

  // PASS 1 — match only. No layout reads, no selector queries, no occlusion for non-matches.
  const cand = [];
  const DECOR_ALLOWANCE = 64;   // room for a trailing shortcut/punctuation on top of the term
  const RAW_CEILING = 4096;     // never serialise a pathological subtree just to test it
  const MAX_KIDS = 12;          // an icon plus a caption is a control; 50 children is a container
  let nodes = [];
  try { nodes = [...document.querySelectorAll(QUERY)]; } catch (e) { return { matches: [], stats }; }
  for (const el of nodes) {
    const attrs = {};
    for (const a of ATTRS) { const v = el.getAttribute(a); if (v) attrs[a] = v; }
    if (el.id) attrs.id = el.id;
    const role = el.getAttribute('role');
    const tag = el.tagName.toLowerCase();
    const interactiveTag = ['button','a','input','textarea','select','label','li'].indexOf(tag) !== -1;
    const isBroad = !!role && BROAD.has(role) && !interactiveTag;
    const hits = new Map();
    const take = (k, kind, source) => {
      const prev = hits.get(k);
      if (!prev || (prev.kind === 'decorated' && kind === 'exact')) hits.set(k, { kind, source });
    };
    for (const a of Object.keys(attrs)) for (const [k, kind] of lookup(attrs[a])) take(k, kind, a);
    let rawText = '';
    if (!isBroad) {
      rawText = (el.textContent || '').replace(NBSP, ' ').replace(/\s+/g, ' ').trim();
      if (rawText) {
        // textContent concatenates display:none descendants, so <button>Task<span
        // style="display:none"> archived</span></button> answers to 'Task archived' (a false
        // VISIBLE match) and simultaneously fails to answer to 'Task' (a false miss). innerText
        // is the RENDERED text and settles both. Read it only for small composite controls, and
        // bound the raw read so a pathological subtree is never serialised just to be tested.
        let useText = rawText;
        if (el.children && el.children.length && el.children.length <= MAX_KIDS && rawText.length <= RAW_CEILING) {
          let shown = null;
          try { shown = (el.innerText || '').replace(NBSP, ' ').replace(/\s+/g, ' ').trim(); } catch (e) { shown = null; }
          // An empty innerText means the element is not rendered at all; for those, textContent
          // is the only evidence there is, and the hidden channel still wants it.
          if (shown) useText = shown;
        }
        // The bound applies to the text actually TESTED, with room for decoration: `Go` must
        // still reach `Go (Ctrl+Shift+Alt+G)`, and one long hidden descendant must not hide a
        // short visible label.
        if (useText.length <= maxLen + DECOR_ALLOWANCE) {
          for (const [k, kind] of lookup(useText)) take(k, kind, 'text');
        }
      }
    }
    if (!hits.size) continue;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
    for (const k of hits.keys()) { if (visible) stats[k].total++; else stats[k].hidden++; }
    cand.push({ el, attrs, role, tag, hits, visible, r, st,
      region: regionOf(el), family: familyOf(role, tag),
      preview: (Object.keys(attrs).map(a => attrs[a]).join(' ') + ' ' + rawText).replace(/\s+/g, ' ').trim().slice(0, 80) });
  }

  // PASS 2 — cap per term. Bucket KINDS are visited in a fixed order, never document order:
  // D365 renders the site map before the command bar, so document order starves the command
  // bar whenever there are more buckets than slots. This orders bucket kinds and guarantees
  // each one a slot; it never scores individual elements against each other.
  const FAM_RANK = { action: 0, field: 1, data: 2, wrapper: 3, other: 4 };
  const REG_RANK = { command: 0, overlay: 1, form: 2, content: 3, grid: 4, navigation: 5 };
  const keep = new Set();
  for (const k of keys) {
    for (const wantVisible of [true, false]) {
      const cap = wantVisible ? perTermCap : hiddenCap;
      const pool = cand.filter(c => c.visible === wantVisible && c.hits.has(k));
      if (pool.length <= cap) { for (const c of pool) keep.add(c); continue; }
      const buckets = new Map();
      for (const c of pool) {
        const bk = c.region + '/' + c.family;
        const arr = buckets.get(bk); if (arr) arr.push(c); else buckets.set(bk, [c]);
      }
      const order = [...buckets.keys()].sort((x, y) => {
        const xs = x.split('/'), ys = y.split('/');
        return (FAM_RANK[xs[1]] - FAM_RANK[ys[1]]) || (REG_RANK[xs[0]] - REG_RANK[ys[0]]) || (x < y ? -1 : 1);
      });
      let taken = 0;
      for (let round = 0; taken < cap; round++) {
        let added = false;
        for (const bk of order) {
          const arr = buckets.get(bk);
          if (round < arr.length && taken < cap) { keep.add(arr[round]); taken++; added = true; }
        }
        if (!added) break;
      }
    }
  }

  // PASS 3 — survivors only: occlusion, then candidate selectors and their uniqueness counts.
  // On a grid with 145 exact cells this runs for ~8 elements instead of 145.
  const out = [];
  for (const c of cand) {
    if (!keep.has(c)) continue;
    const enabled = !c.el.disabled && c.el.getAttribute('aria-disabled') !== 'true' && c.st.pointerEvents !== 'none';
    let occluded = null, coveredBy = null;
    if (c.visible) {
      const cx = c.r.left + c.r.width / 2, cy = c.r.top + c.r.height / 2;
      if (cx >= 0 && cy >= 0 && cx <= innerWidth && cy <= innerHeight) {
        const top = document.elementFromPoint(cx, cy);
        occluded = !!(top && top !== c.el && !c.el.contains(top) && !top.contains(c.el));
        if (occluded) coveredBy = (top.tagName || '').toLowerCase() +
          (top.className && typeof top.className === 'string' ? '.' + top.className.split(/\s+/)[0] : '');
      }
    }
    const sels = [];
    for (const a of Object.keys(c.attrs)) {
      const v = c.attrs[a];
      if (/[\n\r\t]/.test(v)) continue;
      const s = a === 'id' ? `#${CSS.escape(v)}` : `[${a}="${esc(v)}"]`;
      let n = -1; try { n = document.querySelectorAll(s).length; } catch (e) { continue; }
      sels.push({ attr: a, value: v, sel: s, n });
    }
    sels.sort((x, y) => (x.n === 1 ? 0 : 1) - (y.n === 1 ? 0 : 1));
    const matched = [...c.hits.keys()];
    out.push({ tag: c.tag, role: c.role, text: c.preview, visible: c.visible, enabled, occluded, coveredBy,
      actionable: c.visible && enabled && occluded !== true, matched,
      matchedBy: Object.fromEntries(matched.map(k => [k, c.hits.get(k)])),
      region: c.region, family: c.family, attrs: c.attrs, sels: sels.slice(0, 4) });
  }
  return { matches: out, stats };
})
"""

PROBE_JS = r"""
((selector) => {
  let nodes = [];
  try { nodes = [...document.querySelectorAll(selector)]; } catch (e) { return { invalid: true }; }
  let visibleCount = 0, actionableCount = 0;
  const sample = [];
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
    const enabled = !el.disabled && el.getAttribute('aria-disabled') !== 'true' && st.pointerEvents !== 'none';
    let occluded = null;
    if (visible) {
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      if (cx >= 0 && cy >= 0 && cx <= innerWidth && cy <= innerHeight) {
        const top = document.elementFromPoint(cx, cy);
        occluded = !!(top && top !== el && !el.contains(top) && !top.contains(el));
      }
    }
    if (visible) visibleCount++;
    if (visible && enabled && occluded !== true) actionableCount++;
    if (sample.length < 5) sample.push({ tag: el.tagName.toLowerCase(),
      text: (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 60), visible, enabled, occluded });
  }
  return { invalid: false, count: nodes.length, visibleCount, actionableCount, sample };
})
"""

STATE_JS = r"""
((selector) => {
  const el = document.querySelector(selector);
  if (!el) return { found: false };
  const out = { found: true,
    value: el.value != null ? String(el.value).slice(0, 120) : null,
    ariaLabel: el.getAttribute('aria-label'),
    title: el.getAttribute('title'),
    text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120),
    innerValue: null };
  const inner = el.querySelector ? el.querySelector('input,textarea') : null;
  if (inner && inner.value != null) out.innerValue = String(inner.value).slice(0, 120);
  if (el.tagName === 'SELECT') {
    out.options = [...el.options].map(o => ({
      label: o.label, value: o.value, selected: o.selected, disabled: o.disabled })).slice(0, 50);
  }
  return out;
})
"""

# Tag currently-open flyout containers as pre-existing (run BEFORE opening).
PICK_BASELINE_JS = r"""
(() => {
  const CONTAINERS = '[role="listbox"],[role="menu"],[role="tree"],#__flyoutRootNode,' +
    '.ms-Layer,[data-id$="-menu"],[data-id*="Lookup"],ul[aria-label]';
  // Clear markers a previous run could not clean up (watchdog kill) before tagging, so a
  // stale marker is never read as "this container was already open".
  for (const s of document.querySelectorAll('[data-agent-harvest-seen],[data-agent-harvest-target]')) {
    s.removeAttribute('data-agent-harvest-seen');
    s.removeAttribute('data-agent-harvest-target');
  }
  let tagged = 0;
  for (const c of document.querySelectorAll(CONTAINERS)) {
    const r = c.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) { c.setAttribute('data-agent-harvest-seen', '1'); tagged++; }
  }
  return tagged;
})
"""

# Enumerate options tier-by-tier. mark:false = report only (for cross-frame
# ambiguity aggregation in Python); mark:true = tag the decided option for a
# native click and return its attribute evidence (for OPTION_TEMPLATE records).
PICK_COLLECT_JS = r"""
(({ want, triggerSelector, mark }) => {
  // Strip any stale markers first (a killed previous run may have left them).
  for (const el of document.querySelectorAll('[data-agent-harvest-target]')) el.removeAttribute('data-agent-harvest-target');
  const WANT = String(want).toLowerCase().trim();
  const ITEMS = '[role="option"],[role="menuitem"],[role="menuitemcheckbox"],' +
    '[role="menuitemradio"],[role="treeitem"],li[data-id]';
  const CONTAINERS = '[role="listbox"],[role="menu"],[role="tree"],#__flyoutRootNode,' +
    '.ms-Layer,[data-id$="-menu"],[data-id*="Lookup"],ul[aria-label]';
  const esc = v => String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"');

  const visibleContainers = () => [...document.querySelectorAll(CONTAINERS)].filter(c => {
    const r = c.getBoundingClientRect(); return r.width > 0 && r.height > 0;
  });

  const collect = (containers) => {
    const found = [];
    const seen = new Set();
    for (const c of containers) {
      for (const it of c.querySelectorAll(ITEMS)) {
        if (seen.has(it)) continue;
        seen.add(it);
        const r = it.getBoundingClientRect();
        const st = getComputedStyle(it);
        if (!(r.width > 0 && r.height > 0) || st.visibility === 'hidden') continue;
        if (it.getAttribute('aria-disabled') === 'true') continue;
        const text = (it.textContent || '').replace(/\s+/g, ' ').trim();
        const label = it.getAttribute('aria-label') || it.getAttribute('title') || '';
        found.push({ el: it, text: text.slice(0, 100), label,
          exact: text.toLowerCase() === WANT || label.toLowerCase() === WANT,
          meta: { tag: it.tagName.toLowerCase(), role: it.getAttribute('role'),
            title: it.getAttribute('title'), ariaLabel: it.getAttribute('aria-label'),
            dataId: it.getAttribute('data-id') } });
      }
    }
    return found;
  };

  const trigger = (() => { try { return document.querySelector(triggerSelector); } catch (e) { return null; } })();
  const ownedIds = new Set();
  if (trigger) {
    for (const el of [trigger, ...trigger.querySelectorAll('[aria-owns],[aria-controls]')]) {
      for (const a of ['aria-owns', 'aria-controls']) {
        const v = el.getAttribute(a);
        if (v) v.split(/\s+/).forEach(id => ownedIds.add(id));
      }
    }
  }
  const isOwned = (c) => {
    if (c.id && ownedIds.has(c.id)) return true;
    for (const id of ownedIds) {
      const owner = document.getElementById(id);
      if (owner && (owner === c || owner.contains(c) || c.contains(owner))) return true;
    }
    return false;
  };

  const now = visibleContainers();
  let searched = 'fresh';
  let items = collect(now.filter(c => !c.hasAttribute('data-agent-harvest-seen')));
  if (!items.length && ownedIds.size) { searched = 'owned'; items = collect(now.filter(isOwned)); }
  if (!items.length) { searched = 'all'; items = collect(now); }

  const exacts = items.filter(i => i.exact);
  const base = { searched, exactCount: exacts.length,
    optionsSeen: items.map(i => i.text || i.label).slice(0, 15) };
  if (!mark) return base;

  // EXACT ONLY. A lone substring match used to be clicked automatically, so `pick "Task"`
  // silently chose "Create Task to Send a birthday card". All visible option labels remain in
  // optionsSeen so the agent can correct the exact wording and retry.
  const target = exacts.length === 1 ? exacts[0] : null;
  if (!target) return { ...base, decided: false };

  target.el.setAttribute('data-agent-harvest-target', '1');
  const candidates = [];
  const attrPairs = [['title', target.meta.title], ['aria-label', target.meta.ariaLabel],
    ['data-id', target.meta.dataId]];
  for (const [attr, val] of attrPairs) {
    if (!val || /[\n\r\t]/.test(val)) continue;
    const rolePart = target.meta.role ? `[role="${esc(target.meta.role)}"]` : '';
    const sel = `${rolePart}[${attr}="${esc(val)}"]`;
    let n = -1; try { n = document.querySelectorAll(sel).length; } catch (e) { continue; }
    candidates.push({ attr, value: val, sel, n });
  }
  return { ...base, decided: true, picked: target.text || target.label,
    matchedBy: 'exact',
    option: { tag: target.meta.tag, role: target.meta.role, title: target.meta.title,
      ariaLabel: target.meta.ariaLabel, dataId: target.meta.dataId,
      text: target.text, candidates } };
})
"""

PICK_CLEANUP_JS = r"""
(() => {
  for (const el of document.querySelectorAll('[data-agent-harvest-seen]')) el.removeAttribute('data-agent-harvest-seen');
  for (const el of document.querySelectorAll('[data-agent-harvest-target]')) el.removeAttribute('data-agent-harvest-target');
})
"""

EXPANDED_JS = r"""
((selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const src = el.hasAttribute('aria-expanded') ? el : el.querySelector('[aria-expanded]');
  return src ? src.getAttribute('aria-expanded') : null;
})
"""

# ---------------------------------------------------------------- helpers

class WatchdogTimeout(Exception):
    """The browser stopped responding to protocol calls (wedged renderer)."""


def _watchdog_handler(signum, frame):
    raise WatchdogTimeout()


def emit(obj, exit_code=0):
    # default=str: PyYAML turns an unquoted `verified: 2026-08-14` into a datetime.date, which
    # json.dumps refuses — and the invocation then emitted nothing at all.
    # flush=True: the one-JSON contract must hold even if teardown later wedges.
    print(json.dumps(obj, ensure_ascii=False, default=str), flush=True)
    return exit_code


def is_context_destroyed(exc):
    return "execution context was destroyed" in str(exc).lower()


def is_page_mutation(exc):
    """Errors that mean the page/frame changed under us — indeterminate, not infra failure."""
    message = str(exc).lower()
    return "execution context was destroyed" in message or "frame was detached" in message


def nav_signature(url):
    """Navigation-meaningful identity of a D365 URL — ignores appid-style param churn
    that Unified Interface rewrites via history.replaceState without navigating."""
    parsed = urlparse(url)
    query = {}
    if parsed.query:
        for pair in parsed.query.split("&"):
            if "=" in pair:
                key, value = pair.split("=", 1)
                query[key.lower()] = value
    keys = ("pagetype", "etn", "id", "viewid", "entityname")
    return (parsed.hostname, parsed.path, tuple((k, query.get(k)) for k in keys))


def select_page(browser):
    pages = [p for c in browser.contexts for p in c.pages]
    if not pages:
        raise LookupError("NO_TARGET_PAGE: no pages open in the browser.")
    matches = [p for p in pages if (urlparse(p.url).hostname or "") == TARGET_HOST]
    if not matches:
        logins = [p for p in pages if LOGIN_URL_RE.search(p.url)]
        if len(logins) == 1:
            raise LookupError("LOGIN_PAGE: the D365 tab has redirected to the Microsoft login page — the session has expired.")
        origins = sorted({urlparse(p.url).hostname or p.url for p in pages})
        raise LookupError(f"NO_TARGET_PAGE: no tab with host {TARGET_HOST}. Open tabs: {origins}")
    if len(matches) == 1:
        return matches[0]
    # No /main.aspx tie-break. Picking one of several same-host tabs IS guessing, and the
    # tool's stated contract is that it refuses to guess which tab is the D365 session.
    urls = [p.url[:120] for p in matches]
    raise LookupError(f"AMBIGUOUS_TARGET_PAGE: {len(matches)} tabs match {TARGET_HOST}: {urls}. Close extras.")


def visible_frames(page):
    """Main frame + child frames whose iframe element has a visible box."""
    frames = [(page.main_frame, "main")]
    for fr in page.frames:
        if fr is page.main_frame:
            continue
        try:
            fe = fr.frame_element()
            box = fe.bounding_box()
            if not box or box["width"] == 0 or box["height"] == 0:
                continue
        except PWError as exc:
            # A torn-down frame cannot hold a live competing element, so skipping it is safe.
            # Any OTHER discovery failure must not silently shrink the frame set — that would
            # bypass the refuse-to-act guard. Let it surface as a typed RUNTIME_ERROR instead.
            msg = str(exc).lower()
            if "detached" in msg or "closed" in msg or "not attached" in msg:
                continue
            raise
        frames.append((fr, fr.name or fr.url or "iframe"))
    return frames


def frames_for_arg(page, frame_arg):
    if not frame_arg or frame_arg == "main":
        return [(page.main_frame, "main")]
    hits = []
    for fr in page.frames:
        if fr is page.main_frame:
            continue
        if frame_arg in (fr.name or "") or frame_arg in (fr.url or ""):
            hits.append((fr, fr.name or fr.url))
    return hits


def search_frames(page, frame_arg):
    """Frames to search a selector in: explicit --frame, else the same visible-frame
    universe scan harvests — hidden cached D365 iframes hold duplicate DOM and must
    not create phantom ambiguity between harvest time and act time."""
    if frame_arg:
        return frames_for_arg(page, frame_arg)
    return visible_frames(page)


def frame_chain(page, fr):
    """Selector chain from the top document to fr's iframe element — directly
    usable for nested page.frame_locator(...) calls in generated code."""
    chain = []
    cur = fr
    while cur is not page.main_frame:
        parent = cur.parent_frame
        if parent is None:
            break
        sel = None
        try:
            fe = cur.frame_element()
            for attr in ("data-id", "name", "id", "title"):
                value = fe.get_attribute(attr)
                if not value or GENERATED_ID_RE.search(value) or '"' in value:
                    continue
                cand = f'iframe[{attr}="{value}"]'
                if parent.locator(cand).count() == 1:
                    sel = cand
                    break
            if sel is None:
                src = fe.get_attribute("src") or ""
                tail = src.split("?")[0].rsplit("/", 1)[-1]
                if tail and '"' not in tail:
                    cand = f'iframe[src*="{tail}"]'
                    if parent.locator(cand).count() == 1:
                        sel = cand
        except PWError:
            pass
        chain.append(sel or "iframe")
        cur = parent
    chain.reverse()
    return chain


def locate_unique(page, selector, frame_arg, action):
    """Resolve selector to exactly one element in exactly one frame, or a result dict."""
    frames = search_frames(page, frame_arg)
    if frame_arg and not frames:
        return None, {"action": action, "status": "failed", "code": "FRAME_NOT_FOUND",
                      "error": f"no frame matches --frame {frame_arg!r} — re-check the frame value from the last scan"}
    hits = []
    skipped = []
    for fr, label in frames:
        try:
            n = fr.locator(selector).count()
        except PWError as exc:
            if is_context_destroyed(exc):
                raise
            if "selector" in str(exc).lower():
                return None, {"action": action, "status": "failed", "code": "INVALID_SELECTOR",
                              "error": f"selector does not parse: {selector[:120]}"}
            skipped.append(label)
            continue  # frame detached mid-walk — skip it, don't misreport the selector
        if n:
            hits.append((fr, label, n))
    if not hits:
        res = {"action": action, "status": "failed", "code": "NOT_FOUND",
               "note": "selector matched nothing in any frame — probably stale; re-harvest with scan"}
        if skipped:
            res["framesSkipped"] = skipped
            res["note"] = "selector not found, but some frames could not be inspected — re-scan rather than concluding absence"
        return None, res
    total = sum(n for _, _, n in hits)
    if total > 1:
        return None, {"action": action, "status": "ambiguous", "code": "AMBIGUOUS_SELECTOR",
                      "perFrame": [{"frame": lbl, "count": n} for _, lbl, n in hits],
                      "note": "refine the selector (probe candidate combinations) — never click an ambiguous selector"}
    fr, label, _ = hits[0]
    # One hit is not PROOF of uniqueness when a visible frame could not be inspected — the
    # count is a floor, not a fact. A READ may proceed with the fact disclosed; a MUTATION may
    # not, because a warning attached to an already-performed click is useless. Reuses the
    # existing ambiguous code: a routine frame race must not spend a run-ending strike.
    # 2.3.0 discarded this list entirely and acted in silence.
    if skipped and action in ("click", "fill", "press", "pick"):
        return None, {"action": action, "status": "ambiguous", "code": "AMBIGUOUS_SELECTOR",
                      "framesSkipped": skipped,
                      "perFrame": [{"frame": lbl, "count": n} for _, lbl, n in hits],
                      "note": "a visible frame could not be inspected, so this selector's uniqueness "
                              "is unproven — re-run once; if it repeats, add --frame or refine it"}
    return (fr, label, skipped), None


def readiness(page):
    url = page.url
    is_login = bool(LOGIN_URL_RE.search(url))
    busy = False
    for fr, _label in visible_frames(page):
        try:
            r = fr.evaluate(READY_JS)
        except PWError as exc:
            if is_context_destroyed(exc):
                raise
            continue
        if r["hasPassword"]:
            is_login = True
        if r["readyState"] != "complete" or r["busySpinner"]:
            busy = True
    return {"url": url, "isLogin": is_login, "busy": busy}


TERM_WS_RE = re.compile(r"\s+")


def norm_term(text):
    """Mirror of HARVEST_JS `norm()` — NBSP to space, whitespace collapsed, trimmed, lowered.

    Both sides key the same term map, so they MUST agree. Keying one side with .lower().strip()
    left a term containing a double space keyed differently in each half, and the same result
    then reported it found and missing at once.
    """
    return TERM_WS_RE.sub(" ", str(text or "").replace("\u00a0", " ")).strip().lower()


def _skipped(frames):
    """{"framesSkipped": [...]} or {} — the agent's decision row keys on the field's PRESENCE."""
    return {"framesSkipped": frames} if frames else {}


def _global_cap(matches, keys):
    """Page-wide ceiling after frames are merged. Returns (kept, was_capped).

    Terms with no visible match anywhere spend their hidden evidence FIRST, so a hidden-only
    term always keeps at least one match to point at the collapsed section that owns it.
    Term classification is computed from the pre-cap counts regardless, so dropping a match
    here can never manufacture a false `termsMissing`.
    """
    if len(matches) <= GLOBAL_MATCH_CAP:
        return matches, False
    order = {id(m): i for i, m in enumerate(matches)}
    chosen, taken = [], set()

    def pool_of(key, want_visible):
        return [m for m in matches if bool(m.get("visible")) is want_visible and key in m["matched"]]

    def add(match):
        if len(chosen) >= GLOBAL_MATCH_CAP or id(match) in taken:
            return False
        taken.add(id(match))
        chosen.append(match)
        return True

    def round_robin(pools):
        """One match per term per round, so no single term can drain the budget."""
        cursor = {k: 0 for k in pools}
        while len(chosen) < GLOBAL_MATCH_CAP:
            progressed = False
            for k in keys:
                pool = pools.get(k) or []
                while cursor[k] < len(pool) and id(pool[cursor[k]]) in taken:
                    cursor[k] += 1
                if cursor[k] < len(pool) and add(pool[cursor[k]]):
                    cursor[k] += 1
                    progressed = True
            if not progressed:
                return

    visible = {k: pool_of(k, True) for k in keys}
    hidden = {k: pool_of(k, False) for k in keys}
    # 1) ONE hidden match per hidden-only term — enough to point at the collapsed section that
    #    owns it. Spending each hidden pool IN FULL here let five hidden-only terms consume all
    #    40 slots and drop the one visible control on the page.
    for k in keys:
        if not visible[k] and hidden[k]:
            add(hidden[k][0])
    # 2) every visible match, round-robin across all terms.
    round_robin(visible)
    # 3) whatever budget is left goes to the remaining hidden matches.
    round_robin(hidden)
    chosen.sort(key=lambda m: order[id(m)])
    return chosen, True


def read_field_state(frame, selector):
    try:
        return frame.evaluate(STATE_JS, selector)
    except PWError:
        return {"found": False}


# ---------------------------------------------------------------- actions

def do_scan(page, args):
    terms = [t for t in args.terms if t.strip()]
    quiet_ms, load_ms = args.quiet_ms, args.load_ms
    t0 = time.monotonic()
    url0 = page.url
    quiet_since = None
    verdict, matches, ready = None, [], {}
    frame_errors = []
    keys, seen = [], set()
    for raw in terms:
        k = norm_term(raw)
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    stats = {k: {"total": 0, "hidden": 0} for k in keys}
    busy_exit = False
    while True:
        ready = readiness(page)
        if ready["isLogin"]:
            verdict = "login"
            break
        if nav_signature(ready["url"]) != nav_signature(url0):
            return {"action": "scan", "status": "indeterminate", "code": "NAVIGATION_DURING_ACTION",
                    "urlBefore": url0, "urlAfter": ready["url"],
                    "note": "the page navigated mid-scan — re-run the scan once against the new page"}
        page_quiet = not ready["busy"]
        matches = []
        frame_errors = []
        stats = {k: {"total": 0, "hidden": 0} for k in keys}
        if terms:
            for fr, label in visible_frames(page):
                try:
                    got = fr.evaluate(HARVEST_JS, {"terms": terms, "perTermCap": PER_TERM_VIS_CAP,
                                                   "hiddenCap": PER_TERM_HID_CAP})
                except PWError as exc:
                    if is_context_destroyed(exc):
                        raise
                    frame_errors.append(label)
                    continue
                for k, v in (got.get("stats") or {}).items():
                    if k in stats:
                        stats[k]["total"] += v.get("total", 0)
                        stats[k]["hidden"] += v.get("hidden", 0)
                frame_matches = got.get("matches") or []
                for m in frame_matches:
                    m["frame"] = label
                    for s in m.get("sels", []):
                        s["risk"] = "generated" if GENERATED_ID_RE.search(str(s.get("value", ""))) else "stable"
                matches.extend(frame_matches)
        # Pre-cap totals, so the early exit cannot be defeated by a term whose matches were
        # trimmed by the per-term cap.
        found_terms = {k for k in keys if stats[k]["total"]}
        # Only a QUIET page may return instantly. While D365 is provably busy, a matching grid
        # cell can be mounted before the command bar exists, and exiting on it reproduces the
        # very defect this release removes.
        if terms and page_quiet and len(found_terms) == len(keys):
            verdict = "found"  # every term visibly present on a settled page — instant
            break
        if not page_quiet:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.monotonic()
        if not terms and page_quiet:
            verdict = "ready"
            break
        # Partial results and absence both get the full quiet dwell, so termsMissing
        # is confirmed over the same window the absence verdict uses.
        if quiet_since is not None and (time.monotonic() - quiet_since) * 1000 >= quiet_ms:
            verdict = "found" if matches else "absent"
            break
        # The load allowance applies only while the page is provably busy —
        # once quiet, the absence clock is allowed to finish past load_ms.
        # If visible matches exist when the allowance expires, that's a usable
        # partial result, not a stuck page.
        if not page_quiet and (time.monotonic() - t0) * 1000 >= load_ms:
            # The page never settled. Terms not seen here are UNRESOLVED, never absent.
            busy_exit = True
            verdict = "found" if any(m["visible"] for m in matches) else "load-timeout"
            break
        time.sleep(0.25)

    # "Could not inspect a frame" must never masquerade as "absent".
    if verdict == "absent" and frame_errors:
        return {"action": "scan", "status": "failed", "code": "FRAME_READ_ERROR",
                "frames": frame_errors,
                "note": "some visible frames could not be inspected — retry once; do not conclude absence"}

    try:
        title = page.title()
    except PWError:
        title = None

    frames_meta = {}
    for fr, label in visible_frames(page):
        if label == "main":
            continue
        if any(m.get("frame") == label for m in matches):
            try:
                frames_meta[label] = {"name": fr.name or None, "url": fr.url,
                                      "chain": frame_chain(page, fr)}
            except PWError:
                frames_meta[label] = {"name": fr.name or None, "url": fr.url, "chain": []}

    matches, page_capped = _global_cap(matches, keys)
    # Classification comes from the PRE-cap per-term counts, never from what survived the
    # cap. Deriving it from the returned sample let a dropped match become a false
    # `termsMissing` — the very failure this release exists to remove.
    found = [k for k in keys if stats[k]["total"]]
    found_hidden = [k for k in keys if not stats[k]["total"] and stats[k]["hidden"]]
    missing, unresolved = [], []
    for k in keys:
        if k in found or k in found_hidden:
            continue
        # Absence requires a settled page AND every visible frame read. Anything else is
        # "not established", which is not the same claim as "not there".
        (unresolved if (busy_exit or frame_errors) else missing).append(k)
    ret_vis = {k: sum(1 for m in matches if k in m["matched"] and m["visible"]) for k in keys}
    ret_hid = {k: sum(1 for m in matches if k in m["matched"] and not m["visible"]) for k in keys}
    truncated = [k for k in keys if ret_vis[k] < stats[k]["total"] or ret_hid[k] < stats[k]["hidden"]]
    result = {"action": "scan", "status": "ok", "verdict": verdict, **ready, "title": title,
              "waitedMs": int((time.monotonic() - t0) * 1000),
              "termsFound": found, "termsMissing": missing, "matches": matches}
    if found_hidden:
        result["termsFoundHidden"] = found_hidden
    if unresolved:
        result["termsUnresolved"] = unresolved
        result["note"] = ("neither found nor absent — the page never settled or a frame could not be "
                          "read; re-scan these terms, never record them as absent")
    if truncated:
        result["status"] = "ambiguous"
        result["code"] = "SCAN_RESULTS_TRUNCATED"
        result["truncatedTerms"] = truncated
        result["matchCounts"] = {k: {"visible": stats[k]["total"], "hidden": stats[k]["hidden"],
                                     "returned": ret_vis[k] + ret_hid[k]} for k in truncated}
        if page_capped:
            result["pageCapped"] = True
        result["note"] = ("more matches exist than were returned — do NOT choose from this list; "
                          "narrow the CSS selector and probe it")
    if frames_meta:
        result["frames"] = frames_meta
    if frame_errors:
        result["frameErrors"] = frame_errors
    return result


def do_probe(page, args):
    frames = search_frames(page, args.frame)
    if args.frame and not frames:
        return {"action": "probe", "status": "failed", "code": "FRAME_NOT_FOUND",
                "error": f"no frame matches --frame {args.frame!r}"}
    results = []
    skipped = []
    for fr, label in frames:
        try:
            r = fr.evaluate(PROBE_JS, args.selector)
        except PWError as exc:
            if is_context_destroyed(exc):
                raise
            skipped.append(label)
            continue
        if r.get("invalid"):
            return {"action": "probe", "status": "failed", "code": "INVALID_SELECTOR"}
        if r["count"]:
            results.append({"frame": label, **{k: r[k] for k in ("count", "visibleCount", "actionableCount", "sample")}})
    total = sum(r["count"] for r in results)
    # A single hit across the frames we COULD read is not uniqueness. Status stays ok and
    # framesSkipped is disclosed (the agent re-runs once per its decision row) — escalating
    # to FRAME_READ_ERROR would spend a run-ending strike on a routine frame race.
    out = {"action": "probe", "status": "ok", "count": total,
           "unique": total == 1 and not skipped, "perFrame": results}
    if skipped:
        out["framesSkipped"] = skipped
    return out


def actionability_details(frame, selector):
    try:
        return frame.evaluate(PROBE_JS, selector)
    except PWError:
        return {}


def do_click(page, args):
    resolved, err = locate_unique(page, args.selector, args.frame, "click")
    if err:
        return err
    fr, label, _ = resolved
    loc = fr.locator(args.selector)
    url_before = page.url
    # Trial separates "not actionable" from "the click's aftermath timed out".
    try:
        loc.click(trial=True, timeout=ACTION_TIMEOUT_MS)
    except PWTimeoutError:
        details = actionability_details(fr, args.selector)
        return {"action": "click", "status": "failed", "code": "ELEMENT_NOT_ACTIONABLE",
                "frame": label, "details": details.get("sample", details),
                "note": "element exists but is not clickable (covered/disabled/unstable)"}
    except PWError as exc:
        if is_page_mutation(exc):
            return {"action": "click", "status": "indeterminate", "code": "NAVIGATION_DURING_ACTION",
                    "urlBefore": url_before, "note": "the page mutated before the click — scan and re-assess"}
        raise
    try:
        loc.click(timeout=ACTION_TIMEOUT_MS, no_wait_after=True)
    except (PWTimeoutError, PWError) as exc:
        current_url = page.url
        if is_page_mutation(exc) or "navigation" in str(exc).lower() or nav_signature(current_url) != nav_signature(url_before):
            return {"action": "click", "status": "indeterminate", "code": "NAVIGATION_DURING_ACTION",
                    "urlBefore": url_before, "urlAfter": current_url,
                    "note": "the page navigated while clicking — scan for the expected effect; retry ONLY if it is missing"}
        if isinstance(exc, PWTimeoutError):
            return {"action": "click", "status": "failed", "code": "ELEMENT_NOT_ACTIONABLE",
                    "frame": label, "note": "element became non-actionable between trial and click"}
        raise
    return {"action": "click", "status": "ok", "frame": label,
            "urlBefore": url_before, "urlAfter": page.url}


def do_fill(page, args):
    resolved, err = locate_unique(page, args.selector, args.frame, "fill")
    if err:
        return err
    fr, label, _ = resolved
    loc = fr.locator(args.selector)
    try:
        tag = loc.evaluate("el => el.tagName.toLowerCase()", timeout=ACTION_TIMEOUT_MS)
        if tag not in ("input", "textarea"):
            inner = loc.locator("input,textarea")
            if inner.count() != 1:
                return {"action": "fill", "status": "failed", "code": "NO_INPUT",
                        "note": f"selector is a <{tag}> without exactly one inner input"}
            loc = inner
        loc.fill(args.text, timeout=ACTION_TIMEOUT_MS)
    except PWTimeoutError:
        return {"action": "fill", "status": "failed", "code": "ELEMENT_NOT_ACTIONABLE", "frame": label}
    except PWError as exc:
        if is_page_mutation(exc):
            return {"action": "fill", "status": "indeterminate", "code": "NAVIGATION_DURING_ACTION"}
        raise
    try:
        value = loc.input_value(timeout=1000)
    except (PWError, PWTimeoutError):
        return {"action": "fill", "status": "ok", "frame": label, "value": None, "verified": None,
                "note": "fill dispatched but the control re-rendered before read-back — use state if the value matters"}
    return {"action": "fill", "status": "ok", "frame": label,
            "value": value, "verified": value == args.text}


def do_press(page, args):
    try:
        if args.selector:
            resolved, err = locate_unique(page, args.selector, args.frame, "press")
            if err:
                return err
            fr, label, _ = resolved
            fr.locator(args.selector).press(args.key, timeout=ACTION_TIMEOUT_MS)
        else:
            label = "page"
            page.keyboard.press(args.key)
    except PWTimeoutError:
        return {"action": "press", "status": "failed", "code": "ELEMENT_NOT_ACTIONABLE", "key": args.key}
    return {"action": "press", "status": "ok", "key": args.key, "target": label}


def do_state(page, args):
    resolved, err = locate_unique(page, args.selector, args.frame, "state")
    if err:
        return err
    fr, label, frames_skipped = resolved
    st = read_field_state(fr, args.selector)
    if not st.get("found"):
        return {"action": "state", "status": "failed", "code": "NOT_FOUND",
                "note": "the element disappeared between resolution and read — re-scan"}
    return {"action": "state", "status": "ok", "frame": label, **_skipped(frames_skipped), **st}


def verify_pick(fr, trigger_selector, want_l, picked_l, deadline_s):
    """Poll field-state facets until one EQUALS the wanted label; (verified, verifiedBy, state).

    Equality, not containment: a substring test verified `My Task` as a successful pick of
    `Task` — the same confident-wrong report removed from scan and from option selection.
    """
    state_keys = ("value", "innerValue", "ariaLabel", "title", "text")
    vdeadline = time.monotonic() + deadline_s
    field_state = {}
    while time.monotonic() < vdeadline:
        st = read_field_state(fr, trigger_selector)
        field_state = {k: st.get(k) for k in state_keys if st.get(k)}
        for key, val in field_state.items():
            fv = str(val).lower()
            if norm_term(fv) == want_l or (picked_l and norm_term(fv) == picked_l):
                return True, key, field_state
        time.sleep(0.2)
    return False, None, field_state


def do_pick(page, args):
    resolved, err = locate_unique(page, args.trigger_selector, args.frame, "pick")
    if err:
        return err
    fr, label, _ = resolved
    loc = fr.locator(args.trigger_selector)
    want = args.option_text.strip()
    want_l = norm_term(want)
    # Tag AND value-holding in ONE evaluation, BEFORE anything is opened or clicked. Doing it
    # afterwards meant a D365 re-render turned `false` into `null`, which slipped past the
    # action-menu rule and let the trigger's own label verify the pick as a success.
    try:
        trigger_meta = loc.evaluate("""el => {
          const r = (el.getAttribute('role') || '').toLowerCase();
          const tag = el.tagName.toLowerCase();
          const holdsValue = ['combobox','textbox','listbox','searchbox','spinbutton'].includes(r)
            || ['input','textarea','select'].includes(tag)
            || !!el.querySelector('input,textarea,select,[role=\"combobox\"],[role=\"textbox\"]');
          return { tag, holdsValue };
        }""", timeout=ACTION_TIMEOUT_MS)
    except (PWTimeoutError, PWError) as exc:
        if is_page_mutation(exc):
            return {"action": "pick", "status": "indeterminate", "code": "NAVIGATION_DURING_ACTION"}
        return {"action": "pick", "status": "failed", "code": "NOT_FOUND",
                "note": "trigger detached before it could be inspected — re-scan"}
    tag = trigger_meta["tag"]
    holds_value = trigger_meta["holdsValue"]

    if tag == "select":
        try:
            loc.select_option(label=want, timeout=ACTION_TIMEOUT_MS)
        except PWTimeoutError:
            # Playwright retries a missing label until timeout — distinguish "label
            # absent" from a genuinely non-actionable select by reading the options.
            st = read_field_state(fr, args.trigger_selector)
            labels = [o.get("label") for o in (st.get("options") or [])]
            if labels and not any(norm_term(lbl) == want_l for lbl in labels):
                return {"action": "pick", "status": "failed", "code": "OPTION_NOT_IN_SELECT",
                        "options": labels[:30], "note": "the select has no option with that label"}
            return {"action": "pick", "status": "failed", "code": "ELEMENT_NOT_ACTIONABLE"}
        except PWError as exc:
            if is_page_mutation(exc):
                return {"action": "pick", "status": "indeterminate", "code": "NAVIGATION_DURING_ACTION"}
            return {"action": "pick", "status": "failed", "code": "OPTION_NOT_IN_SELECT",
                    "note": "run state on the select to list its options"}
        st = read_field_state(fr, args.trigger_selector)
        selected = next((o.get("label") for o in (st.get("options") or []) if o.get("selected")), None)
        verified = bool(selected) and norm_term(selected) == want_l
        return {"action": "pick", "status": "ok", "method": "native-select", "searched": "native-select",
                # `selected` only — claiming `want` when nothing read back is the same
                # confident-wrong report this release exists to remove.
                "frame": label, "picked": selected, "verified": verified,
                "verifiedBy": "selected-option"}

    # Portal-rendered options can mount in the trigger's frame or the top document.
    option_frames = [(fr, label)]
    if fr is not page.main_frame:
        option_frames.append((page.main_frame, "main"))
    for of, _l in option_frames:
        try:
            of.evaluate(PICK_BASELINE_JS)
        except PWError:
            pass

    url_before = page.url
    try:
        if args.type_text is not None:
            try:
                loc.click(timeout=ACTION_TIMEOUT_MS)
            except PWError:
                pass
            input_loc = loc if tag in ("input", "textarea") else loc.locator("input,textarea")
            if input_loc.count() != 1:
                return {"action": "pick", "status": "failed", "code": "NO_INPUT",
                        "note": "type-ahead pick needs exactly one input inside the trigger"}
            input_loc.fill("", timeout=ACTION_TIMEOUT_MS)
            try:
                input_loc.press_sequentially(args.type_text, delay=25)
            except AttributeError:
                input_loc.type(args.type_text, delay=25)
        else:
            expanded = fr.evaluate(EXPANDED_JS, args.trigger_selector)
            if expanded != "true":
                loc.click(timeout=ACTION_TIMEOUT_MS)

        deadline = time.monotonic() + (5.0 if args.type_text is not None else 3.0)
        picked_info = None
        option_frame_label = None
        seen_union = []
        last_searched = "all"
        while time.monotonic() < deadline:
            report_rows = []
            for of, olabel in option_frames:
                try:
                    rep = of.evaluate(PICK_COLLECT_JS,
                                      {"want": want, "triggerSelector": args.trigger_selector, "mark": False})
                except PWError as exc:
                    if is_context_destroyed(exc):
                        raise
                    continue
                report_rows.append((of, olabel, rep))
            for _of, _lbl, rep in report_rows:
                for t in rep.get("optionsSeen", []):
                    if t not in seen_union:
                        seen_union.append(t)
            non_all = [row for row in report_rows if row[2]["searched"] != "all"]
            if non_all:
                last_searched = non_all[0][2]["searched"]
            elif report_rows:
                last_searched = "all"
            total_exacts = sum(r["exactCount"] for _, _, r in report_rows)
            if total_exacts > 1:
                return {"action": "pick", "status": "ambiguous", "code": "MULTIPLE_EXACT_OPTIONS",
                        "optionsSeen": seen_union[:15],
                        "perFrame": [{"frame": lbl, "exactCount": r["exactCount"], "searched": r["searched"]}
                                     for _, lbl, r in report_rows]}
            chosen = None
            if total_exacts == 1:
                chosen = next(((of, lbl) for of, lbl, r in report_rows if r["exactCount"] == 1), None)
            if chosen:
                of, olabel = chosen
                res = of.evaluate(PICK_COLLECT_JS,
                                  {"want": want, "triggerSelector": args.trigger_selector, "mark": True})
                if res.get("decided"):
                    tloc = of.locator('[data-agent-harvest-target="1"]')
                    if tloc.count() == 1:
                        try:
                            tloc.click(timeout=ACTION_TIMEOUT_MS, no_wait_after=True)
                        except PWTimeoutError:
                            if nav_signature(page.url) != nav_signature(url_before):
                                return {"action": "pick", "status": "indeterminate",
                                        "code": "NAVIGATION_DURING_ACTION", "urlBefore": url_before,
                                        "urlAfter": page.url}
                            return {"action": "pick", "status": "failed", "code": "ELEMENT_NOT_ACTIONABLE",
                                    "note": "the option could not be clicked (occluded/unstable)"}
                        picked_info = res
                        option_frame_label = olabel
                        break
            time.sleep(0.25)

        if picked_info is None:
            return {"action": "pick", "status": "failed", "code": "OPTION_NOT_FOUND",
                    "optionsSeen": seen_union[:15], "searched": last_searched,
                    "typed": args.type_text is not None}

        picked_l = str(picked_info.get("picked", "")).lower()
        # A field trigger displays what you chose; an ACTION menu (command bar, "Quick
        # Actions") never does — so verified:false there is structural, not a failure.
        # holds_value was settled BEFORE the click, so a post-click re-render cannot erase it.
        if holds_value is False:
            # An action menu's trigger never displays the picked option, so polling it can only
            # ever match the trigger's OWN label — a false positive, and 3 wasted seconds.
            verified, verified_by, field_state = False, None, {}
        else:
            verified, verified_by, field_state = verify_pick(fr, args.trigger_selector,
                                                             want_l, picked_l, 3.0)
        option_meta = picked_info.get("option", {})
        template_candidates = []
        role = option_meta.get("role") or ""
        role_part = f'[role="{role}"]' if role and '"' not in role else ""
        for c in option_meta.get("candidates", []):
            value = c.get("value") or ""
            # Build from parts (never string-replace: a value that collides with the
            # role text would corrupt the template) and skip values that would break
            # CSS quoting or the Page layer's .format().
            if c.get("n") != 1 or not value or any(ch in value for ch in '{}"\\'):
                continue
            template_candidates.append({"attr": c["attr"],
                                        "template": f'{role_part}[{c["attr"]}="{{value}}"]',
                                        "instance": c["sel"], "count": 1})
        field_value = field_state.get(verified_by) if verified else next(iter(field_state.values()), None)
        # A menu option that navigates SUCCESSFULLY raises nothing, so verification just fails
        # and the result reads like an unverified field selection — inviting a retry of an
        # action that already happened.
        if nav_signature(page.url) != nav_signature(url_before):
            return {"action": "pick", "status": "indeterminate", "code": "NAVIGATION_DURING_ACTION",
                    "urlBefore": url_before, "urlAfter": page.url, "picked": picked_info.get("picked"),
                    "note": "the option navigated the page — scan for the expected effect; re-run ONLY if it is missing"}
        return {"action": "pick", "status": "ok", "picked": picked_info.get("picked"),
                "matchedBy": picked_info.get("matchedBy"), "searched": picked_info.get("searched"),
                "optionFrame": option_frame_label, "frame": label,
                "fieldValue": field_value, "fieldState": field_state,
                "verified": verified, "verifiedBy": verified_by, "triggerHoldsValue": holds_value,
                "option": {k: option_meta.get(k) for k in ("tag", "role", "title", "ariaLabel", "dataId", "text")},
                "templateCandidates": template_candidates}
    except PWTimeoutError:
        return {"action": "pick", "status": "failed", "code": "ELEMENT_NOT_ACTIONABLE",
                "note": "trigger/input/option exists but could not be actioned (occluded/disabled/unstable)"}
    except PWError as exc:
        if is_page_mutation(exc):
            return {"action": "pick", "status": "indeterminate", "code": "NAVIGATION_DURING_ACTION",
                    "note": "the page mutated mid-pick — scan for the expected effect; re-run ONLY if it is missing"}
        raise
    finally:
        # The alarm fires ONCE and is never re-armed, so touching a wedged renderer here can
        # block forever and no JSON is ever emitted. Markers are idempotent and the next
        # pick's baseline sweep clears them.
        if not isinstance(sys.exc_info()[1], WatchdogTimeout):
            for of, _l in option_frames:
                try:
                    of.evaluate(PICK_CLEANUP_JS)
                except PWError:
                    pass


# ---------------------------------------------------------------- nav paths (no browser)

NAV_FILE_HEADER = """# Known navigation paths for the field automation agent.
# Read with:  agent_harvest.py nav list      Extend with:  agent_harvest.py nav add
# Append-only via the script; prune or correct by hand in a PR.

paths:
"""


def _nav_key(text):
    """Loose comparison key: case- and punctuation-insensitive."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _nav_terms(row):
    """Every term a row answers to — its feature plus all aliases."""
    terms = [row.get("feature")] + list(row.get("aliases") or [])
    return {k for k in (_nav_key(t) for t in terms) if k}


def _nav_read(path):
    """(raw_text, rows). rows is None when the file does not exist."""
    import yaml  # local: a missing PyYAML must not break the browser commands
    if not path.exists():
        return None, None
    with path.open("r", encoding="utf-8", newline="") as handle:
        raw = handle.read()
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict) or not isinstance(data.get("paths"), list):
        raise ValueError(f"{NAV_PATHS_FILE}: expected a top-level 'paths' list")
    for index, row in enumerate(data["paths"]):
        if not isinstance(row, dict):
            raise ValueError(f"{NAV_PATHS_FILE}: entry #{index + 1} is not a mapping ({type(row).__name__})")
        if not row.get("feature") or not row.get("path"):
            raise ValueError(f"{NAV_PATHS_FILE}: entry #{index + 1} is missing 'feature' or 'path'")
    return raw, data["paths"]


def _nav_block(entry, newline):
    """One YAML list item. json.dumps gives valid double-quoted YAML scalars."""
    out = [f'  - feature: {json.dumps(entry["feature"], ensure_ascii=False)}']
    if entry["aliases"]:
        out.append("    aliases: [" + ", ".join(json.dumps(a, ensure_ascii=False) for a in entry["aliases"]) + "]")
    out.append(f'    path: {json.dumps(entry["path"], ensure_ascii=False)}')
    if entry["anchor"]:
        out.append(f'    anchor: {json.dumps(entry["anchor"], ensure_ascii=False)}')
    out.append(f'    source: {entry["source"]}')
    out.append(f'    verified: "{entry["verified"]}"')
    return newline + newline.join(out) + newline


def do_nav(args):
    import yaml
    path = pathlib.Path(NAV_PATHS_FILE)
    try:
        raw, rows = _nav_read(path)
    except (ValueError, yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        return emit({"action": "nav", "status": "failed", "code": "NAV_FILE_INVALID",
                     "file": NAV_PATHS_FILE, "resolvedPath": str(path.resolve()),
                     "error": str(exc)[:300]}, 1)

    if args.nav_action == "list":
        return emit({"action": "nav", "status": "ok", "file": NAV_PATHS_FILE,
                     "resolvedPath": str(path.resolve()), "fileExists": rows is not None,
                     "count": len(rows or []), "paths": rows or []})

    feature, new_path = args.feature.strip(), args.path.strip()
    if not feature or not new_path:
        return emit({"action": "nav", "status": "failed", "code": "BAD_ARGS",
                     "error": "--feature and --path must not be empty"}, 1)
    aliases = [a.strip() for a in (args.aliases or "").split(",") if a.strip()]

    # Wrong-cwd guard: never fork a stray table somewhere the agent will not read it.
    if rows is None and not path.parent.exists():
        return emit({"action": "nav", "status": "failed", "code": "NAV_DIR_MISSING",
                     "resolvedPath": str(path.resolve()),
                     "error": f"{path.parent} does not exist — run from the repo root"}, 1)

    # Dedupe in BOTH directions: the proposed feature AND its aliases are compared
    # against every existing feature AND alias. A one-directional check would let a
    # second row claim terms an existing row already answers to.
    proposed = {k for k in (_nav_key(t) for t in [feature] + aliases) if k}
    for row in rows or []:
        overlap = proposed & _nav_terms(row)
        if not overlap:
            continue
        if _nav_key(row.get("path")) == _nav_key(new_path):
            return emit({"action": "nav", "status": "ok", "code": "ALREADY_PRESENT",
                         "feature": row.get("feature"), "path": row.get("path"),
                         "matchedOn": sorted(overlap)})
        return emit({"action": "nav", "status": "ambiguous", "code": "PATH_CONFLICT",
                     "feature": row.get("feature"), "existingPath": row.get("path"),
                     "proposedFeature": feature, "proposedPath": new_path,
                     "matchedOn": sorted(overlap),
                     "note": "an existing row already answers to these terms with a different path. "
                             "If it is the same feature, report both to the user. If it is a different "
                             "feature colliding on an alias, retry with a more specific feature name "
                             "and narrower aliases. The file was NOT changed."})

    entry = {"feature": feature, "path": new_path,
             "anchor": (args.anchor or "").strip() or None,
             "aliases": aliases, "source": args.source,
             "verified": date.today().isoformat()}

    if rows is None:                      # file absent — create it with the header
        newline = "\n"
        new_text = NAV_FILE_HEADER + _nav_block(entry, newline).lstrip("\n")
    else:                                 # file present — append, never rewrite
        newline = "\r\n" if "\r\n" in raw else "\n"
        body = raw
        if not rows:                      # 'paths: []' cannot take an appended item
            body = re.sub(r"^paths:\s*\[\s*\]\s*$", "paths:", body, count=1, flags=re.M)
        new_text = body.rstrip("\r\n") + newline + _nav_block(entry, newline)

    # Prove the result parses AND that every field survived before touching disk —
    # a malformed or lossy append would poison every later `nav list`.
    try:
        check = yaml.safe_load(new_text) or {}
        after = check.get("paths")
        if not isinstance(after, list) or len(after) != len(rows or []) + 1:
            raise ValueError("row count did not grow by exactly one")
        written = after[-1]
        for field in ("feature", "path", "anchor", "source", "verified"):
            if written.get(field) != entry[field]:
                raise ValueError(f"{field} did not round-trip: {written.get(field)!r} != {entry[field]!r}")
        if list(written.get("aliases") or []) != entry["aliases"]:
            raise ValueError("aliases did not round-trip")
    except (yaml.YAMLError, ValueError, AttributeError) as exc:
        return emit({"action": "nav", "status": "failed", "code": "NAV_WRITE_ABORTED",
                     "error": f"refusing to write: {str(exc)[:200]}",
                     "note": "nothing was changed — the value contains characters this table cannot store"}, 1)

    try:
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(new_text)
    except OSError as exc:
        return emit({"action": "nav", "status": "failed", "code": "NAV_WRITE_FAILED",
                     "resolvedPath": str(path.resolve()), "error": str(exc)[:200]}, 1)

    return emit({"action": "nav", "status": "ok", "code": "ADDED",
                 "entry": entry, "count": len(rows or []) + 1, "file": NAV_PATHS_FILE})


# ---------------------------------------------------------------- CLI

def build_parser():
    parser = argparse.ArgumentParser(prog="agent_harvest.py",
        description="Deterministic SCAN/PROBE/CLICK/FILL/PRESS/STATE/PICK against the live D365 page over CDP.")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("version", help="Print tool version.")

    scan = sub.add_parser("scan", help="Readiness + batch element harvest (no terms = readiness only).")
    scan.add_argument("terms", nargs="*")
    scan.add_argument("--quiet-ms", type=int, default=2000)
    scan.add_argument("--load-ms", type=int, default=15000)

    for name in ("probe", "click", "state"):
        p = sub.add_parser(name)
        p.add_argument("selector")
        p.add_argument("--frame", default=None)

    fill = sub.add_parser("fill", help="Native fill of a textbox.")
    fill.add_argument("selector")
    fill.add_argument("text")
    fill.add_argument("--frame", default=None)

    press = sub.add_parser("press", help="Press a key (Escape, Enter, Tab, ArrowDown, ...).")
    press.add_argument("key")
    press.add_argument("--selector", default=None)
    press.add_argument("--frame", default=None)

    pick = sub.add_parser("pick", help="Choose an option from a dropdown/menu/lookup (self-verifying).")
    pick.add_argument("trigger_selector")
    pick.add_argument("option_text")
    pick.add_argument("--type", dest="type_text", default=None)
    pick.add_argument("--frame", default=None)

    nav = sub.add_parser("nav", help="Read or extend the known navigation paths table (no browser).")
    nav_sub = nav.add_subparsers(dest="nav_action", required=True)
    nav_sub.add_parser("list", help="Print every known navigation path as JSON.")
    nav_add = nav_sub.add_parser("add", help="Append one confirmed navigation path.")
    nav_add.add_argument("--feature", required=True, help="Canonical feature name (dedupe key).")
    nav_add.add_argument("--path", required=True, help='UI steps, e.g. "Contact record → Activities tab → New".')
    nav_add.add_argument("--anchor", default=None, help="Repo file that implements/exercises this path.")
    nav_add.add_argument("--aliases", default=None, help="Comma-separated terms issues use for this feature.")
    nav_add.add_argument("--source", default="exploration",
                         choices=["issue", "codebase", "exploration", "user", "seed"])
    return parser


def validate(args):
    if args.action == "scan":
        if not (0 <= args.quiet_ms <= 30000):
            return "quiet-ms must be 0..30000"
        if not (args.quiet_ms <= args.load_ms <= 120000):
            return "load-ms must be >= quiet-ms and <= 120000"
        if len(args.terms) > 10 or any(len(t) > 80 for t in args.terms):
            return "at most 10 terms, each <= 80 chars"
    if args.action == "pick" and not args.option_text.strip():
        return "option_text must not be empty"
    for attr in ("selector", "trigger_selector"):
        if getattr(args, attr, None) is not None and not getattr(args, attr).strip():
            return f"{attr} must not be empty"
    return None


ACTIONS = {"scan": do_scan, "probe": do_probe, "click": do_click,
           "fill": do_fill, "press": do_press, "state": do_state, "pick": do_pick}


def main() -> int:
    try:
        args = build_parser().parse_args()
    except SystemExit as exc:
        if exc.code in (0, None):  # -h/--help — not an error
            return 0
        # argparse already printed usage to stderr; keep the stdout-JSON contract.
        print(json.dumps({"action": None, "status": "failed", "code": "BAD_ARGS",
                          "error": "Invalid arguments. See the script docstring for usage."}))
        return 1

    if args.action == "version":
        return emit({"action": "version", "status": "ok", "version": VERSION})

    if args.action == "nav":
        return do_nav(args)   # data command — never touches the browser

    problem = validate(args)
    if problem:
        return emit({"action": args.action, "status": "failed", "code": "BAD_ARGS", "error": problem}, 1)

    if PLAYWRIGHT_IMPORT_ERROR is not None:
        return emit({"action": args.action, "status": "failed", "code": "RUNTIME_ERROR",
                     "error": f"Playwright is not importable: {PLAYWRIGHT_IMPORT_ERROR[:160]}",
                     "hint": "Playwright is missing or broken in the repo venv. Installing is not "
                             "permitted from this agent; ask the user to repair the venv."}, 1)

    startup_alarm = hasattr(signal, "SIGALRM")
    if startup_alarm:
        signal.signal(signal.SIGALRM, _watchdog_handler)
        signal.alarm(STARTUP_BUDGET_S)
    try:
        playwright = sync_playwright().start()
    except WatchdogTimeout:
        # A startup HANG is the same fault as a startup EXCEPTION: a broken driver/venv, before
        # any CDP connection or page access. Reporting TOOL_TIMEOUT sent the agent down the
        # "the browser stopped responding" path — one wasted retry and the wrong diagnosis for
        # the user. Same shape as the branch below, so one agent rule covers both.
        return emit({"action": args.action, "status": "failed", "code": "RUNTIME_ERROR",
                     "error": f"Playwright driver failed to start: timed out after {STARTUP_BUDGET_S}s",
                     "hint": "Playwright is missing or broken in the repo venv. Installing is not "
                             "permitted from this agent; ask the user to repair the venv."}, 1)
    except Exception as exc:   # noqa: BLE001 - driver startup must never escape as a traceback
        return emit({"action": args.action, "status": "failed", "code": "RUNTIME_ERROR",
                     "error": f"Playwright driver failed to start: {str(exc)[:200]}",
                     "hint": "Playwright is missing or broken in the repo venv. Installing is not "
                             "permitted from this agent; ask the user to repair the venv."}, 1)
    finally:
        if startup_alarm:
            signal.alarm(0)

    try:
        try:
            # Never call browser.close() — that would kill the shared Chrome.
            browser = playwright.chromium.connect_over_cdp(CDP_URL, timeout=CDP_TIMEOUT_MS)
        except Exception as exc:
            return emit({"action": args.action, "status": "failed", "code": "CDP_UNREACHABLE",
                         "error": str(exc)[:200],
                         "hint": "Run scripts/start-chrome-debug-persistent.bat on Windows and check the 9222 forward."}, 1)
        try:
            page = select_page(browser)
        except LookupError as exc:
            return emit({"action": args.action, "status": "failed",
                         "code": str(exc).split(":", 1)[0], "error": str(exc)}, 1)
        except Exception as exc:   # noqa: BLE001 - a CDP drop while enumerating pages
            return emit({"action": args.action, "status": "failed", "code": "CDP_UNREACHABLE",
                         "error": str(exc)[:200],
                         "hint": "the browser went away while listing tabs — re-run once"}, 1)

        # Hard watchdog: protocol calls (evaluate/title) have no timeout of their own
        # and can block forever on a wedged renderer — the one-JSON contract must hold.
        budget_s = 60
        if args.action == "scan":
            budget_s = max(60, int((args.load_ms + args.quiet_ms) / 1000) + 30)
        use_alarm = hasattr(signal, "SIGALRM")
        if use_alarm:
            signal.signal(signal.SIGALRM, _watchdog_handler)
            signal.alarm(budget_s)
        try:
            result = ACTIONS[args.action](page, args)
        except WatchdogTimeout:
            return emit({"action": args.action, "status": "failed", "code": "TOOL_TIMEOUT",
                         "note": "the browser did not respond within the watchdog — "
                                 "treat like a stuck page (load-timeout rules)"})
        except Exception as exc:
            if is_page_mutation(exc):
                result = {"action": args.action, "status": "indeterminate",
                          "code": "NAVIGATION_DURING_ACTION",
                          "note": "the page mutated while the action ran — scan and check the expected effect"}
            else:
                return emit({"action": args.action, "status": "failed", "code": "RUNTIME_ERROR",
                             "error": str(exc)[:300]}, 1)
        finally:
            if use_alarm:
                signal.alarm(0)
        result.setdefault("pageUrl", page.url)
        return emit(result)
    finally:
        # Stop OUR driver connection only. NEVER browser.close() — the Chrome session is
        # shared and long-lived, and killing it costs a manual re-login.
        try:
            playwright.stop()
        except Exception:   # noqa: BLE001 - teardown must never replace a real result
            pass


if __name__ == "__main__":
    sys.exit(main())
