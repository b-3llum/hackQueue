// hackQueue board. No dependencies: the page must work from a self-hosted box
// with no CDN and no build step.

const GUILD_ID = location.pathname.split("/").filter(Boolean)[1];
const PERIODS = [
  { key: "weekly", label: "This week" },
  { key: "monthly", label: "This month" },
  { key: "alltime", label: "All-time" },
];

const state = { board: "composite", period: "weekly", data: null, member: null };
// last score we displayed per member, so a refresh can show what moved
const lastSeen = new Map();
let lastUpdate = null;
const $ = (id) => document.getElementById(id);

// ── board ────────────────────────────────────────────────────────────────

async function load() {
  const res = await fetch(`/api/g/${GUILD_ID}?board=${state.board}&period=${state.period}`);
  if (!res.ok) {
    $("board").innerHTML =
      '<div class="empty">This leaderboard isn\'t published. A moderator can turn it on with <code>/config web on</code>.</div>';
    return;
  }
  state.data = await res.json();
  lastUpdate = Date.now();
  render();
}

function render() {
  const d = state.data;
  $("guild-name").textContent = d.guild.name;
  const icon = $("guild-icon");
  if (d.guild.icon) {
    icon.src = d.guild.icon;
    icon.hidden = false;
  }

  const boardLabel = d.boards.find((b) => b.key === d.board)?.label ?? d.board;
  const periodLabel = PERIODS.find((p) => p.key === d.period).label;
  $("period-label").textContent = `${boardLabel} · ${periodLabel}`;

  renderTabs("board-tabs", d.boards, state.board, (key) => {
    state.board = key;
    load();
  });
  renderTabs("period-tabs", PERIODS, state.period, (key) => {
    state.period = key;
    load();
  });
  renderSummary(d);
  renderRows(d);
  renderLegend(d);

  $("notice").innerHTML = d.stale.length
    ? `<div class="notice">Showing the last data we have for ${d.stale.join(", ")} — that platform is unreachable right now, so those scores may be behind.</div>`
    : "";
  tickClock();
}

// The pulse says "this data is live"; it stops when we've lost the server.
// The ticker counts real seconds since the last successful poll.
function tickClock() {
  const el = $("updated");
  if (!lastUpdate) return;
  const secs = Math.round((Date.now() - lastUpdate) / 1000);
  const stale = secs > 180;
  el.className = stale ? "live stale" : "live";
  el.textContent = stale ? `last update ${ago(secs)}` : `live · updated ${ago(secs)}`;
  el.title = new Date(lastUpdate).toLocaleString();
}

