const $ = (id) => document.getElementById(id);
let me = null;
let chain = null;
let world = null;
let active = null;
let roomSlug = "";
let tick = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || "GET",
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || "Request failed");
  return payload;
}

function clockText(seconds) {
  const safe = Math.max(0, seconds | 0);
  return `${String(Math.floor(safe / 60)).padStart(2, "0")}:${String(safe % 60).padStart(2, "0")}`;
}

function show(name) {
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("hidden", view.id !== `view-${name}`));
  document.querySelectorAll("nav button").forEach((button) => button.classList.toggle("on", button.dataset.view === name));
  if (name === "rooms") loadRooms();
  if (name === "world") paintWorld();
  if (name === "expeditions") loadExpedition();
  if (name === "board") loadBoard();
  if (name === "profile") paintProfile();
}

function paintChain() {
  const visual = chain?.visual || "UNAVAILABLE";
  const clock = $("clock");
  clock.classList.toggle("rest", visual === "RESTING");
  clock.classList.toggle("down", visual === "UNAVAILABLE");
  const settlement = chain?.settlement === "SETTLING" ? " · settling" : "";
  $("chain-state").textContent = visual + settlement;
  if (!active && $("timer").textContent === "Reading the chain…") $("timer").textContent = "25:00";
  const round = chain?.round ? `Settled round ${chain.round}` : "No confirmed round";
  const work = chain?.work ? ` · ${chain.work} work` : "";
  const pending = chain?.pendingRound ? ` · round ${chain.pendingRound} not certified` : "";
  $("chain-line").textContent = chain?.reason || `${round}${work}${pending}`;
}

function paintSession() {
  $("cancel").classList.toggle("hidden", !active);
  $("start-25").disabled = Boolean(active);
  $("start-50").disabled = Boolean(active);
  if (!active) {
    $("timer").textContent = "25:00";
    return;
  }
  $("timer").textContent = clockText(active.remaining);
  $("session-note").textContent = `${active.kind === "custom" ? "Custom" : active.kind + " minute"} session · ${clockText(active.remaining)} left`;
}

async function refreshMe() {
  const payload = await api("/api/me");
  me = payload.user ? payload : null;
  const next = payload.active || null;
  const justFinished = active && !next;
  active = next;
  $("who").textContent = payload.user ? payload.user.username : "Guest";
  $("auth-open").textContent = payload.user ? "Sign out" : "Sign in";
  paintSession();
  if (justFinished) {
    $("session-note").textContent = "Session saved.";
    paintProfile();
  }
}

function interpretStatus(payload) {
  if (!payload || payload.schemaVersion !== 1 || !payload.builder?.anchor?.settledThroughRound) {
    return { visual: "UNAVAILABLE", settlement: "UNAVAILABLE", round: null, work: null, reason: "Squeekchain status is unread. Missing data is not zero activity." };
  }
  const measurement = payload.measurement || {};
  const anchor = payload.builder.anchor;
  const observed = Date.parse(measurement.observedAt || "");
  const limit = (Number(measurement.staleAfterSeconds) || 120) * 1000;
  const fresh = measurement.freshness === "online" && Number.isFinite(observed) && Date.now() - observed < limit;
  const status = measurement.coreStatus;
  const visual = !fresh || !status ? "UNAVAILABLE" : status === "ACTIVE" ? "LIVE" : status === "IDLE" ? "RESTING" : "UNAVAILABLE";
  const submission = payload.settlementQueue?.latestSubmission || {};
  const confirmed = submission.lifecycle === "confirmed" && String(submission.settledThroughRound) === String(anchor.settledThroughRound);
  const pending = submission.settledThroughRound && !confirmed ? String(submission.settledThroughRound) : null;
  const round = Number(anchor.settledThroughRound);
  const stages = [[0, "The First Burrow"], [800, "The Growing Village"], [2000, "The Hamster City"], [5000, "The Crystal Kingdom"], [10000, "The Infinite World"]];
  let name = stages[0][1];
  let index = 1;
  let nextRound = stages[1][0];
  stages.forEach(([floor, label], number) => {
    if (round >= floor) {
      name = label;
      index = number + 1;
      nextRound = stages[number + 1] ? stages[number + 1][0] : null;
    }
  });
  return {
    chain: {
      visual,
      settlement: pending ? "SETTLING" : "CONFIRMED",
      round: String(anchor.settledThroughRound),
      work: String(anchor.cumulativeWork),
      pendingRound: pending,
      reason: visual === "UNAVAILABLE" ? "Fresh activity is not available. The hamster is not shown as resting." : null
    },
    world: { stage: index, name, settledRounds: round, nextRound }
  };
}

async function refreshChain() {
  try {
    const payload = await api("/api/chain");
    chain = payload.chain;
    world = payload.world;
  } catch {
    const response = await fetch("https://api.squeekchain.com/v1/status", { cache: "no-store" });
    if (!response.ok) throw new Error("Status unread");
    const read = interpretStatus(await response.json());
    chain = read.chain;
    world = read.world;
  }
  paintChain();
  paintWorld();
}

