const express = require('express');
const WebSocket = require('ws');
const http = require('http');
const path = require('path');
try { require('dotenv').config(); } catch {}

const BOT_USERNAME = process.env.BOT_USERNAME || '';
const ROOM_TTL = 3 * 60 * 1000;

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

app.use(express.static(path.join(__dirname, 'public')));
app.get('/ping', (req, res) => res.json({ status: 'ok', uptime: Math.floor(process.uptime()) }));
app.get('/health', (req, res) => res.json({ status: 'ok', rooms: rooms.size }));

let gamesPlayed = 0;
app.get('/api/stats', (req, res) => {
  let totalPlayers = 0;
  rooms.forEach(r => { totalPlayers += r.players.filter(p => p.connected).length; });
  res.json({ rooms: rooms.size, players: totalPlayers, games: gamesPlayed });
});
app.get('*', (req, res) => res.sendFile(path.join(__dirname, 'public', 'index.html')));

// ─────────────────────────────────────────────
// ROLE DEFINITIONS
// ─────────────────────────────────────────────
const ROLE_INFO = {
  mafia:      { emoji: '🔫', name: 'Мафія',    color: '#e53e3e' },
  sheriff:    { emoji: '⭐', name: 'Шериф',    color: '#d69e2e' },
  doctor:     { emoji: '💊', name: 'Лікар',    color: '#38a169' },
  civilian:   { emoji: '🏘️', name: 'Мирний',  color: '#4a90d9' },
  prostitute: { emoji: '💃', name: 'Повія',   color: '#ed64a6' },
  detective:  { emoji: '🕵️', name: 'Детектив',color: '#9f7aea' },
};

// Night turn order (detective is passive — no active turn needed)
const NIGHT_ORDER = ['mafia', 'prostitute', 'sheriff', 'doctor'];

// ─────────────────────────────────────────────
// ROLE POOL
// ─────────────────────────────────────────────
function getRolePool(count, settings = {}) {
  const roles = settings.roles || {};
  const useSheriff    = roles.sheriff    !== false;
  const useDoctor     = roles.doctor     !== false;
  const useProstitute = roles.prostitute !== false;
  const useDetective  = roles.detective  !== false;

  // Base mafia count
  let mafiaCount;
  if (count <= 5)       mafiaCount = 1;
  else if (count <= 8)  mafiaCount = 2;
  else if (count <= 12) mafiaCount = 3;
  else if (count <= 16) mafiaCount = 4;
  else                  mafiaCount = 5;

  const pool = Array(mafiaCount).fill('mafia');
  const specials = [];
  if (useSheriff)                        specials.push('sheriff');
  if (useDoctor     && count >= 6)       specials.push('doctor');
  if (useProstitute && count >= 6)       specials.push('prostitute');
  if (useDetective  && count >= 7)       specials.push('detective');

  // Fill rest with civilians
  const civCount = count - mafiaCount - specials.length;
  if (civCount < 0) {
    // Too many specials — drop last ones
    specials.splice(specials.length + civCount);
    pool.push(...specials, ...Array(0).fill('civilian'));
  } else {
    pool.push(...specials, ...Array(civCount).fill('civilian'));
  }
  return pool;
}

function shuffle(arr) {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

// ─────────────────────────────────────────────
// ROOM FACTORY
// ─────────────────────────────────────────────
const rooms = new Map();

function generateRoomCode() {
  let code;
  do { code = String(Math.floor(10000 + Math.random() * 90000)); }
  while (rooms.has(code));
  return code;
}

function createRoom() {
  return {
    code: '', phase: 'lobby', night: 0,
    players: [], messages: [], nightLog: [],
    votes: {}, mafiaVotes: {}, nightActed: {},
    mafiaTarget: null, doctorTarget: null, sheriffTarget: null,
    prostituteTarget: null,
    phaseTime: 0, winner: null, nightTurn: null,
    suspects: {},   // { voterId: targetId }
    settings: {
      fastMode: false,
      roles: { prostitute: true, detective: true, sheriff: true, doctor: true },
      dayDuration: 60,
    },
    _timers: [],
  };
}

function clearTimers(room) { room._timers.forEach(t => clearTimeout(t)); room._timers = []; }
function delay(room, fn, ms) { const t = setTimeout(fn, ms); room._timers.push(t); return t; }

// ─────────────────────────────────────────────
// BROADCAST HELPERS
// ─────────────────────────────────────────────
function send(ws, msg) { if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg)); }
function broadcast(room, msg) { room.players.forEach(p => send(p.ws, msg)); }
function broadcastExcept(room, msg, excludeId) { room.players.filter(p => p.id !== excludeId).forEach(p => send(p.ws, msg)); }
function sendPrivate(room, id, msg) { const p = room.players.find(p => p.id === id); if (p) send(p.ws, msg); }

