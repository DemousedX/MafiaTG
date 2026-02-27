const express = require('express');
const WebSocket = require('ws');
const http = require('http');
const path = require('path');
try { require('dotenv').config(); } catch {}

const BOT_USERNAME = process.env.BOT_USERNAME || '';
const ROOM_TTL = 3 * 60 * 1000; // 3 хв після кінця гри

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

app.use(express.static(path.join(__dirname, 'public')));

// ── Keep-alive ping (для UptimeRobot / Render) ──
app.get('/ping', (req, res) => {
  res.json({ status: 'ok', uptime: Math.floor(process.uptime()) });
});

// ── Health check ─────────────────────────────────
app.get('/health', (req, res) => {
  res.json({ status: 'ok', rooms: rooms.size });
});

// ── Stats API (для Telegram бота) ─────────────────
let gamesPlayed = 0;

app.get('/api/stats', (req, res) => {
  let totalPlayers = 0;
  rooms.forEach(r => {
    totalPlayers += r.players.filter(p => p.connected).length;
  });
  res.json({ rooms: rooms.size, players: totalPlayers, games: gamesPlayed });
});

// Serve index.html for all other routes
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// ─────────────────────────────────────────────
// GAME STATE
// ─────────────────────────────────────────────
const rooms = new Map();

function generateRoomCode() {
  let code;
  do { code = String(Math.floor(10000 + Math.random() * 90000)); }
  while (rooms.has(code));
  return code;
}

function getRolePool(count) {
  if (count <= 5)  return ['mafia', 'sheriff', ...Array(count - 2).fill('civilian')];
  if (count <= 8)  return ['mafia', 'mafia', 'sheriff', 'doctor', ...Array(count - 4).fill('civilian')];
  if (count <= 12) return ['mafia', 'mafia', 'mafia', 'sheriff', 'doctor', ...Array(count - 5).fill('civilian')];
  if (count <= 16) return ['mafia', 'mafia', 'mafia', 'mafia', 'sheriff', 'doctor', ...Array(count - 6).fill('civilian')];
  return ['mafia', 'mafia', 'mafia', 'mafia', 'mafia', 'sheriff', 'doctor', ...Array(count - 7).fill('civilian')];
}

function shuffle(arr) {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

const ROLE_INFO = {
  mafia:    { emoji: '🔫', name: 'Мафія',     color: '#e53e3e', desc: 'Кожну ніч обирайте жертву. Перемагаєте, коли рівні мирним.' },
  sheriff:  { emoji: '⭐', name: 'Шериф',     color: '#d69e2e', desc: 'Кожну ніч перевіряйте одного гравця — мафія чи ні.' },
  doctor:   { emoji: '💊', name: 'Лікар',     color: '#38a169', desc: 'Кожну ніч рятуйте одного гравця від смерті. Можна себе!' },
  civilian: { emoji: '🏘️', name: 'Мирний',   color: '#4a90d9', desc: 'Вдень переконуйте та голосуйте проти мафії.' },
};

// ─────────────────────────────────────────────
// BROADCAST HELPERS
// ─────────────────────────────────────────────
function send(ws, msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

function broadcast(room, msg) {
  room.players.forEach(p => send(p.ws, msg));
}

function broadcastExcept(room, msg, excludeId) {
  room.players.filter(p => p.id !== excludeId).forEach(p => send(p.ws, msg));
}

function sendPrivate(room, id, msg) {
  const p = room.players.find(p => p.id === id);
  if (p) send(p.ws, msg);
}

// ─────────────────────────────────────────────
// STATE SNAPSHOT
// ─────────────────────────────────────────────
function getSnapshot(room, forPlayerId) {
  const me = room.players.find(p => p.id === forPlayerId);
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
      id: p.id,
      name: p.name,
      alive: p.alive,
      isHost: p.isHost,
      connected: p.connected,
      voted: room.votes[p.id] !== undefined,
      avatarUrl: p.avatarUrl || '',
      tgVerified: p.tgVerified || false,
    })),
    messages: room.messages,
    nightLog: room.nightLog,
    votes: room.phase === 'voting' ? room.votes : {},
    mafiaVotes: me?.role === 'mafia' ? room.mafiaVotes : {},
    nightActed: room.nightActed,
    winner: room.winner,
    revealedRoles: room.winner ? room.players.map(p => ({ id: p.id, role: p.role })) : [],
  };
}

function broadcastState(room) {
  room.players.forEach(p => send(p.ws, getSnapshot(room, p.id)));
}