function ago(secs) {
  if (secs < 5) return "just now";
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.round(mins / 60)}h ago`;
}

function renderTabs(id, items, active, onPick) {
  $(id).replaceChildren(
    ...items.map((item) => {
      const b = document.createElement("button");
      b.className = "tab";
      b.type = "button";
      b.textContent = item.label;
      b.setAttribute("aria-pressed", String(item.key === active));
      b.addEventListener("click", () => onPick(item.key));
      return b;
    })
  );
}

function renderSummary(d) {
  const s = d.summary;
  const unit = d.board === "composite" ? "index total" : `${s.unit} on the board`;
  const stats = [
    ["Members", s.members],
    ["Active", s.active],
    [unit, d.board === "composite" ? s.total.toFixed(0) : Math.round(s.total).toLocaleString()],
  ];
  $("summary").replaceChildren(
    ...stats.map(([k, v]) => {
      const el = document.createElement("div");
      el.className = "stat";
      el.innerHTML = `<span class="k"></span><span class="v"></span>`;
      el.querySelector(".k").textContent = k;
      el.querySelector(".v").textContent = v;
      return el;
    })
  );
}

function renderRows(d) {
  const board = $("board");
  if (!d.rows.length) {
    // A delta board (weekly/monthly) is empty when nobody's GAINED points in
    // the window — which is normal early in a period. Don't tell people to
    // link (they may already have); point them at all-time instead.
    const msg =
      d.period === "alltime"
        ? 'Nobody\'s linked an account yet. In Discord: <code>/link htb &lt;id&gt;</code>.'
        : `No points gained ${d.period === "weekly" ? "this week" : "this month"} yet — the board fills as people solve. Try <button class="tab inline-link" data-period="alltime">all-time</button>.`;
    board.innerHTML = `<div class="empty">${msg}</div>`;
    const jump = board.querySelector("[data-period]");
    if (jump) jump.addEventListener("click", () => { state.period = "alltime"; load(); });
    return;
  }
  const top = Math.max(...d.rows.map((r) => r.value), 0) || 1;

  board.replaceChildren(
    ...d.rows.map((row, i) => {
      const el = document.createElement("button");
      el.className = i === 0 ? "row leader" : "row";
      el.type = "button";
      el.setAttribute("aria-label", `${row.name} — open details`);
      el.addEventListener("click", () => openMember(row.user_id));

      const rank = document.createElement("div");
      rank.className = "rank";
      rank.textContent = String(row.rank).padStart(2, "0");

      const avatar = avatarFor(row.avatar, row.name);

      const who = document.createElement("div");
      who.className = "who";
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = row.name;
      if (!row.verified) {
        const flag = document.createElement("span");
        flag.className = "unverified";
        flag.textContent = "unverified";
        flag.title = "This member hasn't proven they own the linked account.";
        name.appendChild(flag);
      }
      who.appendChild(name);
      if (row.handle) {
        const handle = document.createElement("div");
        handle.className = "handle";
        handle.textContent = row.handle;
        who.appendChild(handle);
      }

      // Each segment is one platform's contribution — the bar is the formula.
      const bar = document.createElement("div");
      bar.className = "bar";
      Object.entries(row.parts)
        .sort((a, b) => b[1] - a[1])
        .forEach(([platform, value], n) => {
          const seg = document.createElement("div");
          seg.className = `seg ${platform}`;
          seg.title = `${d.platform_labels[platform] ?? platform}: ${value}`;
          seg.style.width = `${Math.min((value / top) * 100, 100)}%`;
          seg.style.animationDelay = `${30 * i + 50 * n}ms`;
          bar.appendChild(seg);
        });

      const move = document.createElement("div");
      move.className = "move";
      if (row.movement === null) {
        move.textContent = "new";
      } else if (row.movement > 0) {
        move.textContent = `▲${row.movement}`;
        move.classList.add("up");
      } else if (row.movement < 0) {
        move.textContent = `▼${-row.movement}`;
        move.classList.add("down");
      } else {
        move.textContent = "—";
      }
      move.title = "Movement since the last period";

      const score = document.createElement("div");
      score.className = "score";
      score.textContent =
        d.board === "composite" ? row.value.toFixed(1) : Math.round(row.value).toLocaleString();

      // If this member's score moved since the last refresh, say so — the
      // flash and the delta are the only reason to animate anything here.
      const key = `${d.board}:${d.period}:${row.user_id}`;
      const before = lastSeen.get(key);
      if (before !== undefined && row.value > before) {
        el.classList.add("changed");
        score.classList.add("rising");
        const delta = document.createElement("span");
        delta.className = "delta";
        const gained = row.value - before;
        delta.textContent = `+${d.board === "composite" ? gained.toFixed(1) : Math.round(gained)}`;
        score.appendChild(delta);
      }
      lastSeen.set(key, row.value);

      el.append(rank, avatar, who, bar, move, score);
      return el;
    })
  );
}

function avatarFor(url, name) {
  if (url) {
    const img = document.createElement("img");
    img.className = "avatar";
    img.alt = "";
    img.loading = "lazy";
    img.src = url;
    return img;
  }
  const div = document.createElement("div");
  div.className = "avatar avatar-blank";
  div.textContent = (name[0] || "?").toUpperCase();
  return div;
}

function renderLegend(d) {
  const platforms = new Set();
  d.rows.forEach((r) => Object.keys(r.parts).forEach((p) => platforms.add(p)));
  const legend = $("legend");
  if (d.board !== "composite" || platforms.size < 2) {
    legend.replaceChildren();
    return;
  }
  legend.replaceChildren(
    ...[...platforms].map((p) => {
      const span = document.createElement("span");
      const swatch = document.createElement("i");
      swatch.className = `seg ${p}`;
      swatch.style.animation = "none";
      span.append(swatch, document.createTextNode(d.platform_labels[p] ?? p));
      return span;
    })
  );
}

// ── member panel ─────────────────────────────────────────────────────────

async function openMember(userId) {
  const res = await fetch(`/api/g/${GUILD_ID}/member/${userId}`);
  if (!res.ok) return;
  state.member = await res.json();
  renderMember();
  $("scrim").classList.add("open");
  $("panel").classList.add("open");
  $("panel-close").focus();
}

function closeMember() {
  $("scrim").classList.remove("open");
  $("panel").classList.remove("open");
}

function renderMember() {
  const m = state.member;
  $("panel-avatar").replaceWith(dressAvatar(avatarFor(m.avatar, m.name)));
  $("panel-name").textContent = m.name;

  // No cross-platform total here on purpose: HTB flags, THM points and
  // Root-Me score are different units and summing them would be a lie. Gains
  // are shown per platform, where the unit is unambiguous.
  kpis([
    ["Solves", m.total_solves],
    ["Streak", m.streak_weeks ? `${m.streak_weeks}w` : "—"],
    ["Claims", m.claims.approved || "—"],
  ]);

  // per-platform cards, each with its own score series
  $("panel-platforms").replaceChildren(
    ...m.platforms.map((p) => {
      const card = document.createElement("div");
      card.className = "pcard";

      const top = document.createElement("div");
      top.className = "pcard-top";
      const nameEl = document.createElement("span");
      nameEl.className = "pname";
      nameEl.textContent = p.label;
      const scoreEl = document.createElement("span");
      scoreEl.className = "pscore";
      scoreEl.textContent =
        p.score === null ? "no data yet" : `${p.score.toLocaleString()} ${p.unit}`;
      top.append(nameEl, scoreEl);

      const meta = document.createElement("div");
      meta.className = "meta";
      const bits = [];
      if (p.profile_url) {
        bits.push(`<a href="${p.profile_url}" target="_blank" rel="noopener">${escapeHtml(p.username)}</a>`);
      } else {
        bits.push(escapeHtml(p.username));
      }
      if (p.rank) bits.push(`global #${p.rank.toLocaleString()}`);
      if (p.weekly_gain) bits.push(`+${p.weekly_gain} this week`);
      if (p.verified) bits.push("verified");
      else if (p.verifiable) bits.push("unverified");
      if (p.status !== "ok") bits.push(`⚠ ${p.status.replace("_", " ")}`);
      meta.innerHTML = bits.join(" · ");

      card.append(top, meta);

      const counters = counterLine(p);
      if (counters) {
        const c = document.createElement("div");
        c.className = "counters";
        c.textContent = counters;
        card.appendChild(c);
      }
      if (p.series.length > 1) card.appendChild(sparkline(p.series));
      return card;
    })
  );

  // claims
  const claims = $("panel-claims");
  claims.textContent = m.claims.approved
    ? `${m.claims.approved} approved · ${m.claims.points} points`
    : "No approved claims yet.";

  // recent solves
  const solves = $("panel-solves");
  if (!m.recent_solves.length) {
    solves.innerHTML = '<li class="when">Nothing recorded yet.</li>';
  } else {
    solves.replaceChildren(
      ...m.recent_solves.map((s) => {
        const li = document.createElement("li");
        const kind = document.createElement("span");
        kind.className = "kind";
        kind.textContent = s.kind;
        const name = document.createElement("span");
        if (s.url) {
          const a = document.createElement("a");
          a.href = s.url;
          a.target = "_blank";
          a.rel = "noopener";
          a.textContent = s.name;
          name.appendChild(a);
        } else {
          name.textContent = s.name;
        }
        if (s.first_blood) name.append(" 🩸");
        const when = document.createElement("span");
        when.className = "when";
        when.textContent = s.solved_at ? new Date(s.solved_at).toLocaleDateString() : "";
        li.append(kind, name, when);
        return li;
      })
    );
  }

  renderActivity(m.activity);
}