// ─────────────────────────────────────────────
// SNAPSHOT
// ─────────────────────────────────────────────
function getSnapshot(room, forPlayerId) {
  const me = room.players.find(p => p.id === forPlayerId);

  // Aggregate suspect counts → array of suspected playerIds (with ≥1 token)
  const suspectCounts = {};
  Object.values(room.suspects).forEach(tid => { if (tid) suspectCounts[tid] = (suspectCounts[tid] || 0) + 1; });
  const suspectedIds = Object.keys(suspectCounts).filter(id => suspectCounts[id] > 0);

  return {
    type: 'snapshot',
    phase: room.phase,
    phaseTime: room.phaseTime,
    night: room.night,
    code: room.code,
    myId: forPlayerId,
    myRole: me?.role || null,
    myAlive: me?.alive !== false,
    players: room.players.map(p => ({
      id: p.id, name: p.name, alive: p.alive, isHost: p.isHost,
      connected: p.connected, voted: room.votes[p.id] !== undefined,
      avatarUrl: p.avatarUrl || '', tgVerified: p.tgVerified || false,
    })),
    messages: room.messages,
    nightLog: room.nightLog,
    votes: room.phase === 'voting' ? room.votes : {},
    mafiaVotes: me?.role === 'mafia' ? room.mafiaVotes : {},
    nightActed: room.nightActed,
    nightTurn: room.nightTurn || null,
    winner: room.winner,
    revealedRoles: room.winner ? room.players.map(p => ({ id: p.id, role: p.role })) : [],
    suspects: suspectedIds,
    settings: room.settings,
  };
}

function broadcastState(room) { room.players.forEach(p => send(p.ws, getSnapshot(room, p.id))); }

// ─────────────────────────────────────────────
// NIGHT HELPERS
// ─────────────────────────────────────────────
function getAlive(room) { return room.players.filter(p => p.alive); }

function getActiveNightOrder(room) {
  const alive = getAlive(room);
  const { roles = {} } = room.settings;
  return NIGHT_ORDER.filter(role => {
    if (roles[role] === false) return false;
    return alive.some(p => p.role === role);
  });
}

function findFirstNightRole(room) { return getActiveNightOrder(room)[0] || null; }
function findNextNightRole(room, current) {
  const order = getActiveNightOrder(room);
  const idx = order.indexOf(current);
  if (idx === -1 || idx === order.length - 1) return null;
  return order[idx + 1];
}

// ─────────────────────────────────────────────
// COUNTDOWN
// ─────────────────────────────────────────────
function startCountdown(room, seconds, onEnd) {
  const fast = room.settings?.fastMode;
  room.phaseTime = fast ? Math.ceil(seconds * 0.6) : seconds;
  const tick = () => {
    broadcast(room, { type: 'tick', time: room.phaseTime });
    if (room.phaseTime <= 0) { onEnd(); return; }
    room.phaseTime--;
    room._timers.push(setTimeout(tick, 1000));
  };
  tick();
}

// ─────────────────────────────────────────────
// WIN CHECK
// ─────────────────────────────────────────────
function checkWin(room) {
  const alive = getAlive(room);
  const mafiaCount = alive.filter(p => p.role === 'mafia').length;
  const civCount   = alive.filter(p => p.role !== 'mafia').length;
  if (mafiaCount === 0)        { endGame(room, 'civilians'); return true; }
  if (mafiaCount >= civCount)  { endGame(room, 'mafia');     return true; }
  return false;
}

