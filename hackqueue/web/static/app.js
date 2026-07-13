// hackQueue board. Reads the guild id from the path, fetches JSON, renders.
// No dependencies: the page must work from a self-hosted box with no CDN.

const GUILD_ID = location.pathname.split("/").filter(Boolean).pop();
const PERIODS = [
  { key: "weekly", label: "This week" },
  { key: "monthly", label: "This month" },
  { key: "alltime", label: "All-time" },
];

const state = { board: "composite", period: "weekly", data: null };

const $ = (id) => document.getElementById(id);

async function load() {
  const url = `/api/g/${GUILD_ID}?board=${state.board}&period=${state.period}`;
  const res = await fetch(url);
  if (!res.ok) {
    $("board").innerHTML =
      '<div class="empty">This leaderboard isn\'t published. A moderator can turn it on with <code>/config web on</code>.</div>';
    return;
  }
  state.data = await res.json();
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
  const periodLabel = PERIODS.find((p) => p.key === d.period).label;
  const boardLabel = d.boards.find((b) => b.key === d.board)?.label ?? d.board;
  const gained = d.period === "alltime" ? "total" : "gained";
  $("period-label").textContent = `${boardLabel} · ${periodLabel} · points ${gained}`;

  renderPills("board-pills", d.boards, state.board, (key) => {
    state.board = key;
    load();
  });
  renderPills("period-pills", PERIODS.map((p) => ({ key: p.key, label: p.label })), state.period, (key) => {
    state.period = key;
    load();
  });

  renderRows(d);
  renderLegend(d);

  $("notice").innerHTML = d.stale.length
    ? `<div class="notice">Showing the last data we have for ${d.stale.join(", ")} — that platform is unreachable right now, so those scores may be behind.</div>`
    : "";
  const when = new Date(d.generated_at);
  $("updated").textContent = `updated ${when.toLocaleString()}`;
}

function renderPills(containerId, items, active, onPick) {
  const box = $(containerId);
  box.replaceChildren(
    ...items.map((item) => {
      const b = document.createElement("button");
      b.className = "pill";
      b.type = "button";
      b.textContent = item.label;
      b.setAttribute("aria-pressed", String(item.key === active));
      b.addEventListener("click", () => onPick(item.key));
      return b;
    })
  );
}

function renderRows(d) {
  const board = $("board");
  if (!d.rows.length) {
    board.innerHTML =
      '<div class="empty">Nobody has scored this period yet. Link an account in Discord with <code>/link</code> and start pwning.</div>';
    return;
  }

  // Bars are scaled against the leader, so the top row always fills the track.
  const top = Math.max(...d.rows.map((r) => r.value), 0) || 1;

  board.replaceChildren(
    ...d.rows.map((row, i) => {
      const el = document.createElement("article");
      el.className = i === 0 ? "row leader" : "row";

      const rank = document.createElement("div");
      rank.className = "rank";
      rank.textContent = String(row.rank).padStart(2, "0");

      // No avatar (member left, or never seen): initial in a disc, never a
      // broken-image icon.
      let avatar;
      if (row.avatar) {
        avatar = document.createElement("img");
        avatar.className = "avatar";
        avatar.alt = "";
        avatar.loading = "lazy";
        avatar.src = row.avatar;
      } else {
        avatar = document.createElement("div");
        avatar.className = "avatar avatar-blank";
        avatar.textContent = (row.name[0] || "?").toUpperCase();
      }

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

      // The signature: each segment is one platform's contribution to the
      // score, so the bar is the composite formula drawn to scale.
      const bar = document.createElement("div");
      bar.className = "bar";
      const parts = Object.entries(row.parts).sort((a, b) => b[1] - a[1]);
      const segments = parts.map(([platform, value], n) => {
        const seg = document.createElement("div");
        seg.className = `seg ${platform}`;
        seg.title = `${d.platform_labels[platform] ?? platform}: ${value}`;
        // Width is set now, not by a timer: if the animation never runs
        // (background tab, reduced motion), the bar is still correct. The
        // grow is a transform, which can't change the layout.
        seg.style.width = `${Math.min((value / top) * 100, 100)}%`;
        seg.style.animationDelay = `${40 * i + 60 * n}ms`;
        return seg;
      });
      bar.append(...segments);

      const score = document.createElement("div");
      score.className = "score";
      score.textContent = formatValue(row.value, d.board);

      el.append(rank, avatar, who, bar, score);
      return el;
    })
  );
}

function formatValue(value, board) {
  // Composite is a 0–100 index; every other board is raw points.
  return board === "composite" ? value.toFixed(1) : Math.round(value).toLocaleString();
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
      swatch.style.background = `var(--${p})`;
      span.append(swatch, document.createTextNode(d.platform_labels[p] ?? p));
      return span;
    })
  );
}

load();
setInterval(load, 120000); // the poller runs on the order of an hour; this is plenty