function dressAvatar(el) {
  el.id = "panel-avatar";
  return el;
}

function kpis(items) {
  $("panel-kpis").replaceChildren(
    ...items.map(([k, v]) => {
      const el = document.createElement("div");
      el.className = "kpi";
      el.innerHTML = '<span class="k"></span><span class="v"></span>';
      el.querySelector(".k").textContent = k;
      el.querySelector(".v").textContent = v;
      return el;
    })
  );
}

function counterLine(p) {
  const c = p.counters || {};
  const bits = [];
  const owns = (c.user_owns || 0) + (c.system_owns || 0);
  if (owns) bits.push(`${owns} machine owns`);
  if (c.challenges) bits.push(`${c.challenges} challenges`);
  if (c.prolab_flags) {
    const done = c.prolabs_completed ? ` (${c.prolabs_completed} completed)` : "";
    bits.push(`${c.prolab_flags} Pro Lab flags${done}`);
  }
  if (c.fortress_flags) bits.push(`${c.fortress_flags} Fortress flags`);
  if (c.validations) bits.push(`${c.validations} validations`);
  if (c.rooms_completed) bits.push(`${c.rooms_completed} rooms`);
  const bloods = (c.user_bloods || 0) + (c.system_bloods || 0);
  if (bloods) bits.push(`${bloods} bloods 🩸`);
  return bits.join(" · ");
}