function endGame(room, winner) {
  clearTimers(room);
  room.phase = 'game_over';
  room.winner = winner;
  gamesPlayed++;
  broadcastState(room);
  broadcast(room, {
    type: 'game_over', winner,
    message: winner === 'civilians' ? '🎉 Мирні перемогли! Мафія знищена!' : '🔫 Мафія перемогла! Місто захоплено!',
    players: room.players.map(p => ({
      id: p.id, name: p.name, role: p.role, alive: p.alive,
      avatarUrl: p.avatarUrl || '', tgVerified: p.tgVerified || false,
    })),
  });
  room.phase = 'finished';
  room._deleteTimer = setTimeout(() => {
    if (rooms.has(room.code)) { rooms.delete(room.code); console.log(`[room] ${room.code} deleted`); }
  }, ROOM_TTL);
}

// ─────────────────────────────────────────────
// NIGHT PHASE
// ─────────────────────────────────────────────
function assignRoles(room) {
  const pool = shuffle(getRolePool(room.players.length, room.settings));
  room.players.forEach((p, i) => { p.role = pool[i]; p.alive = true; });
}

function startNight(room) {
  clearTimers(room);
  room.phase = 'night';
  room.night++;
  room.mafiaTarget = null;
  room.doctorTarget = null;
  room.sheriffTarget = null;
  room.prostituteTarget = null;
  room.mafiaVotes = {};
  room.nightActed = {};
  room.nightLog = [];
  room.suspects = {}; // reset suspect tokens each round

  const firstTurn = findFirstNightRole(room);
  room.nightTurn = firstTurn;

  broadcast(room, { type: 'phase', phase: 'night', night: room.night, turn: firstTurn });
  broadcastState(room);

  const nightDuration = room.settings?.fastMode ? 60 : 120;
  startCountdown(room, nightDuration, () => endNight(room));
}

function resolveMafiaVote(room) {
  const counts = {};
  Object.values(room.mafiaVotes).forEach(id => { if (id) counts[id] = (counts[id] || 0) + 1; });
  let best = null, max = 0;
  Object.entries(counts).forEach(([id, n]) => { if (n > max) { max = n; best = id; } });
  return best;
}

function endNight(room) {
  clearTimers(room);
  const log = [];

  // Finalize mafia target from votes
  if (Object.keys(room.mafiaVotes).length > 0) {
    room.mafiaTarget = resolveMafiaVote(room);
  }

  // ── Prostitute blocking logic ──────────────
  const prostTarget = room.prostituteTarget;
  if (prostTarget) {
    const prostitute = room.players.find(p => p.role === 'prostitute' && p.alive);
    if (prostitute) {
      // If mafia targeted the prostitute herself → mafia is blocked
      if (room.mafiaTarget === prostitute.id) {
        room.mafiaTarget = null;
        log.push({ emoji: '💃', text: 'Повія відвернула мафію від своїх планів!' });
      }
      // Block sheriff action if prostitute visited sheriff's target
      const sheriffPlayer = getAlive(room).find(p => p.role === 'sheriff');
      if (sheriffPlayer && prostTarget === sheriffPlayer.id) {
        room.sheriffTarget = null;
        sendPrivate(room, sheriffPlayer.id, { type: 'action_blocked' });
      }
      // Block doctor action if prostitute visited doctor
      const doctorPlayer = getAlive(room).find(p => p.role === 'doctor');
      if (doctorPlayer && prostTarget === doctorPlayer.id) {
        room.doctorTarget = null;
        sendPrivate(room, doctorPlayer.id, { type: 'action_blocked' });
      }
      // If prostitute visited a mafia member → remove that member's vote
      const prostTargetPlayer = room.players.find(p => p.id === prostTarget);
      if (prostTargetPlayer?.role === 'mafia') {
        // Find which mafia player this is and void their vote
        delete room.mafiaVotes[prostTarget];
        room.mafiaTarget = resolveMafiaVote(room) || null;
      }
    }
  }

  // ── Mafia kill ─────────────────────────────
  if (room.mafiaTarget) {
    const target = room.players.find(p => p.id === room.mafiaTarget);
    if (target?.alive) {
      if (room.doctorTarget === room.mafiaTarget) {
        log.push({ emoji: '🏥', text: 'Лікар врятував гравця цієї ночі!' });
      } else {
        target.alive = false;
        log.push({ emoji: '💀', text: `${target.name} був вбитий мафією!` });

        // Detective learns role of killed player
        const detective = getAlive(room).find(p => p.role === 'detective');
        if (detective) {
          sendPrivate(room, detective.id, {
            type: 'detective_result',
            name: target.name,
            role: target.role,
          });
        }
      }
    }
  } else {
    log.push({ emoji: '😴', text: 'Мафія нікого не вбила цієї ночі.' });
  }

  // ── Sheriff private check ──────────────────
  if (room.sheriffTarget) {
    const target = room.players.find(p => p.id === room.sheriffTarget);
    const sheriff = getAlive(room).find(p => p.role === 'sheriff');
    if (target && sheriff) {
      sendPrivate(room, sheriff.id, {
        type: 'sheriff_result',
        name: target.name,
        isMafia: target.role === 'mafia',
      });
    }
  }

  room.nightLog = log;
  room.phase = 'dawn';
  broadcastState(room);
  broadcast(room, { type: 'phase', phase: 'dawn', log });

  if (checkWin(room)) return;

  const dawnDelay = room.settings?.fastMode ? 3000 : 6000;
  delay(room, () => startDay(room), dawnDelay);
}