// ─────────────────────────────────────────────
// GAME LOGIC
// ─────────────────────────────────────────────
function createRoom() {
  return {
    code: '', phase: 'lobby', night: 0,
    players: [], messages: [], nightLog: [],
    votes: {}, mafiaVotes: {}, nightActed: {},
    mafiaTarget: null, doctorTarget: null, sheriffTarget: null,
    phaseTime: 0, winner: null,
    _timers: [],
  };
}

function clearTimers(room) {
  room._timers.forEach(t => clearTimeout(t));
  room._timers = [];
}

function delay(room, fn, ms) {
  const t = setTimeout(fn, ms);
  room._timers.push(t);
  return t;
}

function startCountdown(room, seconds, onEnd) {
  room.phaseTime = seconds;
  const tick = () => {
    broadcast(room, { type: 'tick', time: room.phaseTime });
    if (room.phaseTime <= 0) { onEnd(); return; }
    room.phaseTime--;
    const t = setTimeout(tick, 1000);
    room._timers.push(t);
  };
  tick();
}

function assignRoles(room) {
  const pool = shuffle(getRolePool(room.players.length));
  room.players.forEach((p, i) => {
    p.role = pool[i];
    p.alive = true;
  });
}

function getAlive(room) { return room.players.filter(p => p.alive); }
function getMafia(room) { return getAlive(room).filter(p => p.role === 'mafia'); }

function checkWin(room) {
  const alive = getAlive(room);
  const mafiaCount = alive.filter(p => p.role === 'mafia').length;
  const civCount = alive.filter(p => p.role !== 'mafia').length;

  if (mafiaCount === 0) { endGame(room, 'civilians'); return true; }
  if (mafiaCount >= civCount) { endGame(room, 'mafia'); return true; }
  return false;
}

function endGame(room, winner) {
  clearTimers(room);
  room.phase = 'game_over';
  room.winner = winner;
  gamesPlayed++;
  broadcastState(room);
  broadcast(room, {
    type: 'game_over',
    winner,
    message: winner === 'civilians'
      ? '🎉 Мирні перемогли! Мафія знищена!'
      : '🔫 Мафія перемогла! Місто захоплено!',
    players: room.players.map(p => ({ id: p.id, name: p.name, role: p.role, alive: p.alive, avatarUrl: p.avatarUrl || '', tgVerified: p.tgVerified || false })),
  });
  // Перевести в стан finished; видалити після ROOM_TTL якщо не перезапущено
  room.phase = 'finished';
  room._deleteTimer = setTimeout(() => {
    if (rooms.has(room.code)) {
      rooms.delete(room.code);
      console.log(`[room] ${room.code} deleted after game`);
    }
  }, ROOM_TTL);
}

// ─── NIGHT ───────────────────────────────────
function startNight(room) {
  clearTimers(room);
  room.phase = 'night';
  room.night++;
  room.mafiaTarget = null;
  room.doctorTarget = null;
  room.sheriffTarget = null;
  room.mafiaVotes = {};
  room.nightActed = {};
  room.nightLog = [];

  broadcast(room, { type: 'phase', phase: 'night', night: room.night });
  broadcastState(room);

  startCountdown(room, 30, () => endNight(room));
}

function allNightActed(room) {
  const alive = getAlive(room);
  const hasMafia   = alive.some(p => p.role === 'mafia');
  const hasSheriff = alive.some(p => p.role === 'sheriff');
  const hasDoctor  = alive.some(p => p.role === 'doctor');

  // Мафія готова якщо всі проголосували (target OR skip = nightActed['mafia'])
  const mafiaOk   = !hasMafia   || room.nightActed['mafia'];
  const sheriffOk = !hasSheriff || room.nightActed['sheriff'];
  const doctorOk  = !hasDoctor  || room.nightActed['doctor'];

  return mafiaOk && sheriffOk && doctorOk;
}

function resolveMafiaVote(room) {
  const counts = {};
  Object.values(room.mafiaVotes).forEach(id => { counts[id] = (counts[id] || 0) + 1; });
  let best = null, max = 0;
  Object.entries(counts).forEach(([id, n]) => { if (n > max) { max = n; best = id; } });
  return best;
}