// A score series drawn as a filled line. Flat lines still render (a straight
// line at mid-height) rather than collapsing to nothing.
function sparkline(series) {
  const w = 100;
  const h = 30;
  const values = series.map(([, v]) => v);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = values.length > 1 ? w / (values.length - 1) : w;
  const pts = values.map((v, i) => [i * step, h - ((v - min) / span) * (h - 4) - 2]);

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");

  const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = document.createElementNS("http://www.w3.org/2000/svg", "path");
  area.setAttribute("class", "area");
  area.setAttribute("d", `${d} L${w},${h} L0,${h} Z`);
  const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
  line.setAttribute("d", d);
  const cursor = document.createElementNS("http://www.w3.org/2000/svg", "line");
  cursor.setAttribute("class", "cursor");
  cursor.setAttribute("y1", "0");
  cursor.setAttribute("y2", String(h));
  cursor.setAttribute("opacity", "0");
  svg.append(area, line, cursor);

  // Hover reads the series: date + score under the pointer. The chart is only
  // worth drawing if you can interrogate it.
  const wrap = document.createElement("div");
  wrap.className = "spark-wrap";
  const readout = document.createElement("div");
  readout.className = "spark-read";
  wrap.append(svg, readout);
  wrap.addEventListener("pointermove", (e) => {
    const box = svg.getBoundingClientRect();
    const frac = Math.min(Math.max((e.clientX - box.left) / box.width, 0), 1);
    const i = Math.round(frac * (series.length - 1));
    const [when, value] = series[i];
    cursor.setAttribute("x1", String(pts[i][0]));
    cursor.setAttribute("x2", String(pts[i][0]));
    cursor.setAttribute("opacity", "1");
    readout.textContent = `${new Date(when).toLocaleDateString()} · ${value.toLocaleString()}`;
  });
  wrap.addEventListener("pointerleave", () => {
    cursor.setAttribute("opacity", "0");
  });
  return wrap;
}

function renderActivity(weeks) {
  const max = Math.max(...weeks.map((w) => w.solves), 1);
  $("panel-activity").replaceChildren(
    ...weeks.map((w) => {
      const el = document.createElement("div");
      el.style.height = `${Math.max((w.solves / max) * 100, 8)}%`;
      el.style.background = w.solves ? "var(--fg-dim)" : "var(--sunk)";
      el.title = `week of ${w.week}: ${w.solves} solve${w.solves === 1 ? "" : "s"}`;
      return el;
    })
  );
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

// ── wiring ───────────────────────────────────────────────────────────────

$("scrim").addEventListener("click", closeMember);
$("panel-close").addEventListener("click", closeMember);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") return closeMember();
  // j/k walk the board (vim keys — the audience will expect them)
  if (e.key !== "j" && e.key !== "k") return;
  const rows = [...document.querySelectorAll(".row")];
  if (!rows.length) return;
  const at = rows.indexOf(document.activeElement);
  const next = e.key === "j" ? Math.min(at + 1, rows.length - 1) : Math.max(at - 1, 0);
  rows[at === -1 ? 0 : next].focus();
});

// The ticker runs even between polls, so "updated 40s ago" is always honest.
setInterval(tickClock, 1000);

load();
setInterval(load, 120000);