// ─────────────────────────────────────────────
// DAY PHASE
// ─────────────────────────────────────────────
function startDay(room) {
  clearTimers(room);
  room.phase = 'day';
  room.votes = {};
  room.messages = [];
  // suspects reset at start of night, keep visible during day

  broadcast(room, { type: 'phase', phase: 'day' });
  broadcastState(room);

  const dayDuration = room.settings?.fastMode ? 40 : (room.settings?.dayDuration || 60);
  startCountdown(room, dayDuration * 2, () => endVoting(room));

  // Switch to voting halfway
  delay(room, () => {
    if (room.phase === 'day') {
      room.phase = 'voting';
      broadcast(room, { type: 'phase', phase: 'voting' });
      broadcastState(room);
    }
  }, dayDuration * 1000);
}

function endVoting(room) {
  clearTimers(room);
  const counts = {};
  Object.values(room.votes).forEach(id => { if (id !== null) counts[id] = (counts[id] || 0) + 1; });

  let eliminated = null, max = 0;
  Object.entries(counts).forEach(([id, n]) => { if (n > max) { max = n; eliminated = id; } });

  if (eliminated) {
    const p = room.players.find(p => p.id === eliminated);
    if (p) {
      p.alive = false;
      broadcast(room, {
        type: 'eliminated', id: eliminated, name: p.name, role: p.role,
        message: `⚖️ ${p.name} (${ROLE_INFO[p.role]?.name || p.role}) виключений голосуванням!`,
      });
      // Detective learns role of eliminated player
      const detective = getAlive(room).find(pp => pp.role === 'detective');
      if (detective) {
        sendPrivate(room, detective.id, { type: 'detective_result', name: p.name, role: p.role });
      }
    }
  } else {
    broadcast(room, { type: 'no_elimination', message: '🤷 Голосування не визначило підозрюваного.' });
  }

  if (checkWin(room)) return;
  const nightDelay = room.settings?.fastMode ? 2000 : 3000;
  delay(room, () => startNight(room), nightDelay);
}