function endNight(room) {
  clearTimers(room);
  const log = [];

  // Resolve mafia target from votes
  if (Object.keys(room.mafiaVotes).length > 0) {
    room.mafiaTarget = resolveMafiaVote(room);
  }

  // Mafia kill
  if (room.mafiaTarget) {
    const target = room.players.find(p => p.id === room.mafiaTarget);
    if (target?.alive) {
      if (room.doctorTarget === room.mafiaTarget) {
        log.push({ emoji: '🏥', text: `Лікар врятував гравця цієї ночі!` });
      } else {
        target.alive = false;
        log.push({ emoji: '💀', text: `${target.name} був вбитий мафією!` });
      }
    }
  } else {
    log.push({ emoji: '😴', text: 'Мафія нікого не вбила цієї ночі.' });
  }

  // Sheriff check — private
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

  delay(room, () => startDay(room), 6000);
}

// ─── DAY ─────────────────────────────────────
function startDay(room) {
  clearTimers(room);
  room.phase = 'day';
  room.votes = {};
  room.messages = [];

  broadcast(room, { type: 'phase', phase: 'day' });
  broadcastState(room);

  // After 60s → switch to voting
  startCountdown(room, 120, () => endVoting(room));

  delay(room, () => {
    if (room.phase === 'day') {
      room.phase = 'voting';
      broadcast(room, { type: 'phase', phase: 'voting' });
      broadcastState(room);
    }
  }, 60000);
}

function endVoting(room) {
  clearTimers(room);

  const counts = {};
  Object.values(room.votes).forEach(id => {
    if (id !== null) counts[id] = (counts[id] || 0) + 1;
  });

  let eliminated = null, max = 0;
  Object.entries(counts).forEach(([id, n]) => { if (n > max) { max = n; eliminated = id; } });

  if (eliminated) {
    const p = room.players.find(p => p.id === eliminated);
    if (p) {
      p.alive = false;
      broadcast(room, {
        type: 'eliminated',
        id: eliminated,
        name: p.name,
        role: p.role,
        message: `⚖️ ${p.name} (${ROLE_INFO[p.role].name}) виключений голосуванням!`,
      });
    }
  } else {
    broadcast(room, { type: 'no_elimination', message: '🤷 Голосування не визначило підозрюваного.' });
  }

  if (checkWin(room)) return;
  delay(room, () => startNight(room), 3000);
}