async function refreshToday() {
  let payload;
  try {
    payload = await api("/api/expedition");
  } catch {
    $("today").textContent = "—";
    $("live-count").textContent = "Room counts need the Squeek Hours server.";
    $("mission-name").textContent = "Mission 001 — Build the First City";
    $("mission-bar").textContent = "Community totals are counted by the server, not by the chain.";
    return;
  }
  $("today").textContent = String(payload.community.todayMinutes);
  $("live-count").textContent = `${payload.community.activeUsers} people in rooms right now`;
  const mission = payload.mission;
  const done = Math.min(mission.sessions, payload.community.sessions);
  $("mission-name").textContent = `Mission ${mission.id} — ${mission.name}`;
  $("mission-bar").textContent = `${done} / ${mission.sessions} sessions · ${payload.community.minutes} / ${mission.minutes} minutes`;
}

async function start(kind) {
  if (!me?.user) {
    $("auth").showModal();
    return;
  }
  const payload = await api("/api/sessions", { method: "POST", body: { kind, room: roomSlug || undefined } });
  active = { id: payload.id, kind, remaining: payload.remaining };
  paintSession();
}

async function loadRooms() {
  const payload = await api("/api/rooms");
  const list = $("room-list");
  list.replaceChildren();
  payload.rooms.forEach((room) => {
    const card = document.createElement("article");
    const title = document.createElement("h2");
    title.textContent = room.name;
    const meta = document.createElement("p");
    meta.textContent = `${room.live} here now`;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Join";
    button.addEventListener("click", () => enter(room.slug));
    card.append(title, meta, button);
    list.appendChild(card);
  });
}

async function enter(slug, invite) {
  roomSlug = slug;
  await api(`/api/rooms/${slug}/join`, { method: "POST", body: { focus: $("room-focus").value, invite } });
  $("room-panel").classList.remove("hidden");
  await paintRoom();
}

async function paintRoom() {
  if (!roomSlug) return;
  const payload = await api(`/api/rooms/${roomSlug}`);
  $("room-name").textContent = payload.room.name;
  $("room-goal").textContent = payload.room.goal || "No shared goal yet.";
  const people = $("room-people");
  people.replaceChildren();
  payload.people.forEach((person) => {
    const item = document.createElement("li");
    item.textContent = `${person.username} · ${person.focus || "focused"}`;
    people.appendChild(item);
  });
  if (!payload.people.length) {
    const item = document.createElement("li");
    item.textContent = "Nobody is in this room yet.";
    people.appendChild(item);
  }
  const log = $("room-log");
  log.replaceChildren();
  payload.messages.forEach((note) => {
    const item = document.createElement("li");
    item.textContent = `${note.username}: ${note.body}`;
    log.appendChild(item);
  });
}

function paintWorld() {
  if (!world) return;
  $("world-name").textContent = world.name || "World unread";
  const next = world.nextRound ? ` Next stage opens at settled round ${world.nextRound}.` : " This is the last stage.";
  $("world-copy").textContent = world.reason
    || `Stage ${world.stage} follows confirmed round ${world.settledRounds}. Provisional blocks do not unlock a stage.${next}`;
}

async function loadExpedition() {
  const payload = await api("/api/expedition");
  const root = $("expedition");
  root.replaceChildren();
  const community = document.createElement("article");
  community.innerHTML = "<h2>Community progress</h2>";
  const communityText = document.createElement("p");
  communityText.textContent = `${payload.community.sessions} / ${payload.mission.sessions} sessions, ${payload.community.people} / ${payload.mission.people} people, ${payload.community.minutes} / ${payload.mission.minutes} minutes, ${payload.community.consecutiveDays} / ${payload.mission.days} active days.`;
  community.appendChild(communityText);
  const hamster = document.createElement("article");
  hamster.innerHTML = "<h2>Hamster progress</h2>";
  const hamsterText = document.createElement("p");
  hamsterText.textContent = payload.hamster.name
    ? `${payload.hamster.name}, settled round ${payload.hamster.settledRounds}. This is chain progress, not focus minutes.`
    : "Confirmed chain progress is unread.";
  hamster.appendChild(hamsterText);
  root.append(community, hamster);
}

async function loadBoard() {
  const payload = await api("/api/leaderboard");
  const list = $("leaders");
  list.replaceChildren();
  if (!payload.leaders.length) {
    const item = document.createElement("li");
    item.textContent = "No finished sessions yet.";
    list.appendChild(item);
    return;
  }
  payload.leaders.forEach((leader) => {
    const item = document.createElement("li");
    item.textContent = `${leader.username} · ${leader.level} · ${leader.xp} XP · ${leader.minutes} min · streak ${leader.streak}`;
    list.appendChild(item);
  });
}