// ─────────────────────────────────────────────
// WEBSOCKET HANDLER
// ─────────────────────────────────────────────
wss.on('connection', (ws) => {
  let myId = null;
  let myRoomCode = null;

  const pingInterval = setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.ping(); }, 25000);

  ws.on('close', () => {
    clearInterval(pingInterval);
    const room = rooms.get(myRoomCode);
    if (!room) return;
    const me = room.players.find(p => p.id === myId);
    if (me) {
      me.connected = false; me.ws = null;
      broadcastExcept(room, { type: 'player_disconnected', id: myId, name: me.name }, myId);
      broadcastState(room);
    }
  });

  ws.on('message', (raw) => {
    let msg; try { msg = JSON.parse(raw); } catch { return; }
    const room = rooms.get(myRoomCode);
    const me = room?.players.find(p => p.id === myId);

    switch (msg.type) {

      // ── CREATE ROOM ──────────────────────────
      case 'create_room': {
        const name = (msg.name || 'Гравець').slice(0, 20);
        const code = generateRoomCode();
        const newRoom = createRoom();
        newRoom.code = code;
        myId = 'p_' + Math.random().toString(36).slice(2);
        myRoomCode = code;
        newRoom.players.push({
          id: myId, name, role: null, alive: true, isHost: true, connected: true, ws,
          avatarUrl: String(msg.avatarUrl || '').slice(0, 300),
          tgVerified: !!msg.tgVerified,
        });
        rooms.set(code, newRoom);
        send(ws, { type: 'joined', id: myId, code, isHost: true, botUsername: BOT_USERNAME });
        broadcastState(newRoom);
        break;
      }

      // ── JOIN ROOM ───────────────────────────
      case 'join_room': {
        const code = String(msg.code).trim();
        const joinRoom = rooms.get(code);
        if (!joinRoom) { send(ws, { type: 'error', msg: 'Кімнату не знайдено 🔍' }); return; }

        if (joinRoom.phase !== 'lobby') {
          // Try reconnect
          const existing = joinRoom.players.find(p => p.name === msg.name?.trim() && !p._left);
          if (existing) {
            existing.ws = ws; existing.connected = true;
            if (msg.avatarUrl) existing.avatarUrl = String(msg.avatarUrl).slice(0, 300);
            myId = existing.id; myRoomCode = code;
            send(ws, { type: 'joined', id: myId, code, isHost: existing.isHost, botUsername: BOT_USERNAME, reconnecting: true });
            send(ws, { type: 'your_role', role: existing.role });
            if (existing.role === 'mafia') {
              const team = joinRoom.players.filter(p => p.role === 'mafia').map(p => ({ id: p.id, name: p.name, avatarUrl: p.avatarUrl || '' }));
              send(ws, { type: 'mafia_team', team });
            }
            send(ws, getSnapshot(joinRoom, myId));
            broadcast(joinRoom, { type: 'player_reconnected', id: myId, name: existing.name });
            broadcastState(joinRoom);
            return;
          }
          send(ws, { type: 'error', msg: 'Гра вже розпочалась 🎮' }); return;
        }

        if (joinRoom.players.length >= 20) { send(ws, { type: 'error', msg: 'Кімната заповнена (макс. 20) 😔' }); return; }
        const name = (msg.name || 'Гравець').slice(0, 20);
        if (joinRoom.players.some(p => p.name === name)) { send(ws, { type: 'error', msg: "Це ім'я вже зайнято 👤" }); return; }

        myId = 'p_' + Math.random().toString(36).slice(2);
        myRoomCode = code;
        joinRoom.players.push({
          id: myId, name, role: null, alive: true, isHost: false, connected: true, ws,
          avatarUrl: String(msg.avatarUrl || '').slice(0, 300),
          tgVerified: !!msg.tgVerified,
        });
        send(ws, { type: 'joined', id: myId, code, isHost: false, botUsername: BOT_USERNAME });
        broadcast(joinRoom, { type: 'player_joined', id: myId, name });
        broadcastState(joinRoom);
        break;
      }

      // ── START GAME ──────────────────────────
      case 'start_game': {
        if (!room || !me?.isHost || room.phase !== 'lobby') return;
        if (room.players.length < 4) { send(ws, { type: 'error', msg: 'Потрібно мінімум 4 гравці! 👥' }); return; }

        assignRoles(room);
        room.players.forEach(p => sendPrivate(room, p.id, { type: 'your_role', role: p.role }));

        const mafiaIds = room.players.filter(p => p.role === 'mafia').map(p => ({ id: p.id, name: p.name, avatarUrl: p.avatarUrl || '' }));
        room.players.filter(p => p.role === 'mafia').forEach(p => sendPrivate(room, p.id, { type: 'mafia_team', team: mafiaIds }));

        broadcast(room, { type: 'game_starting' });
        const startDelay = room.settings?.fastMode ? 5000 : 8000;
        delay(room, () => startNight(room), startDelay);
        broadcastState(room);
        break;
      }

      // ── NIGHT ACTION ────────────────────────
      case 'night_action': {
        if (!room || !me?.alive || room.phase !== 'night') return;
        if (room.nightTurn !== me.role) return;
        if (room.nightActed[me.role]) return;

        const targetId = msg.targetId || null;

        if (me.role === 'mafia') {
          room.mafiaVotes[myId] = targetId;
          room.mafiaTarget = resolveMafiaVote(room);
          room.players.filter(p => p.role === 'mafia').forEach(p => send(p.ws, getSnapshot(room, p.id)));
          const aliveMafia = getAlive(room).filter(p => p.role === 'mafia');
          if (!aliveMafia.every(p => room.mafiaVotes[p.id] !== undefined)) {
            send(ws, { type: 'night_acted' }); break;
          }
          room.nightActed['mafia'] = true;

        } else if (me.role === 'prostitute') {
          if (targetId) {
            const t = room.players.find(p => p.id === targetId && p.alive);
            if (!t) return;
            room.prostituteTarget = targetId;
          }
          room.nightActed['prostitute'] = true;

        } else if (me.role === 'sheriff') {
          if (targetId) {
            const t = room.players.find(p => p.id === targetId && p.alive);
            if (!t) return;
            room.sheriffTarget = targetId;
          }
          room.nightActed['sheriff'] = true;

        } else if (me.role === 'doctor') {
          if (targetId) {
            const t = room.players.find(p => p.id === targetId && p.alive);
            if (!t) return;
            room.doctorTarget = targetId;
          }
          room.nightActed['doctor'] = true;

        } else if (me.role === 'detective') {
          // Passive — auto confirm, no target needed
          room.nightActed['detective'] = true;
        }

        send(ws, { type: 'night_acted' });

        const prevTurn = room.nightTurn;
        const nextTurn = findNextNightRole(room, prevTurn);
        if (nextTurn) {
          room.nightTurn = nextTurn;
          delay(room, () => {
            broadcast(room, { type: 'night_turn', from: prevTurn, to: nextTurn });
            broadcastState(room);
          }, 500);
        } else {
          delay(room, () => endNight(room), 1000);
        }
        break;
      }

      // ── CHAT ────────────────────────────────
      case 'chat': {
        if (!room || !me?.alive) return;
        if (room.phase !== 'day' && room.phase !== 'voting') return;
        const text = String(msg.text || '').trim().slice(0, 300);
        if (!text) return;
        const chatMsg = {
          id: Date.now() + Math.random(), playerId: myId, name: me.name, text,
          time: new Date().toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit' }),
        };
        room.messages.push(chatMsg);
        if (room.messages.length > 200) room.messages = room.messages.slice(-200);
        broadcast(room, { type: 'chat', message: chatMsg });
        break;
      }

      // ── VOTE ────────────────────────────────
      case 'vote': {
        if (!room || !me?.alive || room.phase !== 'voting') return;
        room.votes[myId] = msg.targetId || null;
        broadcast(room, { type: 'vote_update', votes: room.votes });
        broadcastState(room);
        const aliveIds = getAlive(room).map(p => p.id);
        if (aliveIds.every(id => room.votes[id] !== undefined)) {
          clearTimers(room);
          delay(room, () => endVoting(room), 500);
        }
        break;
      }

      // ── SUSPECT TOKEN ────────────────────────
      case 'suspect': {
        if (!room || !me?.alive) return;
        if (room.phase !== 'day' && room.phase !== 'voting') return;
        const targetId = msg.targetId;
        if (msg.active) {
          room.suspects[myId] = targetId;
        } else {
          if (room.suspects[myId] === targetId) delete room.suspects[myId];
        }
        // Recompute and broadcast
        const suspectedIds = Object.values(room.suspects).filter(Boolean);
        broadcast(room, { type: 'suspect_update', suspects: suspectedIds });
        break;
      }

      // ── REACTION (dead players) ──────────────
      case 'reaction': {
        if (!room) return;
        const alive = room.players.find(p => p.id === myId)?.alive;
        if (alive) return; // only dead players
        if (room.phase !== 'day' && room.phase !== 'voting') return;
        const emoji = String(msg.emoji || '').slice(0, 8);
        if (!emoji) return;
        broadcast(room, { type: 'reaction', playerId: myId, name: me.name, emoji });
        break;
      }

      // ── UPDATE SETTINGS (host only) ──────────
      case 'update_settings': {
        if (!room || !me?.isHost || room.phase !== 'lobby') return;
        const s = msg.settings || {};
        if (typeof s.fastMode === 'boolean') room.settings.fastMode = s.fastMode;
        if (s.roles && typeof s.roles === 'object') {
          for (const key of ['prostitute', 'detective', 'sheriff', 'doctor']) {
            if (typeof s.roles[key] === 'boolean') room.settings.roles[key] = s.roles[key];
          }
        }
        if (typeof s.dayDuration === 'number') {
          room.settings.dayDuration = Math.max(30, Math.min(300, s.dayDuration));
        }
        broadcastState(room);
        break;
      }

      // ── LEAVE ROOM ──────────────────────────
      case 'leave_room': {
        if (!room) return;
        const lIdx = room.players.findIndex(p => p.id === myId);
        if (lIdx === -1) return;
        const leaving = room.players[lIdx];
        leaving._left = true;
        room.players.splice(lIdx, 1);
        myId = null; myRoomCode = null;
        if (leaving.isHost && room.players.length > 0) {
          room.players[0].isHost = true;
          send(room.players[0].ws, { type: 'promoted_host' });
        }
        if (room.players.length === 0) {
          clearTimers(room);
          if (room._deleteTimer) clearTimeout(room._deleteTimer);
          rooms.delete(room.code);
        } else {
          broadcast(room, { type: 'player_left', id: leaving.id, name: leaving.name });
          broadcastState(room);
        }
        send(ws, { type: 'left_room' });
        break;
      }

      // ── RESTART GAME ────────────────────────
      case 'restart_game': {
        if (!room || !me?.isHost) return;
        if (room.phase !== 'finished' && room.phase !== 'game_over') return;
        if (room._deleteTimer) { clearTimeout(room._deleteTimer); room._deleteTimer = null; }
        clearTimers(room);
        room.phase = 'lobby'; room.night = 0;
        room.messages = []; room.nightLog = []; room.votes = {};
        room.mafiaVotes = {}; room.nightActed = {}; room.suspects = {};
        room.mafiaTarget = null; room.doctorTarget = null;
        room.sheriffTarget = null; room.prostituteTarget = null;
        room.winner = null; room.phaseTime = 0; room.nightTurn = null;
        room.players.forEach(p => { p.role = null; p.alive = true; });
        broadcast(room, { type: 'room_restarted' });
        broadcastState(room);
        break;
      }

      // ── KICK (host) ─────────────────────────
      case 'kick': {
        if (!room || !me?.isHost || room.phase !== 'lobby') return;
        const idx = room.players.findIndex(p => p.id === msg.targetId && !p.isHost);
        if (idx === -1) return;
        const kicked = room.players[idx];
        send(kicked.ws, { type: 'kicked' });
        room.players.splice(idx, 1);
        broadcast(room, { type: 'player_left', id: msg.targetId, name: kicked.name });
        broadcastState(room);
        break;
      }

      case 'ping': send(ws, { type: 'pong' }); break;
    }
  });
});

const PORT = process.env.PORT || 3000;
server.listen(PORT, () => console.log(`🎭 Mafia server running → http://localhost:${PORT}`));