// ─────────────────────────────────────────────
// WEBSOCKET HANDLER
// ─────────────────────────────────────────────
wss.on('connection', (ws) => {
  let myId = null;
  let myRoomCode = null;

  // Keep-alive ping
  const pingInterval = setInterval(() => {
    if (ws.readyState === WebSocket.OPEN) ws.ping();
  }, 25000);

  ws.on('close', () => {
    clearInterval(pingInterval);
    const room = rooms.get(myRoomCode);
    if (!room) return;
    const me = room.players.find(p => p.id === myId);
    if (me) {
      me.connected = false;
      me.ws = null;
      broadcastExcept(room, { type: 'player_disconnected', id: myId, name: me.name }, myId);
      broadcastState(room);
    }
  });

  ws.on('message', (raw) => {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }

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
          id: myId, name, role: null,
          alive: true, isHost: true,
          connected: true, ws,
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

        if (!joinRoom) {
          send(ws, { type: 'error', msg: 'Кімнату не знайдено 🔍' }); return;
        }
        if (joinRoom.phase !== 'lobby') {
          // Try reconnect by name (allow in finished state too for results viewing)
          const existing = joinRoom.players.find(p => p.name === msg.name?.trim() && !p._left);
          if (existing) {
            existing.ws = ws;
            existing.connected = true;
            // Оновити аватар якщо передано новий
            if (msg.avatarUrl) existing.avatarUrl = String(msg.avatarUrl).slice(0, 300);
            myId = existing.id;
            myRoomCode = code;
            send(ws, { type: 'joined', id: myId, code, isHost: existing.isHost, botUsername: BOT_USERNAME, reconnecting: true });
            // Resend private role info
            send(ws, { type: 'your_role', role: existing.role });
            if (existing.role === 'mafia') {
              const mafiaIds = joinRoom.players.filter(p => p.role === 'mafia').map(p => ({ id: p.id, name: p.name, avatarUrl: p.avatarUrl||'' }));
              send(ws, { type: 'mafia_team', team: mafiaIds });
            }
            send(ws, getSnapshot(joinRoom, myId));
            broadcast(joinRoom, { type: 'player_reconnected', id: myId, name: existing.name });
            broadcastState(joinRoom);
            return;
          }
          send(ws, { type: 'error', msg: 'Гра вже розпочалась 🎮' }); return;
        }
        if (joinRoom.players.length >= 20) {
          send(ws, { type: 'error', msg: 'Кімната заповнена (макс. 20) 😔' }); return;
        }

        const name = (msg.name || 'Гравець').slice(0, 20);
        if (joinRoom.players.some(p => p.name === name)) {
          send(ws, { type: 'error', msg: 'Це ім\'я вже зайнято 👤' }); return;
        }

        myId = 'p_' + Math.random().toString(36).slice(2);
        myRoomCode = code;

        joinRoom.players.push({
          id: myId, name, role: null,
          alive: true, isHost: false,
          connected: true, ws,
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
        if (room.players.length < 4) {
          send(ws, { type: 'error', msg: 'Потрібно мінімум 4 гравці! 👥' }); return;
        }

        assignRoles(room);
        // Tell each player their role privately
        room.players.forEach(p => {
          sendPrivate(room, p.id, { type: 'your_role', role: p.role });
        });

        // Share mafia team info
        const mafiaIds = room.players.filter(p => p.role === 'mafia').map(p => ({ id: p.id, name: p.name }));
        room.players.filter(p => p.role === 'mafia').forEach(p => {
          sendPrivate(room, p.id, { type: 'mafia_team', team: mafiaIds });
        });

        broadcast(room, { type: 'game_starting' });
        delay(room, () => startNight(room), 3000);
        broadcastState(room);
        break;
      }

      // ── NIGHT ACTION ────────────────────────
      case 'night_action': {
        if (!room || !me?.alive || room.phase !== 'night') return;
        const targetId = msg.targetId || null; // null = пропустити

        if (me.role === 'mafia') {
          // Мафія може пропустити (targetId === null)
          room.mafiaVotes[myId] = targetId;
          room.mafiaTarget = resolveMafiaVote(room);
          const mafiaTeam = room.players.filter(p => p.role === 'mafia');
          mafiaTeam.forEach(p => send(p.ws, getSnapshot(room, p.id)));
          // Якщо всі мафіозі проголосували (включно зі skip) — мафія готова
          const aliveMafia = getAlive(room).filter(p => p.role === 'mafia');
          const allMafiaVoted = aliveMafia.every(p => room.mafiaVotes[p.id] !== undefined);
          if (allMafiaVoted) room.nightActed['mafia'] = true;
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
        }

        send(ws, { type: 'night_acted' });
        if (allNightActed(room)) {
          clearTimers(room);
          delay(room, () => endNight(room), 500);
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
          id: Date.now() + Math.random(),
          playerId: myId,
          name: me.name,
          text,
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
        room.votes[myId] = msg.targetId || null; // null = skip
        broadcast(room, { type: 'vote_update', votes: room.votes });
        broadcastState(room);

        // Check if all alive voted
        const aliveIds = getAlive(room).map(p => p.id);
        const allVoted = aliveIds.every(id => room.votes[id] !== undefined);
        if (allVoted) {
          clearTimers(room);
          delay(room, () => endVoting(room), 500);
        }
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
        // Cancel delete timer
        if (room._deleteTimer) { clearTimeout(room._deleteTimer); room._deleteTimer = null; }
        // Reset room to lobby
        clearTimers(room);
        room.phase = 'lobby';
        room.night = 0;
        room.messages = [];
        room.nightLog = [];
        room.votes = {};
        room.mafiaVotes = {};
        room.nightActed = {};
        room.mafiaTarget = null;
        room.doctorTarget = null;
        room.sheriffTarget = null;
        room.winner = null;
        room.phaseTime = 0;
        // Reset player roles/alive
        room.players.forEach(p => { p.role = null; p.alive = true; });
        broadcast(room, { type: 'room_restarted' });
        broadcastState(room);
        break;
      }

      // ── RESTART GAME ────────────────────────
      case 'restart_game': {
        if (!room || !me?.isHost) return;
        if (room._deleteTimer) { clearTimeout(room._deleteTimer); room._deleteTimer = null; }
        clearTimers(room);
        room.phase = 'lobby'; room.night = 0;
        room.messages = []; room.nightLog = []; room.votes = {};
        room.mafiaVotes = {}; room.nightActed = {};
        room.mafiaTarget = null; room.doctorTarget = null;
        room.sheriffTarget = null; room.winner = null; room.phaseTime = 0;
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

      // ── PING ────────────────────────────────
      case 'ping': send(ws, { type: 'pong' }); break;
    }
  });
});

const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
  console.log(`🎭 Mafia server running → http://localhost:${PORT}`);
});