function paintProfile() {
  if (!me?.user) {
    $("profile-name").textContent = "Sign in to keep a profile";
    $("profile-stats").textContent = "";
    return;
  }
  const stats = me.stats;
  $("profile-name").textContent = me.user.username;
  $("profile-stats").textContent = `${stats.level} · ${stats.xp} XP · ${stats.minutes} minutes · ${stats.sessions} sessions · streak ${stats.streak} · best ${stats.best}`;
  const themes = $("themes");
  themes.replaceChildren();
  me.themes.forEach((theme) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = theme.open ? theme.id : `${theme.id} · ${theme.xp} XP`;
    button.disabled = !theme.open;
    button.className = me.user.theme === theme.id ? "" : "ghost";
    button.addEventListener("click", async () => {
      await api("/api/theme", { method: "POST", body: { theme: theme.id } });
      await refreshMe();
      paintProfile();
    });
    themes.appendChild(button);
  });
  const badges = $("badges");
  badges.replaceChildren();
  me.achievements.forEach((badge) => {
    const item = document.createElement("li");
    item.textContent = badge.earned ? badge.name : `${badge.name} — still open`;
    badges.appendChild(item);
  });
}

function shareCard() {
  const canvas = document.createElement("canvas");
  canvas.width = 1200;
  canvas.height = 675;
  const pen = canvas.getContext("2d");
  pen.fillStyle = "#090d16";
  pen.fillRect(0, 0, 1200, 675);
  pen.fillStyle = "#a6ff5b";
  pen.font = "64px Georgia";
  pen.fillText("SQUEEK HOURS", 64, 140);
  pen.fillStyle = "#f5f7fa";
  pen.font = "36px Segoe UI";
  const name = me?.user?.username || "Guest";
  const minutes = me?.stats?.minutes || 0;
  const sessions = me?.stats?.sessions || 0;
  pen.fillText(`${name} · ${minutes} focus minutes`, 64, 260);
  pen.fillText(`${sessions} sessions · ${me?.stats?.level || "Bronze"}`, 64, 320);
  pen.fillStyle = "#49d9cf";
  pen.fillText("Squeekchain keeps the chain. People keep the hours.", 64, 460);
  const link = document.createElement("a");
  link.href = canvas.toDataURL("image/png");
  link.download = "squeek-hours.png";
  link.click();
}

document.querySelectorAll("nav button").forEach((button) => {
  button.addEventListener("click", () => show(button.dataset.view));
});
$("start-25").addEventListener("click", () => start("25").catch((error) => { $("session-note").textContent = error.message; }));
$("start-50").addEventListener("click", () => start("50").catch((error) => { $("session-note").textContent = error.message; }));
$("cancel").addEventListener("click", async () => {
  if (!active) return;
  await api(`/api/sessions/${active.id}/cancel`, { method: "POST", body: {} });
  active = null;
  paintSession();
  paintChain();
});
$("auth-open").addEventListener("click", async () => {
  if (me?.user) {
    await api("/api/logout", { method: "POST", body: {} });
    me = null;
    active = null;
    roomSlug = "";
    await refreshMe();
    return;
  }
  $("auth").showModal();
});
$("auth-close").addEventListener("click", () => $("auth").close());
$("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await sendAuth("/api/login");
});
$("register").addEventListener("click", () => sendAuth("/api/register"));
$("room-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!roomSlug) return;
  await api(`/api/rooms/${roomSlug}/messages`, { method: "POST", body: { body: $("room-text").value } });
  $("room-text").value = "";
  await paintRoom();
});
$("room-focus").addEventListener("change", () => {
  if (roomSlug) enter(roomSlug).catch(() => {});
});
$("private-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = await api("/api/rooms", { method: "POST", body: { name: $("private-name").value, goal: $("private-goal").value } });
  $("private-result").textContent = `Code ${payload.slug} · invite ${payload.invite}`;
});
$("invite-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await enter($("invite-slug").value.trim(), $("invite-code").value.trim());
});
$("share").addEventListener("click", shareCard);

async function sendAuth(path) {
  const form = new FormData($("auth-form"));
  try {
    await api(path, { method: "POST", body: { username: form.get("username"), password: form.get("password") } });
    $("auth-error").textContent = "";
    $("auth").close();
    await refreshMe();
  } catch (error) {
    $("auth-error").textContent = error.message;
  }
}

show("home");
refreshChain().catch(() => { $("chain-state").textContent = "UNAVAILABLE"; });
refreshMe().catch(() => {});
refreshToday().catch(() => {});
setInterval(() => refreshChain().catch(() => {}), 8000);
setInterval(() => refreshToday().catch(() => {}), 8000);
setInterval(() => {
  if (active) {
    active.remaining = Math.max(0, active.remaining - 1);
    paintSession();
    if (active.remaining === 0) refreshMe().catch(() => {});
  }
}, 1000);
setInterval(() => {
  if (roomSlug && !document.getElementById("view-rooms").classList.contains("hidden")) {
    enter(roomSlug).catch(() => {});
  }
}, 4000);
